"""Source parsers. Each returns a list of RawItem."""

from __future__ import annotations

import csv
import json
import logging
import re
import statistics
import unicodedata
from datetime import datetime
from typing import Any
from urllib.parse import urljoin

import feedparser

from .models import RawItem, Source
from .radiation import parse_safecast
from .util import (
    UTC, canon_url, domain_of, join_url, json_loads, norm_text,
    parse_dt, safe_float, safe_int, sha1, strip_html, truncate, utcnow,
)


def _item(source: Source, title: str, body: str = "", url: str = "",
          published: datetime | None = None, payload: dict[str, Any] | None = None,
          guid: str | None = None, origin_domain: str | None = None,
          kind: str = "event", country: str | None = None,
          lat: float | None = None, lon: float | None = None,
          category: str | None = None) -> RawItem:
    title = strip_html(title).strip()
    body = strip_html(body).strip()
    if not url:
        url = f"{source.url}#{guid or sha1(title, body)[:16]}"
    return RawItem(
        source_id=source.id,
        title=truncate(title, 400),
        body=truncate(body, 4000),
        url=canon_url(url),
        published_at=published,
        lang=source.lang,
        payload=payload or {},
        guid=guid,
        origin_domain=origin_domain or domain_of(url),
        category=category or source.category,
        theater=source.theater,
        kind=kind,
        country=country,
        lat=lat,
        lon=lon,
    )


def _rss_time(entry: Any) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        value = entry.get(key)
        if value:
            dt = parse_dt(tuple(value))
            if dt:
                return dt
    for key in ("published", "updated", "created", "dc_date"):
        if entry.get(key):
            dt = parse_dt(entry.get(key))
            if dt:
                return dt
    return None


def _entry_body(entry: Any) -> str:
    if entry.get("summary"):
        return entry.get("summary")
    if entry.get("description"):
        return entry.get("description")
    content = entry.get("content") or []
    if content and isinstance(content, list):
        return content[0].get("value", "")
    return ""


def _entry_origin(entry: Any, url: str, source: Source) -> str:
    src = entry.get("source")
    if isinstance(src, dict):
        href = src.get("href") or src.get("url")
        if href:
            return domain_of(href)
    return domain_of(url)


# ---------------- RSS ----------------
def parse_rss(source: Source, text: str, cfg: Any = None) -> list[RawItem]:
    parsed = feedparser.parse(text)
    items: list[RawItem] = []
    for entry in parsed.entries[: source.max_items]:
        url = entry.get("link") or entry.get("id") or ""
        title = entry.get("title") or ""
        body = _entry_body(entry)
        if not title and not body:
            continue
        items.append(_item(
            source, title, body, url, _rss_time(entry),
            payload={"feed_id": entry.get("id")},
            guid=entry.get("id") or url,
            origin_domain=_entry_origin(entry, url, source),
        ))
    return items


def parse_gnews(source: Source, text: str, cfg: Any = None) -> list[RawItem]:
    parsed = feedparser.parse(text)
    items: list[RawItem] = []
    for entry in parsed.entries[: source.max_items]:
        url = entry.get("link") or ""
        origin = _entry_origin(entry, url, source)
        if source.origin_domains and not any(
            origin == d or origin.endswith("." + d) for d in source.origin_domains
        ):
            continue
        title = strip_html(entry.get("title") or "")
        src_name = ""
        src = entry.get("source")
        if isinstance(src, dict):
            src_name = strip_html(src.get("title") or "")
        if src_name and title.lower().endswith(" - " + src_name.lower()):
            title = title[: -(len(src_name) + 3)].strip()
        items.append(_item(
            source, title, _entry_body(entry), url, _rss_time(entry),
            payload={"feed_id": entry.get("id")},
            guid=entry.get("id") or url, origin_domain=origin,
        ))
    return items


def parse_gdacs(source: Source, text: str, cfg: Any = None) -> list[RawItem]:
    parsed = feedparser.parse(text)
    items: list[RawItem] = []
    for entry in parsed.entries[: source.max_items]:
        payload = {
            "alertlevel": (entry.get("gdacs_alertlevel") or "").lower() or None,
            "eventtype": (entry.get("gdacs_eventtype") or "").lower() or None,
            "country": entry.get("gdacs_country"),
            "lat": safe_float(entry.get("geo_lat")),
            "lon": safe_float(entry.get("geo_long")),
        }
        title = entry.get("title") or ""
        items.append(_item(
            source, title, _entry_body(entry), entry.get("link") or "",
            _rss_time(entry), payload, guid=entry.get("id"),
            origin_domain=domain_of(entry.get("link") or ""),
        ))
    return items


def parse_tsunami(source: Source, text: str, cfg: Any = None) -> list[RawItem]:
    parsed = feedparser.parse(text)
    items: list[RawItem] = []
    for entry in parsed.entries[: source.max_items]:
        title = entry.get("title") or ""
        nt = title.lower()
        if "warning" in nt:
            mtype = "warning"
        elif "watch" in nt:
            mtype = "watch"
        elif "advisory" in nt:
            mtype = "advisory"
        elif "information" in nt or "statement" in nt:
            mtype = "information"
        else:
            mtype = "unknown"
        summary = _entry_body(entry)
        payload = {
            "message_type": mtype,
            "lat": safe_float(entry.get("geo_lat")),
            "lon": safe_float(entry.get("geo_long")),
        }
        items.append(_item(
            source, title, summary, entry.get("link") or entry.get("id") or "",
            _rss_time(entry), payload, guid=entry.get("id"),
            origin_domain="tsunami.gov",
        ))
    return items


# ---------------- GeoJSON ----------------
def _geojson_feature_item(source: Source, feature: dict[str, Any]) -> RawItem | None:
    props = feature.get("properties") or {}
    geom = feature.get("geometry") or {}
    coords = geom.get("coordinates") or []
    lon = safe_float(coords[0]) if len(coords) > 0 else None
    lat = safe_float(coords[1]) if len(coords) > 1 else None
    depth = safe_float(coords[2]) if len(coords) > 2 else None
    mag = safe_float(props.get("mag"))
    if mag is None:
        return None
    place = props.get("place") or props.get("flynn_region") or props.get("region") or "unknown"
    t = props.get("time")
    published = parse_dt(t)
    if published is None and props.get("time") is None:
        published = parse_dt(props.get("datetime"))
    payload = {
        "mag": mag,
        "depth": depth,
        "place": place,
        "alert": (props.get("alert") or "").lower() or None,
        "tsunami": safe_int(props.get("tsunami")),
        "sig": safe_int(props.get("sig")),
        "mmi": safe_float(props.get("mmi")),
        "lat": lat,
        "lon": lon,
        "event_id": feature.get("id") or props.get("unid") or props.get("eventId"),
    }
    title = f"M{mag:.1f} — {place}"
    body_parts = [place]
    if depth is not None:
        body_parts.append(f"depth {depth:.0f} km")
    if props.get("alert"):
        body_parts.append(f"PAGER {props.get('alert')}")
    if props.get("tsunami"):
        body_parts.append("tsunami flag")
    url = props.get("url") or props.get("link") or ""
    if not url:
        url = f"{source.url}#{payload['event_id'] or abs(hash(title))}"
    return _item(
        source, title, "; ".join(body_parts), url, published, payload,
        guid=str(payload.get("event_id") or url), kind="event",
        country=None, lat=lat, lon=lon,
    )


