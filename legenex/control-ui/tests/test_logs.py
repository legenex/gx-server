from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import TempEnv, env_vars, fake_key

from gx_control_ui import logs
from gx_control_ui.logs import Stream, clamp_lines, read_stream, tail_file


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

    def test_catalogue_is_fixed_and_safe(self):
        ids = [s.id for s in logs.STREAMS]
        self.assertEqual(len(ids), len(set(ids)))
        for s in logs.STREAMS:
            self.assertIn(s.kind, ("file", "glob", "docker", "journal"))
            self.assertNotIn(".env", s.target)
            self.assertNotIn("secrets", s.target)
            self.assertNotIn("..", s.target)
        required = {"orchestrator", "litellm", "swap-node1", "swap-node2", "gx-mini",
                    "gx-code-01", "gx-code-02", "gx-auto", "open-webui", "backup",
                    "git-autosync", "git-reconcile", "audit-node1", "audit-node2", "hostwatch-node1"}
        self.assertTrue(required <= set(ids), required - set(ids))

    def test_unknown_stream(self):
        with self.assertRaises(KeyError):
            read_stream(self.env.cfg, "../../etc/passwd", 10)

    def test_file_stream_filter_and_redaction(self):
        secret = fake_key()
        f = self.tmp / "o.log"
        f.write_text("\n".join(["INFO hello", f"ERROR token={secret}", "INFO bye"]) + "\n")
        fake = (Stream("t", "t", "node1", "file", str(f), "g"),)
        with mock.patch.object(logs, "BY_ID", {s.id: s for s in fake}), env_vars(GX_SWAP_API_KEY=secret):
            data = read_stream(self.env.cfg, "t", 50, "error")
        self.assertEqual(data["count"], 1)
        self.assertNotIn(secret, data["lines"][0])
        self.assertEqual(data["error"], "")

    def test_missing_file_and_glob(self):
        fake = (Stream("m", "m", "node1", "file", str(self.tmp / "none.log"), "g"),
                Stream("g", "g", "node1", "glob", str(self.tmp / "nothing-*.tsv"), "g"))
        with mock.patch.object(logs, "BY_ID", {s.id: s for s in fake}):
            self.assertIn("does not exist", read_stream(self.env.cfg, "m", 10)["error"])
            self.assertIn("does not exist", read_stream(self.env.cfg, "g", 10)["error"])

    def test_glob_picks_latest(self):
        import os
        import time
        a = self.tmp / "s-1.tsv"
        b = self.tmp / "s-2.tsv"
        a.write_text("old\n")
        b.write_text("new\n")
        os.utime(a, (time.time() - 100, time.time() - 100))
        fake = (Stream("g", "g", "node1", "glob", str(self.tmp / "s-*.tsv"), "g"),)
        with mock.patch.object(logs, "BY_ID", {s.id: s for s in fake}):
            self.assertEqual(read_stream(self.env.cfg, "g", 10)["lines"], ["new"])

    def test_query_is_bounded(self):
        f = self.tmp / "q.log"
        f.write_text("x\n")
        fake = (Stream("q", "q", "node1", "file", str(f), "g"),)
        with mock.patch.object(logs, "BY_ID", {s.id: s for s in fake}):
            data = read_stream(self.env.cfg, "q", 10, "y" * 5000)
        self.assertEqual(len(data["query"]), logs.MAX_QUERY)

    def test_node2_offline(self):
        data = read_stream(self.env.cfg, "rank1", 10)
        self.assertEqual(data["error"], "offline mode")

    def test_node2_command_is_fixed(self):
        captured = {}

        def fake_run(args, timeout=0, **kw):
            captured["args"] = args
            from gx_control_ui.util import CmdResult
            return CmdResult(0, "l1\nl2\n", 1.0)

        env = TempEnv(offline=False)
        try:
            with mock.patch.object(logs, "run", fake_run):
                data = read_stream(env.cfg, "gx-code-02", "25; rm -rf /")
        finally:
            env.cleanup()
        self.assertEqual(data["lines"], ["l1", "l2"])
        remote = captured["args"][-1]
        self.assertEqual(remote, f"docker logs --tail {logs.DEFAULT_LINES} --timestamps gx-code 2>&1")


if __name__ == "__main__":
    unittest.main()
