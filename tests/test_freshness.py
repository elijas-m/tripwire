"""Deterministic freshness gate: URL resolution, date extraction, fail-open."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx

from tripwire.db import DB
from tripwire.freshness import FreshnessChecker, extract_dates
from tripwire.models import Event
from tripwire.util import utcnow

NEWS_URL = "https://news.google.com/rss/articles/CBMitestarticle?oc=5"
VIDEO_URL = "https://example.com/video/news/idf-ground-incursion"


def cfg(**conf) -> SimpleNamespace:
    base = {"enabled": True, "mode": "enforce", "timeout_s": 1}
    return SimpleNamespace(
        app={"user_agent": "Tripwire/Test"},
        app_cfg={"alert_freshness": {**base, **conf}},
    )


def event(url: str = NEWS_URL) -> Event:
    return Event(source_id="gnews_test", tier=2, url=url, title="IDF ground incursion",
                 category="israel", theater="ISRAEL")


def html_with(*, published="", modified="", uploaded="", kind="NewsArticle") -> str:
    data = {"@context": "https://schema.org", "@type": kind}
    if published:
        data["datePublished"] = published
    if modified:
        data["dateModified"] = modified
    if uploaded:
        data["uploadDate"] = uploaded
    return f'<html><head><script type="application/ld+json">{json.dumps(data)}</script></head></html>'


def google_handler(article_url: str, article_html: str):
    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "news.google.com" and request.method == "POST":
            assert "Fbv4je" in request.content.decode()
            inner = json.dumps(["garturlres", article_url, "x"],
                               separators=(",", ":")).replace('"', '\\"')
            return httpx.Response(200, text=f')]}}\'\n[["wrb.fr","Fbv4je","{inner}"]]')
        if request.url.path.startswith("/rss/articles/"):
            return httpx.Response(
                200,
                text='<html><div data-n-a-ts="1700000000" data-n-a-sg="sig"></div></html>',
                headers={"content-type": "text/html"},
            )
        if str(request.url).startswith(article_url):
            return httpx.Response(200, text=article_html,
                                  headers={"content-type": "text/html; charset=utf-8"})
        return httpx.Response(404)
    return handler


class FreshnessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.tmp.name) / "t.db")
        await self.db.open()

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.tmp.cleanup()

    async def check(self, checker: FreshnessChecker, ev: Event | None = None,
                    level: int = 3):
        return await checker.check(
            ev or event(), level=level, theater="ISRAEL", trigger="TW-IL",
            claim="IDF ground operation in Gaza",
        )

    async def test_stale_video_is_blocked_and_audited(self) -> None:
        page = html_with(kind="VideoObject", published="2026-11-03T15:09:02-05:00",
                         modified="2023-01-15", uploaded="2014-07-18")
        checker = FreshnessChecker(
            cfg(), self.db, transport=httpx.MockTransport(google_handler(VIDEO_URL, page)))
        result = await self.check(checker)
        self.assertEqual(result.status, "stale")
        self.assertEqual(result.resolved_url, VIDEO_URL)
        self.assertIn("2014-07-18", result.reason)
        self.assertTrue(result.enforced)

        row = await self.db.fetchone(
            "SELECT * FROM alert_reviews WHERE model='deterministic-freshness'")
        self.assertEqual(row["verdict"], "stale")
        self.assertEqual(row["resolved_url"], VIDEO_URL)
        self.assertEqual(row["current"], 0)

    async def test_fresh_article_passes(self) -> None:
        recent = (utcnow() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        page = html_with(published=recent)
        checker = FreshnessChecker(
            cfg(), self.db, transport=httpx.MockTransport(google_handler(VIDEO_URL, page)))
        result = await self.check(checker)
        self.assertEqual(result.status, "fresh")
        row = await self.db.fetchone(
            "SELECT * FROM alert_reviews WHERE model='deterministic-freshness'")
        self.assertEqual(row["verdict"], "fresh")
        self.assertEqual(row["current"], 1)

    async def test_old_published_date_is_blocked(self) -> None:
        page = html_with(published="2014-07-18T12:00:00Z")
        checker = FreshnessChecker(
            cfg(), self.db, transport=httpx.MockTransport(google_handler(VIDEO_URL, page)))
        result = await self.check(checker)
        self.assertEqual(result.status, "stale")
        self.assertIn("2014-07-18", result.reason)

    async def test_missing_dates_fail_open(self) -> None:
        page = "<html><body>No dates here</body></html>"
        checker = FreshnessChecker(
            cfg(), self.db, transport=httpx.MockTransport(google_handler(VIDEO_URL, page)))
        result = await self.check(checker)
        self.assertEqual(result.status, "unknown")

    async def test_http_error_fails_open(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503)

        checker = FreshnessChecker(cfg(), self.db, transport=httpx.MockTransport(handler))
        result = await self.check(checker, event("https://example.com/news/x"))
        self.assertEqual(result.status, "unknown")

    async def test_shadow_mode_records_but_does_not_enforce(self) -> None:
        page = html_with(published="2014-07-18T12:00:00Z")
        checker = FreshnessChecker(
            cfg(mode="shadow"), self.db,
            transport=httpx.MockTransport(google_handler(VIDEO_URL, page)))
        result = await self.check(checker)
        self.assertEqual(result.status, "stale")
        self.assertFalse(result.enforced)

    async def test_disabled_skips_without_requests(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, text="")

        checker = FreshnessChecker(
            cfg(enabled=False), self.db, transport=httpx.MockTransport(handler))
        result = await self.check(checker)
        self.assertEqual(result.status, "skip")
        self.assertEqual(calls, 0)

    async def test_url_date_is_fallback(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html>plain</html>",
                                  headers={"content-type": "text/html"})

        checker = FreshnessChecker(cfg(), self.db, transport=httpx.MockTransport(handler))
        result = await self.check(checker, event("https://example.com/2014/07/18/story"))
        self.assertEqual(result.status, "stale")
        self.assertIn("2014-07-18", result.reason)


class DateExtractionTests(unittest.TestCase):
    def test_cbn_payload_dates(self) -> None:
        page = '<script>var x = {"publish_date":"07-18-2014","update_date":"01-15-2023"}</script>'
        dates = extract_dates(page, "https://example.com/video/news/x")
        self.assertEqual(dates.published.strftime("%Y-%m-%d"), "2014-07-18")
        self.assertEqual(dates.modified.strftime("%Y-%m-%d"), "2023-01-15")
        self.assertTrue(dates.video_page)

    def test_jsonld_graph_and_meta(self) -> None:
        page = (
            '<meta property="article:published_time" content="2026-11-03T15:09:02-05:00">'
            '<script type="application/ld+json">'
            '{"@graph":[{"@type":"NewsArticle","dateModified":"2023-01-15"}]}'
            "</script>"
        )
        dates = extract_dates(page, "https://example.com/news/x")
        self.assertEqual(dates.published.strftime("%Y-%m-%d"), "2026-11-03")
        self.assertEqual(dates.modified.strftime("%Y-%m-%d"), "2023-01-15")
        self.assertIn("meta:published", dates.source)


if __name__ == "__main__":
    unittest.main()
