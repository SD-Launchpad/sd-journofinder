import tempfile
from pathlib import Path

from journofinder import aggregate, db


def _articles():
    return [
        {  # Sarah 两篇
            "url": "https://techcrunch.com/a1", "title": "AI agents go mainstream",
            "body": "...", "source_title": "TechCrunch", "source_uri": "techcrunch.com",
            "published_at": "2026-06-01T10:00:00Z", "sentiment": 0.2, "keyword_matched": "AI agents",
            "authors": [{"name": "Sarah Perez", "uri": "sarah_perez@techcrunch.com", "is_agency": False}],
        },
        {
            "url": "https://techcrunch.com/a2", "title": "Memory layers for agents",
            "body": "...", "source_title": "TechCrunch", "source_uri": "techcrunch.com",
            "published_at": "2026-06-03T10:00:00Z", "sentiment": 0.4, "keyword_matched": "agent memory",
            "authors": [{"name": "By Sarah Perez", "uri": "sarah_perez@techcrunch.com", "is_agency": False}],
        },
        {  # 通讯社整篇跳过
            "url": "https://prnewswire.com/x", "title": "Startup raises round",
            "body": "...", "source_title": "PR Newswire", "source_uri": "prnewswire.com",
            "published_at": "2026-06-02T10:00:00Z", "sentiment": 0.0, "keyword_matched": "AI memory",
            "authors": [{"name": "Press Office", "uri": "", "is_agency": True}],
        },
        {  # 机构署名跳过，但文章本身没有有效作者 → 不入库
            "url": "https://example.com/y", "title": "Roundup",
            "body": "...", "source_title": "Example", "source_uri": "example.com",
            "published_at": "2026-06-02T10:00:00Z", "sentiment": 0.0, "keyword_matched": "AI",
            "authors": [{"name": "Editorial Team", "uri": "", "is_agency": False}],
        },
    ]


def test_ingest_and_metrics():
    with tempfile.TemporaryDirectory() as d:
        dbp = Path(d) / "t.db"
        db.init_schema(dbp)
        conn = db.get_conn(dbp)
        try:
            aggregate.ingest_articles(conn, _articles())
            # 只应有 Sarah 一个记者（通讯社 + 机构都被过滤）
            metrics = aggregate.coverage_metrics(conn)
            assert len(metrics) == 1
            m = metrics[0]
            assert m["name"] == "Sarah Perez"
            assert m["outlet"] == "TechCrunch"
            assert m["author_uri"] == "sarah_perez@techcrunch.com"
            assert m["article_count"] == 2  # "By Sarah Perez" 归一化到同一人
            assert "Memory layers" in m["signal"]
            # top_articles 按时间倒序
            tops = aggregate.top_articles_for(conn, m["journalist_id"], limit=3)
            assert tops[0]["title"] == "Memory layers for agents"
        finally:
            conn.close()
