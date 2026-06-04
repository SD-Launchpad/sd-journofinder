"""报告渲染 —— A/B 分层 HTML + CSV + MD。

🟢 Tier A：高相关高置信，强烈推荐建联，含 verified 联系方式 + sharp quotes。
🟡 Tier B：中度相关，可建联，含 inferred 邮箱。
drop 不进报告（但留在 DB，可人工 `tier` 命令捞回后重渲）。
"""

from __future__ import annotations

import csv
import html
import json
import sqlite3
from pathlib import Path
from typing import Any

from .config import BrandConfig

# 媒体层级分类（用于 Layer 1 媒体清单）。按 outlet 名/域名子串匹配。
_OUTLET_TIERS = [
    ("Tier-1 主流", ["wall street journal", "wsj", "new york times", "nytimes", "washington post",
                     "bloomberg", "reuters", "financial times", "ft.com", "economist", "forbes",
                     "cnbc", "guardian", "associated press", "ap news", "axios", "fortune",
                     "business insider", "the atlantic", "npr", "time.com", "time magazine", "cnn",
                     "vox", "politico", "the information"]),
    ("科技媒体", ["techcrunch", "the verge", "ars technica", "venturebeat", "engadget", "gizmodo",
                "mashable", "zdnet", "cnet", "digital trends", "tom's guide", "the register",
                "techradar", "xda", "android police", "makeuseof", "9to5", "semafor", "rest of world",
                "protocol", "lifehacker", "pcmag", "wired"]),
    ("AI 垂直", ["marktechpost", "towards ai", "the decoder", "analytics india", "infoq", "synced",
               "unite.ai", "the batch", "import ai", "deeplearning", "kdnuggets", "hugging face",
               "the rundown", "ben's bites", "dev community", "hackernoon"]),
]


def _classify_outlet(outlet: str | None, outlet_uri: str | None) -> str:
    hay = " ".join(x for x in [(outlet or "").lower(), (outlet_uri or "").lower()] if x)
    for label, toks in _OUTLET_TIERS:
        if any(t in hay for t in toks):
            return label
    if "substack.com" in hay or "medium.com" in hay or "blogspot" in hay or ".blog" in hay:
        return "Newsletter/博客"
    return "其他/地方"


_TIER_ORDER = {"Tier-1 主流": 0, "科技媒体": 1, "AI 垂直": 2, "Newsletter/博客": 3, "其他/地方": 4}


def _collect(conn: sqlite3.Connection, search_id: int) -> list[dict[str, Any]]:
    """汇总一次 search 的记者记录（join 各表），按 tier(A>B) + score 排序，drop 排除。"""
    rows = conn.execute(
        """
        SELECT j.id AS jid, j.name, j.outlet, j.outlet_uri, j.author_uri,
               j.email, j.email_source, j.twitter, j.personal_url,
               t.tier, t.rationale,
               rs.score, rs.reason AS score_reason,
               e.verified_email, e.verified_twitter, e.personal_url AS e_personal, e.recent_quotes_json,
               pa.angles_json,
               (SELECT COUNT(*) FROM articles a WHERE a.journalist_id = j.id) AS article_count,
               (SELECT MAX(published_at) FROM articles a WHERE a.journalist_id = j.id) AS latest_date
        FROM journo_tiers t
        JOIN journalists j ON j.id = t.journalist_id
        LEFT JOIN relevance_scores rs ON rs.journalist_id = j.id AND rs.search_id = t.search_id
        LEFT JOIN enrichment e ON e.journalist_id = j.id AND e.search_id = t.search_id
        LEFT JOIN pitch_angles pa ON pa.journalist_id = j.id AND pa.search_id = t.search_id
        WHERE t.search_id = ? AND t.tier IN ('A', 'B')
        ORDER BY CASE t.tier WHEN 'A' THEN 0 ELSE 1 END, rs.score DESC, article_count DESC
        """,
        (search_id,),
    ).fetchall()

    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        pkg = json.loads(r["angles_json"]) if r["angles_json"] else {}
        # 兼容旧格式（angles_json 曾是 list）与新格式（{angles, pitch}）
        if isinstance(pkg, list):
            d["angles"], d["pitch"] = pkg, {}
        else:
            d["angles"] = pkg.get("angles", [])
            d["pitch"] = pkg.get("pitch", {}) or {}
        d["quotes"] = json.loads(r["recent_quotes_json"]) if r["recent_quotes_json"] else []
        # 联系方式：verified 优先，否则用主表（可能是 inferred / author_uri）
        d["best_email"] = r["verified_email"] or r["email"]
        d["best_twitter"] = r["verified_twitter"] or r["twitter"]
        d["best_personal"] = r["e_personal"] or r["personal_url"]
        d["recent_articles"] = [
            dict(a) for a in conn.execute(
                "SELECT title, url, published_at FROM articles WHERE journalist_id = ? "
                "ORDER BY published_at DESC LIMIT 3", (r["jid"],)
            ).fetchall()
        ]
        out.append(d)
    return out


