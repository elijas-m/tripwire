"""Deduplication and world-event clustering."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .geo import GeoIndex, haversine_km
from .models import Cluster, Event
from .simhash import distance as simhash_distance
from .util import norm_text, now_ts, utcnow

# Physical events share a location by nature; a headline match is not needed.
GEOPHYSICAL_CATEGORIES = frozenset({"earthquake", "volcano", "tsunami", "disaster"})


@dataclass(slots=True)
class DedupResult:
    event: Event
    duplicate: bool = False
    reason: str = ""
    cluster: Cluster | None = None
    novelty: float = 1.0
    is_new_cluster: bool = True
    revision: Event | None = None
    aftershock: bool = False


class Deduper:
    def __init__(self, cfg, geo: GeoIndex) -> None:
        self.cfg = cfg
        self.geo = geo
        self.stopwords: set[str] = set()
        for words in (cfg.lexicon.get("stopwords") or {}).values():
            self.stopwords.update(w.lower() for w in words)
        # Category vocabulary ("volcanic", "ash", "earthquake") is not an entity.
        self.generic_tokens: set[str] = {"utc", "advisory", "issued"}
        for terms in (cfg.lexicon.get("category_terms") or {}).values():
            for term in terms:
                self.generic_tokens.update(
                    re.findall(r"[\w\u0400-\u04FF\u0590-\u05FF]{4,}",
                               norm_text(term.replace("*", "")))
                )
        cooldowns = cfg.thresholds.get("cooldowns", {})
        self.cluster_window_h = float(cooldowns.get("cluster_window_hours", 12))
        self.aftershock_window_h = float(cooldowns.get("aftershock_window_hours", 72))
        self.aftershock_radius_km = float(cooldowns.get("aftershock_radius_km", 150))
        self.hamming_dup = int(cfg.thresholds.get("independence", {}).get("simhash_hamming_dup", 6))

    async def check_exact(self, event: Event, db) -> DedupResult | None:
        if await db.seen_has(event.guid_hash):
            previous = await db.event_by_guid_hash(event.guid_hash)
            cluster = None
            if previous is not None and previous.cluster_id:
                cluster = await db.get_cluster(previous.cluster_id)
            if previous is not None and self.is_revision(previous, event):
                return DedupResult(event, duplicate=False, reason="revision", cluster=cluster,
                                   novelty=1.0, is_new_cluster=False, revision=previous)
            return DedupResult(event, duplicate=True, reason="guid", cluster=cluster)
        if event.url:
            row = await db.event_by_url(event.url)
            if row:
                cluster = await db.get_cluster(row["cluster_id"]) if row["cluster_id"] else None
                if cluster:
                    await db.add_cluster_source(cluster.id, event.source_id)
                return DedupResult(event, duplicate=True, reason="url", cluster=cluster,
                                   novelty=0.4, is_new_cluster=False)
        return None

    @staticmethod
    def _severity(event: Event) -> tuple[float, float]:
        """Comparable severity for revision detection: (metric, rank)."""
        p = event.numeric_payload
        if "advisory_level" in p or "departure" in p:
            # Travel advisories keep a stable GUID across updates, so a new
            # departure order would otherwise be swallowed as a duplicate.
            departure = {"ordered": 2, "authorized": 1}.get(
                str(p.get("departure") or "").lower(), 0)
            try:
                level = int(p.get("advisory_level") or 0)
            except (TypeError, ValueError):
                level = 0
            return 0.0, departure * 10 + level
        if event.category == "earthquake":
            return float(p.get("mag") or 0.0), {"red": 3, "orange": 2, "yellow": 1}.get(
                str(p.get("alert") or "").lower(), 0)
        if event.category == "volcano":
            rank = {"red": 3, "orange": 2, "yellow": 1}.get(str(p.get("colour_code") or "").lower(), 0)
            return float(p.get("ash_ft") or p.get("vei") or 0.0), rank
        if event.category == "tsunami":
            return 0.0, {"warning": 3, "watch": 2, "advisory": 1}.get(
                str(p.get("message_type") or "").lower(), 0)
        return 0.0, 0

    def is_revision(self, previous: Event, event: Event) -> bool:
        """USGS/VAAC revise payloads in place; spec: mag delta >=0.3 or severity crossing up."""
        old_metric, old_rank = self._severity(previous)
        new_metric, new_rank = self._severity(event)
        if new_rank > old_rank:
            return True
        if event.category == "earthquake":
            return new_metric - old_metric >= 0.3
        if event.category == "volcano" and new_metric > old_metric:
            return True
        return False

    async def find_near_duplicate(self, event: Event, db) -> Event | None:
        since = now_ts() - 24 * 3600
        rows = await db.fetchall(
            """SELECT * FROM events
               WHERE ingested_at>=? AND simhash<>0 AND theater=? AND category=?
               ORDER BY ingested_at DESC LIMIT 400""",
            (since, event.theater, event.category),
        )
        event_volcano = self._volcano(event)
        for row in rows:
            if event_volcano:
                if self._volcano(Event.from_row(row)) not in ("", event_volcano):
                    continue
            if simhash_distance(event.simhash, row["simhash"]) <= self.hamming_dup:
                return Event.from_row(row)
        return None

    @staticmethod
    def _volcano(event: Event) -> str:
        if event.category != "volcano":
            return ""
        name = event.numeric_payload.get("volcano_name")
        if not name:
            name = event.title.split(":")[0]
        return norm_text(str(name))

    async def assign(self, event: Event, db) -> DedupResult:
        exact = await self.check_exact(event, db)
        if exact:
            return exact

        existing = await self.find_near_duplicate(event, db)
        if existing and existing.cluster_id:
            cluster = await db.get_cluster(existing.cluster_id)
            if cluster and cluster.state == "open":
                await db.add_cluster_source(cluster.id, event.source_id)
                cluster.last_seen = event.ingested_at or utcnow()
                await db.update_cluster(cluster)
                await db.seen_add(event.guid_hash, "near-dup")
                return DedupResult(event, duplicate=True, reason="simhash",
                                   cluster=cluster, novelty=0.4, is_new_cluster=False)

        cluster = await self.find_cluster(event, db)
        if cluster:
            mag = float(event.numeric_payload.get("mag") or 0.0)
            meta = dict(cluster.meta or {})
            main_mag = float(meta.get("main_mag") or 0.0)
            if event.category == "earthquake" and main_mag and mag > main_mag + 0.2:
                # Stronger shock than the cluster's main event: close old cluster, start a new one.
                cluster.state = "superseded"
                await db.update_cluster(cluster)
                cluster = None
            else:
                await db.add_cluster_source(cluster.id, event.source_id)
                cluster.last_seen = event.ingested_at or utcnow()
                cluster.total_score = max(cluster.total_score, 0.0)
                cluster.tiers_bitmap = cluster.tiers_bitmap | (1 << event.tier)
                meta["count"] = int(meta.get("count", 0)) + 1
                aftershock = bool(
                    event.category == "earthquake" and main_mag and mag and mag <= main_mag + 0.2
                )
                if aftershock:
                    meta["aftershocks"] = int(meta.get("aftershocks", 0)) + 1
                cluster.meta = meta
                await db.update_cluster(cluster)
                await db.seen_add(event.guid_hash, "cluster")
                return DedupResult(event, cluster=cluster, novelty=0.4,
                                   is_new_cluster=False, aftershock=aftershock)

        mag = float(event.numeric_payload.get("mag") or 0.0)
        cluster = Cluster(
            theater=event.theater,
            category=event.category,
            title_canonical=event.title,
            first_seen=event.ingested_at,
            last_seen=event.ingested_at,
            sources_count=1,
            tiers_bitmap=1 << event.tier,
            total_score=0.0,
            level=0,
            state="open",
            country=event.country,
            lat=event.lat,
            lon=event.lon,
            meta={"count": 1, **({"main_mag": mag} if event.category == "earthquake" and mag else {})},
        )
        await db.insert_cluster(cluster)
        await db.add_cluster_source(cluster.id, event.source_id)
        await db.seen_add(event.guid_hash, "new")
        return DedupResult(event, cluster=cluster, novelty=1.0, is_new_cluster=True)

    async def find_cluster(self, event: Event, db) -> Cluster | None:
        window_h = self.aftershock_window_h if event.category == "earthquake" else self.cluster_window_h
        since = now_ts() - int(window_h * 3600)
        clusters = await db.open_clusters(event.theater, event.category, since)
        if not clusters:
            return None

        places = {p.name for p in self.geo.match_places(f"{event.title} {event.body_excerpt[:500]}")}
        tokens = self._title_tokens(event.title)
        event_volcano = self._volcano(event)
        best: Cluster | None = None
        best_score = -1.0

        for cluster in clusters:
            if event_volcano:
                cluster_volcano = norm_text(cluster.title_canonical.split(":")[0])
                if cluster_volcano and cluster_volcano != event_volcano and (
                    event_volcano not in cluster_volcano and cluster_volcano not in event_volcano
                ):
                    continue
            score = 0.0
            radius = self.aftershock_radius_km if event.category == "earthquake" else 150.0
            if (event.lat is not None and event.lon is not None and
                    cluster.lat is not None and cluster.lon is not None):
                dist = haversine_km(event.lat, event.lon, cluster.lat, cluster.lon)
                if dist <= radius:
                    # A shared city must not merge unrelated political stories:
                    # for non-physical events coordinates alone are only a hint.
                    score += 3.0 if event.category in GEOPHYSICAL_CATEGORIES else 1.0
                    if event.category == "earthquake":
                        mag = float(event.numeric_payload.get("mag") or 0)
                        if mag >= 4.0:
                            score += 1.0
            if places and cluster.title_canonical:
                cluster_places = {p.name for p in self.geo.match_places(cluster.title_canonical)}
                shared = places & cluster_places
                if shared:
                    score += 1.0 + min(len(shared), 3)
            if tokens and cluster.title_canonical:
                cluster_tokens = self._title_tokens(cluster.title_canonical)
                shared_tokens = tokens & cluster_tokens
                if len(shared_tokens) >= 3:
                    score += 2.0 + 0.5 * min(len(shared_tokens) - 2, 2)
                elif len(shared_tokens) == 2:
                    # Two common words ("центре", "человек") are not enough;
                    # require the overlap to dominate both short headlines.
                    union = tokens | cluster_tokens
                    if union and len(shared_tokens) / len(union) >= 0.4:
                        score += 2.0
            if score > best_score:
                best, best_score = cluster, score
        return best if best_score >= 2.0 else None

    def _title_tokens(self, title: str) -> set[str]:
        tokens = re.findall(r"[\w\u0400-\u04FF\u0590-\u05FF]{4,}", norm_text(title))
        return {
            t for t in tokens
            if t not in self.stopwords and t not in self.generic_tokens and not t.isdigit()
        }
