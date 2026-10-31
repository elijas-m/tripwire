"""Normalizer: RawItem -> Event."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from .geo import GeoIndex
from .models import Event, RawItem, Source
from .simhash import compute as simhash_compute
from .util import (
    canon_url, detect_lang, domain_of, norm_text, sha1, strip_html, truncate, utcnow,
)


def _stopwords(cfg: Any) -> set[str]:
    out: set[str] = set()
    for words in (cfg.lexicon.get("stopwords") or {}).values():
        out.update(w.lower() for w in words)
    return out


class Normalizer:
    def __init__(self, cfg: Any, geo: GeoIndex) -> None:
        self.cfg = cfg
        self.geo = geo
        self.stopwords = _stopwords(cfg)
        self.max_age = float(cfg.app.get("max_age_hours", 48))
        self.max_age_seismic = float(cfg.app.get("max_age_hours_seismic", 3))
        self.category_terms = {
            cat: [re.compile(r"(?i)" + term.replace("*", r"\w*")) for term in terms]
            for cat, terms in (cfg.lexicon.get("category_terms") or {}).items()
        }

    def infer_category(self, text: str, source: Source) -> str:
        if source.category:
            return source.category
        best, best_hits = "", 0
        for cat, patterns in self.category_terms.items():
            hits = sum(1 for p in patterns if p.search(text))
            if hits > best_hits:
                best, best_hits = cat, hits
        return best or "general"

    def normalize(self, raw: RawItem, source: Source) -> Event | None:
        title = strip_html(raw.title)
        body = strip_html(raw.body)
        if not title and not body:
            return None
        if not title:
            title = truncate(body, 180)
        now = utcnow()
        published = raw.published_at or now
        if published > now + timedelta(hours=6):
            published = now
        category = raw.category or self.infer_category(f"{title} {body[:400]}", source)
        max_age = self.max_age_seismic if category == "earthquake" else min(self.max_age, source.max_age_hours)
        if published < now - timedelta(hours=max_age):
            return None

        theater = raw.theater or source.theater
        country, lat, lon = raw.country, raw.lat, raw.lon
        if country is None or lat is None:
            g_country, g_lat, g_lon = self.geo.geotag(title, body, raw.payload, theater)
            country = country or g_country
            if lat is None:
                lat, lon = g_lat, g_lon

        if theater in ("EU_RU", "ISRAEL", "GULF"):
            inferred = self.geo.theater_for_country(country)
            if inferred and not self.geo.country_for_theater(theater, country):
                # Keep the source theater label; geo_factor will down-weight it.
                pass

        # "US deploys forces to the Middle East" belongs to the destination
        # theater, not to the wire's home theater. The actor's location is not
        # the event location, so drop the actor-derived geography too.
        projected = False
        if theater in ("EU_RU", "ISRAEL", "GULF") and self.geo.is_force_projection(title):
            destination = self.geo.force_projection_theater(title)
            if destination and destination != theater:
                theater = destination
                country = lat = lon = None
                projected = True

        lang = raw.lang or source.lang or detect_lang(f"{title} {body[:300]}")
        url = canon_url(raw.url)
        guid_hash = sha1(source.id, raw.guid or url or title)
        sim = simhash_compute(title, body, self.stopwords)
        payload = dict(raw.payload or {})
        if projected:
            for key in ("country", "lat", "lon"):
                payload.pop(key, None)
        if country:
            payload.setdefault("country", country)
        if lat is not None:
            payload.setdefault("lat", lat)
            payload.setdefault("lon", lon)

        return Event(
            source_id=source.id,
            tier=source.tier,
            url=url,
            guid_hash=guid_hash,
            title=truncate(title, 400),
            body_excerpt=truncate(body, 1500),
            lang=lang,
            published_at=published,
            ingested_at=now,
            category=category,
            theater=theater,
            country=country,
            lat=lat,
            lon=lon,
            numeric_payload=payload,
            simhash=sim,
            origin_domain=raw.origin_domain or domain_of(url),
            kind=raw.kind,
        )
