"""聚合层 —— journofinder 的核心：把文章按记者署名归一化聚合。

newsapi / web 返回的是「文章 + authors」。一篇文章可有多个作者；同一个记者会出现在
多篇文章里。这里展开 author → 归一化去重 → 写入 journalists / articles，并算出每个记者
的 coverage 指标（窗口内文章数、最近日期、近期标题信号、平均情感）。

过滤：机构署名（Editorial Team/Staff）、通讯社（isAgency）不是可建联的记者，drop。
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from . import db
from .textutil import is_wire_source, looks_like_person, normalize_author_name

logger = logging.getLogger("journofinder.aggregate")


def ingest_articles(conn: sqlite3.Connection, articles: list[dict[str, Any]]) -> int:
    """写入 journalists + articles，返回入库记者数。

    一篇多作者文章会拆给每个作者各存一条 article 记录（按 url+作者去重在 DB 层靠
    url UNIQUE 控制：同一 url 只存第一个作者那条；这是有意取舍——一篇报道归到主笔即可，
    避免同一篇灌满多个边缘合著者）。
    """
    for art in articles:
        authors = art.get("authors") or []
        outlet_uri = art.get("source_uri")
        # 通讯社/通稿站整篇跳过（没有可建联的独立记者）
        if is_wire_source(outlet_uri, art.get("url")):
            continue
        wrote_for_url = False
        for a in authors:
            name = (a.get("name") or "").strip()
            if a.get("is_agency") or not looks_like_person(name):
                continue
            jid = db.upsert_journalist(
                conn,
                name=name,
                name_normalized=normalize_author_name(name),
                outlet=art.get("source_title"),
                outlet_uri=outlet_uri,
                author_uri=(a.get("uri") or None),
                source=art.get("source", "newsapi"),
            )
            # 该 url 只挂第一个有效作者，避免重复
            if not wrote_for_url:
                db.insert_article(
                    conn,
                    journalist_id=jid,
                    url=art.get("url") or "",
                    title=art.get("title") or "",
                    body=art.get("body") or "",
                    source_title=art.get("source_title"),
                    published_at=art.get("published_at"),
                    sentiment=art.get("sentiment"),
                    keyword_matched=art.get("keyword_matched"),
                )
                wrote_for_url = True
    conn.commit()
    new_journalists = conn.execute("SELECT COUNT(*) FROM journalists").fetchone()[0]
    logger.info("聚合完成：库内共 %d 个记者", new_journalists)
    return int(new_journalists)


def coverage_metrics(conn: sqlite3.Connection, signal_titles: int = 5) -> list[dict[str, Any]]:
    """每个记者的 coverage 指标，按文章数 + 最近度排序（多者优先）。

    返回 dict：{journalist_id, name, outlet, outlet_uri, author_uri,
               article_count, latest_date, avg_sentiment, signal}
    signal = 最近 N 篇标题拼接（喂打分/分层/pitch）。
    """
    rows = conn.execute(
        """
        SELECT j.id AS journalist_id, j.name, j.outlet, j.outlet_uri, j.author_uri,
               COUNT(a.id) AS article_count,
               MAX(a.published_at) AS latest_date,
               AVG(a.sentiment) AS avg_sentiment
        FROM journalists j
        JOIN articles a ON a.journalist_id = j.id
        GROUP BY j.id
        ORDER BY article_count DESC, latest_date DESC
        """
    ).fetchall()

    out: list[dict[str, Any]] = []
    for r in rows:
        titles = conn.execute(
            "SELECT title, published_at FROM articles WHERE journalist_id = ? "
            "ORDER BY published_at DESC LIMIT ?",
            (r["journalist_id"], signal_titles),
        ).fetchall()
        signal = " | ".join(t["title"] for t in titles if t["title"])
        out.append({
            "journalist_id": r["journalist_id"],
            "name": r["name"],
            "outlet": r["outlet"],
            "outlet_uri": r["outlet_uri"],
            "author_uri": r["author_uri"],
            "article_count": r["article_count"],
            "latest_date": r["latest_date"],
            "avg_sentiment": r["avg_sentiment"],
            "signal": signal,
        })
    return out


def top_articles_for(conn: sqlite3.Connection, journalist_id: int, limit: int = 3) -> list[dict]:
    """记者近期文章（喂 pitch angle 生成）。"""
    rows = conn.execute(
        "SELECT title, url, body, published_at FROM articles WHERE journalist_id = ? "
        "ORDER BY published_at DESC LIMIT ?",
        (journalist_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]
