"""联系方式补全 —— 只取真实来源，绝不推测。

优先级（阿蓓定）：LinkedIn URL > X > Email > 其它。

分层（高效优先）：
  阶段 1（便宜）：对全部 A+B 记者跑 Querit/Brave 网搜 + deepseek 抽取，并从文章正文
                  抽 same-org 真邮箱。记者 social presence 强，多数能在这步命中。
  阶段 2（兜底）：阶段 1 既没拿到 LinkedIn 也没拿到 X 的记者，才上 Apodex 深挖
                  （受 budget.max_deepdive 上限），结果标 verified。

每个字段都过名字校验（防抓到别人的资料）。任何不确定 → 留空。NewsAPI 的 author_uri
是内部标识符不是邮箱，已不再当联系方式（历史值在 run_enrichment 开头清掉）。
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from . import llm
from .sources import web_discovery
from .textutil import host_of

logger = logging.getLogger("journofinder.enrich")

# 阶段 2 Apodex 深挖并发数（单个 deepresearch 慢，并发缩短墙钟）。
DEEPDIVE_WORKERS = 4

# 事务/通用邮箱本地名 —— 不是个人记者邮箱，一律丢。
_GENERIC_LOCALS = {
    "info", "sales", "support", "noreply", "no-reply", "contact", "hello",
    "press", "pr", "editor", "editorial", "tips", "admin", "team", "help",
    "newsletter", "marketing", "media", "billing", "careers", "jobs",
    "privacy", "legal", "abuse", "feedback", "general", "english", "global",
}

_EMAIL_RE = re.compile(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}", re.I)
_LINKEDIN_RE = re.compile(r"(?:https?://)?(?:[a-z]{2,3}\.)?linkedin\.com/in/[\w%\-]+", re.I)
_HANDLE_RE = re.compile(
    r"(?:@|(?:https?://)?(?:www\.)?(?:twitter|x)\.com/)([A-Za-z0-9_]{2,15})", re.I
)


# ---------- 名字校验（防抓错人；宁缺勿编） ----------

def _name_tokens(name: str) -> list[str]:
    return [t for t in re.split(r"[^a-z]+", (name or "").lower()) if len(t) >= 2]


def _local_matches_name(local: str, name: str) -> bool:
    """email 本地名是否确属这位记者（含名字词 / flast / firstlast 等常见拼法）。"""
    low = re.sub(r"[^a-z]", "", local.lower())
    if not low:
        return False
    toks = _name_tokens(name)
    if any(len(t) >= 3 and t in low for t in toks):
        return True
    if len(toks) >= 2:
        first, last = toks[0], toks[-1]
        if low in {first + last, last + first, first[0] + last, last + first[0]}:
            return True
        if low.startswith(first[0]) and last in low:
            return True
    return False


def _validate_linkedin(url: str | None, name: str) -> str | None:
    m = _LINKEDIN_RE.search(url or "")
    if not m:
        return None
    full = m.group(0)
    if not full.lower().startswith("http"):
        full = "https://" + full
    slug = full.rsplit("/in/", 1)[-1].lower()
    if any(len(t) >= 3 and t in slug for t in _name_tokens(name)):
        return full.split("?")[0]
    return None


def _validate_twitter(raw: str | None, name: str, outlet_uri: str | None) -> str | None:
    """信任 LLM 的本人判定，代码只挡机构 handle（含 outlet 主词）。"""
    m = _HANDLE_RE.search(raw or "")
    handle = m.group(1) if m else (raw or "").lstrip("@").strip()
    if not re.fullmatch(r"[A-Za-z0-9_]{2,15}", handle or ""):
        return None
    low = handle.lower()
    outlet_word = (outlet_uri or "").split(".")[0].lower()
    if len(outlet_word) >= 4 and outlet_word in low:
        return None
    return "@" + handle


def _validate_email(email: str | None, name: str, outlet_uri: str | None) -> str | None:
    e = (email or "").strip().lower()
    if not _EMAIL_RE.fullmatch(e):
        return None
    local, _, host = e.partition("@")
    if local in _GENERIC_LOCALS or not host:
        return None
    # 最强约束：本地名必须确属这位记者，否则可能发错人。
    return e if _local_matches_name(local, name) else None


def _validate_personal(url: str | None, name: str, outlet_uri: str | None) -> str | None:
    if not url:
        return None
    h = (host_of(url) or "").lower()
    if not h:
        return None
    flat = h.replace(".", "")
    if any(len(t) >= 3 and t in flat for t in _name_tokens(name)):
        return url  # 个人域名含名字
    if outlet_uri and outlet_uri.lower() in h:
        return url  # outlet 上的作者/staff 页
    return None


def _validate_contact(name: str, outlet_uri: str | None, raw: dict) -> dict:
    """对一组原始联系方式逐字段校验，校验不过的丢成 None。"""
    return {
        "linkedin": _validate_linkedin(raw.get("linkedin"), name),
        "twitter": _validate_twitter(raw.get("twitter"), name, outlet_uri),
        "email": _validate_email(raw.get("email"), name, outlet_uri),
        "personal_url": _validate_personal(raw.get("personal_url"), name, outlet_uri),
    }


# ---------- 阶段 1：真实来源抓取（便宜） ----------

def search_contact(name: str, outlet: str | None, outlet_uri: str | None) -> dict:
    """Querit/Brave 网搜 → deepseek 抽取 → 名字校验。缺 key 自动返回空。"""
    q = f'"{name}" {outlet or ""} (linkedin OR "x.com" OR twitter)'.strip()
    # Querit 主（合约不限速）；Querit 召回不足时才补 Brave（免费档 ~1 req/s，避免限流）
    rows = web_discovery._querit_search(q, 8)
    if len(rows) < 4:
        rows += web_discovery._brave_search(q, 8)
    if not rows:
        return {}
    raw = llm.extract_contact_from_search(name, outlet, rows)
    return _validate_contact(name, outlet_uri, raw)


def search_contact_via_articles(
    conn: sqlite3.Connection, jid: int, name: str, outlet_uri: str | None
) -> str | None:
    """从该记者文章正文抽 same-org 真邮箱（本地名须确属本人）。"""
    domain = (outlet_uri or "").lower().lstrip("www.")
    if not domain:
        return None
    for (body,) in conn.execute(
        "SELECT body FROM articles WHERE journalist_id = ? AND body IS NOT NULL", (jid,)
    ):
        for m in _EMAIL_RE.findall(body or ""):
            e = m.lower()
            local, _, host = e.partition("@")
            if (host == domain or host.endswith("." + domain)) \
                    and local not in _GENERIC_LOCALS and _local_matches_name(local, name):
                return e
    return None


# ---------- 写库 ----------

def _save(
    conn: sqlite3.Connection, search_id: int, jid: int, *,
    model: str | None, verified: bool,
    linkedin: str | None, email: str | None, twitter: str | None,
    personal_url: str | None, email_source: str | None, quotes: list | None,
) -> None:
    """enrichment 表只存 verified（深挖核实）字段；联系方式回填 journalists 主表。"""
    conn.execute(
        "INSERT OR REPLACE INTO enrichment "
        "(search_id, journalist_id, model, verified_email, verified_twitter, "
        " verified_linkedin, personal_url, recent_quotes_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (search_id, jid, model,
         email if verified else None,
         twitter if verified else None,
         linkedin if verified else None,
         personal_url,
         json.dumps(quotes or [], ensure_ascii=False)),
    )
    if email:
        conn.execute(
            "UPDATE journalists SET email = COALESCE(email, ?), "
            "email_source = COALESCE(email_source, ?) WHERE id = ?",
            (email, email_source, jid),
        )
    if twitter:
        conn.execute("UPDATE journalists SET twitter = COALESCE(twitter, ?) WHERE id = ?", (twitter, jid))
    if linkedin:
        conn.execute("UPDATE journalists SET linkedin = COALESCE(linkedin, ?) WHERE id = ?", (linkedin, jid))
    if personal_url:
        conn.execute("UPDATE journalists SET personal_url = COALESCE(personal_url, ?) WHERE id = ?", (personal_url, jid))
    conn.commit()


# ---------- 编排 ----------

def run_enrichment(
    conn: sqlite3.Connection,
    search_id: int,
    tiers: dict[int, dict],
    journalists: list[dict[str, Any]],
    *,
    tier_a_top_n: int = 10,        # 保留向后兼容（阶段 2 上限实际用 max_deepdive）
    max_deepdive: int = 10,
    search_all: bool = True,
    apodex_fallback: bool = True,
) -> dict[str, int]:
    """阶段 1 全员便宜网搜 + 文章正文邮箱；阶段 2 对缺 LinkedIn/X 者 Apodex 兜底。

    journalists 已按 coverage 排序；tiers[jid] = {'tier','rationale'}。
    返回 {searched, web_hits, deepdived, empty}。
    """
    # 清掉历史推测来源（author_uri / inferred），它们不是真实联系方式。
    conn.execute(
        "UPDATE journalists SET email = NULL, email_source = NULL "
        "WHERE email_source IN ('author_uri', 'inferred')"
    )
    conn.commit()

    by_id = {j["journalist_id"]: j for j in journalists}
    order = [j["journalist_id"] for j in journalists]
    targets = {jid for jid, t in tiers.items() if t.get("tier") in ("A", "B")}
    target_ids = [jid for jid in order if jid in targets]

    stats = {"searched": 0, "web_hits": 0, "deepdived": 0, "empty": 0}
    need_deepdive: list[int] = []

    # 阶段 1
    for jid in target_ids:
        j = by_id.get(jid)
        if not j:
            continue
        raw = search_contact(j["name"], j.get("outlet"), j.get("outlet_uri")) if search_all else {}
        if not raw.get("email"):
            ae = search_contact_via_articles(conn, jid, j["name"], j.get("outlet_uri"))
            if ae:
                raw["email"] = ae
        stats["searched"] += 1
        if any(raw.get(k) for k in ("linkedin", "twitter", "email", "personal_url")):
            stats["web_hits"] += 1
            _save(conn, search_id, jid, model="web", verified=False,
                  linkedin=raw.get("linkedin"), email=raw.get("email"),
                  twitter=raw.get("twitter"), personal_url=raw.get("personal_url"),
                  email_source="web" if raw.get("email") else None, quotes=[])
        # 没拿到高优先级（LinkedIn / X）→ 候选深挖
        if not raw.get("linkedin") and not raw.get("twitter"):
            need_deepdive.append(jid)
        logger.info("阶段1 [%s] %s — linkedin=%s twitter=%s email=%s",
                    tiers.get(jid, {}).get("tier"), j["name"],
                    bool(raw.get("linkedin")), bool(raw.get("twitter")), bool(raw.get("email")))

    # 阶段 2：Apodex 兜底（A 优先，受上限）。深挖是网络密集且单个慢（2-8min），并发跑；
    # SQLite 连接不跨线程 —— 仅在主线程 _save，线程里只做 Apodex 调用。
    if apodex_fallback and llm.apodex_available() and need_deepdive:
        a_first = sorted(need_deepdive, key=lambda x: 0 if tiers.get(x, {}).get("tier") == "A" else 1)
        targets = [jid for jid in a_first[:max_deepdive] if by_id.get(jid)]

        def _deepdive_one(jid: int) -> tuple[int, dict]:
            j = by_id[jid]
            return jid, llm.deepdive_contact(j["name"], j.get("outlet"), j.get("signal") or "")

        with ThreadPoolExecutor(max_workers=min(DEEPDIVE_WORKERS, len(targets) or 1)) as ex:
            for fut in as_completed([ex.submit(_deepdive_one, jid) for jid in targets]):
                try:
                    jid, dd = fut.result()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("阶段2深挖失败: %s", exc)
                    continue
                j = by_id[jid]
                v = _validate_contact(j["name"], j.get("outlet_uri"), dd)
                quotes = dd.get("recent_quotes") or []
                if any(v.get(k) for k in ("linkedin", "twitter", "email", "personal_url")) or quotes:
                    _save(conn, search_id, jid, model=llm.deepdive_model(), verified=True,
                          linkedin=v.get("linkedin"), email=v.get("email"),
                          twitter=v.get("twitter"), personal_url=v.get("personal_url"),
                          email_source="verified" if v.get("email") else None, quotes=quotes)
                    stats["deepdived"] += 1
                logger.info("阶段2深挖 [%s] %s — linkedin=%s twitter=%s email=%s",
                            tiers.get(jid, {}).get("tier"), j["name"],
                            bool(v.get("linkedin")), bool(v.get("twitter")), bool(v.get("email")))

    # empty：目标里既无 linkedin/twitter/email/personal 的
    placed = conn.execute(
        "SELECT COUNT(*) FROM journalists WHERE id IN (%s) AND "
        "(linkedin IS NOT NULL OR twitter IS NOT NULL OR email IS NOT NULL OR personal_url IS NOT NULL)"
        % (",".join("?" * len(target_ids)) or "NULL"),
        target_ids,
    ).fetchone()[0] if target_ids else 0
    stats["empty"] = len(target_ids) - placed

    logger.info("补全完成：searched=%d web_hits=%d deepdived=%d empty=%d",
                stats["searched"], stats["web_hits"], stats["deepdived"], stats["empty"])
    return stats
