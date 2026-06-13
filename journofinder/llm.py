"""LLM 客户端（OpenAI 兼容）+ 记者语境的 prompts。

路由：apodex-* 模型走 Apodex，其余走 OpenRouter。
模型分工：
  relevance 打分 → deepseek（便宜批量）
  tier 分层 / pitch angle → claude-sonnet（判断任务，旗舰）
  记者补召 / 联系方式深挖 → apodex-deepresearch（强搜索）

改编自 shanda/pitchfinder/pitchfinder/llm.py：prompt 从「创作者」改成「记者/媒体」。
Apodex（原 MiroMind 升级版）走 APODEX_API_KEY。
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from typing import Any

from openai import OpenAI

from . import env

logger = logging.getLogger("journofinder.llm")

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_RELEVANCE_MODEL = "deepseek/deepseek-chat-v3.1"
DEFAULT_PITCH_MODEL = "anthropic/claude-sonnet-4.6"
DEFAULT_DEEPDIVE_MODEL = "apodex-1-0-deepresearch"
# 单次 LLM 调用硬超时（秒）—— 防 Apodex SSE 流无限 hang（曾卡死 2h+）。
# 正常深挖远小于此；超时即按瞬时错误重试/放弃，不阻塞整条 enrich。
LLM_TIMEOUT_SECONDS = 240

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```\s*$", re.MULTILINE)
_BAD_ESCAPE_RE = re.compile(r'\\(?!["\\/bfnrtu])')


def _client_for_model(model: str) -> OpenAI:
    """apodex-* → Apodex；其余 → OpenRouter。"""
    if model.startswith("apodex"):
        api_key = env.get("APODEX_API_KEY")
        if not api_key:
            raise RuntimeError("APODEX_API_KEY 未设置")
        base_url = env.get("APODEX_BASE_URL", "https://api.apodex.ai/v1")
        return OpenAI(api_key=api_key, base_url=base_url)

    api_key = env.get("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY 未设置")
    base_url = env.get("OPENROUTER_BASE_URL", DEFAULT_BASE_URL)
    headers = {}
    if env.get("OPENROUTER_REFERER"):
        headers["HTTP-Referer"] = env.get("OPENROUTER_REFERER")
    if env.get("OPENROUTER_TITLE"):
        headers["X-Title"] = env.get("OPENROUTER_TITLE")
    return OpenAI(api_key=api_key, base_url=base_url, default_headers=headers or None)


def relevance_model() -> str:
    return env.get("JOURNO_RELEVANCE_MODEL", DEFAULT_RELEVANCE_MODEL)


def pitch_model() -> str:
    return env.get("JOURNO_PITCH_MODEL", DEFAULT_PITCH_MODEL)


def deepdive_model() -> str:
    return env.get("JOURNO_DEEPDIVE_MODEL", DEFAULT_DEEPDIVE_MODEL)


def apodex_available() -> bool:
    return bool(env.get("APODEX_API_KEY"))


# ---------- JSON 解析（容错） ----------

def _strip_fences(text: str) -> str:
    return _FENCE_RE.sub("", text).strip()


def _loads_lenient(s: str) -> Any:
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return json.loads(_BAD_ESCAPE_RE.sub(r"\\\\", s))


def _parse_json(text: str) -> Any:
    cleaned = _strip_fences(text)
    if not cleaned:
        raise ValueError("空响应")
    try:
        return _loads_lenient(cleaned)
    except json.JSONDecodeError:
        m = re.search(r"(\{.*\}|\[.*\])", cleaned, re.DOTALL)
        if m:
            return _loads_lenient(m.group(1))
        raise


def _call_once(model: str, prompt: str, max_tokens: int) -> str:
    client = _client_for_model(model)
    if model.startswith("apodex"):  # Apodex 用流式 SSE
        stream = client.chat.completions.create(
            model=model, max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}], stream=True,
            timeout=LLM_TIMEOUT_SECONDS,
        )
        buf: list[str] = []
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta and getattr(delta, "content", None):
                buf.append(delta.content)
        return "".join(buf)
    resp = client.chat.completions.create(
        model=model, max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
        timeout=LLM_TIMEOUT_SECONDS,
    )
    return resp.choices[0].message.content or ""


def call_json(model: str, prompt: str, max_tokens: int = 1024, max_retries: int = 2) -> Any:
    """期望 JSON 输出的 LLM 调用，空响应/解析失败/瞬时错误时退避重试。失败抛出。"""
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            text = _call_once(model, prompt, max_tokens)
            if not text.strip():
                raise ValueError("模型返回空")
            return _parse_json(text)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < max_retries:
                delay = (2 ** attempt) + random.uniform(0, 0.5)
                logger.warning("LLM 调用 %d/%d 失败 (model=%s): %s — %.1fs 后重试",
                               attempt + 1, max_retries + 1, model, exc, delay)
                time.sleep(delay)
            else:
                logger.warning("LLM 调用重试耗尽 (model=%s): %s", model, exc)
    assert last_exc is not None
    raise last_exc


# ---------- 1. relevance 打分（记者对品牌空间的相关度） ----------

def score_relevance(brand_summary: str, journalist: dict) -> dict:
    """这个记者是否覆盖我的 sector/competitors？打 0-100。

    journalist：{name, outlet, signal（近期标题拼接）, article_count}
    """
    signal = (journalist.get("signal") or "")[:1500]
    prompt = f"""Score how relevant this JOURNALIST is for a PR outreach list, based on their recent coverage.

