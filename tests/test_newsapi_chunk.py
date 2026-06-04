from journofinder.sources.newsapi_ai import WORD_LIMIT, _chunk_by_words


def _words(group):
    return sum(max(1, len(k.split())) for k in group)


def test_every_group_within_word_limit():
    # 模拟 EverMind 的关键词（brand + themes + competitors，含多词短语）
    kws = [
        "EverMind", "AI memory", "long-term memory", "agent memory", "LLM memory",
        "memory for AI agents", "persistent memory", "self-evolving AI", "AI agents", "AI model",
        "memOS", "mem0", "Letta", "Zep", "Hyperspell", "MemGPT", "Cognee", "Supermemory",
    ]
    groups = _chunk_by_words(kws)
    assert groups, "应至少分出一组"
    for g in groups:
        assert _words(g) <= WORD_LIMIT, f"组词数超限: {g} = {_words(g)}"
    # 不丢关键词
    flat = [k for g in groups for k in g]
    assert flat == kws


def test_overlong_phrase_truncated():
    long_phrase = " ".join(["w"] * 30)
    groups = _chunk_by_words([long_phrase, "AI memory"])
    for g in groups:
        assert _words(g) <= WORD_LIMIT


def test_short_list_single_group():
    groups = _chunk_by_words(["AI memory", "agent memory"])
    assert len(groups) == 1  # 4 词，一组装得下
