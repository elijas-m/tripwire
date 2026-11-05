"""Inter-annotator agreement on ``case_type`` (Cohen's kappa).

The two annotators label independently and never see each other's files or the
sealed scores, so agreement measures the dictionary, not the data. A kappa below
the threshold means the taxonomy needs tightening, not that labels are wrong.
"""

from __future__ import annotations

import random
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from .calibration import _percentile, _read_jsonl, resolve_labels
from .util import utcnow

DEFAULT_MIN_PAIRS = 5
DEFAULT_BOOTSTRAP = 1000
DEFAULT_SEED = 42
DEFAULT_THRESHOLD = 0.7
NOTE_CHARS = 160


def cohen_kappa(pairs: Iterable[tuple[str, str]]) -> dict[str, Any]:
    """Return ``n``, ``po``, ``pe`` and ``kappa`` for labelled pairs.

    ``kappa`` is ``None`` when ``pe`` is 1: both sides used a single category, so
    chance agreement cannot be separated from real agreement.
    """
    pairs = list(pairs)
    n = len(pairs)
    if not n:
        return {"n": 0, "po": None, "pe": None, "kappa": None}
    po = sum(1 for left, right in pairs if left == right) / n
    left_counts = Counter(left for left, _ in pairs)
    right_counts = Counter(right for _, right in pairs)
    pe = sum(left_counts[key] * right_counts[key]
             for key in set(left_counts) | set(right_counts)) / (n * n)
    kappa = None if pe == 1 else (po - pe) / (1 - pe)
    return {"n": n, "po": po, "pe": pe, "kappa": kappa}


def bootstrap_ci(
    pairs: list[tuple[str, str]],
    iterations: int = DEFAULT_BOOTSTRAP,
    seed: int = DEFAULT_SEED,
) -> list[float] | None:
    """95% percentile interval over candidate_ids resampled with replacement."""
    if not pairs or iterations <= 0:
        return None
    rng = random.Random(seed)
    values: list[float] = []
    for _ in range(iterations):
        sample = [pairs[rng.randrange(len(pairs))] for _ in range(len(pairs))]
        kappa = cohen_kappa(sample)["kappa"]
        if kappa is not None:
            values.append(kappa)
    if not values:
        return None
    return [round(_percentile(values, 2.5), 4), round(_percentile(values, 97.5), 4)]


def _current(rows: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Latest label per candidate, unresolved drafts dropped."""
    resolved = resolve_labels(rows)
    return {candidate_id: row for candidate_id, row in resolved.items()
            if str(row.get("case_type") or "").strip()}


def _short_note(row: dict[str, Any]) -> str:
    text = " ".join(str(row.get("notes") or "").split())
    return text[:NOTE_CHARS]


def _verdict(n: int, kappa: float | None, min_pairs: int,
             threshold: float) -> str:
    if n < min_pairs:
        return f"недостаточно меток: нужно минимум {min_pairs} пар"
    if kappa is None:
        return "kappa не определена: обе стороны использовали одну категорию"
    if kappa >= threshold:
        return f"kappa {threshold} или выше: словарь согласован"
    return f"kappa ниже {threshold}: правится словарь, а не метки"


def compute_agreement(
    user_rows: Iterable[dict[str, Any]],
    agent_rows: Iterable[dict[str, Any]],
    *,
    min_pairs: int = DEFAULT_MIN_PAIRS,
    bootstrap: int = DEFAULT_BOOTSTRAP,
    seed: int = DEFAULT_SEED,
    threshold: float = DEFAULT_THRESHOLD,
) -> dict[str, Any]:
    user = _current(user_rows)
    agent = _current(agent_rows)
    shared = sorted(set(user) & set(agent))
    pairs = [(user[cid]["case_type"], agent[cid]["case_type"]) for cid in shared]
    stats = cohen_kappa(pairs)
    ci = bootstrap_ci(pairs, iterations=bootstrap, seed=seed) if stats["n"] >= min_pairs else None
    disagreements = [
        {
            "candidate_id": cid,
            "theater": user[cid].get("theater") or agent[cid].get("theater"),
            "user": user[cid]["case_type"],
            "agent": agent[cid]["case_type"],
            "user_notes": _short_note(user[cid]),
            "agent_notes": _short_note(agent[cid]),
        }
        for cid in shared
        if user[cid]["case_type"] != agent[cid]["case_type"]
    ]
    return {
        "generated_at": utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "n": stats["n"],
        "po": stats["po"],
        "pe": stats["pe"],
        "kappa": stats["kappa"],
        "ci_low": ci[0] if ci else None,
        "ci_high": ci[1] if ci else None,
        "raw_agreement": stats["po"],
        "distributions": {
            "user": dict(Counter(row["case_type"] for row in user.values())),
            "agent": dict(Counter(row["case_type"] for row in agent.values())),
        },
        "only_user": len(set(user) - set(agent)),
        "only_agent": len(set(agent) - set(user)),
        "bootstrap": {"resamples": bootstrap, "seed": seed},
        "min_pairs": min_pairs,
        "threshold": threshold,
        "verdict": _verdict(stats["n"], stats["kappa"], min_pairs, threshold),
        "supersedes_applied": True,
        "disagreements": disagreements,
    }


def agreement_files(
    user_path: Path,
    agent_path: Path,
    **kwargs: Any,
) -> dict[str, Any]:
    for path in (user_path, agent_path):
        if not path.exists():
            raise FileNotFoundError(f"label file not found: {path}")
    return compute_agreement(_read_jsonl(user_path), _read_jsonl(agent_path), **kwargs)


def _distribution_line(name: str, counts: dict[str, int]) -> str:
    if not counts:
        return f"{name}: нет меток"
    body = ", ".join(f"{case}={count}" for case, count in sorted(counts.items()))
    return f"{name}: {body}"


def format_agreement(report: dict[str, Any]) -> str:
    lines = [f"Согласие по case_type: {report['verdict']}"]
    if report["po"] is None:
        lines.append(f"пар {report['n']}")
    elif report["kappa"] is None:
        lines.append(f"пар {report['n']}, po {report['po']:.3f}")
    else:
        lines.append(f"пар {report['n']}, po {report['po']:.3f}, "
                     f"pe {report['pe']:.3f}, kappa {report['kappa']:.3f}")
        if report["ci_low"] is not None:
            bootstrap = report["bootstrap"]
            lines.append(f"95% CI (bootstrap {bootstrap['resamples']}, "
                         f"seed {bootstrap['seed']}): "
                         f"{report['ci_low']:.3f}–{report['ci_high']:.3f}")
    for side in ("user", "agent"):
        lines.append("  " + _distribution_line(side, report["distributions"][side]))
    if report["only_user"] or report["only_agent"]:
        lines.append(f"Без пары: user {report['only_user']}, "
                     f"agent {report['only_agent']}")
    lines.append(f"Расхождения ({len(report['disagreements'])}):")
    for item in report["disagreements"]:
        lines.append(f"  {item['candidate_id']} [{item['theater']}]: "
                     f"user={item['user']}, agent={item['agent']}")
    return "\n".join(lines)
