from journofinder.sources.newsapi_ai import WORD_LIMIT, _safe_keyword


def test_normal_keyword_unchanged():
    # 正常短语不动
    assert _safe_keyword("AI memory") == "AI memory"
    assert _safe_keyword("memory for AI agents") == "memory for AI agents"
    assert _safe_keyword("EverMind") == "EverMind"


def test_overlong_phrase_truncated_to_word_limit():
    long_phrase = " ".join(["w"] * 30)
    out = _safe_keyword(long_phrase)
    assert len(out.split()) == WORD_LIMIT  # 截断到 15 词，单关键词查询不会超限


def test_empty():
    assert _safe_keyword("") == ""
