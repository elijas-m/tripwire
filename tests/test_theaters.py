"""Stage 3 tests: lexicon, geo filter, correlator, theater pipeline."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tripwire.app import TripwireApp
from tripwire.config import Config
from tripwire.correlator import Correlator
from tripwire.db import DB
from tripwire.dedup import Deduper
from tripwire.geo import GeoIndex
from tripwire.models import Cluster, Event, L2, RawItem
from tripwire.parsers import parse_ukmto
from tripwire.rules.lexicon import LexiconEngine
from tripwire.util import utcnow

CFG = Config(Path(__file__).resolve().parent.parent).load()


def event(text: str, *, tier: int = 1, theater: str = "EU_RU",
          country: str | None = "UA", category: str = "eu_ru") -> Event:
    return Event(
        source_id="gnews_nato", tier=tier, title=text, body_excerpt=text,
        category=category, theater=theater, country=country, ingested_at=utcnow(),
    )


class LexiconTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = LexiconEngine(CFG)

    def test_single_s3_term(self) -> None:
        result = self.engine.score(event("Russia threatens nuclear strike"), 1.0, 1.0)
        self.assertEqual(result.base, 5.0)
        self.assertEqual(result.score, 1.2)   # tier 1 x no fact verb

    def test_secondary_terms_add_half_weight(self) -> None:
        result = self.engine.score(
            event("Massive strike on Kyiv after airspace closed"), 1.0, 1.0)
        self.assertAlmostEqual(result.base, 3.0)

    def test_base_is_capped(self) -> None:
        text = ("nuclear strike tactical nuclear article 5 general mobilization state of war "
                "declaration of war ground invasion nationwide lockdown martial law "
                "mass evacuation ballistic missile attack")
        result = self.engine.score(event(text), 1.0, 1.0)
        self.assertEqual(result.base, 12.0)

    def test_hard_negative_drops_low_tier_rumor(self) -> None:
        result = self.engine.score(
            event("NATO begins military exercise near the border", tier=2), 1.0, 1.0)
        self.assertTrue(result.dropped)
        self.assertEqual(result.drop_reason, "hard_negative")

    def test_tier_zero_ignores_hard_negative(self) -> None:
        result = self.engine.score(
            event("NATO begins military exercise near the border", tier=0), 1.0, 1.0)
        self.assertFalse(result.dropped)

    def test_feature_headline_dropped_even_for_tier_zero(self) -> None:
        result = self.engine.score(event(
            "How a polio outbreak transformed wastewater testing in the United Kingdom",
            tier=0), 1.0, 1.0)
        self.assertTrue(result.dropped)
        self.assertEqual(result.drop_reason, "feature_article")

    def test_russian_feature_headline_dropped(self) -> None:
        result = self.engine.score(event(
            "Как вспышка полиомиелита изменила тестирование сточных вод"), 1.0, 1.0)
        self.assertTrue(result.dropped)
        self.assertEqual(result.drop_reason, "feature_article")

    def test_breaking_headline_is_not_a_feature(self) -> None:
        result = self.engine.score(
            event("Missile strike kills dozens in Kyiv", tier=0), 1.0, 1.0)
        self.assertFalse(result.dropped)

    def test_escalation_terms_in_denial_sentence_do_not_score(self) -> None:
        result = self.engine.score(
            event("State Department denies reports of an ordered departure "
                  "from the Kyiv embassy", tier=0), 1.0, 1.0)
        self.assertEqual(result.base, 0.0)
        self.assertEqual(result.score, 0.0)

    def test_denial_does_not_silence_other_sentences(self) -> None:
        result = self.engine.score(
            event("US denies evacuation reports. Missile strike hit Kyiv overnight.",
                  tier=0), 1.0, 1.0)
        self.assertGreater(result.base, 0.0)

    def test_research_publication_dropped_even_for_tier_zero(self) -> None:
        result = self.engine.score(event(
            "Research: Measles, mumps and rubella: lab-confirmed cases in England 2026",
            tier=0, theater="PANDEMIC", category="pandemic"), 1.0, 1.0)
        self.assertTrue(result.dropped)
        self.assertEqual(result.drop_reason, "research_article")

    def test_russian_research_publication_dropped(self) -> None:
        result = self.engine.score(event(
            "Исследование: корь, паротит и краснуха в Англии",
            tier=0, theater="PANDEMIC"), 1.0, 1.0)
        self.assertTrue(result.dropped)
        self.assertEqual(result.drop_reason, "research_article")

    def test_outbreak_bulletin_is_not_research(self) -> None:
        result = self.engine.score(event(
            "Measles outbreak: 40 confirmed cases reported in England",
            tier=0, theater="PANDEMIC", category="pandemic"), 1.0, 1.0)
        self.assertFalse(result.dropped)

    def test_research_vessel_headline_is_not_dropped(self) -> None:
        result = self.engine.score(
            event("Research vessel seized in the Baltic Sea", tier=0), 1.0, 1.0)
        self.assertFalse(result.dropped)

    def test_preparedness_coordination_announcement_dropped(self) -> None:
        result = self.engine.score(event(
            "WHO, CEPI, and PATH collaborate to develop a standardized Nipah disease "
            "severity score to strengthen outbreak preparedness and vaccine research",
            tier=0, theater="PANDEMIC", category="pandemic"), 1.0, 1.0)
        self.assertTrue(result.dropped)
        self.assertEqual(result.drop_reason, "research_article")

    def test_factors_multiply_score(self) -> None:
        result = self.engine.score(event("Russia struck the capital with a massive strike"),
                                   geo_factor=0.3, novelty_factor=0.4)
        self.assertAlmostEqual(result.score, round(2.0 * 0.8 * 1.0 * 0.3 * 0.4, 3))


class GeoFactorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.geo = GeoIndex(CFG)

    def test_eu_ru_core(self) -> None:
        self.assertEqual(self.geo.geo_factor("EU_RU", "UA", None, None, 1), 1.0)

    def test_eu_ru_outside(self) -> None:
        self.assertEqual(self.geo.geo_factor("EU_RU", "BR", None, None, 1), 0.0)

    def test_israel_periphery(self) -> None:
        self.assertEqual(self.geo.geo_factor("ISRAEL", "EG", None, None, 1), 0.3)

    def test_pandemic_boost(self) -> None:
        self.assertEqual(self.geo.geo_factor("PANDEMIC", "VN", None, None, 1), 1.25)
        self.assertEqual(self.geo.geo_factor("PANDEMIC", "DE", None, None, 1), 1.0)

    def test_pandemic_country_count(self) -> None:
        matches = self.geo.match_countries(
            "Outbreak spread from the Democratic Republic of the Congo to Kenya and Nigeria")
        self.assertGreaterEqual(len({iso for iso, _a, _l in matches}), 3)

    def test_unknown_tier_fallback(self) -> None:
        self.assertEqual(self.geo.geo_factor("EU_RU", None, None, None, 0), 1.0)
        self.assertEqual(self.geo.geo_factor("EU_RU", None, None, None, 2), 0.3)


class PandemicBonusTests(unittest.TestCase):
    def setUp(self) -> None:
        from tripwire.rules.domain import DomainEngine
        self.engine = DomainEngine(CFG, GeoIndex(CFG))

    def test_ebola_don_scores_accumulated_points(self) -> None:
        ev = event(
            "Ebola disease caused by Bundibugyo virus, Democratic Republic of the Congo, "
            "Kenya and Nigeria",
            tier=0, theater="PANDEMIC", country="CD", category="pandemic",
        )
        ev.body_excerpt = (
            "the Bundibugyo virus disease outbreak has expanded in the Democratic Republic "
            "of the Congo, with Kenya and Nigeria reporting imported cases. A total of 9704 "
            "confirmed cases, including 4687 deaths (crude case fatality ratio: 48.3%), "
            "have been reported."
        )
        points, reasons = self.engine.bonus(ev)
        self.assertGreaterEqual(points, 14.0)
        self.assertIn("watch-list pathogen", reasons)
        self.assertTrue(any("CFR" in r for r in reasons))

    def test_awareness_item_gets_no_watchlist_bonus(self) -> None:
        ev = event(
            "World Polio Day 2026 Message from WHO Regional Director for Africa, "
            "Dr Mohamed Janabi",
            tier=0, theater="PANDEMIC", country=None, category="pandemic",
        )
        ev.body_excerpt = ev.title + " WHO | Regional Office for Africa"
        points, reasons = self.engine.bonus(ev)
        self.assertEqual(points, 0.0)
        self.assertNotIn("watch-list pathogen", reasons)

    def test_watchlist_requires_outbreak_evidence(self) -> None:
        ev = event("Polio vaccination campaign reaches two million children",
                   tier=0, theater="PANDEMIC", country=None, category="pandemic")
        points, reasons = self.engine.bonus(ev)
        self.assertNotIn("watch-list pathogen", reasons)

    def test_watchlist_ignores_outbreak_ending(self) -> None:
        ev = event(
            "Somalia restructures polio programme to end polio outbreak and protect communities",
            tier=0, theater="PANDEMIC", country="SO", category="pandemic",
        )
        points, reasons = self.engine.bonus(ev)
        self.assertNotIn("watch-list pathogen", reasons)
        self.assertEqual(points, 0.0)

    def test_watchlist_hits_on_outbreak_with_cases(self) -> None:
        ev = event("Marburg outbreak: 12 confirmed cases reported in Uganda",
                   tier=0, theater="PANDEMIC", country="UG", category="pandemic")
        points, reasons = self.engine.bonus(ev)
        self.assertIn("watch-list pathogen", reasons)
        self.assertEqual(points, 4.0)


class ForceProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.geo = GeoIndex(CFG)
        self.lexicon = LexiconEngine(CFG)

    def test_russian_deployment_headline(self) -> None:
        text = "США перебрасывают на Ближний Восток комплексы Patriot"
        self.assertTrue(self.geo.is_force_projection(text))
        self.assertEqual(self.geo.force_projection_theater(text), "GULF")
        result = self.lexicon.score(event(text, tier=2, country="US"), 0.3, 1.0)
        self.assertGreaterEqual(result.base, 2.0)
        self.assertFalse(result.dropped)

    def test_patriot_deployment_to_israel_routes_to_israel(self) -> None:
        text = "US deploys Patriot system in Israel amid preparations for Iran strikes"
        self.assertTrue(self.geo.is_force_projection(text))
        self.assertEqual(self.geo.force_projection_theater(text), "ISRAEL")

    def test_carrier_group_to_middle_east(self) -> None:
        text = "Pentagon sends aircraft carrier strike group to Middle East"
        self.assertTrue(self.geo.is_force_projection(text))
        self.assertEqual(self.geo.force_projection_theater(text), "GULF")

    def test_transport_planes_landing_in_israel(self) -> None:
        text = ("Top US commander briefs Trump on Iran military options as "
                "10 US military transport planes land in Israel")
        self.assertTrue(self.geo.is_force_projection(text))
        self.assertEqual(self.geo.force_projection_theater(text), "ISRAEL")
        result = self.lexicon.score(event(text, tier=2, country="IL"), 1.0, 1.0)
        self.assertGreaterEqual(result.base, 2.0)

    def test_civilian_deployments_are_ignored(self) -> None:
        for text in (
            "ECDC to deploy experts as Ebola cases emerge in Kenya and Nigeria",
            "Israel Police brace for 'January 6' scenario at Knesset after "
            "election, deploy 22,000 officers",
            "IAEA's Grossi Calls for Progress in Deploying Nuclear Power as "
            "Ministerial Conference Opens",
        ):
            self.assertFalse(self.geo.is_force_projection(text), text)

    def test_planned_deployment_is_not_a_fact(self) -> None:
        for text in (
            "US may deploy troops to the Middle East",
            "US plans to deploy troops to the Middle East",
            "США могут перебросить войска на Ближний Восток",
        ):
            self.assertFalse(self.geo.is_force_projection(text), text)


class ForceProjectionBonusTests(unittest.TestCase):
    def setUp(self) -> None:
        from tripwire.rules.domain import DomainEngine
        self.engine = DomainEngine(CFG, GeoIndex(CFG))

    def test_deployment_gets_projection_bonus(self) -> None:
        ev = event("США перебрасывают на Ближний Восток комплексы Patriot",
                   tier=2, theater="GULF", country=None)
        points, reasons = self.engine.bonus(ev)
        self.assertIn("force projection", reasons)
        self.assertEqual(points, 8.0)

    def test_planned_deployment_gets_no_bonus(self) -> None:
        ev = event("US may deploy troops to the Middle East",
                   tier=2, theater="GULF", country=None)
        points, reasons = self.engine.bonus(ev)
        self.assertNotIn("force projection", reasons)
        self.assertEqual(points, 0.0)

    def test_civilian_deployment_gets_no_bonus(self) -> None:
        ev = event("ECDC to deploy experts as Ebola cases emerge in Kenya and Nigeria",
                   tier=2, theater="GULF", country=None)
        points, reasons = self.engine.bonus(ev)
        self.assertNotIn("force projection", reasons)


class GulfSourceTests(unittest.TestCase):
    def test_ukmto_markdown_advisories(self) -> None:
        text = (
            "# Your choice regarding cookies\n\n"
            "*   [22/10/2026 Attack:UKMTO WARNING 181-26 ATTACK Incident Date: 22 OCT "
            "2026 Incident Time:...](https://www.ukmto.org/recent-incidents#8c1c646b)\n"
            "*   [20/10/2026 Attack:UKMTO WARNING 180-26 - ATTACK Incident Date:20 Oct "
            "2026 Incident...](https://www.ukmto.org/recent-incidents#5e0f8fa7)\n"
            "*   [Home](https://www.ukmto.org/)\n"
        )
        source = CFG.sources_by_id["ukmto_jina"]
        items = parse_ukmto(source, text, CFG)
        self.assertEqual([i.payload["advisory"] for i in items],
                         ["UKMTO WARNING 181-26", "UKMTO WARNING 180-26"])
        self.assertEqual(items[0].published_at.day, 22)
        self.assertEqual(items[0].category, "gulf")


class CorrelatorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.tmp.name) / "t.db")
        await self.db.open()
        self.correlator = Correlator(CFG, self.db)

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.tmp.cleanup()

    async def cluster_with(self, *source_ids: str) -> Cluster:
        cluster = Cluster(theater="EU_RU", category="eu_ru", title_canonical="test",
                          first_seen=utcnow(), last_seen=utcnow(), state="open")
        await self.db.insert_cluster(cluster)
        for sid in source_ids:
            await self.db.add_cluster_source(cluster.id, sid)
        return cluster

    async def test_warning_needs_two_sources_or_tier01(self) -> None:
        self.assertEqual(self.correlator._allowed_level(2, 1, False, False), 1)
        self.assertEqual(self.correlator._allowed_level(2, 2, False, False), 2)
        self.assertEqual(self.correlator._allowed_level(2, 1, True, False), 2)

    async def test_critical_needs_three_independent_sources(self) -> None:
        self.assertEqual(self.correlator._allowed_level(3, 2, True, False), 2)
        cluster = await self.cluster_with("gnews_nato", "ria_rss", "dod_rss")
        sources, tier01 = await self.correlator._independence(cluster)
        self.assertEqual(sources, 3)
        self.assertTrue(tier01)
        self.assertEqual(self.correlator._allowed_level(3, sources, tier01, False), 3)

    async def test_tripwire_bypasses_independence(self) -> None:
        self.assertEqual(self.correlator._allowed_level(3, 1, False, True), 3)

    async def test_theater_score_accumulates_with_decay(self) -> None:
        cluster = await self.cluster_with("gnews_nato")
        first = await self.correlator.add(event("massive strike on Kyiv"), 10.0, cluster)
        self.assertAlmostEqual(first.score, 10.0)
        second = await self.correlator.add(event("airspace closed over Ukraine"), 3.0, cluster)
        self.assertAlmostEqual(second.score, 13.0, places=1)

    async def test_ppi_cannot_lift_theater_to_critical(self) -> None:
        await self.db.indicator_set("ppi", "ANOMALY", {})
        cluster = await self.cluster_with("gnews_nato")
        decision = await self.correlator.add(event("massive strike on Kyiv"), 15.0, cluster)
        self.assertEqual(decision.cluster_level, 2)
        self.assertEqual(decision.level, 2)

    async def test_strong_event_stays_critical_despite_ppi(self) -> None:
        await self.db.indicator_set("ppi", "ANOMALY", {})
        cluster = await self.cluster_with("gnews_nato", "ria_rss", "dod_rss")
        decision = await self.correlator.add(event("massive strike on Kyiv"), 20.0, cluster)
        self.assertEqual(decision.cluster_level, 3)

    async def test_concurrent_updates_do_not_lose_points(self) -> None:
        cluster = await self.cluster_with("gnews_nato")
        await asyncio.gather(*(
            self.correlator.add(event("massive strike on Kyiv"), 2.0, cluster)
            for _ in range(5)
        ))
        state = await self.db.theater_get("EU_RU")
        self.assertAlmostEqual(float(state["score"]), 10.0, places=1)


class ClusterMatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.tmp.name) / "t.db")
        await self.db.open()
        self.deduper = Deduper(CFG, GeoIndex(CFG))

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.tmp.cleanup()

    async def test_unrelated_domestic_incidents_do_not_cluster(self) -> None:
        first = event("В центре Саратова столкнулись трамвай и внедорожник, "
                      "один человек погиб")
        first.source_id = "ria_rss"
        first.guid_hash = "tram"
        d1 = await self.deduper.assign(first, self.db)
        second = event("В Коми дело о гибели трех человек после пожара в учебном "
                       "центре МВД дошло до суда")
        second.source_id = "interfax_rss"
        second.guid_hash = "court"
        d2 = await self.deduper.assign(second, self.db)
        self.assertTrue(d2.is_new_cluster)
        self.assertNotEqual(d1.cluster.id, d2.cluster.id)


class FakeTelegram:
    configured = True
    failures = 0
    backup_fail_threshold = 3

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.edits: list[tuple[int, str]] = []

    async def send_message(self, text, thread_id=None, **kwargs) -> int:
        self.messages.append(text)
        return len(self.messages)

    async def edit_message(self, message_id, text, **kwargs) -> bool:
        self.edits.append((int(message_id), text))
        return True

    async def pin(self, message_id: int) -> bool:
        return True


class PipelineTests(unittest.IsolatedAsyncioTestCase):
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

    def raw(self, text: str, source_id: str = "gnews_nato", **kwargs) -> RawItem:
        return RawItem(
            source_id=source_id, title=text, body=text,
            url=f"https://example.org/{abs(hash(text))}",
            published_at=utcnow(), category="eu_ru", guid=f"g-{text}",
            **kwargs,
        )

    def volcano_raw(self, advisory: str, ash: int = 13000,
                    colour: str = "red") -> RawItem:
        hour = int(advisory) % 24
        minute = (int(advisory) * 7) % 60
        stamp = f"2026-10-30 {hour:02d}:{minute:02d} UTC"
        return RawItem(
            source_id="vaac_toulouse",
            title=f"ETNA: volcanic ash advisory {advisory} ({stamp})",
            body=f"ETNA — VA advisory {advisory} issued {stamp}",
            url=f"https://vaac.meteo.fr/advisory/etna-{advisory}", published_at=utcnow(),
            payload={"volcano_name": "Etna", "colour_code": colour, "ash_ft": ash,
                     "country": "IT"},
            category="volcano", theater="NATURE", country="IT", lat=37.751, lon=14.9934,
            guid=f"vaac-etna-{advisory}",
        )

    async def test_nuclear_tripwire_fires_critical_end_to_end(self) -> None:
        source = self.app.sources["gnews_nato"]
        result = await self.app.process(
            self.raw("Putin orders tactical nuclear weapons use against Ukraine"), source)
        self.assertEqual(result["kind"], "scored")
        self.assertEqual(result["alert"]["action"], "dry_run")
        self.assertEqual(result["alert"]["level"], 3)
        row = await self.app.db.fetchone("SELECT level, status FROM alerts")
        self.assertEqual(int(row["level"]), 3)

    async def test_out_of_theater_event_scores_zero(self) -> None:
        source = self.app.sources["gnews_nato"]
        result = await self.app.process(
            self.raw("Massive strike kills dozens in Brazil"), source)
        self.assertIn(result["kind"], ("zero", "dropped"))

    async def test_deployment_story_counts_in_destination_theater(self) -> None:
        source = self.app.sources["interfax_rss"]
        result = await self.app.process(RawItem(
            source_id="interfax_rss",
            title="США перебрасывают на Ближний Восток комплексы Patriot",
            body="США перебрасывают на Ближний Восток комплексы Patriot",
            url="https://www.interfax.ru/world/999001",
            published_at=utcnow(), category="eu_ru", theater="EU_RU",
            guid="interfax-patriot-me",
        ), source)
        self.assertEqual(result["kind"], "scored")
        row = await self.app.db.fetchone(
            "SELECT theater, country, score FROM events ORDER BY id DESC LIMIT 1")
        self.assertEqual(row["theater"], "GULF")
        self.assertIsNone(row["country"])
        self.assertGreater(float(row["score"]), 1.0)
        alerts = await self.app.db.fetchone("SELECT COUNT(*) AS n FROM alerts")
        self.assertEqual(int(alerts["n"]), 0)

    async def test_ukhsa_research_release_not_alerted(self) -> None:
        source = self.app.sources["ukhsa_atom"]
        result = await self.app.process(RawItem(
            source_id="ukhsa_atom",
            title=("Research: Measles, mumps and rubella: lab-confirmed cases in "
                   "England 2026"),
            body=("UKHSA lab-confirmed cases of measles, mumps and rubella in England."),
            url="https://gov.uk/government/publications/measles-mumps-rubella-2026",
            published_at=utcnow(), category="pandemic", theater="PANDEMIC",
            guid="ukhsa-research-measles",
        ), source)
        self.assertEqual(result["kind"], "zero")
        row = await self.app.db.fetchone("SELECT COUNT(*) AS n FROM alerts")
        self.assertEqual(int(row["n"]), 0)

    async def test_alert_uses_short_importance_label(self) -> None:
        self.app.alerts.tg = FakeTelegram()
        source = self.app.sources["gnews_nato"]
        result = await self.app.process(
            self.raw("Putin orders tactical nuclear weapons use against Ukraine"), source)
        self.assertEqual(result["alert"]["action"], "sent")
        text = self.app.alerts.tg.messages[-1]
        self.assertIn("Важно: ", text)
        self.assertNotIn("Почему важно", text)

    async def test_weak_cluster_does_not_alert_on_theater_level(self) -> None:
        ev = event("В Коми дело о гибели трех человек после пожара в учебном "
                   "центре МВД дошло до суда")
        ev.score = 0.06
        decision = SimpleNamespace(cluster_level=L2, theater="EU_RU")
        before = await self.app.db.fetchone("SELECT COUNT(*) AS n FROM alerts")
        result = await self.app.alerts.handle(decision, None, ev)
        self.assertEqual(result["action"], "weak_evidence")
        after = await self.app.db.fetchone("SELECT COUNT(*) AS n FROM alerts")
        self.assertEqual(int(before["n"]), int(after["n"]))

    async def test_who_feature_article_not_alerted(self) -> None:
        source = self.app.sources["gnews_who"]
        result = await self.app.process(RawItem(
            source_id="gnews_who",
            title=("How a polio outbreak transformed wastewater testing in the "
                   "United Kingdom"),
            body=("How a polio outbreak transformed wastewater testing in the "
                  "United Kingdom World Health Organization (WHO)"),
            url="https://news.google.com/rss/articles/polio-feature",
            published_at=utcnow(), category="pandemic", theater="PANDEMIC",
            guid="gnews-who-polio-feature",
        ), source)
        self.assertEqual(result["kind"], "zero")
        row = await self.app.db.fetchone("SELECT COUNT(*) AS n FROM alerts")
        self.assertEqual(int(row["n"]), 0)

    async def test_nature_critical_has_no_automatic_repeats(self) -> None:
        self.app.alerts.tg = FakeTelegram()
        source = self.app.sources["vaac_toulouse"]
        result = await self.app.process(RawItem(
            source_id="vaac_toulouse",
            title="ETNA: volcanic ash advisory 202 (2026-10-28 13:21 UTC)",
            body="ETNA — VA advisory 202 issued 2026-10-28 13:21 UTC",
            url="https://vaac.meteo.fr/advisory/etna-202", published_at=utcnow(),
            payload={"volcano_name": "Etna", "colour_code": "orange",
                     "ash_ft": 11000, "country": "IT"},
            category="volcano", theater="NATURE", country="IT", lat=37.751, lon=14.9934,
            guid="vaac-etna-202",
        ), source)
        self.assertEqual(result["alert"]["action"], "sent")
        self.assertEqual(result["alert"]["level"], 3)
        row = await self.app.db.fetchone(
            "SELECT COUNT(*) AS n FROM pending_actions WHERE kind='alert_repeat'")
        self.assertEqual(int(row["n"]), 0)

    async def test_volcano_repeat_advisory_edits_without_new_message(self) -> None:
        tg = FakeTelegram()
        self.app.alerts.tg = tg
        source = self.app.sources["vaac_toulouse"]
        first = await self.app.process(self.volcano_raw("301"), source)
        self.assertEqual(first["alert"]["action"], "sent")
        second = await self.app.process(self.volcano_raw("302"), source)
        self.assertEqual(second["alert"]["action"], "edited")
        row = await self.app.db.fetchone("SELECT COUNT(*) AS n FROM alerts")
        self.assertEqual(int(row["n"]), 1)
        self.assertEqual(len(tg.messages), 1)
        self.assertEqual(len(tg.edits), 1)

    async def test_volcano_higher_ash_sends_new_message(self) -> None:
        tg = FakeTelegram()
        self.app.alerts.tg = tg
        source = self.app.sources["vaac_toulouse"]
        first = await self.app.process(self.volcano_raw("301"), source)
        self.assertEqual(first["alert"]["action"], "sent")
        self.app.alerts.cooldowns = {"critical_minutes": 0}
        second = await self.app.process(self.volcano_raw("302", ash=16000), source)
        self.assertEqual(second["alert"]["action"], "sent")
        row = await self.app.db.fetchone("SELECT COUNT(*) AS n FROM alerts")
        self.assertEqual(int(row["n"]), 2)
        self.assertEqual(len(tg.messages), 2)

    async def test_volcano_higher_colour_sends_new_message(self) -> None:
        tg = FakeTelegram()
        self.app.alerts.tg = tg
        source = self.app.sources["vaac_toulouse"]
        first = await self.app.process(
            self.volcano_raw("301", ash=8000, colour="orange"), source)
        self.assertEqual(first["alert"]["action"], "sent")
        self.app.alerts.cooldowns = {"critical_minutes": 0}
        second = await self.app.process(
            self.volcano_raw("302", ash=8000, colour="red"), source)
        self.assertEqual(second["alert"]["action"], "sent")
        row = await self.app.db.fetchone("SELECT COUNT(*) AS n FROM alerts")
        self.assertEqual(int(row["n"]), 2)

    async def test_global_repeats_disabled_for_critical(self) -> None:
        self.app.alerts.tg = FakeTelegram()
        source = self.app.sources["gnews_nato"]
        result = await self.app.process(
            self.raw("Putin orders tactical nuclear weapons use against Ukraine"), source)
        self.assertEqual(result["alert"]["action"], "sent")
        self.assertEqual(result["alert"]["level"], 3)
        row = await self.app.db.fetchone(
            "SELECT COUNT(*) AS n FROM pending_actions WHERE kind='alert_repeat'")
        self.assertEqual(int(row["n"]), 0)

    async def test_nature_warning_scores_without_delivery(self) -> None:
        source = self.app.sources["usgs_sig_hour"]
        result = await self.app.process(RawItem(
            source_id="usgs_sig_hour",
            title="M 6.6 - 40 km S of Kahramanmaras, Turkey",
            body="Significant earthquake reported by USGS.",
            url="https://usgs.gov/nature-warning", published_at=utcnow(),
            payload={"mag": 6.6, "depth": 20.0, "alert": "yellow", "place": "Turkey"},
            category="earthquake", theater="NATURE", country="TR", lat=37.58, lon=36.93,
            guid="usgs-nature-warning",
        ), source)
        self.assertEqual(result["kind"], "scored")
        self.assertEqual(result["alert"]["action"], "suppressed")
        self.assertEqual(result["alert"]["level"], 2)
        state = await self.app.db.theater_get("NATURE")
        self.assertGreater(float(state["score"]), 0.0)
        self.assertGreaterEqual(int(state["level"]), L2)
        row = await self.app.db.fetchone("SELECT COUNT(*) AS n FROM alerts")
        self.assertEqual(int(row["n"]), 0)

    async def test_nature_critical_still_alerts(self) -> None:
        source = self.app.sources["usgs_sig_hour"]
        result = await self.app.process(RawItem(
            source_id="usgs_sig_hour",
            title="M 7.8 - 30 km SSE of Kahramanmaras, Turkey",
            body="Significant earthquake reported by USGS.",
            url="https://usgs.gov/nature-critical", published_at=utcnow(),
            payload={"mag": 7.8, "depth": 20.0, "alert": "red", "place": "Turkey"},
            category="earthquake", theater="NATURE", country="TR", lat=37.58, lon=36.93,
            guid="usgs-nature-critical",
        ), source)
        self.assertEqual(result["kind"], "scored")
        self.assertEqual(result["alert"]["action"], "dry_run")
        self.assertEqual(result["alert"]["level"], 3)
        row = await self.app.db.fetchone("SELECT level FROM alerts")
        self.assertEqual(int(row["level"]), 3)

    async def test_ebola_outbreak_warning_end_to_end(self) -> None:
        source = self.app.sources["who_don_odata"]
        raw = RawItem(
            source_id="who_don_odata", title=(
                "Ebola disease caused by Bundibugyo virus, Democratic Republic of the Congo, "
                "Kenya and Nigeria"),
            body=("The Bundibugyo virus disease outbreak has expanded in the Democratic Republic "
                  "of the Congo, with Kenya and Nigeria reporting imported cases. A total of "
                  "9704 confirmed cases (crude case fatality ratio: 48.3%) were reported."),
            url="https://who.int/2026-DON622", published_at=utcnow(),
            category="pandemic", guid="who-don-622",
        )
        result = await self.app.process(raw, source)
        self.assertEqual(result["kind"], "scored")
        self.assertEqual(result["alert"]["action"], "dry_run")
        self.assertEqual(result["alert"]["level"], 2)

    async def test_who_awareness_item_not_alerted(self) -> None:
        source = self.app.sources["who_news_rss"]
        raw = RawItem(
            source_id="who_news_rss",
            title=("World Polio Day 2026 Message from WHO Regional Director for Africa, "
                   "Dr Mohamed Janabi"),
            body="World Polio Day 2026 Message from WHO Regional Director for Africa "
                 "WHO | Regional Office for Africa",
            url="https://who.int/polio-day-2026", published_at=utcnow(),
            category="pandemic", guid="who-polio-day-2026",
        )
        result = await self.app.process(raw, source)
        self.assertEqual(result["kind"], "zero")

    async def test_second_warning_cluster_suppressed_by_theater_cooldown(self) -> None:
        don = self.app.sources["who_don_odata"]
        news = self.app.sources["who_news_rss"]
        first = await self.app.process(RawItem(
            source_id="who_don_odata",
            title=("Ebola disease caused by Bundibugyo virus, Democratic Republic of the "
                   "Congo, Kenya and Nigeria"),
            body=("The Bundibugyo virus disease outbreak has expanded in the Democratic "
                  "Republic of the Congo, with Kenya and Nigeria reporting imported cases. "
                  "A total of 9704 confirmed cases (crude case fatality ratio: 48.3%) were "
                  "reported."),
            url="https://who.int/2026-DON622", published_at=utcnow(),
            category="pandemic", guid="who-don-622",
        ), don)
        self.assertEqual(first["alert"]["action"], "dry_run")
        second = await self.app.process(RawItem(
            source_id="who_news_rss",
            title="Marburg outbreak: 12 confirmed cases reported in Uganda",
            body=("Uganda's ministry of health reported 12 confirmed cases in the Marburg "
                  "outbreak, with three deaths."),
            url="https://who.int/marburg-uganda", published_at=utcnow(),
            category="pandemic", guid="who-marburg-uganda",
        ), news)
        self.assertEqual(second["kind"], "scored")
        self.assertEqual(second["alert"]["action"], "theater_cooldown")

    async def test_concurrent_same_story_creates_one_cluster_one_alert(self) -> None:
        don = self.app.sources["who_don_odata"]
        news = self.app.sources["who_news_rss"]
        title = ("Ebola disease caused by Bundibugyo virus, Democratic Republic of the "
                 "Congo, Kenya and Nigeria")
        body = ("The Bundibugyo virus disease outbreak has expanded in the Democratic "
                "Republic of the Congo, with Kenya and Nigeria reporting imported cases. "
                "A total of 9704 confirmed cases (crude case fatality ratio: 48.3%) were "
                "reported.")
        await asyncio.gather(
            self.app.process(RawItem(
                source_id="who_don_odata", title=title, body=body,
                url="https://who.int/don-a", published_at=utcnow(),
                category="pandemic", guid="don-a",
            ), don),
            self.app.process(RawItem(
                source_id="who_news_rss", title=title, body=body,
                url="https://who.int/news-b", published_at=utcnow(),
                category="pandemic", guid="news-b",
            ), news),
        )
        alerts = await self.app.db.fetchone("SELECT COUNT(*) AS n FROM alerts")
        self.assertEqual(int(alerts["n"]), 1)
        clusters = await self.app.db.fetchone(
            "SELECT COUNT(*) AS n FROM clusters WHERE theater='PANDEMIC'")
        self.assertEqual(int(clusters["n"]), 1)

    async def test_same_cluster_gets_novelty_discount(self) -> None:
        source = self.app.sources["reuters_world"] if "reuters_world" in self.app.sources \
            else self.app.sources["gnews_nato"]
        first = self.raw("Russia launches massive missile strike on Kyiv power grid")
        await self.app.process(first, source)
        second = self.raw("Russia launches massive missile strike on Kyiv power grid again")
        result = await self.app.process(second, source)
        row = await self.app.db.fetchone(
            "SELECT score FROM events WHERE title LIKE '%again%' ORDER BY id DESC")
        self.assertIsNotNone(row)
        self.assertLess(float(row["score"]), 5.0)
        self.assertIn(result["kind"], ("duplicate", "scored"))


if __name__ == "__main__":
    unittest.main()
