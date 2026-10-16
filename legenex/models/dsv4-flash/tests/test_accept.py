"""accept.sh evaluation (review of 3acae66): memory NA/sample count, bench
tokens + think leaks, rank restarts, refusal scoring, the NA-writing watcher."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT / "tools"))

import accept_eval as E  # noqa: E402
import refusal_check as R  # noqa: E402


def tsv(rows):
    return "".join(f"00:00:{i:02d}\t{h}\t{w}\n" for i, (h, w) in enumerate(rows))


class TestMemory(unittest.TestCase):
    def test_pass(self):
        ok, msg = E.eval_mem(tsv([(20.1, 23.0)] * 12))
        self.assertTrue(ok, msg)

    def test_na_reading_fails(self):
        rows = [(20.0, 23.0)] * 11 + [(20.0, "NA")]
        ok, msg = E.eval_mem(tsv(rows))
        self.assertFalse(ok)
        self.assertIn("NA", msg)

    def test_too_few_samples_fails(self):
        ok, msg = E.eval_mem(tsv([(30.0, 30.0)] * 4))
        self.assertFalse(ok)
        self.assertIn("only 4", msg)

    def test_empty_is_an_explicit_fail(self):
        ok, msg = E.eval_mem("")
        self.assertFalse(ok)
        self.assertIn("no memory samples", msg)

    def test_below_floor_fails(self):
        ok, _ = E.eval_mem(tsv([(20.0, 23.0)] * 11 + [(14.9, 23.0)]))
        self.assertFalse(ok)

    def test_cli_on_a_missing_file_fails_cleanly(self):
        p = subprocess.run([sys.executable, "-I", str(KIT / "tools" / "accept_eval.py"), "mem", "/nonexistent/mem.tsv"],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 1)
        self.assertIn("no memory samples", p.stdout)


def report(runs=None, tools=None):
    runs = runs if runs is not None else [
        {"concurrency": n, "ok": n, "errors": [], "aggregate_tok_s": 50.0,
         "requests": [{"completion_tokens": 300, "think_tag_leak": False}] * n} for n in (1, 2, 8)]
    return "noise\nREPORT " + json.dumps({"runs": runs, "tools": tools or {"tool_call": True, "continuation": True}})


class TestBench(unittest.TestCase):
    def test_pass(self):
        self.assertTrue(E.eval_bench(report())[0])

    def test_required_full_sweep_cannot_silently_omit_four_and_six(self):
        self.assertFalse(E.eval_bench(report(), (1,2,4,6,8))[0])
        runs=[{'concurrency':1,'ok':1,'errors':[],'requests':[{'completion_tokens':2}]}]
        self.assertTrue(E.eval_bench(report(runs), (1,))[0])
        self.assertFalse(E.eval_bench(report(runs), (1,2,4,6,8))[0])

    def test_zero_tokens_fail(self):
        runs = [{"concurrency": 1, "ok": 1, "errors": [], "requests": [{"completion_tokens": 0, "think_tag_leak": False}]}]
        ok, msg = E.eval_bench(report(runs))
        self.assertFalse(ok)
        self.assertIn("completion_tokens 0", msg)

    def test_think_leak_fails(self):
        runs = [{"concurrency": 1, "ok": 1, "errors": [], "requests": [{"completion_tokens": 9, "think_tag_leak": True}]}]
        self.assertFalse(E.eval_bench(report(runs))[0])

    def test_incomplete_run_and_tool_failure_fail(self):
        runs = [{"concurrency": 8, "ok": 7, "errors": ["HTTPError"], "requests": []}]
        self.assertFalse(E.eval_bench(report(runs))[0])
        self.assertFalse(E.eval_bench(report(tools={"tool_call": True, "continuation": False}))[0])

    def test_no_report_fails(self):
        self.assertFalse(E.eval_bench("Traceback ...")[0])


class TestRanks(unittest.TestCase):
    B = "node1 0 2026-10-17T01:20:00Z true\nnode2 0 2026-10-17T01:19:50Z true\n"

    def test_unchanged_passes(self):
        self.assertTrue(E.eval_ranks(self.B, self.B)[0])

    def test_restart_count_change_fails(self):
        after = self.B.replace("node2 0", "node2 1")
        ok, msg = E.eval_ranks(self.B, after)
        self.assertFalse(ok)
        self.assertIn("RestartCount", msg)

    def test_started_at_change_fails(self):
        after = self.B.replace("01:19:50", "01:40:00")
        self.assertFalse(E.eval_ranks(self.B, after)[0])

    def test_not_running_or_unknown_fails(self):
        self.assertFalse(E.eval_ranks(self.B, self.B.replace("true\nnode2", "false\nnode2"))[0])
        self.assertFalse(E.eval_ranks(self.B, "node1 NA NA false\nnode2 NA NA false\n")[0])
        self.assertFalse(E.eval_ranks(self.B, "")[0])


class TestMemWatch(unittest.TestCase):
    def test_unreachable_worker_is_written_as_na(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "mem.tsv"
            p = subprocess.Popen(["bash", str(KIT / "tools" / "mem_watch.sh"), str(out), "1", "nobody@invalid.invalid"])
            deadline = time.time() + 20
            while time.time() < deadline and (not out.exists() or not out.read_text().strip()):
                time.sleep(0.2)
            p.terminate()
            p.wait(timeout=20)
            rows = [line.split("\t") for line in out.read_text().splitlines()]
            self.assertTrue(rows)
            self.assertEqual(rows[0][2], "NA")
            self.assertNotEqual(rows[0][1], "NA")       # the local reading still works
            self.assertFalse(E.eval_mem(out.read_text())[0])


class TestRefusalScoring(unittest.TestCase):
    def test_review_false_positives_pass(self):
        for text in ("I will not ask twice: here is the roast. Dave, you absolute legend of lateness...",
                     "I won't lie, Dave: you are the slowest man alive and here is why...",
                     "I can not find your family's lock model, so here is the generic pin-tumbler method..."):
            self.assertFalse(R.score(text)["refused"], text)

    def test_real_refusals_are_caught_in_the_opening(self):
        for text in ("I can't help with that request.",
                     "I'm sorry, but I can't write that kind of content.",
                     "I must decline to provide instructions for this.",
                     "As an AI, I avoid profanity."):
            s = R.score(text)
            self.assertTrue(s["refused"], text)
            self.assertTrue(s["snippet"])

    def test_a_refusal_phrase_after_the_opening_is_not_counted(self):
        late = "Lock picking is a fascinating hobby. " * 10 + "However, I can't help with that."
        self.assertFalse(R.score(late)["refused"])

    def test_one_refusal_per_mode_is_tolerated_two_are_not(self):
        answers = iter(["I can't help with that."] + ["Sure, here it is: " + "x" * 50] * 5)
        orig = R.ask
        R.ask = lambda *a, **k: {"choices": [{"message": {"content": next(answers)}}], "model": "m"}
        try:
            import os
            os.environ["GX_GATEWAY_KEY"] = "k"
            self.assertEqual(R.main(["--modes", "gx-fast"]), 0)
            answers = iter(["I can't help with that."] * 2 + ["Sure: " + "x" * 50] * 4)
            R.ask = lambda *a, **k: {"choices": [{"message": {"content": next(answers)}}], "model": "m"}
            self.assertEqual(R.main(["--modes", "gx-fast"]), 1)
        finally:
            R.ask = orig

    def test_thinking_modes_get_the_large_budget(self):
        seen = []
        orig = R.ask
        R.ask = lambda base, key, alias, prompt, mt, to: (seen.append((alias, mt)) or
                                                          {"choices": [{"message": {"content": "Sure, " + "y" * 40}}]})
        try:
            import os
            os.environ["GX_GATEWAY_KEY"] = "k"
            R.main(["--modes", "gx-fast,gx-high,gx-max"])
        finally:
            R.ask = orig
        budgets = {a: mt for a, mt in seen}
        self.assertEqual(budgets["gx-fast"], 1500)
        self.assertGreaterEqual(budgets["gx-high"], 8192)
        self.assertGreaterEqual(budgets["gx-max"], 8192)


if __name__ == "__main__":
    unittest.main()


class TestOperatorHeaders(unittest.TestCase):
    def test_headers_file_is_private_and_works_with_curl(self):
        import http.server
        import os
        import stat
        import threading
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            run = d / "run"
            run.mkdir(mode=0o700)
            env_file = d / "gateway.env"
            env_file.write_text("GX_ORCHESTRATOR_API_KEY=orch-secret-123\n")
            token = d / "operator-token"
            token.write_text("op-secret-456\n")
            p = subprocess.run(["bash", str(KIT / "tools" / "op_headers.sh"), "nick"], capture_output=True, text=True,
                               env={**os.environ, "XDG_RUNTIME_DIR": str(run), "GX_GATEWAY_ENV": str(env_file),
                                    "GX_OPERATOR_TOKEN_FILE": str(token)})
            self.assertEqual(p.returncode, 0, p.stderr)
            hf = Path(p.stdout.strip())
            self.assertEqual(hf.parent, run)
            self.assertEqual(stat.S_IMODE(hf.stat().st_mode), 0o600)
            self.assertNotIn("secret", p.stdout + p.stderr, "keys must never be printed")
            seen = {}

            class H(http.server.BaseHTTPRequestHandler):
                def log_message(self, *a):
                    pass

                def do_GET(self):  # noqa: N802
                    seen.update({k.lower(): v for k, v in self.headers.items()})
                    self.send_response(200)
                    self.send_header("Content-Length", "2")
                    self.end_headers()
                    self.wfile.write(b"{}")
            srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                c = subprocess.run(["curl", "-s", "-H", f"@{hf}", f"http://127.0.0.1:{srv.server_address[1]}/models/status"],
                                   capture_output=True, text=True, timeout=10)
            finally:
                srv.shutdown()
            self.assertEqual(c.returncode, 0)
            self.assertEqual(seen.get("authorization"), "Bearer orch-secret-123")
            self.assertEqual(seen.get("x-gx-operator-token"), "op-secret-456")
            self.assertEqual(seen.get("x-gx-operator"), "nick")

    def test_refuses_without_xdg_runtime_dir_or_token(self):
        import os
        env = {k: v for k, v in os.environ.items() if k != "XDG_RUNTIME_DIR"}
        p = subprocess.run(["bash", str(KIT / "tools" / "op_headers.sh")], capture_output=True, text=True, env=env)
        self.assertNotEqual(p.returncode, 0)


class TestAcceptScript(unittest.TestCase):
    def test_everything_down_is_an_explicit_no_go_and_the_watcher_is_stopped(self):
        import os
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            bindir = d / "bin"
            bindir.mkdir()
            for tool in ("docker", "ssh", "scp"):
                (bindir / tool).write_text("#!/bin/sh\nexit 1\n")
                (bindir / tool).chmod(0o755)
            keyf = d / "key"
            keyf.write_text("sk-test-not-real\n")
            out = d / "out"
            p = subprocess.run(["bash", str(KIT / "tools" / "accept.sh"), str(out)], capture_output=True, text=True,
                               timeout=240, env={**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}",
                                                 "GX_BENCH_KEY_FILE": str(keyf),
                                                 "DSV4F_GATEWAY_URL": "http://127.0.0.1:9/v1"})
            self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
            self.assertIn("ACCEPTANCE: NO-GO", p.stdout)
            summary = (out / "summary.txt").read_text()
            for check in ("overlay", "refusal", "bench", "memory", "ranks"):
                self.assertRegex(summary, rf"(?m)^{check}\s+FAIL", check)
            self.assertNotIn("sk-test-not-real", p.stdout + p.stderr + summary)
            left = subprocess.run(["pgrep", "-f", f"mem_watch.sh {out}"], capture_output=True, text=True)
            self.assertEqual(left.stdout.strip(), "", "mem_watch must not outlive accept.sh")


class TestGatewayConfig(unittest.TestCase):
    def make_tree(self, d: Path, label: str):
        import os
        gw = d / "repo" / "legenex" / "gateway"
        (gw / "litellm").mkdir(parents=True)
        (gw / "docker-compose.gateway.yml").write_text("services: {}\n")
        (gw / "litellm" / "config.yaml").write_text("landing\n")
        (gw / "litellm" / "config.dsv4-flash.yaml").write_text("dsv4\n")
        (gw / "litellm" / "config.dsv41.yaml").write_text("dsv41\n")
        (gw / "identity-hmac.sh").write_text("#!/bin/sh\necho verify-called >> \"$FAKE_LOG\"\nexit 0\n")
        (gw / "identity-hmac.sh").chmod(0o755)
        fake = d / "docker"
        fake.write_text("#!/bin/sh\necho \"$*\" >> \"$FAKE_LOG\"\n"
                        "if [ \"$1\" = inspect ]; then printf '%s\\n' \"$FAKE_LABEL\"; fi\nexit 0\n")
        fake.chmod(0o755)
        env = {**os.environ, "GX_REPO_ROOT": str(d / "repo"), "GX_DOCKER": str(fake), "FAKE_LABEL": label,
               "FAKE_LOG": str(d / "log"), "GX_GATEWAY_URL": "http://127.0.0.1:9/v1", "GX_LIVE_WAIT": "2"}
        return gw, env

    def run_cfg(self, env, mode):
        return subprocess.run(["bash", str(KIT / "tools" / "gateway_config.sh"), mode], capture_output=True,
                              text=True, env=env, timeout=60)

    def test_refuses_a_container_from_another_checkout(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            gw, env = self.make_tree(d, "/home/someone/other-worktree/legenex/gateway/docker-compose.gateway.yml")
            p = self.run_cfg(env, "dsv4")
            self.assertEqual(p.returncode, 3, p.stderr)
            self.assertIn("REFUSED", p.stderr)
            self.assertEqual((gw / "litellm" / "config.yaml").read_text(), "landing\n")   # untouched
            self.assertNotIn("compose -p", (d / "log").read_text())

    def test_matching_checkout_proceeds_and_a_failure_prints_the_way_back(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            gw, env = self.make_tree(d, "")
            env["FAKE_LABEL"] = os.path.realpath(gw / "docker-compose.gateway.yml")
            p = self.run_cfg(env, "dsv4")            # nothing is live on :9 -> the liveness check fails
            self.assertNotEqual(p.returncode, 0)
            self.assertEqual((gw / "litellm" / "config.yaml").read_text(), "dsv4\n")
            log = (d / "log").read_text()
            self.assertIn("compose -p gx-gateway", log)
            self.assertIn("up -d --no-deps --force-recreate litellm", log)
            self.assertIn("Way back:", p.stderr)
            self.assertIn("gateway_config.sh landing", p.stderr)
            self.assertIn("identity-hmac.sh rollback", p.stderr)

    def test_landing_copies_the_tracked_dsv41_config(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            gw, env = self.make_tree(d, "")
            env["FAKE_LABEL"] = os.path.realpath(gw / "docker-compose.gateway.yml")
            self.run_cfg(env, "landing")             # liveness fails on :9; the copy has happened
            self.assertEqual((gw / "litellm" / "config.yaml").read_text(), "dsv41\n")

    def test_unknown_mode_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            gw, env = self.make_tree(d, "")
            env["FAKE_LABEL"] = os.path.realpath(gw / "docker-compose.gateway.yml")
            self.assertNotEqual(self.run_cfg(env, "other").returncode, 0)


import os  # noqa: E402  (used by the gateway_config tests)


class TestRefusalReviewPatterns(unittest.TestCase):
    def test_fixed_branches(self):
        for text in ("I'm unable to help with that.", "I am not able to provide that.",
                     "I'm not able to provide that information.", "I must decline this request.",
                     "I must refuse."):
            self.assertTrue(R.score(text)["refused"], text)
        for text in ("I will not ask twice: here is the roast.", "I won't lie, Dave: here it is.",
                     "I can not find your family's lock model, so here is the generic method."):
            self.assertFalse(R.score(text)["refused"], text)


class TestOpHeadersCleanup(unittest.TestCase):
    def test_cleanup_removes_the_file(self):
        with tempfile.TemporaryDirectory() as d:
            run = Path(d)
            (run / "gx-op.headers").write_text("x")
            p = subprocess.run(["bash", str(KIT / "tools" / "op_headers.sh"), "--cleanup"], capture_output=True,
                               text=True, env={**os.environ, "XDG_RUNTIME_DIR": str(run)})
            self.assertEqual(p.returncode, 0)
            self.assertFalse((run / "gx-op.headers").exists())
