"""补召源 —— 捞 NewsAPI.ai 没索引到的独立记者 / newsletter 作者（recall 优先）。

两条路径，缺 key 自动跳过：
  1. MiroMind 深搜：结构化返回记者名 + 媒体 + 近期文章（强搜索，主补召）
  2. Querit / Brave 网搜：拿 title/snippet/url，再用便宜模型抽取记者署名

输出与 newsapi_ai.fetch_articles 同形（article dict），直接汇入同一聚合流程：
  {url, title, body, source_title, source_uri, published_at, sentiment,
   keyword_matched, authors: [{name, uri, is_agency}], source: 'web'}
"""

from __future__ import annotations

import json
import logging
from typing import Any

import requests

from .. import env, llm
from ..textutil import host_of, is_low_quality_source

logger = logging.getLogger("journofinder.web_discovery")

HTTP_TIMEOUT = 30


def _brave_search(query: str, count: int = 10) -> list[dict[str, Any]]:
    key = env.get("BRAVE_API_KEY") or env.get("BRAVE_SEARCH_API_KEY")
    if not key:
        return []
    url = env.get("BRAVE_SEARCH_URL", "https://api.search.brave.com/res/v1/web/search")
    try:
        r = requests.get(
            url,
            params={"q": query, "count": min(max(count, 1), 20)},
            headers={"X-Subscription-Token": key, "Accept": "application/json"},
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        results = (r.json().get("web") or {}).get("results") or []
        return [{"title": x.get("title", ""), "url": x.get("url", ""),
                 "snippet": x.get("description", "")} for x in results]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Brave 搜索失败 (%r): %s", query, exc)
        return []


def _querit_search(query: str, count: int = 10) -> list[dict[str, Any]]:
    token = env.get("QUERIT_API_TOKEN")
    if not token:
        return []
    url = env.get("QUERIT_SEARCH_URL", "https://api.querit.ai/v1/search")
    try:
        r = requests.post(
            url,
            json={"query": query, "count": count},
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                     "Accept": "application/json"},
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        results_obj = data.get("results")
        inner = results_obj.get("result") if isinstance(results_obj, dict) else results_obj
        return [{"title": x.get("title", ""), "url": x.get("url", ""),
                 "snippet": x.get("snippet", "")} for x in (inner or [])]
    except Exception as exc:  # noqa: BLE001
        logger.warning("Querit 搜索失败 (%r): %s", query, exc)
        return []


def _candidate_to_article(c: dict, keyword: str) -> dict[str, Any] | None:
    """MiroMind / 抽取出的记者候选 → article dict（单作者）。"""
    name = (c.get("name") or "").strip()
    url = (c.get("article_url") or c.get("url") or "").strip()
    title = (c.get("article_title") or c.get("title") or "").strip()
    if not name:
        return None
    if url and is_low_quality_source(url, title):
        return None
    outlet = (c.get("outlet") or "").strip() or None
    outlet_uri = (c.get("outlet_uri") or "").strip() or (host_of(url) if url else None)
    return {
        "url": url or f"web://{name}/{outlet_uri or 'unknown'}",
        "title": title or f"(web-discovered coverage by {name})",
        "body": (c.get("snippet") or "").strip(),
        "source_title": outlet,
        "source_uri": outlet_uri,
        "published_at": None,
        "sentiment": None,
        "keyword_matched": keyword,
        "authors": [{"name": name, "uri": "", "is_agency": False}],
        "source": "web",
    }


def _extract_from_snippets(rows: list[dict], topics: str) -> list[dict]:
    """把 Querit/Brave 的 title/snippet/url 喂便宜模型，抽出记者署名。"""
    if not rows:
        return []
    listing = "\n".join(
        f'{i}. {r.get("title","")} | {r.get("url","")} | {r.get("snippet","")[:160]}'
        for i, r in enumerate(rows[:25], 1)
    )
    prompt = f"""These are web search results about: {topics}

For each result that is a NEWS/EDITORIAL ARTICLE with an identifiable individual
journalist byline, extract the journalist. Skip company blogs, directories, wire
services, and results with no clear individual author.

Results:
{listing}

Return JSON only:
[{{"name": "<journalist>", "outlet": "<publication>", "outlet_uri": "<domain>", "article_title": "<title>", "article_url": "<url>"}}]"""
    try:
        result = llm.call_json(llm.relevance_model(), prompt, max_tokens=1500)
    except Exception as exc:  # noqa: BLE001
        logger.warning("snippet 抽取失败: %s", exc)
        return []
    return [e for e in result if isinstance(e, dict) and (e.get("name") or "").strip()] if isinstance(result, list) else []


def discover_web_journalists(
    themes: list[str],
    competitors: list[str],
    *,
    n: int = 15,
) -> list[dict[str, Any]]:
    """补召记者，返回 article dict 列表（汇入主聚合流程）。"""
    topics = ", ".join([*themes, *competitors][:10])
    candidates: list[dict] = []

    # 路径 1：MiroMind 深搜（结构化）
    candidates += llm.miromind_find_journalists(themes, competitors, n=n)

    # 路径 2：Querit / Brave 网搜 + snippet 抽取
    if env.get("QUERIT_API_TOKEN") or env.get("BRAVE_API_KEY"):
        rows: list[dict] = []
        for t in [*themes, *competitors][:6]:
            q = f'{t} reporter OR journalist coverage 2026'
            rows += _querit_search(q, 8)
            rows += _brave_search(q, 8)
        candidates += _extract_from_snippets(rows, topics)

    # 转 article dict + 去重（按 name+outlet_uri）
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    kw = themes[0] if themes else (competitors[0] if competitors else "")
    for c in candidates:
        art = _candidate_to_article(c, kw)
        if not art:
            continue
        a = art["authors"][0]
        sig = (a["name"].lower(), (art.get("source_uri") or "").lower())
        if sig in seen:
            continue
        seen.add(sig)
        out.append(art)

    logger.info("web 补召: %d 个记者候选", len(out))
    return out
