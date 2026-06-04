"""NewsAPI.ai (Event Registry) —— journofinder 的主引擎。

用品牌的行业/赛道/竞品关键词查近期文章，解析每篇的 `authors`（记者署名）+ `source`
（媒体）。谁在密集报道这个领域，谁就是目标记者。

关键字段（已 smoke test 确认返回）：
  authors: [{"uri": "sarah_perez@techcrunch.com", "name": "Sarah Perez", "isAgency": false}]
  source:  {"uri": "techcrunch.com", "title": "TechCrunch"}
note: author 的 uri 常为 first_last@domain 格式，是高质量邮箱种子（见 enrich.py）。

改编自 shanda-pulse/scrapers/newsapi_ai.py：保留抗毒逐词重试 + 多语言 + 日期窗，
新增 authors / source 解析（原版只取了正文 + 情感）。
Env-gated：未设 NEWSAPI_AI_KEY 时直接 no-op。
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

from .. import env
from ..textutil import is_low_quality_source, to_iso

logger = logging.getLogger("journofinder.newsapi_ai")

RATE_LIMIT_SECONDS = 1.0


KEYWORD_LIMIT = 15  # Event Registry 免费档单查询关键词上限


def _chunk(seq: list, n: int) -> list[list]:
    return [seq[i:i + n] for i in range(0, len(seq), n)]


def fetch_articles(
    keywords: list[str],
    *,
    since_iso: str | None = None,
    until_iso: str | None = None,
    languages: list[str] | None = None,
    articles_count: int = 100,
    sort_by: str = "date",
    pages: int = 1,
) -> list[dict[str, Any]]:
    """按关键词查文章，返回归一化的 article dict（含 authors / source）。

    每个 dict：
      {url, title, body, source_title, source_uri, published_at, sentiment,
       keyword_matched, authors: [{name, uri, is_agency}]}

    - sort_by: "date"（最新）| "sourceImportance"（按来源权威度，把 Tier-1 大刊顶上来）| "rel"
    - pages: 翻几页（每页 articles_count 篇）扩大池子
    - 关键词 >15 自动分组（免费档单查询上限 15），分组 OR 查询再合并去重
    """
    api_key = env.get("NEWSAPI_AI_KEY")
    if not api_key:
        logger.info("NEWSAPI_AI_KEY 未设置，跳过 NewsAPI.ai（主源缺失，将只靠补召源）")
        return []
    if not keywords:
        return []

    url = env.get("NEWSAPI_AI_URL", "https://eventregistry.org/api/v1/article/getArticles")
    langs = languages or ["eng"]
    count = min(max(articles_count, 1), 100)  # Event Registry 单页上限 100

    # 关键词 ≤15 分组；每组翻 pages 页。
    seen_u: set[str] = set()
    raw: list[dict] = []
    for group in _chunk([k for k in keywords if k], KEYWORD_LIMIT):
        for page in range(1, max(1, pages) + 1):
            got = _fetch(api_key, url, group, since_iso, until_iso, langs, count, sort_by, page)
            # 抗毒：某组 OR 查询第 1 页为空且组内不止一个关键词，逐词重试（坏词只损失自己）
            if not got and page == 1 and len(group) > 1:
                logger.info("NewsAPI.ai 组 OR 查询为空，逐关键词重试（抗毒）")
                for kw in group:
                    got += _fetch(api_key, url, [kw], since_iso, until_iso, langs, count, sort_by, 1)
            for r in got:
                u = (r.get("url") or "").strip()
                if u and u not in seen_u:
                    seen_u.add(u)
                    raw.append(r)
            if not got:  # 该页没结果，后续页也不必翻
                break

    kw_lower = [(k, k.lower()) for k in keywords if k]
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    dropped_spam = 0
    for art in raw:
        u = (art.get("url") or "").strip()
        if not u or u in seen:
            continue
        seen.add(u)
        title = (art.get("title") or "").strip()
        body = (art.get("body") or "").strip()
        if is_low_quality_source(u, title):
            dropped_spam += 1
            continue
        hay = (title + "\n" + body).lower()
        matched = next((k for k, kl in kw_lower if kl and kl in hay), keywords[0])
        src = art.get("source") or {}
        authors = []
        for a in (art.get("authors") or []):
            if not isinstance(a, dict):
                continue
            authors.append({
                "name": (a.get("name") or "").strip(),
                "uri": (a.get("uri") or "").strip(),
                "is_agency": bool(a.get("isAgency")),
            })
        sentiment = art.get("sentiment")
        out.append({
            "url": u,
            "title": title,
            "body": body,
            "source_title": (src.get("title") or "").strip() or None,
            "source_uri": (src.get("uri") or "").strip() or None,
            "published_at": to_iso(art.get("dateTime") or art.get("date")),
            "sentiment": float(sentiment) if isinstance(sentiment, (int, float)) else None,
            "keyword_matched": matched,
            "authors": authors,
        })

    logger.info("NewsAPI.ai: %d 篇文章 · drop %d 垃圾源", len(out), dropped_spam)
    return out


def _fetch(
    api_key: str,
    url: str,
    keywords: list[str],
    since_iso: str | None,
    until_iso: str | None,
    langs: list[str],
    count: int,
    sort_by: str = "date",
    page: int = 1,
) -> list[dict]:
    """一次 getArticles 调用。出错/空返回 []。"""
    payload: dict[str, Any] = {
        "action": "getArticles",
        "keyword": keywords,
        "keywordOper": "or",
        "lang": langs,
        "articlesPage": page,
        "articlesCount": count,
        "articlesSortBy": sort_by,
        "dataType": ["news", "pr", "blog"],
        "includeArticleAuthors": True,
        "includeArticleSourceInfo": True,
        "includeArticleSentiment": True,
        "resultType": "articles",
        "apiKey": api_key,
    }
    if since_iso:
        payload["dateStart"] = since_iso[:10]
    if until_iso:
        payload["dateEnd"] = until_iso[:10]
    try:
        time.sleep(RATE_LIMIT_SECONDS)
        resp = requests.post(url, json=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("NewsAPI.ai 请求失败 (keywords=%r): %s", keywords[:3], exc)
        return []
    if isinstance(data, dict) and data.get("error"):
        logger.warning("NewsAPI.ai 返回错误 (keywords=%r): %s", keywords[:3], data.get("error"))
        return []
    return ((data or {}).get("articles") or {}).get("results") or []
