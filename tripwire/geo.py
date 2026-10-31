"""Gazetteer matching and theater geo-fencing."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from .util import norm_text

_WORD_CHARS = re.compile(r"\w", re.UNICODE)

# A deployment phrase needs a military object; "deploy experts" or "deploy
# police" are not force projection.
_PROJECTION_VERB_RE = re.compile(
    r"(?<![\w])(?:deploy\w*|redeploy\w*|send|sends|sending|sent|"
    r"rush|rushes|rushing|rushed|transfer|transfers|transferring|transferred|"
    r"переброс\w*|перебрасыва\w*|передислоцир\w*|передислокац\w*|"
    r"פריס\w*|שליח\w*|תגבור\w*)(?![\w])"
)
# "Troops move to the border", "transport planes land in Israel".
_PROJECTION_SUBJECT_RE = re.compile(
    r"(?<![\w])(?:troops?|soldiers?|marines?|warships?|destroyers?|"
    r"aircraft\s+carriers?|carrier\s+strike\s+group|fighter\s+jets?|bombers?|"
    r"(?:military\s+)?(?:transport\s+planes?|transport\s+aircraft|"
    r"cargo\s+planes?|warplanes?|military\s+aircraft|refueling\s+aircraft))\s+"
    r"(?:move|moves|moving|moved|head|heads|heading|headed|arrive|arrives|"
    r"arrived|land|lands|landing|landed|deploy|deploys|deploying|deployed)(?![\w])"
)
# "moves troops" is safe; "move forces" is not (market forces, a move forces X).
_PROJECTION_MOVE_RE = re.compile(
    r"(?<![\w])(?:move|moves|moving|moved)\s+(?:additional\s+)?"
    r"(?:troops?|soldiers?|marines?|warships?|destroyers?|aircraft\s+carriers?|"
    r"carrier\s+strike\s+group|fighter\s+jets?|bombers?|patriot|thaad|"
    r"air[- ]defen[cs]e|missile[- ]defen[cs]e)(?![\w])"
)
_PROJECTION_OBJECT_RE = re.compile(
    r"(?<![\w])(?:troops?|forces?|soldiers?|marines?|paratroopers?|"
    r"warships?|destroyers?|submarines?|carriers?|carrier\s+strike\s+group|"
    r"aircraft\s+carriers?|bombers?|fighter\s+jets?|"
    r"(?:military\s+)?(?:transport\s+planes?|transport\s+aircraft|"
    r"cargo\s+planes?|warplanes?|military\s+aircraft|refueling\s+aircraft)|"
    r"patriot|thaad|air[- ]defen[cs]e|missile[- ]defen[cs]e|"
    r"military\s+(?:assets|hardware|equipment)|"
    r"сил\w*|войск\w*|бригад\w*|батальон\w*|дивизи\w*|полк\w*|"
    r"комплекс\w*|патриот\w*|зрк|авианос\w*|техник\w*|ракет\w*|вооруж\w*|"
    r"כוחות|סוללות|נושאת\s+מטוסים|פטריוט)(?![\w])"
)
# Reported plans and conditional framing are not force movements yet.
_PROJECTION_SPECULATION_RE = re.compile(
    r"(?<![\w])(?:may|could|might|would|considering|weighing|"
    r"plans?\s+to|planning\s+to|expected\s+to|reportedly|rumou?r\w*|"
    r"possible|possibly|может|могут|планир\w*|рассматрива\w*|намерен\w*|"
    r"якобы|возможно)(?![\w])"
)


@dataclass(slots=True)
class Place:
    name: str
    country: str
    lat: float | None
    lon: float | None
    theater: str | None
    kind: str
    aliases: tuple[str, ...]


def _alias_pattern(alias: str) -> re.Pattern[str]:
    escaped = re.escape(alias)
    prefix = r"(?<![\w])" if _WORD_CHARS.match(alias[0]) else ""
    suffix = r"(?![\w])" if _WORD_CHARS.match(alias[-1]) else ""
    return re.compile(prefix + escaped + suffix, re.IGNORECASE)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


class GeoIndex:
    def __init__(self, cfg: Any) -> None:
        self.cfg = cfg
        self.countries: list[tuple[str, str, re.Pattern[str]]] = []
        for iso, entry in (cfg.gazetteer.get("countries") or {}).items():
            for alias in entry.get("names", []):
                a = alias.lower()
                if len(a) < 2:
                    continue
                self.countries.append((a, iso, _alias_pattern(a)))
        self.countries.sort(key=lambda x: len(x[0]), reverse=True)

        self.places: list[tuple[str, Place, re.Pattern[str]]] = []
        for raw in cfg.gazetteer.get("places", []):
            place = Place(
                name=raw["name"], country=raw.get("country", ""),
                lat=raw.get("lat"), lon=raw.get("lon"),
                theater=raw.get("theater"), kind=raw.get("kind", "place"),
                aliases=tuple(a.lower() for a in raw.get("aliases", [raw["name"]])),
            )
            for alias in place.aliases:
                if len(alias) < 3:
                    continue
                self.places.append((alias, place, _alias_pattern(alias)))
        self.places.sort(key=lambda x: len(x[0]), reverse=True)

        self.volcanoes: list[tuple[str, Place, re.Pattern[str]]] = []
        for raw in cfg.gazetteer.get("volcanoes", []):
            place = Place(
                name=raw["name"], country=raw.get("country", ""),
                lat=raw.get("lat"), lon=raw.get("lon"), theater="NATURE",
                kind="volcano", aliases=tuple(a.lower() for a in raw.get("aliases", [raw["name"]])),
            )
            for alias in place.aliases:
                if len(alias) < 3:
                    continue
                self.volcanoes.append((alias, place, _alias_pattern(alias)))
        self.volcanoes.sort(key=lambda x: len(x[0]), reverse=True)

        self.theater_countries: dict[str, set[str]] = {}
        self.theater_core: dict[str, set[str]] = {}
        for name, entry in (cfg.geofences.get("theaters") or {}).items():
            countries = entry.get("countries") or []
            if "*" in countries:
                self.theater_countries[name] = {"*"}
            else:
                self.theater_countries[name] = set(countries)
            self.theater_core[name] = set(entry.get("core_countries") or [])

        self.flashpoints = cfg.geofences.get("flashpoints", [])
        self.projection_regions: list[tuple[str, list[re.Pattern[str]]]] = []
        for entry in cfg.geofences.get("force_projection_regions") or []:
            theater = str(entry.get("theater") or "")
            patterns = [
                _alias_pattern(alias.lower())
                for alias in (entry.get("aliases") or [])
                if len(alias) >= 3
            ]
            if theater and patterns:
                self.projection_regions.append((theater, patterns))
        nato = cfg.geofences.get("nato", {}).get("countries", [])
        eu = cfg.geofences.get("eu", {}).get("countries", [])
        self.western = set(nato) | set(eu)
        boost = cfg.geofences.get("pandemic_boost") or {}
        self.pandemic_boost_factor = float(boost.get("factor", 1.0))
        self.pandemic_boost_countries = set(boost.get("countries") or [])
        self.high_consequence = {
            v.lower() for v in cfg.geofences.get("high_consequence_volcanoes", [])
        }

    # ---------- matching ----------
    def match_countries(self, text: str) -> list[tuple[str, str, int]]:
        nt = norm_text(text)
        found: dict[str, tuple[str, int]] = {}
        for alias, iso, pattern in self.countries:
            if iso in found:
                continue
            if pattern.search(nt):
                found[iso] = (alias, len(alias))
        return [(iso, alias, ln) for iso, (alias, ln) in found.items()]

    def match_places(self, text: str, limit: int = 8) -> list[Place]:
        nt = norm_text(text)
        found: dict[str, Place] = {}
        for alias, place, pattern in self.places:
            if place.name in found:
                continue
            if pattern.search(nt):
                found[place.name] = place
            if len(found) >= limit:
                break
        return list(found.values())

    def match_volcanoes(self, text: str) -> list[Place]:
        nt = norm_text(text)
        found: dict[str, Place] = {}
        for alias, place, pattern in self.volcanoes:
            if place.name in found:
                continue
            if pattern.search(nt):
                found[place.name] = place
        return list(found.values())

    def is_high_consequence(self, name: str) -> bool:
        return norm_text(name) in {norm_text(v) for v in self.high_consequence}

    # ---------- geo decisions ----------
    def country_for_theater(self, theater: str, country: str | None) -> bool:
        countries = self.theater_countries.get(theater, set())
        if "*" in countries:
            return True
        return bool(country and country in countries)

    def geo_factor(self, theater: str, country: str | None, lat: float | None,
                   lon: float | None, tier: int) -> float:
        if theater == "PANDEMIC":
            if country and country in self.pandemic_boost_countries:
                return self.pandemic_boost_factor
            return 1.0
        if theater == "NATURE":
            return 1.0
        if country:
            core = self.theater_core.get(theater, set())
            if country in core:
                return 1.0
            if theater == "EU_RU" and country in self.western:
                return 0.3
            if theater in ("ISRAEL", "GULF") and country in ("IQ", "EG", "JO", "SY", "YE", "IR"):
                return 0.3
            if self.country_for_theater(theater, country):
                return 1.0
            return 0.0
        if lat is not None and lon is not None:
            for fp in self.flashpoints:
                if fp.get("theater") != theater:
                    continue
                if haversine_km(lat, lon, fp["lat"], fp["lon"]) <= float(fp.get("radius_km", 150)):
                    return 1.0
        return 1.0 if tier == 0 else 0.3

    def theater_for_country(self, country: str | None) -> str | None:
        if not country:
            return None
        for theater, countries in self.theater_countries.items():
            if theater in ("PANDEMIC", "NATURE", "MARKETS"):
                continue
            if country in countries:
                return theater
        return None

    # ---------- force projection ----------
    def is_force_projection(self, text: str) -> bool:
        """A factual (not planned/conditional) movement of military assets."""
        nt = norm_text(text)
        if not nt or _PROJECTION_SPECULATION_RE.search(nt):
            return False
        if _PROJECTION_SUBJECT_RE.search(nt) or _PROJECTION_MOVE_RE.search(nt):
            return True
        return bool(
            _PROJECTION_VERB_RE.search(nt) and _PROJECTION_OBJECT_RE.search(nt)
        )

    def force_projection_theater(self, text: str) -> str | None:
        nt = norm_text(text)
        for theater, patterns in self.projection_regions:
            if any(p.search(nt) for p in patterns):
                return theater
        return None

    def flashpoint(self, theater: str, lat: float | None, lon: float | None) -> dict[str, Any] | None:
        if lat is None or lon is None:
            return None
        best = None
        best_d = 1e9
        for fp in self.flashpoints:
            if fp.get("theater") != theater:
                continue
            d = haversine_km(lat, lon, fp["lat"], fp["lon"])
            if d <= float(fp.get("radius_km", 150)) and d < best_d:
                best, best_d = fp, d
        return best

    def geotag(self, title: str, body: str, payload: dict[str, Any],
               default_theater: str) -> tuple[str | None, float | None, float | None]:
        lat = payload.get("lat")
        lon = payload.get("lon")
        country = payload.get("country")
        text = f"{title} {body[:1000]}"

        if not country:
            for place in self.match_places(text):
                if place.country:
                    country = place.country
                    if lat is None and place.lat is not None:
                        lat, lon = place.lat, place.lon
                    break
        if not country:
            matches = self.match_countries(text)
            if matches:
                country = max(matches, key=lambda m: m[2])[0]

        if lat is None or lon is None:
            for place in self.match_places(text):
                if place.lat is not None and place.lon is not None:
                    lat, lon = place.lat, place.lon
                    break

        if lat is None or lon is None:
            for place in self.match_volcanoes(text):
                if place.lat is not None and place.lon is not None:
                    lat, lon = place.lat, place.lon
                    country = country or place.country or None
                    break

        if not country and default_theater in ("PANDEMIC", "NATURE") and (lat is None or lon is None):
            return None, None, None
        return country, lat, lon
