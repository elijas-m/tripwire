"""Stage 4 tests: market shock, Pentagon Pizza Index, PPI multiplier cap."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from tripwire.config import Config
from tripwire.db import DB
from tripwire.indicators import compute_market_shock, market_summary
from tripwire.parsers import parse_ppi

CFG = Config(Path(__file__).resolve().parent.parent).load()
MARKET_TH = CFG.thresholds.get("market_shock") or {}


def tg_post(text: str, post: str = "PentagonPizzaReport/100") -> str:
    return (
        f'<div class="tgme_widget_message" data-post="{post}">'
        '<time datetime="2026-10-24T01:00:00+00:00"></time>'
        f'<div class="tgme_widget_message_text">{text}</div>'
        "</div>"
    )


class MarketShockTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.tmp.name) / "t.db")
        await self.db.open()

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.tmp.cleanup()

    async def test_brent_surge_sets_anomaly(self) -> None:
        now = int(time.time())
        await self.db.market_add("ICEEUR:BRN1!", now - 25 * 3600, 100.0, None, "tradingview")
        await self.db.market_add("ICEEUR:BRN1!", now, 108.0, 8.0, "tradingview")
        result = await compute_market_shock(self.db, now, MARKET_TH)
        self.assertEqual(result["value"], "ANOMALY")
        self.assertAlmostEqual(result["brent_pct"], 8.0, places=1)
        stored = await self.db.indicator_get("market_shock")
        self.assertEqual(stored["value"], "ANOMALY")

    async def test_flat_market_is_normal(self) -> None:
        now = int(time.time())
        await self.db.market_add("ICEEUR:BRN1!", now - 25 * 3600, 100.0, None, "tradingview")
        await self.db.market_add("ICEEUR:BRN1!", now, 101.0, 1.0, "tradingview")
        result = await compute_market_shock(self.db, now, MARKET_TH)
        self.assertEqual(result["value"], "NORMAL")
        self.assertEqual(result["triggers"], [])

    async def test_high_vix_sets_anomaly(self) -> None:
        now = int(time.time())
        await self.db.market_add("TVC:VIX", now, 41.0, 12.0, "tradingview")
        result = await compute_market_shock(self.db, now, MARKET_TH)
        self.assertEqual(result["value"], "ANOMALY")
        self.assertIn("VIX 41.0", result["triggers"])

    async def _seed(self, symbol: str, base: float, latest: float, now: int) -> None:
        await self.db.market_add(symbol, now - 24 * 3600 - 600, base, None, "tradingview")
        await self.db.market_add(symbol, now, latest, None, "tradingview")

    async def test_gold_rally_alone_sets_anomaly(self) -> None:
        now = int(time.time())
        await self._seed("TVC:GOLD", 100.0, 103.5, now)
        result = await compute_market_shock(self.db, now, MARKET_TH)
        self.assertEqual(result["value"], "ANOMALY")
        self.assertIn("Gold +3.5%/24h", result["triggers"])

    async def test_gold_drop_alone_is_normal(self) -> None:
        now = int(time.time())
        await self._seed("TVC:GOLD", 100.0, 96.5, now)
        result = await compute_market_shock(self.db, now, MARKET_TH)
        self.assertEqual(result["value"], "NORMAL")
        self.assertEqual(result["triggers"], [])
        self.assertAlmostEqual(result["tvc:gold_pct"], -3.5, places=1)

    async def test_gold_drop_with_vix_rise_sets_anomaly(self) -> None:
        now = int(time.time())
        await self._seed("TVC:GOLD", 100.0, 96.5, now)
        await self._seed("TVC:VIX", 20.0, 25.0, now)
        result = await compute_market_shock(self.db, now, MARKET_TH)
        self.assertEqual(result["value"], "ANOMALY")
        self.assertIn("Gold -3.5%/24h (VIX +25.0%)", result["triggers"])

    async def test_silver_drop_alone_is_normal(self) -> None:
        now = int(time.time())
        await self._seed("TVC:SILVER", 50.0, 46.0, now)
        result = await compute_market_shock(self.db, now, MARKET_TH)
        self.assertEqual(result["value"], "NORMAL")
        self.assertEqual(result["triggers"], [])

    async def test_silver_drop_with_vix_rise_sets_anomaly(self) -> None:
        now = int(time.time())
        await self._seed("TVC:SILVER", 50.0, 46.0, now)
        await self._seed("TVC:VIX", 20.0, 25.0, now)
        result = await compute_market_shock(self.db, now, MARKET_TH)
        self.assertEqual(result["value"], "ANOMALY")
        self.assertIn("Silver -8.0%/24h (VIX +25.0%)", result["triggers"])

    async def test_vix_confirm_threshold_comes_from_settings(self) -> None:
        now = int(time.time())
        await self._seed("TVC:GOLD", 100.0, 96.5, now)
        await self._seed("TVC:VIX", 20.0, 25.0, now)
        result = await compute_market_shock(
            self.db, now, {**MARKET_TH, "vix_confirm_pct": 50.0},
        )
        self.assertEqual(result["value"], "NORMAL")
        self.assertEqual(result["triggers"], [])


class MarketSummaryTests(unittest.TestCase):
    def test_summary_lists_known_moves(self) -> None:
        parts = market_summary(
            {"brent_pct": 2.3, "tvc:gold_pct": -3.3, "tvc:silver_pct": -6.2, "vix": 22.7}
        )
        self.assertEqual(parts, ["Brent +2.3%", "золото -3.3%", "серебро -6.2%"])

    def test_summary_without_data(self) -> None:
        self.assertEqual(market_summary(None), [])


class PizzaIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = CFG.sources_by_id["ppi_tg"]

    def test_anomaly_verdict(self) -> None:
        items = parse_ppi(
            self.source,
            tg_post("Pentagon Pizza Index: ANOMALY. Every target location is off the charts."),
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].kind, "indicator")
        self.assertEqual(items[0].payload["state"], "ANOMALY")

    def test_elevated_verdict(self) -> None:
        items = parse_ppi(
            self.source, tg_post("Pizza Index elevated — two locations busier than usual."))
        self.assertEqual(items[0].payload["state"], "ELEVATED")

    def test_normal_verdict(self) -> None:
        items = parse_ppi(self.source, tg_post("Pizza index: quiet, back to normal."))
        self.assertEqual(items[0].payload["state"], "NORMAL")

    def test_post_without_verdict_keeps_state(self) -> None:
        self.assertEqual(parse_ppi(self.source, tg_post("Happy Friday everyone!")), [])


if __name__ == "__main__":
    unittest.main()
