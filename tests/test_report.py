"""report 展示 —— 只展示真实来源，按 LinkedIn > X > Email 优先级。"""

from __future__ import annotations

from journofinder.report import _contact_cell_html, _email_display, _primary_contact


def test_email_display_drops_author_uri_and_inferred():
    # NewsAPI author_uri 是标识符不是邮箱 → 绝不展示
    assert _email_display({"email_source": "author_uri", "email": "x_y@z.com"})[0] is None
    assert _email_display({"email_source": "inferred", "email": "x.y@z.com"})[0] is None
    # web（网搜）/ verified（深挖）才展示
    e, _l, c = _email_display({"email_source": "web", "email": "sharon.goldman@fortune.com"})
    assert e == "sharon.goldman@fortune.com" and c == "eml-web"
    e, _l, c = _email_display({"verified_email": "a@b.com"})
    assert e == "a@b.com" and c == "eml-verified"


def test_primary_contact_priority():
    assert _primary_contact({"best_linkedin": "https://linkedin.com/in/x"})
    assert _primary_contact({"best_twitter": "@x"})
    assert _primary_contact({"email_source": "web", "email": "a@b.com"})
    # 推测来源不算「有联系方式」
    assert not _primary_contact({"email_source": "author_uri", "email": "x_y@z.com"})
    assert not _primary_contact({})


def test_contact_cell_linkedin_first_then_x():
    cell = _contact_cell_html({
        "best_linkedin": "https://www.linkedin.com/in/sg",
        "best_twitter": "@sg",
        "email_source": "web", "email": "a@b.com",
    })
    assert "LinkedIn" in cell and "linkedin.com/in/sg" in cell

    cell = _contact_cell_html({"best_twitter": "@sg"})
    assert "x.com/sg" in cell

    # 历史 author_uri 邮箱 → 不展示，显示未公开
    cell = _contact_cell_html({"email_source": "author_uri", "email": "x_y@z.com"})
    assert "未公开" in cell
