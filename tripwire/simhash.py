"""SimHash-64 for near-duplicate detection."""

from __future__ import annotations

import hashlib
import re
from typing import Iterable

_TOKEN_RE = re.compile(r"[\w\u0400-\u04FF\u0590-\u05FF]+", re.UNICODE)


def tokenize(text: str, stopwords: set[str], numbers: bool = True) -> list[str]:
    tokens = []
    for tok in _TOKEN_RE.findall(text):
        tok = tok.lower().replace("ё", "е")
        if len(tok) < 2 and not tok.isdigit():
            continue
        if not numbers and tok.isdigit():
            continue
        if tok in stopwords:
            continue
        tokens.append(tok)
    return tokens


def _hash64(token: str) -> int:
    digest = hashlib.blake2b(token.encode("utf-8", "ignore"), digest_size=8).digest()
    return int.from_bytes(digest, "big")


def simhash(tokens: Iterable[str]) -> int:
    vector = [0] * 64
    count = 0
    for token in tokens:
        h = _hash64(token)
        for i in range(64):
            vector[i] += 1 if (h >> i) & 1 else -1
        count += 1
    if count == 0:
        return 0
    result = 0
    for i in range(64):
        if vector[i] > 0:
            result |= 1 << i
    return result


def compute(title: str, body: str, stopwords: set[str]) -> int:
    title_tokens = tokenize(title, stopwords)
    body_tokens = tokenize(body[:400], stopwords)
    weighted: list[str] = title_tokens * 3 + body_tokens
    value = simhash(weighted)
    # SQLite INTEGER is signed 64-bit; keep the same bit pattern without overflow.
    return value - (1 << 64) if value >= (1 << 63) else value


def distance(a: int, b: int) -> int:
    return ((a ^ b) & 0xFFFFFFFFFFFFFFFF).bit_count()
