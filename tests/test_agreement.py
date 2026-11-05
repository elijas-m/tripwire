"""Inter-annotator kappa: hand-computed values, drafts and supersedes chains."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tripwire.agreement import (
    agreement_files,
    bootstrap_ci,
    cohen_kappa,
    compute_agreement,
    format_agreement,
)
from tripwire.__main__ import parse_args


def _row(candidate_id: str, case_type: str | None, *, label_id: str | None = None,
         supersedes: str | None = None, labeled_at: str = "2026-11-04T12:00:00Z",
         theater: str = "EU_RU", notes: str = "") -> dict:
    return {
        "label_id": label_id or f"L-{candidate_id}",
        "candidate_id": candidate_id,
        "cluster_id": int(candidate_id.split(":")[1].split("@")[0]),
        "theater": theater,
        "case_type": case_type,
        "labeled_at": labeled_at,
        "supersedes": supersedes,
        "notes": notes,
    }


class KappaTests(unittest.TestCase):
    def test_hand_computed_kappa(self) -> None:
        pairs = (["anticipatory"] * 4 + ["anticipatory"] * 2
                 + ["detection"] + ["detection"] * 3)
        right = (["anticipatory"] * 4 + ["detection"] * 2
                 + ["anticipatory"] + ["detection"] * 3)
        stats = cohen_kappa(list(zip(pairs, right)))
        self.assertEqual(stats["n"], 10)
        self.assertAlmostEqual(stats["po"], 0.7)
        self.assertAlmostEqual(stats["pe"], 0.5)
        self.assertAlmostEqual(stats["kappa"], 0.4)

    def test_perfect_agreement(self) -> None:
        stats = cohen_kappa([("detection", "detection"), ("non_material", "non_material")])
        self.assertAlmostEqual(stats["po"], 1.0)
        self.assertAlmostEqual(stats["kappa"], 1.0)

    def test_single_category_is_undefined(self) -> None:
        stats = cohen_kappa([("detection", "detection")] * 5)
        self.assertAlmostEqual(stats["pe"], 1.0)
        self.assertIsNone(stats["kappa"])

    def test_empty_pairs(self) -> None:
        self.assertIsNone(cohen_kappa([])["kappa"])
        self.assertIsNone(bootstrap_ci([]))

    def test_bootstrap_is_deterministic(self) -> None:
        pairs = [("anticipatory", "detection"), ("detection", "detection"),
                 ("non_material", "non_material"), ("out_of_scope", "non_material"),
                 ("detection", "anticipatory")] * 4
        first = bootstrap_ci(pairs, iterations=200, seed=42)
        second = bootstrap_ci(pairs, iterations=200, seed=42)
        self.assertEqual(first, second)
        self.assertLessEqual(first[0], first[1])


class AgreementReportTests(unittest.TestCase):
    def _pairs(self, n_perfect: int = 3, mismatches: int = 0) -> tuple[list, list]:
        user = [_row(f"cluster:{100 + i}@2026-11-01T00:00:00Z", "detection")
                for i in range(n_perfect)]
        agent = [_row(f"cluster:{100 + i}@2026-11-01T00:00:00Z", "detection")
                 for i in range(n_perfect)]
        for i in range(mismatches):
            cid = f"cluster:{200 + i}@2026-11-01T00:00:00Z"
            user.append(_row(cid, "anticipatory"))
            agent.append(_row(cid, "detection"))
        return user, agent

    def test_supersedes_win_and_drafts_are_skipped(self) -> None:
        cid = "cluster:101@2026-11-01T00:00:00Z"
        second = "cluster:103@2026-11-01T00:00:00Z"
        draft = "cluster:102@2026-11-01T00:00:00Z"
        user = [
            _row(cid, "anticipatory", label_id="L1"),
            _row(cid, "detection", label_id="L2", supersedes="L1",
                 labeled_at="2026-11-04T13:00:00Z"),
            _row(draft, None, label_id="L3"),
            _row(second, "non_material"),
        ]
        agent = [_row(cid, "detection"), _row(draft, "non_material"),
                 _row(second, "non_material")]
        report = compute_agreement(user, agent, min_pairs=1)
        self.assertEqual(report["n"], 2)
        self.assertEqual(report["kappa"], 1.0)
        self.assertEqual(report["only_user"], 0)
        self.assertEqual(report["only_agent"], 1)
        self.assertEqual(report["disagreements"], [])

    def test_insufficient_pairs(self) -> None:
        user, agent = self._pairs(n_perfect=3)
        report = compute_agreement(user, agent, min_pairs=5)
        self.assertEqual(report["n"], 3)
        self.assertIsNone(report["kappa"])
        self.assertIsNone(report["ci_low"])
        self.assertIn("недостаточно меток", report["verdict"])
        self.assertEqual(report["distributions"]["user"], {"detection": 3})

    def test_disagreements_keep_short_notes(self) -> None:
        cid = "cluster:301@2026-11-01T00:00:00Z"
        user = [_row(cid, "anticipatory", notes="  фоновые\n серии  " + "x" * 300)]
        agent = [_row(cid, "detection", notes="рутинная атака")]
        report = compute_agreement(user, agent, min_pairs=1)
        item = report["disagreements"][0]
        self.assertEqual(item["user"], "anticipatory")
        self.assertEqual(item["agent"], "detection")
        self.assertEqual(item["user_notes"][:12], "фоновые сери")
        self.assertEqual(len(item["user_notes"]), 160)
        self.assertNotIn("\n", item["user_notes"])
        self.assertIn("cluster:301", format_agreement(report))

    def test_verdict_uses_threshold(self) -> None:
        user, agent = self._pairs(n_perfect=5, mismatches=5)
        report = compute_agreement(user, agent, min_pairs=5, bootstrap=50)
        self.assertLess(report["kappa"], 0.7)
        self.assertIn("правится словарь", report["verdict"])

    def test_missing_files_raise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                agreement_files(Path(tmp) / "labels.jsonl",
                                Path(tmp) / "labels.agent.jsonl")

    def test_files_roundtrip(self) -> None:
        user, agent = self._pairs(n_perfect=4, mismatches=1)
        with tempfile.TemporaryDirectory() as tmp:
            user_path = Path(tmp) / "labels.jsonl"
            agent_path = Path(tmp) / "labels.agent.jsonl"
            for path, rows in ((user_path, user), (agent_path, agent)):
                path.write_text(
                    "".join(json.dumps(row) + "\n" for row in rows),
                    encoding="utf-8")
            report = agreement_files(user_path, agent_path, bootstrap=100)
        self.assertEqual(report["n"], 5)
        self.assertEqual(report["only_user"], 0)
        self.assertEqual(report["only_agent"], 0)


class KappaCliTests(unittest.TestCase):
    def test_defaults(self) -> None:
        args = parse_args([])
        self.assertFalse(args.kappa_labels)
        self.assertEqual(args.kappa_dir, "backtest/labels/m2")
        self.assertEqual(args.kappa_user_file, "labels.jsonl")
        self.assertEqual(args.kappa_agent_file, "labels.agent.jsonl")
        self.assertEqual(args.kappa_bootstrap, 1000)
        self.assertEqual(args.kappa_seed, 42)
        self.assertEqual(args.kappa_min_pairs, 5)
        self.assertAlmostEqual(args.kappa_threshold, 0.7)

    def test_flags(self) -> None:
        args = parse_args(["--kappa-labels", "--kappa-dir", "r",
                           "--kappa-json", "r/agreement.json",
                           "--kappa-bootstrap", "10", "--kappa-seed", "7"])
        self.assertTrue(args.kappa_labels)
        self.assertEqual(args.kappa_dir, "r")
        self.assertEqual(args.kappa_json, "r/agreement.json")
        self.assertEqual(args.kappa_bootstrap, 10)
        self.assertEqual(args.kappa_seed, 7)


if __name__ == "__main__":
    unittest.main()
