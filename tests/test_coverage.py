"""Coverage templates: which negatives need a second look for the 168 h horizon."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tripwire.__main__ import parse_args
from tripwire.coverage import build_coverage_template
from tripwire.util import utcnow


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _candidate(cluster_id: int, first_seen: datetime) -> dict:
    return {
        "candidate_id": f"cluster:{cluster_id}@{_iso(first_seen)}",
        "cluster_id": cluster_id,
        "theater": "EU_RU",
        "first_seen": _iso(first_seen),
    }


def _label(cluster_id: int, first_seen: datetime, case_type: str | None, *,
           observed_until: datetime | None = None, label_id: str | None = None,
           supersedes: str | None = None, confidence: str = "high",
           in_scope: bool = True,
           labeled_at: datetime | None = None) -> dict:
    return {
        "label_id": label_id or f"L-{cluster_id}",
        "candidate_id": f"cluster:{cluster_id}@{_iso(first_seen)}",
        "cluster_id": cluster_id,
        "theater": "EU_RU",
        "subject": {"country": "UA", "actor": ""},
        "in_scope": in_scope,
        "case_type": case_type,
        "outcome": None,
        "outcome_type": "none",
        "outcome_severity": 0,
        "outcome_at": None,
        "observed_until": _iso(observed_until) if observed_until else None,
        "evidence": [],
        "confidence": confidence,
        "annotator": "user",
        "labeled_at": _iso(labeled_at or utcnow()),
        "supersedes": supersedes,
        "notes": "",
    }


class CoverageTemplateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.round = self.root / "2026-11-04"
        self.round.mkdir()
        self.now = utcnow()
        self.old = self.now - timedelta(hours=200)
        self.fresh = self.now - timedelta(hours=100)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write(self, candidates: list[dict], labels: list[dict]) -> None:
        for name, rows in (("candidates.jsonl", candidates), ("labels.jsonl", labels)):
            (self.round / name).write_text(
                "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    def test_selects_only_uncovered_negatives(self) -> None:
        self._write(
            [_candidate(i, self.old) for i in range(101, 108)]
            + [_candidate(108, self.fresh)],
            [
                _label(101, self.old, "detection",
                       observed_until=self.old + timedelta(hours=80)),
                _label(102, self.old, "non_material",
                       observed_until=self.old + timedelta(hours=170)),
                _label(103, self.old, "anticipatory",
                       observed_until=self.old + timedelta(hours=80)),
                _label(104, self.old, "out_of_scope", in_scope=False,
                       observed_until=self.old + timedelta(hours=80)),
                _label(105, self.old, "detection", confidence="low",
                       observed_until=self.old + timedelta(hours=80)),
                _label(106, self.old, "detection", label_id="L1",
                       observed_until=self.old + timedelta(hours=80)),
                _label(106, self.old, "non_material", label_id="L2", supersedes="L1",
                       observed_until=self.old + timedelta(hours=150),
                       labeled_at=self.now),
                _label(107, self.old, "detection", observed_until=None),
                _label(108, self.fresh, "detection",
                       observed_until=self.fresh + timedelta(hours=80)),
            ])
        result = build_coverage_template(self.round, self.root / "coverage")
        rows = [json.loads(line) for line in
                (self.root / "coverage" / "labels.template.jsonl")
                .read_text(encoding="utf-8").splitlines()]
        self.assertEqual(result["candidates"], 3)
        self.assertEqual(result["pending"], 1)
        self.assertEqual({row["cluster_id"] for row in rows}, {101, 106, 107})
        superseded = {row["cluster_id"]: row["supersedes"] for row in rows}
        self.assertEqual(superseded[106], "L2")
        for row in rows:
            self.assertIsNone(row["case_type"])
            self.assertIsNone(row["observed_until"])
            self.assertIsNone(row["labeled_at"])
            self.assertEqual(row["annotator"], "")
            self.assertTrue(row["target_until"].endswith("Z"))
        self.assertEqual(rows[0]["target_until"],
                         _iso(self.old + timedelta(hours=168)))

    def test_existing_template_is_not_overwritten(self) -> None:
        self._write([_candidate(101, self.old)],
                    [_label(101, self.old, "detection")])
        build_coverage_template(self.round, self.root / "coverage")
        with self.assertRaises(FileExistsError):
            build_coverage_template(self.round, self.root / "coverage")
        result = build_coverage_template(self.round, self.root / "coverage", force=True)
        self.assertEqual(result["candidates"], 1)

    def test_missing_round_files_raise(self) -> None:
        with self.assertRaises(FileNotFoundError):
            build_coverage_template(self.round, self.root / "coverage")


class CoverageCliTests(unittest.TestCase):
    def test_defaults(self) -> None:
        args = parse_args([])
        self.assertFalse(args.coverage_labels)
        self.assertEqual(args.coverage_hours, 168)
        self.assertEqual(args.coverage_round, "")
        self.assertFalse(args.coverage_force)

    def test_flags(self) -> None:
        args = parse_args(["--coverage-labels", "--coverage-round", "r",
                           "--coverage-out", "o", "--coverage-hours", "72",
                           "--coverage-force"])
        self.assertTrue(args.coverage_labels)
        self.assertEqual(args.coverage_round, "r")
        self.assertEqual(args.coverage_out, "o")
        self.assertEqual(args.coverage_hours, 72)
        self.assertTrue(args.coverage_force)


if __name__ == "__main__":
    unittest.main()
