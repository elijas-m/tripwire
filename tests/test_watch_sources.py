"""EASA CZIB, travel advisories, PortWatch, gpsjam and OpenSky sources."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from tripwire.config import Config
from tripwire.db import DB
from tripwire.models import L2, Event, Source
from tripwire.parsers import (
    parse_easa_czib, parse_gpsjam, parse_opensky, parse_portwatch, parse_travel_advisory,
)
from tripwire.rules.tripwire import TripwireEngine
from tripwire.util import utcnow

CFG = Config(Path(__file__).resolve().parent.parent).load()


def source(parser: str, **kwargs) -> Source:
    data = {
        "id": f"test_{parser}", "name": parser, "type": "json", "parser": parser,
        "url": "https://example.test/feed", **kwargs,
    }
    return Source.from_dict(data, {})


class EasaCzibTests(unittest.TestCase):
    HTML = """
    <html><body><article>
      <a href="/en/domains/air-operations/czibs/czib-2026-04-r2">Airspace of Iran</a>
      <a href="/en/domains/air-operations/czibs/2025-01-r3">Airspace of the Russian Federation</a>
      <a href="/en/domains/air-operations/czibs/czib-2026-07r3">Airspace of the Persian Gulf and Gulf of Oman</a>
      <a href="/en/domains/air-operations/czibs/czib-2026-09-r1">Airspace of Saudi Arabia</a>
      <a href="/en/other/page">Not a bulletin</a>
    </article></body></html>
    """

    def test_bulletins_are_routed_to_their_theater(self) -> None:
        src = source(
            "easa_czib", base_url="https://www.easa.europa.eu", theater="EU_RU",
            category="eu_ru", link_pattern=r"(?i)/domains/air-operations/czibs/",
        )
        items = parse_easa_czib(src, self.HTML)
        by_region = {item.payload["region"]: item for item in items}
        self.assertEqual(len(items), 4)
        self.assertEqual(by_region["Airspace of Iran"].theater, "ISRAEL")
        self.assertEqual(by_region["Airspace of the Persian Gulf and Gulf of Oman"].theater, "GULF")
        self.assertEqual(by_region["Airspace of Saudi Arabia"].category, "gulf")
        russia = by_region["Airspace of the Russian Federation"]
        self.assertEqual(russia.theater, "EU_RU")
        self.assertEqual(russia.payload["czib"], "2025-01-R3")
        self.assertIn("EASA CZIB 2025-01-R3", russia.title)
        self.assertLess(abs((russia.published_at - utcnow()).total_seconds()), 60)


class TravelAdvisoryTests(unittest.TestCase):
    XML = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <item>
        <title>Iran - Level 4: Do Not Travel</title>
        <link>https://travel.state.gov/iran.html</link>
        <guid>https://travel.state.gov/iran.html</guid>
        <pubDate>Fri, 23 Oct 2026 00:00:00 GMT</pubDate>
        <description><![CDATA[<p>Do not travel to Iran.</p>]]></description>
      </item>
      <item>
        <title>Belarus - Level 4: Do Not Travel</title>
        <link>https://travel.state.gov/belarus.html</link>
        <guid>https://travel.state.gov/belarus.html</guid>
        <pubDate>Mon, 29 Dec 2025 00:00:00 GMT</pubDate>
        <description><![CDATA[<p>Do not travel to Belarus. Ordered departure On February 28,
        2022, the State Department ordered the departure of employees.</p>]]></description>
      </item>
      <item>
        <title>Example - Level 3: Reconsider Travel</title>
        <link>https://travel.state.gov/example.html</link>
        <guid>https://travel.state.gov/example.html</guid>
        <pubDate>Fri, 30 Oct 2026 00:00:00 GMT</pubDate>
        <description><![CDATA[<p>Reconsider travel. Authorized departure for family members.</p>]]></description>
      </item>
    </channel></rss>
    """

    def test_level_and_departure_status_are_extracted(self) -> None:
        items = parse_travel_advisory(source("travel_advisory", max_items=30), self.XML)
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0].title, "Belarus - Level 4: Do Not Travel")
        self.assertEqual(items[0].payload["departure"], "ordered")
        self.assertEqual(items[0].payload["advisory_level"], 4)
        by_title = {item.title: item for item in items}
        self.assertEqual(
            by_title["Example - Level 3: Reconsider Travel"].payload["departure"], "authorized")
        self.assertEqual(
            by_title["Iran - Level 4: Do Not Travel"].payload["departure"], "")

    def test_departure_advisories_survive_max_items(self) -> None:
        items = parse_travel_advisory(source("travel_advisory", max_items=1), self.XML)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].payload["departure"], "ordered")


