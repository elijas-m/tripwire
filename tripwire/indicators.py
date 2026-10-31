"""Derived indicators: market shock (§4.6) and Pentagon Pizza Index (§8)."""

from __future__ import annotations

from typing import Any

from .telegram_api import esc
from .util import now_ts as clock_ts

MARKET_SYMBOLS = {
    "ICEEUR:BRN1!": "Brent",
    "NYMEX:CL1!": "WTI",
    "TVC:GOLD": "Gold",
    "TVC:SILVER": "Silver",
    "TVC:VIX": "VIX",
    "FX_IDC:USDRUB": "USD/RUB",
    "FX_IDC:USDILS": "USD/ILS",
}

DEFAULT_MARKET_THRESHOLDS = {
    "brent_surge_pct": 7.0,
    "brent_spike_pct": 4.0,
    "gold_rally_pct": 3.0,
    "metal_drop_pct": 3.0,
    "vix_confirm_pct": 20.0,
    "vix_spike_pct": 30.0,
    "vix_high": 35.0,
    "fx_drop_pct": 5.0,
}

_SUMMARY_ORDER = (
    ("brent_pct", "Brent"),
    ("tvc:gold_pct", "золото"),
    ("tvc:silver_pct", "серебро"),
    ("tvc:vix_pct", "VIX"),
)


def market_summary(meta: dict[str, Any] | None) -> list[str]:
    """24h moves for the digest and /status, e.g. ["Brent +2.3%", "VIX +33.6%"]."""
    meta = meta or {}
    parts: list[str] = []
    for key, label in _SUMMARY_ORDER:
        value = meta.get(key)
        if isinstance(value, (int, float)):
            parts.append(f"{label} {value:+.1f}%")
    return parts


async def _change(db: Any, symbol: str, hours: float, now_ts: int) -> float | None:
    rows = await db.market_recent(symbol, now_ts - int(hours * 3600) - 1800)
    if not rows:
        return None
    latest = rows[-1]
    close = latest["close"]
    if close is None:
        return None
    baseline = None
    cutoff = now_ts - int(hours * 3600)
    for row in rows:
        if row["ts"] <= cutoff and row["close"]:
            baseline = row["close"]
    if baseline:
        return (float(close) / float(baseline) - 1.0) * 100.0
    fallback = latest["change_pct"]
    if fallback is not None and hours >= 20:
        return float(fallback)
    return None


