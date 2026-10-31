"""Synthetic end-to-end test per tripwire family (§11.4, --selftest)."""

from __future__ import annotations

import logging
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .models import LEVEL_NAMES, L3, RawItem
from .util import utcnow

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Fixture:
    tripwire_id: str
    source_id: str
    title: str
    body: str
    category: str
    theater: str
    payload: dict[str, Any] = field(default_factory=dict)
    country: str | None = None
    lat: float | None = None
    lon: float | None = None
    expected_level: int = L3


FIXTURES: list[Fixture] = [
    Fixture(
        tripwire_id="TW-RU-NUCLEAR-USE",
        source_id="kremlin_events",
        title="Putin orders tactical nuclear weapons use against Ukraine",
        body=("The Russian president ordered the use of tactical nuclear weapons "
              "against military targets in Ukraine, the Kremlin said."),
        category="eu_ru", theater="EU_RU", country="RU", lat=55.75, lon=37.62,
    ),
    Fixture(
        tripwire_id="TW-PAN-PHEIC",
        source_id="who_news_rss",
        title="WHO declares public health emergency of international concern over new virus",
        body=("The WHO Director-General declared a public health emergency of international "
              "concern (PHEIC) following the outbreak of a novel virus."),
        category="pandemic", theater="PANDEMIC",
    ),
    Fixture(
        tripwire_id="TW-IL-IRAN-STRIKE",
        source_id="idf_tg",
        title="Israel struck Iran overnight",
        body=("The IDF confirmed that Israel struck Iranian military sites in retaliation "
              "for the missile attack."),
        category="israel", theater="ISRAEL", country="IL", lat=31.77, lon=35.21,
    ),
    Fixture(
        tripwire_id="TW-GULF-HORMUZ",
        source_id="ukmto_jina",
        title="Strait of Hormuz closed after mining incident",
        body="Shipping traffic was halted after the Strait of Hormuz was closed by mining.",
        category="gulf", theater="GULF", country="IR", lat=26.57, lon=56.25,
    ),
    Fixture(
        tripwire_id="TW-NAT-M75",
        source_id="usgs_sig_hour",
        title="M 7.8 - 30 km SSE of Kahramanmaras, Turkey",
        body="Significant earthquake reported by USGS.",
        category="earthquake", theater="NATURE", country="TR", lat=37.58, lon=36.93,
        payload={"mag": 7.8, "depth": 20.0, "alert": "red", "place": "Turkey"},
    ),
    Fixture(
        tripwire_id="TW-NAT-TSUNAMI-WARNING",
        source_id="noaa_ptwc_atom",
        title="Tsunami Warning issued for the Pacific coasts",
        body="A tsunami warning is in effect for coastal areas following the earthquake.",
        category="tsunami", theater="NATURE",
    ),
    Fixture(
        tripwire_id="TW-NAT-VOLCANO-RED",
        source_id="vaac_washington",
        title="Aviation colour code RED for Raung volcano",
        body="The volcano aviation colour code was raised to RED after an eruption.",
        category="volcano", theater="NATURE", country="ID", lat=-8.12, lon=114.04,
        payload={"colour_code": "red", "volcano_name": "Raung"},
    ),
]


async def run_selftest(app, only: list[str] | None = None, deliver: bool = True) -> dict[str, Any]:
    """Run fixtures through an isolated pipeline and report delivery to the TEST topic."""
    from .app import TripwireApp

    wanted = {item.upper() for item in (only or []) if item}
    fixtures = [item for item in FIXTURES if not wanted or item.tripwire_id in wanted]
    if not fixtures:
        return {"results": [], "summary": "нет подходящих фикстур", "delivered": False}

    run_id = uuid.uuid4().hex[:8]
    tempdir = tempfile.mkdtemp(prefix="tripwire-selftest-")
    previous_override = app.cfg.data_dir_override
    app.cfg.data_dir_override = tempdir
    tester = None
    results: list[dict[str, Any]] = []
    try:
        tester = TripwireApp(app.cfg, commands=False)
        await tester.start()
        tester.alerts.route_override = app.cfg.channel_topic("TEST")
        tester.alerts.test_mode = True
        for fixture in fixtures:
            source = app.cfg.sources_by_id.get(fixture.source_id)
            if source is None:
                results.append({
                    "tripwire": fixture.tripwire_id, "ok": False, "action": "missing-source",
                    "level": 0, "detail": f"нет источника {fixture.source_id}",
                })
                continue
            raw = RawItem(
                source_id=source.id,
                title=fixture.title,
                body=fixture.body,
                url=f"https://selftest.tripwire.invalid/{run_id}/{fixture.tripwire_id}",
                published_at=utcnow(),
                payload=dict(fixture.payload),
                guid=f"selftest-{run_id}-{fixture.tripwire_id}",
                category=fixture.category,
                theater=fixture.theater,
                country=fixture.country,
                lat=fixture.lat,
                lon=fixture.lon,
            )
            try:
                outcome = await tester.process(raw, source)
            except Exception as exc:
                log.exception("selftest fixture %s crashed", fixture.tripwire_id)
                results.append({
                    "tripwire": fixture.tripwire_id, "ok": False, "action": "crash",
                    "level": 0, "detail": str(exc)[:160],
                })
                continue
            alert = (outcome or {}).get("alert") or {}
            action = str(alert.get("action") or outcome.get("kind") or "none")
            level = int(alert.get("level") or 0)
            ok = action in ("sent", "dry_run", "edited") and level >= fixture.expected_level
            results.append({
                "tripwire": fixture.tripwire_id, "ok": ok, "action": action, "level": level,
                "detail": "получен" if ok else f"ожидался {LEVEL_NAMES[fixture.expected_level]}",
            })

        passed = sum(1 for item in results if item["ok"])
        delivered = bool(tester.tg.configured)
        stamp = datetime.now().strftime("%d.%m %H:%M")
        lines = [f"🧪 <b>SELFTEST</b> · {stamp} · {passed}/{len(results)}", ""]
        for item in results:
            icon = "✅" if item["ok"] else "❌"
            lines.append(
                f"{icon} {item['tripwire']} — {LEVEL_NAMES.get(item['level'], '—')} "
                f"[{item['action']}]"
            )
            if not item["ok"]:
                lines.append(f"    {item['detail']}")
        lines.append("")
        lines.append("Режим: Telegram" if delivered else "Режим: dry-run (Telegram не настроен)")
        summary = "\n".join(lines)[:4096]
        if deliver and delivered:
            try:
                await tester.tg.send_message(
                    summary, app.cfg.channel_topic("TEST"), disable_notification=True
                )
            except Exception:
                log.exception("selftest summary delivery failed")
                delivered = False
        else:
            log.info("selftest summary (dry-run):\n%s", summary)
        # A manual run also counts as the weekly check.
        await app.db.meta_set("last_selftest", int(time.time()))
        return {"results": results, "summary": summary, "delivered": delivered}
    finally:
        if tester is not None:
            try:
                await tester.close()
            except Exception:
                log.exception("selftest app shutdown failed")
        app.cfg.data_dir_override = previous_override
        shutil.rmtree(tempdir, ignore_errors=True)