Brand / launch context:
{brand_summary}

Journalist: {journalist.get('name')} ({journalist.get('outlet') or 'unknown outlet'})
Recent article headlines by this journalist ({journalist.get('article_count', 0)} in window):
{signal}

Score 0-100 on TOPICAL fit AND source quality together:
- 90+: an individual reporter at an established outlet who covers THIS exact space
  (the sector, the category, the competitors) deeply and specifically.
- 70-89: a credible reporter on an adjacent beat; they would plausibly cover this.
- 50-69: tangential — covers the broad area but not this specific space.
- <50: not relevant.

Penalise hard (cap at 40) if this looks like a CONTENT FARM / SEO aggregator / wire
re-poster rather than a real reporter worth pitching: keyword-stuffed roundups,
churned daily briefs with no individual point of view, or generic rewrites.

Return JSON only: {{"score": <int>, "reason": "<one sentence; note if farm/wire>"}}"""
    result = call_json(relevance_model(), prompt, max_tokens=256)
    if not isinstance(result, dict):
        return {"score": 0, "reason": "non-dict response"}
    try:
        score = int(result.get("score", 0))
    except (TypeError, ValueError):
        score = 0
    return {"score": max(0, min(100, score)), "reason": str(result.get("reason", ""))[:500]}


# ---------- 2. tier 分层（A / B / drop） ----------

def _build_tier_prompt(brand_summary: str, competitors: list[str] | None, batch: list[dict]) -> str:
    rows = "\n".join(
        f'{c["journalist_id"]}\t{c["name"]} — {c.get("outlet") or "unknown"} '
        f'[{c.get("outlet_uri") or "no-domain"}] · {c.get("article_count", 0)} articles · '
        f'recent: {c.get("signal", "")[:200]}'
        for c in batch
    )
    comp_line = (
        "Brand competitors (DROP these companies' OWNED media / staff bylines): "
        + ", ".join(competitors) + "\n\n" if competitors else ""
    )
    return f"""You are triaging JOURNALISTS for a founder's press outreach list.

GOAL: find NEUTRAL, INDEPENDENT third-party reporters and editors — at real
editorial outlets or credible independent newsletters — who recently covered this
space and could write about this launch.

Brand / launch context:
{brand_summary}

