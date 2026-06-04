"""编排 —— campaign 的 7 步漏斗。

discover → aggregate → score → tier → enrich → pitch → render

每步都把结果落 SQLite，可单步重跑（见 cli.py 的子命令）。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import aggregate, db, enrich, llm, report
from .config import BrandConfig
from .sources import newsapi_ai, web_discovery

logger = logging.getLogger("journofinder.pipeline")


def _window(days: int) -> tuple[str, str]:
    """[since, until] ISO 日期窗（用固定 until=今天，避免 Date.now 类不确定性留给调用方）。"""
    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00Z")
    until = now.strftime("%Y-%m-%dT23:59:59Z")
    return since, until


# ---------- 1. discover ----------

def discover(conn: sqlite3.Connection, cfg: BrandConfig) -> int:
    keywords = cfg.all_keywords()
    since, until = _window(cfg.discovery.date_window_days)
    articles: list[dict] = []

    if "newsapi_ai" in cfg.discovery.providers:
        articles += newsapi_ai.fetch_articles(
            keywords, since_iso=since, until_iso=until,
            languages=cfg.discovery.languages,
            articles_count=cfg.discovery.articles_count,
        )

    if cfg.discovery.web_augment:
        articles += web_discovery.discover_web_journalists(cfg.themes, cfg.competitors, n=15)

    logger.info("discover：共 %d 篇文章/候选", len(articles))
    aggregate.ingest_articles(conn, articles)
    return len(articles)


# ---------- 2+3. score（含聚合指标） ----------

def score(conn: sqlite3.Connection, search_id: int, cfg: BrandConfig, max_workers: int = 8) -> list[dict]:
    """对每个记者打 relevance 分（并发），写 relevance_scores。返回带 score 的记者列表。"""
    journalists = aggregate.coverage_metrics(conn)
    brand_summary = cfg.brand_summary()

    def _score_one(j: dict) -> tuple[int, dict]:
        try:
            res = llm.score_relevance(brand_summary, j)
        except Exception as exc:  # noqa: BLE001
            logger.warning("打分失败 %s: %s", j.get("name"), exc)
            res = {"score": 0, "reason": f"score error: {exc}"}
        return j["journalist_id"], res

    scored: dict[int, dict] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(_score_one, j) for j in journalists]
        for fut in as_completed(futures):
            jid, res = fut.result()
            scored[jid] = res
            conn.execute(
                "INSERT OR REPLACE INTO relevance_scores (search_id, journalist_id, score, reason) "
                "VALUES (?, ?, ?, ?)",
                (search_id, jid, res["score"], res["reason"]),
            )
    conn.commit()

    for j in journalists:
        j["score"] = scored.get(j["journalist_id"], {}).get("score", 0)
        j["score_reason"] = scored.get(j["journalist_id"], {}).get("reason", "")
    journalists.sort(key=lambda x: (x["score"], x["article_count"]), reverse=True)
    logger.info("score：%d 个记者已打分", len(journalists))
    return journalists


# ---------- 4. tier ----------

def tier(conn: sqlite3.Connection, search_id: int, cfg: BrandConfig, scored: list[dict]) -> dict[int, dict]:
    """对达标记者分 A/B/drop，写 journo_tiers（不覆盖人工 manual）。"""
    eligible = [j for j in scored if j["score"] >= cfg.tiering.min_score][: cfg.tiering.max_journalists]
    tiers = llm.classify_tiers(
        cfg.brand_summary(), eligible,
        model=cfg.tiering.model, competitors=cfg.competitors,
    )
    for jid, t in tiers.items():
        existing = conn.execute(
            "SELECT source FROM journo_tiers WHERE search_id = ? AND journalist_id = ?",
            (search_id, jid),
        ).fetchone()
        if existing and existing["source"] == "manual":
            continue  # 不覆盖人工捞回/覆盖
        conn.execute(
            "INSERT OR REPLACE INTO journo_tiers (search_id, journalist_id, tier, rationale, source) "
            "VALUES (?, ?, ?, ?, 'auto')",
            (search_id, jid, t["tier"], t["rationale"]),
        )
    conn.commit()
    logger.info("tier：%d 个记者分层（A=%d B=%d drop=%d）",
                len(tiers),
                sum(1 for t in tiers.values() if t["tier"] == "A"),
                sum(1 for t in tiers.values() if t["tier"] == "B"),
                sum(1 for t in tiers.values() if t["tier"] == "drop"))
    return tiers


# ---------- 6. pitch ----------

def pitch(conn: sqlite3.Connection, search_id: int, cfg: BrandConfig,
          scored: list[dict], tiers: dict[int, dict]) -> int:
    """对 A/B 记者生成 1-3 个 pitch angle，写 pitch_angles。"""
    brand_summary = cfg.brand_summary()
    n = 0
    for j in scored:
        jid = j["journalist_id"]
        if tiers.get(jid, {}).get("tier") not in ("A", "B"):
            continue
        top = aggregate.top_articles_for(conn, jid, limit=3)
        angles = llm.generate_pitch_angles(brand_summary, j["name"], top)
        conn.execute(
            "INSERT OR REPLACE INTO pitch_angles (search_id, journalist_id, angles_json) VALUES (?, ?, ?)",
            (search_id, jid, json.dumps(angles, ensure_ascii=False)),
        )
        n += 1
    conn.commit()
    logger.info("pitch：%d 个记者生成 angle", n)
    return n


# ---------- 完整 campaign ----------

def run_campaign(cfg: BrandConfig, db_path: str | Path, out_path: str | Path,
                 skip_discovery: bool = False) -> dict:
    """跑完整漏斗，产出报告。返回交付摘要。"""
    db.init_schema(db_path)
    conn = db.get_conn(db_path)
    try:
        search_id = db.create_search(conn, cfg.brand, cfg.brand_summary(), cfg.all_keywords())

        if not skip_discovery:
            discover(conn, cfg)

        scored = score(conn, search_id, cfg)
        tiers = tier(conn, search_id, cfg, scored)
        enrich.run_enrichment(
            conn, search_id, tiers, scored,
            tier_a_top_n=cfg.enrich.tier_a_top_n,
            max_deepdive=cfg.budget.max_deepdive,
        )
        pitch(conn, search_id, cfg, scored, tiers)

        out = report.render(conn, search_id, cfg, out_path)
        return out
    finally:
        conn.close()
