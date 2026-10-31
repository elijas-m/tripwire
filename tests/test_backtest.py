"""Backtest harness and calibration guards (§15)."""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tripwire.backtest import load_scenarios, run_backtest, _verdict, Scenario
from tripwire.config import Config
from tripwire.app import TripwireApp
from tripwire.util import now_ts, set_clock, utcnow


class CalibrationGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cfg = Config().load()
        cls.rules = {rule["id"]: rule for rule in cls.cfg.tripwires}

    def test_invasion_tripwire_matches_official_decision_wording(self) -> None:
        import re

        pattern = self.rules["TW-EU-INVASION"]["regex"]
        positive = [
            "I made a decision to carry out a special military operation.",
            "Принял решение о проведении специальной военной операции.",
            "The Kremlin said Russia launched a full-scale invasion of Ukraine.",
        ]
        negative = [
            "NATO exercise Steadfast Defender is a scheduled drill.",
            "Годовщина начала специальной военной операции: итоги.",
        ]
        for text in positive:
            self.assertTrue(re.search(pattern, text), text)
        negate = self.rules["TW-EU-INVASION"]["negate_regex"]
        for text in negative:
            self.assertTrue(re.search(negate, text), text)

    def test_nuclear_tripwire_ignores_risk_warnings(self) -> None:
        import re

        pattern = self.rules["TW-RU-NUCLEAR-USE"]["regex"]
        negate = self.rules["TW-RU-NUCLEAR-USE"]["negate_regex"]
        self.assertFalse(re.search(pattern, "Pentagon warns of risk of nuclear weapons use by Russia"))
        hypothetical = "Analysts discuss a possible nuclear attack scenario"
        self.assertTrue(re.search(pattern, hypothetical))
        self.assertIsNotNone(re.search(negate, hypothetical))
        confirmed = "Russia used nuclear weapons against military targets, officials confirmed."
        self.assertTrue(re.search(pattern, confirmed))
        self.assertIsNone(re.search(negate, confirmed))

    def test_recognition_is_escalation_but_not_war(self) -> None:
        import re

        terms = self.cfg.lexicon["terms"]
        self.assertIn("recognis* the independence", terms["en"]["S3"])
        self.assertIn("признани* Донецкой", terms["ru"]["S3"])
        self.assertNotIn("special military operation", terms["en"]["S2"])


class ScenarioCatalogTests(unittest.TestCase):
    def test_required_events_and_controls_present(self) -> None:
        scenarios = {sc.id: sc for sc in load_scenarios()}
        required = {
            "2022-02-24-invasion", "2022-02-21-recognition", "2023-10-07-hamas",
            "2024-04-13-iran-strike", "2019-09-14-abqaiq", "2020-01-30-pheic",
            "2020-03-11-pandemic", "2022-01-15-hunga-tonga", "2023-02-06-turkey-quake",
            "2026-06-israel-iran-stages",
        }
        controls = {key for key, sc in scenarios.items() if sc.control}
        self.assertTrue(required <= set(scenarios))
        self.assertGreaterEqual(len(controls), 9)
        self.assertEqual(scenarios["2022-02-24-invasion"].max_delay_min, 15)
        self.assertEqual(scenarios["2023-02-06-turkey-quake"].max_delay_min, 10)
        for scenario in scenarios.values():
            if scenario.control:
                continue
            self.assertIsNotNone(scenario.truth_at, scenario.id)

    def test_recognition_has_early_warning_lead(self) -> None:
        scenario = next(sc for sc in load_scenarios() if sc.id == "2022-02-21-recognition")
        self.assertIsNotNone(scenario.truth_at)
        self.assertGreaterEqual(scenario.min_lead_min or 0, 2880)

    def test_stages_parse(self) -> None:
        scenario = next(sc for sc in load_scenarios()
                        if sc.id == "2026-06-israel-iran-stages")
        self.assertEqual([stage.id for stage in scenario.stages],
                         ["escalation", "deescalation", "resumption-rumor",
                          "resumption-confirmed"])
        self.assertEqual(scenario.stages[1].max_level, 1)

    def test_clock_injection_roundtrip(self) -> None:
        fixed = datetime(2020, 1, 1, 12, 0, tzinfo=timezone.utc)
        set_clock(lambda: fixed)
        try:
            self.assertEqual(utcnow(), fixed)
            self.assertEqual(now_ts(), int(fixed.timestamp()))
        finally:
            set_clock(None)
        self.assertLess(abs(time.time() - now_ts()), 5)

    def test_verdict_logic(self) -> None:
        control = Scenario("c", "c", "WATCH", None, True, "", [])
        ok, _ = _verdict(control, {"max_level": 1, "delay_min": 0})
        self.assertTrue(ok)
        ok, _ = _verdict(control, {"max_level": 2, "delay_min": 0})
        self.assertFalse(ok)
        critical = Scenario("e", "e", "CRITICAL", 15, False, "", [])
        ok, _ = _verdict(critical, {"max_level": 3, "delay_min": 4})
        self.assertTrue(ok)
        ok, _ = _verdict(critical, {"max_level": 3, "delay_min": 20})
        self.assertFalse(ok)


class BacktestReplayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.cfg = Config().load()
        self.app = TripwireApp(self.cfg)
        await self.app.start()

    async def asyncTearDown(self) -> None:
        await self.app.close()

    async def test_turkey_and_pentagon_control(self) -> None:
        report = await run_backtest(
            self.app, only=["2023-02-06-turkey-quake", "control-pentagon-warns"]
        )
        results = {item["id"]: item for item in report["results"]}
        self.assertEqual(report["passed"], 2)
        self.assertGreaterEqual(results["2023-02-06-turkey-quake"]["max_level"], 3)
        self.assertLessEqual(results["control-pentagon-warns"]["max_level"], 1)
        # Point-in-time: the replay keeps the historical clock, not "one minute ago".
        first = results["2023-02-06-turkey-quake"]["steps"][0]
        self.assertEqual(first["at"], datetime(2023, 2, 6, 1, 17, 34,
                                               tzinfo=timezone.utc))

    async def test_point_in_time_metrics_and_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            export = Path(tmp) / "report.json"
            report = await run_backtest(
                self.app,
                only=["2022-02-24-invasion", "2022-02-21-recognition",
                      "control-false-evacuation", "2026-06-israel-iran-stages"],
                export=export,
            )
            self.assertEqual(report["passed"], 4)
            results = {item["id"]: item for item in report["results"]}
            self.assertGreaterEqual(
                results["2022-02-21-recognition"]["lead_min"] or 0, 2880)
            self.assertEqual(results["2022-02-24-invasion"]["detection_min"], 0)
            self.assertLessEqual(results["control-false-evacuation"]["max_level"], 1)
            stages = {item["id"]: item for item in
                      results["2026-06-israel-iran-stages"]["stages"]}
            self.assertTrue(all(item["ok"] for item in stages.values()))
            self.assertIn("≤ WATCH", stages["deescalation"]["detail"])
            self.assertEqual(report["metrics"]["false_alarms"], 0)
            self.assertEqual(report["metrics"]["stage_failures"], 0)
            self.assertTrue(export.exists())
            payload = json.loads(export.read_text(encoding="utf-8"))
            self.assertEqual(payload["metrics"]["misses"], 0)
            exported = {item["id"]: item for item in payload["results"]}
            self.assertEqual(exported["2022-02-24-invasion"]["steps"][0]["at"][:10],
                             "2022-02-24")

    async def test_clock_is_reset_after_replay(self) -> None:
        await run_backtest(self.app, only=["2023-02-06-turkey-quake"])
        self.assertLess(abs(time.time() - now_ts()), 5)


if __name__ == "__main__":
    unittest.main()