def parse_usgs(source: Source, text: str, cfg: Any = None) -> list[RawItem]:
    data = json_loads(text, {})
    out = []
    for feature in (data.get("features") or [])[: source.max_items]:
        item = _geojson_feature_item(source, feature)
        if item:
            out.append(item)
    return out


def parse_emsc_feature(source: Source, feature: dict[str, Any]) -> RawItem | None:
    props = feature.get("properties") or {}
    merged = {**props, "url": "https://www.seismicportal.eu/eventdetails.html?unid=" + str(props.get("unid", ""))}
    item = _geojson_feature_item(source, {**feature, "properties": merged})
    if item:
        item.payload["region"] = props.get("flynn_region")
    return item


def parse_emsc(source: Source, text: str, cfg: Any = None) -> list[RawItem]:
    data = json_loads(text, {})
    out = []
    for feature in (data.get("features") or [])[: source.max_items]:
        item = parse_emsc_feature(source, feature)
        if item:
            out.append(item)
    return out


def parse_ingv(source: Source, text: str, cfg: Any = None) -> list[RawItem]:
    data = json_loads(text, {})
    out = []
    for feature in (data.get("features") or [])[: source.max_items]:
        props = feature.get("properties") or {}
        merged = {**props}
        merged.setdefault("place", props.get("place") or props.get("area") or "Italy")
        merged.setdefault("url", props.get("url") or "https://terremoti.ingv.it/")
        item = _geojson_feature_item(source, {**feature, "properties": merged})
        if item:
            out.append(item)
    return out


# ---------------- Specialized JSON ----------------
def parse_hans(source: Source, text: str, cfg: Any = None) -> list[RawItem]:
    data = json_loads(text, [])
    if isinstance(data, dict):
        data = data.get("value") or data.get("items") or []
    out = []
    for row in data[: source.max_items]:
        if not isinstance(row, dict):
            continue
        volcano = (row.get("volcano_name") or row.get("obs_abbr") or
                   row.get("volcano_cds_csv") or "volcano")
        notice_type = row.get("notice_type_title") or row.get("notice_type_cd") or "notice"
        body = row.get("notice_data") or row.get("notice_text") or row.get("summary") or ""
        if isinstance(body, dict):
            body = json.dumps(body, ensure_ascii=False)
        text_all = f"{volcano} {notice_type} {body}"
        colour = _extract_colour(text_all)
        ash_ft = _extract_ash_ft(text_all)
        payload = {
            "volcano_name": volcano,
            "colour_code": colour,
            "alert_level": "warning" if colour == "red" else ("watch" if colour == "orange" else None),
            "ash_ft": ash_ft,
            "notice_type": notice_type,
            "obs": row.get("obs_abbr"),
        }
        published = parse_dt(row.get("sent_utc") or row.get("sent_unixtime"))
        url = row.get("notice_url") or row.get("url") or source.url
        out.append(_item(
            source, f"{volcano}: {notice_type}", body, url, published, payload,
            guid=str(row.get("notice_url") or row.get("id") or url),
        ))
    return out


def parse_who_don(source: Source, text: str, cfg: Any = None) -> list[RawItem]:
    data = json_loads(text, {})
    rows = data.get("value") if isinstance(data, dict) else data
    out = []
    for row in (rows or [])[: source.max_items]:
        if not isinstance(row, dict):
            continue
        title = row.get("Title") or row.get("title") or ""
        summary = row.get("Summary") or row.get("summary") or ""
        url = row.get("ItemDefaultUrl") or row.get("Url") or row.get("url") or ""
        if url and not url.startswith("http"):
            url = "https://www.who.int" + url
        published = parse_dt(row.get("PublicationDateAndTime") or row.get("PublicationDate"))
        out.append(_item(
            source, title, summary, url, published,
            {"don_id": row.get("DonId"), "origin": "who_don"}, guid=str(row.get("DonId") or url),
        ))
    return out


def parse_ukrainealarm(source: Source, text: str, cfg: Any = None) -> list[RawItem]:
    data = json_loads(text, {})
    states = data.get("states") or {}
    active = [name for name, st in states.items() if isinstance(st, dict) and st.get("alertnow")]
    total = max(len(states), 1)
    share = len(active) / total
    if not active:
        return []
    payload = {
        "active_count": len(active),
        "total": total,
        "share": round(share, 3),
        "active": active,
        "all_ukraine": len(active) >= 20,
    }
    title = f"Ukraine air alerts: {len(active)}/{total} regions"
    body = ", ".join(active)
    return [_item(source, title, body, source.url, datetime.utcnow(), payload,
                  guid=f"ua-{int(datetime.utcnow().timestamp() // 180)}", kind="indicator")]


def parse_tzeva(source: Source, text: str, cfg: Any = None) -> list[RawItem]:
    data = json_loads(text, [])
    if not isinstance(data, list):
        return []
    raw_alerts: list[dict[str, Any]] = []
    if data and isinstance(data[0], dict) and "alerts" in data[0]:
        for group in data[: source.max_items]:
            for alert in group.get("alerts") or []:
                raw_alerts.append(alert)
    else:
        raw_alerts = [a for a in data if isinstance(a, dict)]

    districts = _oref_districts(cfg)
    out = []
    total_districts = max(len(districts), 1)
    for alert in raw_alerts[-source.max_items:]:
        if alert.get("isDrill"):
            continue
        cities = [c for c in (alert.get("cities") or []) if c]
        if not cities:
            continue
        threat = safe_int(alert.get("threat"))
        affected = _districts_for_cities(cities, districts)
        share = len(affected) / total_districts
        payload = {
            "cities": cities,
            "districts": affected,
            "district_share": round(share, 3),
            "threat": threat,
            "threat_text": _threat_text(threat),
            "is_drill": False,
            "alert_id": alert.get("id"),
        }
        title = f"התרעה: {_threat_text(threat)} — {', '.join(cities[:6])}"
        published = parse_dt(alert.get("time"))
        url = f"https://www.tzevaadom.co.il/alert/{alert.get('id') or alert.get('time')}"
        out.append(_item(source, title, ", ".join(cities), url, published, payload,
                         guid=f"tzeva-{alert.get('id')}-{alert.get('time')}"))
    return out


def parse_oref(source: Source, text: str, cfg: Any = None) -> list[RawItem]:
    data = json_loads(text, [])
    if isinstance(data, dict):
        data = [data]
    out = []
    for alert in data or []:
        if not isinstance(alert, dict):
            continue
        cities = alert.get("data") or alert.get("cities") or []
        if isinstance(cities, str):
            cities = [c.strip() for c in cities.split(",")]
        title = alert.get("title") or "התרעה"
        payload = {"cities": cities, "threat": alert.get("cat"), "alert_id": alert.get("id")}
        out.append(_item(source, title, ", ".join(cities), f"https://www.oref.org.il/#{alert.get('id')}",
                         parse_dt(alert.get("time")), payload, guid=str(alert.get("id"))))
    return out


