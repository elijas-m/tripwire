"""Backtest harness and calibration guards (§15)."""

from __future__ import annotations

import asyncio
import unittest

from tripwire.backtest import load_scenarios, run_backtest, _verdict, Scenario
from tripwire.config import Config
from tripwire.app import TripwireApp


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
        }
        controls = {key for key, sc in scenarios.items() if sc.control}
        self.assertTrue(required <= set(scenarios))
        self.assertGreaterEqual(len(controls), 4)
        self.assertEqual(scenarios["2022-02-24-invasion"].max_delay_min, 15)
        self.assertEqual(scenarios["2023-02-06-turkey-quake"].max_delay_min, 10)

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


if __name__ == "__main__":
    unittest.main()
