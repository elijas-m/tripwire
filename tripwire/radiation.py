"""Safecast radiation indicator: level 1-5 from fixed µSv/h sensors.

Crowd-sourced sensors are noisy (bad clocks, damaged tubes), so the level is
driven by per-device deviation from that device's own short-term baseline, and
implausible readings are dropped before scoring.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta
from typing import Any

from .models import RawItem, Source
from .telegram_api import esc
from .util import json_loads, parse_dt, utcnow

SOURCE_ID = "safecast_radiation"

DEFAULTS = {
    "baseline_hours": 3.0,
    "recent_minutes": 60.0,
    "min_samples": 5.0,
    "spike_factor": 3.0,
    "spike_delta_usv": 0.5,
    "extreme_usv": 10.0,
    "max_plausible_usv": 100.0,
}
LEVEL_NAMES = {1: "NORMAL", 2: "ELEVATED", 3: "WATCH", 4: "ALERT", 5: "EMERGENCY"}


def _settings(settings: dict[str, Any] | None) -> dict[str, float]:
    th = dict(DEFAULTS)
    for key, value in (settings or {}).items():
        if key in th:
            th[key] = float(value)
    return th


def summarize(
    rows: list[dict[str, Any]],
    settings: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Collapse a Safecast measurement window into an indicator payload."""
    th = _settings(settings)
    now = now or utcnow()
    start = now - timedelta(hours=th["baseline_hours"])
    future_cut = now + timedelta(minutes=10)
    recent_cut = now - timedelta(minutes=th["recent_minutes"])

    samples: list[tuple[datetime, float, str]] = []
    dropped = {"future": 0, "stale": 0, "implausible": 0}
    for row in rows:
        try:
            value = float(row.get("value"))
        except (TypeError, ValueError):
            dropped["implausible"] += 1
            continue
        ts = parse_dt(row.get("captured_at"))
        if ts is None:
            dropped["stale"] += 1
            continue
        if ts > future_cut:
            dropped["future"] += 1
            continue
        if ts < start:
            dropped["stale"] += 1
            continue
        if not 0.0 < value <= th["max_plausible_usv"]:
            dropped["implausible"] += 1
            continue
        device = row.get("device_id")
        device_key = "" if device is None else str(device)
        samples.append((ts, value, device_key))

    if not samples:
        return None

    values = [value for _ts, value, _dev in samples]
    by_device: dict[str, list[tuple[datetime, float]]] = {}
    for ts, value, device_key in samples:
        if device_key:
            by_device.setdefault(device_key, []).append((ts, value))

    anomalous: list[str] = []
    extreme = 0
    for device_key, points in by_device.items():
        if len(points) < th["min_samples"]:
            continue
        recent = [value for ts, value in points if ts >= recent_cut]
        if not recent:
            continue
        baseline = statistics.median(value for _ts, value in points)
        threshold = max(baseline * th["spike_factor"], baseline + th["spike_delta_usv"])
        peak = max(recent)
        if peak > threshold:
            anomalous.append(device_key)
        if peak > th["extreme_usv"]:
            extreme += 1

    if extreme >= 3:
        level = 5
    elif extreme == 2:
        level = 4
    elif extreme == 1 or len(anomalous) >= 2:
        level = 3
    elif len(anomalous) == 1:
        level = 2
    else:
        level = 1

    state = LEVEL_NAMES[level]
    return {
        "state": state,
        "level": level,
        "value": state,
        "samples": len(samples),
        "devices": len(by_device),
        "median_usv": round(statistics.median(values), 4),
        "max_usv": round(max(values), 3),
        "anomalous": sorted(anomalous),
        "extreme": extreme,
        "dropped": dropped,
        "as_of": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def parse_safecast(source: Source, text: str, cfg: Any = None) -> list[RawItem]:
    data = json_loads(text, None)
    if not isinstance(data, list):
        return []
    settings = cfg.g("radiation") if cfg is not None and hasattr(cfg, "g") else None
    payload = summarize([row for row in data if isinstance(row, dict)], settings)
    if payload is None:
        return []
    now = utcnow()
    title = f"Радиация: {payload['state']} {payload['level']}/5"
    body = (
        f"{payload['median_usv']:.2f} мкЗв/ч · датчиков {payload['devices']} · "
        f"выбросов {len(payload['anomalous'])}"
    )
    return [
        RawItem(
            source_id=source.id,
            title=title,
            body=body,
            url=source.url,
            published_at=now,
            payload=payload,
            guid=f"safecast-{int(now.timestamp())}",
            kind="indicator",
        )
    ]


async def digest_parts(db: Any) -> list[str]:
    row = await db.indicator_get(SOURCE_ID)
    if not row:
        return []
    meta = row.get("meta") or {}
    text = f"Радиация {row.get('value') or 'NORMAL'}"
    median = meta.get("median_usv")
    if isinstance(median, (int, float)):
        text += f" ({median:.2f} мкЗв/ч)"
    return [text]


async def status_lines(db: Any) -> list[str]:
    row = await db.indicator_get(SOURCE_ID)
    if not row:
        return []
    meta = row.get("meta") or {}
    state = str(row.get("value") or meta.get("state") or "—")
    line = f"☢️ Радиация: <b>{esc(state)}</b>"
    level = meta.get("level")
    if isinstance(level, int) and level > 0:
        line += f" {level}/5"
    median = meta.get("median_usv")
    if isinstance(median, (int, float)):
        line += f" · {median:.2f} мкЗв/ч"
    devices = meta.get("devices")
    if isinstance(devices, int) and devices > 0:
        line += f" · датчиков {devices}"
    anomalous = meta.get("anomalous")
    if isinstance(anomalous, list) and anomalous:
        line += f" · выбросов {len(anomalous)}"
    return [line]