{comp_line}For EACH journalist below, assign an outreach tier:
- "A": high relevance AND high confidence — a reporter at an established editorial
  outlet whose beat maps directly to this launch. Strongly recommend pitching.
- "B": moderate / narrower relevance, or lower confidence, but still a genuine,
  on-topic, independent reporter worth a pitch. Recall-first: unsure between B and
  drop on an INDEPENDENT reporter → pick B.
- "drop": see the hard rules below.

ALWAYS "drop" (on-topic wording is NOT enough to save these):
1. NOT NEUTRAL — the byline belongs to a company/vendor's OWNED blog or marketing
   site that exists to promote its own product, OR a commercial lead-gen / affiliate
   "best X" directory that monetises this exact category. We can't partner with
   commercially self-interested sources to promote us.
2. COMPETITOR — a listed competitor, any direct competitor, or their owned media/staff.
3. content farm / SEO aggregator / wire re-poster / generic keyword rewrite / off-topic.

KEEP (A/B) independent reporters, editors, columnists and newsletter authors at real
editorial outlets EVEN IF the outlet runs ads. The test is "neutral independent
journalist" vs "company marketing its own product/category". Use the [domain] as a
signal: a vendor/product/comparison domain → likely drop; an editorial outlet
(techcrunch, theverge, a personal newsletter) → likely keep.

Journalists (id<TAB>name — outlet [domain] · count · recent):
{rows}

Return JSON only, one object per journalist:
[{{"journalist_id": <int>, "tier": "A"|"B"|"drop", "rationale": "<one short sentence; if drop, say why: vendor/competitor/farm>"}}]"""


def classify_tiers(
    brand_summary: str,
    journalists: list[dict],
    model: str | None = None,
    batch_size: int = 10,
    competitors: list[str] | None = None,
) -> dict[int, dict]:
    """每个记者分 A / B / drop。批处理省钱，未返回的默认 B（recall-first）。"""
    out: dict[int, dict] = {}
    chosen = model or pitch_model()
    for start in range(0, len(journalists), batch_size):
        batch = journalists[start:start + batch_size]
        prompt = _build_tier_prompt(brand_summary, competitors, batch)
        try:
            result = call_json(chosen, prompt, max_tokens=1400)
        except Exception as exc:  # noqa: BLE001
            logger.warning("classify_tiers 批失败 (%s): %s", chosen, exc)
            result = []
        if isinstance(result, list):
            for e in result:
                if not isinstance(e, dict):
                    continue
                try:
                    jid = int(e.get("journalist_id"))
                except (TypeError, ValueError):
                    continue
                tier = str(e.get("tier", "B")).strip().upper()
                tier = {"A": "A", "B": "B", "DROP": "drop"}.get(tier, "B")
                out[jid] = {"tier": tier, "rationale": str(e.get("rationale", ""))[:300]}
        for c in batch:
            out.setdefault(c["journalist_id"], {"tier": "B", "rationale": "(defaulted)"})
    return out


# ---------- 3. pitch angle（founder → journalist） ----------

def generate_pitch_package(
    brand_summary: str, journalist_name: str, outlet: str | None,
    top_articles: list[dict], do_not: list[str] | None = None,
) -> dict:
    """一次调用同时产出：2-3 个 pitch angle + 一封可直接发的完整 pitch（subject + body）。

    返回 {"angles": [{angle, references_article}], "pitch": {"subject", "body"}}。
    """
    lines: list[str] = []
    for i, it in enumerate(top_articles[:3], 1):
        date_s = (it.get("published_at") or "")[:10]
        title = it.get("title", "")
        summary = (it.get("body") or "")[:300]
        lines.append(f'{i}. "{title}" ({date_s}) — {summary}')
    block = "\n".join(lines) if lines else "(no recent relevant articles)"
    guard = ("\nHard guardrails (do NOT violate):\n- " + "\n- ".join(do_not)) if do_not else ""

    prompt = f"""You are a world-class PR strategist and storyteller pitching a product launch
