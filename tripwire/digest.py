"""Daily digest and source degradation report (§10, §11)."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from . import diplo, ews, observability, radiation
from .alerts import THEATER_LABELS
from .indicators import market_summary, world_digest
from .models import L1, L2, L3, LEVEL_NAMES
from .telegram_api import esc
from .util import UTC

MIN_EVENT_SCORE = 1.0


async def source_problems(app) -> dict[str, list[dict[str, Any]]]:
    """Failed feeds and silent daily feeds, from source_health."""
    health = {row["source_id"]: row for row in await app.db.health_all()}
    now = int(time.time())
    failed: list[dict[str, Any]] = []
    silent: list[dict[str, Any]] = []
    for sid, src in app.sources.items():
        row = health.get(sid) or {}
        failures = int(row.get("consecutive_failures") or 0)
        if failures > 0:
            failed.append({
                "id": sid, "name": src.name, "failures": failures,
                "error": row.get("last_error") or "",
                "last_ok": row.get("last_ok"),
            })
            continue
        total = int(row.get("total_items") or 0)
        last_ok = int(row.get("last_ok") or 0)
        if total > 0 and last_ok and now - last_ok > 72 * 3600:
            silent.append({
                "id": sid, "name": src.name,
                "hours": round((now - last_ok) / 3600.0),
                "last_ok": last_ok,
            })
    return {"failed": failed, "silent": silent}


async def build_digest(app, hours: int = 24) -> str:
    now = int(time.time())
    since = now - hours * 3600
    tz = ZoneInfo(app.cfg.app.get("timezone", "UTC"))
    stamp = datetime.fromtimestamp(now, UTC).astimezone(tz).strftime("%d.%m %H:%M")

    snapshot = await app.correlator.snapshot()
    theaters = " · ".join(
        f"{THEATER_LABELS.get(row['theater'], row['theater'])} "
        f"{LEVEL_NAMES.get(int(row['level']), 'NOISE')}({float(row['score']):.1f})"
        for row in snapshot
    )

    alerts = await app.db.alerts_since(since)
    counts = {level: sum(1 for row in alerts if int(row["level"]) == level) for level in (L2, L3)}
    acked = sum(1 for row in alerts if row["ack_at"])

    digest_cfg = app.cfg.app_cfg.get("digest", {}) or {}
    min_score = float(digest_cfg.get("min_event_score", MIN_EVENT_SCORE))

    watch_rows = await app.db.fetchall(
        """SELECT theater, title, score, level FROM events
           WHERE ingested_at>=? AND level=? AND score>=?
           ORDER BY score DESC LIMIT 12""",
        (since, L1, min_score),
    )
    watch_lines = [
        f"• {THEATER_LABELS.get(row['theater'], row['theater'])} "
        f"{float(row['score']):.1f} — {esc((row['title'] or '')[:120])}"
        for row in watch_rows
    ]

    problems = await source_problems(app)
    coverage = await observability.assess(app.cfg, app.db)
    if coverage.state == observability.STATE_UNKNOWN:
        health_parts = [f"{len(app.sources)} источников · нет данных"]
    else:
        health_parts = [coverage.summary]
    if coverage.down:
        health_parts.append(f"нет данных: {esc(observability.down_labels(app.cfg, coverage))}")
    if problems["failed"]:
        names = ", ".join(
            f"{item['name']} ({item['failures']}×)" for item in problems["failed"][:5]
        )
        health_parts.append(f"сбои: {esc(names)}")
    if problems["silent"]:
        names = ", ".join(
            f"{item['name']} ({item['hours']} ч)" for item in problems["silent"][:5]
        )
        health_parts.append(f"молчат: {esc(names)}")

    ppi = await app.db.indicator_get("ppi")
    shock = await app.db.indicator_get("market_shock")
    indicator_parts: list[str] = []
    if ppi:
        indicator_parts.append(f"PPI {ppi['value']}")
    if shock:
        moves = market_summary(shock.get("meta"))
        value = shock.get("value") or "NORMAL"
        indicator_parts.append(
            f"Рынки {value}" + (f" ({', '.join(moves)})" if moves else "")
        )
    indicator_parts.extend(await ews.digest_parts(app.db))
    indicator_parts.extend(await radiation.digest_parts(app.db))
    indicator_parts.extend(await diplo.digest_parts(app.db))
    indicator_parts.extend(await world_digest(app.db))

    lines = [f"📊 <b>ДАЙДЖЕСТ</b> · {stamp} · {hours} ч", ""]
    if theaters:
        lines.append(f"<b>Театры:</b> {esc(theaters)}")
    lines.append(
        f"<b>Алерты:</b> {len(alerts)} (CRITICAL {counts[L3]}, WARNING {counts[L2]}, "
        f"подтверждено {acked})"
    )
    if watch_lines:
        lines += ["", f"<b>WATCH за сутки ({len(watch_rows)}):</b>", *watch_lines]
    lines += ["", f"<b>Источники:</b> {' · '.join(health_parts)}"]
    if indicator_parts:
        lines.append(f"<b>Индикаторы:</b> {' · '.join(indicator_parts)}")
    return "\n".join(lines)[:4096]
