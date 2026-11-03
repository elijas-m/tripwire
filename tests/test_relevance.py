"""Pre-send relevance gate: verdicts, fail-open behavior, audit log."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx

from tripwire.alerts import AlertManager
from tripwire.config import Config
from tripwire.db import DB
from tripwire.models import L2, L3, Event, TripwireHit
from tripwire.relevance import AlertReviewer, ReviewResult
from tripwire.util import utcnow

CFG = Config(Path(__file__).resolve().parent.parent).load()


class FakeTelegram:
    configured = True
    failures = 0
    backup_fail_threshold = 3

    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_message(self, text, thread_id=None, **kwargs) -> int:
        self.messages.append(text)
        return len(self.messages)

    async def edit_message(self, message_id, text, **kwargs) -> bool:
        return True

    async def pin(self, message_id: int) -> bool:
        return True


def make_cfg(**conf) -> SimpleNamespace:
    base = {"enabled": True, "model": "test/model:free", "timeout_s": 1}
    return SimpleNamespace(
        app_cfg={"alert_review": {**base, **conf}},
        secrets={"OPENROUTER_API_KEY": "test-key"},
    )


def event() -> Event:
    return Event(
        source_id="gnews_who", tier=0,
        title=("WHO Director-General's opening remarks at the Ninth meeting of the "
               "Intergovernmental Working Group (IGWG) on the WHO Pandemic Agreement"),
        body_excerpt="Opening remarks at the ninth IGWG meeting on the pandemic agreement.",
        category="pandemic", theater="PANDEMIC", ingested_at=utcnow(),
    )


def tripwire() -> TripwireHit:
    return TripwireHit(
        id="TW-PAN-PANDEMIC", level=L3,
        why="ВОЗ объявила пандемию — глобальная чрезвычайная ситуация.",
        one_liner="ВОЗ объявила пандемию",
    )


def response(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


class ReviewerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.tmp.name) / "t.db")
        await self.db.open()

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.tmp.cleanup()

    async def review(self, reviewer: AlertReviewer) -> ReviewResult:
        ev = event()
        ev.id = 13991
        return await reviewer.review(
            event=ev, cluster=None, level=L3, theater="PANDEMIC",
            tripwire=tripwire(), claim="ВОЗ объявила пандемию",
            why="ВОЗ объявила пандемию — глобальная чрезвычайная ситуация.",
        )

    async def test_reject_is_recorded(self) -> None:
        seen: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content))
            return response(json.dumps({"relevant": False, "reason": "meeting, not a declaration"}))

        reviewer = AlertReviewer(make_cfg(), self.db, transport=httpx.MockTransport(handler))
        result = await self.review(reviewer)
        self.assertEqual(result.verdict, "reject")
        self.assertTrue(result.enforced)
        self.assertEqual(result.reason, "meeting, not a declaration")
        self.assertEqual(seen[0]["model"], "test/model:free")

        row = await self.db.fetchone("SELECT * FROM alert_reviews")
        self.assertEqual(row["verdict"], "reject")
        self.assertEqual(row["mode"], "enforce")
        self.assertEqual(row["event_id"], 13991)
        self.assertEqual(row["trigger"], "TW-PAN-PANDEMIC")
        self.assertEqual(reviewer.stats["rejected"], 1)

    async def test_pass_is_recorded(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return response(json.dumps({"relevant": True, "reason": "declaration reported"}))

        reviewer = AlertReviewer(make_cfg(), self.db, transport=httpx.MockTransport(handler))
        result = await self.review(reviewer)
        self.assertEqual(result.verdict, "pass")
        row = await self.db.fetchone("SELECT * FROM alert_reviews")
        self.assertEqual(row["verdict"], "pass")

    async def test_http_error_fails_open(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="upstream down")

        reviewer = AlertReviewer(make_cfg(), self.db, transport=httpx.MockTransport(handler))
        result = await self.review(reviewer)
        self.assertEqual(result.verdict, "error")
        self.assertEqual(reviewer.stats["errors"], 1)
        row = await self.db.fetchone("SELECT verdict FROM alert_reviews")
        self.assertEqual(row["verdict"], "error")

    async def test_timeout_fails_open(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow model", request=request)

        reviewer = AlertReviewer(make_cfg(), self.db, transport=httpx.MockTransport(handler))
        result = await self.review(reviewer)
        self.assertEqual(result.verdict, "error")
        self.assertIn("ReadTimeout", result.reason)

    async def test_invalid_json_fails_open(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return response("I think this one is probably fine.")

        reviewer = AlertReviewer(make_cfg(), self.db, transport=httpx.MockTransport(handler))
        result = await self.review(reviewer)
        self.assertEqual(result.verdict, "error")

    async def test_missing_key_skips_without_request(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return response('{"relevant": false, "reason": "x"}')

        cfg = make_cfg()
        cfg.secrets = {}
        reviewer = AlertReviewer(cfg, self.db, transport=httpx.MockTransport(handler))
        result = await self.review(reviewer)
        self.assertEqual(result.verdict, "skip")
        self.assertEqual(calls, 0)
        row = await self.db.fetchone("SELECT COUNT(*) AS n FROM alert_reviews")
        self.assertEqual(int(row["n"]), 0)

    async def test_shadow_mode_does_not_enforce(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return response(json.dumps({"relevant": False, "reason": "procedural"}))

        reviewer = AlertReviewer(
            make_cfg(mode="shadow"), self.db, transport=httpx.MockTransport(handler))
        result = await self.review(reviewer)
        self.assertEqual(result.verdict, "reject")
        self.assertFalse(result.enforced)
        row = await self.db.fetchone("SELECT mode FROM alert_reviews")
        self.assertEqual(row["mode"], "shadow")


class StubReviewer:
    def __init__(self, result: ReviewResult) -> None:
        self.result = result
        self.calls = 0

    async def review(self, **kwargs) -> ReviewResult:
        self.calls += 1
        return self.result


class AlertManagerReviewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.tmp.name) / "t.db")
        await self.db.open()
        self.tg = FakeTelegram()
        self.decision = SimpleNamespace(
            cluster_level=L3, theater="PANDEMIC", score=25.5, threshold=16.0)

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.tmp.cleanup()

    def manager(self, reviewer) -> AlertManager:
        manager = AlertManager(CFG, self.db, self.tg, reviewer=reviewer)
        self.assertEqual(manager.tg, self.tg)
        return manager

    async def test_rejected_alert_is_not_sent(self) -> None:
        reviewer = StubReviewer(ReviewResult(
            verdict="reject", reason="meeting, not a declaration",
            model="stub", enforced=True))
        manager = self.manager(reviewer)
        result = await manager._send_new(
            self.decision, None, event(), L3, "PANDEMIC", None, tripwire(),
            escalate=False)
        self.assertEqual(result["action"], "rejected")
        self.assertEqual(reviewer.calls, 1)
        self.assertEqual(self.tg.messages, [])
        row = await self.db.fetchone("SELECT COUNT(*) AS n FROM alerts")
        self.assertEqual(int(row["n"]), 0)

    async def test_provider_error_sends_alert(self) -> None:
        reviewer = StubReviewer(ReviewResult(
            verdict="error", reason="HTTP 503", model="stub", enforced=True))

        async def fake_translate(items):
            return [text or "" for text, _lang in items]

        manager = self.manager(reviewer)
        manager.translator.translate_many = fake_translate
        result = await manager._send_new(
            self.decision, None, event(), L3, "PANDEMIC", None, tripwire(),
            escalate=False)
        self.assertEqual(result["action"], "sent")
        self.assertEqual(len(self.tg.messages), 1)
