"""Brier calibration for the geopolitical label set.

Predictions are anchored at ``first_seen``: the theater score recorded after the
cluster's first scored event (``clusters.score_initial``). The final cluster
score is never used, because it keeps growing with later events and would leak
the future into the prediction. Outcomes are read from labels and count only
when they happen strictly after the anchor.
"""

from __future__ import annotations

import json
import math
import random
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .util import utcnow

SCHEMA_VERSION = 1
DEFAULT_HORIZONS = (24, 72, 168)
PRIMARY_HORIZON = 72
DEFAULT_SPLIT = 0.6
DEFAULT_MIN_RECORDS = 100
DEFAULT_MIN_POSITIVES = 20
DEFAULT_BOOTSTRAP = 1000
MATERIAL_CASES = ("detection", "non_material", "out_of_scope")


@dataclass(slots=True)
class Record:
    candidate_id: str
    cluster_id: int
    theater: str
    stratum: str
    score: float
    prediction_at: datetime
    case_type: str
    ys: dict[int, int | None]


def _parse_dt(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        moment = value
    else:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ") if value else None


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            text = line.strip()
            if not text:
                continue
            try:
                rows.append(json.loads(text))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON ({exc.msg})") from exc
    return rows


def resolve_labels(rows: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Apply ``supersedes`` chains: one current label per candidate."""
    rows = list(rows)
    superseded = {row.get("supersedes") for row in rows if row.get("supersedes")}
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row.get("label_id") in superseded:
            continue
        candidate_id = row.get("candidate_id")
        if not candidate_id:
            continue
        current = latest.get(candidate_id)
        if current is None or _label_time(row) >= _label_time(current):
            latest[candidate_id] = row
    return latest


def _label_time(row: dict[str, Any]) -> datetime:
    return _parse_dt(row.get("labeled_at")) or datetime.min.replace(tzinfo=timezone.utc)


def load_sealed(path: Path) -> dict[str, dict[str, Any]]:
    return {row["candidate_id"]: row for row in _read_jsonl(path)
            if row.get("candidate_id")}


def build_records(
    labels: Iterable[dict[str, Any]],
    sealed: dict[str, dict[str, Any]],
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
) -> tuple[list[Record], Counter]:
    """Join labels with sealed scores and resolve outcomes per horizon."""
    excluded: Counter = Counter()
    records: list[Record] = []
    for label in labels:
        candidate_id = label.get("candidate_id")
        hidden = sealed.get(candidate_id)
        if hidden is None:
            excluded["no_sealed_score"] += 1
            continue
        if str(label.get("confidence") or "").lower() == "low":
            excluded["low_confidence"] += 1
            continue
        if label.get("in_scope") is False:
            excluded["out_of_scope_subject"] += 1
            continue
        case_type = str(label.get("case_type") or "")
        if case_type == "unresolved":
            excluded["unresolved"] += 1
            continue
        anchor = _parse_dt(hidden.get("first_seen"))
        if anchor is None:
            excluded["no_anchor_time"] += 1
            continue
        raw_score = hidden.get("score_initial")
        if raw_score is None:
            excluded["no_anchor_score"] += 1
            continue
        outcome_at = _parse_dt(label.get("outcome_at"))
        observed_until = _parse_dt(label.get("observed_until"))
        ys: dict[int, int | None] = {}
        if case_type == "anticipatory":
            if outcome_at is None:
                excluded["anticipatory_without_outcome"] += 1
                continue
            if outcome_at <= anchor:
                excluded["outcome_before_anchor"] += 1
                continue
            for horizon in horizons:
                ys[horizon] = 1 if outcome_at <= anchor + timedelta(hours=horizon) else 0
        elif case_type in MATERIAL_CASES:
            for horizon in horizons:
                covered = (observed_until is not None
                           and observed_until >= anchor + timedelta(hours=horizon))
                ys[horizon] = 0 if covered else None
            if all(value is None for value in ys.values()):
                excluded["no_observation_coverage"] += 1
                continue
        else:
            excluded["unknown_case_type"] += 1
            continue
        records.append(Record(
            candidate_id=str(candidate_id),
            cluster_id=int(label.get("cluster_id") or hidden.get("cluster_id") or 0),
            theater=str(label.get("theater") or ""),
            stratum=str(hidden.get("stratum") or ""),
            score=float(raw_score),
            prediction_at=anchor,
            case_type=case_type,
            ys=ys,
        ))
    return records, excluded


class _Constant:
    def __init__(self, probability: float) -> None:
        self.probability = min(max(probability, 0.0), 1.0)
        self.blocks: list[tuple[float, float, int]] = [(math.inf, self.probability, 0)]

    def predict(self, _x: float) -> float:
        return self.probability

    @property
    def fallback(self) -> bool:
        return True


class _Isotonic:
    def __init__(self, blocks: list[tuple[float, float, int]]) -> None:
        self.blocks = blocks

    def predict(self, x: float) -> float:
        for upper, probability, _count in self.blocks:
            if x <= upper:
                return probability
        return self.blocks[-1][1]

    @property
    def fallback(self) -> bool:
        return False


class _Platt:
    def __init__(self, a: float, b: float, mean: float, std: float,
                 constant: float | None = None) -> None:
        self.a = a
        self.b = b
        self.mean = mean
        self.std = std
        self.constant = constant

    def predict(self, x: float) -> float:
        if self.constant is not None:
            return self.constant
        z = self.a * ((x - self.mean) / self.std) + self.b
        return _sigmoid(z)

    @property
    def fallback(self) -> bool:
        return self.constant is not None


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-min(z, 500.0)))
    exp = math.exp(max(z, -500.0))
    return exp / (1.0 + exp)


def fit_isotonic(points: list[tuple[float, int]]) -> _Isotonic | _Constant:
    """Pool adjacent violators over score groups."""
    if not points:
        return _Constant(0.0)
    positives = sum(y for _, y in points)
    if positives == 0 or positives == len(points):
        return _Constant(positives / len(points))
    counts: dict[float, list[int]] = defaultdict(lambda: [0, 0])
    for x, y in points:
        counts[x][0] += 1
        counts[x][1] += int(y)
    blocks: list[list[float]] = []
    for x in sorted(counts):
        n, positives = counts[x]
        blocks.append([x, float(n), float(positives)])
        while len(blocks) > 1 and (blocks[-2][2] / blocks[-2][1]
                                   >= blocks[-1][2] / blocks[-1][1]):
            last = blocks.pop()
            blocks[-1][1] += last[1]
            blocks[-1][2] += last[2]
            blocks[-1][0] = last[0]
    out = [(upper, total / weight, int(weight)) for upper, weight, total in blocks]
    return _Isotonic(out)


def fit_platt(points: list[tuple[float, int]]) -> _Platt | _Constant:
    """Two-parameter logistic fit on the standardized score."""
    if not points:
        return _Constant(0.0)
    positives = sum(y for _, y in points)
    negatives = len(points) - positives
    base = positives / len(points)
    if positives == 0 or negatives == 0:
        return _Constant(base)
    scores = [x for x, _ in points]
    mean = sum(scores) / len(scores)
    variance = sum((x - mean) ** 2 for x in scores) / len(scores)
    std = math.sqrt(variance) or 1.0
    xs = [(x - mean) / std for x in scores]
    target_pos = (positives + 1.0) / (positives + 2.0)
    target_neg = 1.0 / (negatives + 2.0)
    targets = [target_pos if y else target_neg for _, y in points]
    a, b = 0.0, math.log((positives + 1.0) / (negatives + 1.0))
    for _ in range(100):
        g_a = g_b = h_aa = h_ab = h_bb = 0.0
        for x, target in zip(xs, targets):
            p = _sigmoid(a * x + b)
            diff = p - target
            weight = max(p * (1.0 - p), 1e-12)
            g_a += diff * x
            g_b += diff
            h_aa += weight * x * x
            h_ab += weight * x
            h_bb += weight
        h_aa += 1e-6
        h_bb += 1e-6
        determinant = h_aa * h_bb - h_ab * h_ab
        if abs(determinant) < 1e-12:
            break
        step_a = (h_bb * g_a - h_ab * g_b) / determinant
        step_b = (h_aa * g_b - h_ab * g_a) / determinant
        step_a = max(-1.0, min(1.0, step_a))
        step_b = max(-1.0, min(1.0, step_b))
        a -= step_a
        b -= step_b
        if abs(step_a) + abs(step_b) < 1e-9:
            break
    a = max(-50.0, min(50.0, a))
    return _Platt(a, b, mean, std)


def brier(probabilities: Iterable[float], outcomes: Iterable[int]) -> float | None:
    pairs = list(zip(probabilities, outcomes))
    if not pairs:
        return None
    return sum((p - y) ** 2 for p, y in pairs) / len(pairs)


def _split(records: list[Record], ratio: float) -> tuple[list[Record], list[Record]]:
    if not records:
        return [], []
    if len(records) == 1:
        return records, []
    cut = int(len(records) * ratio)
    cut = min(max(cut, 1), len(records) - 1)
    return records[:cut], records[cut:]


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * quantile / 100.0
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _bootstrap(groups: dict[Any, list[Record]], horizon: int, model,
               base: float, iterations: int, seed: str) -> dict[str, list[float]]:
    if not groups or iterations <= 0:
        return {}
    rng = random.Random(seed)
    keys = sorted(groups)
    samples = {name: [] for name in
               ("brier_isotonic", "skill_isotonic", "brier_platt", "skill_platt")}
    for _ in range(iterations):
        picked: list[Record] = []
        for _ in range(len(keys)):
            picked.extend(groups[rng.choice(keys)])
        ys = [record.ys[horizon] for record in picked]
        iso_brier = brier([model["isotonic"].predict(r.score) for r in picked], ys)
        platt_brier = brier([model["platt"].predict(r.score) for r in picked], ys)
        base_brier = brier([base] * len(picked), ys)
        samples["brier_isotonic"].append(iso_brier)
        samples["brier_platt"].append(platt_brier)
        samples["skill_isotonic"].append(
            1.0 - iso_brier / base_brier if base_brier else 0.0)
        samples["skill_platt"].append(
            1.0 - platt_brier / base_brier if base_brier else 0.0)
    return {name: [round(_percentile(values, 2.5), 4),
                   round(_percentile(values, 97.5), 4)]
            for name, values in samples.items()}


def _horizon_report(
    records: list[Record], horizon: int, *, split: float,
    min_records: int, min_positives: int, bootstrap: int, seed: str,
) -> dict[str, Any]:
    usable = [record for record in records if record.ys.get(horizon) is not None]
    usable.sort(key=lambda record: (record.prediction_at, record.candidate_id))
    if not usable:
        return {
            "records": 0, "positives": 0, "publishable": False,
            "model": None, "train": {"n": 0, "positives": 0},
            "test": {"n": 0, "positives": 0},
            "brier": None, "brier_base": None, "skill": None,
            "ci95": {}, "breaks": [],
        }
    train, test = _split(usable, split)
    train_positives = sum(record.ys[horizon] for record in train)
    test_positives = sum(record.ys[horizon] for record in test)
    positives = train_positives + test_positives
    base = (train_positives / len(train)) if train else (positives / len(usable))
    points = [(record.score, int(record.ys[horizon])) for record in train]
    isotonic = fit_isotonic(points)
    platt = fit_platt(points)
    model = {"isotonic": isotonic, "platt": platt}

    iso_brier = platt_brier = base_brier = None
    skill_iso = skill_platt = None
    if test:
        test_iso = brier([isotonic.predict(r.score) for r in test],
                         [r.ys[horizon] for r in test])
        test_platt = brier([platt.predict(r.score) for r in test],
                           [r.ys[horizon] for r in test])
        test_base = brier([base] * len(test), [r.ys[horizon] for r in test])
        iso_brier = round(test_iso, 4)
        platt_brier = round(test_platt, 4)
        base_brier = round(test_base, 4)
        if test_base:
            skill_iso = round(1.0 - test_iso / test_base, 4)
            skill_platt = round(1.0 - test_platt / test_base, 4)

    by_cluster: dict[Any, list[Record]] = defaultdict(list)
    by_day: dict[Any, list[Record]] = defaultdict(list)
    for record in test:
        by_cluster[record.cluster_id or record.candidate_id].append(record)
        by_day[record.prediction_at.date()].append(record)
    ci = {
        "cluster": _bootstrap(by_cluster, horizon, model, base,
                              bootstrap, f"{seed}:cluster"),
        "day": _bootstrap(by_day, horizon, model, base,
                          bootstrap, f"{seed}:day"),
    }

    breaks = [{"score": round(upper, 3) if math.isfinite(upper) else None,
               "p": round(probability, 4), "n": count}
              for upper, probability, count in isotonic.blocks]

    return {
        "records": len(usable),
        "positives": positives,
        "negatives": len(usable) - positives,
        "publishable": len(usable) >= min_records and positives >= min_positives,
        "model": "isotonic" if not isotonic.fallback else "base_rate",
        "fallback_platt": platt.fallback,
        "base_rate": round(base, 4),
        "train": {"n": len(train), "positives": train_positives,
                  "from": _iso(train[0].prediction_at),
                  "to": _iso(train[-1].prediction_at)},
        "test": {"n": len(test), "positives": test_positives,
                 "from": _iso(test[0].prediction_at) if test else None,
                 "to": _iso(test[-1].prediction_at) if test else None},
        "brier": {"isotonic": iso_brier, "platt": platt_brier},
        "brier_base": base_brier,
        "skill": {"isotonic": skill_iso, "platt": skill_platt},
        "ci95": ci,
        "breaks": breaks,
    }


def calibrate(
    records: list[Record],
    *,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
    split: float = DEFAULT_SPLIT,
    min_records: int = DEFAULT_MIN_RECORDS,
    min_positives: int = DEFAULT_MIN_POSITIVES,
    bootstrap: int = DEFAULT_BOOTSTRAP,
    seed: str = "tripwire",
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _iso(utcnow()),
        "score_field": "score_initial",
        "anchor": "first_seen",
        "split": split,
        "horizons": {},
    }
    for horizon in horizons:
        report["horizons"][str(horizon)] = _horizon_report(
            records, horizon, split=split, min_records=min_records,
            min_positives=min_positives, bootstrap=bootstrap, seed=seed,
        )
    return report


def calibrate_files(
    labels_path: Path,
    sealed_path: Path,
    **kwargs: Any,
) -> dict[str, Any]:
    labels = resolve_labels(_read_jsonl(labels_path))
    sealed = load_sealed(sealed_path)
    records, excluded = build_records(labels.values(), sealed)
    report = calibrate(records, **kwargs)
    report["dataset"] = {
        "labels_loaded": len(labels),
        "records": len(records),
        "excluded": dict(excluded),
        "strata": dict(Counter(record.stratum for record in records)),
        "theaters": dict(Counter(record.theater for record in records)),
    }
    return report


def format_report(report: dict[str, Any]) -> str:
    dataset = report.get("dataset") or {}
    lines = [
        f"Калибровка Brier: {dataset.get('records', 0)} эпизодов из "
        f"{dataset.get('labels_loaded', 0)} меток, балл на якоре "
        f"({report.get('score_field')} @ {report.get('anchor')})",
    ]
    excluded = dataset.get("excluded") or {}
    if excluded:
        summary = ", ".join(f"{name}={count}" for name, count in sorted(excluded.items()))
        lines.append(f"Исключено: {summary}")
    for horizon in ("24", "72", "168"):
        entry = report["horizons"].get(horizon)
        if not entry:
            continue
        if not entry["records"]:
            lines.append(f"{horizon} ч: нет пригодных эпизодов")
            continue
        brier_iso = entry["brier"]["isotonic"]
        skill = entry["skill"]["isotonic"]
        base = entry["brier_base"]
        text = (f"{horizon} ч: N={entry['records']} (+{entry['positives']}), "
                f"train {entry['train']['n']}/test {entry['test']['n']}")
        if brier_iso is None:
            text += ", тест пуст"
        else:
            text += (f", Brier={brier_iso:.3f} против базы {base:.3f}"
                     f", skill={skill:+.3f}" if skill is not None else
                     f", Brier={brier_iso:.3f} против базы {base:.3f}")
        lines.append(text)
        cluster_ci = (entry.get("ci95") or {}).get("cluster") or {}
        if cluster_ci.get("brier_isotonic"):
            low, high = cluster_ci["brier_isotonic"]
            lines.append(f"    95% CI (кластеры): Brier {low:.3f}–{high:.3f}")
        if not entry["publishable"]:
            lines.append("    недостаточно данных: нужно ≥100 эпизодов и ≥20 позитивов "
                         "на горизонт")
    return "\n".join(lines)
