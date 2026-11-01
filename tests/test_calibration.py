"""Brier calibration: horizon labels, leakage guards and anchor-score capture."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from tripwire.app import TripwireApp
from tripwire.calibration import (
    build_records,
    brier,
    calibrate,
    calibrate_files,
    fit_isotonic,
    fit_platt,
    resolve_labels,
)
from tripwire.db import DB
from tripwire.models import Event, RawItem
from tripwire.util import utcnow

ANCHOR = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def _iso(moment: datetime | None) -> str | None:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ") if moment else None


def _label(candidate_id: str, *, case_type: str, confidence: str = "high",
           in_scope: bool = True, outcome_at: datetime | None = None,
           observed_until: datetime | None = None, label_id: str | None = None,
           supersedes: str | None = None) -> dict:
    return {
        "label_id": label_id or f"L-{candidate_id}",
        "candidate_id": candidate_id,
        "cluster_id": int(candidate_id.split(":")[1].split("@")[0]),
        "theater": "EU_RU",
        "in_scope": in_scope,
        "case_type": case_type,
        "outcome": 1 if case_type == "anticipatory" else None,
        "outcome_at": _iso(outcome_at),
        "observed_until": _iso(observed_until),
        "confidence": confidence,
        "labeled_at": "2026-11-01T00:00:00Z",
        "supersedes": supersedes,
    }


def _sealed(candidate_id: str, *, score_initial: float | None = 5.0,
            first_seen: datetime = ANCHOR, stratum: str = "random") -> dict:
    return {
        "candidate_id": candidate_id,
        "cluster_id": int(candidate_id.split(":")[1].split("@")[0]),
        "stratum": stratum,
        "score_initial": score_initial,
        "score": 9.0,
        "level": 2,
        "alert_level": 0,
        "alert_count": 0,
        "decided_at": None,
        "first_seen": _iso(first_seen),
    }


class HorizonTests(unittest.TestCase):
    def _records(self, label: dict, sealed: dict):
        records, excluded = build_records([label], {sealed["candidate_id"]: sealed})
        return records, excluded

    def test_anticipatory_outcome_maps_to_each_horizon(self) -> None:
        candidate = "cluster:1@2026-10-01T12:00:00Z"
        label = _label(candidate, case_type="anticipatory",
                       outcome_at=ANCHOR + timedelta(hours=30))
        records, _ = self._records(label, _sealed(candidate))
        self.assertEqual(records[0].ys, {24: 0, 72: 1, 168: 1})

    def test_negative_needs_coverage_for_each_horizon(self) -> None:
        candidate = "cluster:2@2026-10-01T12:00:00Z"
        label = _label(candidate, case_type="detection",
                       observed_until=ANCHOR + timedelta(hours=80))
        records, _ = self._records(label, _sealed(candidate))
        self.assertEqual(records[0].ys, {24: 0, 72: 0, 168: None})

    def test_full_coverage_gives_all_negatives(self) -> None:
        candidate = "cluster:3@2026-10-01T12:00:00Z"
        label = _label(candidate, case_type="non_material",
                       observed_until=ANCHOR + timedelta(hours=200))
        records, _ = self._records(label, _sealed(candidate))
        self.assertEqual(records[0].ys, {24: 0, 72: 0, 168: 0})

    def test_unresolved_and_low_confidence_are_excluded(self) -> None:
        candidate = "cluster:4@2026-10-01T12:00:00Z"
        records, excluded = self._records(
            _label(candidate, case_type="unresolved"), _sealed(candidate))
        self.assertFalse(records)
        self.assertEqual(excluded["unresolved"], 1)
        records, excluded = self._records(
            _label(candidate, case_type="detection", confidence="low",
                   observed_until=ANCHOR + timedelta(hours=200)),
            _sealed(candidate))
        self.assertFalse(records)
        self.assertEqual(excluded["low_confidence"], 1)

    def test_out_of_scope_subject_is_excluded(self) -> None:
        candidate = "cluster:5@2026-10-01T12:00:00Z"
        records, excluded = self._records(
            _label(candidate, case_type="out_of_scope", in_scope=False,
                   observed_until=ANCHOR + timedelta(hours=200)),
            _sealed(candidate))
        self.assertFalse(records)
        self.assertEqual(excluded["out_of_scope_subject"], 1)

    def test_missing_anchor_score_is_excluded(self) -> None:
        candidate = "cluster:6@2026-10-01T12:00:00Z"
        records, excluded = self._records(
            _label(candidate, case_type="detection",
                   observed_until=ANCHOR + timedelta(hours=200)),
            _sealed(candidate, score_initial=None))
        self.assertFalse(records)
        self.assertEqual(excluded["no_anchor_score"], 1)

    def test_outcome_before_anchor_is_excluded(self) -> None:
        candidate = "cluster:7@2026-10-01T12:00:00Z"
        records, excluded = self._records(
            _label(candidate, case_type="anticipatory",
                   outcome_at=ANCHOR - timedelta(hours=1)),
            _sealed(candidate))
        self.assertFalse(records)
        self.assertEqual(excluded["outcome_before_anchor"], 1)

    def test_supersedes_chain_keeps_last_label(self) -> None:
        candidate = "cluster:8@2026-10-01T12:00:00Z"
        first = _label(candidate, case_type="detection", label_id="L-1",
                       observed_until=ANCHOR + timedelta(hours=200))
        second = _label(candidate, case_type="unresolved", label_id="L-2",
                        supersedes="L-1")
        resolved = resolve_labels([first, second])
        self.assertEqual(resolved[candidate]["label_id"], "L-2")


class ModelTests(unittest.TestCase):
    def test_isotonic_is_monotonic_and_pools_inversions(self) -> None:
        model = fit_isotonic([(0.0, 0), (1.0, 0), (2.0, 1), (3.0, 1)])
        self.assertLessEqual(model.predict(0.0), model.predict(2.0))
        self.assertEqual(model.predict(0.0), 0.0)
        self.assertEqual(model.predict(3.0), 1.0)
        pooled = fit_isotonic([(0.0, 1), (1.0, 0)])
        self.assertAlmostEqual(pooled.predict(0.0), 0.5)
        self.assertAlmostEqual(pooled.predict(1.0), 0.5)

    def test_platt_is_increasing_and_bounded(self) -> None:
        points = [(float(x), 1 if x >= 5 else 0) for x in range(10) for _ in range(5)]
        model = fit_platt(points)
        low, high = model.predict(0.0), model.predict(9.0)
        self.assertGreater(high, low)
        self.assertGreater(low, 0.0)
        self.assertLess(high, 1.0)

    def test_brier_matches_definition(self) -> None:
        self.assertAlmostEqual(brier([0.5, 0.5], [1, 0]), 0.25)
        self.assertIsNone(brier([], []))


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _synthetic_dataset(root: Path, *, count: int = 120,
                       positives: int = 30) -> tuple[Path, Path]:
    labels: list[dict] = []
    sealed: list[dict] = []
    for index in range(count):
        anchor = ANCHOR + timedelta(hours=index * 6)
        candidate = f"cluster:{1000 + index}@{_iso(anchor)}"
        if index < positives:
            # Twenty outcomes inside 24h, ten inside 72h: both horizons publishable.
            outcome = anchor + timedelta(hours=12 if index < 20 else 48)
            label = _label(candidate, case_type="anticipatory", outcome_at=outcome)
            score = 7.0 + (index % 5)
        else:
            label = _label(candidate, case_type="detection",
                           observed_until=anchor + timedelta(hours=200))
            score = float(index % 7)
        labels.append(label)
        sealed.append(_sealed(candidate, score_initial=score, first_seen=anchor,
                              stratum="alert" if index < positives else "random"))
    _write_jsonl(root / "labels.jsonl", labels)
    _write_jsonl(root / "sealed.jsonl", sealed)
    return root / "labels.jsonl", root / "sealed.jsonl"


class CalibrationTests(unittest.TestCase):
    def test_time_split_and_no_leakage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            labels, sealed = _synthetic_dataset(Path(tmp))
            report = calibrate_files(labels, sealed, bootstrap=50)
        entry = report["horizons"]["72"]
        self.assertEqual(entry["train"]["n"] + entry["test"]["n"], entry["records"])
        self.assertLessEqual(entry["train"]["to"], entry["test"]["from"])

    def test_publishable_dataset_beats_base_rate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            labels, sealed = _synthetic_dataset(Path(tmp))
            first = calibrate_files(labels, sealed, bootstrap=100)
            second = calibrate_files(labels, sealed, bootstrap=100)
        for horizon in ("24", "72", "168"):
            entry = first["horizons"][horizon]
            self.assertTrue(entry["publishable"], horizon)
            self.assertIsNotNone(entry["brier"]["isotonic"])
            self.assertLess(entry["brier"]["isotonic"], entry["brier_base"])
            self.assertGreater(entry["skill"]["isotonic"], 0.0)
            self.assertEqual(entry["ci95"], second["horizons"][horizon]["ci95"])

    def test_guard_blocks_small_samples(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            labels, sealed = _synthetic_dataset(Path(tmp), count=40, positives=10)
            report = calibrate_files(labels, sealed, bootstrap=20)
        for horizon in ("24", "72", "168"):
            self.assertFalse(report["horizons"][horizon]["publishable"])

    def test_degenerate_train_falls_back_to_base_rate(self) -> None:
        records, _ = build_records(
            [_label(f"cluster:{2000 + i}@{_iso(ANCHOR + timedelta(days=i))}",
                    case_type="detection",
                    observed_until=ANCHOR + timedelta(days=i, hours=200))
             for i in range(10)],
            {f"cluster:{2000 + i}@{_iso(ANCHOR + timedelta(days=i))}":
             _sealed(f"cluster:{2000 + i}@{_iso(ANCHOR + timedelta(days=i))}",
                     score_initial=float(i), first_seen=ANCHOR + timedelta(days=i))
             for i in range(10)},
        )
        report = calibrate(records, bootstrap=10)
        entry = report["horizons"]["72"]
        self.assertEqual(entry["model"], "base_rate")
        self.assertTrue(entry["fallback_platt"])
        self.assertIn(entry["brier"]["isotonic"], (0.0, None))


class AnchorScoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_setter_freezes_first_score(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = DB(Path(tmp) / "t.db")
            await db.open()
            try:
                await db.execute(
                    "INSERT INTO clusters (id, theater, category, first_seen, last_seen)"
                    " VALUES (1, 'EU_RU', 'eu_ru', 100, 100)")
                self.assertTrue(await db.set_cluster_initial_score(1, 5.5))
                self.assertFalse(await db.set_cluster_initial_score(1, 9.5))
                row = await db.fetchone("SELECT score_initial FROM clusters WHERE id=1")
                self.assertAlmostEqual(float(row["score_initial"]), 5.5)
            finally:
                await db.close()

    async def test_pipeline_captures_anchor_score(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("TRIPWIRE_DATA_DIR")
            os.environ["TRIPWIRE_DATA_DIR"] = tmp
            app = TripwireApp()
            await app.start()
            try:
                source = app.sources["gnews_nato"]
                result = await app.process(RawItem(
                    source_id="gnews_nato",
                    title="Putin orders tactical nuclear weapons use against Ukraine",
                    body="", url="https://example.org/nuclear-anchor",
                    published_at=utcnow(), category="eu_ru", guid="anchor-1",
                ), source)
                self.assertEqual(result["kind"], "scored")
                row = await app.db.fetchone(
                    "SELECT total_score, score_initial FROM clusters ORDER BY id DESC LIMIT 1")
                self.assertIsNotNone(row["score_initial"])
                self.assertAlmostEqual(float(row["score_initial"]),
                                       float(row["total_score"]))
            finally:
                await app.close()
                if old is None:
                    os.environ.pop("TRIPWIRE_DATA_DIR", None)
                else:
                    os.environ["TRIPWIRE_DATA_DIR"] = old

    async def test_preexisting_cluster_does_not_anchor_late(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("TRIPWIRE_DATA_DIR")
            os.environ["TRIPWIRE_DATA_DIR"] = tmp
            app = TripwireApp()
            await app.start()
            try:
                source = app.sources["gnews_nato"]
                await app.process(RawItem(
                    source_id="gnews_nato",
                    title="Putin orders tactical nuclear weapons use against Ukraine",
                    body="", url="https://example.org/anchor-late",
                    published_at=utcnow(), category="eu_ru", guid="anchor-late-1",
                ), source)
                row = await app.db.fetchone(
                    "SELECT id FROM clusters ORDER BY id DESC LIMIT 1")
                cluster_id = int(row["id"])
                await app.db.execute(
                    "UPDATE clusters SET score_initial=NULL WHERE id=?", (cluster_id,))
                await app.db.insert_event(Event(
                    source_id="gnews_nato", tier=0,
                    url="https://example.org/anchor-late-2",
                    guid_hash="anchor-late-2", title="Second event",
                    body_excerpt="", theater="EU_RU", category="eu_ru",
                    cluster_id=cluster_id, ingested_at=utcnow(),
                    published_at=utcnow(),
                ))
                await app._capture_initial_score(
                    SimpleNamespace(id=cluster_id, score_initial=None), 9.0)
                row = await app.db.fetchone(
                    "SELECT score_initial FROM clusters WHERE id=?", (cluster_id,))
                self.assertIsNone(row["score_initial"])
            finally:
                await app.close()
                if old is None:
                    os.environ.pop("TRIPWIRE_DATA_DIR", None)
                else:
                    os.environ["TRIPWIRE_DATA_DIR"] = old


if __name__ == "__main__":
    unittest.main()