# ---------- HTML ----------

_CSS = """
body{font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;max-width:920px;margin:0 auto;padding:32px 20px;color:#1a1a1a;background:#fafafa}
h1{font-size:26px;margin:0 0 4px}
.sub{color:#666;margin:0 0 20px}
.legend{background:#fff;border:1px solid #eee;border-radius:10px;padding:12px 16px;margin-bottom:24px;font-size:13px;color:#444}
.sec{font-size:18px;margin:28px 0 12px;font-weight:600}
.card{background:#fff;border:1px solid #eaeaea;border-radius:12px;padding:18px 20px;margin-bottom:14px}
.card.A{border-left:4px solid #16a34a}
.card.B{border-left:4px solid #eab308}
.nm{font-size:17px;font-weight:600}
.tag{font-size:11px;font-weight:700;padding:2px 8px;border-radius:999px;vertical-align:middle;margin-left:8px}
.tag.A{background:#dcfce7;color:#15803d}
.tag.B{background:#fef9c3;color:#a16207}
.meta{color:#666;font-size:13px;margin:3px 0 10px}
.contact{font-size:13px;margin:8px 0;padding:8px 12px;background:#f6f8fa;border-radius:8px}
.contact .lbl{color:#888}
.eml-verified{color:#15803d;font-weight:600}
.eml-inferred{color:#a16207}
.rationale{font-size:13px;color:#555;font-style:italic;margin:6px 0}
.angles{margin:10px 0 0;padding-left:0;list-style:none}
.angles li{background:#f0f7ff;border-left:3px solid #3b82f6;padding:8px 12px;margin:6px 0;border-radius:0 8px 8px 0;font-size:14px}
.angles .ref{display:block;color:#888;font-size:12px;margin-top:3px}
.arts{font-size:12px;color:#777;margin-top:8px}
.arts a{color:#555}
.quotes{font-size:13px;margin:8px 0 0;padding-left:16px;color:#444}
table.media{width:100%;border-collapse:collapse;font-size:13px;background:#fff;border:1px solid #eaeaea;border-radius:10px;overflow:hidden;margin-bottom:8px}
table.media th{text-align:left;background:#f6f8fa;padding:8px 10px;color:#555;font-weight:600;border-bottom:1px solid #eaeaea}
table.media td{padding:7px 10px;border-bottom:1px solid #f0f0f0;vertical-align:top}
table.media .rp{color:#666;font-size:12px}
.mt{font-size:11px;font-weight:700;padding:2px 7px;border-radius:999px}
.mt-0{background:#dbeafe;color:#1d4ed8}.mt-1{background:#dcfce7;color:#15803d}.mt-2{background:#f3e8ff;color:#7e22ce}.mt-3{background:#fef9c3;color:#a16207}.mt-9,.mt-4{background:#f1f5f9;color:#64748b}
.pitch{margin:10px 0 0;padding:12px 14px;background:#fffdf5;border:1px solid #f0e6c8;border-radius:8px}
.pitch-h{font-size:12px;font-weight:700;color:#a16207;margin-bottom:6px}
.pitch-sub{font-size:13px;margin-bottom:6px}
.pitch-body{font-size:13px;color:#333;line-height:1.55;white-space:normal}
"""


def _contact_html(d: dict) -> str:
    bits = []
    email = d.get("best_email")
    if email:
        src = d.get("email_source") or ("verified" if d.get("verified_email") else "")
        cls = "eml-verified" if (d.get("verified_email") or src == "verified") else "eml-inferred"
        tag = "verified" if cls == "eml-verified" else ("author_uri" if src == "author_uri" else "inferred")
        bits.append(f'<span class="lbl">email:</span> <span class="{cls}">{html.escape(email)}</span> <span class="lbl">({tag})</span>')
    if d.get("best_twitter"):
        bits.append(f'<span class="lbl">twitter:</span> {html.escape(d["best_twitter"])}')
    if d.get("best_personal"):
        u = html.escape(d["best_personal"])
        bits.append(f'<span class="lbl">page:</span> <a href="{u}">{u}</a>')
    if not bits:
        bits.append('<span class="lbl">联系方式待补全</span>')
    return '<div class="contact">' + " · ".join(bits) + "</div>"


