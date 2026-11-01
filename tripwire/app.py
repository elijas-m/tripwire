"""Tripwire application: collectors, processing pipeline, scheduler."""

from __future__ import annotations

import asyncio
import gzip
import logging
import random
import shutil
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from . import diplo, ews, systemd
from .alerts import AlertManager
from .collectors.base import make_collector
from .commands import CommandService
from .config import Config
from .correlator import Correlator
from .db import DB
from .dedup import Deduper
from .digest import build_digest, source_problems
from .fetch import Fetcher
from .geo import GeoIndex
from .indicators import compute_market_shock
from .models import L0, L1, L2, L3, Event, RawItem, ScoreResult, Source
from .normalize import Normalizer
from .rules.domain import DomainEngine
from .rules.lexicon import LexiconEngine
from .rules.tripwire import TripwireEngine
from .telegram_api import TelegramClient
from .util import UTC, jitter_interval, now_ts, to_ts, utcnow

log = logging.getLogger(__name__)


class TripwireApp:
    def __init__(self, cfg: Config | None = None, *, commands: bool = True) -> None:
        self.cfg = (cfg or Config()).load()
        self.commands_enabled = commands
        self.db = DB(self.cfg.data_dir() / "tripwire.db")
        self.tg = TelegramClient(self.cfg)
        self.fetcher = Fetcher(self.cfg)
        self.geo = GeoIndex(self.cfg)
        self.normalizer = Normalizer(self.cfg, self.geo)
        self.deduper = Deduper(self.cfg, self.geo)
        self.tripwires = TripwireEngine(self.cfg)
        self.domain = DomainEngine(self.cfg, self.geo)
        self.lexicon = LexiconEngine(self.cfg)
        self.correlator = Correlator(self.cfg, self.db)
        self.alerts = AlertManager(self.cfg, self.db, self.tg)
        self.sources = {s.id: s for s in self.cfg.enabled_sources()}
        self.commands = CommandService(self)
        self.stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._commands_task: asyncio.Task | None = None
        self._pipeline_locks: dict[str, asyncio.Lock] = {}
        self.fetch_sem = asyncio.Semaphore(
            int(self.cfg.app.get("fetch", {}).get("max_connections", 16))
        )
        self.stats: Counter[str] = Counter()
        self.started_at = utcnow()
        self.keepalive = False
        self._last_stats_log = time.monotonic()

    # ---------- lifecycle ----------
    async def start(self) -> None:
        await self.db.open()
        await self.db.heartbeat("start", f"sources={len(self.sources)}")
        await self.db.meta_set("started_at", utcnow().isoformat())
        await self.correlator.load_overrides()
        if self.commands_enabled and self.tg.configured and self.cfg.admin_ids():
            self._commands_task = asyncio.create_task(
                self.commands.run(), name="commands"
            )
        systemd.notify("READY=1")
        log.info(
            "tripwire started: %d enabled sources, telegram=%s, db=%s",
            len(self.sources), "configured" if self.tg.configured else "dry-run", self.db.path,
        )

    async def close(self) -> None:
        systemd.notify("STOPPING=1")
        tasks = list(self._tasks)
        if self._commands_task is not None:
            tasks.append(self._commands_task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.fetcher.close()
        await self.tg.close()
        await self.db.close()
        log.info("tripwire stopped; traffic %.1f MB", self.fetcher.traffic_mb())

    def start_tasks(self) -> None:
        for source in self.sources.values():
            if source.type == "ws":
                self._tasks.append(asyncio.create_task(self._ws_loop(source), name=f"ws:{source.id}"))
            else:
                self._tasks.append(asyncio.create_task(self._source_loop(source), name=f"src:{source.id}"))
        for coro, name in (
            (self._pending_loop(), "pending"),
            (self._recompute_loop(), "recompute"),
            (self._heartbeat_loop(), "heartbeat"),
            (self._maintenance_loop(), "maintenance"),
            (self._health_loop(), "health"),
            (self._digest_loop(), "digest"),
            (self._selftest_loop(), "selftest"),
            (self._stats_loop(), "stats"),
        ):
            self._tasks.append(asyncio.create_task(coro, name=name))

    async def run_forever(self) -> None:
        self.start_tasks()
        await self.stop.wait()

    async def reload_config(self) -> bool:
        """SIGHUP / /reload: validate new config, then rebuild engines and source loops."""
        try:
            new_cfg = Config(self.cfg.root).load()
        except Exception as exc:
            log.error("config reload rejected: %s", exc)
            await self._notify_digest(f"⚠️ <b>Конфиг не перезагружен</b>\n{exc}")
            return False

        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

        self.cfg = new_cfg
        self.geo = GeoIndex(new_cfg)
        self.normalizer = Normalizer(new_cfg, self.geo)
        self.deduper = Deduper(new_cfg, self.geo)
        self.tripwires = TripwireEngine(new_cfg)
        self.domain = DomainEngine(new_cfg, self.geo)
        self.lexicon = LexiconEngine(new_cfg)
        self.correlator = Correlator(new_cfg, self.db)
        await self.correlator.load_overrides()
        self.alerts = AlertManager(new_cfg, self.db, self.tg)
        self.sources = {s.id: s for s in new_cfg.enabled_sources()}
        self.start_tasks()
        log.info("config reloaded: %d enabled sources", len(self.sources))
        await self._notify_digest(f"♻️ Конфиг перезагружен: {len(self.sources)} источников")
        return True

    async def _notify_digest(self, text: str) -> None:
        if not self.tg.configured:
            log.info("digest note (dry-run): %s", text)
            return
        try:
            await self.tg.send_message(text, self.cfg.channel_topic("DIGEST"), disable_notification=True)
        except Exception:
            log.exception("failed to send digest note")

    # ---------- scheduling ----------
    async def _sleep(self, seconds: float) -> bool:
        """Sleep unless asked to stop. Returns False when stopping."""
        if seconds <= 0:
            return not self.stop.is_set()
        try:
            await asyncio.wait_for(self.stop.wait(), timeout=seconds)
            return False
        except asyncio.TimeoutError:
            return True

    async def _source_loop(self, source: Source) -> None:
        collector = make_collector(source, self.cfg)
        if not await self._sleep(random.uniform(0, min(45.0, source.interval_s))):
            return
        while not self.stop.is_set():
            started = time.monotonic()
            try:
                await self._run_source(source, collector)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("source %s crashed", source.id)
                try:
                    await self.db.health_error(source.id, "collector crash")
                except Exception:
                    pass
            interval = await self._effective_interval(source)
            wait = jitter_interval(interval, source.jitter) - (time.monotonic() - started)
            if not await self._sleep(max(5.0, wait)):
                return

    async def _effective_interval(self, source: Source) -> int:
        if not source.adaptive:
            return source.interval_s
        try:
            state = await self.db.theater_get(source.theater)
        except Exception:
            return source.interval_s
        level = int(state.get("level") or 0)
        factor = 0.25 if level >= L3 else 0.33 if level >= L2 else 1.0
        return max(20, int(source.interval_s * factor))

    async def _run_source(self, source: Source, collector) -> None:
        health = await self.db.health_get(source.id)
        async with self.fetch_sem:
            result = await collector.run(source, self.fetcher, health)
        if result.error:
            await self.db.health_error(source.id, result.error)
            log.warning("source %s: %s", source.id, result.error)
            return
        fetch = result.fetch
        if fetch is not None and fetch.ok:
            await self.db.health_ok(
                source.id, fetch.latency_ms, len(result.items),
                fetch.etag, fetch.last_modified, fetch.not_modified,
            )
        if result.not_modified:
            self.stats["not_modified"] += 1
            return
        for raw in result.items:
            await self._process_safe(raw, source)

    async def _ws_loop(self, source: Source) -> None:
        collector = make_collector(source, self.cfg)

        async def on_items(items: list[RawItem]) -> None:
            await self.db.health_ok(source.id, 0.0, len(items), None, None)
            for raw in items:
                await self._process_safe(raw, source)

        while not self.stop.is_set():
            try:
                await collector.run_stream(source, on_items, self.stop)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("websocket source %s crashed", source.id)
                await self.db.health_error(source.id, "ws crash")
            await self._sleep(30)

    # ---------- pipeline ----------
    async def _process_safe(self, raw: RawItem, source: Source) -> None:
        try:
            await self.process(raw, source)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("failed to process item from %s: %s", source.id, raw.title[:120])
            self.stats["errors"] += 1

    async def process(self, raw: RawItem, source: Source) -> dict[str, Any]:
        if raw.kind == "market":
            payload = raw.payload or {}
            symbol = str(payload.get("symbol") or raw.guid or source.id)
            ts = to_ts(raw.published_at) or now_ts()
            await self.db.market_add(
                symbol, ts, payload.get("close"), payload.get("change_pct"), source.id,
            )
            shock = await compute_market_shock(
                self.db, settings=self.cfg.g("market_shock"),
            )
            self.stats["market"] += 1
            return {"kind": "market", "symbol": symbol, "shock": shock["value"]}

        if raw.kind == "indicator":
            payload = raw.payload or {}
            value = payload.get("state")
            if not value:
                value = "ACTIVE" if payload.get("active_count") or payload.get("notable") else "NORMAL"
            await self.db.indicator_set(source.id, str(value), payload)
            if source.parser == "ppi_telegram":
                await self.db.indicator_set("ppi", str(value), payload)
            elif source.parser == "ews_dashboard":
                await ews.notify_update(self, source, payload)
            self.stats["indicator"] += 1
            return {"kind": "indicator", "value": value}

        event = self.normalizer.normalize(raw, source)
        if event is None:
            self.stats["filtered"] += 1
            return {"kind": "filtered"}

        lock = self._pipeline_locks.setdefault(event.theater, asyncio.Lock())
        async with lock:
            return await self._process_event(event)

    async def _process_event(self, event: Event) -> dict[str, Any]:
        dedup = await self.deduper.assign(event, self.db)
        if dedup.revision is not None:
            return await self._process_revision(event, dedup)

        if dedup.duplicate:
            self.stats["duplicates"] += 1
            if dedup.cluster is not None and dedup.cluster.tg_message_id:
                try:
                    await self.alerts.refresh(dedup.cluster)
                except Exception:
                    log.exception("failed to refresh cluster %s", dedup.cluster.id)
            return {"kind": "duplicate", "reason": dedup.reason}

        cluster = dedup.cluster
        if cluster is None or cluster.id is None:
            self.stats["filtered"] += 1
            return {"kind": "filtered"}
        event.cluster_id = cluster.id
        event_id = await self.db.insert_event(event)
        if event_id is None:
            self.stats["filtered"] += 1
            return {"kind": "duplicate", "reason": "url"}
        self.stats["events"] += 1

        if event.category == "indicator" or event.theater == "MARKETS":
            self.stats["indicator"] += 1
            return {"kind": "indicator_stored"}

        if dedup.aftershock:
            self.stats["aftershocks"] = self.stats.get("aftershocks", 0) + 1
            await self.db.update_event_scoring(event_id, 0.0, cluster.level, [], status="aftershock")
            if cluster.tg_message_id:
                try:
                    await self.alerts.refresh(cluster, force=True)
                except Exception:
                    log.exception("failed to refresh aftershock cluster %s", cluster.id)
            return {"kind": "aftershock", "cluster": cluster.id}

        return await self._score_and_alert(event, cluster, event_id, dedup.novelty)

    async def _process_revision(self, event: Event, dedup) -> dict[str, Any]:
        previous = dedup.revision
        cluster = dedup.cluster
        if previous is None or cluster is None or cluster.id is None:
            self.stats["duplicates"] += 1
            return {"kind": "duplicate", "reason": "revision-no-cluster"}
        event.id = previous.id
        event.cluster_id = cluster.id
        revisions = int(previous.numeric_payload.get("revision") or 0) + 1
        event.numeric_payload["revision"] = revisions
        event.numeric_payload["revised_from"] = {
            "mag": previous.numeric_payload.get("mag"),
            "alert": previous.numeric_payload.get("alert"),
        }
        await self.db.update_event_revision(previous.id, event)
        self.stats["revisions"] = self.stats.get("revisions", 0) + 1
        log.info("revision %s: %s -> %s", previous.id, previous.title[:60], event.title[:60])
        return await self._score_and_alert(
            event, cluster, previous.id, 1.0,
            old_score=float(previous.score or 0.0), force_edit=True,
        )

    async def _capture_initial_score(self, cluster: Cluster, score: float) -> None:
        """Anchor score for calibration: set once on the cluster's first event."""
        if cluster is None or cluster.id is None or cluster.score_initial is not None:
            return
        # A pre-existing cluster must not anchor at a later event.
        count = await self.db.fetchone(
            "SELECT COUNT(*) AS n FROM events WHERE cluster_id=?", (cluster.id,))
        if count is None or int(count["n"]) != 1:
            return
        if await self.db.set_cluster_initial_score(cluster.id, score):
            cluster.score_initial = round(float(score), 3)

    async def _score_and_alert(
        self, event: Event, cluster: Cluster, event_id: int, novelty: float,
        old_score: float | None = None, force_edit: bool = False,
    ) -> dict[str, Any]:
        hits = await self.tripwires.match(event, self.db)
        tripwire = max(hits, key=lambda hit: hit.level) if hits else None
        rules = [hit.id for hit in hits]
        if hits:
            event.matched_rules = rules
            await self.db.update_event_payload(event_id, event.numeric_payload)

        geo_factor = self.geo.geo_factor(
            event.theater, event.country, event.lat, event.lon, event.tier
        )
        domain_result = self.domain.evaluate(event, geo_factor, novelty)
        lex_result = self.lexicon.score(event, geo_factor, novelty)

        if domain_result is not None and domain_result.dropped and tripwire is None:
            await self.db.update_event_scoring(event_id, 0.0, L0, rules, status="dropped")
            if old_score is None:
                await self._capture_initial_score(cluster, 0.0)
            self.stats["dropped"] += 1
            return {"kind": "dropped", "reason": domain_result.drop_reason}

        result = domain_result if (
            domain_result is not None and domain_result.score >= lex_result.score
        ) else lex_result

        bonus, bonus_reasons = self.domain.bonus(event)
        if bonus and not lex_result.dropped:
            factor = self.domain.trust.get(event.tier, 0.25) * geo_factor * novelty
            bonus = round(bonus * factor, 3)
            result = ScoreResult(
                score=round(result.score + bonus, 3),
                base=result.base,
                matched=list(result.matched) + bonus_reasons,
                factors={**result.factors, "bonus": bonus},
                dropped=result.dropped,
                drop_reason=result.drop_reason,
            )

        if tripwire is None and (result.dropped or result.score <= 0):
            await self.db.update_event_scoring(event_id, 0.0, L0, rules, status="zero")
            if old_score is None:
                await self._capture_initial_score(cluster, 0.0)
            self.stats["zero"] += 1
            return {"kind": "zero"}

        if old_score is None:
            decision = await self.correlator.add(event, result.score, cluster, tripwire)
        else:
            decision = await self.correlator.add_revision(
                event, result.score, old_score, cluster, tripwire
            )
        event.score = result.score
        event.level = decision.cluster_level
        await self.db.update_event_scoring(event_id, result.score, decision.cluster_level, rules)
        if old_score is None:
            await self._capture_initial_score(cluster, decision.score)
        cluster.total_score = decision.score
        cluster.level = max(cluster.level, decision.cluster_level)
        if tripwire is not None:
            cluster.tripwire_id = tripwire.id
        await self.db.update_cluster(cluster)

        outcome = await self.alerts.handle(
            decision, cluster, event, result, tripwire, force_edit=force_edit
        )
        if outcome.get("action") not in ("below_threshold", "suppressed"):
            log.info(
                "event %s %s -> %s (theater %s %.1f/%s)",
                event_id, event.title[:80], outcome.get("action"),
                decision.theater, decision.score, decision.level,
            )
        return {"kind": "scored", "alert": outcome}

    # ---------- background loops ----------
    async def _pending_loop(self) -> None:
        while not self.stop.is_set():
            if not await self._sleep(30):
                return
            try:
                sent = await self.alerts.process_pending()
                if sent:
                    log.info("sent %d reminder(s)", sent)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("pending-actions loop failed")

    async def _recompute_loop(self) -> None:
        while not self.stop.is_set():
            if not await self._sleep(15 * 60):
                return
            try:
                states = await self.correlator.recompute()
                await compute_market_shock(self.db, settings=self.cfg.g("market_shock"))
                await diplo.compute_diplo_evac(self.db, settings=self.cfg.g("diplo_evac"))
                log.debug("theater recompute: %s", states)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("correlator recompute failed")

    async def _heartbeat_loop(self) -> None:
        interval = float(self.cfg.app.get("heartbeat_s", 30))
        while not self.stop.is_set():
            try:
                await self.db.heartbeat("main", f"events={self.stats.get('events', 0)}")
                systemd.notify("WATCHDOG=1")
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("heartbeat failed")
            if not await self._sleep(interval):
                return

    async def _maintenance_loop(self) -> None:
        while not self.stop.is_set():
            if not await self._sleep(10 * 60):
                return
            try:
                await self._maintenance_tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("maintenance failed")

    async def _maintenance_tick(self) -> None:
        now = int(time.time())
        last_cleanup = int(await self.db.meta_get("last_cleanup", 0) or 0)
        if now - last_cleanup >= 86400:
            await self.db.cleanup()
            await self.db.meta_set("last_cleanup", now)
            log.info("daily cleanup done")
        last_backup = int(await self.db.meta_get("last_backup", 0) or 0)
        if now - last_backup >= 86400:
            target = self.db.path.parent / "backups" / f"tripwire-{datetime.now(UTC):%Y%m%d}.db"
            await self.db.backup(target)
            packed = await asyncio.to_thread(self._gzip, target)
            await self.db.meta_set("last_backup", now)
            self._prune_backups()
            log.info("backup written to %s", packed)
        last_vacuum = int(await self.db.meta_get("last_vacuum", 0) or 0)
        if now - last_vacuum >= 7 * 86400:
            await self.db.vacuum()
            await self.db.meta_set("last_vacuum", now)
            log.info("weekly vacuum done")

    @staticmethod
    def _gzip(source: Path) -> Path:
        target = source.with_name(source.name + ".gz")
        with source.open("rb") as src, gzip.open(target, "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst)
        source.unlink()
        return target

    def _prune_backups(self, keep: int = 14) -> None:
        folder = self.db.path.parent / "backups"
        backups = sorted(folder.glob("tripwire-*.db.gz"))
        for path in backups[:-keep]:
            try:
                path.unlink()
            except OSError:
                log.warning("could not remove old backup %s", path)

    async def _health_loop(self) -> None:
        interval = float(self.cfg.app.get("health_check_s", 300))
        while not self.stop.is_set():
            if not await self._sleep(interval):
                return
            try:
                await self._check_sources()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("source health check failed")

    async def _check_sources(self) -> None:
        problems = await source_problems(self)
        now = int(time.time())
        failing = [item for item in problems["failed"] if item["failures"] >= 3]
        silent = problems["silent"]

        long_down: list[dict[str, Any]] = []
        for item in problems["failed"]:
            health = await self.db.health_get(item["id"])
            first = (health.get("first_error_at") or health.get("last_error_at")
                     or health.get("last_checked") or now)
            try:
                down_for = now - int(first)
            except (TypeError, ValueError):
                down_for = 0
            if int(item["failures"]) >= 3 and down_for >= 1800:
                long_down.append({**item, "down_minutes": down_for // 60})
        down_share = len(long_down) / max(len(self.sources), 1)

        no_events = False
        no_events_minutes = 0
        started = await self.db.meta_get("started_at")
        uptime = 0.0
        if started:
            try:
                uptime = now - datetime.fromisoformat(str(started)).timestamp()
            except ValueError:
                uptime = 0.0
        if uptime >= 5400:
            last_event = await self.db.last_event_ts()
            reference = last_event if last_event else int(now - uptime)
            silent_for = now - reference
            if silent_for >= 3600:
                no_events = True
                no_events_minutes = silent_for // 60

        if not failing and not silent and not no_events and down_share < 0.4:
            return
        last = int(await self.db.meta_get("last_degraded_alert", 0) or 0)
        if now - last < int(self.cfg.app.get("degraded_recheck_s", 3600)):
            return

        lines = ["⚠️ <b>МОНИТОРИНГ ДЕГРАДИРОВАН</b>", ""]
        if down_share >= 0.4:
            lines.append(
                f"• Недоступно {len(long_down)}/{len(self.sources)} источников >30 мин"
            )
        if no_events:
            lines.append(f"• Нет новых событий {no_events_minutes} мин")
        for item in failing[:8]:
            lines.append(
                f"• {item['name']} — {item['failures']} ошибок: {item['error'][:120]}"
            )
        for item in silent[:8]:
            lines.append(f"• {item['name']} — молчит {item['hours']} ч")
        await self.db.meta_set("last_degraded_alert", now)
        await self._notify_digest("\n".join(lines)[:4096])

    def _seconds_until_digest(self) -> float:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(self.cfg.app.get("timezone", "UTC"))
        raw = str(self.cfg.app.get("digest_at", "09:00"))
        try:
            hour, minute = (int(part) for part in raw.split(":", 1))
        except (TypeError, ValueError):
            hour, minute = 9, 0
        now = datetime.now(tz)
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return (target - now).total_seconds()

    async def _digest_loop(self) -> None:
        while not self.stop.is_set():
            if not await self._sleep(self._seconds_until_digest()):
                return
            try:
                await self._notify_digest(await build_digest(self))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("daily digest failed")
            if not await self._sleep(120):
                return

    async def _selftest_loop(self) -> None:
        interval = float(self.cfg.app.get("selftest_interval_days", 7)) * 86400
        while not self.stop.is_set():
            if not await self._sleep(3600):
                return
            try:
                now = int(time.time())
                last = int(await self.db.meta_get("last_selftest", 0) or 0)
                if now - last < interval:
                    continue
                await self.db.meta_set("last_selftest", now)
                from .selftest import run_selftest

                report = await run_selftest(self, deliver=self.tg.configured)
                passed = sum(1 for item in report["results"] if item["ok"])
                log.info(
                    "weekly selftest: %d/%d passed, delivered=%s",
                    passed, len(report["results"]), report["delivered"],
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("weekly selftest failed")

    async def _stats_loop(self) -> None:
        while not self.stop.is_set():
            if not await self._sleep(3600):
                return
            log.info(
                "hourly stats: %s; traffic %.1f MB; requests %d (304: %d)",
                dict(self.stats), self.fetcher.traffic_mb(),
                self.fetcher.requests_total, self.fetcher.not_modified_total,
            )

    # ---------- one-shot ----------
    async def run_once(self, only: Iterable[str] | None = None) -> dict[str, int]:
        wanted = list(only) if only else list(self.sources)
        unknown = [sid for sid in wanted if sid not in self.sources]
        if unknown:
            raise SystemExit(f"unknown or disabled sources: {', '.join(unknown)}")
        coros = []
        for sid in wanted:
            source = self.sources[sid]
            if source.type == "ws":
                log.warning("skipping websocket source %s in --once mode", sid)
                continue
            collector = make_collector(source, self.cfg)
            coros.append(self._once_source(source, collector))
        log.info("once: fetching %d source(s)", len(coros))
        await asyncio.gather(*coros, return_exceptions=True)
        for _ in range(3):
            if not any(self.stats[k] for k in ("events", "duplicates")):
                break
            await asyncio.sleep(0.05)
        log.info(
            "once done: %s; traffic %.2f MB", dict(self.stats), self.fetcher.traffic_mb()
        )
        return dict(self.stats)

    async def _once_source(self, source: Source, collector) -> None:
        try:
            await self._run_source(source, collector)
        except Exception:
            log.exception("once: source %s failed", source.id)