def parse_adsb(source: Source, text: str, cfg: Any = None) -> list[RawItem]:
    data = json_loads(text, {})
    aircraft = data.get("ac") or []
    types: dict[str, int] = {}
    notable: list[str] = []
    notable_types = {
        "E4B", "E6B", "VC25", "C32", "C40", "RC135", "C135", "KC135", "B52", "B1", "B2",
        "P8", "P3", "U2", "RQ4", "MQ9", "C5M", "C17", "A50", "IL96", "IL78", "TU214", "IL20",
    }
    for ac in aircraft:
        t = (ac.get("t") or "").upper()
        if not t:
            continue
        types[t] = types.get(t, 0) + 1
        if t in notable_types:
            callsign = (ac.get("flight") or "").strip()
            notable.append(f"{t}/{callsign}" if callsign else t)
    payload = {
        "aircraft_count": len(aircraft),
        "types": dict(sorted(types.items(), key=lambda kv: kv[1], reverse=True)[:15]),
        "notable": sorted(set(notable)),
    }
    title = f"ADS-B military: {len(aircraft)} aircraft"
    body = ", ".join(payload["notable"][:20])
    return [_item(source, title, body, source.url, datetime.utcnow(), payload,
                  guid=f"adsb-{int(datetime.utcnow().timestamp() // 300)}", kind="indicator")]


EWS_ALERT_LEVELS = {0: "normal", 1: "normal", 2: "normal", 3: "elevated", 4: "alarm", 5: "emergency"}
EWS_COHORT_LABELS = {"business_jet": "бизнес-джеты", "military": "военные"}


def _json_object_prefix(text: str, key: str) -> dict[str, Any] | None:
    """Extract a complete JSON object for `key` from a truncated document prefix."""
    marker = f'"{key}"'
    start = 0
    while True:
        idx = text.find(marker, start)
        if idx < 0:
            return None
        start = idx + len(marker)
        colon = text.find(":", start)
        if colon < 0 or text[start:colon].strip():
            continue
        brace = colon + 1
        while brace < len(text) and text[brace].isspace():
            brace += 1
        if brace >= len(text) or text[brace] != "{":
            continue
        depth = 0
        in_str = False
        escaped = False
        for pos in range(brace, len(text)):
            ch = text[pos]
            if in_str:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    value = json_loads(text[brace:pos + 1], None)
                    return value if isinstance(value, dict) else None
        return None


def parse_ews_dashboard(source: Source, text: str, cfg: Any = None) -> list[RawItem]:
    """EWS heatmap dashboard; works with a Range-fetched JSON prefix."""
    data = json_loads(text, None)
    if not isinstance(data, dict):
        data = {}
    current = data.get("current")
    if not isinstance(current, dict):
        current = _json_object_prefix(text, "current")
    if not isinstance(current, dict):
        return []
    signals = data.get("signals") if isinstance(data.get("signals"), dict) else {}
    composite = signals.get("composite") if isinstance(signals.get("composite"), dict) else None
    if not isinstance(composite, dict):
        composite = _json_object_prefix(text, "composite")
    composite = composite or {}

    level = safe_int(current.get("emergencyLevel"))
    if level is None:
        level = safe_int(composite.get("emergencyLevel"))
    if level is None:
        return []
    level = max(0, min(5, level))
    alert = str(
        current.get("alertLevel") or composite.get("alertLevel") or EWS_ALERT_LEVELS[level]
    ).lower()
    z = safe_float(current.get("zScore"))
    if z is None:
        z = safe_float(composite.get("sigmaShift"))
    concurrent = safe_int(current.get("concurrentCount"))
    if concurrent is None:
        concurrent = safe_int(composite.get("actualConcurrentCount"))
    expected = safe_float(current.get("baselineMean"))
    if expected is None:
        expected = safe_float(composite.get("expectedConcurrentCount"))
    cohort = data.get("cohort")
    if not isinstance(cohort, dict):
        cohort = _json_object_prefix(text, "cohort") or {}
    cohort_type = str(cohort.get("cohortType") or "")
    label = EWS_COHORT_LABELS.get(cohort_type) or str(cohort.get("sourceLabel") or "")
    as_of = current.get("asOf") or composite.get("asOf")

    payload = {
        "state": alert.upper(),
        "alert_level": alert,
        "emergency_level": level,
        "z_score": round(z, 2) if z is not None else None,
        "concurrent": concurrent,
        "expected": round(expected) if expected is not None else None,
        "as_of": as_of,
        "cohort_type": cohort_type,
        "cohort_label": label,
    }
    title = f"EWS {label or 'сигнал'}: {level}/5"
    body = f"{alert} · "
    if z is not None:
        body += f"z {z:+.2f}σ · "
    if concurrent is not None:
        body += f"{concurrent} ВС"
        if expected is not None:
            body += f" при норме ~{int(round(expected))}"
    return [_item(source, title, body, source.url, parse_dt(as_of) or datetime.utcnow(),
                  payload, guid=f"ews-{source.id}-{as_of or int(datetime.utcnow().timestamp())}",
                  kind="indicator")]


def parse_tradingview(source: Source, text: str, cfg: Any = None) -> list[RawItem]:
    data = json_loads(text, {})
    rows = data.get("data") if isinstance(data, dict) else None
    out = []
    ts = datetime.utcnow()
    for row in rows or []:
        symbol = row.get("s") or ""
        values = row.get("d") or []
        if len(values) < 3:
            continue
        meta = (source.market_symbols or {}).get(symbol, {})
        payload = {
            "symbol": symbol,
            "name": meta.get("name", symbol),
            "kind": meta.get("kind", "market"),
            "close": safe_float(values[0]),
            "change_pct": safe_float(values[1]),
            "change_abs": safe_float(values[2]),
        }
        out.append(_item(source, f"{payload['name']} {payload['close']}", "", f"{source.url}#{symbol}",
                         ts, payload, guid=symbol, kind="market"))
    return out


def parse_eia(source: Source, text: str, cfg: Any = None) -> list[RawItem]:
    data = json_loads(text, {})
    rows = (((data.get("response") or {}).get("data")) or [])[: source.max_items]
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        period = row.get("period")
        value = safe_float(row.get("value"))
        if value is None:
            continue
        out.append(_item(source, f"EIA Brent {period}: {value}", "", f"{source.url}#{period}",
                         parse_dt(period), {"symbol": "BRENT_SPOT", "close": value,
                                            "change_pct": None, "name": "Brent spot"},
                         guid=str(period), kind="market"))
    return out


def parse_frankfurter(source: Source, text: str, cfg: Any = None) -> list[RawItem]:
    data = json_loads(text, {})
    rates = data.get("rates") or {}
    ts = parse_dt(data.get("date")) or datetime.utcnow()
    out = []
    for code, value in rates.items():
        symbol = f"USD{code}"
        out.append(_item(source, f"{symbol} {value}", "", f"{source.url}#{symbol}", ts,
                         {"symbol": symbol, "close": safe_float(value), "change_pct": None,
                          "name": symbol}, guid=f"{symbol}-{data.get('date')}", kind="market"))
    return out


