"""Revision detection for feeds whose GUIDs survive in-place updates."""

from __future__ import annotations

import unittest
from pathlib import Path

from tripwire.config import Config
from tripwire.dedup import Deduper
from tripwire.geo import GeoIndex
from tripwire.models import Event

CFG = Config(Path(__file__).resolve().parent.parent).load()


class AdvisoryRevisionTests(unittest.TestCase):
    @staticmethod
    def _event(**payload) -> Event:
        return Event(source_id="state_travel_rss", category="eu_ru", theater="EU_RU",
                     numeric_payload=payload)

    def setUp(self) -> None:
        self.deduper = Deduper(CFG, GeoIndex(CFG))

    def test_new_departure_order_on_known_advisory_is_a_revision(self) -> None:
        previous = self._event(feed_id="belarus", advisory_level=4, departure="authorized")
        current = self._event(feed_id="belarus", advisory_level=4, departure="ordered")
        self.assertTrue(self.deduper.is_revision(previous, current))

    def test_unchanged_advisory_is_not_a_revision(self) -> None:
        previous = self._event(feed_id="iran", advisory_level=4, departure="")
        current = self._event(feed_id="iran", advisory_level=4, departure="")
        self.assertFalse(self.deduper.is_revision(previous, current))

    def test_advisory_stored_without_payload_is_upgraded_once(self) -> None:
        previous = self._event(feed_id="belarus")
        current = self._event(feed_id="belarus", advisory_level=3, departure="authorized")
        self.assertTrue(self.deduper.is_revision(previous, current))


if __name__ == "__main__":
    unittest.main()
