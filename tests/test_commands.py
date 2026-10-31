"""Stage 5 tests: threshold overrides, mute, /why rendering, selftest isolation."""

from __future__ import annotations

import os
import tempfile
import time
import unittest

from tripwire.app import TripwireApp
from tripwire.commands import CommandService, format_status_full, format_why, theater_arg
from tripwire.correlator import Correlator
from tripwire.models import RawItem
from tripwire.selftest import run_selftest
from tripwire.util import utcnow


class CommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_env = os.environ.get("TRIPWIRE_DATA_DIR")
        os.environ["TRIPWIRE_DATA_DIR"] = self.tmp.name
        self.app = TripwireApp()
        await self.app.start()

    async def asyncTearDown(self) -> None:
        await self.app.close()
        if self.old_env is None:
            os.environ.pop("TRIPWIRE_DATA_DIR", None)
        else:
            os.environ["TRIPWIRE_DATA_DIR"] = self.old_env
        self.tmp.cleanup()

    async def _warning_event(self, suffix: str) -> str:
        source = self.app.sources["who_don_odata"]
        if suffix == "b":
            title = "Nipah virus outbreak spreads across Bangladesh, India and Nepal"
            body = ("The Nipah virus outbreak has expanded across three countries with 120 "
                    "confirmed cases and 74 deaths (case fatality ratio 61%). "
                    "Human-to-human transmission was confirmed.")
        else:
            title = ("Ebola disease caused by Bundibugyo virus in the Democratic Republic "
                     f"of the Congo, Kenya and Nigeria {suffix}")
            body = ("The Bundibugyo virus disease outbreak has expanded in the Democratic "
                    "Republic of the Congo, with Kenya and Nigeria reporting imported cases. "
                    "A total of 9704 confirmed cases (crude case fatality ratio: 48.3%) were "
                    "reported.")
        outcome = await self.app.process(RawItem(
            source_id="who_don_odata", title=title, body=body,
            url=f"https://who.int/don-{suffix}", published_at=utcnow(),
            category="pandemic", guid=f"don-{suffix}",
        ), source)
        return ((outcome or {}).get("alert") or {}).get("action") or ""

    async def test_threshold_override_applies_and_persists(self) -> None:
        correlator = Correlator(self.app.cfg, self.app.db)
        await correlator.load_overrides()
        self.assertEqual(correlator._required_level(9.0, "EU_RU"), 2)
        result = await correlator.set_override("EU_RU", "warning", 12.0, 1.0)
        self.assertEqual(result["levels"]["warning"], 12.0)
        self.assertEqual(correlator._required_level(9.0, "EU_RU"), 1)

        reloaded = Correlator(self.app.cfg, self.app.db)
        await reloaded.load_overrides()
        self.assertEqual(reloaded.effective_levels("EU_RU")["warning"], 12.0)

    async def test_expired_threshold_override_is_ignored(self) -> None:
        correlator = Correlator(self.app.cfg, self.app.db)
        await correlator.set_override("GULF", "critical", 25.0, 1.0)
        correlator._overrides["GULF"]["critical"] = (25.0, int(time.time()) - 5)
        self.assertEqual(
            correlator.effective_levels("GULF")["critical"],
            float(self.app.cfg.thresholds["levels"]["critical"]),
        )

    async def test_mute_suppresses_warning_but_not_critical(self) -> None:
        first = await self._warning_event("a")
        self.assertEqual(first, "dry_run")

        await self.app.db.theater_mute("PANDEMIC", int(time.time()) + 3600)
        second = await self._warning_event("b")
        self.assertEqual(second, "muted")

        source = self.app.sources["usgs_sig_hour"]
        outcome = await self.app.process(RawItem(
            source_id="usgs_sig_hour",
            title="M 7.9 - offshore earthquake",
            body="Significant earthquake reported by USGS.",
            url="https://usgs.gov/selftest-mute", published_at=utcnow(),
            category="earthquake", theater="NATURE", country="TR",
            lat=37.0, lon=37.0,
            payload={"mag": 7.9, "depth": 10.0, "alert": "red"},
            guid="selftest-mute-critical",
        ), source)
        action = ((outcome or {}).get("alert") or {}).get("action")
        self.assertEqual(action, "dry_run")

    async def test_why_renderer_contains_breakdown(self) -> None:
        await self._warning_event("why")
        alert = await self.app.db.latest_alert()
        self.assertIsNotNone(alert)
        explain = __import__("json").loads(alert["explain"])
        text = format_why(alert, explain, self.app.cfg)
        self.assertIn("/why", text)
        self.assertIn("Вклад источников", text)
        self.assertIn("Счёт:", text)
        self.assertIn("Факторы", text)

    async def test_status_renderer(self) -> None:
        text = await format_status_full(self.app)
        self.assertIn("STATUS", text)
        self.assertIn("Источники:", text)

    async def test_dispatch_threshold_mute_and_ack(self) -> None:
        service = CommandService(self.app)
        text = await service.dispatch("threshold", ["gulf", "warning", "10"])
        self.assertIn("Залив", text)
        self.assertEqual(
            self.app.correlator.effective_levels("GULF")["warning"], 10.0
        )
        journal = await self.app.db.journal_recent()
        self.assertEqual(journal[0]["action"], "threshold")

        text = await service.dispatch("mute", ["israel", "2"])
        self.assertIn("CRITICAL", text)
        state = await self.app.db.theater_get("ISRAEL")
        self.assertGreater(int(state["muted_until"]), int(time.time()))
        journal = await self.app.db.journal_recent()
        self.assertEqual(journal[0]["action"], "mute")

        await self._warning_event("ack")
        reply = await service.dispatch("ack", [])
        self.assertIn("Подтверждён", reply)
        self.assertIsNotNone((await self.app.db.latest_alert())["ack_at"])

    async def test_dispatch_rejects_bad_threshold(self) -> None:
        service = CommandService(self.app)
        with self.assertRaises(ValueError):
            await service.dispatch("threshold", ["gulf", "warning", "20"])
        with self.assertRaises(ValueError):
            await service.dispatch("threshold", ["atlantis", "warning", "9"])

    async def test_dispatch_unknown_command(self) -> None:
        service = CommandService(self.app)
        text = await service.dispatch("nope", [])
        self.assertIn("Неизвестная команда", text)

    async def test_selftest_isolated_from_production_db(self) -> None:
        report = await run_selftest(self.app, only=["TW-RU-NUCLEAR-USE"])
        self.assertTrue(report["results"])
        self.assertTrue(all(item["ok"] for item in report["results"]))
        self.assertFalse(report["delivered"])
        self.assertIsNone(await self.app.db.latest_alert())

    def test_theater_aliases(self) -> None:
        self.assertEqual(theater_arg("Израиль"), "ISRAEL")
        self.assertEqual(theater_arg("gulf"), "GULF")
        self.assertIsNone(theater_arg("atlantis"))


if __name__ == "__main__":
    unittest.main()
