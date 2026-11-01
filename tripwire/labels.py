"""Blind label-candidate export for the geopolitical calibration set."""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Iterable

from .db import DB
from .util import from_ts, now_ts, utcnow

log = logging.getLogger(__name__)

GEO_THEATERS = ("EU_RU", "ISRAEL", "GULF", "MENA")
SCHEMA_VERSION = 2
NEAR_MISS_MIN = 4.0
NEAR_MISS_MAX = 8.0
MAX_EVIDENCE_EVENTS = 8
# Sources that report precursors (airspace closures, evacuation orders, attacks
# in progress) rather than post-hoc news. Calibration needs their episodes, so
# they get their own stratum instead of being diluted in the random pool.
PRECURSOR_SOURCES = (
    "easa_czib", "state_travel_rss", "fcdo_atom",
    "ukmto_jina", "tzeva_notifications", "tzeva_history",
)


def _iso(ts: int | float | None) -> str | None:
    moment = from_ts(ts)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ") if moment else None


def _candidate_id(cluster_id: int, first_seen: int) -> str:
    return f"cluster:{cluster_id}@{_iso(first_seen)}"


def _sample(pool: list[Any], count: int, seed: str) -> list[Any]:
    if count <= 0:
        return []
    ranked = sorted(pool, key=lambda row: hashlib.sha1(
        f"{seed}:{row['id']}".encode()).hexdigest())
    return ranked[:count]


def _extra_conditions(
    until: int | None, require_anchor: bool,
) -> tuple[str, tuple[Any, ...]]:
    """SQL tail and parameters shared by every stratum query."""
    clauses: list[str] = []
    params: list[Any] = []
    if until is not None:
        clauses.append("AND c.first_seen<=?")
        params.append(until)
    if require_anchor:
        clauses.append("AND c.score_initial IS NOT NULL")
    return "".join(f" {clause}" for clause in clauses), tuple(params)


def _load_exclusions(
    paths: Iterable[str | Path], skip_dir: Path,
) -> tuple[set[str], set[int]]:
    """Completed labels only: a candidate is spent once it has a case_type.

    Directories are scanned for their canonical ``labels.jsonl``; blank
    templates and draft files are not evidence that a human labeled a round.
    """
    candidates: set[str] = set()
    clusters: set[int] = set()
    skip = Path(skip_dir).resolve()
    for raw in paths:
        path = Path(raw)
        if not path.exists():
            raise FileNotFoundError(f"exclude path not found: {path}")
        files = [path] if path.is_file() else sorted(path.rglob("labels.jsonl"))
        for file in files:
            resolved = file.resolve()
            if resolved == skip or skip in resolved.parents:
                continue
            text = resolved.read_text(encoding="utf-8")
            for number, line in enumerate(text.splitlines(), 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"{resolved}:{number}: invalid JSON ({exc.msg})") from exc
                if not row.get("label_id") or row.get("case_type") in (None, ""):
                    continue
                if row.get("candidate_id"):
                    candidates.add(str(row["candidate_id"]))
                try:
                    clusters.add(int(row["cluster_id"]))
                except (KeyError, TypeError, ValueError):
                    pass
    return candidates, clusters


async def _alert_rows(
    db: DB, theater: str, since: int, cap: int,
    until: int | None = None, require_anchor: bool = False,
) -> list[Any]:
    extra, extra_params = _extra_conditions(until, require_anchor)
    rows = await db.fetchall(
        f"""SELECT c.*, MIN(COALESCE(a.decided_at, a.sent_at)) AS decided_at,
                  MAX(a.level) AS alert_level, COUNT(a.id) AS alert_count
           FROM clusters c JOIN alerts a ON a.cluster_id = c.id
           WHERE c.theater=? AND c.first_seen>=?{extra} AND a.level>=2
           GROUP BY c.id
           ORDER BY decided_at DESC""",
        (theater, since, *extra_params),
    )
    return rows[:cap] if cap > 0 else rows


