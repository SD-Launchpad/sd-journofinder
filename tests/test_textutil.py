from journofinder.textutil import (
    is_in_time_window,
    is_low_quality_source,
    is_wire_source,
    looks_like_person,
    normalize_author_name,
)


def test_normalize_author_name():
    assert normalize_author_name("By Sarah Perez") == "sarah perez"
    assert normalize_author_name("  Sarah   Perez  ") == "sarah perez"
    assert normalize_author_name("written by Markus Kasanmascheff") == "markus kasanmascheff"
    assert normalize_author_name("") == ""


def test_looks_like_person():
    assert looks_like_person("Sarah Perez")
    assert looks_like_person("Markus Kasanmascheff")
    # 机构 / 单词 / 含数字的不算真人
    assert not looks_like_person("Editorial Team")
    assert not looks_like_person("Staff")
    assert not looks_like_person("Admin")
    assert not looks_like_person("Newsroom")
    assert not looks_like_person("Bot 9000")
    assert not looks_like_person("Reuters")  # 单词


def test_is_low_quality_source():
    assert is_low_quality_source("https://togel-gacor.com/x", "")
    assert is_low_quality_source("https://x.com/p", "Situs Slot88 Terbaik")
    assert not is_low_quality_source("https://techcrunch.com/2026/06/04/foo", "Apple approves Poke")


def test_is_wire_source():
    assert is_wire_source("prnewswire.com")
    assert is_wire_source(None, "https://www.businesswire.com/news/x")
    assert not is_wire_source("techcrunch.com", "https://techcrunch.com/x")


def test_is_in_time_window():
    since, until = "2026-05-01T00:00:00Z", "2026-06-04T23:59:59Z"
    assert is_in_time_window("2026-05-20T10:00:00Z", since, until)
    assert not is_in_time_window("2026-04-01T10:00:00Z", since, until)
    assert not is_in_time_window("2026-07-01T10:00:00Z", since, until)
    # 无日期保守保留
    assert is_in_time_window(None, since, until)
