"""Source observability: coverage states from source_health."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from tripwire import observability
from tripwire.config import Config
from tripwire.db import DB
from tripwire.models import Source
from tripwire.util import UTC, set_clock

ROOT = Path(__file__).resolve().parent.parent
BASE = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
HOUR = 3600


class ObservabilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.cfg = Config(ROOT).load()
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.tmp.name) / "t.db")
        await self.db.open()
        set_clock(lambda: BASE)

    async def asyncTearDown(self) -> None:
        set_clock(None)
        await self.db.close()
        self.tmp.cleanup()

    async def _seed(self, skip: tuple[str, ...] = ()) -> None:
        for source in self.cfg.enabled_sources():
            if source.id not in skip:
                await self.db.health_ok(source.id, 10.0, 1, None, None)

    async def _age(self, source_id: str, hours: float) -> None:
        await self.db.execute(
            "UPDATE source_health SET last_ok=? WHERE source_id=?",
            (int(BASE.timestamp() - hours * HOUR), source_id),
        )

    def _first(self, predicate) -> Source:
        return next(source for source in self.cfg.enabled_sources() if predicate(source))

    async def test_unknown_without_health_rows(self) -> None:
        result = await observability.assess(self.cfg, self.db)
        self.assertEqual(result.state, observability.STATE_UNKNOWN)
        self.assertEqual(result.total, len(self.cfg.enabled_sources()))
        self.assertEqual(result.summary, "нет данных")

    async def test_all_fresh_is_full(self) -> None:
        await self._seed()
        result = await observability.assess(self.cfg, self.db)
        self.assertEqual(result.state, observability.STATE_FULL)
        self.assertEqual(result.healthy, result.total)
        self.assertEqual(result.down, [])

    async def test_missing_key_source_degrades(self) -> None:
        key = self._first(lambda source: source.tier == 0)
        await self._seed(skip=(key.id,))
        result = await observability.assess(self.cfg, self.db)
        self.assertEqual(result.state, observability.STATE_DEGRADED)
        self.assertIn(key.id, result.key_down)
        self.assertIn(key.id, [source_id for source_id, _ in result.down])

    async def test_low_ratio_is_blind(self) -> None:
        await self._seed()
        sources = self.cfg.enabled_sources()
        for source in sources[: len(sources) // 2]:
            await self._age(source.id, 30)
        result = await observability.assess(self.cfg, self.db)
        self.assertEqual(result.state, observability.STATE_BLIND)
        self.assertLess(result.ratio, 0.7)

    async def test_failure_marks_source_down(self) -> None:
        await self._seed()
        key = self._first(lambda source: source.tier == 0)
        await self.db.health_error(key.id, "HTTP 503")
        result = await observability.assess(self.cfg, self.db)
        self.assertIn(key.id, result.key_down)
        labels = observability.down_labels(self.cfg, result)
        self.assertIn(key.name, labels)
        self.assertIn("сбой", labels)

    async def test_window_follows_poll_interval(self) -> None:
        await self._seed()
        source = self._first(lambda item: item.interval_s == 1800 and item.tier > 0)
        await self._age(source.id, 50 / 60)
        fresh = await observability.assess(self.cfg, self.db)
        self.assertNotIn(source.id, [item[0] for item in fresh.down])
        await self._age(source.id, 70 / 60)
        stale = await observability.assess(self.cfg, self.db)
        self.assertIn(source.id, [item[0] for item in stale.down])

    async def test_explicit_key_sources_override_tier_zero(self) -> None:
        low = self._first(lambda source: source.tier == 3)
        self.cfg.thresholds["observability"]["key_sources"] = [low.id]
        await self._seed(skip=(low.id,))
        result = await observability.assess(self.cfg, self.db)
        self.assertEqual(result.state, observability.STATE_DEGRADED)
        self.assertEqual(result.key_down, [low.id])

    async def test_labels_limit_and_counter(self) -> None:
        await self._seed()
        sources = self.cfg.enabled_sources()[:5]
        for source in sources:
            await self._age(source.id, 30)
        result = await observability.assess(self.cfg, self.db)
        labels = observability.down_labels(self.cfg, result, limit=3)
        self.assertIn("+2", labels)


if __name__ == "__main__":
    unittest.main()