async def _near_miss_rows(
    db: DB, theater: str, since: int, low: float, high: float, cap: int,
    until: int | None = None, require_anchor: bool = False,
) -> list[Any]:
    extra, extra_params = _extra_conditions(until, require_anchor)
    return await db.fetchall(
        f"""SELECT c.*, NULL AS decided_at, 0 AS alert_level, 0 AS alert_count
           FROM clusters c
           WHERE c.theater=? AND c.first_seen>=?{extra}
             AND c.total_score>=? AND c.total_score<?
             AND EXISTS (SELECT 1 FROM events e WHERE e.cluster_id=c.id)
             AND NOT EXISTS (SELECT 1 FROM alerts a
                             WHERE a.cluster_id=c.id AND a.level>=2)
           ORDER BY c.total_score DESC, c.last_seen DESC
           LIMIT ?""",
        (theater, since, *extra_params, low, high, cap),
    )


async def _random_rows(
    db: DB, theater: str, since: int, below: float, count: int, seed: str,
    exclude: set[int] | None = None,
    until: int | None = None, require_anchor: bool = False,
) -> list[Any]:
    if count <= 0:
        return []
    extra, extra_params = _extra_conditions(until, require_anchor)
    pool = await db.fetchall(
        f"""SELECT c.*, NULL AS decided_at, 0 AS alert_level, 0 AS alert_count
           FROM clusters c
           WHERE c.theater=? AND c.first_seen>=?{extra} AND c.total_score<?
             AND EXISTS (SELECT 1 FROM events e WHERE e.cluster_id=c.id)
             AND NOT EXISTS (SELECT 1 FROM alerts a
                             WHERE a.cluster_id=c.id AND a.level>=2)
           ORDER BY c.id""",
        (theater, since, *extra_params, below),
    )
    if exclude:
        pool = [row for row in pool if row["id"] not in exclude]
    return _sample(pool, count, seed)


async def _precursor_rows(
    db: DB, theater: str, since: int,
    until: int | None = None, require_anchor: bool = False,
) -> list[Any]:
    """Clusters whose first event came from a precursor source."""
    placeholders = ",".join("?" for _ in PRECURSOR_SOURCES)
    extra, extra_params = _extra_conditions(until, require_anchor)
    return await db.fetchall(
        f"""SELECT c.*, NULL AS decided_at, 0 AS alert_level, 0 AS alert_count
            FROM clusters c
            WHERE c.theater=? AND c.first_seen>=?{extra}
              AND EXISTS (SELECT 1 FROM events e
                          WHERE e.cluster_id=c.id AND e.source_id IN ({placeholders})
                            AND e.id=(SELECT MIN(e2.id) FROM events e2
                                      WHERE e2.cluster_id=c.id))
              AND NOT EXISTS (SELECT 1 FROM alerts a
                              WHERE a.cluster_id=c.id AND a.level>=2)
            ORDER BY c.id""",
        (theater, since, *extra_params, *PRECURSOR_SOURCES),
    )


async def _candidate(db: DB, row: Any) -> dict[str, Any]:
    events = await db.fetchall(
        """SELECT title, body_excerpt, url, source_id, tier, origin_domain, published_at
           FROM events WHERE cluster_id=?
           ORDER BY COALESCE(published_at, ingested_at), id
           LIMIT ?""",
        (row["id"], MAX_EVIDENCE_EVENTS),
    )
    return {
        "candidate_id": _candidate_id(row["id"], row["first_seen"]),
        "cluster_id": row["id"],
        "theater": row["theater"],
        "category": row["category"],
        "country": row["country"],
        "title": row["title_canonical"] or "",
        "first_seen": _iso(row["first_seen"]),
        "last_seen": _iso(row["last_seen"]),
        "events": [
            {
                "title": item["title"],
                "body": item["body_excerpt"] or "",
                "url": item["url"],
                "source_id": item["source_id"],
                "tier": item["tier"],
                "origin_domain": item["origin_domain"],
                "published_at": _iso(item["published_at"]),
            }
            for item in events
        ],
    }


def _template_row(candidate: dict[str, Any]) -> dict[str, Any]:
    first_seen = str(candidate.get("first_seen") or "")
    month = first_seen[:7] if len(first_seen) >= 7 else "0000-00"
    return {
        "label_id": f"L{month}-{candidate['cluster_id']}",
        "candidate_id": candidate["candidate_id"],
        "cluster_id": candidate["cluster_id"],
        "theater": candidate["theater"],
        "subject": None,
        "in_scope": True,
        "case_type": None,
        "outcome": None,
        "outcome_type": None,
        "outcome_severity": None,
        "outcome_at": None,
        "observed_until": None,
        "evidence": [],
        "confidence": None,
        "annotator": "",
        "labeled_at": None,
        "supersedes": None,
        "notes": "",
    }


