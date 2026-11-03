"""Layer A: hard tripwires."""

from __future__ import annotations

import ast
import re
from typing import Any

from ..corroborate import WINDOW_HOURS, trigger_relevant
from ..models import Event, L0, L1, L2, L3, TripwireHit
from ..util import domain_of, json_loads, norm_text, now_ts, publisher_domain


class _SafeEval(ast.NodeVisitor):
    ALLOWED = (
        ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not,
        ast.USub, ast.Compare, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt,
        ast.GtE, ast.Is, ast.IsNot, ast.In, ast.NotIn, ast.Name, ast.Load,
        ast.Constant, ast.List, ast.Tuple, ast.BinOp, ast.Add, ast.Sub,
        ast.Mult, ast.Div,
    )

    def generic_visit(self, node: ast.AST) -> None:
        if not isinstance(node, self.ALLOWED):
            raise ValueError(f"unsupported expression node: {type(node).__name__}")
        super().generic_visit(node)


_DEPARTURE_DENIAL = re.compile(
    r"\b(?:denied|denies|dismissed|rumou?r|hoax|refuted|false\s+report|not\s+true|"
    r"no\s+plans?|no\s+evidence|unconfirmed)\b"
    r"|(?:опроверг\w*|слух\w*|не\s+подтверд\w*|дезинформац\w*|фейк\w*)",
    re.I,
)


def eval_numeric(expr: str, payload: dict[str, Any]) -> bool:
    try:
        tree = ast.parse(expr, mode="eval")
        _SafeEval().visit(tree)
        code = compile(tree, "<tripwire>", "eval")
        env = {k: v for k, v in payload.items() if re.fullmatch(r"[A-Za-z_]\w*", str(k))}
        return bool(eval(code, {"__builtins__": {}}, env))  # noqa: S307 - AST validated
    except Exception:
        return False


