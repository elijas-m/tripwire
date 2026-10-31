"""Admin command loop for the Telegram forum group (§14)."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from . import diplo, ews, radiation
from .alerts import THEATER_LABELS
from .digest import build_digest, source_problems
from .indicators import market_summary
from .models import LEVEL_NAMES
from .telegram_api import TelegramError, esc
from .util import UTC, json_loads

log = logging.getLogger(__name__)

THEATER_ALIASES = {
    "eu_ru": "EU_RU", "eu": "EU_RU", "европа": "EU_RU", "россия": "EU_RU",
    "israel": "ISRAEL", "il": "ISRAEL", "израиль": "ISRAEL",
    "gulf": "GULF", "залив": "GULF",
    "pandemic": "PANDEMIC", "health": "PANDEMIC", "здоровье": "PANDEMIC",
    "nature": "NATURE", "природа": "NATURE",
    "markets": "MARKETS", "рынки": "MARKETS",
}

LEVEL_ALIASES = {
    "watch": "watch", "w": "watch", "l1": "watch",
    "warning": "warning", "warn": "warning", "l2": "warning",
    "critical": "critical", "crit": "critical", "l3": "critical",
}

COMMAND_LIST = [
    {"command": "status", "description": "Состояние театров, счёт и здоровье источников"},
    {"command": "why", "description": "Разбор алерта: /why <alert_id|trigger_id>"},
    {"command": "ack", "description": "Подтвердить CRITICAL и остановить повторы"},
    {"command": "mute", "description": "Заглушить WARNING театра: /mute <theater> <hours>"},
    {"command": "digest", "description": "Сформировать дайджест сейчас"},
    {"command": "sources", "description": "Источники, last_ok и ошибки: /sources [theater]"},
    {"command": "test", "description": "Синтетический прогон: /test <tripwire_id>"},
    {"command": "reload", "description": "Перечитать конфигурацию"},
    {"command": "threshold", "description": "/threshold <theater> <level> <value>"},
]


def theater_arg(value: str) -> str | None:
    key = (value or "").strip().lower()
    if key.upper() in THEATER_LABELS:
        return key.upper()
    return THEATER_ALIASES.get(key)


def _tz(cfg) -> ZoneInfo:
    return ZoneInfo(cfg.app.get("timezone", "UTC"))


def _fmt_ts(ts: Any, cfg, fallback: str = "—") -> str:
    try:
        value = int(ts)
    except (TypeError, ValueError):
        return fallback
    if value <= 0:
        return fallback
    return datetime.fromtimestamp(value, UTC).astimezone(_tz(cfg)).strftime("%d.%m %H:%M")


async def format_status(app) -> str:
    snapshot = await app.correlator.snapshot()
    lines = ["📊 <b>STATUS</b> · Tripwire", ""]
    for row in snapshot:
        theater = row["theater"]
        label = THEATER_LABELS.get(theater, theater)
        level = int(row["level"])
        muted = ""
        try:
            muted_until = int(row.get("muted_until") or 0)
            if muted_until > time.time():
                muted = f" · 🔇 до {_fmt_ts(muted_until, app.cfg)}"
        except (TypeError, ValueError):
            pass
        thresholds = app.correlator.effective_levels(theater)
        lines.append(
            f"{label}: <b>{LEVEL_NAMES.get(level, 'NOISE')}</b> · {float(row['score']):.1f} "
            f"(W {thresholds['warning']:.0f} / C {thresholds['critical']:.0f}){muted}"
        )
    if len(lines) == 2:
        lines.append("Театры: нет данных")

    lines.append("")
    ppi = await app.db.indicator_get("ppi")
    shock = await app.db.indicator_get("market_shock")
    lines.append(f"PPI: {esc(str((ppi or {}).get('value') or '—'))}")
    shock_value = (shock or {}).get("value") or "—"
    shock_line = f"Рынки: {esc(str(shock_value))}"
    moves = market_summary((shock or {}).get("meta"))
    if moves:
        shock_line += " · " + " · ".join(moves)
    lines.append(shock_line)
    lines.extend(await ews.status_lines(app.db))
    lines.extend(await radiation.status_lines(app.db))
    lines.extend(await diplo.status_lines(app.db))
    lines.append("")
    return "\n".join(lines)


async def format_status_full(app) -> str:
    text = await format_status(app)
    problems = await source_problems(app)
    enabled = len(app.sources)
    failing = len(problems["failed"])
    silent = len(problems["silent"])
    open_clusters = await app.db.open_cluster_count()
    hour_ago = int(time.time()) - 3600
    recent = await app.db.fetchone(
        "SELECT COUNT(*) AS n FROM events WHERE ingested_at>=?", (hour_ago,)
    )
    translator = getattr(app.alerts, "translator", None)
    lines = [
        text,
        f"Источники: {enabled - failing}/{enabled} ок · сбои {failing} · молчат {silent}",
        f"Кластеры открыты: {open_clusters} · событий за час: {int(recent['n']) if recent else 0}",
    ]
    if translator is not None:
        lines.append(await translator.status_line())
    for item in problems["failed"][:5]:
        lines.append(f"🔴 {esc(item['name'])} — {item['failures']}× {esc(item['error'][:80])}")
    for item in problems["silent"][:5]:
        lines.append(f"🟠 {esc(item['name'])} — молчит {item['hours']} ч")
    return "\n".join(lines)[:4096]


async def format_sources(app, theater: str | None = None) -> str:
    health = {row["source_id"]: row for row in await app.db.health_all()}
    now = int(time.time())
    rows: list[tuple[int, str, str]] = []
    for src in app.cfg.enabled_sources():
        if theater and src.theater != theater:
            continue
        row = health.get(src.id) or {}
        failures = int(row.get("consecutive_failures") or 0)
        last_ok = row.get("last_ok")
        try:
            age = now - int(last_ok) if last_ok else None
        except (TypeError, ValueError):
            age = None
        if failures:
            icon, rank = "🔴", 0
        elif last_ok is None:
            icon, rank = "⚪", 2
        elif age is not None and age > 72 * 3600:
            icon, rank = "🟠", 1
        else:
            icon, rank = "🟢", 3
        line = (
            f"{icon} {esc(src.name)} · {src.theater} · tier {src.tier} · "
            f"ok {_fmt_ts(last_ok, app.cfg)}"
        )
        if row.get("last_items") is not None:
            line += f" · {int(row.get('last_items') or 0)} items"
        if failures:
            line += f" · {failures}× {esc(str(row.get('last_error') or '')[:60])}"
        rows.append((rank, src.id, line))
    rows.sort(key=lambda item: (item[0], item[1]))
    title = "📡 <b>ИСТОЧНИКИ</b>"
    if theater:
        title += f" · {esc(THEATER_LABELS.get(theater, theater))}"
    lines = [title, f"Всего: {len(rows)}"]
    for _, _, line in rows[:55]:
        lines.append(line)
    if len(rows) > 55:
        lines.append(f"…и ещё {len(rows) - 55}")
    return "\n".join(lines)[:4096]


def format_why(alert: Any, explain: dict[str, Any], cfg) -> str:
    decision = explain.get("decision") or {}
    cluster = explain.get("cluster") or {}
    level = int(explain.get("level") or 0)
    theater = str(explain.get("theater") or "")
    lines = [
        f"🧠 <b>/why</b> · алерт #{alert['id']} · "
        f"{LEVEL_NAMES.get(level, '—')} · {esc(explain.get('theater_label') or theater)}",
        "",
        esc((cluster.get("title") or "")[:220]),
        f"Кластер #{cluster.get('id')} · {esc(str(cluster.get('category') or ''))} · "
        f"источников {int(explain.get('confirmations') or 0)}",
        "",
        f"Счёт: <b>{float(decision.get('score') or 0):.1f}</b> "
        f"(порог {float(decision.get('threshold') or 0):.0f}) · "
        f"уровень кластера {LEVEL_NAMES.get(int(decision.get('cluster_level') or 0), '—')} · "
        f"театр {LEVEL_NAMES.get(int(decision.get('theater_level') or 0), '—')}",
        f"Независимость: {int(decision.get('sources') or 0)} домен(ов), "
        f"tier0/1: {'да' if decision.get('tier01') else 'нет'}",
    ]
    trigger = explain.get("trigger")
    if trigger:
        lines.append(f"Триггер: <b>{esc(str(trigger))}</b>")
        why = explain.get("trigger_why") or ""
        if why:
            lines.append(esc(str(why)[:300]))
    rules = explain.get("rules") or []
    if rules and rules != [trigger]:
        lines.append("Правила: " + esc(", ".join(str(r) for r in rules[:10])))

    modifiers = decision.get("modifiers") or {}
    if modifiers:
        rendered = ", ".join(
            f"{esc(str(name))} ×{float(value):.2f}" for name, value in modifiers.items()
        )
        lines.append(f"Множители: {rendered}")

    lexicon = explain.get("lexicon") or {}
    if lexicon:
        lines.append("")
        lines.append(
            f"Лексикон: база {float(lexicon.get('base') or 0):.1f} → "
            f"{float(lexicon.get('score') or 0):.1f}"
        )
        matched = lexicon.get("matched") or []
        if matched:
            lines.append("Маркеры: " + esc(", ".join(str(m) for m in matched[:14])))
        factors = lexicon.get("factors") or {}
        if factors:
            lines.append(
                "Факторы: " + ", ".join(f"{esc(str(k))} {float(v):.2f}" for k, v in factors.items())
            )

    events = explain.get("events") or []
    if events:
        scores = [float(ev.get("score") or 0) for ev in events]
        total = sum(scores) or 1.0
        lines.append("")
        lines.append("<b>Вклад источников:</b>")
        for ev in sorted(events, key=lambda item: float(item.get("score") or 0), reverse=True)[:10]:
            src = cfg.sources_by_id.get(ev.get("source_id"))
            name = src.name if src else str(ev.get("source_id") or "?")
            score = float(ev.get("score") or 0)
            share = 100.0 * score / total
            lines.append(
                f"• {esc(name)} (tier {int(ev.get('tier') or 0)}) — {score:.1f} "
                f"({share:.0f}%) — {esc(str(ev.get('title') or '')[:110])}"
            )

    lines.append("")
    market = explain.get("market") or {}
    ppi = explain.get("ppi") or {}
    if market or ppi:
        market_meta = market.get("meta") or {}
        market_line = f"Рынок: {esc(str(market.get('value') or '—'))}"
        moves = market_summary(market_meta)
        if moves:
            market_line += f" ({', '.join(moves)})"
        lines.append(f"{market_line} · PPI: {esc(str(ppi.get('value') or '—'))}")
    lines.append(f"Создан: {esc(str(explain.get('created_at') or '—'))}")
    return "\n".join(lines)[:4096]


class CommandService:
    def __init__(self, app) -> None:
        self.app = app

    @property
    def tg(self):
        return self.app.tg

    async def run(self) -> None:
        app = self.app
        if not app.cfg.admin_ids():
            log.warning("command loop disabled: TRIPWIRE_ADMINS is empty")
            return
        try:
            await self.tg.set_commands(COMMAND_LIST)
        except Exception:
            log.exception("failed to register bot commands")
        offset = int(await app.db.meta_get("tg_offset", 0) or 0)
        log.info("command loop started (offset=%s)", offset)
        while not app.stop.is_set():
            try:
                updates = await self.tg.get_updates(offset)
            except asyncio.CancelledError:
                raise
            except TelegramError as exc:
                log.warning("getUpdates failed: %s", exc)
                if not await self._pause(10):
                    return
                continue
            except Exception:
                log.exception("getUpdates crashed")
                if not await self._pause(10):
                    return
                continue
            for update in updates:
                try:
                    offset = max(offset, int(update.get("update_id") or 0) + 1)
                except (TypeError, ValueError):
                    continue
                try:
                    await self.handle(update)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("command handler failed")
                await app.db.meta_set("tg_offset", offset)

    async def _pause(self, seconds: float) -> bool:
        try:
            await asyncio.wait_for(self.app.stop.wait(), timeout=seconds)
            return False
        except asyncio.TimeoutError:
            return True

    async def handle(self, update: dict[str, Any]) -> None:
        app = self.app
        message = update.get("message") or update.get("edited_message")
        if not isinstance(message, dict):
            return
        chat_id = str((message.get("chat") or {}).get("id") or "")
        if chat_id != str(self.tg.chat_id):
            return
        sender = (message.get("from") or {}).get("id")
        try:
            sender_id = int(sender or 0)
        except (TypeError, ValueError):
            sender_id = 0
        if sender_id not in app.cfg.admin_ids():
            log.warning("command ignored: sender is not an admin")
            return
        text = str(message.get("text") or "").strip()
        if not text.startswith("/"):
            return
        head, *args = text.split()
        command = head.split("@", 1)[0].lstrip("/").lower()
        thread = message.get("message_thread_id")
        reply_to = message.get("message_id")
        try:
            response = await self.dispatch(command, args)
        except (ValueError, KeyError) as exc:
            response = f"⚠️ Ошибка команды: {esc(str(exc))}"
        if response:
            await self.reply(response, thread, reply_to)

    async def reply(self, text: str, thread: Any = None, reply_to: Any = None) -> None:
        try:
            await self.tg.send_message(text[:4096], thread, reply_to=reply_to)
        except TelegramError as exc:
            log.warning("command reply failed: %s", exc)

    async def dispatch(self, command: str, args: list[str]) -> str:
        app = self.app
        if command == "status":
            return await format_status_full(app)
        if command == "why":
            return await self._why(args[0] if args else "")
        if command == "ack":
            if args:
                try:
                    alert_id = int(args[0])
                except ValueError as exc:
                    raise ValueError("/ack [alert_id]") from exc
                if not await app.db.ack_alert(alert_id):
                    return f"Алерт #{alert_id} не найден"
                return f"✅ Подтверждён алерт #{alert_id}"
            alert_id = await app.alerts.ack()
            if not alert_id:
                return "Нет неподтверждённых алертов"
            return f"✅ Подтверждён алерт #{alert_id}; повторы остановлены"
        if command == "mute":
            return await self._mute(args)
        if command == "digest":
            text = await build_digest(app)
            await self.tg.send_message(
                text, app.cfg.channel_topic("DIGEST"), disable_notification=True
            )
            return "📊 Дайджест отправлен в Digest & Status"
        if command == "sources":
            theater = None
            if args:
                theater = theater_arg(args[0])
                if theater is None:
                    raise ValueError("неизвестный театр")
            return await format_sources(app, theater)
        if command == "test":
            if not args:
                raise ValueError("/test <tripwire_id>")
            return await self._test(args[0])
        if command == "reload":
            ok = await app.reload_config()
            return "♻️ Конфиг перезагружен" if ok else "⚠️ Конфиг отклонён, работает прежний"
        if command == "threshold":
            return await self._threshold(args)
        if command in ("help", "start"):
            return (
                "Команды: /status, /why <alert_id|trigger_id>, /ack, "
                "/mute <theater> <hours>, /digest, /sources [theater], "
                "/test <tripwire_id>, /reload, /threshold <theater> <level> <value>"
            )
        return f"Неизвестная команда: /{esc(command)}"

    async def _why(self, arg: str) -> str:
        app = self.app
        arg = (arg or "").strip()
        alert = None
        if arg:
            try:
                alert = await app.db.get_alert(int(arg))
            except ValueError:
                alert = await app.db.latest_alert_by_tripwire(arg)
                if alert is None and arg not in app.alerts.tripwires_by_id:
                    return f"Триггер {esc(arg)} не найден"
            if alert is None:
                return f"Алерт #{esc(arg)} не найден"
        else:
            alert = await app.db.latest_alert()
            if alert is None:
                return "Журнал алертов пуст"
        explain = json_loads(alert["explain"], {})
        if not explain:
            return f"У алерта #{alert['id']} нет сохранённого разбора"
        explain.setdefault("rules", [alert["tripwire_id"]] if alert["tripwire_id"] else [])
        return format_why(alert, explain, app.cfg)

    async def _mute(self, args: list[str]) -> str:
        app = self.app
        if len(args) < 2:
            raise ValueError("/mute <theater> <hours>")
        theater = theater_arg(args[0])
        if not theater:
            raise ValueError("неизвестный театр")
        hours = float(args[1].replace(",", "."))
        max_hours = float(app.cfg.app.get("commands", {}).get("mute_max_hours", 168))
        if not 0.1 <= hours <= max_hours:
            raise ValueError(f"часы должны быть в диапазоне 0.1..{max_hours:.0f}")
        until = int(time.time() + hours * 3600)
        await app.db.theater_mute(theater, until)
        await app.db.journal_add(
            "admin", "mute", {"theater": theater, "hours": hours, "until": until}
        )
        return (
            f"🔇 WARNING театра {esc(THEATER_LABELS.get(theater, theater))} "
            f"заглушены до {_fmt_ts(until, app.cfg)}. CRITICAL не глушатся."
        )

    async def _test(self, tripwire_id: str) -> str:
        from .selftest import run_selftest

        report = await run_selftest(self.app, only=[tripwire_id.upper()])
        results = report["results"]
        if not results:
            return f"⚠️ Неизвестный tripwire: {esc(tripwire_id)}"
        passed = sum(1 for item in results if item["ok"])
        icon = "✅" if passed == len(results) else "⚠️"
        return (
            f"{icon} Selftest {passed}/{len(results)} · "
            f"{'Telegram' if report['delivered'] else 'dry-run'} · "
            f"подробности в 🧪 Test"
        )

    async def _threshold(self, args: list[str]) -> str:
        app = self.app
        if len(args) < 3:
            raise ValueError("/threshold <theater> <level> <value>")
        theater = theater_arg(args[0])
        if not theater:
            raise ValueError("неизвестный театр")
        level_name = LEVEL_ALIASES.get(args[1].strip().lower())
        if not level_name:
            raise ValueError("уровень: watch | warning | critical")
        value = float(args[2].replace(",", "."))
        if value <= 0:
            raise ValueError("порог должен быть > 0")
        current = app.correlator.effective_levels(theater)
        current[level_name] = value
        if not (current["watch"] < current["warning"] < current["critical"]):
            raise ValueError("нужно watch < warning < critical")
        ttl = float(app.cfg.app.get("commands", {}).get("threshold_ttl_hours", 24))
        result = await app.correlator.set_override(theater, level_name, value, ttl)
        await app.db.journal_add(
            "admin", "threshold",
            {"theater": theater, "level": level_name, "value": value,
             "ttl_hours": ttl, "until": result["until"]},
        )
        levels = result["levels"]
        return (
            f"⚙️ {esc(THEATER_LABELS.get(theater, theater))}: "
            f"W {levels['warning']:.1f} / C {levels['critical']:.1f} "
            f"(watch {levels['watch']:.1f}) до {_fmt_ts(result['until'], app.cfg)}"
        )