def _card_html(d: dict) -> str:
    tier = d["tier"]
    parts = [f'<div class="card {tier}">']
    parts.append(f'<div class="nm">{html.escape(d["name"] or "")}<span class="tag {tier}">[{tier}]</span></div>')
    meta = " · ".join(x for x in [
        html.escape(d.get("outlet") or "未知媒体"),
        f'score {d.get("score", "?")}',
        f'{d.get("article_count", 0)} 篇近期报道',
        (d.get("latest_date") or "")[:10],
    ] if x)
    parts.append(f'<div class="meta">{meta}</div>')
    parts.append(_contact_html(d))
    if d.get("rationale"):
        parts.append(f'<div class="rationale">分层理由：{html.escape(d["rationale"])}</div>')
    if d.get("angles"):
        parts.append('<ul class="angles">')
        for a in d["angles"]:
            ref = a.get("references_article") or ""
            parts.append(f'<li>{html.escape(a.get("angle",""))}'
                         + (f'<span class="ref">↳ 引用：{html.escape(ref)}</span>' if ref else "")
                         + "</li>")
        parts.append("</ul>")
    pitch = d.get("pitch") or {}
    if pitch.get("subject") or pitch.get("body"):
        parts.append('<div class="pitch"><div class="pitch-h">✉️ 可直接发的 pitch</div>')
        if pitch.get("subject"):
            parts.append(f'<div class="pitch-sub"><b>Subject:</b> {html.escape(pitch["subject"])}</div>')
        if pitch.get("body"):
            body_html = html.escape(pitch["body"]).replace("\n", "<br>")
            parts.append(f'<div class="pitch-body">{body_html}</div>')
        parts.append("</div>")
    if d.get("quotes"):
        parts.append('<ul class="quotes">')
        for q in d["quotes"][:3]:
            qt = html.escape(str(q.get("quote", "")))
            dt = html.escape(str(q.get("date", "")))
            parts.append(f'<li>“{qt}” <span class="lbl">({dt})</span></li>')
        parts.append("</ul>")
    if d.get("recent_articles"):
        links = " · ".join(
            f'<a href="{html.escape(a["url"] or "")}">{html.escape((a["title"] or "")[:60])}</a>'
            for a in d["recent_articles"] if a.get("url")
        )
        if links:
            parts.append(f'<div class="arts">近期报道：{links}</div>')
    parts.append("</div>")
    return "\n".join(parts)


# 媒体层级 → 为什么相关（权威性维度）
_TIER_WHY = {
    "Tier-1 主流": "主流大刊，AI 报道权威、决策者受众覆盖广，背书价值最高",
    "科技媒体": "科技垂直媒体，AI/agent 报道的核心阵地，开发者+行业受众",
    "AI 垂直": "AI 专业媒体，技术受众精准，懂 benchmark 与架构差异",
    "Newsletter/博客": "独立 newsletter/博客，垂直影响力，深度内容触达从业者",
    "其他/地方": "地方/行业媒体，补充长尾覆盖",
}


def _media_list(records: list[dict]) -> list[dict]:
    """Layer 1：按媒体聚合 → [{outlet, media_tier, n, has_A, reporters, why}]，
    按媒体层级(Tier-1→地方) + 记者数排序。why = 权威性 + 真实证据(记者近期写了什么)。"""
    by_outlet: dict[str, dict] = {}
    for r in records:
        key = (r.get("outlet") or r.get("outlet_uri") or "未知媒体")
        g = by_outlet.setdefault(key, {
            "outlet": key, "outlet_uri": r.get("outlet_uri"),
            "media_tier": _classify_outlet(r.get("outlet"), r.get("outlet_uri")),
            "n": 0, "has_A": False, "reporters": [], "evidence": [],
        })
        g["n"] += 1
        g["has_A"] = g["has_A"] or (r["tier"] == "A")
        g["reporters"].append(r.get("name") or "")
        for a in (r.get("recent_articles") or [])[:1]:  # 每位记者取最近 1 篇作证据
            t = (a.get("title") or "").strip()
            if t and t not in g["evidence"]:
                g["evidence"].append(t)
    for g in by_outlet.values():
        ev = "；".join(g["evidence"][:2])
        g["why"] = _TIER_WHY.get(g["media_tier"], "") + (f"。近 30 天相关报道：{ev}" if ev else "")
    out = list(by_outlet.values())
    out.sort(key=lambda g: (_TIER_ORDER.get(g["media_tier"], 9), -g["n"]))
    return out


