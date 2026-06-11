"""SQLite schema + 连接 + 少量复用的 upsert 辅助。

记者（journalists）是聚合单位：一个记者来自一个或多个文章署名。文章（articles）挂在
记者下。其余表按 search（一次 campaign 跑）记录打分 / 分层 / 深挖 / pitch。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS journalists (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  name_normalized TEXT NOT NULL,
  outlet TEXT,                              -- 媒体显示名，如 "TechCrunch"
  outlet_uri TEXT,                          -- 媒体域名，如 "techcrunch.com"
  author_uri TEXT,                          -- NewsAPI.ai 的 author uri，常为 first_last@domain
  email TEXT,
  email_source TEXT,                        -- 'verified'(深挖核实) | 'web'(网搜命中) | null。不再推测
  twitter TEXT,
  linkedin TEXT,
  personal_url TEXT,
  beat TEXT,
  source TEXT DEFAULT 'newsapi',            -- 'newsapi' | 'web'
  added_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(name_normalized, outlet_uri)
);

CREATE TABLE IF NOT EXISTS articles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  journalist_id INTEGER NOT NULL,
  url TEXT UNIQUE,
  title TEXT,
  body TEXT,
  source_title TEXT,
  published_at DATETIME,
  sentiment REAL,
  keyword_matched TEXT,
  fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (journalist_id) REFERENCES journalists(id)
);

CREATE TABLE IF NOT EXISTS searches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  brand TEXT,
  description TEXT NOT NULL,
  extracted_keywords TEXT,
  ran_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS relevance_scores (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  search_id INTEGER NOT NULL,
  journalist_id INTEGER NOT NULL,
  score INTEGER NOT NULL,
  reason TEXT,
  UNIQUE(search_id, journalist_id),
  FOREIGN KEY (search_id) REFERENCES searches(id),
  FOREIGN KEY (journalist_id) REFERENCES journalists(id)
);

CREATE TABLE IF NOT EXISTS journo_tiers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  search_id INTEGER NOT NULL,
  journalist_id INTEGER NOT NULL,
  tier TEXT NOT NULL,                       -- 'A' | 'B' | 'drop'
  rationale TEXT,
  source TEXT DEFAULT 'auto',               -- 'auto' (LLM) | 'manual' (人工捞回/覆盖)
  set_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(search_id, journalist_id),
  FOREIGN KEY (search_id) REFERENCES searches(id),
  FOREIGN KEY (journalist_id) REFERENCES journalists(id)
);

CREATE TABLE IF NOT EXISTS enrichment (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  search_id INTEGER NOT NULL,
  journalist_id INTEGER NOT NULL,
  model TEXT,
  verified_email TEXT,
  verified_twitter TEXT,
  verified_linkedin TEXT,
  personal_url TEXT,
  recent_quotes_json TEXT,
  ran_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(search_id, journalist_id),
  FOREIGN KEY (search_id) REFERENCES searches(id),
  FOREIGN KEY (journalist_id) REFERENCES journalists(id)
);

CREATE TABLE IF NOT EXISTS pitch_angles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  search_id INTEGER NOT NULL,
  journalist_id INTEGER NOT NULL,
  angles_json TEXT,
  UNIQUE(search_id, journalist_id),
  FOREIGN KEY (search_id) REFERENCES searches(id),
  FOREIGN KEY (journalist_id) REFERENCES journalists(id)
);

CREATE INDEX IF NOT EXISTS idx_articles_journalist ON articles(journalist_id);
CREATE INDEX IF NOT EXISTS idx_articles_published ON articles(published_at);
CREATE INDEX IF NOT EXISTS idx_relevance_search ON relevance_scores(search_id);
CREATE INDEX IF NOT EXISTS idx_tiers_search ON journo_tiers(search_id);
"""


def get_conn(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# 幂等迁移：对 CREATE IF NOT EXISTS 之外新增的列做 ALTER（老库升级用）。
# 每项 (table, column, ddl)；列已存在则跳过。
_MIGRATIONS = [
    ("journalists", "linkedin", "ALTER TABLE journalists ADD COLUMN linkedin TEXT"),
    ("enrichment", "verified_linkedin", "ALTER TABLE enrichment ADD COLUMN verified_linkedin TEXT"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, ddl in _MIGRATIONS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(ddl)
    conn.commit()


def init_schema(db_path: str | Path) -> None:
    conn = get_conn(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
        _migrate(conn)
    finally:
        conn.close()


def upsert_journalist(
    conn: sqlite3.Connection,
    *,
    name: str,
    name_normalized: str,
    outlet: str | None,
    outlet_uri: str | None,
    author_uri: str | None,
    source: str = "newsapi",
) -> int:
    """按 (name_normalized, outlet_uri) 去重写入记者，返回 journalist_id。

    已存在则补全空缺的 author_uri / outlet（不覆盖已有非空值）。
    """
    row = conn.execute(
        "SELECT id, author_uri, outlet FROM journalists "
        "WHERE name_normalized = ? AND IFNULL(outlet_uri,'') = IFNULL(?, '')",
        (name_normalized, outlet_uri),
    ).fetchone()
    if row:
        jid = row["id"]
        if author_uri and not row["author_uri"]:
            conn.execute("UPDATE journalists SET author_uri = ? WHERE id = ?", (author_uri, jid))
        if outlet and not row["outlet"]:
            conn.execute("UPDATE journalists SET outlet = ? WHERE id = ?", (outlet, jid))
        return jid
    cur = conn.execute(
        "INSERT INTO journalists (name, name_normalized, outlet, outlet_uri, author_uri, source) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (name, name_normalized, outlet, outlet_uri, author_uri, source),
    )
    return int(cur.lastrowid)


def insert_article(
    conn: sqlite3.Connection,
    *,
    journalist_id: int,
    url: str,
    title: str,
    body: str,
    source_title: str | None,
    published_at: str | None,
    sentiment: float | None,
    keyword_matched: str | None,
) -> None:
    """写入文章，url 冲突则忽略（同一篇被多关键词命中只存一次）。"""
    conn.execute(
        "INSERT OR IGNORE INTO articles "
        "(journalist_id, url, title, body, source_title, published_at, sentiment, keyword_matched) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (journalist_id, url, title, body, source_title, published_at, sentiment, keyword_matched),
    )


def create_search(conn: sqlite3.Connection, brand: str, description: str, keywords: list[str]) -> int:
    cur = conn.execute(
        "INSERT INTO searches (brand, description, extracted_keywords) VALUES (?, ?, ?)",
        (brand, description, json.dumps(keywords, ensure_ascii=False)),
    )
    conn.commit()
    return int(cur.lastrowid)
