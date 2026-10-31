"""Stage 2 tests: dedup/revisions/aftershocks/swarm + nature parsers."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from tripwire.config import Config
from tripwire.db import DB
from tripwire.dedup import Deduper
from tripwire.geo import GeoIndex
from tripwire.models import Event, L2, L3
from tripwire.parsers import (
    extract_vaac_advisory, parse_imo_quakes, parse_spa, parse_vaac_tokyo,
)
from tripwire.rules.domain import DomainEngine
from tripwire.rules.tripwire import TripwireEngine
from tripwire.simhash import compute, distance
from tripwire.util import utcnow

CFG = Config(Path(__file__).resolve().parent.parent).load()


def quake(mag: float, lat: float = 40.826, lon: float = 14.139, **payload) -> Event:
    return Event(
        source_id="usgs_sig_hour", tier=0, url=f"https://example.org/q/{mag}/{lat}/{lon}",
        guid_hash=f"g-{mag}-{lat}-{lon}", title=f"M{mag:.1f} — Campi Flegrei",
        body_excerpt="earthquake", category="earthquake", theater="NATURE",
        lat=lat, lon=lon, numeric_payload={"mag": mag, **payload},
        simhash=0, ingested_at=utcnow(),
    )


class DedupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.tmp.name) / "t.db")
        await self.db.open()
        self.deduper = Deduper(CFG, GeoIndex(CFG))

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.tmp.cleanup()

    async def test_revision_detected_on_mag_upgrade(self) -> None:
        old = quake(6.1)
        new = quake(6.5)
        self.assertTrue(self.deduper.is_revision(old, new))
        minor = quake(6.2)
        self.assertFalse(self.deduper.is_revision(old, minor))

    async def test_revision_detected_on_pager_upgrade(self) -> None:
        old = quake(6.6, alert="orange")
        new = quake(6.6, alert="red")
        self.assertTrue(self.deduper.is_revision(old, new))

    async def test_aftershock_updates_cluster(self) -> None:
        main = quake(6.8)
        d1 = await self.deduper.assign(main, self.db)
        self.assertTrue(d1.is_new_cluster)
        main.cluster_id = d1.cluster.id
        await self.db.insert_event(main)
        aft = quake(5.1, lat=40.83, lon=14.14)
        d2 = await self.deduper.assign(aft, self.db)
        self.assertFalse(d2.is_new_cluster)
        self.assertTrue(d2.aftershock)
        self.assertEqual(d2.cluster.meta.get("aftershocks"), 1)
        self.assertAlmostEqual(d2.cluster.meta.get("main_mag"), 6.8)

    async def test_stronger_shock_supersedes_cluster(self) -> None:
        main = quake(6.4)
        d1 = await self.deduper.assign(main, self.db)
        main.cluster_id = d1.cluster.id
        await self.db.insert_event(main)
        stronger = quake(7.1, lat=40.84, lon=14.15)
        d2 = await self.deduper.assign(stronger, self.db)
        self.assertTrue(d2.is_new_cluster)
        old = await self.db.get_cluster(d1.cluster.id)
        self.assertEqual(old.state, "superseded")

    async def test_simhash_syndicated_copy(self) -> None:
        stop = set(CFG.lexicon.get("stopwords", {}).get("en", []))
        a = compute("Russia launches missile strike on Kyiv power grid",
                    "Missiles struck critical infrastructure across the city overnight.", stop)
        b = compute("Missile strike hits Kyiv power grid, Russia launches attack",
                    "Critical infrastructure was struck by missiles overnight across the city.", stop)
        self.assertLessEqual(distance(a, b), 6)


class SwarmTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.tmp.name) / "t.db")
        await self.db.open()
        self.engine = TripwireEngine(CFG)

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.tmp.cleanup()

    async def test_swarm_fires_at_ten_quakes(self) -> None:
        now = int(time.time())
        for i in range(9):
            ev = quake(4.6 + i * 0.01, lat=40.826 + i * 0.001, lon=14.139)
            ev.ingested_at = utcnow()
            await self.db.insert_event(ev)
        current = quake(4.7)
        await self.db.insert_event(current)
        hits = await self.engine.match(current, self.db)
        self.assertIn("TW-NAT-SWARM", [h.id for h in hits])

    async def test_swarm_below_threshold(self) -> None:
        for i in range(3):
            await self.db.insert_event(quake(4.6 + i * 0.01))
        current = quake(4.7)
        await self.db.insert_event(current)
        hits = await self.engine.match(current, self.db)
        self.assertNotIn("TW-NAT-SWARM", [h.id for h in hits])

    async def test_ash_height_tripwire_levels(self) -> None:
        warning = Event(category="volcano", theater="NATURE",
                        numeric_payload={"volcano_name": "Etna", "ash_ft": 30000})
        levels = {h.id: h.level for h in await self.engine.match(warning, self.db)}
        self.assertEqual(levels.get("TW-NAT-ASH-FL300"), L2)
        critical = Event(category="volcano", theater="NATURE",
                         numeric_payload={"volcano_name": "Etna", "ash_ft": 45000})
        levels = {h.id: h.level for h in await self.engine.match(critical, self.db)}
        self.assertEqual(levels.get("TW-NAT-ASH-FL450"), L3)


class VolcanoClusterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.tmp.name) / "t.db")
        await self.db.open()
        self.deduper = Deduper(CFG, GeoIndex(CFG))

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.tmp.cleanup()

    def advisory(self, name: str, hhmm: str) -> Event:
        title = f"{name}: volcanic ash advisory {hhmm} UTC"
        return Event(
            source_id="vaac_washington", tier=0, url=f"https://x/{name}/{hhmm}",
            guid_hash=f"g-{name}-{hhmm}", title=title,
            body_excerpt=f"{name} — VA advisory {hhmm} UTC",
            category="volcano", theater="NATURE",
            lat=38.9072, lon=-77.0369,   # same coordinates must not merge distinct volcanoes
            numeric_payload={"volcano_name": name, "ash_advisory": True},
            ingested_at=utcnow(),
        )

    async def test_distinct_volcanoes_do_not_cluster(self) -> None:
        first = self.advisory("Fuego Guatemala", "2357")
        d1 = await self.deduper.assign(first, self.db)
        first.cluster_id = d1.cluster.id
        await self.db.insert_event(first)
        second = self.advisory("Reventador Ecuador", "2143")
        d2 = await self.deduper.assign(second, self.db)
        self.assertTrue(d2.is_new_cluster)
        self.assertNotEqual(d1.cluster.id, d2.cluster.id)

    async def test_same_volcano_clusters(self) -> None:
        first = self.advisory("Fuego Guatemala", "2357")
        d1 = await self.deduper.assign(first, self.db)
        first.cluster_id = d1.cluster.id
        await self.db.insert_event(first)
        second = self.advisory("Fuego Guatemala", "1754")
        d2 = await self.deduper.assign(second, self.db)
        self.assertFalse(d2.is_new_cluster)
        self.assertEqual(d1.cluster.id, d2.cluster.id)


class VolcanoDomainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = DomainEngine(CFG)

    def test_routine_ash_advisory_is_dropped(self) -> None:
        event = Event(source_id="vaac_tokyo", tier=0, category="volcano", theater="NATURE",
                      title="Sakurajima: volcanic ash advisory 2026/305",
                      numeric_payload={"volcano_name": "Sakurajima", "ash_advisory": True,
                                       "ash_ft": 5000})
        result = self.engine.evaluate(event, 1.0, 1.0)
        self.assertTrue(result.dropped)

    def test_ash_fl300_scores_warning_level(self) -> None:
        event = Event(source_id="vaac_toulouse", tier=0, category="volcano", theater="NATURE",
                      title="Etna: volcanic ash advisory 201",
                      numeric_payload={"volcano_name": "Etna", "ash_ft": 30000})
        result = self.engine.evaluate(event, 1.0, 1.0)
        self.assertFalse(result.dropped)
        self.assertEqual(result.score, 8.0)

    def test_novelty_factor_reduces_nature_score(self) -> None:
        event = Event(source_id="vaac_toulouse", tier=0, category="volcano", theater="NATURE",
                      title="Etna: volcanic ash advisory 201",
                      numeric_payload={"volcano_name": "Etna", "ash_ft": 30000})
        result = self.engine.evaluate(event, 1.0, 0.4)
        self.assertAlmostEqual(result.score, 3.2)

    def test_volcano_geotag_uses_gazetteer(self) -> None:
        geo = GeoIndex(CFG)
        country, lat, lon = geo.geotag(
            "Sakurajima (Aira Caldera): volcanic ash advisory 2026/305",
            "Sakurajima (JAPAN) — VA advisory 2026/305 issued 2026/10/24 00:05 UTC",
            {"volcano_name": "Sakurajima (Aira Caldera)"}, "NATURE",
        )
        self.assertEqual(country, "JP")
        self.assertAlmostEqual(lat, 31.585, places=2)
        self.assertAlmostEqual(lon, 130.657, places=2)


class NatureParserTests(unittest.TestCase):
    def test_vaac_tokyo_rows(self) -> None:
        html = """
        <table><tr class="mtx" id=tr0><td style="DISPLAY: none">2026/10/24 00:05:00</td>
        <td>00:05 UTC, 24 Oct. 2026</td><td>SAKURAJIMA (AIRA CALDERA)</td><td>JAPAN</td>
        <td><font>2026/305</font></td>
        <td><a href="TextData/2026/20261024_28208001_0305_Text.html">Text</a></td></tr></table>
        """
        source = CFG.sources_by_id["vaac_tokyo"]
        items = parse_vaac_tokyo(source, html, CFG)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].payload["volcano_name"], "Sakurajima (Aira Caldera)")
        self.assertEqual(items[0].published_at.year, 2026)

    def test_imo_embedded_js(self) -> None:
        html = """<script>VI.quakeInfo = [
        {'t':new Date(2026,10-1,23,23,49,17),'lat':'63.804','lon':'-22.688','dep':'4.3',
         's':'3.8','q':'50.0','dR':'Reykjanestá'}];</script>"""
        source = CFG.sources_by_id["imo_earthquakes"]
        items = parse_imo_quakes(source, html, CFG)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].payload["mag"], 3.8)
        self.assertEqual(items[0].published_at.hour, 23)

    def test_vaac_tokyo_ash_height(self) -> None:
        text = ("ERUPTION DETAILS: EXPLODED AT 20261023/2355Z OVER FL050 EXTD N "
                "OBS VA CLD: VA NOT IDENTIFIABLE FM SATELLITE DATA")
        info = extract_vaac_advisory(text)
        self.assertEqual(info["ash_ft"], 5000)
        self.assertNotIn("colour_code", info)

    def test_vaac_washington_layer_and_colour(self) -> None:
        text = ("ERUPTION DETAILS: VA EMS CONT. EST VA CLD: SFC/FL140 N1429 W09044 - "
                "N1418 W09051. AVIATION COLOR CODE: ORANGE")
        info = extract_vaac_advisory(text)
        self.assertEqual(info["ash_ft"], 14000)
        self.assertEqual(info["colour_code"], "orange")

    def test_vaac_british_colour_spelling(self) -> None:
        info = extract_vaac_advisory("AVIATION COLOUR CODE: RED. TOP FL300.")
        self.assertEqual(info["colour_code"], "red")
        self.assertEqual(info["ash_ft"], 30000)

    def test_spa_next_data_list(self) -> None:
        html = ('<script id="__NEXT_DATA__">{"props":{"pageProps":[{"uuid":"N1",'
                '"title":"A sufficiently long headline about the region",'
                '"published_at":1792766365,'
                '"sharable_link":"spa.gov.sa/en/N1?type=news&uuid=N1"}]}}</script>')
        source = CFG.sources_by_id["spa_en"]
        items = parse_spa(source, html, CFG)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].url, "https://spa.gov.sa/en/N1?type=news&uuid=N1")


if __name__ == "__main__":
    unittest.main()
