"""Hermetic log-stream tests: config-driven sources, redaction, fixed node-2
command construction, no path outside the configured list."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import TempEnv, env_vars, fake_key

from gx_control_ui import logs
from gx_control_ui.logs import Stream, clamp_lines, read_stream, stream_by_id, streams, tail_file


class TestTail(unittest.TestCase):
    def test_tail_small_and_large(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "x.log")
            p.write_text("".join(f"line {i}\n" for i in range(100000)))
            self.assertEqual(tail_file(str(p), 3), ["line 99997", "line 99998", "line 99999"])
            q = Path(d, "y.log")
            q.write_text("a\nb")
            self.assertEqual(tail_file(str(q), 10), ["a", "b"])

    def test_clamp(self):
        self.assertEqual(clamp_lines("5"), logs.MIN_LINES)
        self.assertEqual(clamp_lines(10**9), logs.MAX_LINES)
        self.assertEqual(clamp_lines("abc"), logs.DEFAULT_LINES)
        self.assertEqual(clamp_lines(None), logs.DEFAULT_LINES)
        self.assertEqual(clamp_lines(300), 300)


class TestStreams(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv()
        self.tmp = Path(self.env.root)

    def tearDown(self):
        self.env.cleanup()

    def test_sources_come_from_config_and_are_safe(self):
        ids = [s.id for s in streams(self.env.cfg)]
        self.assertEqual(len(ids), len(set(ids)))
        for s in streams(self.env.cfg):
            self.assertIn(s.kind, ("file", "glob", "docker", "journal"))
            self.assertNotIn(".env", s.target)
            self.assertNotIn("secrets", s.target)
            self.assertNotIn("..", s.target)
        required = {"orchestrator", "scheduler-history", "scheduler-queue", "litellm", "mia",
                    "open-webui", "backup", "git-autosync", "git-reconcile", "audit-node1",
                    "hostwatch-node1", "control-ui-audit"}
        self.assertTrue(required <= set(ids), required - set(ids))
        # retired sources are gone
        for gone in ("swap-node1", "swap-node2", "gx-mini", "gx-code-01", "gx-code-02", "gx-auto",
                     "rank0-watch", "gxmax-safety"):
            self.assertNotIn(gone, ids, gone)

    def test_scheduler_history_source_points_at_the_state_dir(self):
        s = stream_by_id(self.env.cfg, "scheduler-history")
        self.assertEqual(s.target, str(self.env.cfg.gx_state_root / "scheduler" / "history.jsonl"))

    def test_unknown_stream(self):
        with self.assertRaises(KeyError):
            read_stream(self.env.cfg, "../../etc/passwd", 10)

    def test_file_stream_filter_and_redaction(self):
        secret = fake_key()
        f = self.tmp / "o.log"
        f.write_text("\n".join(["INFO hello", f"ERROR token={secret}", "INFO bye"]) + "\n")
        with mock.patch.object(logs, "stream_by_id", lambda cfg, i: Stream(
                "t", "t", "node1", "file", str(f), "g")):
            with env_vars(GX_ORCHESTRATOR_API_KEY=secret):
                data = read_stream(self.env.cfg, "t", 50, "error")
        self.assertEqual(data["count"], 1)
        self.assertNotIn(secret, data["lines"][0])
        self.assertEqual(data["error"], "")

    def test_missing_file_and_glob(self):
        with mock.patch.object(logs, "stream_by_id", lambda cfg, i: Stream(
                "m", "m", "node1", "file", str(self.tmp / "none.log"), "g")):
            self.assertIn("does not exist", read_stream(self.env.cfg, "m", 10)["error"])

    def test_glob_picks_latest(self):
        import os
        import time
        a = self.tmp / "dsv41-1.log"
        b = self.tmp / "dsv41-2.log"
        a.write_text("old\n")
        b.write_text("new\n")
        os.utime(a, (time.time() - 100, time.time() - 100))
        with mock.patch.object(logs, "stream_by_id", lambda cfg, i: Stream(
                "mia", "mia", "node1", "glob", str(self.tmp / "dsv41-*.log"), "g")):
            self.assertEqual(read_stream(self.env.cfg, "mia", 10)["lines"], ["new"])

    def test_query_is_bounded(self):
        f = self.tmp / "q.log"
        f.write_text("x\n")
        with mock.patch.object(logs, "stream_by_id", lambda cfg, i: Stream(
                "q", "q", "node1", "file", str(f), "g")):
            data = read_stream(self.env.cfg, "q", 10, "y" * 5000)
        self.assertEqual(len(data["query"]), logs.MAX_QUERY)

    def test_node2_offline(self):
        self.assertIn("offline mode", read_stream(self.env.cfg, "rank1", 10)["error"])

    def test_node2_command_is_fixed(self):
        captured = {}

        def fake_run(args, timeout=0, **kw):
            captured["args"] = args
            from gx_control_ui.util import CmdResult
            return CmdResult(0, "l1\nl2\n", 1.0)

        env = TempEnv(offline=False)
        self.addCleanup(env.cleanup)
        with mock.patch.object(logs, "run", fake_run):
            with mock.patch.object(logs, "stream_by_id", lambda cfg, i: Stream(
                    "rank1", "rank1", "node2", "file", "/home/legenex-02/gx-max-rank1.log", "g")):
                # a hostile "lines" value is clamped to the default — it never
                # reaches the remote command, and the target is shlex-quoted
                data = read_stream(env.cfg, "rank1", "25; rm -rf /", "")
                remote_hostile = captured["args"][-1]
                data = read_stream(env.cfg, "rank1", 25, "")
        self.assertEqual(data["lines"], ["l1", "l2"])
        self.assertEqual(remote_hostile, f"tail -n {logs.DEFAULT_LINES} -- "
                                          "/home/legenex-02/gx-max-rank1.log")
        self.assertEqual(captured["args"][-1], "tail -n 25 -- /home/legenex-02/gx-max-rank1.log")


if __name__ == "__main__":
    unittest.main()