def _media_list_html(records: list[dict]) -> str:
    rows = _media_list(records)
    parts = ['<div class="sec">📰 媒体清单（Layer 1 —— 先看相关媒体）</div>',
             '<table class="media"><thead><tr><th>媒体</th><th>层级</th><th>记者数</th><th>含强推</th><th>记者</th></tr></thead><tbody>']
    for g in rows:
        star = "🟢" if g["has_A"] else ""
        reporters = "、".join(html.escape(n) for n in g["reporters"] if n)
        parts.append(
            f'<tr><td><b>{html.escape(g["outlet"])}</b></td>'
            f'<td><span class="mt mt-{_TIER_ORDER.get(g["media_tier"],9)}">{html.escape(g["media_tier"])}</span></td>'
            f'<td>{g["n"]}</td><td>{star}</td><td class="rp">{reporters}</td></tr>'
        )
    parts.append("</tbody></table>")
    return "\n".join(parts)


def _journalist_table_html(records: list[dict]) -> str:
    """Layer 2 顶部：记者花名册（记者/媒体/层级/Tier/score/email），与媒体表呼应。"""
    parts = ['<table class="media jt"><thead><tr><th>记者</th><th>媒体</th><th>层级</th>'
             '<th>Tier</th><th>score</th><th>Email</th></tr></thead><tbody>']
    for d in records:
        mt = _classify_outlet(d.get("outlet"), d.get("outlet_uri"))
        tier = d["tier"]
        email = d.get("best_email") or "—"
        esrc = "verified" if d.get("verified_email") else (d.get("email_source") or "")
        parts.append(
            f'<tr><td><b>{html.escape(d.get("name") or "")}</b></td>'
            f'<td>{html.escape(d.get("outlet") or "未知")}</td>'
            f'<td><span class="mt mt-{_TIER_ORDER.get(mt,9)}">{html.escape(mt)}</span></td>'
            f'<td><span class="tag {tier}">{tier}</span></td>'
            f'<td>{d.get("score","")}</td>'
            f'<td class="rp">{html.escape(email)}'
            + (f' <span class="lbl">({esrc})</span>' if email != "—" and esrc else "")
            + '</td></tr>'
        )
    parts.append("</tbody></table>")
    return "\n".join(parts)


def _render_html(cfg: BrandConfig, records: list[dict]) -> str:
    a = [r for r in records if r["tier"] == "A"]
    b = [r for r in records if r["tier"] == "B"]
    a_cards = [_card_html(r) for r in a] or ['<p class="sub">（无）</p>']
    b_cards = [_card_html(r) for r in b] or ['<p class="sub">（无）</p>']
    body = [
        f"<h1>{html.escape(cfg.brand)} · 媒体/记者建联名单</h1>",
        f'<p class="sub">{html.escape(cfg.one_liner)}</p>',
        '<div class="legend">🟢 <b>Tier A</b>：高相关高置信，强烈推荐建联（verified 联系方式 + sharp quotes）　|　'
        '🟡 <b>Tier B</b>：中度相关，可建联（inferred 邮箱）。drop 已排除。</div>',
        _media_list_html(records),
        f'<div class="sec">👤 记者花名册（Layer 2 —— 共 {len(records)} 位，下方含分层理由 + 完整 pitch）</div>',
        _journalist_table_html(records),
        f'<div class="sec">🟢 Tier A — 强推（{len(a)}）</div>',
        *a_cards,
        f'<div class="sec">🟡 Tier B — 可建联（{len(b)}）</div>',
        *b_cards,
    ]
    return (f'<!doctype html><html lang="zh"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{html.escape(cfg.brand)} — journofinder</title>'
            f'<style>{_CSS}</style></head><body>' + "\n".join(body) + "</body></html>")


# ---------- CSV ----------

_CSV_COLS = ["tier", "name", "outlet", "media_tier", "outlet_uri", "score", "article_count", "latest_date",
             "email", "email_source", "twitter", "personal_url",
             "pitch_subject", "pitch_body", "angle_1", "angle_2", "angle_3", "tier_rationale"]


