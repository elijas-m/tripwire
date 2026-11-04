"""P0/P1 guardrails: theater scope, confirmation, publisher independence, modifiers."""

from __future__ import annotations

import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from tripwire.alerts import AlertManager
from tripwire.config import Config
from tripwire.correlator import Correlator
from tripwire.db import DB
from tripwire.geo import GeoIndex
from tripwire.models import Cluster, Event, L1, L2, L3, TripwireHit
from tripwire.normalize import Normalizer
from tripwire.rules.tripwire import TripwireEngine
from tripwire.telegram_api import TelegramClient, _is_test_runner
from tripwire.util import UTC, publisher_domain, utcnow

CFG = Config(Path(__file__).resolve().parent.parent).load()


def event(text: str, *, tier: int = 1, theater: str = "EU_RU",
          country: str | None = "UA", category: str = "eu_ru",
          source_id: str = "gnews_nato") -> Event:
    return Event(
        source_id=source_id, tier=tier, title=text, body_excerpt=text,
        category=category, theater=theater, country=country, ingested_at=utcnow(),
    )


class PublisherDomainTests(unittest.TestCase):
    def test_article_origin_wins_over_feed_host(self) -> None:
        self.assertEqual(
            publisher_domain("timesofisrael.com", "gnews_toi", CFG), "timesofisrael.com")
        self.assertEqual(publisher_domain("jpost.com", "gnews_toi", CFG), "jpost.com")

    def test_google_news_transport_falls_back_to_configured_origin(self) -> None:
        self.assertEqual(publisher_domain("news.google.com", "gnews_nato", CFG), "nato.int")
        self.assertEqual(publisher_domain("", "gnews_nato", CFG), "nato.int")

    def test_telegram_channel_is_its_own_identity(self) -> None:
        self.assertEqual(publisher_domain("t.me", "idf_tg", CFG), "idf_tg")
        self.assertEqual(publisher_domain("t.me", "kremlin_tg", CFG), "kremlin_tg")

    def test_plain_feed_uses_its_host(self) -> None:
        self.assertEqual(publisher_domain("", "who_don_odata", CFG), "who.int")


class TripwireTheaterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.tmp.name) / "t.db")
        await self.db.open()
        self.engine = TripwireEngine(CFG)

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.tmp.cleanup()

    async def test_rule_does_not_fire_outside_its_theater(self) -> None:
        text = "Putin orders tactical nuclear weapons use against Ukraine"
        hits = {h.id for h in await self.engine.match(event(text, theater="EU_RU"), self.db)}
        self.assertIn("TW-RU-NUCLEAR-USE", hits)
        hits = {h.id for h in await self.engine.match(
            event(text, theater="ISRAEL", country="IL", category="israel"), self.db)}
        self.assertNotIn("TW-RU-NUCLEAR-USE", hits)

    def test_theater_lists_and_unscoped_rules(self) -> None:
        self.assertTrue(self.engine._theater_ok({"theater": "ISRAEL"},
                                                event("x", theater="ISRAEL")))
        self.assertFalse(self.engine._theater_ok({"theater": "ISRAEL"},
                                                 event("x", theater="GULF")))
        self.assertTrue(self.engine._theater_ok({"theaters": ["ISRAEL", "GULF"]},
                                                event("x", theater="GULF")))
        self.assertFalse(self.engine._theater_ok({"theaters": ["ISRAEL", "GULF"]},
                                                 event("x", theater="EU_RU")))
        self.assertTrue(self.engine._theater_ok({}, event("x", theater="EU_RU")))


class PandemicTripwireTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.tmp.name) / "t.db")
        await self.db.open()
        self.engine = TripwireEngine(CFG)

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.tmp.cleanup()

    def pandemic_event(self, title: str, body: str | None = None) -> Event:
        return Event(
            source_id="gnews_who", tier=0, title=title, body_excerpt=body or title,
            category="pandemic", theater="PANDEMIC", country=None,
            ingested_at=utcnow(),
        )

    async def hit_ids(self, ev: Event) -> set[str]:
        return {hit.id for hit in await self.engine.match(ev, self.db)}

    async def test_agreement_meeting_is_not_a_pandemic_declaration(self) -> None:
        hits = await self.hit_ids(self.pandemic_event(
            "WHO Director-General's opening remarks at the Ninth meeting of the "
            "Intergovernmental Working Group (IGWG) on the WHO Pandemic Agreement"))
        self.assertNotIn("TW-PAN-PANDEMIC", hits)

    async def test_pandemic_declaration_speech_still_fires(self) -> None:
        ev = self.pandemic_event(
            "WHO Director-General's opening remarks at the media briefing on "
            "COVID-19 - 11 March 2020",
            "We have therefore made the assessment that COVID-19 can be "
            "characterized as a pandemic.")
        hits = await self.engine.match(ev, self.db)
        hit = next((h for h in hits if h.id == "TW-PAN-PANDEMIC"), None)
        self.assertIsNotNone(hit)
        self.assertEqual(hit.level, L3)

    async def test_end_of_pandemic_is_negated(self) -> None:
        hits = await self.hit_ids(self.pandemic_event(
            "WHO declares the end of the COVID-19 pandemic"))
        self.assertNotIn("TW-PAN-PANDEMIC", hits)

    async def test_russian_declaration_fires(self) -> None:
        hits = await self.hit_ids(self.pandemic_event(
            "ВОЗ объявила пандемию из-за нового вируса"))
        self.assertIn("TW-PAN-PANDEMIC", hits)


class TripwireConfirmationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.tmp.name) / "t.db")
        await self.db.open()
        self.engine = TripwireEngine(CFG)

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.tmp.cleanup()

    def strike_event(self, tier: int) -> Event:
        return event("Israel struck Iran with a missile attack", tier=tier,
                     theater="ISRAEL", country="IL", category="israel",
                     source_id="gnews_toi")

    async def test_single_wire_copy_caps_critical_at_warning(self) -> None:
        hits = await self.engine.match(self.strike_event(2), self.db)
        hit = next(h for h in hits if h.id == "TW-IL-IRAN-STRIKE")
        self.assertEqual(hit.level, L2)
        self.assertFalse(hit.confirmed)
        self.assertEqual(hit.confirm_reason, "один источник")
        self.assertTrue(hit.payload.get("unconfirmed"))

    async def test_official_source_keeps_critical(self) -> None:
        hits = await self.engine.match(self.strike_event(0), self.db)
        hit = next(h for h in hits if h.id == "TW-IL-IRAN-STRIKE")
        self.assertEqual(hit.level, L3)
        self.assertTrue(hit.confirmed)
        self.assertIn("официальный источник", hit.confirm_reason)
        self.assertEqual(hit.horizon, "tactical")
        self.assertIn("Прекращение взаимных ударов", hit.refute)
        self.assertIn("МАГАТЭ", hit.next_tripwire)

    async def _cluster_with(self, *peers: tuple[int, str]) -> int:
        cluster = Cluster(theater="ISRAEL", category="israel", title_canonical="test",
                          first_seen=utcnow(), last_seen=utcnow(), state="open")
        await self.db.insert_cluster(cluster)
        for index, (tier, origin) in enumerate(peers):
            await self.db.insert_event(Event(
                source_id="gnews_toi", tier=tier,
                title="Israel struck Iran with a missile attack",
                url=f"https://{origin}/p{index}", guid_hash=f"peer-{index}",
                category="israel", theater="ISRAEL", origin_domain=origin,
                cluster_id=cluster.id, ingested_at=utcnow(),
            ))
        return cluster.id

    async def _unrelated_bucket(self, current: Event, count: int) -> None:
        cluster = Cluster(theater="ISRAEL", category="israel", title_canonical="test",
                          first_seen=utcnow(), last_seen=utcnow(), state="open")
        await self.db.insert_cluster(cluster)
        current.cluster_id = cluster.id
        await self.db.insert_event(current)
        for index in range(count):
            await self.db.insert_event(Event(
                source_id="gnews_toi", tier=2,
                title=f"Israeli election talks continue after coalition meeting {index}",
                url=f"https://outlet{index}.example/story{index}",
                guid_hash=f"unrelated-{index}", category="israel", theater="ISRAEL",
                origin_domain=f"outlet{index}.example",
                cluster_id=cluster.id, ingested_at=utcnow(),
            ))

    async def test_two_publishers_confirm_critical(self) -> None:
        current = self.strike_event(2)
        current.cluster_id = await self._cluster_with((2, "a.example"), (2, "b.example"))
        hits = await self.engine.match(current, self.db)
        hit = next(h for h in hits if h.id == "TW-IL-IRAN-STRIKE")
        self.assertEqual(hit.level, L3)
        self.assertTrue(hit.confirmed)
        self.assertIn("2 независимых", hit.confirm_reason)

    async def test_official_peer_in_cluster_confirms_critical(self) -> None:
        current = self.strike_event(2)
        current.cluster_id = await self._cluster_with((0, "idf.il"))
        hits = await self.engine.match(current, self.db)
        hit = next(h for h in hits if h.id == "TW-IL-IRAN-STRIKE")
        self.assertEqual(hit.level, L3)
        self.assertIn("официальный источник", hit.confirm_reason)

    async def test_same_publisher_duplicate_does_not_confirm(self) -> None:
        current = self.strike_event(2)
        current.cluster_id = await self._cluster_with(
            (2, "a.example"), (2, "a.example"))
        hits = await self.engine.match(current, self.db)
        hit = next(h for h in hits if h.id == "TW-IL-IRAN-STRIKE")
        self.assertEqual(hit.level, L2)
        self.assertFalse(hit.confirmed)

    async def test_unrelated_bucket_members_do_not_confirm(self) -> None:
        current = self.strike_event(2)
        await self._unrelated_bucket(current, 20)
        hits = await self.engine.match(current, self.db)
        hit = next(h for h in hits if h.id == "TW-IL-IRAN-STRIKE")
        self.assertEqual(hit.level, L2)
        self.assertFalse(hit.confirmed)
        self.assertEqual(hit.confirm_reason, "один источник")

    async def test_relevant_peer_confirms_inside_noisy_bucket(self) -> None:
        current = self.strike_event(2)
        await self._unrelated_bucket(current, 20)
        await self.db.insert_event(Event(
            source_id="gnews_jpost", tier=2,
            title="Israel struck Iran with a missile attack",
            url="https://jpost.com/iran-strike", guid_hash="relevant-peer",
            category="israel", theater="ISRAEL", origin_domain="jpost.com",
            cluster_id=current.cluster_id, ingested_at=utcnow(),
        ))
        hits = await self.engine.match(current, self.db)
        hit = next(h for h in hits if h.id == "TW-IL-IRAN-STRIKE")
        self.assertEqual(hit.level, L3)
        self.assertTrue(hit.confirmed)
        self.assertIn("2 независимых", hit.confirm_reason)


class AirspaceTighteningTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.tmp.name) / "t.db")
        await self.db.open()
        self.engine = TripwireEngine(CFG)

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.tmp.cleanup()

    async def _airspace_hits(self, text: str) -> list[str]:
        hits = await self.engine.match(event(text), self.db)
        return [h.id for h in hits if h.id == "TW-EU-AIRSPACE-FULL"]

    async def test_full_closure_fires(self) -> None:
        self.assertTrue(await self._airspace_hits(
            "Ukraine airspace closed to all civilian flights"))
        self.assertTrue(await self._airspace_hits(
            "Воздушное пространство Украины закрыто для гражданских рейсов"))

    async def test_partial_and_planned_closures_are_ignored(self) -> None:
        for text in (
            "Ukraine airspace partially closed to civilian flights",
            "Ukraine airspace will be closed from midnight",
            "Ukraine airspace to close tomorrow",
            "Ukraine plans to close airspace to civilian flights",
            "Ukraine may close airspace to civilian flights",
        ):
            self.assertEqual(await self._airspace_hits(text), [], text)


class IndependenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.tmp.name) / "t.db")
        await self.db.open()
        self.correlator = Correlator(CFG, self.db)

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.tmp.cleanup()

    async def cluster_with(self, *origins: tuple[str, int]) -> Cluster:
        cluster = Cluster(theater="ISRAEL", category="israel", title_canonical="test",
                          first_seen=utcnow(), last_seen=utcnow(), state="open")
        await self.db.insert_cluster(cluster)
        for index, (origin, tier) in enumerate(origins):
            await self.db.insert_event(Event(
                source_id="gnews_toi", tier=tier,
                title="Israel struck Iran with a missile attack",
                url=f"https://{origin}/{index}", guid_hash=f"g{index}",
                category="israel", theater="ISRAEL", origin_domain=origin,
                cluster_id=cluster.id, ingested_at=utcnow(),
            ))
        return cluster

    def current(self) -> Event:
        ev = event("Israel struck Iran with a missile attack", tier=2,
                   theater="ISRAEL", country="IL", category="israel",
                   source_id="gnews_toi")
        return ev

    async def test_distinct_publishers_count(self) -> None:
        cluster = await self.cluster_with(("a.example", 2), ("b.example", 2))
        self.assertEqual(
            await self.correlator._independence(cluster, self.current()), (2, False))
        self.assertEqual(self.correlator._allowed_level(2, 2, False, False), L2)

    async def test_same_publisher_counts_once(self) -> None:
        cluster = await self.cluster_with(("a.example", 2), ("a.example", 2))
        self.assertEqual(
            await self.correlator._independence(cluster, self.current()), (1, False))
        self.assertEqual(self.correlator._allowed_level(2, 1, False, False), L1)

    async def test_tier_zero_is_official(self) -> None:
        cluster = await self.cluster_with(("a.example", 0))
        self.assertEqual(
            await self.correlator._independence(cluster, self.current()), (1, True))
        self.assertEqual(self.correlator._allowed_level(2, 1, True, False), L2)

    async def test_unrelated_members_are_not_independent_confirmation(self) -> None:
        cluster = await self.cluster_with(("a.example", 2), ("b.example", 2))
        for index in range(20):
            await self.db.insert_event(Event(
                source_id="gnews_toi", tier=2,
                title=f"Israeli election talks continue after coalition meeting {index}",
                url=f"https://outlet{index}.example/s{index}",
                guid_hash=f"noise-{index}", category="israel", theater="ISRAEL",
                origin_domain=f"outlet{index}.example",
                cluster_id=cluster.id, ingested_at=utcnow(),
            ))
        self.assertEqual(
            await self.correlator._independence(cluster, self.current()), (2, False))

    async def test_telegram_channels_are_independent(self) -> None:
        cluster = Cluster(theater="EU_RU", category="eu_ru", title_canonical="test",
                          first_seen=utcnow(), last_seen=utcnow(), state="open")
        await self.db.insert_cluster(cluster)
        for index, sid in enumerate(("idf_tg", "nexta_tg")):
            await self.db.insert_event(Event(
                source_id=sid, tier=0 if index == 0 else 2,
                title="Russia launches missile strike on Kyiv power grid",
                url=f"https://t.me/s/{sid}/{index}", guid_hash=f"tg-{index}",
                category="eu_ru", theater="EU_RU", origin_domain="t.me",
                cluster_id=cluster.id, ingested_at=utcnow(),
            ))
        self.assertEqual(
            await self.correlator._independence(
                cluster, event("Russia launches missile strike on Kyiv power grid",
                               tier=2, theater="EU_RU", category="eu_ru")),
            (2, True),
        )


class ModifierCapTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.tmp.name) / "t.db")
        await self.db.open()
        self.correlator = Correlator(CFG, self.db)
        await self.db.indicator_set("market_shock", "ANOMALY", {})
        await self.db.indicator_set("ppi", "ANOMALY", {})
        cluster = Cluster(theater="EU_RU", category="eu_ru", title_canonical="test",
                          first_seen=utcnow(), last_seen=utcnow(), state="open")
        await self.db.insert_cluster(cluster)
        for sid in ("gnews_nato", "ria_rss", "dod_rss"):
            await self.db.add_cluster_source(cluster.id, sid)
        self.cluster = cluster

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.tmp.cleanup()

    async def test_modifiers_cannot_create_critical(self) -> None:
        decision = await self.correlator.add(
            event("major escalation on the front line", tier=2), 12.0, self.cluster)
        self.assertAlmostEqual(decision.score, 17.25, places=2)
        self.assertEqual(decision.cluster_level, L2)
        self.assertEqual(decision.level, L2)

    async def test_base_above_threshold_stays_critical(self) -> None:
        decision = await self.correlator.add(
            event("major escalation on the front line", tier=2), 16.0, self.cluster)
        self.assertEqual(decision.cluster_level, L3)

    async def test_unconfirmed_tripwire_cannot_create_critical(self) -> None:
        hit = TripwireHit(id="TW-IL-IRAN-STRIKE", level=L3, why="", one_liner="",
                          confirmed=False, confirm_reason="один источник")
        decision = await self.correlator.add(
            event("Israel struck Iran", tier=2), 0.0, self.cluster, tripwire=hit)
        self.assertEqual(decision.cluster_level, L2)

    async def test_related_theater_bonus_only(self) -> None:
        ev = event("escalation", tier=2)
        await self.db.theater_set("GULF", 5.0, L1, int(time.time()))
        self.assertIn("multi_theater", await self.correlator._modifiers(ev, "EU_RU"))
        await self.db.theater_set("GULF", 0.0, 0, int(time.time()))
        await self.db.theater_set("NATURE", 5.0, L1, int(time.time()))
        self.assertNotIn("multi_theater", await self.correlator._modifiers(ev, "EU_RU"))
        await self.db.theater_set("MENA", 5.0, L1, int(time.time()))
        self.assertIn("multi_theater", await self.correlator._modifiers(ev, "EU_RU"))


class TurkeyRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.geo = GeoIndex(CFG)
        self.normalizer = Normalizer(CFG, self.geo)

    def test_turkey_is_mena_not_eu_ru(self) -> None:
        self.assertEqual(self.geo.theater_for_country("TR"), "MENA")
        self.assertFalse(self.geo.country_for_theater("EU_RU", "TR"))

    def test_turkey_story_routes_to_mena(self) -> None:
        source = CFG.sources_by_id["gnews_nato"]
        raw = SimpleNamespace(
            source_id=source.id, title="Turkey shuts its border crossings after attack",
            body="Turkey closed border crossings near Ankara.", url="https://example.org/tr",
            published_at=utcnow(), lang=None, payload={}, guid="tr-1", origin_domain=None,
            category="eu_ru", theater=None, kind="event", country=None, lat=None, lon=None,
        )
        normalized = self.normalizer.normalize(raw, source)
        self.assertIsNotNone(normalized)
        self.assertEqual(normalized.theater, "MENA")

    def test_force_projection_to_turkey_targets_mena(self) -> None:
        self.assertEqual(
            self.geo.force_projection_theater("US deploys Patriot systems to Turkey"),
            "MENA")

    def test_relations_include_israel_and_gulf(self) -> None:
        correlator = Correlator(CFG, None)
        self.assertIn("GULF", correlator.relations["ISRAEL"])
        self.assertIn("ISRAEL", correlator.relations["GULF"])
        self.assertIn("MENA", correlator.relations["EU_RU"])


class _TranslatorStub:
    async def translate_many(self, items):
        return [f"перевод: {text}" for text, _lang in items]


class AlertFormatTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.tmp.name) / "t.db")
        await self.db.open()
        self.alerts = AlertManager(CFG, self.db, None)
        self.alerts.translator = _TranslatorStub()

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.tmp.cleanup()

    def _event(self) -> Event:
        ev = event("כותרת מקורית", tier=0, theater="ISRAEL", country="IL",
                   category="israel", source_id="who_don_odata")
        ev.title = "כותרת מקורית"
        ev.lang = "he"
        ev.published_at = datetime(2026, 10, 30, 12, 34, tzinfo=UTC)
        ev.ingested_at = datetime(2026, 10, 30, 13, 5, tzinfo=UTC)
        ev.url = "https://example.org/item"
        return ev

    async def test_header_score_confidence_and_times(self) -> None:
        cluster = Cluster(theater="ISRAEL", category="israel", title_canonical="test",
                          first_seen=utcnow(), last_seen=utcnow(), state="open")
        await self.db.insert_cluster(cluster)
        current = self._event()
        current.cluster_id = cluster.id
        await self.db.insert_event(current)
        await self.db.insert_event(Event(
            source_id="second_wire", tier=2, title="כותרת מקורית", url="https://b.example/1",
            guid_hash="peer-alert", category="israel", theater="ISRAEL",
            cluster_id=cluster.id, ingested_at=utcnow(),
        ))
        hit = TripwireHit(id="TW-IL-IRAN-STRIKE", level=L3, why="Взаимные удары.",
                          one_liner="Удар Израиля по Ирану", confirmed=True,
                          confirm_reason="официальный источник (tier 0)")
        decision = SimpleNamespace(score=20.0, threshold=16.0, sources=2)
        text = await self.alerts._compose(
            L3, "ISRAEL", cluster, current, decision, None, hit, 7)
        self.assertRegex(text, r"\d{2}\.\d{2}\.\d{4} \d{2}:\d{2} UTC")
        self.assertIn("счёт 20.0 (порог 16)", text)
        self.assertIn("подтверждение: официальный источник (tier 0)", text)
        self.assertIn("оценка не откалибрована", text)
        self.assertIn("опубл. 30.10 12:34 UTC", text)
        self.assertIn("получ. 30.10 13:05 UTC", text)
        self.assertIn("ориг.: כותרת מקורית", text)
        self.assertIn("Триггер: TW-IL-IRAN-STRIKE", text)
        self.assertNotIn("Не установлено:", text)

    async def test_alert_lists_only_supporting_sources(self) -> None:
        cluster = Cluster(theater="ISRAEL", category="israel", title_canonical="test",
                          first_seen=utcnow(), last_seen=utcnow(), state="open")
        await self.db.insert_cluster(cluster)
        current = self._event()
        current.cluster_id = cluster.id
        await self.db.insert_event(current)
        await self.db.insert_event(Event(
            source_id="second_wire", tier=2, title="Unrelated election story",
            url="https://b.example/unrelated", guid_hash="unrelated-alert",
            category="israel", theater="ISRAEL", cluster_id=cluster.id,
            ingested_at=utcnow(),
        ))
        lines, total = await self.alerts._source_lines(cluster, current)
        text = "\n".join(lines)
        self.assertEqual(total, 1)
        self.assertIn("перевод: כותרת מקורית", text)
        self.assertNotIn("Unrelated election", text)

    async def test_alert_lists_relevant_peer_sources(self) -> None:
        cluster = Cluster(theater="ISRAEL", category="israel", title_canonical="test",
                          first_seen=utcnow(), last_seen=utcnow(), state="open")
        await self.db.insert_cluster(cluster)
        current = self._event()
        current.cluster_id = cluster.id
        await self.db.insert_event(current)
        await self.db.insert_event(Event(
            source_id="second_wire", tier=2, title="כותרת מקורית",
            url="https://b.example/relevant", guid_hash="relevant-alert",
            category="israel", theater="ISRAEL", cluster_id=cluster.id,
            ingested_at=utcnow(),
        ))
        lines, total = await self.alerts._source_lines(cluster, current)
        self.assertEqual(total, 2)

    async def test_alert_reports_observability(self) -> None:
        sources = list(CFG.enabled_sources())
        for source in sources:
            await self.db.health_ok(source.id, 10.0, 1, None, None)
        hit = TripwireHit(id="TW-IL-IRAN-STRIKE", level=L3, why="Взаимные удары.",
                          one_liner="Удар Израиля по Ирану", confirmed=True,
                          confirm_reason="официальный источник (tier 0)")
        decision = SimpleNamespace(score=20.0, threshold=16.0, sources=2)
        text = await self.alerts._compose(
            L3, "ISRAEL", None, self._event(), decision, None, hit, 7)
        self.assertIn(f"Наблюдаемость: {len(sources)}/{len(sources)} · 100% · FULL", text)
        self.assertNotIn("наблюдение неполное", text)

    async def test_degraded_observability_is_stated(self) -> None:
        key = next(source for source in CFG.enabled_sources() if source.tier == 0)
        for source in CFG.enabled_sources():
            if source.id != key.id:
                await self.db.health_ok(source.id, 10.0, 1, None, None)
        hit = TripwireHit(id="TW-IL-IRAN-STRIKE", level=L3, why="Взаимные удары.",
                          one_liner="Удар Израиля по Ирану", confirmed=True,
                          confirm_reason="официальный источник (tier 0)")
        decision = SimpleNamespace(score=20.0, threshold=16.0, sources=2)
        text = await self.alerts._compose(
            L3, "ISRAEL", None, self._event(), decision, None, hit, 7)
        self.assertIn("DEGRADED", text)
        self.assertIn("наблюдение неполное", text)
        self.assertIn("полнота наблюдения", text)
        self.assertIn(key.name, text)

    async def test_unconfirmed_alert_states_unknowns(self) -> None:
        hit = TripwireHit(id="TW-IL-IRAN-STRIKE", level=L2, why="Взаимные удары.",
                          one_liner="Удар Израиля по Ирану", confirmed=False,
                          confirm_reason="один источник")
        decision = SimpleNamespace(score=8.0, threshold=8.0, sources=1)
        text = await self.alerts._compose(
            L2, "ISRAEL", None, self._event(), decision, None, hit, 8)
        self.assertIn("подтверждение: один источник", text)
        self.assertIn("Не подтверждено независимо.", text)
        self.assertIn("Не установлено:", text)
        self.assertIn("независимое подтверждение", text)

    async def test_bluf_blocks_and_guidance(self) -> None:
        hit = TripwireHit(
            id="TW-IL-IRAN-STRIKE", level=L3, why="Взаимные удары.",
            one_liner="Удар Израиля по Ирану", confirmed=True,
            confirm_reason="официальный источник (tier 0)",
            horizon="tactical",
            refute="Прекращение взаимных ударов и прекращение огня.",
            next_tripwire="Высылка инспекторов МАГАТЭ.",
        )
        decision = SimpleNamespace(score=20.0, threshold=16.0, sources=2)
        text = await self.alerts._compose(
            L3, "ISRAEL", None, self._event(), decision, None, hit, 7)
        self.assertIn("· ТАКТИЧЕСКИЙ ·", text)
        self.assertIn("Что делать: Рассмотрите выезд в ближайшие 48 ч", text)
        self.assertIn("Это не распоряжение органов гражданской обороны", text)
        self.assertIn("Что опровергнет: Прекращение взаимных ударов", text)
        self.assertIn("Следующий tripwire: Высылка инспекторов МАГАТЭ.", text)
        self.assertLessEqual(len(text), 4096)

    async def test_bluf_theater_override(self) -> None:
        hit = TripwireHit(
            id="TW-NAT-TSUNAMI-WARNING", level=L3, why="Цунами.",
            one_liner="Предупреждение о цунами", confirmed=True,
            confirm_reason="официальное локальное оповещение",
        )
        decision = SimpleNamespace(score=16.0, threshold=16.0, sources=1)
        ev = self._event()
        ev.theater = "NATURE"
        text = await self.alerts._compose(
            L3, "NATURE", None, ev, decision, None, hit, 9)
        self.assertIn("Что делать: Следуйте указаниям местных служб.", text)

    async def test_bluf_structural_horizon(self) -> None:
        hit = TripwireHit(
            id="TW-PAN-PHEIC", level=L3, why="ЧС ВОЗ.",
            one_liner="ВОЗ объявила PHEIC", confirmed=True,
            confirm_reason="официальный источник (tier 0)", horizon="structural",
        )
        decision = SimpleNamespace(score=16.0, threshold=16.0, sources=1)
        ev = self._event()
        ev.theater = "PANDEMIC"
        text = await self.alerts._compose(
            L3, "PANDEMIC", None, ev, decision, None, hit, 10)
        self.assertIn("· СТРАТЕГИЧЕСКИЙ ·", text)
        self.assertNotIn("ТАКТИЧЕСКИЙ", text)
        self.assertIn("Следуйте рекомендациям органов здравоохранения", text)

    async def test_correlation_refute_has_no_next_tripwire(self) -> None:
        decision = SimpleNamespace(score=9.0, threshold=8.0, sources=2)
        text = await self.alerts._compose(
            L2, "ISRAEL", None, self._event(), decision, None, None, 11)
        self.assertIn("Что делать: Не планируйте поездки", text)
        self.assertIn(
            "Что опровергнет: счёт театра ниже порога WATCH и удержание 6 ч.", text)
        self.assertNotIn("Следующий tripwire:", text)
        self.assertIn("· ТАКТИЧЕСКИЙ ·", text)


class RuntimeGuardTests(unittest.IsolatedAsyncioTestCase):
    def test_runner_detection(self) -> None:
        self.assertTrue(_is_test_runner("unittest.__main__", ""))
        self.assertTrue(_is_test_runner("pytest", ""))
        self.assertTrue(_is_test_runner("", "tests/test_theaters.py"))
        self.assertTrue(_is_test_runner("", "/usr/local/bin/pytest"))
        self.assertTrue(_is_test_runner("", "C:/Python312/Scripts/pytest.exe"))
        self.assertFalse(
            _is_test_runner("tripwire.__main__", "/opt/tripwire/tripwire/__main__.py"))
        self.assertFalse(_is_test_runner("", "/usr/local/bin/tripwire"))

    async def test_live_credentials_are_ignored_under_tests(self) -> None:
        secrets = {"TELEGRAM_BOT_TOKEN": "123:live", "TELEGRAM_CHAT_ID": "-100"}
        cfg = SimpleNamespace(
            telegram_cfg={},
            secret=lambda key, default=None: secrets.get(key, default),
        )
        client = TelegramClient(cfg)
        try:
            self.assertFalse(client.configured)
        finally:
            await client.close()


if __name__ == "__main__":
    unittest.main()