# ---------------- HTML ----------------
class _LinkParser:
    def __init__(self) -> None:
        from html.parser import HTMLParser

        outer = self

        class Parser(HTMLParser):
            def __init__(self) -> None:
                super().__init__(convert_charrefs=True)
                self.links: list[dict[str, Any]] = []
                self.current: dict[str, Any] | None = None
                self.stack: list[str] = []
                self.sections: list[str | None] = [None]
                self.last_time: str | None = None
                self.last_date: str | None = None
                self.date_buffer: list[str] | None = None
                self.in_time = False

            def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
                a = dict(attrs)
                if tag == "section":
                    self.sections.append(a.get("id") or a.get("class"))
                if tag == "a":
                    href = a.get("href") or ""
                    self.current = {"href": href, "text": [], "datetime": None,
                                    "section": self.sections[-1]}
                elif tag == "time":
                    dt = a.get("datetime")
                    if dt:
                        self.last_time = dt
                        if self.current is not None:
                            self.current["datetime"] = dt
                elif tag in ("div", "span", "p"):
                    classes = (a.get("class") or "").lower().split()
                    if any(tok.startswith("date") or tok.endswith("-date") for tok in classes):
                        self.date_buffer = []

            def handle_data(self, data: str) -> None:
                if self.date_buffer is not None:
                    self.date_buffer.append(data)
                if self.current is not None:
                    self.current["text"].append(data)

            def handle_endtag(self, tag: str) -> None:
                if tag in ("div", "span", "p") and self.date_buffer is not None:
                    value = " ".join("".join(self.date_buffer).split())
                    if value:
                        self.last_date = value
                    self.date_buffer = None
                if tag == "section" and len(self.sections) > 1:
                    self.sections.pop()
                if tag == "li":
                    self.last_date = None
                if tag == "a" and self.current is not None:
                    text = " ".join("".join(self.current["text"]).split())
                    link = {"href": self.current["href"], "text": text,
                            "datetime": self.current.get("datetime") or self.last_time,
                            "date_hint": self.last_date,
                            "section": self.current.get("section")}
                    self.links.append(link)
                    self.current = None
                    self.last_time = None
                    self.last_date = None

        self.parser = Parser()

    def feed(self, text: str) -> list[dict[str, Any]]:
        self.parser.feed(text)
        return self.parser.links


NAV_TITLES = {
    "news", "newsroom", "see all", "all news", "more", "read more", "view all",
    "home", "contact", "search", "menu", "skip to content",
}


def _listing_date(link: dict[str, Any]) -> datetime | None:
    published = parse_dt(link.get("datetime"))
    if published is not None:
        return published
    hint = (link.get("date_hint") or "").strip()
    if not hint:
        return None
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%d/%m/%Y", "%d %B %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(hint[:20], fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return parse_dt(hint)


def parse_html_links(source: Source, text: str, cfg: Any = None) -> list[RawItem]:
    parser = _LinkParser()
    links = parser.feed(text)
    pattern = re.compile(source.link_pattern) if source.link_pattern else None
    base = source.base_url or source.url
    self_url = canon_url(source.url)
    news_sections = {
        link.get("section") for link in links
        if link.get("section") and re.search(
            r"(news|press|actualit|novost|nachrichten|uudised|aktual|media)", str(link["section"]), re.I
        )
    }
    if news_sections:
        links = [link for link in links if link.get("section") in news_sections]
    seen: set[str] = set()
    out: list[RawItem] = []
    for link in links:
        href = link["href"].strip()
        title = link["text"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        if len(title) < source.min_title_len or title.lower() in NAV_TITLES:
            continue
        if pattern and not pattern.search(href):
            continue
        url = canon_url(join_url(base, href))
        if not url or url in seen or url == self_url:
            continue
        seen.add(url)
        published = _listing_date(link)
        out.append(_item(source, title, "", url, published, {}, guid=url))
        if len(out) >= source.max_items:
            break
    return out


_CZIB_THEATERS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("russian federation", "russia", "ukraine", "belarus", "moldova"), "EU_RU"),
    (("israel", "lebanon", "syria", "jordan", "iran", "iraq", "palestin", "gaza"), "ISRAEL"),
    (("saudi", "yemen", "persian gulf", "gulf of oman", "red sea", "qatar", "kuwait",
      "bahrain", "oman", "emirates"), "GULF"),
    (("turkey", "türkiye"), "MENA"),
)
_CZIB_CATEGORY = {"EU_RU": "eu_ru", "ISRAEL": "israel", "GULF": "gulf", "MENA": "mena"}


def parse_easa_czib(source: Source, text: str, cfg: Any = None) -> list[RawItem]:
    """Active EASA conflict-zone bulletins; a revision gets a new URL and event."""
    out: list[RawItem] = []
    for item in parse_html_links(source, text, cfg):
        region = item.title
        haystack = region.lower()
        theater = next(
            (name for words, name in _CZIB_THEATERS if any(word in haystack for word in words)),
            source.theater,
        )
        czib = item.url.rstrip("/").rsplit("/", 1)[-1].upper()
        short = czib[len("CZIB-"):] if czib.startswith("CZIB-") else czib
        effective = item.published_at
        item.theater = theater
        item.category = _CZIB_CATEGORY.get(theater, source.category)
        item.title = f"EASA CZIB {short}: {region}"
        item.body = (
            f"Active EASA conflict zone information bulletin for {region}"
            + (f", effective {effective:%Y-%m-%d}." if effective else ".")
        )
        item.payload = {
            "czib": czib,
            "region": region,
            "effective": effective.strftime("%Y-%m-%d") if effective else None,
        }
        # A bulletin is current knowledge: the page date is the bulletin's
        # effective date, not the time we learned it is still active.
        item.published_at = utcnow()
        out.append(item)
    return out


_TRAVEL_LEVEL = re.compile(r"Level\s*([1-4])", re.I)
_DEPARTURE_STATUS = re.compile(r"(?<![\w-])(Ordered|Authorized)\s+departure", re.I)


def parse_travel_advisory(source: Source, text: str, cfg: Any = None) -> list[RawItem]:
    """State Department advisories with the level and departure status made explicit."""
    parsed = feedparser.parse(text)
    out: list[RawItem] = []
    for entry in parsed.entries:
        url = entry.get("link") or entry.get("id") or ""
        title = entry.get("title") or ""
        body = _entry_body(entry)
        if not title and not body:
            continue
        plain = strip_html(body)
        level = _TRAVEL_LEVEL.search(title) or _TRAVEL_LEVEL.search(plain[:300])
        # Status headings sit near the top of the advisory; a dated historical
        # mention deeper in the text must not be read as the current status.
        status = _DEPARTURE_STATUS.search(plain[:2500])
        payload = {
            "feed_id": entry.get("id"),
            "advisory_level": int(level.group(1)) if level else None,
            "departure": status.group(1).lower() if status else "",
        }
        out.append(_item(
            source, title, body, url, _rss_time(entry), payload,
            guid=entry.get("id") or url,
            origin_domain=_entry_origin(entry, url, source),
        ))
    # Departure advisories are rare and operationally important, so they must
    # not fall outside max_items just because the feed is alphabetical.
    rank = {"ordered": 0, "authorized": 1}
    out.sort(key=lambda item: rank.get(item.payload.get("departure"), 2))
    return out[: source.max_items]