def _render_csv(records: list[dict], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(_CSV_COLS)
        for d in records:
            angles = [a.get("angle", "") for a in d.get("angles", [])][:3]
            angles += [""] * (3 - len(angles))
            esrc = "verified" if d.get("verified_email") else (d.get("email_source") or "")
            pitch = d.get("pitch") or {}
            w.writerow([
                d["tier"], d.get("name", ""), d.get("outlet", ""),
                _classify_outlet(d.get("outlet"), d.get("outlet_uri")), d.get("outlet_uri", ""),
                d.get("score", ""), d.get("article_count", 0), (d.get("latest_date") or "")[:10],
                d.get("best_email") or "", esrc, d.get("best_twitter") or "", d.get("best_personal") or "",
                pitch.get("subject", ""), pitch.get("body", ""),
                *angles, d.get("rationale", ""),
            ])


# ---------- MD ----------

def _render_md(cfg: BrandConfig, records: list[dict]) -> str:
    lines = [f"# {cfg.brand} · 媒体/记者建联名单", "", f"_{cfg.one_liner}_", ""]
    # Layer 1：媒体清单
    lines += ["## 📰 媒体清单（Layer 1）", "", "| 媒体 | 层级 | 记者数 | 含强推 | 记者 |", "|---|---|---|---|---|"]
    for g in _media_list(records):
        star = "🟢" if g["has_A"] else ""
        reporters = "、".join(n for n in g["reporters"] if n)
        lines.append(f"| {g['outlet']} | {g['media_tier']} | {g['n']} | {star} | {reporters} |")
    # Layer 2 顶部：记者花名册
    lines += ["", f"## 👤 记者花名册（Layer 2 —— 共 {len(records)} 位）", "",
              "| 记者 | 媒体 | 层级 | Tier | score | Email |", "|---|---|---|---|---|---|"]
    for d in records:
        mt = _classify_outlet(d.get("outlet"), d.get("outlet_uri"))
        email = d.get("best_email") or "—"
        lines.append(f"| {d.get('name','')} | {d.get('outlet') or '未知'} | {mt} | {d['tier']} | {d.get('score','')} | {email} |")
    lines += ["", "### 记者明细（分层理由 + angle + pitch）", ""]
    for tier, label in [("A", "🟢 Tier A — 强推"), ("B", "🟡 Tier B — 可建联")]:
        recs = [r for r in records if r["tier"] == tier]
        lines.append(f"## {label}（{len(recs)}）")
        lines.append("")
        for i, d in enumerate(recs, 1):
            lines.append(f"### {i}. {d.get('name','')} — {d.get('outlet') or '未知媒体'} [{tier}]")
            lines.append(f"- score {d.get('score','?')} · {d.get('article_count',0)} 篇近期报道 · {(d.get('latest_date') or '')[:10]}")
            if d.get("best_email"):
                esrc = "verified" if d.get("verified_email") else (d.get("email_source") or "inferred")
                lines.append(f"- email: {d['best_email']} ({esrc})")
            if d.get("best_twitter"):
                lines.append(f"- twitter: {d['best_twitter']}")
            if d.get("rationale"):
                lines.append(f"- 分层理由：{d['rationale']}")
            for a in d.get("angles", []):
                lines.append(f"- **angle**: {a.get('angle','')}" + (f" _(↳ {a.get('references_article')})_" if a.get("references_article") else ""))
            pitch = d.get("pitch") or {}
            if pitch.get("subject") or pitch.get("body"):
                lines.append("")
                lines.append(f"  ✉️ **Pitch — Subject:** {pitch.get('subject','')}")
                lines.append("")
                for para in (pitch.get("body","") or "").split("\n"):
                    lines.append(f"  > {para}")
            lines.append("")
    return "\n".join(lines)


def render(conn: sqlite3.Connection, search_id: int, cfg: BrandConfig, out_path: str | Path) -> dict:
    """渲染 HTML（out_path）+ 同名 .csv / .md。返回交付摘要。"""
    records = _collect(conn, search_id)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(_render_html(cfg, records), encoding="utf-8")
    csv_path = out_path.with_suffix(".csv")
    md_path = out_path.with_suffix(".md")
    _render_csv(records, csv_path)
    md_path.write_text(_render_md(cfg, records), encoding="utf-8")

    a = sum(1 for r in records if r["tier"] == "A")
    b = sum(1 for r in records if r["tier"] == "B")
    dropped = conn.execute(
        "SELECT COUNT(*) FROM journo_tiers WHERE search_id = ? AND tier = 'drop'", (search_id,)
    ).fetchone()[0]
    return {
        "search_id": search_id, "tier_a": a, "tier_b": b, "dropped": dropped,
        "html": str(out_path), "csv": str(csv_path), "md": str(md_path),
    }