class TripwireEngine:
    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.rules: list[dict[str, Any]] = []
        self.skipped_theater = 0
        for raw in cfg.tripwires:
            rule = dict(raw)
            rule["_regex"] = re.compile(rule["regex"]) if rule.get("regex") else None
            rule["_negate"] = re.compile(rule["negate_regex"]) if rule.get("negate_regex") else None
            self.rules.append(rule)
        self.nato = set(cfg.geofences.get("nato", {}).get("countries", []))
        self.eu = set(cfg.geofences.get("eu", {}).get("countries", []))
        self.high_consequence = {
            norm_text(v) for v in cfg.geofences.get("high_consequence_volcanoes", [])
        }
        self.swarm_zones = [
            (str(z.get("name") or ""), float(z["lat"]), float(z["lon"]))
            for z in (cfg.geofences.get("supervolcano_zones") or [])
            if z.get("lat") is not None and z.get("lon") is not None
        ]
        self.country_alias_patterns: list[tuple[str, re.Pattern[str]]] = []
        for iso, entry in (cfg.gazetteer.get("countries") or {}).items():
            for alias in entry.get("names", []):
                a = norm_text(alias)
                if len(a) < 2:
                    continue
                if a[0].isalnum():
                    pat = r"(?<![\w])" + re.escape(a) + r"(?![\w])"
                else:
                    pat = re.escape(a)
                self.country_alias_patterns.append((iso, re.compile(pat)))

    async def match(self, event: Event, db) -> list[TripwireHit]:
        hits: list[TripwireHit] = []
        text = f"{event.title}\n{event.body_excerpt}"
        for rule in self.rules:
            if not self._theater_ok(rule, event):
                self.skipped_theater += 1
                continue
            if rule.get("domain") and rule["domain"] != event.category:
                continue
            if not self._source_ok(rule, event):
                continue
            kind = rule.get("type")
            hit = None
            if kind == "regex":
                hit = self._match_regex(rule, text, event)
            elif kind == "numeric":
                if eval_numeric(rule.get("expr", "False"), event.numeric_payload):
                    hit = self._hit(rule)
            elif kind == "handler":
                hit = await self._match_handler(rule, text, event, db)
            if hit is not None:
                hit.theater = event.theater
                hit.confirmed, hit.confirm_reason = await self._confirmation(rule, event, db)
                if hit.level >= L3 and not hit.confirmed:
                    # A single weak report may alert, but it must not claim CRITICAL.
                    hit.level = L2
                    hit.payload["unconfirmed"] = True
                hits.append(hit)
                if hit.id not in event.matched_rules:
                    event.matched_rules.append(hit.id)
        if hits:
            for hit in hits:
                hit.payload.setdefault("tripwire", hit.id)
        return hits

    # ---------- rule scope ----------
    def _theater_ok(self, rule: dict[str, Any], event: Event) -> bool:
        allowed = rule.get("theaters")
        if isinstance(allowed, str):
            allowed = [allowed]
        if not allowed:
            allowed = [rule["theater"]] if rule.get("theater") else []
        if not allowed:
            return True
        return (event.theater or "").upper() in {str(t).upper() for t in allowed}

    async def _confirmation(self, rule: dict[str, Any], event: Event,
                            db) -> tuple[bool, str]:
        """Whether a hard rule may claim CRITICAL.

        A single tier-2 wire copy is enough to raise WARNING, but strategic
        CRITICAL needs an official source, a structured official product or an
        independent publisher.
        """
        if rule.get("confirm") == "immediate_civil":
            return True, "официальное локальное оповещение"
        if event.tier <= 1:
            return True, f"официальный источник (tier {event.tier})"
        rows: list[dict[str, Any]] = []
        if event.cluster_id is not None:
            # The cluster may be a broad bucket (one city, many stories).
            # Only members that describe this trigger can confirm it.
            rows = [
                row for row in await db.cluster_origin_rows(event.cluster_id)
                if trigger_relevant(event, row, rule.get("id"), window_h=WINDOW_HOURS)
            ]
        if any(int(3 if row.get("tier") is None else row["tier"]) <= 1 for row in rows):
            return True, "в кластере есть официальный источник"
        origins = {
            publisher_domain(row.get("origin_domain"), str(row.get("source_id") or ""), self.cfg)
            for row in rows
        }
        origins.discard("")
        if len(origins) >= 2:
            return True, f"{len(origins)} независимых источника"
        return False, "один источник"

    # ---------- primitives ----------
    @staticmethod
    def _hit(rule: dict[str, Any], **payload: Any) -> TripwireHit:
        raw = str(rule.get("level", "CRITICAL")).strip().upper()
        level = {"CRITICAL": L3, "WARNING": L2, "WATCH": L1, "NOISE": L0,
                 "L3": L3, "L2": L2, "L1": L1, "L0": L0}.get(raw, L3)
        return TripwireHit(
            id=rule["id"],
            level=level,
            why=rule.get("why", ""),
            one_liner=rule.get("one_liner", rule["id"]),
            payload=payload,
            horizon=str(rule.get("horizon") or ""),
            refute=str(rule.get("refute") or ""),
            next_tripwire=str(rule.get("next_tripwire") or ""),
        )

    def _source_ok(self, rule: dict[str, Any], event: Event) -> bool:
        source_ids = rule.get("source_ids") or []
        if source_ids and event.source_id not in source_ids:
            return False
        domains = rule.get("source_domains") or []
        if domains:
            ev_domains = {event.origin_domain or "", domain_of(event.url)}
            if not any(any(d == dom or dom.endswith("." + d) for dom in ev_domains) for d in domains):
                return False
        max_tier = rule.get("max_tier")
        if max_tier is not None and event.tier > int(max_tier):
            return False
        require = rule.get("require_country_in")
        if require == "NATO_EU":
            if not event.country or event.country not in (self.nato | self.eu):
                return False
        elif isinstance(require, list):
            if not event.country or event.country not in require:
                return False
        return True

    def _match_regex(self, rule: dict[str, Any], text: str, event: Event) -> TripwireHit | None:
        if rule["_regex"] is None or not rule["_regex"].search(text):
            return None
        if rule["_negate"] is not None and rule["_negate"].search(text):
            return None
        return self._hit(rule)

    async def _match_handler(self, rule: dict[str, Any], text: str, event: Event,
                             db) -> TripwireHit | None:
        handler = rule.get("handler")
        if handler == "airspace_full_closure":
            return self._airspace(rule, text, event)
        if handler == "ordered_departure":
            return await self._ordered_departure(rule, text, event, db)
        if handler == "strike_on_nato":
            return self._strike_nato(rule, text, event)
        if handler == "oref_nationwide_directive":
            return self._oref_directive(rule, text, event)
        if handler == "oref_polygon_share":
            return await self._oref_share(rule, event, db)
        if handler == "volcano_high_consequence":
            return self._volcano_hc(rule, event)
        if handler == "volcano_swarm":
            return await self._volcano_swarm(rule, event, db)
        return None

    # ---------- handlers ----------
    def _airspace(self, rule: dict[str, Any], text: str, event: Event) -> TripwireHit | None:
        nt = norm_text(text)
        if not re.search(r"(airspace|воздушн\w+ пространств|מרחב אווירי).{0,100}"
                         r"(closed|closure|shut|закрыт|נסגר)", nt, re.S):
            return None
        if re.search(
            r"(partial\w*|частичн\w*|will (?:be )?clos\w*|to clos\w*|plans? to clos\w*|"
            r"may clos\w*|might clos\w*|could clos\w*|планир\w*|закро\w*|"
            r"может закрыт\w*|могут закрыт\w*)",
            nt,
        ):
            return None
        countries = set(rule.get("countries") or [])
        mentioned = set()
        for iso, pattern in self.country_alias_patterns:
            if iso in countries and pattern.search(nt):
                mentioned.add(iso)
        if event.country in countries:
            mentioned.add(event.country)
        if not mentioned:
            return None
        return self._hit(rule, countries=sorted(mentioned))

    async def _ordered_departure(self, rule, text, event, db) -> TripwireHit | None:
        if rule["_regex"] is None or not rule["_regex"].search(text):
            return None
        if _DEPARTURE_DENIAL.search(text):
            # "US denies reports of ordered departure" is the opposite signal.
            return None
        departure = str(event.numeric_payload.get("departure") or "")
        if not departure:
            if re.search(r"authorized\s+departure", text, re.I):
                departure = "authorized"
            elif re.search(r"ordered\s+departure", text, re.I):
                departure = "ordered"
        event.numeric_payload["departure"] = departure
        if departure == "authorized":
            # Voluntary departure of family members is a warning, not the
            # mandatory staff withdrawal that precedes an evacuation.
            hit = self._hit(rule)
            hit.level = min(hit.level, L2)
            hit.why += " Разрешённый выезд добровольный и не равен приказу о выезде."
            return hit
        capitals = rule.get("capitals") or []
        nt = norm_text(text)
        found = [c for c in capitals if c.lower() in nt]
        if not found and event.country:
            mapping = {"RU": "Moscow", "UA": "Kyiv", "PL": "Warsaw", "LT": "Vilnius",
                       "LV": "Riga", "EE": "Tallinn", "FI": "Helsinki", "RO": "Bucharest",
                       "MD": "Chisinau"}
            if event.country in mapping:
                found = [mapping[event.country]]
        if not found:
            return None
        capital = found[0]
        event.numeric_payload["capital"] = capital

        since = now_ts() - 72 * 3600
        rows = await db.fetchall(
            """SELECT numeric_payload FROM events
               WHERE ingested_at>=? AND matched_rules LIKE '%TW-EU-ORDERED-DEPARTURE%'""",
            (since,),
        )
        seen = {capital}
        for row in rows:
            cap = json_loads(row["numeric_payload"]).get("capital")
            if cap:
                seen.add(cap)
        if len(seen) >= 3:
            hit = self._hit(rule, capitals=sorted(seen))
            hit.level = L3
            hit.why += " Одновременный выезд из ≥3 столиц за 72 ч — признак подготовки к войне."
            return hit
        return self._hit(rule, capitals=sorted(seen))

    def _strike_nato(self, rule, text, event) -> TripwireHit | None:
        if rule["_regex"] is None or not rule["_regex"].search(text):
            return None
        if event.country not in self.nato:
            return None
        nt = norm_text(text)
        if not re.search(r"(killed|dead|casualt|injured|жертв|погиб|ранен)", nt):
            return None
        if event.tier > 1:
            return None
        return self._hit(rule, country=event.country)

    def _oref_directive(self, rule, text, event) -> TripwireHit | None:
        nt = norm_text(text)
        patterns = [
            r"(only essential activity|essential activity only|activities essential only)",
            r"(closure of (all )?(schools|educational institutions) (nationwide|across the country))",
            r"(הנחיות פיקוד העורף)",
            r"(פעילות חיונית בלבד)",
            r"(סגירת (כל )?(מוסדות החינוך|בתי הספר))",
            r"(только необходим\w+ деятельность)",
            r"(закрыт\w+ (все )?школ\w+ (по всей стране|в масштабах страны))",
        ]
        if not any(re.search(p, nt) for p in patterns):
            return None
        if event.source_id not in ("idf_tg", "israel_mfa_tg", "tzeva_notifications",
                                   "tzeva_history", "gnews_idf"):
            return None
        return self._hit(rule)

    async def _oref_share(self, rule, event, db) -> TripwireHit | None:
        if event.category != "israel":
            return None
        window_min = int(rule.get("window_min", 10))
        since = now_ts() - window_min * 60
        rows = await db.fetchall(
            """SELECT numeric_payload FROM events
               WHERE ingested_at>=? AND category='israel'
                 AND source_id IN ('tzeva_notifications','tzeva_history')""",
            (since,),
        )
        districts: set[str] = set()
        for row in rows:
            payload = json_loads(row["numeric_payload"])
            for d in payload.get("districts") or []:
                districts.add(d)
            for d in payload.get("district_list") or []:
                districts.add(d)
        total = len(self.cfg.geofences.get("oref_districts") or []) or 30
        share = len(districts) / total
        if share >= float(rule.get("min_share", 0.25)):
            return self._hit(rule, districts=sorted(districts), share=round(share, 3))
        return None

    def _volcano_hc(self, rule, event) -> TripwireHit | None:
        payload = event.numeric_payload
        name = payload.get("volcano_name") or payload.get("volcano") or ""
        colour = (payload.get("colour_code") or "").lower()
        if colour not in ("orange", "red"):
            return None
        if not name or norm_text(name) not in self.high_consequence:
            return None
        hit = self._hit(rule, volcano=name, colour_code=colour)
        if colour == "red":
            hit.why = "Красный код на high-consequence вулкане — извержение с региональными последствиями."
        return hit

    async def _volcano_swarm(self, rule, event, db) -> TripwireHit | None:
        from ..geo import haversine_km

        payload = event.numeric_payload
        mag = payload.get("mag")
        if mag is None or float(mag) < float(rule.get("min_mag", 4.5)):
            return None
        if event.lat is None or event.lon is None:
            return None
        radius = float(rule.get("radius_km", 30))
        zone = next(
            (name for name, lat, lon in self.swarm_zones
             if haversine_km(event.lat, event.lon, lat, lon) <= radius),
            None,
        )
        if zone is None:
            return None
        zone_coords = next((lat, lon) for name, lat, lon in self.swarm_zones if name == zone)
        since = now_ts() - int(float(rule.get("window_hours", 24)) * 3600)
        rows = await db.fetchall(
            "SELECT lat, lon, numeric_payload FROM events "
            "WHERE category='earthquake' AND ingested_at>=? AND lat IS NOT NULL AND lon IS NOT NULL",
            (since,),
        )
        min_mag = float(rule.get("min_mag", 4.5))
        count = 0
        for row in rows:
            if haversine_km(row["lat"], row["lon"], zone_coords[0], zone_coords[1]) > radius:
                continue
            if float(json_loads(row["numeric_payload"]).get("mag") or 0) >= min_mag:
                count += 1
        if count < int(rule.get("min_count", 10)):
            return None
        event.numeric_payload["swarm_zone"] = zone
        event.numeric_payload["swarm_count"] = count
        return self._hit(rule, zone=zone, count=count, window_hours=rule.get("window_hours", 24))
