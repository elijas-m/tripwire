"""Core data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .util import UTC, json_dumps, json_loads, to_ts, from_ts

L0, L1, L2, L3 = 0, 1, 2, 3
LEVEL_NAMES = {L0: "NOISE", L1: "WATCH", L2: "WARNING", L3: "CRITICAL"}


@dataclass(slots=True)
class Source:
    id: str
    name: str
    type: str
    parser: str
    url: str
    tier: int = 3
    theater: str = "EU_RU"
    category: str | None = None
    interval_s: int = 600
    adaptive: bool = True
    max_items: int = 30
    max_age_hours: float = 48
    silence_hours: float = 72
    lang: str = "en"
    enabled: bool = True
    method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    body: str | None = None
    base_url: str | None = None
    link_pattern: str | None = None
    min_title_len: int = 8
    origin_domains: list[str] = field(default_factory=list)
    market_symbols: dict[str, dict] = field(default_factory=dict)
    timeout_s: int = 25
    jitter: float = 0.2

    @classmethod
    def from_dict(cls, raw: dict[str, Any], defaults: dict[str, Any]) -> "Source":
        data = {**defaults, **raw}
        headers = {**defaults.get("headers", {}), **raw.get("headers", {})}
        return cls(
            id=str(data["id"]),
            name=str(data.get("name") or data["id"]),
            type=str(data.get("type", "rss")),
            parser=str(data.get("parser", "rss")),
            url=str(data.get("url", "")),
            tier=int(data.get("tier", 3)),
            theater=str(data.get("theater", "EU_RU")),
            category=data.get("category"),
            interval_s=int(data.get("interval_s", 600)),
            adaptive=bool(data.get("adaptive", True)),
            max_items=int(data.get("max_items", 30)),
            max_age_hours=float(data.get("max_age_hours", 48)),
            silence_hours=float(data.get("silence_hours", 72)),
            lang=str(data.get("lang", "en")),
            enabled=bool(data.get("enabled", True)),
            method=str(data.get("method", "GET")).upper(),
            headers={str(k): str(v) for k, v in headers.items()},
            body=data.get("body"),
            base_url=data.get("base_url"),
            link_pattern=data.get("link_pattern"),
            min_title_len=int(data.get("min_title_len", 8)),
            origin_domains=[str(d).lower() for d in data.get("origin_domains", [])],
            market_symbols=data.get("market_symbols") or {},
            timeout_s=int(data.get("timeout_s", 25)),
            jitter=float(data.get("jitter", 0.2)),
        )


@dataclass(slots=True)
class RawItem:
    source_id: str
    title: str
    body: str = ""
    url: str = ""
    published_at: datetime | None = None
    lang: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    guid: str | None = None
    origin_domain: str | None = None
    category: str | None = None
    theater: str | None = None
    kind: str = "event"          # event | indicator | market
    country: str | None = None
    lat: float | None = None
    lon: float | None = None


@dataclass(slots=True)
class Event:
    id: int | None = None
    source_id: str = ""
    tier: int = 3
    url: str = ""
    guid_hash: str = ""
    title: str = ""
    body_excerpt: str = ""
    lang: str = "en"
    published_at: datetime | None = None
    ingested_at: datetime | None = None
    category: str = ""
    theater: str = ""
    country: str | None = None
    lat: float | None = None
    lon: float | None = None
    numeric_payload: dict[str, Any] = field(default_factory=dict)
    simhash: int = 0
    cluster_id: int | None = None
    score: float = 0.0
    level: int = L0
    matched_rules: list[str] = field(default_factory=list)
    origin_domain: str = ""
    status: str = "new"
    kind: str = "event"
    recorded_at: datetime | None = None

    def row(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "tier": self.tier,
            "url": self.url,
            "guid_hash": self.guid_hash,
            "title": self.title,
            "body_excerpt": self.body_excerpt,
            "lang": self.lang,
            "published_at": to_ts(self.published_at),
            "ingested_at": to_ts(self.ingested_at),
            "recorded_at": to_ts(self.recorded_at),
            "category": self.category,
            "theater": self.theater,
            "country": self.country,
            "lat": self.lat,
            "lon": self.lon,
            "numeric_payload": json_dumps(self.numeric_payload),
            "simhash": self.simhash,
            "cluster_id": self.cluster_id,
            "score": round(self.score, 3),
            "level": self.level,
            "matched_rules": json_dumps(self.matched_rules),
            "origin_domain": self.origin_domain,
            "status": self.status,
            "kind": self.kind,
        }

    @classmethod
    def from_row(cls, row: Any) -> "Event":
        keys = row.keys() if hasattr(row, "keys") else row
        return cls(
            id=row["id"],
            source_id=row["source_id"],
            tier=row["tier"],
            url=row["url"] or "",
            guid_hash=row["guid_hash"] or "",
            title=row["title"] or "",
            body_excerpt=row["body_excerpt"] or "",
            lang=row["lang"] or "en",
            published_at=from_ts(row["published_at"]),
            ingested_at=from_ts(row["ingested_at"]),
            recorded_at=from_ts(row["recorded_at"]) if "recorded_at" in keys else None,
            category=row["category"] or "",
            theater=row["theater"] or "",
            country=row["country"],
            lat=row["lat"],
            lon=row["lon"],
            numeric_payload=json_loads(row["numeric_payload"]),
            simhash=row["simhash"] or 0,
            cluster_id=row["cluster_id"],
            score=row["score"] or 0.0,
            level=row["level"] or L0,
            matched_rules=json_loads(row["matched_rules"], []),
            origin_domain=row["origin_domain"] or "",
            status=row["status"] or "new",
            kind=row["kind"] or "event",
        )


@dataclass(slots=True)
class Cluster:
    id: int | None = None
    theater: str = ""
    category: str = ""
    title_canonical: str = ""
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    sources_count: int = 0
    tiers_bitmap: int = 0
    total_score: float = 0.0
    score_initial: float | None = None
    level: int = L0
    tg_message_id: int | None = None
    tg_thread_id: int | None = None
    state: str = "open"
    country: str | None = None
    lat: float | None = None
    lon: float | None = None
    last_alert_at: datetime | None = None
    ack_at: datetime | None = None
    tripwire_id: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TripwireHit:
    id: str
    level: int
    why: str
    one_liner: str
    payload: dict[str, Any] = field(default_factory=dict)
    theater: str | None = None
    confirmed: bool = True
    confirm_reason: str = ""
    horizon: str = ""
    refute: str = ""
    next_tripwire: str = ""


@dataclass(slots=True)
class ScoreResult:
    score: float
    base: float
    matched: list[str]
    factors: dict[str, float]
    dropped: bool = False
    drop_reason: str = ""
