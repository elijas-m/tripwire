"""Diplomatic evacuation indicator: detection patterns, levels and display."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from tripwire.config import Config
from tripwire.db import DB
from tripwire.diplo import (
    compute_diplo_evac,
    digest_parts,
    is_diplomatic_evacuation,
    status_lines,
    summarize,
)
from tripwire.models import Event
from tripwire.util import utcnow

CFG = Config(Path(__file__).resolve().parent.parent).load()
DIPLO_TH = CFG.thresholds.get("diplo_evac") or {}


def event(minutes_ago: float, title: str, cluster: int, country: str | None = None) -> dict:
    return {
        "id": cluster,
        "cluster_id": cluster,
        "title": title,
        "country": country,
        "theater": "EU_RU",
        "url": f"https://example.test/{cluster}",
        "status": "zero",
        "ts": int(time.time() - minutes_ago * 60),
    }


class DetectionTests(unittest.TestCase):
    def test_english_evacuation(self) -> None:
        self.assertTrue(is_diplomatic_evacuation("US embassy in Kabul evacuated overnight"))
        self.assertTrue(is_diplomatic_evacuation("Washington authorizes departure of diplomats from Kyiv"))
        self.assertTrue(is_diplomatic_evacuation("US orders departure of embassy staff from Beirut"))

    def test_russian_evacuation(self) -> None:
        self.assertTrue(
            is_diplomatic_evacuation(
                "Эвакуация дипломатов США на Ближнем Востоке проходит в хаосе, пишут СМИ"
            )
        )
        self.assertTrue(is_diplomatic_evacuation("Госдеп предписал выезд персонала посольства из Москвы"))

    def test_non_diplomatic_evacuation_is_ignored(self) -> None:
        self.assertFalse(is_diplomatic_evacuation("IDF evacuates Hebron neighborhood"))
        self.assertFalse(
            is_diplomatic_evacuation('Эвакуация специалистов АЭС "Бушер" продолжается')
        )

    def test_denial_and_rumor_are_ignored(self) -> None:
        self.assertFalse(
            is_diplomatic_evacuation(
                'Дипломаты назвали дезинформацией слухи об эвакуации специалистов АЭС "Бушер"'
            )
        )
        self.assertFalse(
            is_diplomatic_evacuation("Embassy denies reports that diplomats were evacuated")
        )

    def test_routine_closure_is_ignored(self) -> None:
        self.assertFalse(is_diplomatic_evacuation("Embassy closed for the holiday"))

    def test_retrospective_story_is_ignored(self) -> None:
        self.assertFalse(is_diplomatic_evacuation("2021 Kabul embassy evacuation haunts Washington"))
        self.assertTrue(is_diplomatic_evacuation("2026 Kabul embassy evacuation continues"))

    def test_slander_headline_is_ignored(self) -> None:
        self.assertFalse(
            is_diplomatic_evacuation("Посольство Китая призвало США прекратить клевету в адрес Пекина")
        )


class SummarizeTests(unittest.TestCase):
    def test_no_events_is_normal(self) -> None:
        payload = summarize([], DIPLO_TH)
        self.assertEqual(payload["state"], "NORMAL")
        self.assertEqual(payload["level"], 1)
        self.assertEqual(payload["clusters"], 0)

    def test_single_evacuation_is_elevated(self) -> None:
        rows = [event(60, "US embassy in Beirut evacuated", 1, "LB")]
        payload = summarize(rows, DIPLO_TH)
        self.assertEqual(payload["state"], "ELEVATED")
        self.assertEqual(payload["clusters"], 1)
        self.assertEqual(payload["countries"], ["LB"])

    def test_same_cluster_counts_once(self) -> None:
        rows = [
            event(120, "US embassy in Beirut evacuated", 7, "LB"),
            event(60, "US diplomats pulled out of Beirut", 7, "LB"),
        ]
        payload = summarize(rows, DIPLO_TH)
        self.assertEqual(payload["clusters"], 1)
        self.assertEqual(payload["state"], "ELEVATED")

    def test_two_countries_within_window_is_anomaly(self) -> None:
        rows = [
            event(60, "US embassy in Beirut evacuated", 1, "LB"),
            event(90, "Embassy of France orders departure from Tehran", 2, "IR"),
        ]
        payload = summarize(rows, DIPLO_TH)
        self.assertEqual(payload["state"], "ANOMALY")
        self.assertEqual(payload["countries_72h"], ["IR", "LB"])

    def test_old_country_event_does_not_trigger_anomaly(self) -> None:
        rows = [
            event(60, "US embassy in Beirut evacuated", 1, "LB"),
            event(4 * 24 * 60, "Embassy of France orders departure from Tehran", 2, "IR"),
        ]
        payload = summarize(rows, DIPLO_TH)
        self.assertEqual(payload["state"], "ELEVATED")
        self.assertEqual(payload["countries_72h"], ["LB"])

    def test_three_clusters_is_anomaly(self) -> None:
        rows = [
            event(60, "US embassy in Beirut evacuated", 1, "LB"),
            event(120, "French embassy in Tehran closes", 2, "IR"),
            event(180, "German consulate in Cairo suspends operations", 3, None),
        ]
        payload = summarize(rows, DIPLO_TH)
        self.assertEqual(payload["state"], "ANOMALY")
        self.assertEqual(payload["clusters"], 3)

    def test_window_comes_from_settings(self) -> None:
        rows = [event(5 * 24 * 60, "US embassy in Beirut evacuated", 1, "LB")]
        self.assertEqual(summarize(rows, DIPLO_TH)["state"], "ELEVATED")
        payload = summarize(rows, {**DIPLO_TH, "window_hours": 48})
        self.assertEqual(payload["state"], "NORMAL")

    def test_future_rows_are_ignored(self) -> None:
        row = event(0, "US embassy in Beirut evacuated", 1, "LB")
        row["ts"] = int(time.time()) + 3600
        self.assertEqual(summarize([row], DIPLO_TH)["state"], "NORMAL")


class ComputeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.tmp.name) / "t.db")
        await self.db.open()

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.tmp.cleanup()

    async def add(self, title: str, cluster: int, status: str = "zero",
                  country: str | None = "LB") -> None:
        await self.db.insert_event(
            Event(
                source_id="test",
                tier=2,
                title=title,
                guid_hash=f"g-{cluster}",
                ingested_at=utcnow(),
                cluster_id=cluster,
                country=country,
                status=status,
                kind="event",
            )
        )

    async def test_compute_stores_indicator(self) -> None:
        await self.add("US embassy in Beirut evacuated", 1)
        await self.add("French embassy in Tehran closes", 2, country="IR")
        payload = await compute_diplo_evac(self.db, DIPLO_TH)
        self.assertEqual(payload["state"], "ANOMALY")
        stored = await self.db.indicator_get("diplo_evac")
        self.assertEqual(stored["value"], "ANOMALY")
        self.assertEqual(stored["meta"]["clusters"], 2)

    async def test_dropped_events_are_ignored(self) -> None:
        await self.add("US embassy in Beirut evacuated", 1, status="dropped")
        payload = await compute_diplo_evac(self.db, DIPLO_TH)
        self.assertEqual(payload["state"], "NORMAL")


class DisplayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.tmp.name) / "t.db")
        await self.db.open()

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.tmp.cleanup()

    async def test_digest_and_status(self) -> None:
        await self.db.indicator_set(
            "diplo_evac",
            "ANOMALY",
            {"state": "ANOMALY", "level": 3, "clusters": 3,
             "countries": ["IR", "LB"], "window_hours": 168},
        )
        self.assertEqual(await digest_parts(self.db), ["Дипэвакуация ANOMALY (3 за 7д)"])
        line = (await status_lines(self.db))[0]
        self.assertIn("ANOMALY", line)
        self.assertIn("3/7д", line)
        self.assertIn("IR, LB", line)

    async def test_display_is_empty_without_state(self) -> None:
        self.assertEqual(await digest_parts(self.db), [])
        self.assertEqual(await status_lines(self.db), [])


if __name__ == "__main__":
    unittest.main()
