"""Translation layer: caching, language skips, fallback and alert integration."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx

from tripwire.app import TripwireApp
from tripwire.config import Config
from tripwire.db import DB
from tripwire.models import Event
from tripwire.translate import Translator
from tripwire.util import utcnow


class FakeProviders:
    def __init__(self, google_status: int = 200, mymemory_status: int = 200) -> None:
        self.google_status = google_status
        self.mymemory_status = mymemory_status
        self.google_calls = 0
        self.mymemory_calls = 0
        self.google_texts: list[str] = []
        self.mymemory_queries: list[str] = []
        self.mymemory_pairs: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "clients5.google.com":
            self.google_calls += 1
            texts = request.url.params.get_list("q")
            self.google_texts = texts
            if self.google_status >= 400:
                return httpx.Response(self.google_status, text="error")
            return httpx.Response(200, json=[[f"G: {text}", "en"] for text in texts])
        if host == "api.mymemory.translated.net":
            self.mymemory_calls += 1
            query = request.url.params.get("q", "")
            self.mymemory_queries.append(query)
            self.mymemory_pairs.append(request.url.params.get("langpair", ""))
            if self.mymemory_status >= 400:
                return httpx.Response(200, json={
                    "responseData": {"translatedText": "QUERY LENGTH LIMIT EXCEEDED"},
                    "responseStatus": self.mymemory_status,
                })
            return httpx.Response(200, json={
                "responseData": {"translatedText": f"M: {query}"},
                "responseStatus": 200,
            })
        return httpx.Response(500)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


class TranslatorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.tmp.name) / "t.db")
        await self.db.open()
        self.cfg = Config(Path(__file__).resolve().parent.parent).load()

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.tmp.cleanup()

    def make(self, fake: FakeProviders, **overrides) -> Translator:
        self.cfg.app_cfg["translate"] = {
            **self.cfg.app_cfg.get("translate", {}),
            "enabled": True,
            **overrides,
        }
        return Translator(self.cfg, self.db, transport=fake.transport)

    async def test_disabled_keeps_original(self) -> None:
        fake = FakeProviders()
        translator = self.make(fake, enabled=False)
        result = await translator.translate_many([("Missile strike on Kyiv", "en")])
        self.assertEqual(result, ["Missile strike on Kyiv"])
        self.assertEqual(fake.google_calls, 0)
        self.assertFalse(translator.active)

    async def test_translates_batch_via_google_and_caches(self) -> None:
        fake = FakeProviders()
        translator = self.make(fake)
        items = [("Missile strike on Kyiv", "en"), ("Airspace closed", "en")]
        first = await translator.translate_many(items)
        self.assertEqual(first, ["G: Missile strike on Kyiv", "G: Airspace closed"])
        self.assertEqual(fake.google_calls, 1)
        self.assertEqual(fake.mymemory_calls, 0)
        self.assertEqual(fake.google_texts, ["Missile strike on Kyiv", "Airspace closed"])

        second = await translator.translate_many(items)
        self.assertEqual(second, first)
        self.assertEqual(fake.google_calls, 1)
        self.assertEqual(translator.cache_hits, 2)
        self.assertEqual(translator.provider_hits["google"], 2)
        self.assertEqual(await self.db.translation_count(), 2)

    async def test_google_failure_falls_back_to_mymemory(self) -> None:
        fake = FakeProviders(google_status=500)
        translator = self.make(fake)
        items = [("Missile strike on Kyiv", "en"), ("Airspace closed", "en")]
        first = await translator.translate_many(items)
        self.assertEqual(first, ["M: Missile strike on Kyiv", "M: Airspace closed"])
        self.assertEqual(fake.google_calls, 1)
        self.assertEqual(fake.mymemory_calls, 2)
        self.assertEqual(translator.failures, 1)
        self.assertEqual(translator.provider_hits["mymemory"], 2)
        self.assertEqual(await self.db.translation_count(), 2)

        second = await translator.translate_many(items)
        self.assertEqual(second, first)
        self.assertEqual(fake.google_calls, 1)
        self.assertEqual(fake.mymemory_calls, 2)
        self.assertEqual(translator.cache_hits, 2)

    async def test_both_providers_fail_keeps_original(self) -> None:
        fake = FakeProviders(google_status=500, mymemory_status=403)
        translator = self.make(fake)
        result = await translator.translate_many([("Missile strike on Kyiv", "en")])
        self.assertEqual(result, ["Missile strike on Kyiv"])
        self.assertEqual(translator.failures, 2)
        self.assertEqual(await self.db.translation_count(), 0)

    async def test_skips_source_and_detected_russian(self) -> None:
        fake = FakeProviders()
        translator = self.make(fake)
        result = await translator.translate_many([
            ("Удар по Киеву", "uk"),
            ("Массированный удар", "en"),
            ("", "en"),
        ])
        self.assertEqual(result, ["Удар по Киеву", "Массированный удар", ""])
        self.assertEqual(fake.google_calls, 0)
        self.assertEqual(fake.mymemory_calls, 0)

    async def test_char_cap_leaves_overflow_original(self) -> None:
        fake = FakeProviders()
        translator = self.make(fake, max_chars=10)
        result = await translator.translate_many([
            ("Short text", "en"), ("This one is too long", "en"),
        ])
        self.assertEqual(result, ["G: Short text", "This one is too long"])
        self.assertEqual(fake.google_texts, ["Short text"])

    async def test_mymemory_chunks_long_text(self) -> None:
        fake = FakeProviders()
        translator = self.make(fake, providers=["mymemory"], max_chars=2000)
        text = "word " * 150
        result = await translator.translate_many([(text, "en")])
        self.assertEqual(fake.mymemory_calls, 2)
        self.assertTrue(all(len(q) <= 480 for q in fake.mymemory_queries))
        self.assertTrue(result[0].startswith("M: word"))
        self.assertGreater(len(result[0]), len(text))

    async def test_mymemory_uses_source_language(self) -> None:
        fake = FakeProviders()
        translator = self.make(fake, providers=["mymemory"])
        await translator.translate_many([("Missile strike on Kyiv", "de")])
        self.assertEqual(fake.mymemory_pairs, ["de|ru"])

    async def test_unknown_provider_is_ignored(self) -> None:
        fake = FakeProviders()
        translator = self.make(fake, providers=["google", "deepl"])
        self.assertEqual(translator.providers, ["google"])
        result = await translator.translate_many([("Missile strike on Kyiv", "en")])
        self.assertEqual(result, ["G: Missile strike on Kyiv"])
        self.assertIn("google → RU", await translator.status_line())

    async def test_status_line(self) -> None:
        fake = FakeProviders()
        translator = self.make(fake)
        self.assertIn("google/mymemory", await translator.status_line())
        await translator.translate_many([("Missile strike on Kyiv", "en")])
        line = await translator.status_line()
        self.assertIn("кэш 1", line)
        self.assertIn("запросов 1", line)
        self.assertIn("google 1", line)


class AlertTranslationTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_compose_translates_one_liner_and_source_title(self) -> None:
        fake = FakeProviders()
        self.app.cfg.app_cfg["translate"] = {
            **self.app.cfg.app_cfg.get("translate", {}), "enabled": True,
        }
        self.app.alerts.translator = Translator(
            self.app.cfg, self.app.db, transport=fake.transport,
        )
        event = Event(
            source_id="gnews_nato", tier=1, title="Missile strike on Kyiv",
            category="eu_ru", theater="EU_RU", lang="en", ingested_at=utcnow(),
        )
        text = await self.app.alerts._compose(
            3, "EU_RU", None, event, SimpleNamespace(score=12.0, threshold=8.0),
            None, None, 1,
        )
        self.assertIn("G: Missile strike on Kyiv", text)
        self.assertEqual(fake.google_calls, 1)
        self.assertEqual(fake.google_texts, ["Missile strike on Kyiv"])