def parse_portwatch(source: Source, text: str, cfg: Any = None) -> list[RawItem]:
    """IMF PortWatch daily chokepoint transits; flags volume drops vs the prior week."""
    by_name: dict[str, dict[str, int]] = {}
    reader = csv.DictReader(text.lstrip("\ufeff").splitlines())
    for row in reader:
        name = (row.get("portname") or "").strip()
        date = (row.get("date") or "").strip().replace("/", "-")
        count = safe_int(row.get("n_total"))
        if not name or not date or count is None:
            continue
        by_name.setdefault(name, {})[date] = count
    if not by_name:
        return []

    latest_date = max(date for days in by_name.values() for date in days)
    chokepoints: dict[str, dict[str, Any]] = {}
    notable: list[str] = []
    for name, days in by_name.items():
        latest = days.get(latest_date)
        if latest is None:
            continue
        prior = [days[date] for date in sorted(days) if date < latest_date][-7:]
        baseline = statistics.median(prior) if prior else None
        delta = round((latest - baseline) / baseline * 100.0, 1) if baseline else None
        chokepoints[name] = {"n_total": latest, "baseline": baseline, "delta_pct": delta}
        if baseline is not None and baseline >= 8 and latest <= baseline * 0.5:
            notable.append(f"{name} {delta:+.0f}%")
    if not chokepoints:
        return []

    state = "ANOMALY" if notable else "NORMAL"
    payload = {
        "state": state,
        "as_of": latest_date,
        "chokepoints": chokepoints,
        "notable": notable,
    }
    body = ", ".join(notable) if notable else "transit volumes within the 7-day baseline"
    return [_item(
        source, f"IMF PortWatch: {len(chokepoints)} chokepoints · {latest_date}",
        body, source.url, utcnow(), payload,
        guid=f"portwatch-{latest_date}", kind="indicator",
    )]


def parse_gpsjam(source: Source, text: str, cfg: Any = None) -> list[RawItem]:
    """Daily count of GPS interference hexes from the gpsjam manifest."""
    rows: list[tuple[str, int, bool, str]] = []
    for row in csv.DictReader(text.splitlines()):
        date = (row.get("date") or "").strip()
        try:
            count = int(float(row.get("num_bad_aircraft_hexes") or ""))
        except (TypeError, ValueError):
            continue
        if not date:
            continue
        rows.append((
            date, count,
            (row.get("suspect") or "").strip().lower() == "true",
            (row.get("source") or "").strip(),
        ))
    if not rows:
        return []
    rows.sort(key=lambda item: item[0])
    date, latest, suspect, origin = rows[-1]
    prior = [count for _date, count, _s, _o in rows[-8:-1]]
    baseline = statistics.median(prior) if prior else None
    delta = round((latest / baseline - 1.0) * 100.0, 1) if baseline else None
    try:
        age_days = (utcnow().date() - datetime.strptime(date, "%Y-%m-%d").date()).days
    except ValueError:
        age_days = 0
    if suspect or age_days > 4:
        state = "UNKNOWN"
    elif baseline is not None and latest >= max(300, baseline * 1.5):
        state = "ELEVATED"
    else:
        state = "NORMAL"
    payload = {
        "state": state,
        "date": date,
        "bad_hexes": latest,
        "baseline_7d": baseline,
        "delta_pct": delta,
        "suspect": suspect,
        "origin": origin,
    }
    body = (
        f"7-day baseline {baseline:.0f} ({delta:+.0f}%)" if baseline is not None
        else "no baseline yet"
    )
    return [_item(
        source, f"GPS interference: {latest} hexes · {date}", body,
        source.url, utcnow(), payload, guid=f"gpsjam-{date}", kind="indicator",
    )]


UA_AIRBORNE_BBOX = (46.0, 30.0, 52.4, 40.3)


def parse_opensky(source: Source, text: str, cfg: Any = None) -> list[RawItem]:
    """Airborne aircraft over Ukraine; the airspace has been closed since 2022."""
    data = json_loads(text, {})
    states = data.get("states")
    if not isinstance(states, list) or not states:
        return []
    airborne = core = 0
    for state in states:
        if not isinstance(state, list) or len(state) < 9 or state[8]:
            continue
        lat, lon = state[5], state[6]
        if lat is None or lon is None:
            continue
        airborne += 1
        south, west, north, east = UA_AIRBORNE_BBOX
        if south <= lat <= north and west <= lon <= east:
            core += 1
    if core == 0:
        value = "CLOSED"
    elif core < 8:
        value = "ACTIVITY"
    else:
        value = "REOPENING"
    payload = {
        "state": value,
        "airborne_core": core,
        "airborne_region": airborne,
        "as_of": data.get("time"),
        "zone": "UA",
    }
    return [_item(
        source, f"Airspace Ukraine: {value} · {core} airborne", "", source.url,
        utcnow(), payload, guid=f"opensky-{int(utcnow().timestamp() // 1800)}",
        kind="indicator",
    )]


def parse_html_table(source: Source, text: str, cfg: Any = None) -> list[RawItem]:
    rows = re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", text)
    out = []
    seen = set()
    for row in rows:
        cells = [strip_html(c) for c in re.findall(r"(?is)<t[dh][^>]*>(.*?)</t[dh]>", row)]
        if len(cells) < 4:
            continue
        mags = [safe_float(c) for c in cells]
        mag = next((m for m in mags if m is not None and 1.0 <= m <= 9.9), None)
        if mag is None:
            continue
        region = max((c for c in cells if len(c) > 3 and not re.fullmatch(r"[\d.,: -]+", c)),
                     key=len, default="")
        times = [c for c in cells if re.search(r"\d{1,2}:\d{2}", c)]
        published = None
        if times:
            published = parse_dt(times[0].replace(".", "-"))
        key = f"{mag}-{region}-{times[0] if times else ''}"
        if key in seen:
            continue
        seen.add(key)
        out.append(_item(source, f"M{mag:.1f} — {region}", row, f"{source.url}#{key}",
                         published, {"mag": mag, "place": region}, guid=key))
        if len(out) >= source.max_items:
            break
    return out


