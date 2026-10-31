"""Stage 4 tests: daily digest content and source degradation detection."""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
import unittest
from datetime import timedelta
from pathlib import Path

from tripwire.app import TripwireApp
from tripwire.digest import build_digest, source_problems
from tripwire.models import Event, L1, RawItem
from tripwire.util import utcnow


class DigestTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_digest_contains_theater_alert_and_indicators(self) -> None:
        source = self.app.sources["who_don_odata"]
        await self.app.process(RawItem(
            source_id="who_don_odata",
            title=("Ebola disease caused by Bundibugyo virus, Democratic Republic of the "
                   "Congo, Kenya and Nigeria"),
            body=("The Bundibugyo virus disease outbreak has expanded in the Democratic "
                  "Republic of the Congo, with Kenya and Nigeria reporting imported cases. "
                  "A total of 9704 confirmed cases (crude case fatality ratio: 48.3%) were "
                  "reported."),
            url="https://who.int/don-digest", published_at=utcnow(),
            category="pandemic", guid="don-digest",
        ), source)
        await self.app.db.indicator_set("ppi", "ELEVATED", {})
        text = await build_digest(self.app)
        self.assertIn("ДАЙДЖЕСТ", text)
        self.assertIn("Здоровье", text)
        self.assertIn("WARNING", text)
        self.assertIn("PPI ELEVATED", text)
        self.assertNotIn("Почти сработало", text)

    async def test_empty_watch_and_indicator_sections_omitted(self) -> None:
        text = await build_digest(self.app)
        self.assertNotIn("WATCH за сутки", text)
        self.assertNotIn("• —", text)
        self.assertNotIn("Индикаторы", text)

    async def test_failed_source_detected(self) -> None:
        for _ in range(3):
            await self.app.db.health_error("ria_rss", "HTTP 503")
        problems = await source_problems(self.app)
        self.assertTrue(any(item["id"] == "ria_rss" for item in problems["failed"]))

    async def test_silent_daily_source_detected(self) -> None:
        old = int(time.time()) - 80 * 3600
        await self.app.db.health_ok("ria_rss", 10.0, 5, None, None)
        await self.app.db.execute(
            "UPDATE source_health SET last_ok=?, total_items=10 WHERE source_id='ria_rss'",
            (old,),
        )
        problems = await source_problems(self.app)
        self.assertTrue(any(item["id"] == "ria_rss" for item in problems["silent"]))

    async def test_no_events_for_an_hour_is_degraded(self) -> None:
        await self.app.db.meta_set("started_at", (utcnow() - timedelta(hours=3)).isoformat())
        await self.app._check_sources()
        self.assertGreater(int(await self.app.db.meta_get("last_degraded_alert", 0) or 0), 0)

    async def test_first_error_timestamp_resets_after_success(self) -> None:
        await self.app.db.health_error("ria_rss", "HTTP 503")
        row = await self.app.db.health_get("ria_rss")
        self.assertIsNotNone(row.get("first_error_at"))
        await self.app.db.health_ok("ria_rss", 10.0, 1, None, None)
        row = await self.app.db.health_get("ria_rss")
        self.assertEqual(int(row.get("first_error_at") or 0), 0)

    async def test_backup_is_gzipped(self) -> None:
        target = Path(self.tmp.name) / "backups" / "tripwire-test.db"
        await self.app.db.backup(target)
        packed = await asyncio.to_thread(self.app._gzip, target)
        self.assertTrue(packed.exists())
        self.assertFalse(target.exists())
        self.assertGreater(packed.stat().st_size, 0)

    async def test_digest_schedule_is_within_a_day(self) -> None:
        self.assertGreater(self.app._seconds_until_digest(), 0)
        self.assertLessEqual(self.app._seconds_until_digest(), 86400)

    async def test_digest_omits_events_below_one_point(self) -> None:
        base = utcnow()
        for title, score, guid in (
            ("Low signal item", 0.5, "digest-low"),
            ("Strong watch item", 1.5, "digest-high"),
        ):
            event_id = await self.app.db.insert_event(Event(
                source_id="ria_rss", tier=1, title=title, guid_hash=guid,
                ingested_at=base, published_at=base, theater="EU_RU", category="eu_ru",
            ))
            await self.app.db.update_event_scoring(event_id, score, L1, [])
        text = await build_digest(self.app)
        self.assertNotIn("Low signal item", text)
        self.assertIn("Strong watch item", text)


if __name__ == "__main__":
    unittest.main()
