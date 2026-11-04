"""Deterministic freshness gate for alert sources.

Google News RSS sometimes links to a re-dated page: the publisher resets the
article timestamp while the underlying report or video is years old. This
module resolves the real article URL and reads the article's own dates. When
they are older than the alert's maximum age the alert is withheld.

Every network or parsing problem yields ``unknown`` and the alert proceeds:
this gate must never block delivery because a publisher is unreachable.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterator
from urllib.parse import quote, urlsplit

import httpx

from .models import Event
from .util import UTC, now_ts, parse_dt, strip_html, utcnow

log = logging.getLogger(__name__)

GOOGLE_NEWS_HOSTS = frozenset({"news.google.com"})
BATCH_URL = "https://news.google.com/_/DotsSplashUi/data/batchexecute"
MODEL_NAME = "deterministic-freshness"

_TS_RE = re.compile(r'data-n-a-ts="([^"]+)"')
_SG_RE = re.compile(r'data-n-a-sg="([^"]+)"')
_UNICODE_ESCAPE_RE = re.compile(r"\\u([0-9a-fA-F]{4})")
_GARTURL_TEXT_RE = re.compile(r'garturlres"\s*,\s*"(https?://[^"]+)"')
_JSONLD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.I | re.S,
)
_META_RE = re.compile(r"<meta\b[^>]*>", re.I)
_ATTR_RE = re.compile(r'([a-zA-Z:_-]+)\s*=\s*["\']([^"\']*)["\']')
_CBN_PUBLISH_RE = re.compile(r'"publish_date"\s*:\s*"(\d{1,2}-\d{1,2}-\d{4})"')
_CBN_UPDATE_RE = re.compile(r'"update_date"\s*:\s*"(\d{1,2}-\d{1,2}-\d{4})"')
_URL_DATE_RE = re.compile(r"/((?:19|20)\d{2})/(\d{2})/(\d{2})/")

_ARTICLE_TYPES = frozenset({
    "article", "newsarticle", "reportagenewsarticle", "blogposting",
    "analysisnewsarticle", "opinionnewsarticle", "techarticle",
})
_VIDEO_TYPES = frozenset({"videoobject", "videonewsobject"})

_PUBLISH_META = (
    "article:published_time", "datepublished", "og:published_time", "pubdate",
    "publish-date", "parsely-pub-date", "sailthru.date", "dc.date",
)
_MODIFIED_META = ("article:modified_time", "datemodified", "og:updated_time")
_UPLOAD_META = ("uploaddate", "video:release_date", "og:video:release_date")


@dataclass(slots=True)
class ArticleDates:
    published: datetime | None = None
    modified: datetime | None = None
    uploaded: datetime | None = None
    source: str = ""
    video_page: bool = False
    text: str = ""


@dataclass(slots=True)
class FreshnessResult:
    status: str = "skip"  # fresh | stale | unknown | skip
    reason: str = ""
    resolved_url: str = ""
    published_at: datetime | None = None
    modified_at: datetime | None = None
    uploaded_at: datetime | None = None
    date_source: str = ""
    article_text: str = ""
    checked: bool = False
    latency_ms: int = 0
    enforced: bool = True


class FreshnessChecker:
    def __init__(self, cfg, db, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.db = db
        conf: dict[str, Any] = getattr(cfg, "app_cfg", {}).get("alert_freshness") or {}
        self.enabled = bool(conf.get("enabled", False))
        self.mode = str(conf.get("mode") or "enforce").strip().lower()
        self.max_age_days = float(conf.get("max_age_days", 14))
        self.timeout_s = float(conf.get("timeout_s", 8.0))
        self.max_bytes = int(conf.get("max_bytes", 2_000_000))
        self.max_article_chars = int(conf.get("max_article_chars", 1500))
        self.min_level = int(conf.get("min_level", 2))
        self.user_agent = str(getattr(cfg, "app", {}).get("user_agent", "Tripwire/1.0"))
        self._transport = transport

    @property
    def enforced(self) -> bool:
        return self.mode != "shadow"

    async def check(
        self, event: Event, *, level: int, theater: str = "", trigger: str = "",
        claim: str = "",
    ) -> FreshnessResult:
        if not self.enabled or level < self.min_level or not event.url:
            return FreshnessResult(enforced=self.enforced)
        started = utcnow()
        try:
            result = await self._inspect(event.url)
        except Exception as exc:  # fail open on any transport or parsing problem
            result = FreshnessResult(
                status="unknown",
                reason=f"{type(exc).__name__}: {exc}"[:300],
            )
        result.enforced = self.enforced
        result.checked = result.status != "skip"
        result.latency_ms = int((utcnow() - started).total_seconds() * 1000)
        if result.status not in ("skip",):
            if result.status == "stale":
                log.warning(
                    "alert withheld as stale (%s %s): %s | %s",
                    theater, trigger or "correlation", result.reason,
                    (claim or event.title)[:160],
                )
            await self._record(event, level, theater, trigger, claim, result)
        return result

    async def _inspect(self, url: str) -> FreshnessResult:
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout_s),
            follow_redirects=True,
            headers={"User-Agent": self.user_agent},
            transport=self._transport,
        )
        async with client:
            targets = [url]
            if _is_google_news(url):
                targets = await self._resolve_google_news(client, url)
                if not targets:
                    return FreshnessResult(
                        status="unknown", reason="could not resolve Google News link",
                    )
            failure = FreshnessResult(status="unknown", reason="no article target")
            for target in targets:
                response = await client.get(
                    target, headers={"Accept": "text/html,application/xhtml+xml"},
                )
                if response.status_code != 200:
                    failure = FreshnessResult(
                        status="unknown",
                        reason=f"article HTTP {response.status_code}",
                        resolved_url=str(response.url),
                    )
                    continue
                ctype = response.headers.get("content-type", "").lower()
                if ctype and "html" not in ctype and not ctype.startswith("text/"):
                    failure = FreshnessResult(
                        status="unknown", reason=f"unexpected content type {ctype[:60]}",
                        resolved_url=str(response.url),
                    )
                    continue
                page = response.text[: self.max_bytes]
                resolved = str(response.url)
                dates = extract_dates(page, resolved)
                if dates.published or dates.modified or dates.uploaded:
                    return self._decide(dates, resolved)
                failure = self._decide(dates, resolved)
            return failure

    async def _resolve_google_news(
        self, client: httpx.AsyncClient, url: str,
    ) -> list[str]:
        art_id = _google_article_id(url)
        if not art_id:
            return []
        page = await client.get(url)
        if page.status_code != 200:
            return []
        ts = _TS_RE.search(page.text)
        sg = _SG_RE.search(page.text)
        if not ts or not sg:
            return []
        inner = (
            '["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,'
            'null,null,null,null,null,0,1],"X","X",1,[1,1,1],1,1,null,0,0,null,0],'
            f'"{art_id}",{ts.group(1)},"{sg.group(1)}"]'
        )
        body = "f.req=" + quote(json.dumps([[["Fbv4je", inner]]]))
        response = await client.post(
            BATCH_URL,
            content=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                "Referer": "https://news.google.com/",
            },
        )
        if response.status_code != 200:
            return []
        return parse_batch_urls(response.text)

    def _decide(self, dates: ArticleDates, resolved_url: str) -> FreshnessResult:
        now = utcnow()
        cutoff = now - timedelta(days=self.max_age_days)
        future = now + timedelta(days=2)

        def usable(value: datetime | None) -> datetime | None:
            if value is None or value > future or value.year < 1990:
                return None
            return value

        published = usable(dates.published)
        modified = usable(dates.modified)
        uploaded = usable(dates.uploaded)
        url_date = usable(_date_from_url(resolved_url))

        def old(value: datetime | None) -> bool:
            return value is not None and value < cutoff

        def day(value: datetime) -> str:
            return value.strftime("%Y-%m-%d")

        stale: list[str] = []
        if old(published):
            stale.append(f"article published {day(published)}")
        if old(uploaded) and (published is None or dates.video_page):
            stale.append(f"video uploaded {day(uploaded)}")
        if (old(modified) and published is not None
                and modified < published - timedelta(days=2)):
            stale.append(f"article date looks reset; content modified {day(modified)}")
        if not (published or modified or uploaded) and old(url_date):
            stale.append(f"URL date {day(url_date)}")

        status = "stale" if stale else ("fresh" if (published or modified or uploaded or url_date) else "unknown")
        if status == "fresh":
            reason = f"published {day(published)}" if published else "date inside window"
        elif status == "unknown":
            reason = "no publication date found"
        else:
            reason = "; ".join(stale)

        return FreshnessResult(
            status=status,
            reason=reason,
            resolved_url=resolved_url,
            published_at=published,
            modified_at=modified,
            uploaded_at=uploaded,
            date_source=dates.source,
            article_text=dates.text[: self.max_article_chars],
        )

    async def _record(
        self, event: Event, level: int, theater: str, trigger: str, claim: str,
        result: FreshnessResult,
    ) -> None:
        current = {"stale": 0, "fresh": 1}.get(result.status)
        try:
            await self.db.insert_alert_review(
                reviewed_at=now_ts(),
                event_id=event.id,
                cluster_id=event.cluster_id,
                theater=theater,
                level=level,
                trigger=trigger,
                model=MODEL_NAME,
                verdict=result.status,
                mode=self.mode,
                reason=result.reason,
                latency_ms=result.latency_ms,
                claim=(claim or event.title)[:300],
                event_title=event.title[:300],
                raw="",
                event_published_at=_ts(event.published_at),
                event_url=event.url[:500],
                resolved_url=result.resolved_url[:500],
                article_date=_ts(result.published_at),
                date_source=result.date_source[:300],
                current=current,
            )
        except Exception:
            log.warning("failed to store freshness check", exc_info=True)


def _ts(value: datetime | None) -> int | None:
    return int(value.timestamp()) if value else None


def _is_google_news(url: str) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    return host in GOOGLE_NEWS_HOSTS or (
        host.endswith("google.com") and "/rss/articles/" in parts.path
    )


def _google_article_id(url: str) -> str:
    path = urlsplit(url).path
    if "/articles/" not in path:
        return ""
    return path.split("/articles/", 1)[1].split("/", 1)[0].split("?", 1)[0]


def parse_batch_urls(text: str) -> list[str]:
    """Canonical and AMP article links from a Google batchexecute response.

    The payload is a JSON array whose only meaningful row is ``[wrb.fr,
    Fbv4je, "<inner JSON>" ...]``. The inner JSON is a *string*, and Google
    escapes non-alphanumerics in it (``=`` arrives as ``\\u003d``), so a plain
    regex over the raw body truncates the URL at the query string.
    """
    urls: list[str] = []
    body = text.strip()
    if body.startswith(")]}'"):
        body = body[4:]
    for line in body.splitlines():
        line = line.strip()
        if not line or line.isdigit():
            continue
        try:
            outer = json.loads(line)
        except ValueError:
            continue
        urls = _garturl_urls(outer)
        if urls:
            break
    if not urls:
        urls = _garturl_urls_from_text(text)
    return list(dict.fromkeys(urls))


def _garturl_urls(node: Any) -> list[str]:
    if isinstance(node, str):
        raw = node.strip()
        if not raw.startswith("["):
            return []
        try:
            inner = json.loads(raw)
        except ValueError:
            return []
        return _garturl_urls(inner)
    if isinstance(node, list):
        if node and node[0] == "garturlres":
            return [item for item in node[1:]
                    if isinstance(item, str) and item.startswith("http")]
        found: list[str] = []
        for item in node:
            found.extend(_garturl_urls(item))
        return found
    return []


def _garturl_urls_from_text(text: str) -> list[str]:
    unescaped = _UNICODE_ESCAPE_RE.sub(
        lambda match: chr(int(match.group(1), 16)), text,
    )
    unescaped = unescaped.replace('\\"', '"').replace("\\/", "/")
    return [match.group(1) for match in _GARTURL_TEXT_RE.finditer(unescaped)]


def _date_from_url(url: str) -> datetime | None:
    match = _URL_DATE_RE.search(urlsplit(url).path)
    if not match:
        return None
    try:
        return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)),
                        tzinfo=UTC)
    except ValueError:
        return None


def _parse_cbn(value: str) -> datetime | None:
    try:
        month, day, year = (int(part) for part in value.split("-"))
        return datetime(year, month, day, tzinfo=UTC)
    except (ValueError, TypeError):
        return None


def _first_date(values: list[Any]) -> datetime | None:
    for value in values:
        if not value:
            continue
        text = str(value).strip()
        parsed = _parse_cbn(text) if re.fullmatch(r"\d{1,2}-\d{1,2}-\d{4}", text) else parse_dt(text)
        if parsed is not None:
            return parsed
    return None


def _types(obj: dict[str, Any]) -> set[str]:
    raw = obj.get("@type")
    if isinstance(raw, str):
        return {raw.lower()}
    if isinstance(raw, list):
        return {str(item).lower() for item in raw}
    return set()


def _jsonld_objects(html: str) -> Iterator[dict[str, Any]]:
    for match in _JSONLD_RE.finditer(html):
        raw = match.group(1).strip()
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        stack: list[Any] = [data]
        depth = 0
        while stack and depth < 200:
            depth += 1
            node = stack.pop(0)
            if isinstance(node, list):
                stack.extend(node)
                continue
            if not isinstance(node, dict):
                continue
            yield node
            for key in ("@graph", "mainEntity", "mainEntityOfPage", "video", "associatedMedia"):
                child = node.get(key)
                if isinstance(child, (dict, list)):
                    stack.append(child)


def _meta(html: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for tag in _META_RE.findall(html):
        attrs = {name.lower(): value for name, value in _ATTR_RE.findall(tag)}
        key = (attrs.get("property") or attrs.get("name") or attrs.get("itemprop") or "").lower()
        value = attrs.get("content", "")
        if key and value and key not in out:
            out[key] = value
    return out


def extract_dates(html: str, url: str) -> ArticleDates:
    meta = _meta(html)
    published_jsonld: datetime | None = None
    modified_jsonld: datetime | None = None
    uploaded_jsonld: datetime | None = None
    article_body = ""
    saw_article = False
    saw_video = False
    for obj in _jsonld_objects(html):
        types = _types(obj)
        is_article = bool(types & _ARTICLE_TYPES)
        is_video = bool(types & _VIDEO_TYPES)
        saw_article = saw_article or is_article
        saw_video = saw_video or is_video
        if is_article or is_video:
            if published_jsonld is None:
                published_jsonld = _first_date([obj.get("datePublished")])
            if modified_jsonld is None:
                modified_jsonld = _first_date([obj.get("dateModified")])
            if not article_body and isinstance(obj.get("articleBody"), str):
                article_body = obj["articleBody"]
        if is_video and uploaded_jsonld is None:
            uploaded_jsonld = _first_date([obj.get("uploadDate")])

    published_meta = _first_date([meta.get(key) for key in _PUBLISH_META])
    modified_meta = _first_date([meta.get(key) for key in _MODIFIED_META])
    uploaded_meta = _first_date([meta.get(key) for key in _UPLOAD_META])
    cbn = _CBN_PUBLISH_RE.search(html)
    cbn_update = _CBN_UPDATE_RE.search(html)
    published_payload = _parse_cbn(cbn.group(1)) if cbn else None
    modified_payload = _parse_cbn(cbn_update.group(1)) if cbn_update else None

    published = published_jsonld or published_meta or published_payload
    modified = modified_jsonld or modified_meta or modified_payload
    uploaded = uploaded_jsonld or uploaded_meta

    source_parts = []
    if published_jsonld:
        source_parts.append("jsonld:datePublished")
    elif published_meta:
        source_parts.append("meta:published")
    elif published_payload:
        source_parts.append("page:publish_date")
    if modified_jsonld or modified_meta or modified_payload:
        source_parts.append("modified")
    if uploaded_jsonld or uploaded_meta:
        source_parts.append("uploadDate")

    path = urlsplit(url).path.lower()
    video_page = "/video" in path or (saw_video and not saw_article)
    text = article_body or meta.get("description") or meta.get("og:description") or ""
    if not text:
        text = strip_html(html)
    return ArticleDates(
        published=published,
        modified=modified,
        uploaded=uploaded,
        source=",".join(source_parts),
        video_page=video_page,
        text=re.sub(r"\s+", " ", text).strip()[:3000],
    )
