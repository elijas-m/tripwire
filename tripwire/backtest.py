"""Point-in-time historical replay backtest (§15).

Scenarios are replayed on their original timeline: the simulated clock of the
pipeline is set to each event's historical timestamp, so decay, dedup windows,
alert cooldowns and indicator state behave as they did at the time. Nothing
from the future is visible to the engine.
"""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .models import L1, L2, L3, LEVEL_NAMES, RawItem
from .util import domain_of, set_clock, utcnow

log = logging.getLogger(__name__)

BACKTEST_DIR = Path(__file__).resolve().parent.parent / "backtest"
LEVEL_BY_NAME = {"WATCH": L1, "WARNING": L2, "CRITICAL": L3}
DEFAULT_CONTROL_OBSERVATION_MIN = 30 * 24 * 60


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
class IndicatorStep:
    at: datetime
    id: str
    value: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Stage:
    id: str
    start: datetime
    end: datetime
    expect: str | None = None
    max_level: int | None = None
    truth_at: datetime | None = None
    max_delay_min: float | None = None


@dataclass(slots=True)
class Scenario:
    id: str
    name: str
    expect: str
    max_delay_min: float | None
    control: bool
    provenance: str
    events: list[ReplayEvent]
    truth_at: datetime | None = None
    min_lead_min: float | None = None
    observation_min: float | None = None
    indicators: list[IndicatorStep] = field(default_factory=list)
    stages: list[Stage] = field(default_factory=list)


