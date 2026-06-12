"""enrich 联系方式 —— 只取真实来源、逐字段名字校验、绝不推测。"""

from __future__ import annotations

from journofinder import db
from journofinder.enrich import (
    _local_matches_name,
    _validate_contact,
    _validate_email,
    _validate_linkedin,
    _validate_personal,
    _validate_twitter,
    search_contact_via_articles,
)


def test_local_matches_name():
    assert _local_matches_name("sharon.goldman", "Sharon Goldman")
    assert _local_matches_name("sgoldman", "Sharon Goldman")     # 首字母+姓
    assert _local_matches_name("goldman", "Sharon Goldman")
    assert not _local_matches_name("sales", "Sharon Goldman")
    assert not _local_matches_name("jdoe", "Sharon Goldman")     # 别人 → 防发错人


def test_validate_email_real_name_only_no_guessing():
    assert _validate_email("sharon.goldman@fortune.com", "Sharon Goldman", "fortune.com") \
        == "sharon.goldman@fortune.com"
    # 通用 / 事务箱拒
    assert _validate_email("press@fortune.com", "Sharon Goldman", "fortune.com") is None
    assert _validate_email("info@x.com", "Sharon Goldman", "x.com") is None
    # 名字对不上 → 拒（绝不发错人）
    assert _validate_email("john.smith@fortune.com", "Sharon Goldman", "fortune.com") is None
    # 不是邮箱 → None
    assert _validate_email("sharon-goldman", "Sharon Goldman", "fortune.com") is None
    assert _validate_email(None, "Sharon Goldman", None) is None


def test_validate_linkedin_slug_must_match_name():
    assert _validate_linkedin("https://www.linkedin.com/in/sharongoldman", "Sharon Goldman") \
        == "https://www.linkedin.com/in/sharongoldman"
    assert _validate_linkedin("linkedin.com/in/sharon-goldman-12a", "Sharon Goldman").startswith("https://")
    # slug 与名字无关 → 拒
    assert _validate_linkedin("https://www.linkedin.com/in/john-smith", "Sharon Goldman") is None
    # 非 linkedin → None
    assert _validate_linkedin("https://twitter.com/x", "Sharon Goldman") is None
    assert _validate_linkedin(None, "Sharon Goldman") is None


def test_validate_twitter_blocks_org_handle():
    assert _validate_twitter("@sharongoldman", "Sharon Goldman", "fortune.com") == "@sharongoldman"
    assert _validate_twitter("https://x.com/sgoldman", "Sharon Goldman", "fortune.com") == "@sgoldman"
    # 机构 handle（含 outlet 主词）挡掉
    assert _validate_twitter("@fortune", "Sharon Goldman", "fortune.com") is None
    assert _validate_twitter(None, "Sharon Goldman", "fortune.com") is None


def test_validate_personal():
    assert _validate_personal("https://sharongoldman.com/about", "Sharon Goldman", "fortune.com")
    assert _validate_personal("https://fortune.com/author/sharon-goldman", "Sharon Goldman", "fortune.com")
    assert _validate_personal("https://random-blog.com/post", "Sharon Goldman", "fortune.com") is None
    assert _validate_personal(None, "Sharon Goldman", None) is None


def test_validate_contact_drops_mismatches():
    raw = {
        "linkedin": "https://www.linkedin.com/in/someone-else",  # slug 不匹配
        "twitter": "@sharong",
        "email": "sharon.goldman@fortune.com",
        "personal_url": "https://spam.com",                       # 随机站
    }
    out = _validate_contact("Sharon Goldman", "fortune.com", raw)
    assert out["linkedin"] is None
    assert out["twitter"] == "@sharong"
    assert out["email"] == "sharon.goldman@fortune.com"
    assert out["personal_url"] is None


def test_search_contact_via_articles(tmp_path):
    dbp = str(tmp_path / "t.db")
    db.init_schema(dbp)
    conn = db.get_conn(dbp)
    jid = db.upsert_journalist(
        conn, name="Sharon Goldman", name_normalized="sharon goldman",
        outlet="Fortune", outlet_uri="fortune.com", author_uri=None,
    )
    db.insert_article(
        conn, journalist_id=jid, url="http://x", title="t",
        body="reach me at sharon.goldman@fortune.com for tips",
        source_title="Fortune", published_at=None, sentiment=None, keyword_matched=None,
    )
    conn.commit()
    assert search_contact_via_articles(conn, jid, "Sharon Goldman", "fortune.com") \
        == "sharon.goldman@fortune.com"

    # 通用箱 / 别人邮箱不取
    jid2 = db.upsert_journalist(
        conn, name="John Roe", name_normalized="john roe",
        outlet="Fortune", outlet_uri="fortune.com", author_uri=None,
    )
    db.insert_article(
        conn, journalist_id=jid2, url="http://y", title="t",
        body="email press@fortune.com or sharon.goldman@fortune.com",
        source_title="Fortune", published_at=None, sentiment=None, keyword_matched=None,
    )
    conn.commit()
    assert search_contact_via_articles(conn, jid2, "John Roe", "fortune.com") is None
    conn.close()


def test_db_migration_idempotent_adds_linkedin(tmp_path):
    dbp = str(tmp_path / "m.db")
    db.init_schema(dbp)
    db.init_schema(dbp)  # 幂等：重复不报错
    conn = db.get_conn(dbp)
    jcols = {r["name"] for r in conn.execute("PRAGMA table_info(journalists)")}
    ecols = {r["name"] for r in conn.execute("PRAGMA table_info(enrichment)")}
    assert "linkedin" in jcols
    assert "verified_linkedin" in ecols
    conn.close()
