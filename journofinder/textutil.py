"""共享文本工具：记者名归一化、低质源过滤、时间窗口判断。

低质源 token 表参考 shanda-pulse 的 is_low_quality_source（博彩/SEO 农场）。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import urlparse

# 明显的垃圾/博彩/SEO 农场 token —— 命中即 drop（域名或标题里）
_SPAM_TOKENS = (
    "togel", "sbobet", "nusabet", "slot88", "judi", "casino", "gacor",
    "pkv", "bandar", "situs", "prediksi", "toto", "bet365",
)

# 通讯社 / 通稿 / 聚合 —— 不是可建联的独立记者署名，反查记者时直接忽略
_WIRE_DOMAINS = (
    "prnewswire.com", "businesswire.com", "globenewswire.com", "newswire.com",
    "einpresswire.com", "accesswire.com", "prweb.com",
)

_BY_PREFIX_RE = re.compile(r"^\s*(by|written by|por|von)\s+", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")


def normalize_author_name(name: str) -> str:
    """归一化记者名用于去重聚合：去 "by " 前缀、压空白、小写。

    "By Sarah Perez" / "sarah perez" / "Sarah  Perez" → "sarah perez"
    """
    if not name:
        return ""
    n = _BY_PREFIX_RE.sub("", name.strip())
    n = _WS_RE.sub(" ", n).strip().lower()
    return n


def looks_like_person(name: str) -> bool:
    """粗判是否像真人记者名（而非 "Editorial Team" / "Staff" / "Admin"）。

    反查记者时，机构署名没有建联价值，过滤掉。
    """
    if not name:
        return False
    nl = name.strip().lower()
    junk = ("staff", "team", "editor", "editorial", "newsroom", "admin",
            "desk", "reporter", "correspondent", "guest", "press release",
            "contributor", "news", "wire", "bot")
    # 纯机构词
    if any(nl == j or nl.startswith(j + " ") or nl.endswith(" " + j) for j in junk):
        return False
    # 至少两个词（名 + 姓），且不含数字
    words = [w for w in re.split(r"\s+", name.strip()) if w]
    if len(words) < 2:
        return False
    if any(any(ch.isdigit() for ch in w) for w in words):
        return False
    return True


def host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower().lstrip("www.")


def is_wire_source(outlet_uri: str | None, url: str | None = None) -> bool:
    """是否通讯社/通稿站。"""
    hay = " ".join(x for x in [(outlet_uri or "").lower(), (url or "").lower()] if x)
    return any(d in hay for d in _WIRE_DOMAINS)


def is_low_quality_source(url: str, title: str = "") -> bool:
    """博彩/SEO 农场过滤。"""
    hay = (url + " " + title).lower()
    return any(tok in hay for tok in _SPAM_TOKENS)


def to_iso(raw) -> str | None:
    """Event Registry 的 dateTime ('2026-04-21T12:30:00Z') 或 date ('2026-04-21') → ISO 8601。"""
    if not raw:
        return None
    s = str(raw).strip()
    if "T" in s:
        return s if (s.endswith("Z") or "+" in s) else s + "Z"
    return f"{s}T00:00:00Z"


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def is_in_time_window(published_at: str | None, since_iso: str | None, until_iso: str | None) -> bool:
    """published_at 是否落在 [since, until] 内。无日期 → 保守保留（True）。"""
    dt = _parse_iso(published_at)
    if dt is None:
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    lo = _parse_iso(since_iso)
    hi = _parse_iso(until_iso)
    if lo and lo.tzinfo is None:
        lo = lo.replace(tzinfo=timezone.utc)
    if hi and hi.tzinfo is None:
        hi = hi.replace(tzinfo=timezone.utc)
    if lo and dt < lo:
        return False
    if hi and dt > hi:
        return False
    return True
