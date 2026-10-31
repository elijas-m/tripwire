"""Bitemporal storage: valid, knowledge and transaction time plus as-of reads."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from tripwire.db import DB
from tripwire.models import Event
from tripwire.util import UTC, set_clock

BASE = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def at(minutes: int) -> int:
    return int((BASE + timedelta(minutes=minutes)).timestamp())


class BitemporalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "t.db"
        self.db = DB(self.path)
        await self.db.open()
        set_clock(lambda: BASE)

    async def asyncTearDown(self) -> None:
        set_clock(None)
        await self.db.close()
        self.tmp.cleanup()

    async def _event(self, guid: str = "g1", title: str = "First report") -> Event:
        return await self._insert(Event(
            source_id="wire", tier=2, guid_hash=guid, title=title,
            url=f"https://example.org/{guid}", category="israel", theater="ISRAEL",
            published_at=BASE - timedelta(hours=1),
        ))

    async def _insert(self, event: Event) -> Event:
        event_id = await self.db.insert_event(event)
        self.assertIsNotNone(event_id)
        event.id = event_id
        return event

    async def test_insert_records_baseline_revision(self) -> None:
        event = await self._event()
        history = await self.db.revision_history(event.id)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["reason"], "inserted")
        self.assertEqual(int(history[0]["recorded_at"].timestamp()), int(BASE.timestamp()))
        self.assertEqual(history[0]["event"].title, "First report")
        self.assertIsNone(await self.db.event_as_of(event.id, at(-1)))
        self.assertEqual((await self.db.event_as_of(event.id, at(0))).title, "First report")

    async def test_as_of_returns_state_before_rescoring(self) -> None:
        event = await self._event()
        set_clock(lambda: BASE + timedelta(hours=1))
        await self.db.update_event_scoring(event.id, 5.0, 1, ["outbreak"])
        before = await self.db.event_as_of(event.id, at(30))
        after = await self.db.event_as_of(event.id, at(60))
        self.assertEqual(before.score, 0.0)
        self.assertEqual(before.status, "new")
        self.assertAlmostEqual(after.score, 5.0)
        self.assertEqual(after.status, "scored")
        self.assertEqual([row["reason"] for row in await self.db.revision_history(event.id)],
                         ["inserted", "scored"])

    async def test_revision_keeps_previous_title(self) -> None:
        event = await self._event()
        set_clock(lambda: BASE + timedelta(hours=2))
        revised = Event(
            source_id="wire", tier=2, guid_hash="g1", title="Corrected report",
            url="https://example.org/g1", category="israel", theater="ISRAEL",
            published_at=BASE - timedelta(hours=1), numeric_payload={"mag": 6.9},
        )
        await self.db.update_event_revision(event.id, revised)
        old = await self.db.event_as_of(event.id, at(90))
        new = await self.db.event_as_of(event.id, at(120))
        self.assertEqual(old.title, "First report")
        self.assertEqual(new.title, "Corrected report")
        self.assertEqual(new.status, "revised")
        self.assertEqual(new.numeric_payload, {"mag": 6.9})

    async def test_cluster_and_payload_changes_are_versioned(self) -> None:
        event = await self._event()
        await self.db.update_event_payload(event.id, {"mag": 6.5})
        await self.db.update_event_cluster(event.id, 42)
        history = await self.db.revision_history(event.id)
        self.assertEqual([row["reason"] for row in history],
                         ["inserted", "payload", "clustered"])
        latest = await self.db.event_as_of(event.id, at(0))
        self.assertEqual(latest.cluster_id, 42)
        self.assertEqual(latest.numeric_payload, {"mag": 6.5})

    async def test_events_as_of_snapshot(self) -> None:
        first = await self._event("g1", "First")
        second = await self._event("g2", "Second")
        set_clock(lambda: BASE + timedelta(hours=1))
        await self.db.update_event_scoring(first.id, 9.0, 2, ["a"])
        mid = await self.db.events_as_of(at(30))
        later = await self.db.events_as_of(at(120))
        self.assertEqual({ev.title for ev in mid}, {"First", "Second"})
        self.assertEqual(next(ev for ev in mid if ev.id == first.id).score, 0.0)
        self.assertEqual(next(ev for ev in later if ev.id == first.id).score, 9.0)
        self.assertEqual(next(ev for ev in later if ev.id == second.id).score, 0.0)

    async def test_alert_decision_time_is_independent_of_delivery(self) -> None:
        event = await self._event()
        decision = at(0)
        alert_id = await self.db.insert_alert(
            None, 2, None, {"created_at": "x"}, status="pending", decided_at=decision,
        )
        set_clock(lambda: BASE + timedelta(minutes=15))
        await self.db.alert_sent(alert_id, 77)
        row = await self.db.get_alert(alert_id)
        self.assertEqual(row["decided_at"], decision)
        self.assertEqual(row["sent_at"], decision)

    async def test_migration_backfills_and_seeds_once(self) -> None:
        event = await self._event()
        await self.db.execute("UPDATE events SET recorded_at=NULL")
        await self.db.execute("DELETE FROM event_revisions")
        await self.db.close()
        self.db = DB(self.path)
        await self.db.open()
        row = await self.db.fetchone("SELECT recorded_at FROM events WHERE id=?", (event.id,))
        self.assertIsNotNone(row["recorded_at"])
        history = await self.db.revision_history(event.id)
        self.assertEqual([entry["reason"] for entry in history], ["migrated"])
        await self.db.close()
        self.db = DB(self.path)
        await self.db.open()
        self.assertEqual(len(await self.db.revision_history(event.id)), 1)

    async def test_cleanup_removes_orphan_revisions(self) -> None:
        event = await self._event()
        await self.db.execute("DELETE FROM events WHERE id=?", (event.id,))
        await self.db.cleanup()
        row = await self.db.fetchone(
            "SELECT COUNT(*) AS n FROM event_revisions WHERE event_id=?", (event.id,))
        self.assertEqual(row["n"], 0)


if __name__ == "__main__":
    unittest.main()
