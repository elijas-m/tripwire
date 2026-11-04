"""Telegram Bot API client with throttling, retries and ntfy fallback."""

from __future__ import annotations

import asyncio
import html
import os
import sys
import time
from collections import deque
from typing import Any

import httpx


def _is_test_runner(spec_name: str, argv0: str) -> bool:
    if spec_name in ("unittest.__main__", "pytest", "pytest.__main__"):
        return True
    name = os.path.basename(argv0).lower()
    if name.endswith(".exe"):
        name = name[:-4]
    if name in ("pytest", "py.test"):
        return True
    return name.startswith("test_") and name.endswith(".py")


def _under_tests() -> bool:
    """True inside unittest/pytest, including ``discover -s tests``.

    That invocation imports test modules as top-level packages, so
    ``tests/__init__.py`` (which points at the test secrets file) never runs.
    Detection here keeps the live bot token out of any test process.
    """
    if os.environ.get("TRIPWIRE_TESTING"):
        return True
    main = sys.modules.get("__main__")
    spec = getattr(getattr(main, "__spec__", None), "name", "") or ""
    return _is_test_runner(spec, sys.argv[0] if sys.argv else "")


class TelegramError(RuntimeError):
    pass


class TelegramClient:
    def __init__(self, cfg) -> None:
        self.cfg = cfg
        token = cfg.secret(cfg.telegram_cfg.get("token_env", "TELEGRAM_BOT_TOKEN"), "") or ""
        chat_id = cfg.secret("TELEGRAM_CHAT_ID", "") or ""
        if _under_tests():
            token = chat_id = ""
        self.token = token
        self.api_base = cfg.telegram_cfg.get("api_base", "https://api.telegram.org").rstrip("/")
        self.chat_id = chat_id
        self.rate_per_min = int(cfg.telegram_cfg.get("rate_per_min", 20))
        self.poll_timeout = int(cfg.telegram_cfg.get("poll_timeout_s", 50))
        self._sent: deque[float] = deque()
        self._lock = asyncio.Lock()
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(60.0), follow_redirects=True)
        self.failures = 0
        self.backup_fail_threshold = int(cfg.telegram_cfg.get("backup_fail_threshold", 3))

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    async def close(self) -> None:
        await self._client.aclose()

    async def _throttle(self) -> None:
        async with self._lock:
            now = time.monotonic()
            while self._sent and now - self._sent[0] > 60:
                self._sent.popleft()
            if len(self._sent) >= self.rate_per_min:
                wait = 60 - (now - self._sent[0]) + 0.05
                if wait > 0:
                    await asyncio.sleep(wait)
            self._sent.append(time.monotonic())

    async def call(self, method: str, payload: dict[str, Any], retries: int = 3) -> dict[str, Any]:
        if not self.token:
            raise TelegramError("TELEGRAM_BOT_TOKEN is not set")
        url = f"{self.api_base}/bot{self.token}/{method}"
        attempt = 0
        while True:
            await self._throttle()
            try:
                response = await self._client.post(url, json=payload)
                data = response.json()
                if response.status_code == 200 and data.get("ok"):
                    self.failures = 0
                    return data.get("result") or {}
                retry_after = (data.get("parameters") or {}).get("retry_after")
                if response.status_code == 429 and retry_after and attempt < retries:
                    await asyncio.sleep(min(float(retry_after), 60.0))
                    attempt += 1
                    continue
                if response.status_code >= 500 and attempt < retries:
                    await asyncio.sleep(min(2 ** attempt, 10))
                    attempt += 1
                    continue
                self.failures += 1
                raise TelegramError(f"{method}: HTTP {response.status_code} {data.get('description', '')}")
            except (httpx.HTTPError, ValueError) as exc:
                if attempt < retries:
                    await asyncio.sleep(min(2 ** attempt, 10))
                    attempt += 1
                    continue
                self.failures += 1
                raise TelegramError(f"{method}: {exc}") from exc

    # ---------- messages ----------
    async def send_message(
        self, text: str, thread_id: int | str | None = None, *,
        disable_notification: bool = False, reply_to: int | None = None,
        chat_id: str | None = None,
    ) -> int:
        payload: dict[str, Any] = {
            "chat_id": chat_id or self.chat_id,
            "text": text[:4096],
            "parse_mode": self.cfg.telegram_cfg.get("parse_mode", "HTML"),
            "disable_web_page_preview": bool(self.cfg.telegram_cfg.get("disable_web_page_preview", True)),
            "disable_notification": disable_notification,
        }
        if thread_id:
            payload["message_thread_id"] = int(thread_id)
        if reply_to:
            payload["reply_to_message_id"] = int(reply_to)
            payload["allow_sending_without_reply"] = True
        result = await self.call("sendMessage", payload)
        return int(result.get("message_id", 0))

    async def edit_message(self, message_id: int, text: str, chat_id: str | None = None) -> bool:
        try:
            await self.call("editMessageText", {
                "chat_id": chat_id or self.chat_id,
                "message_id": int(message_id),
                "text": text[:4096],
                "parse_mode": self.cfg.telegram_cfg.get("parse_mode", "HTML"),
                "disable_web_page_preview": bool(self.cfg.telegram_cfg.get("disable_web_page_preview", True)),
            })
            return True
        except TelegramError:
            return False

    async def pin(self, message_id: int, chat_id: str | None = None) -> bool:
        try:
            await self.call("pinChatMessage", {
                "chat_id": chat_id or self.chat_id,
                "message_id": int(message_id),
                "disable_notification": True,
            })
            return True
        except TelegramError:
            return False

    async def get_updates(self, offset: int | None) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": self.poll_timeout,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            payload["offset"] = offset
        result = await self.call("getUpdates", payload)
        return result if isinstance(result, list) else []

    async def set_commands(self, commands: list[dict[str, str]]) -> None:
        try:
            await self.call("setMyCommands", {"commands": commands})
        except TelegramError:
            pass

    async def send_backup(self, text: str, critical: bool = False) -> bool:
        if _under_tests():
            return False
        backup = self.cfg.channels.get("backup", {}).get("ntfy", {})
        if not backup.get("enabled"):
            return False
        topic = self.cfg.secret(backup.get("topic_env", "NTFY_TOPIC"), "")
        if not topic:
            return False
        base = backup.get("base_url", "https://ntfy.sh").rstrip("/")
        headers = {
            "Title": "Tripwire CRITICAL" if critical else "Tripwire WARNING",
            "Priority": str(backup.get("priority_critical", 5) if critical else backup.get("priority_warning", 4)),
            "Tags": "warning" if critical else "triangular_flag_on_post",
        }
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.post(f"{base}/{topic}", content=text.encode("utf-8"), headers=headers)
                return response.status_code < 300
        except httpx.HTTPError:
            return False


def esc(text: str) -> str:
    return html.escape(text or "", quote=False)
