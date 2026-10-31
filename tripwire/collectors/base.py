"""Collector result and HTTP-based collectors."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import timedelta
from urllib.parse import urlencode

from ..fetch import FetchResult, Fetcher
from ..models import RawItem, Source
from ..parsers import extract_vaac_advisory, parse
from ..util import utcnow

VAAC_PARSERS = {"vaac_tokyo", "vaac_messages", "vaac_list"}


@dataclass(slots=True)
class CollectorResult:
    source_id: str
    items: list[RawItem] = field(default_factory=list)
    fetch: FetchResult | None = None
    error: str = ""
    not_modified: bool = False

    @property
    def ok(self) -> bool:
        return not self.error and self.fetch is not None and self.fetch.ok


class HttpCollector:
    """Fetches over HTTP and dispatches to the configured parser."""

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self._vaac_cache: dict[str, dict] = {}

    async def run(self, source: Source, fetcher: Fetcher, health: dict) -> CollectorResult:
        result = await fetcher.fetch(source, health)
        if not result.ok:
            return CollectorResult(source.id, error=result.error or f"HTTP {result.status}", fetch=result)
        if result.not_modified:
            return CollectorResult(source.id, not_modified=True, fetch=result)
        items = parse(source, result.content, self.cfg)
        if source.parser in VAAC_PARSERS:
            await self._enrich_vaac(items, source, fetcher)
        return CollectorResult(source.id, items=items, fetch=result)

    async def _enrich_vaac(self, items: list[RawItem], source: Source, fetcher: Fetcher) -> None:
        """Read the newest advisory bodies so ash height/colour reach the domain rules."""
        cutoff = utcnow() - timedelta(hours=12)
        candidates = sorted(
            (
                item for item in items
                if item.url and item.published_at and item.published_at >= cutoff
                and "ash_ft" not in item.payload and "colour_code" not in item.payload
            ),
            key=lambda item: item.published_at,
            reverse=True,
        )[:2]
        for item in candidates:
            info = self._vaac_cache.get(item.url)
            if info is None:
                probe = replace(source, url=item.url, headers={}, method="GET", timeout_s=15)
                fetched = await fetcher.fetch(probe)
                info = extract_vaac_advisory(fetched.text) if fetched.ok and fetched.text else {}
                if len(self._vaac_cache) >= 512:
                    self._vaac_cache.clear()
                self._vaac_cache[item.url] = info
            item.payload.update(info)
            if info:
                item.payload.setdefault("advisory_text", True)


class RSSCollector(HttpCollector):
    pass


class JSONCollector(HttpCollector):
    pass


class HTMLCollector(HttpCollector):
    pass


class TelegramCollector(HttpCollector):
    pass


class MarketCollector(HttpCollector):
    pass


class SafecastCollector(HttpCollector):
    """Safecast needs a rolling time window; the filter goes into the URL per poll.

    The window covers the indicator's baseline plus one hour of slack, which
    keeps the payload around ~60 KB per poll.
    """

    async def run(self, source: Source, fetcher: Fetcher, health: dict) -> CollectorResult:
        settings = self.cfg.g("radiation") if hasattr(self.cfg, "g") else None
        hours = float((settings or {}).get("baseline_hours") or 6.0) + 1.0
        now = utcnow()
        params = {
            "unit": "usv",
            "per_page": "1000",
            "captured_after": (now - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "captured_before": (now + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        sep = "&" if "?" in source.url else "?"
        url = f"{source.url}{sep}{urlencode(params)}"
        # The URL changes every poll, so a cached ETag cannot match a new window.
        return await super().run(replace(source, url=url), fetcher, {})


def make_collector(source: Source, cfg):
    from .ws import WSCollector

    if source.type == "ws":
        return WSCollector(cfg)
    if source.type == "safecast":
        return SafecastCollector(cfg)
    return HttpCollector(cfg)