async def export_label_candidates(
    db: DB,
    out_dir: Path,
    *,
    since_days: int = 30,
    negatives_per_theater: int = 0,
    alert_cap: int = 0,
    precursor_cap: int = 0,
    near_miss_cap: int = 50,
    seed: str = "tripwire",
    near_miss_range: tuple[float, float] = (NEAR_MISS_MIN, NEAR_MISS_MAX),
    min_age_hours: float = 0.0,
    require_anchor: bool = False,
    exclude_paths: Iterable[str | Path] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Write blind candidates, template, sealed scores and a manifest.

    Never overwrites labels. ``exclude_paths`` scans completed labels and skips
    their candidates, so a weekly round only offers new episodes.
    """
    out_dir = Path(out_dir)
    candidates_path = out_dir / "candidates.jsonl"
    template_path = out_dir / "labels.template.jsonl"
    sealed_path = out_dir / "sealed.jsonl"
    manifest_path = out_dir / "manifest.json"
    if not force:
        for path in (candidates_path, template_path, sealed_path, manifest_path):
            if path.exists():
                raise FileExistsError(f"{path} already exists; pass force=True to overwrite")

    now = now_ts()
    since = now - since_days * 86400
    until = now - int(min_age_hours * 3600) if min_age_hours > 0 else None
    low, high = near_miss_range
    exclude_list = [str(path) for path in (exclude_paths or [])]
    excluded_candidates, excluded_clusters = _load_exclusions(exclude_list, out_dir)
    candidates: list[dict[str, Any]] = []
    sealed: list[dict[str, Any]] = []
    counts = {"alert": 0, "precursor": 0, "near_miss": 0, "random": 0}
    selected: set[int] = set()

    async def add(row: Any, stratum: str) -> None:
        if row["id"] in selected or row["id"] in excluded_clusters:
            return
        candidate = await _candidate(db, row)
        if not candidate["events"]:
            return
        if candidate["candidate_id"] in excluded_candidates:
            return
        selected.add(row["id"])
        candidates.append(candidate)
        counts[stratum] += 1
        sealed.append({
            "candidate_id": candidate["candidate_id"],
            "cluster_id": row["id"],
            "stratum": stratum,
            # Calibration uses the score frozen on the first event; the final
            # score is kept for audit only.
            "score_initial": (round(float(row["score_initial"]), 2)
                              if row["score_initial"] is not None else None),
            "score": round(float(row["total_score"] or 0.0), 2),
            "level": int(row["level"] or 0),
            "alert_level": int(row["alert_level"] or 0),
            "alert_count": int(row["alert_count"] or 0),
            "decided_at": _iso(row["decided_at"]),
            "first_seen": _iso(row["first_seen"]),
        })

    precursor_pool: list[Any] = []
    for theater in GEO_THEATERS:
        for row in await _alert_rows(db, theater, since, alert_cap,
                                     until, require_anchor):
            if row["id"] not in excluded_clusters:
                await add(row, "alert")
        precursor_pool.extend(
            row for row in await _precursor_rows(db, theater, since,
                                                 until, require_anchor)
            if row["id"] not in excluded_clusters
        )
    if precursor_cap > 0:
        for row in _sample(precursor_pool, precursor_cap, seed):
            await add(row, "precursor")
    for theater in GEO_THEATERS:
        for row in await _random_rows(db, theater, since, low,
                                      negatives_per_theater, seed,
                                      exclude=selected | excluded_clusters,
                                      until=until,
                                      require_anchor=require_anchor):
            await add(row, "random")
    near_pool: list[Any] = []
    for theater in GEO_THEATERS:
        near_pool.extend(
            row for row in await _near_miss_rows(db, theater, since, low, high, 200,
                                                 until, require_anchor)
            if row["id"] not in selected and row["id"] not in excluded_clusters
        )
    near_selected = (_sample(near_pool, near_miss_cap, seed)
                     if near_miss_cap > 0 else near_pool)
    for row in near_selected:
        await add(row, "near_miss")

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _iso(now),
        "since_days": since_days,
        "first_seen_max": _iso(until),
        "min_age_hours": min_age_hours,
        "require_anchor": require_anchor,
        "theaters": list(GEO_THEATERS),
        "seed": seed,
        "near_miss_range": [low, high],
        "exclude_paths": exclude_list,
        "excluded_known": len(excluded_candidates),
        "counts": counts,
        "candidates": len(candidates),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_jsonl(candidates_path, candidates)
    _write_jsonl(template_path, [_template_row(item) for item in candidates])
    _write_jsonl(sealed_path, sealed)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log.info("label export: %s -> %s", counts, out_dir)
    return {"out": str(out_dir), "counts": counts, "candidates": len(candidates),
            "manifest": manifest}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            text = line.strip()
            if not text:
                continue
            try:
                rows.append(json.loads(text))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{number}: invalid JSON ({exc.msg})") from exc
    return rows


def merge_label_rounds(root: Path, out_dir: Path) -> dict[str, Any]:
    """Merge dated rounds into one ``labels.jsonl`` + ``sealed.jsonl`` pair."""
    from .calibration import resolve_labels

    root = Path(root).resolve()
    out_dir = Path(out_dir).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"{root} is not a directory")
    if out_dir == root:
        raise ValueError("merged output must differ from the round root")

    def round_dirs(name: str) -> list[Path]:
        found: list[Path] = []
        for path in sorted(root.rglob(name)):
            directory = path.resolve().parent
            if directory == out_dir or out_dir in directory.parents:
                continue
            if directory not in found:
                found.append(directory)
        return found

    label_rounds = round_dirs("labels.jsonl")
    sealed_rounds = round_dirs("sealed.jsonl")
    if not label_rounds and not sealed_rounds:
        raise FileNotFoundError(f"no labels.jsonl or sealed.jsonl under {root}")

    raw_labels: list[dict[str, Any]] = []
    skipped_incomplete = 0
    for directory in label_rounds:
        for row in _read_jsonl(directory / "labels.jsonl"):
            if not row.get("candidate_id") or not row.get("case_type"):
                skipped_incomplete += 1
                continue
            raw_labels.append(row)

    superseded = {row.get("supersedes") for row in raw_labels if row.get("supersedes")}
    current: dict[str, set[str]] = {}
    for row in raw_labels:
        if row.get("label_id") in superseded:
            continue
        current.setdefault(str(row["candidate_id"]), set()).add(str(row.get("label_id")))
    conflicting_labels = sum(1 for ids in current.values() if len(ids) > 1)

    resolved = resolve_labels(raw_labels)

    sealed: dict[str, dict[str, Any]] = {}
    duplicate_sealed = 0
    conflicting_sealed = 0
    for directory in sealed_rounds:
        for row in _read_jsonl(directory / "sealed.jsonl"):
            candidate_id = str(row.get("candidate_id") or "")
            if not candidate_id:
                continue
            current_sealed = sealed.get(candidate_id)
            if current_sealed is not None:
                duplicate_sealed += 1
                if (current_sealed.get("score_initial") is None
                        and row.get("score_initial") is not None):
                    sealed[candidate_id] = row
                elif (current_sealed.get("score_initial") is not None
                      and row.get("score_initial") is not None
                      and current_sealed.get("score_initial") != row.get("score_initial")):
                    conflicting_sealed += 1
                continue
            sealed[candidate_id] = row

    labels = [resolved[candidate_id] for candidate_id in sorted(resolved)]
    sealed_rows = [sealed[candidate_id] for candidate_id in sorted(sealed)]

    out_dir.mkdir(parents=True, exist_ok=True)
    _write_jsonl(out_dir / "labels.jsonl", labels)
    _write_jsonl(out_dir / "sealed.jsonl", sealed_rows)
    rounds = sorted(set(label_rounds) | set(sealed_rounds))
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _iso(now_ts()),
        "root": str(root),
        "rounds": [
            str(directory.relative_to(root)) if directory != root else "."
            for directory in rounds
        ],
        "labels_raw": len(raw_labels),
        "labels": len(labels),
        "sealed": len(sealed_rows),
        "skipped_incomplete": skipped_incomplete,
        "conflicting_labels": conflicting_labels,
        "duplicate_sealed": duplicate_sealed,
        "conflicting_sealed": conflicting_sealed,
        "labels_without_sealed": sum(
            1 for candidate_id in resolved if candidate_id not in sealed),
        "sealed_without_label": sum(
            1 for candidate_id in sealed if candidate_id not in resolved),
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"out": str(out_dir), **manifest}


def _write_jsonl(path: Path, items: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for item in items:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")
