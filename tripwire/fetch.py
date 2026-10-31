"""HTTP transport with conditional GET, retries and byte accounting."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .models import Source

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


@dataclass(slots=True)
class FetchResult:
    ok: bool = False
    status: int = 0
    not_modified: bool = False
    content: bytes = b""
    text: str = ""
    headers: dict[str, str] | None = None
    latency_ms: float = 0.0
    error: str = ""
    wire_bytes: int = 0
    retries: int = 0

    @property
    def etag(self) -> str | None:
        return (self.headers or {}).get("etag")

    @property
    def last_modified(self) -> str | None:
        return (self.headers or {}).get("last-modified")


class Fetcher:
    def __init__(self, cfg: Any) -> None:
        fetch_cfg = cfg.app.get("fetch", {})
        self.timeout = float(fetch_cfg.get("timeout_s", 25))
        self.retries = int(fetch_cfg.get("retries", 2))
        self.user_agent = cfg.app.get("user_agent", "Tripwire/1.0")
        limits = httpx.Limits(
            max_connections=int(fetch_cfg.get("max_connections", 16)),
            max_keepalive_connections=int(fetch_cfg.get("max_keepalive", 8)),
        )
        self.client = httpx.AsyncClient(
            http2=True,
            limits=limits,
            timeout=httpx.Timeout(self.timeout),
            follow_redirects=True,
            headers={"User-Agent": self.user_agent},
        )
        self.wire_bytes_total = 0
        self.requests_total = 0
        self.not_modified_total = 0

    async def close(self) -> None:
        await self.client.aclose()

    async def fetch(self, source: Source, health: dict[str, Any] | None = None) -> FetchResult:
        health = health or {}
        headers = dict(source.headers)
        if source.method == "GET":
            if health.get("etag"):
                headers["If-None-Match"] = str(health["etag"])
            if health.get("last_modified"):
                headers["If-Modified-Since"] = str(health["last_modified"])

        result = FetchResult(headers={})
        attempt = 0
        while attempt <= self.retries:
            started = time.monotonic()
            try:
                response = await self.client.request(
                    source.method,
                    source.url,
                    headers=headers,
                    content=source.body.encode("utf-8") if source.body else None,
                    timeout=httpx.Timeout(source.timeout_s),
                )
                elapsed = (time.monotonic() - started) * 1000
                result.latency_ms = elapsed
                result.status = response.status_code
                result.headers = {k.lower(): v for k, v in response.headers.items()}
                raw = response.content
                result.content = raw
                # Count bytes on the wire, not the decompressed payload.
                wire = response.num_bytes_downloaded or len(raw)
                result.wire_bytes = wire
                self.requests_total += 1
                self.wire_bytes_total += wire

                if response.status_code == 304:
                    result.ok = True
                    result.not_modified = True
                    self.not_modified_total += 1
                    return result
                if 200 <= response.status_code < 300:
                    result.ok = True
                    try:
                        result.text = response.text
                    except Exception:
                        result.text = raw.decode("utf-8", "replace")
                    return result
                if response.status_code in RETRYABLE_STATUS and attempt < self.retries:
                    delay = self._retry_delay(response, attempt)
                    result.error = f"HTTP {response.status_code}"
                    await asyncio.sleep(delay)
                    attempt += 1
                    continue
                result.error = f"HTTP {response.status_code}"
                return result
            except (httpx.TimeoutException, httpx.NetworkError, httpx.ProtocolError, httpx.HTTPError) as exc:
                elapsed = (time.monotonic() - started) * 1000
                result.latency_ms = elapsed
                result.error = f"{type(exc).__name__}: {exc}"[:300]
                if attempt < self.retries:
                    await asyncio.sleep(min(8.0, 1.5 * (2 ** attempt)))
                    attempt += 1
                    continue
                return result
        return result

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("retry-after")
        if retry_after:
            try:
                return min(float(retry_after), 30.0)
            except ValueError:
                pass
        return min(10.0, 1.5 * (2 ** attempt))

    def traffic_mb(self) -> float:
        return self.wire_bytes_total / (1024 * 1024)
