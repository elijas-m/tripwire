"""Historical replay acceptance backtest (§15)."""

from __future__ import annotations

import logging
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from .models import L1, L2, L3, LEVEL_NAMES, RawItem
from .util import domain_of, utcnow

log = logging.getLogger(__name__)

BACKTEST_DIR = Path(__file__).resolve().parent.parent / "backtest"
LEVEL_BY_NAME = {"WATCH": L1, "WARNING": L2, "CRITICAL": L3}


@dataclass(slots=True)
class ReplayEvent:
    at: datetime
    source: str
    title: str
    body: str = ""
    category: str = ""
    theater: str = ""
    country: str | None = None
    lat: float | None = None
    lon: float | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    url: str = ""
    origin_domain: str = ""
    tier0: bool = False


@dataclass(slots=True)
class Scenario:
    id: str
    name: str
    expect: str
    max_delay_min: float | None
    control: bool
    provenance: str
    events: list[ReplayEvent]


def load_scenarios(directory: Path | None = None) -> list[Scenario]:
    path = directory or BACKTEST_DIR
    scenarios: list[Scenario] = []
    for file in sorted(path.glob("*.yaml")):
        data = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
        events = []
        for raw in data.get("events") or []:
            at = raw["at"]
            if isinstance(at, str):
                at = datetime.fromisoformat(at.replace("Z", "+00:00"))
            events.append(ReplayEvent(
                at=at.astimezone(timezone.utc),
                source=str(raw["source"]),
                title=str(raw.get("title") or ""),
                body=str(raw.get("body") or ""),
                category=str(raw.get("category") or ""),
                theater=str(raw.get("theater") or ""),
                country=raw.get("country"),
                lat=raw.get("lat"),
                lon=raw.get("lon"),
                payload=dict(raw.get("payload") or {}),
                url=str(raw.get("url") or ""),
                origin_domain=str(raw.get("origin_domain") or ""),
                tier0=bool(raw.get("tier0")),
            ))
        scenarios.append(Scenario(
            id=str(data.get("id") or file.stem),
            name=str(data.get("name") or file.stem),
            expect=str(data.get("expect") or "CRITICAL").upper(),
            max_delay_min=(float(data["max_delay_min"])
                           if data.get("max_delay_min") is not None else None),
            control=bool(data.get("control")),
            provenance=str(data.get("provenance") or ""),
            events=events,
        ))
    return scenarios


def _shift(events: list[ReplayEvent], now: datetime) -> list[ReplayEvent]:
    """Move the scenario so its last event is ~1 min ago; keep relative timing."""
    latest = max(ev.at for ev in events)
    offset = (now - timedelta(seconds=60)) - latest
    shifted = []
    for ev in events:
        copy = ReplayEvent(**{name: getattr(ev, name) for name in ReplayEvent.__slots__})
        copy.at = ev.at + offset
        shifted.append(copy)
    return shifted


async def _replay(app, scenario: Scenario) -> dict[str, Any]:
    from .app import TripwireApp

    tempdir = tempfile.mkdtemp(prefix="tripwire-backtest-")
    previous_override = app.cfg.data_dir_override
    app.cfg.data_dir_override = tempdir
    tester = None
    run_id = uuid.uuid4().hex[:8]
    steps: list[dict[str, Any]] = []
    try:
        tester = TripwireApp(app.cfg, commands=False)
        await tester.start()
        tester.alerts.silent = True
        events = _shift(scenario.events, utcnow())
        first_tier0 = next((ev.at for ev in events if ev.tier0), events[0].at)
        for index, ev in enumerate(events):
            source = app.cfg.sources_by_id.get(ev.source)
            if source is None:
                steps.append({"at": ev.at, "source": ev.source, "level": -1,
                              "error": f"нет источника {ev.source}"})
                continue
            origin = ev.origin_domain or domain_of(ev.url) or domain_of(source.url)
            raw = RawItem(
                source_id=source.id,
                title=ev.title,
                body=ev.body,
                url=ev.url or f"https://replay.tripwire.invalid/{run_id}/{index}",
                published_at=ev.at,
                payload=dict(ev.payload),
                guid=f"replay-{run_id}-{index}",
                origin_domain=origin,
                category=ev.category or None,
                theater=ev.theater or None,
                country=ev.country,
                lat=ev.lat,
                lon=ev.lon,
            )
            outcome = await tester.process(raw, source)
            alert = (outcome or {}).get("alert") or {}
            steps.append({
                "at": ev.at,
                "source": ev.source,
                "title": ev.title[:100],
                "kind": outcome.get("kind"),
                "action": alert.get("action"),
                "level": int(alert.get("level") or 0),
                "score": None,
            })

        alerts = await tester.db.alerts_since(0)
        for row in alerts:
            if row["level"] >= L2:
                steps.append({
                    "at": datetime.fromtimestamp(int(row["sent_at"]), timezone.utc),
                    "source": "alert",
                    "title": f"alert #{row['id']} {LEVEL_NAMES.get(int(row['level']))}",
                    "kind": "alert", "action": row["status"],
                    "level": int(row["level"]), "score": None,
                })
        state = await tester.correlator.snapshot()
    finally:
        if tester is not None:
            try:
                await tester.close()
            except Exception:
                log.exception("backtest app shutdown failed")
        app.cfg.data_dir_override = previous_override
        shutil.rmtree(tempdir, ignore_errors=True)

    max_level = max((step["level"] for step in steps if step["level"] >= 0), default=0)
    tripwires = sorted({row["tripwire_id"] for row in alerts if row["tripwire_id"]})
    reached_at = min(
        (step["at"] for step in steps
         if step["kind"] != "alert" and step["level"] >= max_level),
        default=None,
    )
    delay = None
    if reached_at is not None:
        delay = round((reached_at - first_tier0).total_seconds() / 60.0, 1)
    return {
        "id": scenario.id, "name": scenario.name, "expect": scenario.expect,
        "control": scenario.control, "max_delay_min": scenario.max_delay_min,
        "max_level": max_level, "delay_min": delay,
        "tripwires": tripwires, "steps": steps,
        "theaters": {row["theater"]: round(float(row["score"]), 1) for row in state},
    }


