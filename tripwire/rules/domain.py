"""Domain rules: nature thresholds and accumulating indicators (§7)."""

from __future__ import annotations

import re
from ..models import Event, ScoreResult
from ..util import norm_text

_OUTBREAK_EVIDENCE_RE = re.compile(
    r"\b(?:outbreak|epidemic|pandemic|upsurge|surge|cluster of cases|"
    r"confirmed cases|reported cases|death toll|human[- ]to[- ]human|"
    r"вспышк\w*|эпидеми\w*)\b"
    r"|\b\d[\d, ]{0,10}\s+(?:confirmed\s+|reported\s+)?(?:cases|deaths|infected)\b"
)
_AWARENESS_RE = re.compile(
    r"\b(?:world\s+[\w-]+\s+day|eradication|vaccination|immuni[sz]ation|"
    r"vaccine campaign|awareness day)\b"
)
_OUTBREAK_ENDING_RE = re.compile(
    r"\b(?:to end|ending|eradication|eliminat\w*|contained|declared over|"
    r"no longer)\b[^.]{0,60}\b(?:outbreak|epidemic)\b"
    r"|\b(?:outbreak|epidemic)\b[^.]{0,40}\b(?:is over|has ended|was contained|"
    r"declared over)\b"
)


class DomainEngine:
    def __init__(self, cfg, geo=None) -> None:
        self.cfg = cfg
        self.geo = geo
        self.th = cfg.thresholds.get("domain", {})
        self.trust = {int(k): float(v) for k, v in cfg.thresholds.get("source_trust", {}).items()}
        self.high_consequence = {
            norm_text(v) for v in cfg.geofences.get("high_consequence_volcanoes", [])
        }
        self.watchlist = [
            re.compile(r"(?<![\w])" + re.escape(norm_text(v)) + r"(?:virus(?:es)?)?(?![\w])")
            for v in dict.fromkeys(cfg.geofences.get("watchlist_pathogens", []))
            if norm_text(v)
        ]

    # ---------- nature ----------
    def evaluate(self, event: Event, geo_factor: float, novelty_factor: float) -> ScoreResult | None:
        if event.category in ("earthquake", "tsunami", "volcano", "disaster"):
            return self._nature(event, geo_factor, novelty_factor)
        return None

    def _nature(self, event: Event, geo_factor: float, novelty_factor: float) -> ScoreResult:
        p = event.numeric_payload
        trust = self.trust.get(event.tier, 0.25)
        reasons: list[str] = []
        points = 0.0

        if event.category == "earthquake":
            cfg = self.th.get("earthquake", {})
            mag = float(p.get("mag") or 0)
            if mag < float(cfg.get("ignore_below", 6.0)):
                return ScoreResult(0.0, 0.0, [], {}, dropped=True, drop_reason="mag_below_6")
            alert = (p.get("alert") or "").lower()
            if alert == "red":
                points = float(cfg.get("pager_red", 20.0))
                reasons.append("PAGER red")
            elif alert == "orange":
                points = float(cfg.get("pager_orange", 10.0))
                reasons.append("PAGER orange")
            elif mag >= float(cfg.get("critical", 7.5)):
                points = 20.0
                reasons.append(f"M{mag:.1f}")
            elif mag >= float(cfg.get("warning", 6.5)):
                points = float(self.cfg.thresholds["levels"]["warning"])
                reasons.append(f"M{mag:.1f}")
                if alert == "yellow":
                    points = max(points, 12.0)
                    reasons.append("PAGER yellow")
            elif mag >= float(cfg.get("watch", 6.0)):
                points = float(self.cfg.thresholds["levels"]["watch"])
                reasons.append(f"M{mag:.1f}")

        elif event.category == "volcano":
            cfg = self.th.get("volcano", {})
            colour = (p.get("colour_code") or "").lower()
            ash_ft = p.get("ash_ft")
            vei = p.get("vei")
            name = norm_text(p.get("volcano_name") or "")
            if colour == "red":
                points = 20.0
                reasons.append("colour code red")
            elif colour == "orange":
                points = float(cfg.get("orange_points", 6.0))
                reasons.append("colour code orange")
                if name in self.high_consequence:
                    points = float(cfg.get("high_consequence_orange", 10.0))
                    reasons.append("high-consequence volcano")
            if ash_ft:
                ash_ft = float(ash_ft)
                if ash_ft >= float(cfg.get("ash_critical_ft", 45000)):
                    points = max(points, 20.0)
                    reasons.append(f"ash {ash_ft/1000:.1f}k ft")
                elif ash_ft >= float(cfg.get("ash_warning_ft", 30000)):
                    points = max(points, 8.0)
                    reasons.append(f"ash {ash_ft/1000:.1f}k ft")
            if vei and float(vei) >= 4:
                points = max(points, 20.0)
                reasons.append(f"VEI {vei}")
            if points == 0:
                return ScoreResult(0.0, 0.0, [], {}, dropped=True, drop_reason="volcano_below_threshold")

        elif event.category == "tsunami":
            mtype = (p.get("message_type") or "").lower()
            if mtype == "warning":
                points = 20.0
                reasons.append("tsunami warning")
            elif mtype in ("watch", "advisory"):
                points = 8.0
                reasons.append(f"tsunami {mtype}")
            else:
                return ScoreResult(0.0, 0.0, [], {}, dropped=True, drop_reason="tsunami_info")

        elif event.category == "disaster":
            level = (p.get("alertlevel") or "").lower()
            if level == "red":
                points = 16.0
                reasons.append("GDACS red")
            elif level == "orange":
                points = 8.0
                reasons.append("GDACS orange")
            else:
                return ScoreResult(0.0, 0.0, [], {}, dropped=True, drop_reason="gdacs_green")

        score = round(points * trust * geo_factor * novelty_factor, 3)
        return ScoreResult(score, round(points, 3), reasons,
                           {"trust": trust, "geo": geo_factor, "novelty": novelty_factor})

    def _watchlist_hit(self, text: str) -> bool:
        """A pathogen name alone is not escalation: require outbreak evidence."""
        if not self.watchlist or not _OUTBREAK_EVIDENCE_RE.search(text):
            return False
        if _OUTBREAK_ENDING_RE.search(text):
            return False
        if _AWARENESS_RE.search(text) and not re.search(
            r"\b(?:outbreak|epidemic|deaths?)\b", text
        ):
            return False
        return any(pattern.search(text) for pattern in self.watchlist)

    # ---------- accumulating indicators ----------
    def bonus(self, event: Event) -> tuple[float, list[str]]:
        text = norm_text(f"{event.title} {event.body_excerpt[:900]}")
        payload = event.numeric_payload
        points = 0.0
        reasons: list[str] = []

        def hit(pattern: str, value: float, label: str) -> None:
            nonlocal points
            if re.search(pattern, text, re.S):
                points += value
                reasons.append(label)

        # Force projection is a signal about the destination theater, so it is
        # scored from the headline and applies to every military theater.
        if (event.theater in ("EU_RU", "ISRAEL", "GULF") and self.geo is not None
                and self.geo.is_force_projection(event.title)):
            points += float(self.th.get("force_projection", {}).get("points", 8.0))
            reasons.append("force projection")

        if event.theater == "PANDEMIC":
            cfg = self.th.get("pandemic", {})
            hit(r"\b(novel|new)\s+(pathogen|virus|reassort\w*|influenza)", cfg.get("new_pathogen", 4), "new pathogen")
            hit(r"human[- ]to[- ]human|от человека к человеку|מאדם לאדם", cfg.get("h2h", 6), "h2h transmission")
            hit(r"\b(ihr|international health regulations)\b.{0,60}\b(committee|emergency)", cfg.get("ihr_committee", 8), "IHR committee")
            hit(r"\b(border|borders)\b.{0,40}\b(clos|shut)|\b(border closure|закрыт\w+ границ)", cfg.get("border_closure", 6), "border closure")
            if self._watchlist_hit(text):
                points += float(cfg.get("watchlist_points", 4))
                reasons.append("watch-list pathogen")
            if self.geo is not None:
                countries = {
                    iso for iso, _alias, _len in self.geo.match_countries(
                        f"{event.title} {event.body_excerpt[:900]}")
                }
                if len(countries) >= 3:
                    points += float(cfg.get("three_countries", 5))
                    reasons.append(f"{len(countries)} countries")
            cfr = re.search(
                r"\b(?:cfr|case[- ]fatality(?:\s+ratio)?)\b[^%\d]{0,25}(\d{1,3}(?:\.\d+)?)\s?%",
                text,
            )
            if cfr and float(cfr.group(1)) > 10:
                cases = re.search(r"\b(\d[\d, ]{1,})\s+(?:confirmed\s+)?cases", text)
                count = int(re.sub(r"[\s,]", "", cases.group(1))) if cases else 0
                if count >= 10:
                    points += float(cfg.get("high_cfr", 5))
                    reasons.append(f"CFR {float(cfr.group(1)):.0f}%")

        elif event.theater == "EU_RU":
            cfg = self.th.get("eu_ru", {})
            hit(r"\b(embassy|consulate)\b.{0,60}\b(evacuat|clos|suspend)", cfg.get("embassy_evac", 5), "embassy evacuation")
            hit(r"\b(border (crossing|checkpoint|section))\b.{0,60}\b(clos|shut|suspend)", cfg.get("border_section_closed", 3), "border section closed")
            hit(r"\b(airspace)\b.{0,50}\b(clos|restrict|suspend)", cfg.get("region_airspace_closed", 5), "airspace closed")
            hit(r"\b(airspace violation|violated .{0,20}airspace|наруш\w+ воздушн\w+ пространств)", cfg.get("airspace_violation", 4), "airspace violation")
            hit(r"\b(shot down|сбит\w*)\b.{0,40}\b(aircraft|jet|drone|самолет|дрон)", cfg.get("airspace_shootdown", 7), "aircraft shot down")
            hit(r"\b(nrf|nato response force|rapid reaction force)\b", cfg.get("nrf_activation", 6), "NATO force activation")
            hit(r"\b(reservists?|резервист\w*)\b.{0,40}\b(call|mobil|призыв|мобилиз)", cfg.get("reservists", 6), "reservists called up")
            hit(r"\b(gps|gnss)\b.{0,30}\b(jamm|interfer|глуш)", cfg.get("gps_jamming", 2), "GPS jamming")
            hit(r"\b(citizens?|nationals?)\b.{0,60}\b(evacuat|leave the country|покинуть страну)", cfg.get("citizens_evac", 5), "citizens evacuation")
            if payload.get("aircraft_count") is not None and payload.get("flights_drop_pct"):
                if float(payload["flights_drop_pct"]) >= 60:
                    points += float(cfg.get("civil_flights_drop", 7))
                    reasons.append("civil flights down >60%")
            if payload.get("notable") and len(payload["notable"]) >= 2:
                points += float(cfg.get("vipplane_activity", 2))
                reasons.append("VIP aircraft activity")
            if payload.get("all_ukraine") and event.category == "eu_ru":
                points += float(cfg.get("ukraine_mass_alerts", 3))
                reasons.append("Ukraine mass alerts")

        elif event.theater == "ISRAEL":
            cfg = self.th.get("israel", {})
            cities = " ".join(payload.get("cities") or [])
            nc = norm_text(cities)
            if nc and any(c in nc for c in ("tel aviv", "jerusalem", "тель-авив", "иерусалим", "תל אביב", "ירושלים")):
                points += float(cfg.get("oref_center", 4))
                reasons.append("alert in center")
            hit(r"\b(ballistic missile|ballistic missiles|שיגור טילים)\b", cfg.get("ballistic_launch", 8), "ballistic launch")
            hit(r"\b(ben[- ]gurion|llbg|נתב[\"']?ג)\b.{0,50}\b(clos|shut|suspend)", cfg.get("ben_gurion_closed", 7), "Ben Gurion closed")
            hit(r"\b(reservists?)\b.{0,40}\b(30[, ]?000|thirty thousand)", cfg.get("reservists_30k", 7), "30k reservists")
            hit(r"\b(nuclear (site|facility|strike))\b", cfg.get("nuclear_strike", 9), "nuclear site strike")
            hit(r"\b(iaea)\b.{0,80}\b(access|inspectors?)\b.{0,40}\b(lost|denied|withdraw|прекрат)", cfg.get("iaea_access_loss", 5), "IAEA access loss")

        elif event.theater == "GULF":
            cfg = self.th.get("gulf", {})
            hit(r"\b(ras tanura|abqaiq|jubail|khurais|oil (facility|infrastructure|refinery|terminal))\b.{0,80}\b(attack|strike|hit|fire|explosion)", cfg.get("oil_infra_attack", 8), "oil infrastructure attack")
            hit(r"\b(vessel|tanker|ship)\b.{0,60}\b(incident|attack|hit|damaged|fire)", cfg.get("vessel_incident", 3), "vessel incident")
            hit(r"\b(vessel|tanker|ship)\b.{0,60}\b(seized|hijack|boarded|захвач)", cfg.get("vessel_seizure", 6), "vessel seizure")
            if payload.get("symbol") == "ICEEUR:BRN1!" and payload.get("change_pct"):
                if float(payload["change_pct"]) >= 15:
                    points += float(cfg.get("brent_15pct", 6))
                    reasons.append("Brent +15%")

        return round(points, 3), reasons
