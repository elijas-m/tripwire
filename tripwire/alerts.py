"""Alert manager: fixed message template, routing, cooldowns, repeats, /ack."""

from __future__ import annotations

import asyncio
import html
import logging
from typing import Any

from .models import L0, L1, L2, L3, LEVEL_NAMES, Cluster, Event, ScoreResult, TripwireHit
from .telegram_api import TelegramClient, TelegramError, esc
from .translate import Translator
from .util import from_ts, json_loads, now_ts, strip_html, utcnow

log = logging.getLogger(__name__)

THEATER_LABELS = {
    "EU_RU": "Европа/Россия",
    "ISRAEL": "Израиль",
    "GULF": "Залив",
    "MENA": "Ближний Восток",
    "PANDEMIC": "Здоровье",
    "NATURE": "Природа",
    "MARKETS": "Рынки",
}

CATEGORY_WHY = {
    "eu_ru": "Совокупность признаков эскалации в театре превысила порог.",
    "israel": "Совокупность признаков эскалации вокруг Израиля превысила порог.",
    "gulf": "Признаки угрозы инфраструктуре или судоходству в Заливе превысили порог.",
    "pandemic": "Признаки эпидемиологической угрозы превысили порог.",
    "earthquake": "Сейсмическое событие выше порога: возможны жертвы и разрушения.",
    "tsunami": "Угроза цунами для населённых побережий.",
    "volcano": "Извержение с потенциальными региональными последствиями.",
    "disaster": "Крупное стихийное бедствие с потенциальными жертвами.",
}

LEVEL_ICONS = {L2: "🟠", L3: "🔴"}
MAX_SOURCES = 6
VOLCANO_COLOUR_RANK = {"green": 0, "yellow": 1, "orange": 2, "red": 3}


def _event_ts(event: Event) -> int:
    dt = event.published_at or event.ingested_at
    return int(dt.timestamp()) if dt else 0


