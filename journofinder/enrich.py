"""联系方式补全 —— 分层策略。

Tier-A（前 N 名）：MiroMind 深挖 verified email/twitter/个人页 + 近期 sharp quotes。
Tier-B：用 outlet 域名 + 姓名规则推断邮箱（标记 inferred）。

关键种子：NewsAPI.ai 的 author uri 常为 `first_last@domain` 真实邮箱模式
（smoke test 见 TechCrunch sarah_perez@techcrunch.com），优先采信它再退到规则推断。
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from typing import Any

from . import db, llm

logger = logging.getLogger("journofinder.enrich")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# 平台托管域名 —— 作者在这些站没有 @domain 邮箱，不做规则推断（避免假邮箱）。
# 这类记者靠 Tier-A 深挖找真实联系方式，或留空让人工补。
_NON_EMAIL_DOMAINS = (
    "medium.com", "substack.com", "youtube.com", "youtu.be", "wordpress.com",
    "blogspot.com", "ghost.io", "tumblr.com", "linkedin.com", "twitter.com",
    "x.com", "reddit.com", "github.io",
)


def _email_from_author_uri(author_uri: str | None) -> str | None:
    """author uri 本身像邮箱就直接采信（NewsAPI.ai 常给 first_last@domain）。"""
    if author_uri and _EMAIL_RE.match(author_uri.strip()):
        return author_uri.strip().lower()
    return None


def _infer_email(name: str, outlet_uri: str | None) -> str | None:
    """按 firstname.lastname@domain 规则推断（最常见的媒体邮箱格式）。

    平台托管域名（medium/substack/youtube 等）不推断 —— 那不是真实邮箱域。
    """
    if not outlet_uri:
        return None
    domain = outlet_uri.strip().lower().lstrip("www.")
    if "." not in domain or "/" in domain:
        return None
    if any(domain == d or domain.endswith("." + d) for d in _NON_EMAIL_DOMAINS):
        return None
    parts = [p for p in re.split(r"\s+", name.strip().lower()) if p.isalpha()]
    if len(parts) < 2:
        return None
    first, last = parts[0], parts[-1]
    return f"{first}.{last}@{domain}"


def resolve_email(name: str, outlet_uri: str | None, author_uri: str | None) -> tuple[str | None, str | None]:
    """返回 (email, source)。source ∈ {'author_uri', 'inferred', None}。"""
    e = _email_from_author_uri(author_uri)
    if e:
        return e, "author_uri"
    e = _infer_email(name, outlet_uri)
    if e:
        return e, "inferred"
    return None, None


def _save(conn: sqlite3.Connection, search_id: int, jid: int, *,
          model: str | None, email: str | None, email_source: str | None,
          twitter: str | None, personal_url: str | None, quotes: list | None) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO enrichment "
        "(search_id, journalist_id, model, verified_email, verified_twitter, personal_url, recent_quotes_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (search_id, jid, model, email if email_source == "verified" else None,
         twitter, personal_url, json.dumps(quotes or [], ensure_ascii=False)),
    )
    # journalists 主表也回填（不覆盖已有非空 email，除非升级到更可信来源）
    if email:
        conn.execute(
            "UPDATE journalists SET email = COALESCE(email, ?), email_source = COALESCE(email_source, ?) WHERE id = ?",
            (email, email_source, jid),
        )
    if twitter:
        conn.execute("UPDATE journalists SET twitter = COALESCE(twitter, ?) WHERE id = ?", (twitter, jid))
    if personal_url:
        conn.execute("UPDATE journalists SET personal_url = COALESCE(personal_url, ?) WHERE id = ?", (personal_url, jid))
    conn.commit()


def run_enrichment(
    conn: sqlite3.Connection,
    search_id: int,
    tiers: dict[int, dict],
    journalists: list[dict[str, Any]],
    *,
    tier_a_top_n: int = 10,
    max_deepdive: int = 10,
) -> dict[str, int]:
    """Tier-A 深挖 + Tier-B 邮箱推断。返回统计 {deepdived, inferred}。

    journalists 已按 coverage 排序；tiers[jid] = {'tier','rationale'}。
    """
    by_id = {j["journalist_id"]: j for j in journalists}
    # A：按 coverage 顺序取前 N（且受 max_deepdive 硬上限约束）
    a_ids = [j["journalist_id"] for j in journalists
             if tiers.get(j["journalist_id"], {}).get("tier") == "A"]
    deepdive_ids = a_ids[: min(tier_a_top_n, max_deepdive)]

    stats = {"deepdived": 0, "inferred": 0}

    for jid in deepdive_ids:
        j = by_id[jid]
        dd = llm.deepdive_contact(j["name"], j.get("outlet"), j.get("signal") or "")
        email = dd.get("email")
        # 深挖到的 email 标 verified；否则退回规则/author_uri 推断
        if email:
            email_source = "verified"
        else:
            email, email_source = resolve_email(j["name"], j.get("outlet_uri"), j.get("author_uri"))
        _save(conn, search_id, jid, model=llm.deepdive_model(),
              email=email, email_source=email_source,
              twitter=dd.get("twitter"), personal_url=dd.get("personal_url"),
              quotes=dd.get("recent_quotes"))
        stats["deepdived"] += 1
        logger.info("深挖 [A] %s — email=%s(%s)", j["name"], email, email_source)

    # B（含未进深挖的 A 余量）：只做邮箱推断
    inferred_ids = [jid for jid, t in tiers.items()
                    if t.get("tier") in ("A", "B") and jid not in deepdive_ids]
    for jid in inferred_ids:
        j = by_id.get(jid)
        if not j:
            continue
        email, email_source = resolve_email(j["name"], j.get("outlet_uri"), j.get("author_uri"))
        _save(conn, search_id, jid, model=None,
              email=email, email_source=email_source,
              twitter=None, personal_url=None, quotes=[])
        if email:
            stats["inferred"] += 1

    logger.info("补全完成：深挖 %d · 推断 %d", stats["deepdived"], stats["inferred"])
    return stats
