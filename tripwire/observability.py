"""Observability: how much of the configured watch is actually being observed."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .util import now_ts

STATE_UNKNOWN = "UNKNOWN"
STATE_FULL = "FULL"
STATE_DEGRADED = "DEGRADED"
STATE_BLIND = "BLIND"


@dataclass(slots=True)
class Observability:
    total: int = 0
    healthy: int = 0
    ratio: float = 0.0
    state: str = STATE_UNKNOWN
    down: list[tuple[str, int | None]] = field(default_factory=list)
    key_down: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        if self.state == STATE_UNKNOWN:
            return "нет данных"
        return f"{self.healthy}/{self.total} · {self.ratio * 100:.0f}% · {self.state}"


def down_labels(cfg: Any, obs: Observability, limit: int = 3) -> str:
    """Human-readable list of sources without fresh data."""
    parts: list[str] = []
    for source_id, hours in obs.down[:limit]:
        source = cfg.sources_by_id.get(source_id)
        name = source.name if source is not None else source_id
        if hours is None:
            parts.append(f"{name} (нет опроса)")
        elif hours <= 0:
            parts.append(f"{name} (сбой)")
        else:
            parts.append(f"{name} ({hours} ч)")
    if len(obs.down) > limit:
        parts.append(f"+{len(obs.down) - limit}")
    return ", ".join(parts)


async def assess(cfg: Any, db: Any, now: int | None = None) -> Observability:
    """Coverage of enabled sources from source_health; read-only and network-free."""
    sources = cfg.enabled_sources()
    total = len(sources)
    if total == 0:
        return Observability()

    settings = (cfg.thresholds.get("observability") or {}) if hasattr(cfg, "thresholds") else {}
    full_ratio = float(settings.get("full_ratio", 0.9))
    degraded_ratio = float(settings.get("degraded_ratio", 0.7))
    window_factor = float(settings.get("window_factor", 2.0))
    min_window_s = int(settings.get("min_window_s", 900))

    rows = {row["source_id"]: row for row in await db.health_all()}
    checked = [rows.get(source.id) for source in sources]
    if not any(row and (row.get("last_checked") or row.get("last_ok")) for row in checked):
        return Observability(total=total)

    moment = int(now if now is not None else now_ts())
    healthy_ids: set[str] = set()
    down: list[tuple[str, int | None]] = []
    for source in sources:
        row = rows.get(source.id) or {}
        last_ok = int(row.get("last_ok") or 0)
        failures = int(row.get("consecutive_failures") or 0)
        interval = int(getattr(source, "interval_s", 0) or 0) or 600
        window = max(window_factor * interval, min_window_s)
        if last_ok and failures == 0 and moment - last_ok <= window:
            healthy_ids.add(source.id)
        else:
            hours = None if not last_ok else max(0, (moment - last_ok) // 3600)
            down.append((source.id, hours))

    key_ids = list(settings.get("key_sources") or [])
    if not key_ids:
        key_ids = [source.id for source in sources if int(getattr(source, "tier", 3)) == 0]
    enabled_ids = {source.id for source in sources}
    key_down = [source_id for source_id in key_ids
                if source_id in enabled_ids and source_id not in healthy_ids]

    ratio = len(healthy_ids) / total
    if ratio < degraded_ratio or len(key_down) >= 3:
        state = STATE_BLIND
    elif ratio < full_ratio or key_down:
        state = STATE_DEGRADED
    else:
        state = STATE_FULL
    return Observability(
        total=total, healthy=len(healthy_ids), ratio=ratio, state=state,
        down=down, key_down=key_down,
    )
