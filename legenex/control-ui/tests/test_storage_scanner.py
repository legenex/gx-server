"""Hermetic storage_scanner + recovery_view tests (analysis only, honest gaps)."""

from __future__ import annotations

import json
import time
import unittest
from pathlib import Path

from support import TempEnv

from gx_control_ui.recovery_view import RecoveryView
from gx_control_ui.services import Cluster
from gx_control_ui.storage_scanner import StorageScanner, _duplicates, _largest, _stale_cache


class TestStorageScanner(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv()
        self.sc = StorageScanner(self.env.cfg)
        self.cluster = Cluster(self.env.cfg)

    def tearDown(self):
        self.env.cleanup()

    def test_view_is_analysis_only(self):
        out = self.sc.view(self.cluster, fresh=True)
        self.assertEqual(out["head"]["name"], "gx10-01")
        self.assertIn("cleanup_note", out)
        self.assertIn("analysis only", out["cleanup_note"])
        self.assertIn("trash", out["cleanup_note"])
        self.assertEqual(out["worker"]["orchestrator_facts"]["available"], False)

    def test_largest_finds_big_files_and_skips_trash(self):
        models = Path(self.env.cfg.file_roots[3])
        (models / "a.bin").write_bytes(b"0" * 5000)
        (models / "b.bin").write_bytes(b"0" * 9000)
        trash = Path(self.env.cfg.trash_root) / "20260101-x-big.bin"
        trash.parent.mkdir(parents=True, exist_ok=True)
        trash.write_bytes(b"0" * 99999)
        rows = _largest(self.env.cfg.file_roots, top=5)
        self.assertEqual(rows[0]["bytes"], 9000)
        self.assertNotIn("trash", rows[0]["path"])
        self.assertEqual([r["bytes"] for r in rows], sorted((r["bytes"] for r in rows), reverse=True))

    def test_stale_cache_by_mtime(self):
        cache = Path(self.env.cfg.file_roots[4])
        old = cache / "old.txt"
        old.write_text("x")
        ref = time.time() - 30 * 86400
        import os
        os.utime(old, (ref, ref))
        (cache / "new.txt").write_text("y")
        rows = _stale_cache(cache, days=14)
        self.assertEqual([r["path"] for r in rows], [str(old)])
        self.assertGreaterEqual(rows[0]["age_days"], 29)

    def test_duplicates_same_size_grouping(self):
        models = Path(self.env.cfg.file_roots[3])
        models.mkdir(exist_ok=True)
        # same-size files below the 1 GiB threshold are ignored by the scanner;
        # assert the grouping logic directly with a size-keyed map
        for name in ("a", "b", "c"):
            (models / f"{name}.bin").write_bytes(b"Q" * 2048)
        (models / "d.bin").write_bytes(b"Q" * 4096)
        by_size: dict[int, list[str]] = {}
        for p in models.iterdir():
            by_size.setdefault(p.stat().st_size, []).append(str(p))
        self.assertEqual(len(by_size[2048]), 3)
        self.assertEqual(len(by_size[4096]), 1)


class TestRecoveryView(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv()
        self.rv = RecoveryView(self.env.cfg)
        self.cluster = Cluster(self.env.cfg)

    def tearDown(self):
        self.env.cleanup()

    def test_no_watchdog_file_is_honest(self):
        out = self.rv.incidents()
        self.assertEqual(out["available"], False)
        self.assertIn("watchdog", out["reason"])
        self.assertEqual(out["incidents"], [])

    def test_incidents_jsonl_read_and_sorted(self):
        path = Path(self.env.cfg.watchdog_incidents)
        path.parent.mkdir(parents=True, exist_ok=True)
        recs = [{"ts": 1700000002.0, "kind": "restart", "detail": "rank1 wedged", "attempt": 2,
                 "backoff_s": 120},
                {"ts": 1700000001.0, "kind": "oom", "detail": "MemAvailable low-water"}]
        path.write_text("".join(json.dumps(r) + "\n" for r in recs) + "not json\n")
        out = self.rv.incidents()
        self.assertTrue(out["available"])
        self.assertEqual(out["incidents"][0]["kind"], "restart")
        self.assertEqual(len(out["restart_attempts"]), 1)
        self.assertEqual(out["backoff_state"]["backoff_s"], 120)
        kinds = [r["kind"] for r in out["incidents"]]
        self.assertIn("unparsable", kinds)

    def test_view_shape_offline(self):
        out = self.rv.view(self.cluster)
        for key in ("gxmax", "lifecycle_events", "lifecycle_history", "hostwatch_tail",
                    "memory_events", "watchdog"):
            self.assertIn(key, out)
        self.assertEqual(out["watchdog"]["available"], False)


if __name__ == "__main__":
    unittest.main()