to a specific JOURNALIST. Produce both sharp angles AND a ready-to-send pitch email.

Brand / launch context:
{brand_summary}

Journalist: {journalist_name} ({outlet or 'unknown outlet'})
Their recent relevant articles:
{block}
{guard}

PART 1 — angles: 2-3 specific story hooks. Each must reference a SPECIFIC argument/story/theme
from THIS journalist's recent work and show how our launch extends, challenges, complicates, or
gives a fresh data point to it. Concrete and specific; no generic "this aligns with your interests".

PART 2 — pitch: a compelling, ready-to-send cold pitch email to this journalist. It must make the
case for WHY THEY, SPECIFICALLY, MUST COVER THIS NOW. Requirements:
- subject: punchy, specific, newsworthy (<= 12 words). Not clickbait.
- body: 110-170 words. Open with a hook tied to their recent piece (show you read it). State the
  single most newsworthy claim (the launch + the one number/fact that matters). Explain why it
  matters to THEIR beat and audience, and why it's timely. One clear, low-friction CTA (offer
  exclusive/early access/data/interview). Confident, concrete, no hype, no fabricated numbers or
  customers. First person ("we"), addressed to the journalist by first name.

Return JSON only:
{{
  "angles": [{{"angle": "...", "references_article": "<article title>"}}],
  "pitch": {{"subject": "...", "body": "..."}}
}}"""
    try:
        result = call_json(pitch_model(), prompt, max_tokens=1600)
    except Exception as exc:  # noqa: BLE001
        logger.warning("generate_pitch_package 失败: %s", exc)
        return {"angles": [], "pitch": {}}
    if not isinstance(result, dict):
        return {"angles": [], "pitch": {}}
    angles = []
    for e in (result.get("angles") or []):
        if isinstance(e, dict) and e.get("angle"):
            angles.append({"angle": str(e["angle"]), "references_article": str(e.get("references_article", ""))})
    pitch = result.get("pitch") or {}
    pitch = {"subject": str(pitch.get("subject", "")), "body": str(pitch.get("body", ""))} if isinstance(pitch, dict) else {}
    return {"angles": angles, "pitch": pitch}


# ---------- 4. Apodex 记者补召（强搜索） ----------

def apodex_find_journalists(themes: list[str], competitors: list[str], n: int = 15) -> list[dict]:
    """用 Apodex 深搜补召 NewsAPI 未索引的独立记者/newsletter 作者。

    返回 [{name, outlet, outlet_uri, article_title, article_url}]。失败/无 key → []。
    """
    if not apodex_available():
        return []
    topics = ", ".join([*themes, *competitors][:10])
    prompt = f"""Find up to {n} INDIVIDUAL journalists, reporters, columnists, or independent
newsletter authors who have published articles in roughly the last 60 days about:
{topics}

Prioritise named individuals at real editorial outlets and well-known independent
newsletters/Substacks. Skip wire services, company blogs, and content farms.

For each, give the most relevant recent article you can find.
Return JSON only (no prose):
[
  {{"name": "<journalist full name>", "outlet": "<publication>", "outlet_uri": "<domain like techcrunch.com>", "article_title": "<recent article>", "article_url": "<url>"}}
]"""
    try:
        result = call_json(deepdive_model(), prompt, max_tokens=2048)
    except Exception as exc:  # noqa: BLE001
        logger.warning("apodex_find_journalists 失败: %s", exc)
        return []
    if not isinstance(result, list):
        return []
    out = []
    for e in result:
        if isinstance(e, dict) and (e.get("name") or "").strip():
            out.append({
                "name": str(e.get("name")).strip(),
                "outlet": str(e.get("outlet", "")).strip() or None,
                "outlet_uri": str(e.get("outlet_uri", "")).strip() or None,
                "article_title": str(e.get("article_title", "")).strip(),
                "article_url": str(e.get("article_url", "")).strip(),
            })
    return out


# ---------- 5. 联系方式深挖（Tier-A，Apodex） ----------

def deepdive_contact(journalist_name: str, outlet: str | None, signal: str) -> dict:
    """对 Tier-A 记者深挖 verified 联系方式 + 近期 sharp quotes。

    返回 {email, twitter, personal_url, recent_quotes: [{quote, date, source}]}。
    """
    if not apodex_available():
        return {}
    prompt = f"""Research this journalist and return verified contact + recent context.

