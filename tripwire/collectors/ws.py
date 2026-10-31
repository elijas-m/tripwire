"""EMSC WebSocket collector with reconnect."""

from __future__ import annotations

import asyncio
import json
import random
from typing import Awaitable, Callable

import websockets

from ..fetch import FetchResult
from ..models import RawItem, Source
from ..parsers import parse_emsc_feature
from .base import CollectorResult


class WSCollector:
    def __init__(self, cfg) -> None:
        self.cfg = cfg

    async def run(self, source: Source, fetcher=None, health=None) -> CollectorResult:
        # WS sources are long-lived; the scheduler uses run_stream instead.
        return CollectorResult(source.id, error="websocket requires run_stream")

    async def run_stream(
        self,
        source: Source,
        on_items: Callable[[list[RawItem]], Awaitable[None]],
        stop: asyncio.Event,
    ) -> None:
        backoff = 5.0
        while not stop.is_set():
            try:
                async with websockets.connect(
                    source.url, ping_interval=30, ping_timeout=20, close_timeout=5,
                    max_size=2 * 1024 * 1024,
                ) as ws:
                    backoff = 5.0
                    while not stop.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=60)
                        except asyncio.TimeoutError:
                            await ws.ping()
                            continue
                        items = self._parse_message(source, raw)
                        if items:
                            await on_items(items)
            except asyncio.CancelledError:
                raise
            except Exception:
                if stop.is_set():
                    return
                await asyncio.sleep(backoff + random.uniform(0, 2))
                backoff = min(backoff * 2, 120.0)

    def _parse_message(self, source: Source, raw: str | bytes) -> list[RawItem]:
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return []
        if isinstance(data, list):
            messages = data
        else:
            messages = [data]
        out = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            action = (msg.get("action") or "").lower()
            if action and action not in ("create", "update", "insert"):
                continue
            feature = msg.get("data") or msg
            if not isinstance(feature, dict) or "properties" not in feature:
                continue
            item = parse_emsc_feature(source, feature)
            if item:
                out.append(item)
        return out