def _parse_dt(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _float_or_none(value: Any) -> float | None:
    return float(value) if value is not None else None


def load_scenarios(directory: Path | None = None) -> list[Scenario]:
    path = directory or BACKTEST_DIR
    scenarios: list[Scenario] = []
    for file in sorted(path.glob("*.yaml")):
        data = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
        events = []
        for raw in data.get("events") or []:
            events.append(ReplayEvent(
                at=_parse_dt(raw["at"]),
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
        indicators = [
            IndicatorStep(
                at=_parse_dt(raw["at"]),
                id=str(raw["id"]),
                value=str(raw.get("value") or "NORMAL"),
                payload=dict(raw.get("payload") or {}),
            )
            for raw in data.get("indicators") or []
        ]
        stages = [
            Stage(
                id=str(raw.get("id") or f"stage-{index + 1}"),
                start=_parse_dt(raw["start"]),
                end=_parse_dt(raw["end"]),
                expect=(str(raw["expect"]).upper() if raw.get("expect") else None),
                max_level=(LEVEL_BY_NAME.get(str(raw["max_level"]).upper())
                           if raw.get("max_level") else None),
                truth_at=_parse_dt(raw.get("truth_at")),
                max_delay_min=_float_or_none(raw.get("max_delay_min")),
            )
            for index, raw in enumerate(data.get("stages") or [])
        ]
        scenarios.append(Scenario(
            id=str(data.get("id") or file.stem),
            name=str(data.get("name") or file.stem),
            expect=str(data.get("expect") or "CRITICAL").upper(),
            max_delay_min=_float_or_none(data.get("max_delay_min")),
            control=bool(data.get("control")),
            provenance=str(data.get("provenance") or ""),
            events=events,
            truth_at=_parse_dt(data.get("truth_at")),
            min_lead_min=_float_or_none(data.get("min_lead_min")),
            observation_min=_float_or_none(data.get("observation_min")),
            indicators=indicators,
            stages=stages,
        ))
    return scenarios


def _timeline(scenario: Scenario) -> list[tuple[datetime, str, Any]]:
    timeline = [(ev.at, "event", ev) for ev in scenario.events]
    timeline += [(step.at, "indicator", step) for step in scenario.indicators]
    timeline.sort(key=lambda item: item[0])
    return timeline


def _replay_event(ev: ReplayEvent, index: int, run_id: str, source) -> RawItem:
    origin = ev.origin_domain or domain_of(ev.url) or domain_of(source.url)
    return RawItem(
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


async def _replay(app, scenario: Scenario) -> dict[str, Any]:
    from .app import TripwireApp

    tempdir = tempfile.mkdtemp(prefix="tripwire-backtest-")
    previous_override = app.cfg.data_dir_override
    app.cfg.data_dir_override = tempdir
    tester = None
    run_id = uuid.uuid4().hex[:8]
    steps: list[dict[str, Any]] = []
    alerts: list[Any] = []
    try:
        tester = TripwireApp(app.cfg, commands=False)
        await tester.start()
        tester.alerts.silent = True
        for index, (at, kind, item) in enumerate(_timeline(scenario)):
            # Point-in-time: every pipeline timestamp resolution sees this event's time.
            set_clock(lambda at=at: at)
            if kind == "indicator":
                await tester.db.indicator_set(item.id, item.value, item.payload)
                steps.append({
                    "at": at, "source": "indicator", "kind": "indicator",
                    "title": f"{item.id}={item.value}", "action": None, "level": -1,
                })
                continue
            source = tester.cfg.sources_by_id.get(item.source)
            if source is None:
                steps.append({"at": at, "source": item.source, "level": -1,
                              "error": f"нет источника {item.source}"})
                continue
            raw = _replay_event(item, index, run_id, source)
            outcome = await tester.process(raw, source) or {}
            alert = outcome.get("alert") or {}
            steps.append({
                "at": at,
                "source": item.source,
                "title": item.title[:100],
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
        set_clock(None)
        if tester is not None:
            try:
                await tester.close()
            except Exception:
                log.exception("backtest app shutdown failed")
        app.cfg.data_dir_override = previous_override
        shutil.rmtree(tempdir, ignore_errors=True)

    event_steps = [step for step in steps if step["kind"] not in ("alert", "indicator")]
    max_level = max((step["level"] for step in event_steps if step["level"] >= 0), default=0)
    truth_at = scenario.truth_at
    if truth_at is None and scenario.events:
        truth_at = min(ev.at for ev in scenario.events)

    def first_at(level: int) -> datetime | None:
        return min((step["at"] for step in event_steps if step["level"] >= level), default=None)

    def relative_minute(at: datetime | None) -> float | None:
        if at is None or truth_at is None:
            return None
        return round((at - truth_at).total_seconds() / 60.0, 1)

    expected = LEVEL_BY_NAME.get(scenario.expect, L3)
    first_hit_at = first_at(L1)
    detected_at = first_at(expected) if not scenario.control else first_at(L2)
    detection_min = relative_minute(detected_at)
    observation_min = None
    if scenario.control:
        span = 0.0
        if scenario.events:
            span = (max(ev.at for ev in scenario.events)
                    - min(ev.at for ev in scenario.events)).total_seconds() / 60.0
        observation_min = scenario.observation_min or max(span, DEFAULT_CONTROL_OBSERVATION_MIN)

    tripwires = sorted({row["tripwire_id"] for row in alerts if row["tripwire_id"]})
    return {
        "id": scenario.id, "name": scenario.name, "expect": scenario.expect,
        "stages": _evaluate_stages(scenario, event_steps),
        "control": scenario.control,
        "max_delay_min": scenario.max_delay_min, "min_lead_min": scenario.min_lead_min,
        "truth_at": truth_at.isoformat() if truth_at else None,
        "observation_min": observation_min,
        "max_level": max_level,
        "first_hit_at": first_hit_at.isoformat() if first_hit_at else None,
        "first_hit_min": relative_minute(first_hit_at),
        "detected_at": detected_at.isoformat() if detected_at else None,
        "detection_min": detection_min,
        "lead_min": (-detection_min if detection_min is not None else None),
        "delay_min": detection_min,
        "miss": (not scenario.control and max_level < expected),
        "near_miss": (max_level == expected - 1 if not scenario.control
                      else max_level == L1),
        "false_alarm": (scenario.control and max_level >= L2),
        "tripwires": tripwires, "steps": steps,
        "theaters": {row["theater"]: round(float(row["score"]), 1) for row in state},
    }


def _evaluate_stages(scenario: Scenario, steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for stage in scenario.stages:
        inside = [step for step in steps if stage.start <= step["at"] <= stage.end]
        entry: dict[str, Any] = {"id": stage.id, "ok": True, "detail": ""}
        parts: list[str] = []
        if stage.expect:
            want = LEVEL_BY_NAME.get(stage.expect, L3)
            hits = [step for step in inside if int(step.get("level") or 0) >= want]
            if not hits:
                entry["ok"] = False
                parts.append(f"нет {stage.expect}")
            else:
                truth = stage.truth_at or stage.start
                delay = round((hits[0]["at"] - truth).total_seconds() / 60.0, 1)
                if stage.max_delay_min is not None and delay > stage.max_delay_min:
                    entry["ok"] = False
                    parts.append(f"{_format_timing(delay)} > {stage.max_delay_min:g} мин")
                else:
                    parts.append(f"{stage.expect}, {_format_timing(delay)}")
        if stage.max_level is not None:
            cards = [step for step in inside
                     if step.get("action") in ("sent", "dry_run")
                     and int(step.get("level") or 0) >= L2]
            bad = [step for step in cards if int(step["level"]) > stage.max_level]
            if bad:
                entry["ok"] = False
                parts.append(f"новая карточка {LEVEL_NAMES.get(int(bad[0]['level']))} "
                             f"> {LEVEL_NAMES.get(stage.max_level)}")
            else:
                parts.append(f"≤ {LEVEL_NAMES.get(stage.max_level)}")
        entry["detail"] = ", ".join(parts) or "OK"
        out.append(entry)
    return out


def _format_timing(minutes: float | None) -> str:
    if minutes is None:
        return "нет"
    if abs(minutes) < 1:
        return "в момент T0"
    if minutes < 0:
        return f"упреждение {-minutes:g} мин"
    return f"задержка {minutes:g} мин"


def _verdict(scenario: Scenario, result: dict[str, Any]) -> tuple[bool, str]:
    observed = int(result["max_level"])
    if scenario.control:
        ok = observed <= L1
        return ok, ("≤ WATCH" if ok else f"{LEVEL_NAMES.get(observed)} > WATCH")
    if scenario.stages:
        stages = result.get("stages") or []
        ok = bool(stages) and all(item["ok"] for item in stages)
        detail = "; ".join(f"{item['id']}: {item['detail']}" for item in stages)
        return ok, detail or "этапы не проверены"
    expected = LEVEL_BY_NAME.get(scenario.expect, L3)
    if observed < expected:
        return False, f"ПРОПУСК: {LEVEL_NAMES.get(observed, 'NOISE')} < {scenario.expect}"
    detection = result.get("detection_min", result.get("delay_min"))
    timing = _format_timing(detection)
    if scenario.max_delay_min is not None:
        if detection is None or detection > scenario.max_delay_min:
            return False, f"{timing} > {scenario.max_delay_min:g} мин"
    if scenario.min_lead_min is not None:
        lead = -detection if detection is not None else None
        if lead is None or lead < scenario.min_lead_min:
            return False, f"упреждение {lead if lead is not None else 'нет'} мин < {scenario.min_lead_min:g}"
    return True, f"OK, {timing}"


def _metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    events = [item for item in results if not item["control"]]
    controls = [item for item in results if item["control"]]
    detected = [item for item in events if not item.get("miss") and not item.get("error")]
    misses = [item for item in events if item.get("miss")]
    near_misses = [item for item in events if item.get("near_miss")]
    false_alarms = [item for item in controls if item.get("false_alarm")]
    control_near = [item for item in controls if item.get("near_miss")]
    stage_checks = sum(len(item.get("stages") or []) for item in results)
    stage_failures = sum(1 for item in results for stage in (item.get("stages") or [])
                         if not stage["ok"])
    observation_min = sum(float(item.get("observation_min") or 0) for item in controls)
    observation_days = observation_min / 1440.0
    per_30d = (len(false_alarms) / observation_days * 30.0) if observation_days else 0.0
    return {
        "scenarios": len(results),
        "passed": sum(1 for item in results if item.get("ok")),
        "event_scenarios": len(events),
        "detected": len(detected),
        "misses": len(misses),
        "miss_ids": [item["id"] for item in misses],
        "near_misses": len(near_misses),
        "near_miss_ids": [item["id"] for item in near_misses],
        "controls": len(controls),
        "false_alarms": len(false_alarms),
        "false_alarm_ids": [item["id"] for item in false_alarms],
        "control_near_misses": len(control_near),
        "control_near_miss_ids": [item["id"] for item in control_near],
        "stage_checks": stage_checks,
        "stage_failures": stage_failures,
        "observation_days": round(observation_days, 1),
        "false_alarms_per_30d": round(per_30d, 2),
        "lead_min": {
            item["id"]: item.get("lead_min")
            for item in detected if item.get("lead_min") is not None
        },
    }


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


async def run_backtest(app, only: list[str] | None = None,
                       directory: Path | None = None,
                       export: Path | None = None) -> dict[str, Any]:
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
                      "control": scenario.control,
                      "max_delay_min": scenario.max_delay_min,
                      "min_lead_min": scenario.min_lead_min,
                      "max_level": -1, "detection_min": None, "delay_min": None,
                      "lead_min": None, "miss": False, "near_miss": False,
                      "false_alarm": False, "observation_min": None,
                      "tripwires": [], "steps": [], "theaters": {},
                      "error": str(exc)[:200]}
        ok, detail = _verdict(scenario, result)
        result["ok"] = ok
        result["detail"] = detail
        results.append(result)
        log.info("backtest %-28s %s %s", scenario.id,
                 LEVEL_NAMES.get(int(result["max_level"]), "—"), detail)

    passed = sum(1 for item in results if item["ok"])
    metrics = _metrics(results)
    lines = [
        f"Point-in-time бэктест: {passed}/{len(results)} сценариев прошли",
        f"Обнаружение: {metrics['detected']}/{metrics['event_scenarios']}, "
        f"пропусков {metrics['misses']}, near-miss {metrics['near_misses']}",
        f"Ложные тревоги: {metrics['false_alarms']}/{metrics['controls']}, "
        f"{metrics['false_alarms_per_30d']:.2f} на 30 дней "
        f"(наблюдение {metrics['observation_days']:g} дн.)",
    ]
    if metrics["stage_checks"]:
        lines.append(
            f"Этапы: {metrics['stage_checks'] - metrics['stage_failures']}"
            f"/{metrics['stage_checks']} пройдено"
        )
    lines.append("")
    for item in results:
        icon = "✅" if item["ok"] else "❌"
        timing = item.get("first_hit_min")
        timing_text = f", первый сигнал {_format_timing(timing)}" if timing is not None else ""
        lines.append(f"{icon} {item['id']} — {LEVEL_NAMES.get(int(item['max_level']), '—')}"
                     f"{timing_text} — {item['detail']}")
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

    report = {"results": results, "passed": passed, "metrics": metrics,
              "summary": "\n".join(lines)[:12000]}
    if export is not None:
        payload = {
            "generated_at": utcnow().isoformat(),
            "metrics": _json_safe(metrics),
            "results": _json_safe(results),
        }
        export.parent.mkdir(parents=True, exist_ok=True)
        export.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("backtest report written to %s", export)
    return report