def parse_volcano_status(source: Source, text: str, cfg: Any = None) -> list[RawItem]:
    plain = strip_html(text)
    out = []
    seen = set()
    # Indonesian (BMKG MAGMA)
    for m in re.finditer(
        r"(?is)(Gunung\s+[A-Z][\w\- ]{2,40}?).{0,250}?Level\s+(I{1,3}V?|IV)\s*[\(\s]*(Awas|Siaga|Waspada|Normal)",
        plain,
    ):
        name = m.group(1).strip()
        word = m.group(3).capitalize()
        colour = {"Awas": "red", "Siaga": "orange", "Waspada": "yellow", "Normal": "green"}[word]
        if colour in ("green", "yellow"):
            continue
        key = f"{name}-{colour}"
        if key in seen:
            continue
        seen.add(key)
        out.append(_item(source, f"{name}: {word}", m.group(0),
                         f"{source.url}#{key}", datetime.utcnow(),
                         {"volcano_name": name, "colour_code": colour,
                          "alert_level": "warning" if colour == "red" else "watch",
                          "level_text": word}, guid=key, category="volcano"))
    # English colour codes
    for m in re.finditer(
        r"(?i)([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s*(?:volcano)?\s*[:\-–]\s*"
        r"(RED|ORANGE|YELLOW|GREEN)\b",
        plain,
    ):
        name = m.group(1).strip()
        colour = m.group(2).lower()
        if colour in ("green", "yellow"):
            continue
        key = f"{name}-{colour}"
        if key in seen:
            continue
        seen.add(key)
        out.append(_item(source, f"{name}: {colour.upper()} aviation code", m.group(0),
                         f"{source.url}#{key}", datetime.utcnow(),
                         {"volcano_name": name, "colour_code": colour,
                          "alert_level": "warning" if colour == "red" else "watch"},
                         guid=key, category="volcano"))
    return out


def parse_jma_eqvol(source: Source, text: str, cfg: Any = None) -> list[RawItem]:
    """JMA 地震火山 Atom feed; keeps only entries that name a volcano."""
    parsed = feedparser.parse(text)
    out: list[RawItem] = []
    for entry in parsed.entries:
        raw_body = _entry_body(entry)
        title = entry.get("title") or ""
        body = unicodedata.normalize("NFKC", raw_body)
        m = re.search(r"【火山名\s+([^】]+?)(?:\s+([^】]*))?】", body)
        if not m:
            continue
        volcano = m.group(1).strip()
        report = (m.group(2) or "").strip() or title
        level_m = re.search(r"噴火警戒レベル\s*([1-5])", body)
        level = int(level_m.group(1)) if level_m else 0
        url = entry.get("link") or entry.get("id") or source.url
        out.append(_item(
            source, f"{volcano}: {report}", body, url, _rss_time(entry),
            {"volcano_name": volcano, "jma_level": level,
             "alert_level": "warning" if level >= 4 else None},
            guid=entry.get("id") or url, category="volcano",
        ))
        if len(out) >= source.max_items:
            break
    return out


_VONA_HEADER_RE = re.compile(
    r"\[([^\[\]\n]+?)\s+-\s+(\d{8})/(\d{4})Z\]\((https?://[^)]+)\)"
)
_VONA_REPORT_RE = re.compile(
    r"\n\n([^\n]+?)\n\n\[View\]\((https?://[^)]+)\)"
)


def parse_vona(source: Source, text: str, cfg: Any = None) -> list[RawItem]:
    """BMKG MAGMA VONA reports as rendered by r.jina.ai (markdown)."""
    headers = list(_VONA_HEADER_RE.finditer(text))
    out: list[RawItem] = []
    for index, header in enumerate(headers):
        tail = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        report = _VONA_REPORT_RE.search(text, header.end(), tail)
        if not report:
            continue
        body, url = report.group(1).strip(), report.group(2)
        try:
            published = datetime.strptime(
                header.group(2) + header.group(3), "%Y%m%d%H%M",
            ).replace(tzinfo=UTC)
        except ValueError:
            published = None
        volcano = header.group(1).strip()
        out.append(_item(
            source, f"{volcano}: VONA {header.group(2)}/{header.group(3)}Z",
            body, url, published,
            {"volcano_name": volcano, "ash_ft": _extract_ash_ft(body)},
            guid=url, category="volcano",
        ))
        if len(out) >= source.max_items:
            break
    return out


def _vaac_dt(date_raw: str, hhmm: str) -> datetime | None:
    try:
        return datetime.strptime(f"{date_raw} {hhmm}", "%d %b %Y %H%M").replace(tzinfo=UTC)
    except ValueError:
        return parse_dt(f"{date_raw} {hhmm}")


_VAAC_FL_RE = re.compile(r"(?i)\bFL\s?(\d{3})\b")
_VAAC_COLOUR_RE = re.compile(
    r"(?i)(?:AVIATION\s+)?COLOU?R\s+CODE\s*[:\-]?\s*(RED|ORANGE|YELLOW|GREEN)\b"
)


def extract_vaac_advisory(text: str) -> dict[str, Any]:
    """Pull ash-top height and colour code out of a VAAC advisory body."""
    plain = strip_html(text)
    out: dict[str, Any] = {}
    heights = [
        int(m.group(1)) * 100
        for m in _VAAC_FL_RE.finditer(plain)
        if 0 < int(m.group(1)) <= 600
    ]
    if heights:
        out["ash_ft"] = max(heights)
    colour = _VAAC_COLOUR_RE.search(plain)
    if colour:
        out["colour_code"] = colour.group(1).lower()
    return out


def parse_vaac_messages(source: Source, text: str, cfg: Any = None) -> list[RawItem]:
    """Washington VAAC message list: names in one column, dated advisory links in another."""
    names: dict[str, str] = {}
    for m in re.finditer(r'href="volcanoes/([A-Z0-9_]+)\.html"[^>]*>([^<]+)</a>', text):
        code = m.group(1).upper()
        name = strip_html(m.group(2)).strip()
        names.setdefault(code[:4], name)
    out: list[RawItem] = []
    seen: set[str] = set()
    pattern = re.compile(
        r"<em>\s*(\d{1,2} \w{3} \d{4})\s*-\s*</em>\s*"
        r'<a href="(/VAAC/ARCH\d+/([A-Z0-9]+)/[^"]+\.html)">(\d{4}) UTC</a>',
        re.I,
    )
    for m in pattern.finditer(text):
        date_raw, href, code, hhmm = m.groups()
        code = code.upper()
        published = _vaac_dt(date_raw, hhmm)
        name = names.get(code[:4]) or code
        key = f"{code}-{date_raw}-{hhmm}"
        if key in seen:
            continue
        seen.add(key)
        url = canon_url(join_url(source.url, href))
        out.append(_item(
            source, f"{name}: volcanic ash advisory {hhmm} UTC",
            f"{name} — VA advisory {date_raw} {hhmm} UTC",
            url, published,
            {"volcano_name": name.title(), "ash_advisory": True, "vaac": "Washington"},
            guid=key, category="volcano",
        ))
        if len(out) >= source.max_items:
            break
    return out


def parse_vaac_list(source: Source, text: str, cfg: Any = None) -> list[RawItem]:
    """Toulouse VAAC list: 'ETNA.201 - 2026-09-30 16:00 utc' links."""
    out: list[RawItem] = []
    seen: set[str] = set()
    pattern = re.compile(
        r'<a[^>]+href="([^"]*?/advisory/[^"]+)"[^>]*>\s*'
        r"([A-Z][A-Z0-9' .\-]{1,40})\.(\d+)\s*-\s*"
        r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})\s*utc\s*</a>",
        re.I,
    )
    for m in pattern.finditer(text):
        href, name, adv, date, hhmm = m.groups()
        name = name.strip()
        key = f"{name}-{adv}"
        if key in seen:
            continue
        seen.add(key)
        url = canon_url(join_url(source.url, href))
        published = parse_dt(f"{date} {hhmm}")
        out.append(_item(
            source, f"{name}: volcanic ash advisory {adv} ({date} {hhmm} UTC)",
            f"{name} — VA advisory {adv} issued {date} {hhmm} UTC",
            url, published,
            {"volcano_name": name.title(), "advisory": adv, "ash_advisory": True,
             "vaac": "Toulouse"},
            guid=key, category="volcano",
        ))
        if len(out) >= source.max_items:
            break
    return out