Journalist: {journalist_name}
Outlet: {outlet or 'unknown'}
Recent coverage signal: {signal[:400]}

Find, only if you can verify from real sources (NEVER guess or construct any handle/address):
- their LinkedIn profile URL (https://www.linkedin.com/in/...)
- their Twitter/X handle
- their professional email (or the outlet's verified byline-contact email)
- their personal site / staff page / author page
- 2-3 SHARP, specific quotes or claims from their recent articles, each with date and source url

Return JSON only:
{{
  "linkedin": "<full LinkedIn URL or null>",
  "twitter": "<@handle or null>",
  "email": "<or null>",
  "personal_url": "<or null>",
  "recent_quotes": [{{"quote": "...", "date": "YYYY-MM-DD", "source": "<url>"}}]
}}"""
    try:
        result = call_json(deepdive_model(), prompt, max_tokens=2048)
    except Exception as exc:  # noqa: BLE001
        logger.warning("deepdive_contact 失败 (%s): %s", journalist_name, exc)
        return {}
    if not isinstance(result, dict):
        return {}
    quotes = result.get("recent_quotes")
    return {
        "linkedin": (result.get("linkedin") or None),
        "email": (result.get("email") or None),
        "twitter": (result.get("twitter") or None),
        "personal_url": (result.get("personal_url") or None),
        "recent_quotes": quotes if isinstance(quotes, list) else [],
    }


# ---------- 6. 从网搜结果抽联系方式（便宜模型，阶段 1） ----------

def extract_contact_from_search(name: str, outlet: str | None, rows: list[dict]) -> dict:
    """从 Brave/Querit 搜索结果（title/url/snippet）里判定哪条联系方式属于这位记者。

    用便宜模型（relevance_model / deepseek）。严格：只在结果明确属于本人时返回；
    任何不确定一律 null —— 绝不猜测、绝不拼造地址或 handle。
    返回 {linkedin, twitter, email, personal_url}（缺失为 None）。
    """
    if not rows:
        return {}
    listing = "\n".join(
        f'{i}. {r.get("title","")} | {r.get("url","")} | {r.get("snippet","")[:200]}'
        for i, r in enumerate(rows[:20], 1)
    )
    prompt = f"""You are extracting verified contact details for ONE specific journalist
from web search results. Be strict: only return a value when the result CLEARLY belongs
to THIS person (their full name matches the profile/byline). If unsure, return null.
NEVER guess, infer, or construct an email address or handle.

Journalist: {name}{f" ({outlet})" if outlet else ""}

Search results:
{listing}

Return JSON only:
{{
  "linkedin": "<full https://www.linkedin.com/in/... URL that is THIS journalist, else null>",
  "twitter": "<@handle or https://x.com/handle that is THIS journalist, else null>",
  "email": "<professional email explicitly shown for THIS journalist, else null>",
  "personal_url": "<their personal site / staff author page, else null>"
}}"""
    try:
        result = call_json(relevance_model(), prompt, max_tokens=400)
    except Exception as exc:  # noqa: BLE001
        logger.warning("extract_contact_from_search 失败 (%s): %s", name, exc)
        return {}
    if not isinstance(result, dict):
        return {}

    def _clean(v: Any) -> str | None:
        s = str(v).strip() if v else ""
        return s or None

    return {
        "linkedin": _clean(result.get("linkedin")),
        "twitter": _clean(result.get("twitter")),
        "email": _clean(result.get("email")),
        "personal_url": _clean(result.get("personal_url")),
    }
