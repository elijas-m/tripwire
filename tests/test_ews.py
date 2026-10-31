"""EWS dashboard parser, indicator rendering and level 4-5 alerts."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from tripwire.app import TripwireApp
from tripwire.config import Config
from tripwire.digest import build_digest
from tripwire.models import RawItem
from tripwire.parsers import parse_ews_dashboard
from tripwire.util import utcnow

CFG = Config(Path(__file__).resolve().parent.parent).load()


def dashboard(level: int = 4, alert: str = "alarm") -> str:
    return json.dumps({
        "mode": "configured",
        "cohort": {"cohortType": "business_jet", "sourceLabel": "Global public metadata + FAA"},
        "liveStatus": {"cadenceMinutes": 30, "concurrentCount": 1500},
        "current": {
            "asOf": "2026-10-30T12:00:00+00:00",
            "concurrentCount": 1500,
            "baselineMean": 900.0,
            "zScore": 4.25,
            "alertLevel": alert,
            "emergencyLevel": level,
        },
        "signals": {"composite": {
            "asOf": "2026-10-30T12:00:00+00:00",
            "actualConcurrentCount": 1500,
            "expectedConcurrentCount": 900.0,
            "sigmaShift": 4.25,
            "alertLevel": alert,
            "emergencyLevel": level,
        }},
        "liveAircraft": [{"hex": "abc123", "registration": "N1", "label": "550"}],
    })


class EwsParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = CFG.sources_by_id["ews_jets"]

    def test_full_json(self) -> None:
        items = parse_ews_dashboard(self.source, dashboard(4))
        self.assertEqual(len(items), 1)
        payload = items[0].payload
        self.assertEqual(payload["emergency_level"], 4)
        self.assertEqual(payload["state"], "ALARM")
        self.assertEqual(payload["z_score"], 4.25)
        self.assertEqual(payload["concurrent"], 1500)
        self.assertEqual(payload["expected"], 900)
        self.assertEqual(payload["cohort_type"], "business_jet")
        self.assertEqual(items[0].kind, "indicator")

    def test_truncated_prefix(self) -> None:
        text = dashboard(4)
        cut = text.index('"liveAircraft"') + 20
        items = parse_ews_dashboard(self.source, text[:cut])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].payload["emergency_level"], 4)
        self.assertEqual(items[0].payload["z_score"], 4.25)

    def test_prefix_without_composite(self) -> None:
        text = dashboard(5, "emergency")
        cut = text.index('"signals"')
        items = parse_ews_dashboard(self.source, text[:cut])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].payload["emergency_level"], 5)

    def test_malformed_returns_nothing(self) -> None:
        self.assertEqual(parse_ews_dashboard(self.source, '{"mode":"configured"'), [])
        self.assertEqual(parse_ews_dashboard(self.source, ""), [])


class FakeTelegram:
    configured = True

    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_message(self, text, thread_id=None, **kwargs) -> int:
        self.messages.append(text)
        return len(self.messages)

    async def close(self) -> None:
        return None


class EwsPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_env = os.environ.get("TRIPWIRE_DATA_DIR")
        os.environ["TRIPWIRE_DATA_DIR"] = self.tmp.name
        self.app = TripwireApp()
        await self.app.start()
        self.tg = FakeTelegram()
        self.app.tg = self.tg
        self.source = self.app.sources["ews_jets"]

    async def asyncTearDown(self) -> None:
        await self.app.close()
        if self.old_env is None:
            os.environ.pop("TRIPWIRE_DATA_DIR", None)
        else:
            os.environ["TRIPWIRE_DATA_DIR"] = self.old_env
        self.tmp.cleanup()

    def indicator(self, level: int, alert: str | None = None) -> RawItem:
        alert = alert or {0: "normal", 1: "normal", 2: "normal", 3: "elevated",
                          4: "alarm", 5: "emergency"}[level]
        return RawItem(
            source_id="ews_jets", title=f"EWS x: {level}/5", body="",
            url=f"https://example.org/ews/{level}", published_at=utcnow(),
            payload={"state": alert.upper(), "alert_level": alert,
                     "emergency_level": level, "z_score": 4.25, "concurrent": 1500,
                     "expected": 900, "as_of": "2026-10-30T12:00:00+00:00",
                     "cohort_type": "business_jet", "cohort_label": "бизнес-джеты"},
            guid=f"ews-{level}", kind="indicator",
        )

    async def test_indicator_stored_and_shown_in_digest(self) -> None:
        from tripwire.commands import format_status
        result = await self.app.process(self.indicator(3), self.source)
        self.assertEqual(result["value"], "ELEVATED")
        row = await self.app.db.indicator_get("ews_jets")
        self.assertEqual(row["meta"]["emergency_level"], 3)
        self.assertEqual(self.tg.messages, [])
        text = await build_digest(self.app)
        self.assertIn("Джеты 3/5", text)
        status = await format_status(self.app)
        self.assertIn("Джеты: <b>ELEVATED</b> 3/5", status)

    async def test_alerts_on_rise_to_four_then_five_only(self) -> None:
        await self.app.process(self.indicator(3), self.source)
        self.assertEqual(len(self.tg.messages), 0)
        await self.app.process(self.indicator(4), self.source)
        self.assertEqual(len(self.tg.messages), 1)
        self.assertIn("4/5", self.tg.messages[0])
        self.assertIn("тревога", self.tg.messages[0])
        await self.app.process(self.indicator(4), self.source)
        self.assertEqual(len(self.tg.messages), 1)
        await self.app.process(self.indicator(5), self.source)
        self.assertEqual(len(self.tg.messages), 2)
        self.assertIn("5/5", self.tg.messages[1])
        await self.app.process(self.indicator(5), self.source)
        self.assertEqual(len(self.tg.messages), 2)

    async def test_drop_to_normal_rearms_alert(self) -> None:
        await self.app.process(self.indicator(4), self.source)
        await self.app.process(self.indicator(2), self.source)
        await self.app.process(self.indicator(4), self.source)
        self.assertEqual(len(self.tg.messages), 2)

    async def test_stays_quiet_below_four(self) -> None:
        await self.app.process(self.indicator(3), self.source)
        row = await self.app.db.fetchone(
            "SELECT value FROM indicator_state WHERE name='ews_military'")
        self.assertIsNone(row)
        self.assertEqual(self.tg.messages, [])