def _verdict(scenario: Scenario, result: dict[str, Any]) -> tuple[bool, str]:
    observed = int(result["max_level"])
    if scenario.control:
        ok = observed <= L1
        return ok, ("≤ WATCH" if ok else f"{LEVEL_NAMES.get(observed)} > WATCH")
    expected = LEVEL_BY_NAME.get(scenario.expect, L3)
    if observed < expected:
        return False, f"{LEVEL_NAMES.get(observed, 'NOISE')} < {scenario.expect}"
    if scenario.max_delay_min is not None:
        delay = result["delay_min"]
        if delay is None or delay > scenario.max_delay_min:
            return False, f"задержка {delay} мин > {scenario.max_delay_min:g}"
    return True, "OK"


async def run_backtest(app, only: list[str] | None = None,
                       directory: Path | None = None) -> dict[str, Any]:
    wanted = {item.lower() for item in (only or []) if item}
    scenarios = [sc for sc in load_scenarios(directory)
                 if not wanted or sc.id.lower() in wanted]
    results = []
    for scenario in scenarios:
        try:
            result = await _replay(app, scenario)
        except Exception as exc:
            log.exception("backtest scenario %s crashed", scenario.id)
            result = {"id": scenario.id, "name": scenario.name, "expect": scenario.expect,
                      "control": scenario.control, "max_delay_min": scenario.max_delay_min,
                      "max_level": -1, "delay_min": None, "tripwires": [], "steps": [],
                      "theaters": {}, "error": str(exc)[:200]}
        ok, detail = _verdict(scenario, result)
        result["ok"] = ok
        result["detail"] = detail
        results.append(result)
        log.info("backtest %-28s %s %s", scenario.id,
                 LEVEL_NAMES.get(int(result["max_level"]), "—"), detail)
    passed = sum(1 for item in results if item["ok"])
    lines = [f"Бэктест: {passed}/{len(results)} сценариев прошли", ""]
    for item in results:
        icon = "✅" if item["ok"] else "❌"
        delay = item.get("delay_min")
        delay_text = f", {delay:g} мин" if delay is not None else ""
        lines.append(f"{icon} {item['id']} — {LEVEL_NAMES.get(int(item['max_level']), '—')}"
                     f"{delay_text} — {item['detail']}")
        if item.get("tripwires"):
            lines.append(f"    tripwire: {', '.join(item['tripwires'])}")
    for item in results:
        if item["ok"]:
            continue
        lines.append("")
        lines.append(f"—— {item['id']}: {item['name']}")
        for step in item["steps"]:
            if step["level"] >= 0:
                lines.append(
                    f"  {step['at'].strftime('%d.%m %H:%M')} {step['source']:<22} "
                    f"{LEVEL_NAMES.get(step['level'], '—'):<8} {step.get('action') or step.get('kind') or ''} "
                    f"{(step.get('title') or '')[:70]}"
                )
    return {"results": results, "passed": passed, "summary": "\n".join(lines)[:12000]}
