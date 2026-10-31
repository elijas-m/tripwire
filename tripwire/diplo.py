"""Diplomatic evacuation indicator, in the spirit of the Pentagon Pizza Index.

Embassy and consulate evacuations are a classic leading indicator of an
imminent conflict. The signal is derived from stored event titles: a title
counts when it ties a diplomatic mission to an evacuation or closure action.
Denials, rumors and stories that only reference an earlier year are ignored.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

from .telegram_api import esc
from .util import UTC, now_ts as clock_ts

log = logging.getLogger(__name__)

SOURCE_ID = "diplo_evac"
LABEL = "Дипэвакуация"

DEFAULTS = {
    "window_hours": 168.0,
    "country_window_hours": 72.0,
    "elevated_clusters": 1.0,
    "anomaly_clusters": 3.0,
    "anomaly_countries": 2.0,
    "max_matches": 5.0,
}
LEVEL_NAMES = {1: "NORMAL", 2: "ELEVATED", 3: "ANOMALY"}

_ACTION_EN = (
    r"(?:evacuat\w*|withdraw\w*|pull(?:s|ed|ing)?\s+out"
    r"|(?:order(?:ed|s|ing)?|authoriz(?:ed|es|ing))\s+departure"
    r"|(?:ordered|told|asked|urged)\s+to\s+leave"
    r"|suspend\w*\s+(?:operations|work|activities)|shut(?:s|ting)?\s+down|clos(?:e|es|ed|ing))"
)
_TARGET_EN = (
    r"(?:embass(?:y|ies)|consulat(?:e|es)|diplomatic\s+(?:mission|staff|personnel)|diplomats?)"
)
_EN = re.compile(
    rf"\b{_TARGET_EN}\b[^.;!?]{{0,80}}\b{_ACTION_EN}\b"
    rf"|\b(?:evacuat\w*|withdraw\w*|pull(?:s|ed|ing)?\s+out"
    rf"|(?:order(?:ed|s|ing)?|authoriz(?:ed|es|ing))\s+departure)\b"
    rf"[^.;!?]{{0,60}}\b{_TARGET_EN}\b",
    re.I,
)

_ACTION_RU = (
    r"(?:эвакуац\w*|вывоз\w*|вывез\w*|сверну\w*|закры\w*|приостанов\w*"
    r"|(?:приказ|предпис)\w*[^.;!?]{0,30}(?:выезд|эвакуац)\w*)"
)
_TARGET_RU = r"(?:посольств\w*|консульств\w*|дипломат\w*)"
_RU = re.compile(
    rf"{_TARGET_RU}[^.;!?]{{0,80}}{_ACTION_RU}"
    rf"|{_ACTION_RU}[^.;!?]{{0,60}}{_TARGET_RU}",
    re.I,
)

_DENIAL = re.compile(
    r"\b(?:denied|denies|dismissed|rumou?rs?|hoax|refuted|false\s+report|"
    r"not\s+true|no\s+plans?|holiday|weekend|renovation|repairs?|maintenance)\b"
    r"|(?:слух\w*|дезинформац\w*|опроверг\w*|не\s+подтверд\w*|не\s+планир\w*|фейк\w*)",
    re.I,
)
_YEAR = re.compile(r"\b(19\d{2}|20\d{2})\b")


def _settings(settings: dict[str, Any] | None) -> dict[str, float]:
    th = dict(DEFAULTS)
    for key, value in (settings or {}).items():
        if key in th:
            th[key] = float(value)
    return th


def is_diplomatic_evacuation(title: str, now_year: int | None = None) -> bool:
    """True when a headline reports a diplomatic mission evacuation or closure."""
    text = (title or "").strip()
    if not text or not (_EN.search(text) or _RU.search(text)):
        return False
    if _DENIAL.search(text):
        return False
    year = now_year or datetime.fromtimestamp(clock_ts(), UTC).year
    years = [int(item) for item in _YEAR.findall(text)]
    return not years or any(item >= year for item in years)


def _cluster_key(row: dict[str, Any]) -> str:
    cluster_id = row.get("cluster_id")
    if cluster_id is not None:
        return f"c{cluster_id}"
    return f"e{row.get('id')}"


def summarize(
    rows: list[dict[str, Any]],
    settings: dict[str, Any] | None = None,
    now_ts: int | None = None,
) -> dict[str, Any]:
    th = _settings(settings)
    now_ts = int(now_ts or clock_ts())
    now_year = datetime.fromtimestamp(now_ts, UTC).year
    since = now_ts - int(th["window_hours"] * 3600)
    country_since = now_ts - int(th["country_window_hours"] * 3600)

    matches: dict[str, dict[str, Any]] = {}
    for row in rows:
        title = str(row.get("title") or "")
        if not is_diplomatic_evacuation(title, now_year):
            continue
        try:
            ts = int(row.get("ts") or 0)
        except (TypeError, ValueError):
            continue
        if ts < since or ts > now_ts + 600:
            continue
        country = str(row.get("country") or "").upper() or None
        key = _cluster_key(row)
        current = matches.get(key)
        if current is not None and current["ts"] >= ts:
            continue
        matches[key] = {
            "ts": ts,
            "title": title[:160],
            "country": country,
            "theater": row.get("theater"),
            "url": row.get("url"),
            "cluster_id": row.get("cluster_id"),
        }

    ordered = sorted(matches.values(), key=lambda item: item["ts"], reverse=True)
    countries = sorted({item["country"] for item in ordered if item["country"]})
    countries_72h = sorted(
        {item["country"] for item in ordered if item["country"] and item["ts"] >= country_since}
    )

    clusters = len(ordered)
    if len(countries_72h) >= th["anomaly_countries"] or clusters >= th["anomaly_clusters"]:
        level = 3
    elif clusters >= th["elevated_clusters"]:
        level = 2
    else:
        level = 1

    state = LEVEL_NAMES[level]
    return {
        "state": state,
        "level": level,
        "clusters": clusters,
        "countries": countries,
        "countries_72h": countries_72h,
        "window_hours": int(th["window_hours"]),
        "matches": ordered[: int(th["max_matches"])],
        "as_of": datetime.fromtimestamp(now_ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


async def compute_diplo_evac(
    db: Any,
    settings: dict[str, Any] | None = None,
    now_ts: int | None = None,
) -> dict[str, Any]:
    th = _settings(settings)
    now_ts = int(now_ts or clock_ts())
    since = now_ts - int(th["window_hours"] * 3600)
    rows = await db.fetchall(
        """SELECT id, cluster_id, title, country, theater, url, status,
                  COALESCE(published_at, ingested_at) AS ts
           FROM events
           WHERE kind='event' AND status<>'dropped' AND ingested_at>=?
           ORDER BY ingested_at DESC""",
        (since,),
    )
    payload = summarize([dict(row) for row in rows], th, now_ts)
    current = await db.indicator_get(SOURCE_ID)
    if (
        current is None
        or current.get("value") != payload["state"]
        or (current.get("meta") or {}).get("clusters") != payload["clusters"]
        or now_ts - int(current.get("updated_at") or 0) >= 900
    ):
        await db.indicator_set(SOURCE_ID, payload["state"], payload)
    return payload


def _window_label(meta: dict[str, Any]) -> str:
    hours = meta.get("window_hours")
    if not isinstance(hours, (int, float)) or hours <= 0:
        return ""
    return f"{int(hours // 24)}д" if hours >= 24 else f"{int(hours)}ч"


async def digest_parts(db: Any) -> list[str]:
    row = await db.indicator_get(SOURCE_ID)
    if not row:
        return []
    meta = row.get("meta") or {}
    text = f"{LABEL} {row.get('value') or 'NORMAL'}"
    clusters = meta.get("clusters")
    window = _window_label(meta)
    if isinstance(clusters, int) and clusters > 0 and window:
        text += f" ({clusters} за {window})"
    return [text]


async def status_lines(db: Any) -> list[str]:
    row = await db.indicator_get(SOURCE_ID)
    if not row:
        return []
    meta = row.get("meta") or {}
    state = str(row.get("value") or meta.get("state") or "—")
    line = f"🛫 {LABEL}: <b>{esc(state)}</b>"
    clusters = meta.get("clusters")
    window = _window_label(meta)
    if isinstance(clusters, int):
        line += f" · {clusters}"
        if window:
            line += f"/{window}"
    countries = meta.get("countries")
    if isinstance(countries, list) and countries:
        line += " · " + ", ".join(esc(str(item)) for item in countries[:5])
    return [line]
