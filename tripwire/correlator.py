"""Theater correlator with exponential decay, independence, hysteresis."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .models import Cluster, Event, L0, L1, L2, L3, TripwireHit
from .util import from_ts, now_ts, publisher_domain


@dataclass(slots=True)
class TheaterDecision:
    theater: str
    score: float
    level: int
    previous_level: int
    cluster_level: int
    threshold: float
    sources: int
    trait_tier01: bool
    modifiers: dict[str, float] = field(default_factory=dict)
    changed: bool = False
    tripwire: str | None = None


class Correlator:
    def __init__(self, cfg, db) -> None:
        self.cfg = cfg
        self.db = db
        self.halflife = {k: float(v) for k, v in (cfg.thresholds.get("halflife_hours") or {}).items()}
        self.hys = cfg.thresholds.get("hysteresis", {})
        self.levels = cfg.thresholds.get("levels", {})
        self.independence = cfg.thresholds.get("independence", {})
        self.tz = ZoneInfo(cfg.app.get("timezone", "UTC"))
        self.night = cfg.app.get("night", {"start": 23, "end": 7})
        self.modifiers = cfg.thresholds.get("modifiers", {})
        self.relations = {
            str(k).upper(): {str(v).upper() for v in (vals or [])}
            for k, vals in (cfg.geofences.get("related_theaters") or {}).items()
        }
        self._lock = asyncio.Lock()
        # theater -> level name -> (value, expire_ts); temporary /threshold overrides
        self._overrides: dict[str, dict[str, tuple[float, int]]] = {}

    # ---------- scoring ----------
    def _decay(self, score: float, last_update: int | None, theater: str, now_ts: int) -> float:
        if not last_update or score <= 0:
            return score
        half = self.halflife.get(theater, 12.0)
        elapsed_h = max(0.0, (now_ts - last_update) / 3600.0)
        return score * (0.5 ** (elapsed_h / half))

    async def _independence(self, cluster: Cluster | None) -> tuple[int, bool]:
        if cluster is None or cluster.id is None:
            return 0, False
        rows = await self.db.cluster_origin_rows(cluster.id)
        domains: set[str] = set()
        tier01 = False
        for row in rows:
            sid = str(row.get("source_id") or "")
            origin = publisher_domain(row.get("origin_domain"), sid, self.cfg)
            if origin:
                domains.add(origin)
            tier = row.get("tier")
            if int(3 if tier is None else tier) <= 1:
                tier01 = True
        if not domains:
            # Legacy clusters created before events were stored with origins.
            for sid in await self.db.cluster_sources(cluster.id):
                src = self.cfg.sources_by_id.get(sid)
                if not src:
                    continue
                domains.add(publisher_domain("", sid, self.cfg))
                if src.tier <= 1:
                    tier01 = True
        return len(domains), tier01

    def _allowed_level(self, wanted: int, sources: int, tier01: bool, tripwire: bool) -> int:
        if tripwire:
            return wanted
        ind = self.independence
        if wanted >= L3:
            if sources >= int(ind.get("critical_sources", 3)) and (
                tier01 or int(ind.get("critical_min_tier01", 1)) == 0
            ):
                return L3
            wanted = L2
        if wanted >= L2:
            if sources >= int(ind.get("warning_sources", 2)) or tier01:
                return L2
            wanted = L1
        return max(wanted, L0)

    # ---------- temporary threshold overrides ----------
    async def load_overrides(self) -> None:
        self._overrides = {}
        now = now_ts()
        for theater in self.cfg.geofences.get("theaters", {}):
            raw = await self.db.meta_get(f"threshold_override:{theater}", {}) or {}
            active: dict[str, tuple[float, int]] = {}
            for name, item in (raw.get("levels") or {}).items():
                if name not in ("watch", "warning", "critical") or not isinstance(item, dict):
                    continue
                try:
                    value = float(item.get("value"))
                    until = int(item.get("until") or 0)
                except (TypeError, ValueError):
                    continue
                if until > now:
                    active[name] = (value, until)
            if active:
                self._overrides[theater] = active

    async def set_override(self, theater: str, level_name: str, value: float,
                           ttl_hours: float) -> dict[str, Any]:
        until = int(now_ts() + max(1.0, ttl_hours) * 3600)
        self._overrides.setdefault(theater, {})[level_name] = (float(value), until)
        levels = {
            name: {"value": val, "until": exp}
            for name, (val, exp) in self._overrides[theater].items()
        }
        await self.db.meta_set(f"threshold_override:{theater}", {"levels": levels})
        return {"theater": theater, "until": until, "levels": self.effective_levels(theater)}

    def effective_levels(self, theater: str) -> dict[str, float]:
        now = now_ts()
        out = {}
        for name in ("watch", "warning", "critical"):
            value = float(self.levels[name])
            entry = self._overrides.get(theater, {}).get(name)
            if entry and entry[1] > now:
                value = entry[0]
            out[name] = value
        return out

    def _required_level(self, score: float, theater: str) -> int:
        levels = self.effective_levels(theater)
        if score >= levels["critical"]:
            return L3
        if score >= levels["warning"]:
            return L2
        if score >= levels["watch"]:
            return L1
        return L0

    async def _modifiers(self, event: Event, theater: str) -> dict[str, float]:
        mods: dict[str, float] = {}
        if theater in ("EU_RU", "ISRAEL", "GULF", "MENA", "PANDEMIC"):
            shock = await self.db.indicator_get("market_shock")
            if shock and shock.get("value") == "ANOMALY":
                mods["market_shock"] = float(self.modifiers.get("market_shock", 1.25))
        if theater in ("EU_RU", "ISRAEL", "GULF", "MENA"):
            ppi = await self.db.indicator_get("ppi")
            if ppi and ppi.get("value") == "ANOMALY":
                mods["ppi"] = float(self.modifiers.get("ppi_anomaly", 1.15))
        if event.tier <= 1 and event.published_at and theater not in ("NATURE", "MARKETS"):
            local = event.published_at.astimezone(self.tz)
            start = int(self.night.get("start", 23))
            end = int(self.night.get("end", 7))
            is_night = local.hour >= start or local.hour < end
            if is_night:
                mods["night"] = float(self.modifiers.get("night", 1.2))
        related = self.relations.get(theater.upper(), set())
        others = 0
        for row in await self.db.theater_all():
            other = str(row["theater"] or "").upper()
            if other != theater and other in related and int(row["level"] or 0) >= L1:
                others += 1
        if others >= 1:
            mods["multi_theater"] = float(self.modifiers.get("multi_theater", 1.2))
        return mods

    async def add(self, event: Event, score: float, cluster: Cluster | None,
                  tripwire: TripwireHit | None = None) -> TheaterDecision:
        return await self._apply(event, score, cluster, tripwire)

    async def add_revision(self, event: Event, new_score: float, old_score: float,
                           cluster: Cluster | None,
                           tripwire: TripwireHit | None = None) -> TheaterDecision:
        """Revisions update in place: only the positive delta is added to the theater score."""
        return await self._apply(event, max(0.0, new_score - old_score), cluster, tripwire,
                                 revision=True)

    async def _apply(self, event: Event, score: float, cluster: Cluster | None,
                     tripwire: TripwireHit | None, revision: bool = False) -> TheaterDecision:
        async with self._lock:
            return await self._apply_locked(event, score, cluster, tripwire, revision)

    async def _apply_locked(self, event: Event, score: float, cluster: Cluster | None,
                            tripwire: TripwireHit | None,
                            revision: bool = False) -> TheaterDecision:
        now = now_ts()
        state = await self.db.theater_get(event.theater)
        prev_score = float(state.get("score") or 0.0)
        decayed = self._decay(prev_score, state.get("last_update"), event.theater, now)
        prev_level = int(state.get("level") or 0)

        mods = await self._modifiers(event, event.theater)
        mult = 1.0
        for value in mods.values():
            mult *= value
        contribution = score * mult
        tripwire_points: float | None = None
        if tripwire is not None:
            tripwire_points = float(
                self.levels["critical" if tripwire.level >= L3 else "warning"]
            )
            contribution = max(contribution, tripwire_points)
        new_score = decayed + contribution

        if cluster is not None:
            cluster.total_score = new_score
        sources, tier01 = await self._independence(cluster)
        levels = self.effective_levels(event.theater)
        # Modifiers are context: they may accelerate a signal, but they cannot
        # lift a theater to CRITICAL on their own.
        base_score = decayed + (tripwire_points if tripwire_points is not None else score)
        base_wanted = self._required_level(base_score, event.theater)
        wanted = self._required_level(new_score, event.theater)
        if wanted >= L3 and base_wanted < L3:
            wanted = L2
        if tripwire is not None:
            wanted = max(wanted, tripwire.level)
            if not tripwire.confirmed:
                # An unconfirmed hard rule may alert at WARNING, but it must not
                # create a new CRITICAL even when the theater score is near it.
                wanted = min(wanted, L2)
        allowed = self._allowed_level(wanted, sources, tier01, tripwire is not None)

        level = max(prev_level, allowed)
        level_since = state.get("level_since") or now
        if allowed < prev_level:
            level = await self._maybe_lower(event.theater, prev_level, new_score, state, now)
            if level < prev_level:
                level_since = now

        await self.db.theater_set(
            event.theater, new_score, level, level_since,
            cooldown_until=state.get("cooldown_until"),
            muted_until=state.get("muted_until"),
        )
        cluster_level = allowed
        if cluster is not None:
            cluster.level = max(cluster.level, cluster_level)
        return TheaterDecision(
            theater=event.theater, score=round(new_score, 2), level=level,
            previous_level=prev_level, cluster_level=cluster_level,
            threshold=float(levels["critical"] if level >= L3 else
                            levels["warning"] if level >= L2 else levels["watch"]),
            sources=sources, trait_tier01=tier01, modifiers=mods,
            changed=level != prev_level, tripwire=tripwire.id if tripwire else None,
        )

    async def _maybe_lower(self, theater: str, current: int, score: float, state: dict, now_ts: int) -> int:
        hours = float(self.hys.get("pandemic_hours" if theater == "PANDEMIC" else "hours", 6))
        factor = float(self.hys.get("down_factor", 0.6))
        levels = self.effective_levels(theater)
        threshold = float(levels["watch"] if current == L1 else
                          levels["warning"] if current == L2 else levels["critical"])
        if score >= threshold * factor:
            await self.db.meta_set(f"below_since:{theater}", "")
            return current
        below_since = await self.db.meta_get(f"below_since:{theater}") or None
        if not below_since:
            await self.db.meta_set(f"below_since:{theater}", now_ts)
            return current
        try:
            below_ts = int(below_since)
        except (TypeError, ValueError):
            below_ts = now_ts
        if now_ts - below_ts >= hours * 3600:
            new_level = current - 1
            await self.db.meta_set(f"below_since:{theater}", "")
            return new_level
        return current

    async def recompute(self) -> list[dict[str, Any]]:
        """Periodic decay and hysteresis check (every 15 min)."""
        async with self._lock:
            return await self._recompute_locked()

    async def _recompute_locked(self) -> list[dict[str, Any]]:
        now = now_ts()
        out = []
        for state in await self.db.theater_all():
            theater = state["theater"]
            score = self._decay(float(state.get("score") or 0), state.get("last_update"), theater, now)
            level = int(state.get("level") or 0)
            if level > 0:
                levels = self.effective_levels(theater)
                threshold = float(levels["watch"] if level == L1 else
                                  levels["warning"] if level == L2 else levels["critical"])
                if score < threshold * float(self.hys.get("down_factor", 0.6)):
                    level = await self._maybe_lower(theater, level, score, state, now)
            await self.db.theater_set(
                theater, score, level, state.get("level_since"),
                cooldown_until=state.get("cooldown_until"), muted_until=state.get("muted_until"),
            )
            out.append({"theater": theater, "score": round(score, 2), "level": level})
        return out

    async def snapshot(self) -> list[dict[str, Any]]:
        rows = await self.db.theater_all()
        now = now_ts()
        out = []
        for row in rows:
            score = self._decay(float(row.get("score") or 0), row.get("last_update"), row["theater"], now)
            out.append({
                "theater": row["theater"], "score": round(score, 2),
                "level": int(row.get("level") or 0),
                "level_since": from_ts(row.get("level_since")),
                "muted_until": from_ts(row.get("muted_until")),
                "cooldown_until": from_ts(row.get("cooldown_until")),
            })
        return out