class PortWatchTests(unittest.TestCase):
    def test_volume_drop_against_weekly_baseline_is_an_anomaly(self) -> None:
        lines = ["date,portname,n_total"]
        for day in range(1, 9):
            lines.append(f"2026-10-{day:02d},Strait of Hormuz,40")
            lines.append(f"2026-10-{day:02d},Suez Canal,50")
        lines.append("2026-10-09,Strait of Hormuz,12")
        lines.append("2026-10-09,Suez Canal,52")
        item = parse_portwatch(source("portwatch_json"), "\n".join(lines))[0]
        self.assertEqual(item.payload["state"], "ANOMALY")
        self.assertEqual(item.payload["as_of"], "2026-10-09")
        self.assertEqual(item.payload["chokepoints"]["Strait of Hormuz"]["n_total"], 12)
        self.assertTrue(any("Hormuz" in flag for flag in item.payload["notable"]))

    def test_stable_volumes_are_normal(self) -> None:
        lines = ["\ufeffdate,portname,n_total"]
        for day in range(1, 9):
            lines.append(f"2026/10/{day:02d},Suez Canal,50")
        item = parse_portwatch(source("portwatch_json"), "\n".join(lines))[0]
        self.assertEqual(item.payload["state"], "NORMAL")
        self.assertEqual(item.payload["as_of"], "2026-10-08")


class GpsJamTests(unittest.TestCase):
    HEADER = "date,suspect,num_bad_aircraft_hexes,source\n"

    def _rows(self, latest: int, suspect: bool = False) -> str:
        today = utcnow().date()
        rows = [self.HEADER]
        for days_ago in range(7, 0, -1):
            rows.append(f"{today - timedelta(days=days_ago)},false,400,merged\n")
        rows.append(f"{today},{str(suspect).lower()},{latest},merged\n")
        return "".join(rows)

    def test_surge_sets_elevated(self) -> None:
        item = parse_gpsjam(source("gpsjam_csv"), self._rows(700))[0]
        self.assertEqual(item.payload["state"], "ELEVATED")
        self.assertEqual(item.payload["bad_hexes"], 700)
        self.assertEqual(item.payload["baseline_7d"], 400)

    def test_suspect_day_is_unknown(self) -> None:
        item = parse_gpsjam(source("gpsjam_csv"), self._rows(700, suspect=True))[0]
        self.assertEqual(item.payload["state"], "UNKNOWN")

    def test_steady_counts_are_normal(self) -> None:
        item = parse_gpsjam(source("gpsjam_csv"), self._rows(420))[0]
        self.assertEqual(item.payload["state"], "NORMAL")


def opensky_state(lat: float, lon: float, on_ground: bool = False) -> list:
    return ["abc123", "CALL", "US", 0, 0, lat, lon, 10000.0, on_ground]


class OpenSkyTests(unittest.TestCase):
    def _parse(self, states: list[list]) -> list:
        return parse_opensky(source("opensky_json"), json.dumps({"time": 1793484977, "states": states}))

    def test_empty_core_is_closed(self) -> None:
        item = self._parse([opensky_state(47.0, 25.0)])[0]
        self.assertEqual(item.payload["state"], "CLOSED")
        self.assertEqual(item.payload["airborne_core"], 0)
        self.assertEqual(item.payload["airborne_region"], 1)

    def test_single_aircraft_is_activity(self) -> None:
        item = self._parse([opensky_state(50.0, 35.0)])[0]
        self.assertEqual(item.payload["state"], "ACTIVITY")
        self.assertEqual(item.payload["airborne_core"], 1)

    def test_many_aircraft_is_reopening(self) -> None:
        states = [opensky_state(50.0, 35.0) for _ in range(8)]
        item = self._parse(states)[0]
        self.assertEqual(item.payload["state"], "REOPENING")

    def test_grounded_aircraft_do_not_count_as_traffic(self) -> None:
        item = self._parse([opensky_state(50.0, 35.0, on_ground=True)])[0]
        self.assertEqual(item.payload["airborne_region"], 0)
        self.assertEqual(item.payload["state"], "CLOSED")


class DepartureDistinctionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.tmp.name) / "t.db")
        await self.db.open()
        self.engine = TripwireEngine(CFG)

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.tmp.cleanup()

    async def _hit(self, text: str, departure: str):
        event = Event(
            source_id="state_travel_rss", tier=0, title="Kyiv embassy notice",
            body_excerpt=text, category="eu_ru", theater="EU_RU", country="UA",
            ingested_at=utcnow(), numeric_payload={"departure": departure},
        )
        hits = await self.engine.match(event, self.db)
        hit = next((hit for hit in hits if hit.id == "TW-EU-ORDERED-DEPARTURE"), None)
        return hit, event

    async def test_authorized_departure_stays_warning(self) -> None:
        hit, event = await self._hit(
            "The embassy authorized departure of family members from Kyiv.", "authorized")
        self.assertIsNotNone(hit)
        self.assertEqual(hit.level, L2)
        self.assertIn("добровольный", hit.why)
        self.assertEqual(event.numeric_payload["departure"], "authorized")

    async def test_ordered_departure_is_recognized(self) -> None:
        hit, event = await self._hit(
            "The embassy ordered departure of staff from Kyiv.", "ordered")
        self.assertIsNotNone(hit)
        self.assertEqual(event.numeric_payload["departure"], "ordered")
        self.assertNotIn("добровольный", hit.why)


if __name__ == "__main__":
    unittest.main()
