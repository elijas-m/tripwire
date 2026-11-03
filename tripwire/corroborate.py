"""Which cluster members may corroborate a trigger.

A cluster is a bucket: unrelated older stories can land in it through a shared
place name or a generic word. Counting every bucket member as confirmation
inflates publisher independence and can turn one wire copy into CRITICAL, so
only events that describe the same trigger are allowed to corroborate it.
"""

from __future__ import annotations

import re
from typing import Any

from .models import Event
from .simhash import distance as simhash_distance
from .util import json_loads, norm_text, now_ts, to_ts

WINDOW_HOURS = 72.0
_TOKEN_RE = re.compile(r"[\w\u0400-\u04FF\u0590-\u05FF]{4,}")

_STOPWORDS = frozenset(
    "about after again against along almost also although among around because "
    "before being between breaking could during every first found have into "
    "latest more most news other over report reports said says since still "
    "such than that their them then there these they this those through under "
    "update while will with would".split()
)

# Russian headlines are inflected; stems cover the common filler words.
_RU_STEMS = (
    "котор", "после", "более", "через", "также", "может", "будет", "своих",
    "своей", "этого", "этой", "кроме", "около", "среди",
)


def _field(peer: Any, name: str) -> Any:
    if isinstance(peer, dict):
        return peer.get(name)
    return getattr(peer, name, None)


def _tokens(text: str | None) -> set[str]:
    return {
        token for token in _TOKEN_RE.findall(norm_text(text))
        if token not in _STOPWORDS and not token.isdigit()
        and not any(token.startswith(stem) for stem in _RU_STEMS)
    }


def title_overlap(left: str | None, right: str | None) -> bool:
    """Same story in different wording: shared specifics dominate both headlines."""
    a, b = _tokens(left), _tokens(right)
    shared = a & b
    if len(shared) >= 3:
        return True
    if len(shared) == 2:
        union = a | b
        return bool(union) and len(shared) / len(union) >= 0.4
    return False


def trigger_relevant(event: Event, peer: Event | dict[str, Any],
                     rule_id: str | None = None, *,
                     window_h: float = WINDOW_HOURS, hamming_dup: int = 6,
                     now: int | None = None) -> bool:
    """Whether a stored event may count as corroboration for the trigger."""
    if peer is event:
        return True
    peer_id = _field(peer, "id")
    if peer_id is not None and event.id is not None and int(peer_id) == int(event.id):
        return True

    ingested = _field(peer, "ingested_at")
    if ingested is not None:
        ts = ingested if isinstance(ingested, (int, float)) else to_ts(ingested)
        current = now_ts() if now is None else now
        if ts is not None and current - int(ts) > window_h * 3600:
            return False

    if rule_id:
        rules = json_loads(_field(peer, "matched_rules") or "[]", []) or []
        if isinstance(rules, str):
            rules = [rules]
        elif not isinstance(rules, list):
            rules = []
        if rule_id in rules:
            return True

    peer_hash = _field(peer, "simhash")
    if event.simhash and peer_hash:
        try:
            if simhash_distance(event.simhash, int(peer_hash)) <= hamming_dup:
                return True
        except (TypeError, ValueError):
            pass

    return title_overlap(event.title, _field(peer, "title"))
