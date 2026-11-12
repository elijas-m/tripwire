"""Blind label export: stratification, blindness and deterministic sampling."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tripwire.db import DB
from tripwire.labels import export_label_candidates, merge_label_rounds
from tripwire.models import Event
from tripwire.util import now_ts, utcnow


class LabelExportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = DB(self.root / "t.db")
        await self.db.open()
        now = now_ts()
        self.now = now
        await self._cluster(101, "EU_RU", now - 3600, 20.0, 3)
        await self._cluster(102, "ISRAEL", now - 3600, 5.5, 1)
        await self._cluster(103, "GULF", now - 3600, 1.2, 0)
        await self._cluster(104, "MENA", now - 3600, 0.5, 0)
        await self._cluster(105, "GULF", now - 3600, 2.0, 0)
        await self._cluster(106, "NATURE", now - 3600, 1.0, 0)
        await self.db.insert_alert(101, 3, None, {"score": 20.0},
                                   decided_at=now - 1800)
        for cluster_id in (101, 102, 103, 104, 105, 106):
            await self._event(cluster_id)

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.tmp.cleanup()

    async def _cluster(self, cluster_id: int, theater: str, first_seen: int,
                       score: float, level: int) -> None:
        await self.db.execute(
            """INSERT INTO clusters
               (id, theater, category, title_canonical, first_seen, last_seen,
                total_score, level)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (cluster_id, theater, theater.lower(), f"Cluster {cluster_id}",
             first_seen, first_seen, score, level),
        )

    async def _event(self, cluster_id: int, source_id: str = "test_feed") -> None:
        await self.db.insert_event(Event(
            source_id=source_id, tier=1, url=f"https://example.test/{cluster_id}",
            guid_hash=f"h{cluster_id}", title=f"Event {cluster_id}",
            body_excerpt="body", theater="EU_RU", category="eu_ru",
            cluster_id=cluster_id, ingested_at=utcnow(), published_at=utcnow(),
        ))

    async def _export(self, out: Path, **kwargs):
        options = {"since_days": 2, "negatives_per_theater": 1}
        options.update(kwargs)
        return await export_label_candidates(self.db, out, **options)

    async def test_strata_and_geofence(self) -> None:
        out = self.root / "pilot"
        result = await self._export(out)
        self.assertEqual(result["counts"],
                         {"alert": 1, "band_8plus": 0, "band_4_8": 0,
                          "band_2_4": 0, "band_0_2": 0, "random": 3})
        self.assertEqual(result["manifest"]["schema_version"], 2)
        self.assertFalse(result["manifest"]["require_anchor"])
        candidates = [json.loads(line) for line in
                      (out / "candidates.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(candidates), 4)
        self.assertNotIn(106, {item["cluster_id"] for item in candidates})

    async def test_require_anchor_excludes_unfrozen_clusters(self) -> None:
        out = self.root / "anchored"
        empty = await self._export(out, require_anchor=True)
        self.assertEqual(empty["candidates"], 0)
        for cluster_id, score in ((101, 20.0), (102, 5.5), (103, 1.2)):
            self.assertTrue(
                await self.db.set_cluster_initial_score(cluster_id, score))
        out = self.root / "anchored-set"
        result = await self._export(out, require_anchor=True)
        self.assertEqual(result["counts"],
                         {"alert": 1, "band_8plus": 0, "band_4_8": 1,
                          "band_2_4": 0, "band_0_2": 1, "random": 0})
        sealed = [json.loads(line) for line in
                  (out / "sealed.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertTrue(all(item["score_initial"] is not None for item in sealed))

    async def test_min_age_skips_open_windows(self) -> None:
        out = self.root / "open"
        result = await self._export(out, min_age_hours=2)
        self.assertEqual(result["candidates"], 0)
        self.assertEqual(result["manifest"]["min_age_hours"], 2)
        closed = await self._export(self.root / "closed", min_age_hours=0.5)
        self.assertEqual(closed["candidates"], 4)

    async def test_candidates_are_blind(self) -> None:
        out = self.root / "pilot"
        await self._export(out)
        candidates = [json.loads(line) for line in
                      (out / "candidates.jsonl").read_text(encoding="utf-8").splitlines()]
        for item in candidates:
            for hidden in ("score", "level", "stratum", "decided_at", "alert_level"):
                self.assertNotIn(hidden, item)
            self.assertTrue(item["events"])
            self.assertNotIn("score", item["events"][0])
        sealed = [json.loads(line) for line in
                  (out / "sealed.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual({item["candidate_id"] for item in sealed},
                         {item["candidate_id"] for item in candidates})
        self.assertEqual({item["stratum"] for item in sealed},
                         {"alert", "random"})

    async def test_sampling_is_deterministic(self) -> None:
        first = self.root / "one"
        second = self.root / "two"
        await self._export(first, seed="fixed")
        await self._export(second, seed="fixed")
        first_ids = [json.loads(line)["candidate_id"] for line in
                     (first / "candidates.jsonl").read_text(encoding="utf-8").splitlines()]
        second_ids = [json.loads(line)["candidate_id"] for line in
                      (second / "candidates.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(first_ids, second_ids)

    async def test_signal_bands_are_not_duplicated(self) -> None:
        await self._cluster(107, "EU_RU", self.now - 3600, 2.0, 0)
        await self._cluster(108, "EU_RU", self.now - 3600, 5.0, 0)
        await self._event(107)
        await self._event(108)
        for cluster_id, score in ((107, 2.0), (108, 5.0)):
            self.assertTrue(await self.db.set_cluster_initial_score(cluster_id, score))
        out = self.root / "bands"
        result = await self._export(out, negatives_per_theater=1, signal_per_band=2)
        self.assertEqual(result["counts"]["band_0_2"], 1)
        self.assertEqual(result["counts"]["band_4_8"], 1)
        sealed = self._rows(out / "sealed.jsonl")
        strata = {item["cluster_id"]: item["stratum"] for item in sealed}
        self.assertEqual(strata[107], "band_0_2")
        self.assertEqual(strata[108], "band_4_8")
        self.assertEqual(len(strata), len(sealed))

    async def test_bands_follow_anchor_not_final_score(self) -> None:
        await self._cluster(109, "EU_RU", self.now - 3600, 12.0, 0)
        await self._event(109)
        self.assertTrue(await self.db.set_cluster_initial_score(109, 0.0))
        out = self.root / "anchor-vs-total"
        result = await self._export(out, require_anchor=True, negatives_per_theater=2)
        strata = {item["cluster_id"]: item["stratum"]
                  for item in self._rows(out / "sealed.jsonl")}
        self.assertEqual(strata[109], "random")
        self.assertEqual(result["counts"]["band_8plus"], 0)

    async def test_band_edges_are_left_open_right_closed(self) -> None:
        for cluster_id, score in ((110, 2.0), (111, 4.0), (112, 8.0)):
            await self._cluster(cluster_id, "ISRAEL", self.now - 3600, score, 0)
            await self._event(cluster_id)
            self.assertTrue(await self.db.set_cluster_initial_score(cluster_id, score))
        out = self.root / "edges"
        await self._export(out, require_anchor=True, signal_per_band=5)
        strata = {item["cluster_id"]: item["stratum"]
                  for item in self._rows(out / "sealed.jsonl")}
        self.assertEqual(strata[110], "band_0_2")
        self.assertEqual(strata[111], "band_2_4")
        self.assertEqual(strata[112], "band_4_8")

    async def test_existing_export_is_not_overwritten(self) -> None:
        out = self.root / "pilot"
        await self._export(out)
        with self.assertRaises(FileExistsError):
            await self._export(out)
        result = await self._export(out, force=True)
        self.assertEqual(result["candidates"], 4)

    def _rows(self, path: Path) -> list[dict]:
        return [json.loads(line) for line in
                path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def _write_rows(self, path: Path, rows: list[dict]) -> None:
        path.write_text("".join(json.dumps(row) + "\n" for row in rows),
                        encoding="utf-8")

    async def test_template_is_blind_and_keys_on_cluster(self) -> None:
        out = self.root / "pilot"
        await self._export(out)
        candidates = self._rows(out / "candidates.jsonl")
        template = self._rows(out / "labels.template.jsonl")
        self.assertEqual(len(template), len(candidates))
        self.assertEqual({row["candidate_id"] for row in template},
                         {row["candidate_id"] for row in candidates})
        first_seen = {row["candidate_id"]: row["first_seen"] for row in candidates}
        label_ids = [row["label_id"] for row in template]
        self.assertEqual(len(label_ids), len(set(label_ids)))
        for row in template:
            self.assertEqual(
                row["label_id"],
                f"L{first_seen[row['candidate_id']][:7]}-{row['cluster_id']}")
            self.assertIsNone(row["case_type"])
            self.assertIsNone(row["labeled_at"])
            self.assertFalse(row["annotator"])
            for hidden in ("score", "level", "stratum", "decided_at", "alert_level"):
                self.assertNotIn(hidden, row)

    async def test_exclusions_skip_completed_labels(self) -> None:
        first = self.root / "round-1"
        await self._export(first)
        candidates = self._rows(first / "candidates.jsonl")
        alert = next(row for row in candidates if row["cluster_id"] == 101)
        near = next(row for row in candidates if row["cluster_id"] == 102)
        self._write_rows(first / "labels.jsonl", [{
            "label_id": "L2026-11-101",
            "candidate_id": alert["candidate_id"],
            "cluster_id": 101,
            "case_type": "detection",
            "labeled_at": "2026-11-04T10:00:00Z",
        }])
        # A draft agent label is not a human label; the episode stays open.
        self._write_rows(first / "labels.agent.jsonl", [{
            "label_id": "L2026-11-102",
            "candidate_id": near["candidate_id"],
            "cluster_id": 102,
            "case_type": "detection",
            "labeled_at": "2026-11-04T10:00:00Z",
        }])
        second = self.root / "round-2"
        result = await self._export(second, exclude_paths=[first])
        self.assertEqual(result["counts"],
                         {"alert": 0, "band_8plus": 0, "band_4_8": 0,
                          "band_2_4": 0, "band_0_2": 0, "random": 3})
        self.assertEqual(result["manifest"]["excluded_known"], 1)
        offered = {row["cluster_id"] for row in self._rows(second / "candidates.jsonl")}
        self.assertNotIn(101, offered)
        self.assertIn(102, offered)

    async def test_unlabeled_round_is_not_excluded(self) -> None:
        first = self.root / "round-1"
        await self._export(first)
        second = self.root / "round-2"
        result = await self._export(second, exclude_paths=[first])
        self.assertEqual(result["manifest"]["excluded_known"], 0)
        self.assertEqual(
            [row["candidate_id"] for row in self._rows(second / "candidates.jsonl")],
            [row["candidate_id"] for row in self._rows(first / "candidates.jsonl")])

    async def test_missing_exclude_path_is_an_error(self) -> None:
        with self.assertRaises(FileNotFoundError):
            await self._export(self.root / "round", exclude_paths=[self.root / "nope"])

    async def test_merge_resolves_supersedes_and_sealed(self) -> None:
        root = self.root / "m2"
        early = root / "2026-11-04"
        late = root / "2026-11-11"
        early.mkdir(parents=True)
        late.mkdir(parents=True)
        candidate_a = "cluster:101@2026-10-30T00:00:00Z"
        candidate_b = "cluster:102@2026-10-30T00:00:00Z"
        candidate_c = "cluster:103@2026-10-31T00:00:00Z"
        candidate_d = "cluster:104@2026-10-31T00:00:00Z"
        self._write_rows(early / "labels.jsonl", [
            {"label_id": "L2026-11-101", "candidate_id": candidate_a,
             "cluster_id": 101, "case_type": "detection",
             "labeled_at": "2026-11-04T10:00:00Z"},
            {"label_id": "L2026-11-102", "candidate_id": candidate_b,
             "cluster_id": 102, "case_type": None,
             "labeled_at": "2026-11-04T10:00:00Z"},
        ])
        self._write_rows(early / "sealed.jsonl", [
            {"candidate_id": candidate_a, "cluster_id": 101, "score_initial": 5.0},
            {"candidate_id": candidate_b, "cluster_id": 102, "score_initial": None},
            {"candidate_id": candidate_d, "cluster_id": 104, "score_initial": 4.0},
        ])
        self._write_rows(late / "labels.jsonl", [
            {"label_id": "L2026-11-201", "candidate_id": candidate_a,
             "cluster_id": 101, "case_type": "anticipatory",
             "labeled_at": "2026-11-11T10:00:00Z", "supersedes": "L2026-11-101"},
            {"label_id": "L2026-11-203", "candidate_id": candidate_c,
             "cluster_id": 103, "case_type": "non_material",
             "labeled_at": "2026-11-11T10:00:00Z"},
        ])
        self._write_rows(late / "sealed.jsonl", [
            {"candidate_id": candidate_a, "cluster_id": 101, "score_initial": 5.0},
            {"candidate_id": candidate_b, "cluster_id": 102, "score_initial": 2.0},
            {"candidate_id": candidate_c, "cluster_id": 103, "score_initial": 3.0},
            {"candidate_id": candidate_d, "cluster_id": 104, "score_initial": 6.0},
        ])

        out = root / "aggregate"
        result = merge_label_rounds(root, out)
        self.assertEqual(result["labels"], 2)
        self.assertEqual(result["labels_raw"], 3)
        self.assertEqual(result["skipped_incomplete"], 1)
        self.assertEqual(result["conflicting_labels"], 0)
        self.assertEqual(result["duplicate_sealed"], 3)
        self.assertEqual(result["conflicting_sealed"], 1)
        self.assertEqual(result["labels_without_sealed"], 0)
        self.assertEqual(result["sealed_without_label"], 2)
        self.assertEqual(result["rounds"], ["2026-11-04", "2026-11-11"])

        labels = {row["candidate_id"]: row for row in self._rows(out / "labels.jsonl")}
        self.assertEqual(labels[candidate_a]["label_id"], "L2026-11-201")
        self.assertEqual(labels[candidate_a]["case_type"], "anticipatory")
        self.assertEqual(labels[candidate_c]["case_type"], "non_material")
        self.assertNotIn(candidate_b, labels)
        sealed = {row["candidate_id"]: row for row in self._rows(out / "sealed.jsonl")}
        self.assertEqual(sealed[candidate_a]["score_initial"], 5.0)
        self.assertEqual(sealed[candidate_b]["score_initial"], 2.0)
        self.assertEqual(sealed[candidate_d]["score_initial"], 4.0)

        again = merge_label_rounds(root, out)
        self.assertEqual(again["rounds"], ["2026-11-04", "2026-11-11"])
        self.assertEqual(again["labels"], 2)

    async def test_merge_rejects_missing_or_empty_root(self) -> None:
        with self.assertRaises(FileNotFoundError):
            merge_label_rounds(self.root / "absent", self.root / "aggregate")
        empty = self.root / "empty"
        empty.mkdir()
        with self.assertRaises(FileNotFoundError):
            merge_label_rounds(empty, empty / "aggregate")
        with self.assertRaises(ValueError):
            merge_label_rounds(empty, empty)


if __name__ == "__main__":
    unittest.main()