def parse_vaac_tokyo(source: Source, text: str, cfg: Any = None) -> list[RawItem]:
    """Tokyo VAAC advisory table: hidden date cell + volcano + area + advisory no + Text link."""
    out: list[RawItem] = []
    seen: set[str] = set()
    for row in re.finditer(r'(?is)<tr class="mtx"[^>]*>(.*?)</tr>', text):
        body = row.group(1)
        cells = re.findall(r"(?is)<td[^>]*>(.*?)</td>", body)
        if len(cells) < 5:
            continue
        date_raw = strip_html(cells[0]).strip()
        volcano = strip_html(cells[2]).strip()
        area = strip_html(cells[3]).strip()
        adv = strip_html(cells[4]).strip()
        link = re.search(r'href="(TextData/[^"]+)"', body)
        if not volcano or not link:
            continue
        published = parse_dt(date_raw.replace("/", "-"))
        key = f"{volcano}-{adv}"
        if key in seen:
            continue
        seen.add(key)
        url = canon_url(join_url(source.url, link.group(1)))
        title = f"{volcano}: volcanic ash advisory {adv}"
        text_body = f"{volcano} ({area}) — VA advisory {adv} issued {date_raw} UTC"
        out.append(_item(source, title, text_body, url, published,
                         {"volcano_name": volcano.title(), "area": area,
                          "advisory": adv, "vaac": "Tokyo", "ash_advisory": True},
                         guid=key, category="volcano"))
        if len(out) >= source.max_items:
            break
    return out


def parse_imo_quakes(source: Source, text: str, cfg: Any = None) -> list[RawItem]:
    """Iceland Met Office embeds quakes in a JS array (en.vedur.is)."""
    m = re.search(r"VI\.quakeInfo\s*=\s*\[(.*?)\];", text, re.S)
    if not m:
        return []
    min_mag = float(getattr(cfg, "imo_min_mag", 3.0) if cfg else 3.0)
    out: list[RawItem] = []
    seen: set[str] = set()
    for entry in re.finditer(r"\{(.*?)\}", m.group(1), re.S):
        raw = entry.group(1)
        fields = dict(re.findall(r"'(\w+)'\s*:\s*'([^']*)'", raw))
        tmatch = re.search(r"'t'\s*:\s*new Date\(([^)]*)\)", raw)
        mag = safe_float(fields.get("s"))
        if mag is None or mag < min_mag:
            continue
        published = None
        if tmatch:
            nums: list[int] = []
            for chunk in tmatch.group(1).split(","):
                chunk = chunk.strip()
                if re.fullmatch(r"\d+\s*-\s*\d+", chunk):
                    a, b = (int(x) for x in chunk.split("-"))
                    nums.append(a - b)
                elif chunk.lstrip("-").isdigit():
                    nums.append(int(chunk))
            if len(nums) >= 5:
                try:
                    published = datetime(nums[0], nums[1] + 1, nums[2], nums[3], nums[4],
                                         nums[5] if len(nums) > 5 else 0, tzinfo=UTC)
                except ValueError:
                    published = None
        place = (fields.get("dR") or "").strip()
        if not place:
            continue
        key = f"{fields.get('lat')},{fields.get('lon')},{published.isoformat() if published else raw[:40]}"
        if key in seen:
            continue
        seen.add(key)
        out.append(_item(
            source, f"M{mag:.1f} — {place}", raw, f"{source.url}#{key}", published,
            {"mag": mag, "place": place, "depth_km": safe_float(fields.get("dep")),
             "lat": safe_float(fields.get("lat")), "lon": safe_float(fields.get("lon")),
             "quality": safe_float(fields.get("q"))},
            guid=key,
        ))
        if len(out) >= source.max_items:
            break
    return out


def parse_spa(source: Source, text: str, cfg: Any = None) -> list[RawItem]:
    m = re.search(r'(?is)<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', text)
    if not m:
        return []
    data = json_loads(m.group(1), {})
    out: list[RawItem] = []
    seen: set[str] = set()

    def walk(node: Any, depth: int = 0) -> None:
        if depth > 8 or len(out) >= source.max_items:
            return
        if isinstance(node, dict):
            title = node.get("title") or node.get("Title") or node.get("headline")
            url = (node.get("url") or node.get("newsUrl") or node.get("link")
                   or node.get("sharable_link") or node.get("shareableLink"))
            if isinstance(title, str) and isinstance(url, str) and len(title) > 15:
                if re.match(r"^[\w.-]+\.[a-z]{2,}/", url, re.I):
                    url = "https://" + url.lstrip("/")
                full = url if url.startswith("http") else join_url(source.base_url or source.url, url)
                full = canon_url(full)
                if full and full not in seen:
                    seen.add(full)
                    summary = node.get("summary") or node.get("subtitle") or ""
                    published = parse_dt(node.get("date") or node.get("publishedAt")
                                         or node.get("published_at"))
                    out.append(_item(source, title, summary, full, published,
                                     {"uuid": node.get("uuid")}, guid=full))
            for value in node.values():
                walk(value, depth + 1)
        elif isinstance(node, list):
            for value in node:
                walk(value, depth + 1)

    walk(data)
    return out


class _TGParser:
    def __init__(self) -> None:
        from html.parser import HTMLParser

        class Parser(HTMLParser):
            def __init__(self) -> None:
                super().__init__(convert_charrefs=True)
                self.messages: list[dict[str, Any]] = []
                self.depth = 0
                self.block_depth: int | None = None
                self.current: dict[str, Any] | None = None
                self.in_text = 0
                self.text_depth: int | None = None

            def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
                a = dict(attrs)
                cls = a.get("class") or ""
                if tag == "div":
                    self.depth += 1
                    if "tgme_widget_message" in cls and "tgme_widget_message_wrap" not in cls:
                        self.current = {"post": a.get("data-post"), "datetime": None, "text": []}
                        self.block_depth = self.depth
                    if "tgme_widget_message_text" in cls and self.current is not None:
                        self.in_text = 1
                        self.text_depth = self.depth
                if tag == "time" and self.current is not None and a.get("datetime"):
                    self.current["datetime"] = a.get("datetime")

            def handle_data(self, data: str) -> None:
                if self.current is not None and self.in_text:
                    self.current["text"].append(data)

            def handle_endtag(self, tag: str) -> None:
                if tag == "div":
                    if self.text_depth is not None and self.depth == self.text_depth:
                        self.in_text = 0
                        self.text_depth = None
                    if self.block_depth is not None and self.depth == self.block_depth:
                        text = " ".join("".join(self.current["text"]).split())
                        self.current["text"] = text
                        self.messages.append(self.current)
                        self.current = None
                        self.block_depth = None
                    self.depth -= 1

        self.parser = Parser()

    def feed(self, text: str) -> list[dict[str, Any]]:
        self.parser.feed(text)
        return self.parser.messages