class AlertManager:
    def __init__(self, cfg, db, tg: TelegramClient | None) -> None:
        self.cfg = cfg
        self.db = db
        self.tg = tg
        self.alerts_cfg = cfg.alerts_cfg or {}
        self.channels_cfg = cfg.channels or {}
        self.suppressed_warning_theaters = {
            str(theater).upper()
            for theater in (self.alerts_cfg.get("suppress_warning_theaters") or [])
        }
        self.suppressed_repeat_theaters = {
            str(theater).upper()
            for theater in (self.alerts_cfg.get("suppress_repeats_theaters") or [])
        }
        self.cooldowns = cfg.thresholds.get("cooldowns", {})
        self.tripwires_by_id = {tw.get("id"): tw for tw in cfg.tripwires}
        self.translator = Translator(cfg, db)
        self.route_override: str | None = None
        self.silent = False
        self.test_mode = False
        self._zone = None

    # ---------- public entry point ----------
    async def handle(
        self,
        decision,
        cluster: Cluster | None,
        event: Event,
        score_result: ScoreResult | None = None,
        tripwire: TripwireHit | None = None,
        force_edit: bool = False,
    ) -> dict[str, Any]:
        level = int(decision.cluster_level)
        if tripwire is not None:
            level = max(level, int(tripwire.level))
        if level < L2:
            return {"action": "below_threshold", "level": level}

        theater = decision.theater
        if self._suppressed(level, theater):
            return {"action": "suppressed", "level": level, "theater": theater}
        if tripwire is None:
            evidence = await self._cluster_evidence(cluster, event)
            floor = float(self.alerts_cfg.get("min_cluster_score", 1.0))
            if evidence < floor:
                return {"action": "weak_evidence", "level": level,
                        "evidence": round(evidence, 2)}
        now = now_ts()

        state = await self.db.theater_get(theater)
        muted_until = int(state.get("muted_until") or 0)
        if muted_until > now and level < L3:
            return {"action": "muted", "level": level}

        previous = await self._previous_alert(cluster)
        escalate = previous is not None and level > int(previous["level"])

        # An ongoing eruption produces a new advisory every few hours. Keep one
        # card per cluster and edit it until the colour code or ash column grows.
        if (previous is not None and cluster is not None
                and level == int(previous["level"])
                and not await self._volcano_escalated(cluster, event)):
            edited = await self._edit_confirmation(cluster, level, theater, tripwire,
                                                   score_result, decision, force=True)
            return {"action": "edited" if edited else "cooldown", "level": level,
                    "alert_id": previous["id"]}

        if tripwire is None and previous is None and level == L2:
            last_theater = await self.db.last_theater_alert(theater)
            window = int(self.cooldowns.get("warning_theater_minutes", 60)) * 60
            if last_theater and now - last_theater < window:
                return {"action": "theater_cooldown", "level": level,
                        "last_alert_at": last_theater}
        if tripwire is not None and not escalate and previous is None:
            last_tw = await self.db.last_tripwire_alert(tripwire.id)
            if last_tw and now - last_tw < self._tripwire_cooldown(tripwire.id):
                edited = await self._edit_confirmation(cluster, level, theater, tripwire,
                                                        score_result, decision, force=force_edit)
                return {"action": "edited" if edited else "tripwire_cooldown", "level": level}

        cooldown = self._cooldown_seconds(level)
        if previous is not None:
            last_level = int(previous["level"])
            last_sent = int(previous["sent_at"] or 0)
            if level < last_level:
                return {"action": "level_down", "level": level, "alert_id": previous["id"]}
            if level == last_level and now - last_sent < cooldown:
                edited = await self._edit_confirmation(cluster, level, theater, tripwire,
                                                        score_result, decision, force=force_edit)
                return {
                    "action": "edited" if edited else "cooldown",
                    "level": level, "alert_id": previous["id"],
                }

        return await self._send_new(
            decision, cluster, event, level, theater, score_result, tripwire,
            escalate=escalate, previous=previous,
        )

    # ---------- send ----------
    async def _send_new(
        self, decision, cluster: Cluster | None, event: Event, level: int, theater: str,
        score_result: ScoreResult | None, tripwire: TripwireHit | None,
        escalate: bool, previous: Any = None,
    ) -> dict[str, Any]:
        thread_id, _topic = self._route(level, theater)
        explain = await self._explain(decision, cluster, event, level, theater,
                                      score_result, tripwire, thread_id)
        alert_id = await self.db.insert_alert(
            cluster.id if cluster else None, level, None, explain,
            tripwire.id if tripwire else None, status="pending",
        )
        explain["alert_id"] = alert_id
        text = await self._compose(level, theater, cluster, event, decision,
                                   score_result, tripwire, alert_id)
        explain["text"] = text
        await self.db.alert_set_explain(alert_id, explain)

        reply_to = None
        if escalate and cluster is not None and cluster.tg_message_id:
            if cluster.tg_thread_id is None or thread_id is None or str(cluster.tg_thread_id) == str(thread_id):
                reply_to = cluster.tg_message_id
        disable_notification = self._silent(level)

        if self.silent or not self.tg or not self.tg.configured:
            log.warning("alert %s dry-run (Telegram not configured):\n%s", alert_id, strip_html(text))
            await self.db.alert_update_status(alert_id, "dry_run")
            return {"action": "dry_run", "alert_id": alert_id, "level": level, "text": text}

        try:
            message_id = await self.tg.send_message(
                text, thread_id, disable_notification=disable_notification, reply_to=reply_to,
            )
        except TelegramError as exc:
            log.error("alert %s delivery failed: %s", alert_id, exc)
            await self.db.alert_update_status(alert_id, "failed")
            backup = False
            if self.tg.failures >= self.tg.backup_fail_threshold:
                backup = await self.tg.send_backup(self._plain(text), critical=level >= L3)
            return {"action": "backup" if backup else "failed", "alert_id": alert_id, "level": level}

        await self.db.alert_sent(alert_id, message_id)
        if cluster is not None and cluster.id is not None:
            cluster.tg_message_id = message_id
            cluster.tg_thread_id = int(thread_id) if thread_id else None
            cluster.last_alert_at = from_ts(now_ts())
            await self.db.set_cluster_message(cluster.id, message_id, cluster.tg_thread_id)
            await self.db.cluster_mark_alert(cluster.id, now_ts())

        if level >= L3 and self.alerts_cfg.get("pin_critical", True):
            await self.tg.pin(message_id)
        if level >= L3 and self._repeats_allowed(level, theater):
            await self._schedule_repeats(alert_id, cluster, level, thread_id)

        return {"action": "sent", "alert_id": alert_id, "level": level, "message_id": message_id}

    async def refresh(self, cluster: Cluster | None, force: bool = False) -> bool:
        """Edit an existing message after a new source confirmed the same cluster."""
        if cluster is None or cluster.id is None:
            return False
        alerts = await self.db.alerts_for_cluster(cluster.id)
        for row in reversed(alerts):
            if (row["status"] or "") in ("sent", "dry_run"):
                level = int(row["level"] or 0)
                if level < L2:
                    return False
                return await self._edit_confirmation(cluster, level, cluster.theater, None, None,
                                                     force=force)
        return False

    async def _edit_confirmation(
        self, cluster: Cluster | None, level: int, theater: str,
        tripwire: TripwireHit | None, score_result: ScoreResult | None,
        decision: Any = None, force: bool = False,
    ) -> bool:
        if not self.alerts_cfg.get("edit_on_confirmation", True):
            return False
        if self._suppressed(level, theater):
            return False
        if cluster is None or not cluster.tg_message_id or self.silent or not self.tg or not self.tg.configured:
            return False
        alerts = await self.db.alerts_for_cluster(cluster.id)
        if not alerts:
            return False
        last = alerts[-1]
        explain = json_loads(last_explain(last), {})
        sources_now = len(await self.db.cluster_sources(cluster.id))
        if not force and sources_now <= int(explain.get("confirmations") or 0):
            return False
        decision = decision or _ExplainDecision(explain, theater, level)
        events = await self.db.events_for_cluster(cluster.id)
        trigger = events[-1] if events else None
        if trigger is None:
            return False
        text = await self._compose(level, theater, cluster, trigger, decision,
                                   score_result, tripwire, int(last["id"]), realert=True)
        ok = await self.tg.edit_message(int(cluster.tg_message_id), text)
        if ok:
            explain["confirmations"] = sources_now
            explain["text"] = text
            await self.db.alert_set_explain(int(last["id"]), explain)
        return ok

    # ---------- repeats ----------
    async def _schedule_repeats(self, alert_id: int, cluster: Cluster | None,
                                level: int, thread_id: str | None) -> None:
        minutes = self.alerts_cfg.get("critical_repeat_minutes")
        if minutes is None:
            minutes = [10, 30]
        if not minutes:
            return
        payload = {
            "alert_id": alert_id,
            "cluster_id": cluster.id if cluster else None,
            "thread_id": thread_id,
            "attempt": 0,
        }
        await self.db.pending_add(now_ts() + int(minutes[0]) * 60, "alert_repeat", payload)

    async def process_pending(self) -> int:
        rows = await self.db.pending_due(now_ts())
        handled = 0
        for row in rows:
            if row["kind"] != "alert_repeat":
                continue
            payload = json_loads(row["payload"], {})
            await self.db.pending_done(int(row["id"]))
            if await self._send_repeat(payload):
                handled += 1
        return handled

    async def _send_repeat(self, payload: dict[str, Any]) -> bool:
        alert_id = int(payload.get("alert_id") or 0)
        alert = await self.db.get_alert(alert_id)
        if not alert or int(alert["level"] or 0) < L3:
            return False
        if alert["ack_at"] or alert["status"] != "sent":
            return False
        cluster_id = alert["cluster_id"]
        if cluster_id:
            alerts = await self.db.alerts_for_cluster(int(cluster_id))
            if alerts and int(alerts[-1]["id"]) != alert_id:
                return False

        minutes = self.alerts_cfg.get("critical_repeat_minutes")
        if minutes is None:
            minutes = [10, 30]
        if not minutes:
            return False

        explain = json_loads(alert["explain"], {})
        if str(explain.get("theater") or "").upper() in self.suppressed_repeat_theaters:
            return False
        original = explain.get("text") or ""
        attempt = int(payload.get("attempt") or 0) + 1
        prefix = f"🔁 ПОВТОР {attempt}: нет /ack\n\n"
        text = prefix + original
        admins = self._admin_mentions()
        if admins and self.channels_cfg.get("telegram", {}).get(
            "mention_admins_on_second_reminder", True
        ) and attempt >= 2:
            text += "\n\n" + admins
        thread_id = payload.get("thread_id") or explain.get("thread_id")
        if not self.tg or not self.tg.configured:
            log.warning("repeat for alert %s not sent (Telegram not configured)", alert_id)
            return False
        try:
            await self.tg.send_message(text[:4096], thread_id, disable_notification=False)
        except TelegramError as exc:
            log.error("repeat for alert %s failed: %s", alert_id, exc)
            return False

        if attempt < len(minutes):
            await self.db.pending_add(
                now_ts() + (int(minutes[attempt]) - int(minutes[attempt - 1])) * 60,
                "alert_repeat",
                {**payload, "attempt": attempt},
            )
        return True

    # ---------- message ----------
    async def _compose(
        self, level: int, theater: str, cluster: Cluster | None, event: Event,
        decision, score_result: ScoreResult | None, tripwire: TripwireHit | None,
        alert_id: int | None, realert: bool = False,
    ) -> str:
        icon = LEVEL_ICONS.get(level, "🟠")
        label = THEATER_LABELS.get(theater, theater)
        stamp = utcnow().strftime("%d.%m.%Y %H:%M UTC")
        header = f"{icon} <b>{LEVEL_NAMES.get(level, 'WARNING')}</b> · ТЕАТР: {esc(label)} · {stamp}"
        if self.test_mode:
            header = f"🧪 <b>SELFTEST</b> · {header}"

        one_liner = (tripwire.one_liner if tripwire and tripwire.one_liner else event.title).strip()
        one_liner = (await self.translator.translate_many([(one_liner, event.lang)]))[0]
        if realert:
            one_liner = "Обновление: " + one_liner
        why = self._why(event, tripwire, score_result)

        sources, total_sources = await self._source_lines(cluster, event)
        score = float(getattr(decision, "score", 0.0) or 0.0)
        threshold = float(getattr(decision, "threshold", 0.0) or 0.0)
        confidence = self._confidence(decision, tripwire, total_sources)
        trigger = (tripwire.id if tripwire else "") or ", ".join(event.matched_rules or []) or "корреляция"
        accompanying = await self._accompanying()
        others = await self._other_theaters(theater)

        lines = [
            header,
            "",
            esc(one_liner[:240]),
            "",
            f"Уровень: <b>{LEVEL_NAMES.get(level, 'WARNING')}</b> · счёт {score:.1f} (порог {threshold:.0f}) · подтверждение: {esc(confidence)} · оценка не откалибрована",
            "",
            f"Важно: {esc(why)}",
        ]
        meta = (cluster.meta if cluster else {}) or {}
        aftershocks = int(meta.get("aftershocks") or 0)
        if aftershocks:
            main_mag = meta.get("main_mag")
            suffix = f" (главный M{float(main_mag):.1f})" if main_mag else ""
            lines.append(f"Афтершоки: {aftershocks}{suffix}")
        revised = event.numeric_payload.get("revised_from") or {}
        new_mag = event.numeric_payload.get("mag")
        if revised.get("mag") is not None and new_mag is not None:
            try:
                if abs(float(new_mag) - float(revised["mag"])) >= 0.3:
                    lines.append(f"Ревизия: M{float(revised['mag']):.1f} → M{float(new_mag):.1f}")
            except (TypeError, ValueError):
                pass
        unknowns: list[str] = []
        if tripwire is not None and not tripwire.confirmed:
            unknowns.append("независимое подтверждение (сейчас один источник)")
        if level >= L2 and total_sources < 2:
            unknowns.append("второй независимый источник")
        if unknowns:
            lines += ["", "Не установлено:", *[f"• {esc(item)}" for item in unknowns]]
        lines += [
            "",
            f"Источники ({total_sources}):",
            *sources,
            "",
            f"Триггер: {esc(trigger)}",
            f"Сопутствующее: {accompanying}",
            f"Статус других театров: {others}",
            "",
            f"/why {alert_id or trigger}   /ack   /mute {theater} 2",
        ]
        return "\n".join(lines)[:4096]

    @staticmethod
    def _confidence(decision, tripwire: TripwireHit | None, total_sources: int) -> str:
        if tripwire is not None and tripwire.confirm_reason:
            return tripwire.confirm_reason
        sources = int(getattr(decision, "sources", 0) or 0) or total_sources
        if sources >= 2:
            return f"{sources} независимых источника"
        if sources == 1:
            return "1 источник"
        return "по совокупности сигналов"

    async def _source_lines(self, cluster: Cluster | None, event: Event) -> tuple[list[str], int]:
        if cluster is not None and cluster.id is not None:
            events = await self.db.events_for_cluster(cluster.id)
        else:
            events = []
        if not events:
            events = [event]
        by_source: dict[str, Event] = {}
        for ev in events:
            current = by_source.get(ev.source_id)
            if current is None or _event_ts(ev) > _event_ts(current):
                by_source[ev.source_id] = ev
        ordered = sorted(by_source.values(), key=lambda e: (e.tier, -_event_ts(e)))
        chosen = ordered[:MAX_SOURCES]
        titles = await self.translator.translate_many(
            [((ev.title or "")[:140], ev.lang) for ev in chosen]
        )
        lines: list[str] = []
        for ev, raw_title in zip(chosen, titles):
            src = self.cfg.sources_by_id.get(ev.source_id)
            name = src.name if src else ev.source_id
            tier = ev.tier
            original = strip_html(ev.title or "")[:160]
            title = esc(raw_title[:180])
            if original and original.strip() != raw_title.strip():
                title += f" <i>(ориг.: {esc(original)})</i>"
            published = ev.published_at or ev.ingested_at
            received = ev.ingested_at
            stamp = ""
            if published:
                stamp = f"опубл. {published.strftime('%d.%m %H:%M UTC')}"
            if received:
                stamp += f" · получ. {received.strftime('%d.%m %H:%M UTC')}"
            link = ""
            if ev.url:
                link = f' <a href="{html.escape(ev.url, quote=True)}">[ссылка]</a>'
            lines.append(f"• {esc(name)} (tier {tier}) · {stamp} — {title}{link}")
        if len(ordered) > MAX_SOURCES:
            lines.append(f"• …и ещё {len(ordered) - MAX_SOURCES}")
        return lines, len(ordered)

    def _why(self, event: Event, tripwire: TripwireHit | None,
             score_result: ScoreResult | None) -> str:
        if tripwire is not None and tripwire.why:
            why = tripwire.why
        elif event.category in CATEGORY_WHY:
            why = CATEGORY_WHY[event.category]
        else:
            why = "Совокупный счёт театра превысил порог."
        if tripwire is not None and not tripwire.confirmed:
            why = "Не подтверждено независимо. " + why
        if score_result is not None and score_result.matched:
            terms = ", ".join(score_result.matched[:5])
            why = f"{why} Маркеры: {terms}."
        return why[:400]

    async def _accompanying(self) -> str:
        parts: list[str] = []
        shock = await self.db.indicator_get("market_shock")
        if shock:
            meta = shock.get("meta") or {}
            brent = meta.get("brent_pct")
            if isinstance(brent, (int, float)):
                parts.append(f"Brent {brent:+.1f}%")
            if shock.get("value") and shock["value"] != "NORMAL":
                parts.append(f"Рынки: {shock['value']}")
        ppi = await self.db.indicator_get("ppi")
        if ppi and ppi.get("value") in ("ANOMALY", "ELEVATED"):
            parts.append(f"PPI: {ppi['value']}")
        return esc(" · ".join(parts)) if parts else "—"

    async def _other_theaters(self, current: str) -> str:
        rows = await self.db.theater_all()
        parts = []
        for row in rows:
            theater = row["theater"]
            if theater == current or theater == "MARKETS":
                continue
            level = int(row["level"] or 0)
            label = THEATER_LABELS.get(theater, theater)
            score = float(row["score"] or 0.0)
            parts.append(f"{label} {LEVEL_NAMES.get(level, 'NOISE')}({score:.1f})")
        return esc(" · ".join(parts)) if parts else "—"

    @staticmethod
    def _plain(text: str) -> str:
        return html.unescape(strip_html(text))

    # ---------- helpers ----------
    async def _cluster_evidence(self, cluster: Cluster | None, event: Event) -> float:
        """Best event score in the cluster: a theater alert needs a real trigger."""
        scores = [float(event.score or 0.0)]
        if cluster is not None and cluster.id is not None:
            rows = await self.db.events_for_cluster(cluster.id)
            if rows:
                scores = [float(row.score or 0.0) for row in rows]
        return max(scores)

    async def _previous_alert(self, cluster: Cluster | None):
        if cluster is None or cluster.id is None:
            return None
        alerts = await self.db.alerts_for_cluster(cluster.id)
        for row in reversed(alerts):
            if (row["status"] or "") in ("sent", "dry_run"):
                return row
        return None

    async def _volcano_escalated(self, cluster: Cluster, event: Event) -> bool:
        """True when a new advisory raises the colour code or ash column."""
        payload = event.numeric_payload or {}
        colour = str(payload.get("colour_code") or "").lower()
        if event.category != "volcano" or colour not in VOLCANO_COLOUR_RANK:
            return True
        try:
            ash = float(payload.get("ash_ft") or 0)
        except (TypeError, ValueError):
            ash = 0.0
        name = str(payload.get("volcano_name") or "").lower()

        prev_rank = -1
        prev_ash = 0.0
        for row in await self.db.events_for_cluster(cluster.id):
            if event.id is not None and row.id == event.id:
                continue
            other = row.numeric_payload or {}
            if not other.get("colour_code") and not other.get("ash_ft"):
                continue
            other_name = str(other.get("volcano_name") or "").lower()
            if name and other_name and other_name != name:
                continue
            prev_rank = max(prev_rank, VOLCANO_COLOUR_RANK.get(
                str(other.get("colour_code") or "").lower(), -1))
            try:
                prev_ash = max(prev_ash, float(other.get("ash_ft") or 0))
            except (TypeError, ValueError):
                pass

        if prev_rank < 0 and prev_ash <= 0:
            return True
        colour_up = prev_rank >= 0 and VOLCANO_COLOUR_RANK[colour] > prev_rank
        return colour_up or ash > prev_ash

    def _suppressed(self, level: int, theater: str) -> bool:
        return level == L2 and str(theater).upper() in self.suppressed_warning_theaters

    def _repeats_allowed(self, level: int, theater: str) -> bool:
        return (level >= L3
                and str(theater).upper() not in self.suppressed_repeat_theaters)

    def _cooldown_seconds(self, level: int) -> int:
        key = "critical_minutes" if level >= L3 else "warning_minutes"
        return int(self.cooldowns.get(key, 60 if level >= L3 else 15)) * 60

    def _tripwire_cooldown(self, tripwire_id: str) -> int:
        rule = self.tripwires_by_id.get(tripwire_id) or {}
        minutes = rule.get("cooldown_min")
        if minutes is None:
            minutes = self.cooldowns.get("tripwire_minutes", 60)
        return int(minutes) * 60

    def _route(self, level: int, theater: str) -> tuple[str | None, str | None]:
        if self.route_override:
            return self.route_override, "TEST"
        level_name = LEVEL_NAMES.get(level, "WARNING")
        rules = self.channels_cfg.get("routing") or []
        for rule in rules:
            if "match_theater" in rule and rule["match_theater"] == theater:
                topic = rule.get("topic")
                return self.cfg.channel_topic(topic), topic
            if "match_level" in rule and rule["match_level"] == level_name:
                topic = rule.get("topic")
                return self.cfg.channel_topic(topic), topic
            if rule.get("default"):
                topic = rule["default"]
                return self.cfg.channel_topic(topic), topic
        topic = "CRITICAL" if level >= L3 else "WARNING"
        return self.cfg.channel_topic(topic), topic

    def _silent(self, level: int) -> bool:
        if level >= L3:
            return False
        local = utcnow().astimezone(self._tz())
        night = self.cfg.app.get("night", {"start": 23, "end": 7})
        start, end = int(night.get("start", 23)), int(night.get("end", 7))
        return local.hour >= start or local.hour < end

    def _tz(self):
        from zoneinfo import ZoneInfo

        if not hasattr(self, "_zone"):
            self._zone = ZoneInfo(self.cfg.app.get("timezone", "UTC"))
        return self._zone

    def _admin_mentions(self) -> str:
        ids = sorted(self.cfg.admin_ids())
        if not ids:
            return ""
        return " ".join(f'<a href="tg://user?id={uid}">admin</a>' for uid in ids)

    async def _explain(
        self, decision, cluster: Cluster | None, event: Event, level: int, theater: str,
        score_result: ScoreResult | None, tripwire: TripwireHit | None,
        thread_id: str | None,
    ) -> dict[str, Any]:
        events: list[dict[str, Any]] = []
        sources: list[str] = []
        if cluster is not None and cluster.id is not None:
            for ev in await self.db.events_for_cluster(cluster.id):
                events.append({
                    "id": ev.id, "source_id": ev.source_id, "tier": ev.tier,
                    "title": ev.title, "url": ev.url, "score": round(ev.score, 2),
                    "level": ev.level, "published_at": ev.published_at.isoformat() if ev.published_at else None,
                })
            sources = await self.db.cluster_sources(cluster.id)
        else:
            events.append({
                "id": event.id, "source_id": event.source_id, "tier": event.tier,
                "title": event.title, "url": event.url, "score": round(event.score, 2),
                "level": event.level,
            })
            sources = [event.source_id]

        explain: dict[str, Any] = {
            "created_at": utcnow().isoformat(),
            "level": level,
            "level_name": LEVEL_NAMES.get(level),
            "theater": theater,
            "theater_label": THEATER_LABELS.get(theater, theater),
            "thread_id": thread_id,
            "trigger": tripwire.id if tripwire else None,
            "trigger_why": tripwire.why if tripwire else None,
            "trigger_payload": tripwire.payload if tripwire else {},
            "decision": {
                "score": round(float(getattr(decision, "score", 0.0) or 0.0), 2),
                "threshold": round(float(getattr(decision, "threshold", 0.0) or 0.0), 2),
                "cluster_level": int(getattr(decision, "cluster_level", 0) or 0),
                "previous_level": int(getattr(decision, "previous_level", 0) or 0),
                "theater_level": int(getattr(decision, "level", 0) or 0),
                "sources": int(getattr(decision, "sources", 0) or 0),
                "tier01": bool(getattr(decision, "trait_tier01", False)),
                "modifiers": dict(getattr(decision, "modifiers", {}) or {}),
            },
            "cluster": {
                "id": cluster.id if cluster else None,
                "category": cluster.category if cluster else event.category,
                "title": cluster.title_canonical if cluster else event.title,
                "sources": sources,
                "sources_count": len(sources),
                "tiers_bitmap": cluster.tiers_bitmap if cluster else 1 << event.tier,
            },
            "events": events,
            "market": await self.db.indicator_get("market_shock"),
            "ppi": await self.db.indicator_get("ppi"),
            "confirmations": len(sources),
            "text": "",
        }
        if score_result is not None:
            explain["lexicon"] = {
                "base": round(score_result.base, 2),
                "score": round(score_result.score, 2),
                "matched": list(score_result.matched[:20]),
                "factors": {k: round(float(v), 3) for k, v in score_result.factors.items()},
                "dropped": score_result.dropped,
                "drop_reason": score_result.drop_reason,
            }
        return explain

    # ---------- ack ----------
    async def ack(self, cluster_id: int | None = None) -> int:
        alert_id = await self.db.ack_latest(cluster_id)
        if alert_id and cluster_id:
            cluster = await self.db.get_cluster(cluster_id)
            if cluster:
                cluster.ack_at = from_ts(now_ts())
                await self.db.update_cluster(cluster)
        return alert_id


def last_explain(row: Any) -> str:
    return row["explain"] if row["explain"] else "{}"


class _ExplainDecision:
    """Minimal decision view reconstructed from a stored explain payload."""

    def __init__(self, explain: dict[str, Any], theater: str, level: int) -> None:
        decision = explain.get("decision") or {}
        self.theater = theater
        self.score = float(decision.get("score") or 0.0)
        self.threshold = float(decision.get("threshold") or 0.0)
        self.cluster_level = int(decision.get("cluster_level") or level)
        self.previous_level = int(decision.get("previous_level") or 0)
        self.level = int(decision.get("theater_level") or 0)
        self.sources = int(decision.get("sources") or 0)
        self.trait_tier01 = bool(decision.get("tier01"))
        self.modifiers = decision.get("modifiers") or {}