async def compute_market_shock(
    db: Any, now_ts: int | None = None, settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Brent +7%/24h or +4%/1h, gold +3%/24h, VIX>35 or +30%/24h, RUB/ILS -5%/24h.

    Thresholds come from `market_shock` in thresholds.yaml. Gold and silver
    sell-offs count only when VIX rises at least `vix_confirm_pct` over the same
    window: a metal drop on its own is often a routine correction, a metal drop
    with spiking volatility is a risk-off shock.
    """
    now_ts = int(now_ts or clock_ts())
    th = dict(DEFAULT_MARKET_THRESHOLDS)
    for key, value in (settings or {}).items():
        if key in th:
            th[key] = float(value)
    triggers: list[str] = []
    metrics: dict[str, float] = {}

    # VIX is computed first: it gates the gold/silver downside triggers.
    vix_pct = await _change(db, "TVC:VIX", 24, now_ts)
    if vix_pct is not None:
        metrics["tvc:vix_pct"] = round(vix_pct, 2)
    vix = await db.market_last("TVC:VIX")
    vix_close = float(vix["close"]) if vix and vix["close"] is not None else None
    if vix_close is not None:
        metrics["vix"] = round(vix_close, 2)
    if vix_pct is not None and vix_pct >= th["vix_spike_pct"]:
        triggers.append(f"VIX {vix_pct:+.1f}%/24h")
    if vix_close is not None and vix_close > th["vix_high"]:
        triggers.append(f"VIX {vix_close:.1f}")

    brent_24 = await _change(db, "ICEEUR:BRN1!", 24, now_ts)
    brent_1 = await _change(db, "ICEEUR:BRN1!", 1, now_ts)
    if brent_24 is not None:
        metrics["brent_pct"] = round(brent_24, 2)
        if brent_24 >= th["brent_surge_pct"]:
            triggers.append(f"Brent {brent_24:+.1f}%/24h")
    if brent_1 is not None and brent_1 >= th["brent_spike_pct"]:
        triggers.append(f"Brent {brent_1:+.1f}%/1h")

    vix_confirmed = vix_pct is not None and vix_pct >= th["vix_confirm_pct"]
    drop_note = f" (VIX {vix_pct:+.1f}%)" if vix_confirmed else ""

    gold = await _change(db, "TVC:GOLD", 24, now_ts)
    if gold is not None:
        metrics["tvc:gold_pct"] = round(gold, 2)
        if gold >= th["gold_rally_pct"]:
            triggers.append(f"Gold {gold:+.1f}%/24h")
        elif gold <= -th["metal_drop_pct"] and vix_confirmed:
            triggers.append(f"Gold {gold:+.1f}%/24h{drop_note}")

    silver = await _change(db, "TVC:SILVER", 24, now_ts)
    if silver is not None:
        metrics["tvc:silver_pct"] = round(silver, 2)
        if silver <= -th["metal_drop_pct"] and vix_confirmed:
            triggers.append(f"Silver {silver:+.1f}%/24h{drop_note}")

    for symbol, label in (("FX_IDC:USDRUB", "USD/RUB"), ("FX_IDC:USDILS", "USD/ILS")):
        pct = await _change(db, symbol, 24, now_ts)
        if pct is None:
            continue
        metrics[f"{label.lower().replace('/', '')}_pct"] = round(pct, 2)
        # USD/XXX rising means the local currency is weakening.
        if pct >= th["fx_drop_pct"]:
            triggers.append(f"{label} {pct:+.1f}%/24h")

    value = "ANOMALY" if triggers else "NORMAL"
    payload = {"value": value, "triggers": triggers, "as_of": now_ts, **metrics}
    current = await db.indicator_get("market_shock")
    if (
        current is None
        or current.get("value") != value
        or (current.get("meta") or {}).get("triggers") != triggers
        or now_ts - int(current.get("updated_at") or 0) >= 900
    ):
        await db.indicator_set("market_shock", value, payload)
    return payload


async def world_digest(db: Any) -> list[str]:
    """One-line states for maritime, GPS and airspace indicators."""
    parts: list[str] = []
    portwatch = await db.indicator_get("imf_portwatch")
    if portwatch:
        meta = portwatch.get("meta") or {}
        text = f"Проливы {portwatch.get('value')}"
        notable = meta.get("notable") or []
        if notable:
            text += f" ({', '.join(str(item) for item in notable[:2])})"
        parts.append(text)
    gps = await db.indicator_get("gpsjam")
    if gps:
        meta = gps.get("meta") or {}
        text = f"GPS-помехи {gps.get('value')}"
        if isinstance(meta.get("bad_hexes"), int):
            text += f" ({meta['bad_hexes']})"
        parts.append(text)
    air = await db.indicator_get("opensky_airspace")
    if air:
        meta = air.get("meta") or {}
        text = f"Небо UA {air.get('value')}"
        if isinstance(meta.get("airborne_core"), int):
            text += f" ({meta['airborne_core']})"
        parts.append(text)
    return parts


async def world_status(db: Any) -> list[str]:
    lines: list[str] = []
    portwatch = await db.indicator_get("imf_portwatch")
    if portwatch:
        meta = portwatch.get("meta") or {}
        line = f"🚢 Проливы: <b>{esc(str(portwatch.get('value') or '—'))}</b>"
        if meta.get("as_of"):
            line += f" · {esc(str(meta['as_of']))}"
        notable = meta.get("notable") or []
        if notable:
            line += " · " + ", ".join(esc(str(item)) for item in notable[:3])
        lines.append(line)
    gps = await db.indicator_get("gpsjam")
    if gps:
        meta = gps.get("meta") or {}
        line = f"📡 GPS-помехи: <b>{esc(str(gps.get('value') or '—'))}</b>"
        if isinstance(meta.get("bad_hexes"), int):
            line += f" · {meta['bad_hexes']} гексов"
        if meta.get("date"):
            line += f" · {esc(str(meta['date']))}"
        lines.append(line)
    air = await db.indicator_get("opensky_airspace")
    if air:
        meta = air.get("meta") or {}
        line = f"✈️ Небо UA: <b>{esc(str(air.get('value') or '—'))}</b>"
        if isinstance(meta.get("airborne_core"), int):
            line += f" · {meta['airborne_core']} в зоне"
        if isinstance(meta.get("airborne_region"), int):
            line += f" · {meta['airborne_region']} в регионе"
        lines.append(line)
    return lines
