"""Coverage-extension templates for negatives shorter than the 168 h horizon.

A negative label is valid for a horizon only when ``observed_until`` reaches
``first_seen + h``. Labels produced right after the 72 h maturity gate cover the
main horizon but not the 7-day one, so they need a second look once the week is
up. This module builds the superseding rows; the annotator fills them after
re-checking the episode.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .calibration import MATERIAL_CASES, _iso, _parse_dt, _read_jsonl, resolve_labels
from .labels import _write_jsonl
from .util import utcnow

DEFAULT_HORIZON_HOURS = 168
SCHEMA_VERSION = 2


def _template_row(label: dict[str, Any], target: datetime) -> dict[str, Any]:
    cluster_id = label.get("cluster_id")
    first_seen = str(label.get("candidate_id") or "").split("@")[-1]
    month = first_seen[:7] if len(first_seen) >= 7 else "0000-00"
    return {
        "label_id": f"C{month}-{cluster_id}",
        "candidate_id": label["candidate_id"],
        "cluster_id": cluster_id,
        "theater": label.get("theater"),
        "subject": label.get("subject"),
        "in_scope": label.get("in_scope", True),
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
        "supersedes": label.get("label_id"),
        "notes": "",
        "target_until": _iso(target),
    }


def build_coverage_template(
    round_dir: Path,
    out_dir: Path | None = None,
    *,
    hours: int = DEFAULT_HORIZON_HOURS,
    force: bool = False,
) -> dict[str, Any]:
    round_dir = Path(round_dir)
    out_dir = (Path(out_dir) if out_dir
               else round_dir.parent / "coverage" / round_dir.name)
    candidates_path = round_dir / "candidates.jsonl"
    labels_path = round_dir / "labels.jsonl"
    for path in (candidates_path, labels_path):
        if not path.exists():
            raise FileNotFoundError(f"round file not found: {path}")
    template_path = out_dir / "labels.template.jsonl"
    manifest_path = out_dir / "manifest.json"
    if not force:
        for path in (template_path, manifest_path):
            if path.exists():
                raise FileExistsError(f"{path} already exists; pass force=True to overwrite")

    first_seen = {
        row["candidate_id"]: _parse_dt(row.get("first_seen"))
        for row in _read_jsonl(candidates_path) if row.get("candidate_id")
    }
    now = utcnow()
    rows: list[dict[str, Any]] = []
    pending = 0
    for candidate_id, label in sorted(resolve_labels(_read_jsonl(labels_path)).items()):
        if label.get("case_type") not in MATERIAL_CASES:
            continue
        if label.get("in_scope") is False:
            continue
        if str(label.get("confidence") or "").lower() == "low":
            continue
        anchor = first_seen.get(candidate_id)
        if anchor is None:
            continue
        target = anchor + timedelta(hours=hours)
        observed = _parse_dt(label.get("observed_until"))
        if observed is not None and observed >= target:
            continue
        if target > now:
            pending += 1
            continue
        rows.append(_template_row(label, target))

    out_dir.mkdir(parents=True, exist_ok=True)
    _write_jsonl(template_path, rows)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _iso(now),
        "source_round": round_dir.name,
        "horizon_hours": hours,
        "candidates": len(rows),
        "pending": pending,
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"out": str(out_dir), "template": str(template_path), **manifest}
