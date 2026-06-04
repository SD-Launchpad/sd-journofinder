from journofinder.enrich import _email_from_author_uri, _infer_email, resolve_email


def test_email_from_author_uri():
    # NewsAPI.ai 常给 first_last@domain 真实邮箱
    assert _email_from_author_uri("sarah_perez@techcrunch.com") == "sarah_perez@techcrunch.com"
    assert _email_from_author_uri("markus_kasanmascheff@winbuzzer.com") == "markus_kasanmascheff@winbuzzer.com"
    # 非邮箱形态返回 None
    assert _email_from_author_uri("sarah-perez") is None
    assert _email_from_author_uri(None) is None


def test_infer_email():
    assert _infer_email("Sarah Perez", "techcrunch.com") == "sarah.perez@techcrunch.com"
    assert _infer_email("Sarah Perez", "www.theverge.com") == "sarah.perez@theverge.com"
    # 中间名取首尾
    assert _infer_email("Mary Jane Watson", "nyt.com") == "mary.watson@nyt.com"
    # 缺域名 / 单名 → None
    assert _infer_email("Sarah Perez", None) is None
    assert _infer_email("Cher", "techcrunch.com") is None
    # 平台托管域名不推断假邮箱（medium/substack/youtube...）
    assert _infer_email("Jaroslaw Wasowski", "medium.com") is None
    assert _infer_email("Rohit Ghumare", "alphasignalai.substack.com") is None
    assert _infer_email("Some One", "youtube.com") is None


def test_resolve_email_priority():
    # author_uri 邮箱优先
    e, src = resolve_email("Sarah Perez", "techcrunch.com", "sarah_perez@techcrunch.com")
    assert e == "sarah_perez@techcrunch.com" and src == "author_uri"
    # 无 author_uri 时退到规则推断
    e, src = resolve_email("Sarah Perez", "techcrunch.com", None)
    assert e == "sarah.perez@techcrunch.com" and src == "inferred"
    # 都拿不到
    e, src = resolve_email("Cher", None, None)
    assert e is None and src is None
