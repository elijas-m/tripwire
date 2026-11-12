"""Layer B: lexical scoring, hard negatives, factuality."""

from __future__ import annotations

import re
from typing import Any

from ..models import Event, ScoreResult

# Escalation words inside a denial sentence ("US denies reports of an ordered
# departure") are the opposite signal and must not add score.
_DENIAL = re.compile(
    r"\b(?:denied|denies|deny|dismissed|dismisses|refuted|refutes|denial|"
    r"rumou?rs?|hoax|unconfirmed|not\s+true|no\s+evidence|no\s+plans?|ruled\s+out)\b"
    r"|\breports?\b[^.!?\n]{0,80}\b(?:are|is|was|were)\s+false\b"
    r"|(?:опроверг|опроверга|слух|не\s+подтверд|дезинформац|фейк|не\s+планир)",
    re.I,
)
_SENTENCE = re.compile(r"[^.!?\n]+")
_CYRILLIC = re.compile(r"[а-яё]")
_LATIN = re.compile(r"[a-z]")
# An inflected form carries the same signal: «взрыв» → «взрывы», «airstrike» → «airstrikes».
_RU_TAIL = r"[а-яё]{0,3}(?![\w])"
_EN_TAIL = r"(?:s|es)?(?![\w])"
_EXACT_TAIL = r"(?![\w])"


def _denied_ranges(text: str) -> list[tuple[int, int]]:
    return [(match.start(), match.end()) for match in _SENTENCE.finditer(text)
            if _DENIAL.search(match.group(0))]


def _term_pattern(term: str) -> re.Pattern[str] | None:
    term = term.strip().lower()
    if not term:
        return None
    if term.startswith("re:"):
        return re.compile(term[3:], re.I)
    chunks = []
    for chunk in term.split():
        if chunk.endswith("*"):
            chunks.append(re.escape(chunk[:-1]) + r"\w*")
        else:
            chunks.append(re.escape(chunk))
    body = r"\s+".join(chunks)
    if term[0].isalnum():
        body = r"(?<![\w])" + body
    if term[-1].isalnum() or term[-1] == "*":
        if _CYRILLIC.search(term):
            body = body + _RU_TAIL
        elif _LATIN.search(term):
            body = body + _EN_TAIL
        else:
            body = body + _EXACT_TAIL
    try:
        return re.compile(body, re.I)
    except re.error:
        return None


class LexiconEngine:
    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.groups: dict[str, float] = {
            k: float(v) for k, v in (cfg.lexicon.get("groups") or {}).items()
        }
        self.terms: list[tuple[str, float, str, re.Pattern[str]]] = []
        for lang, groups in (cfg.lexicon.get("terms") or {}).items():
            for group, words in groups.items():
                weight = self.groups.get(group, 1.0)
                for word in words:
                    pattern = _term_pattern(word)
                    if pattern:
                        self.terms.append((lang, weight, word, pattern))

        self.feature_headlines: list[re.Pattern[str]] = []
        for words in (cfg.lexicon.get("feature_headlines") or {}).values():
            for word in words:
                pattern = _term_pattern(word)
                if pattern:
                    self.feature_headlines.append(pattern)

        self.research_headlines: list[re.Pattern[str]] = []
        for words in (cfg.lexicon.get("research_headlines") or {}).values():
            for word in words:
                pattern = _term_pattern(word)
                if pattern:
                    self.research_headlines.append(pattern)

        self.procedural_headlines: list[re.Pattern[str]] = []
        for words in (cfg.lexicon.get("procedural_headlines") or {}).values():
            for word in words:
                pattern = _term_pattern(word)
                if pattern:
                    self.procedural_headlines.append(pattern)

        self.procedural_exceptions: list[re.Pattern[str]] = []
        for words in (cfg.lexicon.get("procedural_exceptions") or {}).values():
            for word in words:
                pattern = _term_pattern(word)
                if pattern:
                    self.procedural_exceptions.append(pattern)

        self.negatives: list[re.Pattern[str]] = []
        for words in (cfg.lexicon.get("negative") or {}).values():
            for word in words:
                pattern = _term_pattern(word)
                if pattern:
                    self.negatives.append(pattern)

        self.fact_verbs: list[re.Pattern[str]] = []
        for words in (cfg.lexicon.get("fact_verbs") or {}).values():
            for word in words:
                pattern = _term_pattern(word)
                if pattern:
                    self.fact_verbs.append(pattern)

        trust = cfg.thresholds.get("source_trust", {})
        self.trust = {int(k): float(v) for k, v in trust.items()}
        self.fact_factor = float(cfg.thresholds.get("factuality", {}).get("fact", 1.0))
        self.no_fact_factor = float(cfg.thresholds.get("factuality", {}).get("no_fact", 0.3))

    def score(self, event: Event, geo_factor: float, novelty_factor: float) -> ScoreResult:
        text = f"{event.title} {event.body_excerpt[:700]}".lower().replace("ё", "е")
        denied = _denied_ranges(text)
        matches: list[tuple[float, str]] = []
        for _lang, weight, word, pattern in self.terms:
            for match in pattern.finditer(text):
                if not any(start <= match.start() < end for start, end in denied):
                    matches.append((weight, word))
                    break

        base = 0.0
        if matches:
            weights = sorted((w for w, _ in matches), reverse=True)
            base = weights[0] + 0.5 * sum(weights[1:])
            base = min(base, 12.0)
        matched_terms = [w for _, w in matches]

        has_fact = any(p.search(text) for p in self.fact_verbs)
        negative = any(p.search(text) for p in self.negatives)
        tier = event.tier

        title = event.title.lower().replace("ё", "е")
        if any(p.search(title) for p in self.feature_headlines):
            return ScoreResult(score=0.0, base=base, matched=matched_terms,
                               factors={"geo": geo_factor, "novelty": novelty_factor},
                               dropped=True, drop_reason="feature_article")

        if any(p.search(title) for p in self.research_headlines):
            return ScoreResult(score=0.0, base=base, matched=matched_terms,
                               factors={"geo": geo_factor, "novelty": novelty_factor},
                               dropped=True, drop_reason="research_article")

        if (any(p.search(title) for p in self.procedural_headlines)
                and not any(p.search(title) for p in self.procedural_exceptions)):
            return ScoreResult(score=0.0, base=base, matched=matched_terms,
                               factors={"geo": geo_factor, "novelty": novelty_factor},
                               dropped=True, drop_reason="procedural_meeting")

        if negative and not has_fact and tier > 0:
            return ScoreResult(score=0.0, base=base, matched=matched_terms,
                               factors={"geo": geo_factor, "novelty": novelty_factor},
                               dropped=True, drop_reason="hard_negative")

        fact_factor = self.fact_factor if (has_fact or tier == 0) else self.no_fact_factor
        trust = self.trust.get(tier, 0.25)
        score = base * trust * fact_factor * geo_factor * novelty_factor
        return ScoreResult(
            score=round(score, 3), base=round(base, 3), matched=matched_terms,
            factors={
                "trust": trust, "factuality": fact_factor, "geo": geo_factor,
                "novelty": novelty_factor,
            },
        )

    def has_fact_verb(self, text: str) -> bool:
        nt = text.lower().replace("ё", "е")
        return any(p.search(nt) for p in self.fact_verbs)
