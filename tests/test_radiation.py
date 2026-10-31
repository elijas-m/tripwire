"""Safecast radiation indicator: filtering, levels and display."""

from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from tripwire.config import Config
from tripwire.db import DB
from tripwire.models import Source
from tripwire.radiation import digest_parts, parse_safecast, status_lines, summarize
from tripwire.util import utcnow

CFG = Config(Path(__file__).resolve().parent.parent).load()
RAD_TH = CFG.thresholds.get("radiation") or {}


def sample(minutes_ago: float, value: float, device: str | None = "1") -> dict:
    ts = utcnow() - timedelta(minutes=minutes_ago)
    return {
        "captured_at": ts.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "value": value,
        "unit": "usv",
        "device_id": device,
    }


def baseline(device: str = "1", value: float = 0.10, count: int = 12) -> list[dict]:
    return [sample(5 + minutes, value, device) for minutes in range(count)]


class RadiationSummarizeTests(unittest.TestCase):
    def test_steady_background_is_normal(self) -> None:
        payload = summarize(baseline() + [sample(1, 0.11)], RAD_TH)
        self.assertEqual(payload["state"], "NORMAL")
        self.assertEqual(payload["level"], 1)
        self.assertAlmostEqual(payload["median_usv"], 0.1, places=2)
        self.assertEqual(payload["devices"], 1)

    def test_single_device_spike_is_elevated(self) -> None:
        payload = summarize(baseline() + [sample(1, 1.2)], RAD_TH)
        self.assertEqual(payload["state"], "ELEVATED")
        self.assertEqual(payload["anomalous"], ["1"])

    def test_two_spiking_devices_is_watch(self) -> None:
        rows = baseline("1") + [sample(1, 1.2, "1")]
        rows += baseline("2") + [sample(1, 1.3, "2")]
        payload = summarize(rows, RAD_TH)
        self.assertEqual(payload["state"], "WATCH")
        self.assertEqual(payload["anomalous"], ["1", "2"])

    def test_extreme_reading_is_watch(self) -> None:
        payload = summarize(baseline() + [sample(1, 12.0)], RAD_TH)
        self.assertEqual(payload["state"], "WATCH")
        self.assertEqual(payload["extreme"], 1)

    def test_two_extreme_devices_is_alert(self) -> None:
        rows = baseline("1") + [sample(1, 12.0, "1")]
        rows += baseline("2") + [sample(1, 15.0, "2")]
        payload = summarize(rows, RAD_TH)
        self.assertEqual(payload["state"], "ALERT")
        self.assertEqual(payload["extreme"], 2)

    def test_future_and_implausible_rows_are_dropped(self) -> None:
        rows = baseline()
        rows.append({"captured_at": "2072-03-23T11:14:40.000Z", "value": 500.0, "device_id": "1"})
        rows.append({"captured_at": utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"), "value": 0.0})
        rows.append({"captured_at": utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"), "value": 250.0})
        payload = summarize(rows, RAD_TH)
        self.assertEqual(payload["state"], "NORMAL")
        self.assertEqual(payload["dropped"]["future"], 1)
        self.assertEqual(payload["dropped"]["implausible"], 2)

    def test_thresholds_come_from_settings(self) -> None:
        rows = baseline() + [sample(1, 1.2)]
        payload = summarize(rows, {**RAD_TH, "spike_factor": 100.0, "spike_delta_usv": 5.0})
        self.assertEqual(payload["state"], "NORMAL")
        self.assertEqual(payload["anomalous"], [])

    def test_no_samples_returns_none(self) -> None:
        self.assertIsNone(summarize([], RAD_TH))


class SafecastParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = CFG.sources_by_id["safecast_radiation"]

    def test_parser_emits_one_indicator_item(self) -> None:
        import json

        rows = baseline() + [sample(1, 0.11)]
        items = parse_safecast(self.source, json.dumps(rows), CFG)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].kind, "indicator")
        self.assertEqual(items[0].payload["state"], "NORMAL")
        self.assertEqual(items[0].source_id, "safecast_radiation")

    def test_parser_ignores_non_list_payload(self) -> None:
        self.assertEqual(parse_safecast(self.source, '{"error": "nope"}', CFG), [])


class RadiationDisplayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.tmp.name) / "t.db")
        await self.db.open()

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.tmp.cleanup()

    async def test_digest_and_status_show_indicator(self) -> None:
        await self.db.indicator_set(
            "safecast_radiation",
            "ELEVATED",
            {"state": "ELEVATED", "level": 2, "median_usv": 0.12, "devices": 6,
             "anomalous": ["26"]},
        )
        self.assertEqual(await digest_parts(self.db), ["Радиация ELEVATED (0.12 мкЗв/ч)"])
        line = (await status_lines(self.db))[0]
        self.assertIn("ELEVATED", line)
        self.assertIn("2/5", line)
        self.assertIn("датчиков 6", line)
        self.assertIn("выбросов 1", line)

    async def test_display_is_empty_without_state(self) -> None:
        self.assertEqual(await digest_parts(self.db), [])
        self.assertEqual(await status_lines(self.db), [])


if __name__ == "__main__":
    unittest.main()
