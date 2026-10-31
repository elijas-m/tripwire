"""Small helpers shared across the pipeline."""

from __future__ import annotations

import hashlib
import html
import json
import math
import random
import re
import unicodedata
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit, urljoin, parse_qsl, urlencode

UTC = timezone.utc
_clock: Callable[[], datetime] | None = None

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.I)
_TRACKING_PREFIXES = ("utm_", "fbclid", "gclid", "yclid", "mc_", "ref_", "igshid")


def set_clock(fn: Callable[[], datetime] | None) -> None:
    """Point-in-time replay hook; None restores the wall clock."""
    global _clock
    _clock = fn


def utcnow() -> datetime:
    return _clock() if _clock is not None else datetime.now(UTC)


def now_ts() -> int:
    return int(utcnow().timestamp())


def to_ts(dt: datetime | None) -> int | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp())


def from_ts(ts: int | float | None) -> datetime | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(float(ts), UTC)


def parse_dt(value: Any) -> datetime | None:
    """Parse feed timestamps: datetime, struct_time, epoch seconds/ms, RFC/ISO strings."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, (list, tuple)) and len(value) >= 6:
        try:
            return datetime(*value[:6], tzinfo=UTC)
        except (TypeError, ValueError):
            return None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        if s.isdigit():
            return parse_dt(int(s))
        try:
            iso = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return iso.replace(tzinfo=UTC) if iso.tzinfo is None else iso.astimezone(UTC)
        except ValueError:
            pass
        try:
            parsed = parsedate_to_datetime(s)
            return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
        except (TypeError, ValueError, IndexError):
            return None
    return None


def ensure_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def norm_text(text: str | None) -> str:
    """Lowercase, unify ё, NFKC, collapse whitespace. Used for matching and hashing."""
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", text).lower().replace("ё", "е")
    s = _WS_RE.sub(" ", s)
    return s.strip()


def strip_html(text: str | None) -> str:
    if not text:
        return ""
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
    s = _TAG_RE.sub(" ", s)
    s = html.unescape(s)
    return _WS_RE.sub(" ", s).strip()


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0]
    return (cut or text[: limit - 1]).rstrip() + "…"


def canon_url(url: str | None) -> str:
    if not url:
        return ""
    url = url.strip()
    if not _SCHEME_RE.match(url):
        return url
    parts = urlsplit(url)
    scheme = "https" if parts.scheme in ("http", "https") else parts.scheme
    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith(_TRACKING_PREFIXES) and k.lower() not in ("ref", "source")]
    path = re.sub(r"/{2,}", "/", parts.path)
    if path.endswith("/") and len(path) > 1:
        path = path[:-1]
    return urlunsplit((scheme, netloc, path, urlencode(query), ""))


def domain_of(url: str | None) -> str:
    if not url:
        return ""
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return ""
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


_PLATFORM_DOMAINS = frozenset({"news.google.com", "t.me", "telegram.me", "x.com", "twitter.com"})


def publisher_domain(origin_domain: str | None, source_id: str, cfg: Any) -> str:
    """The publishing outlet, not the feed host.

    Google News and Telegram are transport platforms: several feeds share the
    same hostname, so counting the feed domain collapses independent outlets
    into one source. Prefer the configured publisher domain, then the source id.
    """
    origin = (origin_domain or "").strip().lower()
    source = getattr(cfg, "sources_by_id", {}).get(source_id)
    if origin and origin not in _PLATFORM_DOMAINS:
        return origin
    if source is not None:
        if source.origin_domains:
            return str(source.origin_domains[0]).lower()
        if origin in ("t.me", "telegram.me", "x.com", "twitter.com"):
            return source_id
        feed_host = domain_of(source.url)
        if feed_host and feed_host not in _PLATFORM_DOMAINS:
            return feed_host
    return source_id


def join_url(base: str, href: str) -> str:
    try:
        return urljoin(base, href)
    except ValueError:
        return href


def sha1(*parts: Any) -> str:
    h = hashlib.sha1()
    for part in parts:
        h.update(str(part or "").encode("utf-8", "ignore"))
        h.update(b"\x1f")
    return h.hexdigest()


def jitter_interval(interval_s: float, jitter: float, rng: random.Random | None = None) -> float:
    if interval_s <= 0:
        return 0.0
    r = rng or random
    return max(1.0, interval_s * (1.0 + r.uniform(-jitter, jitter)))


def safe_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        f = float(value)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


def safe_int(value: Any) -> int | None:
    f = safe_float(value)
    return int(f) if f is not None else None


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def json_loads(value: Any, default: Any = None) -> Any:
    if value is None or value == "":
        return default if default is not None else {}
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default if default is not None else {}


def detect_lang(text: str) -> str:
    if not text:
        return "en"
    heb = len(re.findall(r"[\u0590-\u05FF]", text))
    cyr = len(re.findall(r"[\u0400-\u04FF]", text))
    total = len(re.findall(r"\w", text)) or 1
    if heb / total > 0.2:
        return "he"
    if cyr / total > 0.2:
        return "ru"
    return "en"


def hamming64(a: int, b: int) -> int:
    return ((a ^ b) & 0xFFFFFFFFFFFFFFFF).bit_count()


def now_iso() -> str:
    return utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
