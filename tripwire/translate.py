"""Russian translation of alert text via keyless providers, cached in SQLite."""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any, Sequence

import httpx

from .util import detect_lang

log = logging.getLogger(__name__)

CACHE_VERSION = "v2"
DEFAULT_SKIP_LANGS = ("ru", "uk", "be")
DEFAULT_PROVIDERS = ("google", "mymemory")
PROVIDER_NAMES = ("google", "mymemory")
GOOGLE_URL = "https://clients5.google.com/translate_a/t"
MYMEMORY_URL = "https://api.mymemory.translated.net/get"
MYMEMORY_MAX_CHARS = 480


def _split_text(text: str, limit: int) -> list[str]:
    """Split on word boundaries so every chunk fits the provider's query limit."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}" if current else word
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        while len(word) > limit:
            chunks.append(word[:limit])
            word = word[limit:]
        current = word
    if current:
        chunks.append(current)
    return chunks or [text[:limit]]


class Translator:
    """Translates short alert strings, falling back to the original on any error."""

    def __init__(self, cfg, db, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.cfg = cfg
        self.db = db
        conf: dict[str, Any] = (
            getattr(cfg, "app_cfg", {}).get("translate")
            or cfg.app.get("translate")
            or {}
        )
        self.enabled = bool(conf.get("enabled", False))
        raw_providers = conf.get("providers")
        if raw_providers is None:
            raw_providers = DEFAULT_PROVIDERS
        elif isinstance(raw_providers, str):
            raw_providers = [raw_providers]
        self.providers: list[str] = []
        for name in raw_providers:
            name = str(name).strip().lower()
            if not name:
                continue
            if name not in PROVIDER_NAMES:
                log.warning("unknown translate provider ignored: %s", name)
                continue
            if name not in self.providers:
                self.providers.append(name)
        self.google_url = str(conf.get("google_url") or GOOGLE_URL)
        self.mymemory_url = str(conf.get("mymemory_url") or MYMEMORY_URL)
        self.mymemory_email = str(conf.get("mymemory_email") or "").strip()
        self.target_lang = str(conf.get("target_lang") or "RU").upper()
        self.timeout_s = float(conf.get("timeout_s", 3.0))
        self.max_chars = int(conf.get("max_chars", 1200))
        self.skip_langs = {
            str(item).lower() for item in (conf.get("skip_langs") or DEFAULT_SKIP_LANGS)
        }
        self._transport = transport
        self.requests = 0
        self.failures = 0
        self.cache_hits = 0
        self.provider_hits = {name: 0 for name in self.providers}
        self._last_warn = 0.0

    @property
    def active(self) -> bool:
        return self.enabled and bool(self.providers)

    async def translate_many(
        self, items: Sequence[tuple[str, str | None]],
    ) -> list[str]:
        """Return translations in the same order; untranslatable items stay as-is."""
        results = [(text or "").strip() for text, _lang in items]
        if not self.active:
            return results

        pending: dict[str, list[int]] = {}
        texts: dict[str, str] = {}
        src_langs: dict[str, str] = {}
        used = 0
        for index, (text, lang) in enumerate(items):
            text = (text or "").strip()
            if not self._eligible(text, lang):
                continue
            cached = await self._cached(text)
            if cached:
                results[index] = cached
                self.cache_hits += 1
                continue
            if used + len(text) > self.max_chars:
                continue
            used += len(text)
            key = self._hash(text)
            pending.setdefault(key, []).append(index)
            texts[key] = text
            src_langs[key] = (lang or detect_lang(text)).lower()
        if not pending:
            return results

        remaining = list(pending)
        for provider in self.providers:
            if not remaining:
                break
            entries = [(key, texts[key], src_langs[key]) for key in remaining]
            try:
                translated = await self._request(provider, entries)
                if len(translated) != len(remaining):
                    raise RuntimeError(
                        f"returned {len(translated)}/{len(remaining)} translations"
                    )
            except Exception as exc:
                self.failures += 1
                self._warn(provider, exc)
                continue

            failed: list[str] = []
            for key, value in zip(remaining, translated):
                value = (value or "").strip()
                if not value:
                    failed.append(key)
                    continue
                for index in pending[key]:
                    results[index] = value
                self.provider_hits[provider] += 1
                await self.db.translation_put(
                    self._cache_key(texts[key], provider), provider,
                    self.target_lang, texts[key], value,
                )
            remaining = failed
        return results

    def _eligible(self, text: str, lang: str | None) -> bool:
        if len(text) < 3 or not any(ch.isalpha() for ch in text):
            return False
        if (lang or "").lower() in self.skip_langs:
            return False
        return detect_lang(text) != "ru"

    def _hash(self, text: str) -> str:
        return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()

    def _cache_key(self, text: str, provider: str) -> str:
        raw = f"{CACHE_VERSION}|{provider}|{self.target_lang}|{text}"
        return hashlib.sha256(raw.encode("utf-8", "ignore")).hexdigest()

    async def _cached(self, text: str) -> str | None:
        # Prefer the best provider in the configured order, then lower-tier caches.
        for provider in self.providers:
            value = await self.db.translation_get(self._cache_key(text, provider))
            if value:
                return value
        return None

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout_s), transport=self._transport,
        )

    async def _request(
        self, provider: str, entries: list[tuple[str, str, str]],
    ) -> list[str]:
        if provider == "google":
            return await self._request_google(entries)
        if provider == "mymemory":
            return await self._request_mymemory(entries)
        raise RuntimeError(f"unknown provider {provider}")

    async def _request_google(self, entries: list[tuple[str, str, str]]) -> list[str]:
        params: list[tuple[str, str]] = [
            ("client", "dict-chrome-ex"),
            ("sl", "auto"),
            ("tl", self.target_lang.lower()),
        ]
        params.extend(("q", text) for _key, text, _lang in entries)
        async with self._client() as client:
            response = await client.get(self.google_url, params=params)
        self.requests += 1
        if response.status_code >= 400:
            # Never log the response body: it may echo source text.
            raise RuntimeError(f"Google HTTP {response.status_code}")
        payload = response.json()
        if not isinstance(payload, list):
            raise RuntimeError("Google: unexpected response")
        translations: list[str] = []
        for item in payload:
            if isinstance(item, list) and item:
                translations.append(str(item[0] or ""))
            elif isinstance(item, str):
                translations.append(item)
            else:
                translations.append("")
        return translations

    async def _request_mymemory(self, entries: list[tuple[str, str, str]]) -> list[str]:
        translations: list[str] = []
        async with self._client() as client:
            for _key, text, src_lang in entries:
                parts: list[str] = []
                for piece in _split_text(text, MYMEMORY_MAX_CHARS):
                    params = {
                        "q": piece,
                        "langpair": f"{src_lang}|{self.target_lang.lower()}",
                    }
                    if self.mymemory_email:
                        params["de"] = self.mymemory_email
                    response = await client.get(self.mymemory_url, params=params)
                    self.requests += 1
                    if response.status_code >= 400:
                        raise RuntimeError(f"MyMemory HTTP {response.status_code}")
                    payload = response.json()
                    status = str(payload.get("responseStatus") or "")
                    if status and status != "200":
                        raise RuntimeError(f"MyMemory status {status}")
                    value = str(
                        (payload.get("responseData") or {}).get("translatedText") or ""
                    ).strip()
                    if not value or value.upper().startswith("MYMEMORY WARNING"):
                        raise RuntimeError("MyMemory returned no translation")
                    parts.append(value)
                translations.append(" ".join(parts))
        return translations

    def _warn(self, provider: str, exc: Exception) -> None:
        now = time.monotonic()
        if self.failures == 1 or now - self._last_warn >= 3600:
            log.warning(
                "translation via %s failed (%d total): %s",
                provider, self.failures, str(exc)[:200] or type(exc).__name__,
            )
            self._last_warn = now

    async def status_line(self) -> str:
        if not self.enabled:
            return "Перевод: выключен"
        if not self.providers:
            return "Перевод: нет провайдеров (оригиналы)"
        cached = await self.db.translation_count()
        names = "/".join(self.providers)
        hits = " · ".join(
            f"{name} {count}" for name, count in self.provider_hits.items() if count
        )
        suffix = f" · {hits}" if hits else ""
        return (
            f"Перевод: {names} → {self.target_lang} · кэш {cached} · "
            f"запросов {self.requests} · ошибок {self.failures}{suffix}"
        )
