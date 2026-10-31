"""EWS (Apocalypse Early Warning System) indicator display and alerts."""

from __future__ import annotations

import logging
from typing import Any

from .telegram_api import esc
from .util import parse_dt

log = logging.getLogger(__name__)

NOTIFY_MIN_LEVEL = 4
SOURCES = {
    "ews_jets": ("✈️ Джеты", "Джеты"),
    "ews_military": ("🪖 Военные", "Военные"),
}
ALERT_LABELS_RU = {
    "normal": "норма",
    "elevated": "повышенная активность",
    "alarm": "тревога",
    "emergency": "чрезвычайная ситуация",
}


def _level_and_meta(row: dict[str, Any] | None) -> tuple[int, dict[str, Any]]:
    meta = (row or {}).get("meta") or {}
    try:
        level = int(meta.get("emergency_level") or 0)
    except (TypeError, ValueError):
        level = 0
    return level, meta


def _as_of(value: Any) -> str:
    dt = parse_dt(value)
    return dt.strftime("%H:%M UTC") if dt else ""


async def notify_update(app, source, payload: dict[str, Any]) -> bool:
    """Alert once when an EWS indicator rises to level 4-5."""
    try:
        level = int(payload.get("emergency_level") or 0)
    except (TypeError, ValueError):
        level = 0
    key = f"ews_notified:{source.id}"
    if level < NOTIFY_MIN_LEVEL:
        if await app.db.meta_get(key, 0):
            await app.db.meta_set(key, 0)
        return False
    notified = await app.db.meta_get(key, 0)
    try:
        notified = int(notified or 0)
    except (TypeError, ValueError):
        notified = 0
    if level <= notified:
        return False
    tg = getattr(app, "tg", None)
    if tg is None or not getattr(tg, "configured", False):
        return False
    _icon, short = SOURCES.get(source.id, ("", source.name))
    alert = str(payload.get("alert_level") or "").lower()
    lines = [
        f"🚨 <b>EWS {esc(short)}: {esc(ALERT_LABELS_RU.get(alert, alert))} — "
        f"{level}/5</b>",
    ]
    z = payload.get("z_score")
    concurrent = payload.get("concurrent")
    expected = payload.get("expected")
    detail: list[str] = []
    if isinstance(z, (int, float)):
        detail.append(f"z {z:+.1f}σ")
    if isinstance(concurrent, int):
        counts = f"{concurrent} ВС"
        if isinstance(expected, int):
            counts += f" при норме ~{expected}"
        detail.append(counts)
    if detail:
        lines.append(" · ".join(detail))
    stamp = _as_of(payload.get("as_of"))
    if stamp:
        lines.append(f"Данные: {stamp}")
    try:
        await tg.send_message("\n".join(lines), app.cfg.channel_topic("DIGEST"))
    except Exception:
        log.exception("failed to send EWS alert for %s", source.id)
        return False
    await app.db.meta_set(key, level)
    return True


async def digest_parts(db) -> list[str]:
    parts: list[str] = []
    for source_id, (_icon, short) in SOURCES.items():
        level, meta = _level_and_meta(await db.indicator_get(source_id))
        if level <= 0:
            continue
        text = f"{short} {level}/5"
        z = meta.get("z_score")
        if isinstance(z, (int, float)):
            text += f" ({z:+.1f}σ)"
        parts.append(text)
    return parts


async def status_lines(db) -> list[str]:
    lines: list[str] = []
    for source_id, (icon, short) in SOURCES.items():
        row = await db.indicator_get(source_id)
        level, meta = _level_and_meta(row)
        if level <= 0:
            continue
        state = str((row or {}).get("value") or meta.get("alert_level") or "")
        line = f"{icon} {short}: <b>{esc(state)}</b> {level}/5"
        z = meta.get("z_score")
        if isinstance(z, (int, float)):
            line += f" · z {z:+.1f}σ"
        concurrent = meta.get("concurrent")
        expected = meta.get("expected")
        if isinstance(concurrent, int):
            line += f" · {concurrent}"
            if isinstance(expected, int):
                line += f"/~{expected}"
        stamp = _as_of(meta.get("as_of"))
        if stamp:
            line += f" · {stamp}"
        lines.append(line)
    return lines