def parse_telegram(source: Source, text: str, cfg: Any = None) -> list[RawItem]:
    parser = _TGParser()
    messages = parser.feed(text)
    out = []
    for msg in messages[-source.max_items:]:
        body = msg.get("text") or ""
        if not body:
            continue
        title = truncate(body, 180)
        post = msg.get("post") or ""
        url = f"https://t.me/{post}" if post else source.url
        published = parse_dt(msg.get("datetime"))
        out.append(_item(source, title, body, url, published, {"post": post},
                         guid=post or url))
    return out


def parse_ppi(source: Source, text: str, cfg: Any = None) -> list[RawItem]:
    """Pentagon Pizza Index verdict from a public channel (§8, variant A)."""
    parser = _TGParser()
    messages = parser.feed(text)
    for msg in reversed(messages):
        body = msg.get("text") or ""
        if not body:
            continue
        nt = norm_text(body)
        if re.search(r"\b(anomaly|anomalous|off the charts|record (high|busy)|spike|surge)\b", nt):
            state = "ANOMALY"
        elif re.search(
            r"\b(elevated|busy|above average|higher than (normal|usual)|unusual|heating up)\b",
            nt,
        ):
            state = "ELEVATED"
        elif re.search(r"\b(normal|quiet|average|steady|typical|back to normal)\b", nt):
            state = "NORMAL"
        else:
            continue
        post = msg.get("post") or ""
        url = f"https://t.me/{post}" if post else source.url
        payload = {"state": state, "excerpt": truncate(body, 300)}
        return [_item(
            source, f"Pentagon Pizza Index: {state}", body, url,
            parse_dt(msg.get("datetime")), payload,
            guid=f"ppi-{post or sha1(body)}", kind="indicator",
        )]
    return []


def parse_ukmto(source: Source, text: str, cfg: Any = None) -> list[RawItem]:
    """UKMTO advisories as rendered by r.jina.ai (markdown links)."""
    out: list[RawItem] = []
    seen: set[str] = set()
    pattern = re.compile(
        r"\[(\d{2}/\d{2}/\d{4})\s+([^\]:]+):\s*"
        r"(UKMTO\s+(?:WARNING|ADVISORY)\s+\d{2,3}-\d{2})\s*[-–:]?\s*([^\]]*)\]"
        r"\((https?://[^)]+)\)",
        re.I,
    )
    for m in pattern.finditer(text):
        date_raw, kind, advisory, rest, url = m.groups()
        advisory = re.sub(r"\s+", " ", advisory.upper())
        if advisory in seen:
            continue
        seen.add(advisory)
        try:
            published = datetime.strptime(date_raw, "%d/%m/%Y").replace(tzinfo=UTC)
        except ValueError:
            published = parse_dt(date_raw)
        kind = kind.strip().lower()
        out.append(_item(
            source, f"{advisory}: {kind}", f"{kind}: {rest.strip()}",
            url, published,
            {"advisory": advisory, "incident_type": kind},
            guid=advisory, category="gulf",
        ))
        if len(out) >= source.max_items:
            break
    return out


# ---------------- helpers ----------------
def _extract_colour(text: str) -> str | None:
    nt = norm_text(text)
    if re.search(r"\bred\b|красн", nt):
        return "red"
    if re.search(r"\borange\b|оранж", nt):
        return "orange"
    if re.search(r"\byellow\b|желт", nt):
        return "yellow"
    if re.search(r"\bgreen\b|зелен", nt):
        return "green"
    return None


def _extract_ash_ft(text: str) -> int | None:
    m = re.search(r"(?i)\bFL\s?(\d{2,3})\b", text)
    if m:
        return int(m.group(1)) * 100
    m = re.search(r"(?i)\b(\d{1,2}(?:[.,]\d)?)\s*(?:km|км)\b", text)
    if m:
        return int(float(m.group(1).replace(",", ".")) * 3280.84)
    m = re.search(r"(?i)\b(\d{4,5})\s*(?:ft|feet|фут)\b", text)
    if m:
        return int(m.group(1))
    return None


def _oref_districts(cfg: Any) -> list[dict[str, Any]]:
    if cfg is None:
        return []
    return cfg.geofences.get("oref_districts", [])


def _districts_for_cities(cities: list[str], districts: list[dict[str, Any]]) -> list[str]:
    out = set()
    norm_cities = [norm_text(c) for c in cities]
    for district in districts:
        for anchor in district.get("cities", []):
            a = norm_text(anchor)
            if any(a == c or a in c or c in a for c in norm_cities):
                out.add(district["name"])
                break
    return sorted(out)


def _threat_text(threat: int | None) -> str:
    return {
        0: "rockets/missiles",
        1: "hostile aircraft/drone",
        2: "earthquake",
        3: "tsunami",
        4: "hazardous materials",
        5: "infiltration",
        6: "infiltration",
        7: "maritime alert",
    }.get(threat, "alert")


PARSERS = {
    "rss": parse_rss,
    "gnews_rss": parse_gnews,
    "usgs_geojson": parse_usgs,
    "emsc_geojson": parse_emsc,
    "ingv_geojson": parse_ingv,
    "gdacs_rss": parse_gdacs,
    "tsunami_atom": parse_tsunami,
    "hans_json": parse_hans,
    "html_links": parse_html_links,
    "html_table": parse_html_table,
    "imo_quakes": parse_imo_quakes,
    "vaac_tokyo": parse_vaac_tokyo,
    "vaac_messages": parse_vaac_messages,
    "vaac_list": parse_vaac_list,
    "volcano_status_html": parse_volcano_status,
    "jma_eqvol": parse_jma_eqvol,
    "vona_jina": parse_vona,
    "who_don_odata": parse_who_don,
    "ukrainealarm_json": parse_ukrainealarm,
    "tzeva_json": parse_tzeva,
    "oref_json": parse_oref,
    "adsb_json": parse_adsb,
    "ews_dashboard": parse_ews_dashboard,
    "safecast_json": parse_safecast,
    "tradingview_json": parse_tradingview,
    "eia_json": parse_eia,
    "frankfurter_json": parse_frankfurter,
    "spa_next_data": parse_spa,
    "telegram_html": parse_telegram,
    "ppi_telegram": parse_ppi,
    "ukmto_jina": parse_ukmto,
    "easa_czib": parse_easa_czib,
    "travel_advisory": parse_travel_advisory,
    "portwatch_json": parse_portwatch,
    "gpsjam_csv": parse_gpsjam,
    "opensky_json": parse_opensky,
}


RSS_PARSERS = {"rss", "gnews_rss", "gdacs_rss", "tsunami_atom"}


def parse(source: Source, raw: Any, cfg: Any = None) -> list[RawItem]:
    fn = PARSERS.get(source.parser)
    if fn is None:
        return []
    if isinstance(raw, bytes) and source.parser not in RSS_PARSERS:
        raw = raw.decode("utf-8", "replace")
    try:
        return fn(source, raw, cfg)
    except Exception:
        logging.getLogger(__name__).exception("parser %s failed for %s", source.parser, source.id)
        return []
