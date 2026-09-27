"""Tests for the gx-max lifecycle state machine (V4.1 Mia adapter).

These use a temporary directory containing fake start.sh / stop.sh scripts
(the real lifecycle drives mia-dsv41/start.sh the same way: bash, cwd =
runtime dir, env merged with the profile overlay) plus a stub OpenAI server
on a loopback port, so the real cluster is never touched. The readiness
probe under test is the REAL one: /health + /v1/models + one completion
(17*19=323) against the stub.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from gx_orchestrator.lifecycle import (  # noqa: E402
    AcquisitionError,
    GxMaxLifecycle,
    State,
    build_env_overlay,
    default_ready_probe,
)
from registry_fixtures import load_fixture_registry  # noqa: E402


class _StubUpstream(BaseHTTPRequestHandler):
    """Serves /health, /v1/models and one canned completion ('323')."""

    healthy = False
    model_id = "DeepSeek-v4.1-Flash-EXL3"

    def log_message(self, *a):  # noqa: A003
        pass

    def do_GET(self):  # noqa: N802
        if self.path.endswith("/health"):
            code = 200 if type(self).healthy else 503
            body = b"ok" if code == 200 else b"unavailable"
        elif self.path.endswith("/models"):
            body = json.dumps({"data": [{"id": type(self).model_id}]}).encode()
            code = 200
        else:
            code, body = 404, b"{}"
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        body = json.dumps({
            "choices": [{"message": {"content": "17*19=323"}}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 5},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class LifecycleTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        _StubUpstream.healthy = False
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), _StubUpstream)
        self.addCleanup(self.srv.shutdown)
        self.addCleanup(self.srv.server_close)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.api_base = f"http://127.0.0.1:{self.srv.server_address[1]}/v1"
        self.registry = load_fixture_registry()
        # Fakes for the host facts: containers never running, memory flat.
        self.mem: dict[str, "float | None"] = {"node1": 100.0, "node2": 100.0}
        self.containers: dict[str, bool] = {"node1": False, "node2": False}
        self.drain_calls: list[float] = []

    def write_start(self, body: str) -> None:
        p = self.dir / "start.sh"
        p.write_text(body)
        p.chmod(0o755)

    def write_stop(self, body: str = "#!/usr/bin/env bash\nexit 0\n") -> None:
        p = self.dir / "stop.sh"
        p.write_text(body)
        p.chmod(0o755)

    def make(self, **kw) -> GxMaxLifecycle:
        params = dict(
            api_base=self.api_base,
            model_id="DeepSeek-v4.1-Flash-EXL3",
            registry=self.registry,
            idle_ttl=0,
            acquire_timeout=20,
            read_mem_gib=lambda node: self.mem.get(node),
            container_running=lambda node: self.containers.get(node, False),
            drain_hook=self._drain_hook,
            settle_seconds=1.0,
            mem_return_wait_s=1.0,
        )
        params.update(kw)
        lc = GxMaxLifecycle(self.dir, **params)
        self.addCleanup(lc.shutdown)
        return lc

    def _drain_hook(self, seconds: float) -> int:
        self.drain_calls.append(seconds)
        return 0

    def go_healthy_after(self, delay: float) -> None:
        def flip():
            time.sleep(delay)
            _StubUpstream.healthy = True
        threading.Thread(target=flip, daemon=True).start()


class TestEnvOverlay(unittest.TestCase):
    """The profile overlay is a pure function of registry facts."""

    def setUp(self):
        self.reg = load_fixture_registry()

    def test_overlay_carries_profile_and_model_facts(self):
        profile = self.reg.profile("fast")
        model = self.reg.production_model()
        runtime = self.reg.runtime("mia-dsv41")
        env = build_env_overlay(profile, model, runtime)
        self.assertEqual(env["SERVED_MODEL_NAME"], "DeepSeek-v4.1-Flash-EXL3")
        self.assertEqual(env["MAX_NUM_SEQS"], "1")
        self.assertEqual(env["MAX_MODEL_LEN"], "600000")
        self.assertEqual(env["SPEC_METHOD"], "dspark")
        self.assertEqual(env["DSPARK_TOKENS"], "3")
        self.assertEqual(env["MODEL_HOST"], model.path)
        self.assertEqual(env["ENGRAM_DIR"], model.engram_dir)
        self.assertEqual(env["NCCL_IB_GID_INDEX"], "3")
        self.assertEqual(env["WEIGHT_SYNC"], "rsync")

    def test_serving_notes_become_kv_env(self):
        model = self.reg.production_model()
        self.assertIn("gpu_mem_util", model.serving_notes)
        env = build_env_overlay(self.reg.profile("balanced"), model,
                                self.reg.runtime("mia-dsv41"))
        self.assertEqual(env["GPU_MEM_UTIL"], "0.85")
        self.assertEqual(env["KV_CACHE_MEMORY_BYTES"], "1073741824")
        self.assertEqual(env["MAX_NUM_BATCHED_TOKENS"], "2048")
        self.assertEqual(env["DSV41_SPARSE_INDEXER_MAX_LOGITS_MB"], "256")

    def test_swarm_has_no_dspark_tokens(self):
        env = build_env_overlay(self.reg.profile("swarm"), self.reg.production_model(),
                                self.reg.runtime("mia-dsv41"))
        self.assertEqual(env["SPEC_METHOD"], "none")
        self.assertNotIn("DSPARK_TOKENS", env)


class TestReadyProbe(unittest.TestCase):
    def test_probe_against_the_real_stub(self):
        pass  # exercised throughout LifecycleTestBase; see TestAcquire


class TestAcquire(LifecycleTestBase):
    def test_starts_down_when_engine_absent(self):
        self.write_start("#!/usr/bin/env bash\nexit 1\n")
        self.write_stop()
        lc = self.make()
        self.assertIs(lc.status().state, State.DOWN)

    def test_adopts_already_running_engine(self):
        _StubUpstream.healthy = True
        self.write_start("#!/usr/bin/env bash\nexit 1\n")
        self.write_stop()
        lc = self.make()
        self.assertIs(lc.status().state, State.READY)

    def test_successful_acquisition_with_real_readiness_probe(self):
        # The fake start script flips the stub healthy then exits 0; READY
        # requires health AND the model id AND the 17*19=323 completion.
        self.write_start("#!/usr/bin/env bash\nsleep 0.3\nexit 0\n")
        self.write_stop()
        self.go_healthy_after(0.4)
        lc = self.make()
        lc.acquire(timeout=15)
        self.assertIs(lc.status().state, State.READY)
        self.assertEqual(lc.status().profile, "balanced")

    def test_health_ok_but_wrong_model_id_is_not_ready(self):
        _StubUpstream.model_id = "something-else"
        try:
            self.write_start("#!/usr/bin/env bash\nexit 0\n")
            self.write_stop()
            _StubUpstream.healthy = True
            lc = self.make()
            with self.assertRaises(AcquisitionError) as ctx:
                lc.acquire(timeout=12)
            self.assertIn("readiness probe", str(ctx.exception))
            self.assertIs(lc.status().state, State.DOWN)
        finally:
            _StubUpstream.model_id = "DeepSeek-v4.1-Flash-EXL3"

    def test_failed_acquisition_raises_and_never_downgrades(self):
        self.write_start("#!/usr/bin/env bash\necho 'boom: rank1 died' >&2\nexit 3\n")
        self.write_stop()
        lc = self.make()
        with self.assertRaises(AcquisitionError) as ctx:
            lc.acquire(timeout=12)
        self.assertIn("boom", str(ctx.exception))
        self.assertIs(lc.status().state, State.DOWN)

    def test_failed_acquisition_unwinds_via_stop_sh_force(self):
        # The kit can fail AFTER launching a container; stop.sh --force must
        # run on every failure path.
        marker = self.dir / "container-started"
        self.write_start(f"#!/usr/bin/env bash\ntouch {marker}\necho 'boom: health never answered' >&2\nexit 1\n")
        stop_args = self.dir / "stop-args"
        self.write_stop(f"#!/usr/bin/env bash\necho \"$@\" > {stop_args}\nrm -f {marker}\nexit 0\n")
        lc = self.make()
        with self.assertRaises(AcquisitionError):
            lc.acquire(timeout=12)
        self.assertIn("--force", stop_args.read_text())
        self.assertFalse(marker.exists(), "the partially-started container was not unwound")

    def test_cleanup_failure_is_appended_not_swallowed(self):
        self.write_start("#!/usr/bin/env bash\necho 'boom: original failure' >&2\nexit 1\n")
        self.write_stop("#!/usr/bin/env bash\necho 'stop script itself is broken' >&2\nexit 9\n")
        lc = self.make()
        with self.assertRaises(AcquisitionError) as ctx:
            lc.acquire(timeout=12)
        msg = str(ctx.exception)
        self.assertIn("boom: original failure", msg)
        self.assertIn("cleanup after failure also failed", msg)

    def test_concurrent_acquire_starts_script_once(self):
        marker = self.dir / "invocations"
        self.write_start(f"#!/usr/bin/env bash\necho x >> {marker}\nsleep 0.8\nexit 0\n")
        self.write_stop()
        self.go_healthy_after(1.0)
        lc = self.make()
        errors: list[Exception] = []

        def worker():
            try:
                lc.acquire(timeout=18)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(25)
        self.assertEqual(errors, [], f"acquire raised: {errors}")
        count = marker.read_text().count("x") if marker.exists() else 0
        self.assertEqual(count, 1, f"start script ran {count} times; must be serialised to 1")

    def test_start_script_receives_the_profile_overlay(self):
        envfile = self.dir / "start-env"
        self.write_start(f"#!/usr/bin/env bash\nenv | grep -E '^(MAX_NUM_SEQS|SPEC_METHOD|MODEL_HOST|NCCL_IB_GID_INDEX|SERVED_MODEL_NAME)=' > {envfile}\nexit 1\n")
        self.write_stop()
        lc = self.make()
        with self.assertRaises(AcquisitionError):
            lc.acquire(profile_name="fast", timeout=12)
        env = dict(line.split("=", 1) for line in envfile.read_text().splitlines())
        self.assertEqual(env["MAX_NUM_SEQS"], "1")
        self.assertEqual(env["SPEC_METHOD"], "dspark")
        self.assertEqual(env["NCCL_IB_GID_INDEX"], "3")
        self.assertEqual(env["SERVED_MODEL_NAME"], "DeepSeek-v4.1-Flash-EXL3")

    def test_unknown_profile_is_a_clear_error(self):
        self.write_start("#!/usr/bin/env bash\nexit 0\n")
        self.write_stop()
        lc = self.make()
        with self.assertRaises(Exception) as ctx:
            lc.acquire(profile_name="turbo", timeout=5)
        self.assertIn("known profiles", str(ctx.exception))


class TestProfileSwitch(LifecycleTestBase):
    def test_switch_while_ready_is_drain_stop_start(self):
        _StubUpstream.healthy = True
        self.write_start("#!/usr/bin/env bash\nexit 0\n")
        stop_log = self.dir / "stop-log"
        self.write_stop(f"#!/usr/bin/env bash\necho stop >> {stop_log}\nexit 0\n")
        lc = self.make()
        self.assertIs(lc.status().state, State.READY)
        # The engine "goes down" while the stop script runs (the stub models
        # the real teardown), then comes back for the new profile.
        _StubUpstream.healthy = False
        self.go_healthy_after(0.3)
        lc.acquire(profile_name="deep", timeout=15)
        st = lc.status()
        self.assertIs(st.state, State.READY)
        self.assertEqual(st.profile, "deep")
        self.assertEqual(stop_log.read_text().count("stop"), 1, "a switch must cycle the engine exactly once")
        self.assertEqual(self.drain_calls, [300.0], "the drain hook ran before the stop")


class TestRelease(LifecycleTestBase):
    def test_release_returns_to_down(self):
        _StubUpstream.healthy = True
        self.write_start("#!/usr/bin/env bash\nexit 0\n")
        self.write_stop()
        lc = self.make()
        self.assertIs(lc.status().state, State.READY)
        _StubUpstream.healthy = False
        lc.release()
        self.assertIs(lc.status().state, State.DOWN)

    def test_release_when_down_is_a_noop(self):
        self.write_start("#!/usr/bin/env bash\nexit 1\n")
        self.write_stop()
        lc = self.make()
        lc.release()
        self.assertIs(lc.status().state, State.DOWN)

    def test_release_verifies_memory_return_on_both_nodes(self):
        # A REAL acquire first, so the pre-start memory floor is recorded.
        self.write_start("#!/usr/bin/env bash\nexit 0\n")
        self.write_stop()
        self.go_healthy_after(0.2)
        lc = self.make()
        lc.acquire(timeout=15)
        self.assertIs(lc.status().state, State.READY)
        # The engine leaves 40 GiB resident on node2 after the stop script.
        _StubUpstream.healthy = False
        self.mem["node2"] = 60.0
        lc.release()
        job = lc.events()["history"][-1]
        self.assertEqual(job["outcome"], "release_warning")
        self.assertIn("memory did not return", job["error"])

    def test_release_waits_for_memory_return_when_slow(self):
        self.write_start("#!/usr/bin/env bash\nexit 0\n")
        self.write_stop()
        self.go_healthy_after(0.2)
        lc = self.make(mem_return_wait_s=10.0)
        lc.acquire(timeout=15)
        self.assertIs(lc.status().state, State.READY)
        _StubUpstream.healthy = False
        # Node 2 reclaims within the wait window -> a clean release.
        self.mem["node2"] = 55.0

        def reclaim():
            time.sleep(1.0)
            self.mem["node2"] = 100.0
        threading.Thread(target=reclaim, daemon=True).start()
        lc.release()
        job = lc.events()["history"][-1]
        self.assertEqual(job["outcome"], "released")


class TestReconcile(LifecycleTestBase):
    """The engine can be torn down outside this process; state must follow."""

    def test_ready_demotes_to_down_when_engine_vanishes(self):
        _StubUpstream.healthy = True
        self.write_start("#!/usr/bin/env bash\nexit 1\n")
        self.write_stop()
        lc = self.make()
        self.assertIs(lc.status().state, State.READY)
        _StubUpstream.healthy = False
        lc._RECONCILE_INTERVAL = 0.0  # do not wait out the probe throttle
        self.assertIs(lc.status().state, State.DOWN)
        self.assertFalse(lc.is_ready())

    def test_reconcile_does_not_disturb_a_healthy_engine(self):
        _StubUpstream.healthy = True
        lc = self.make()
        lc._RECONCILE_INTERVAL = 0.0
        for _ in range(3):
            self.assertIs(lc.status().state, State.READY)

    def test_down_is_promoted_when_engine_started_externally(self):
        self.write_start("#!/usr/bin/env bash\nexit 1\n")
        self.write_stop()
        lc = self.make()
        lc._RECONCILE_INTERVAL = 0.0
        self.assertIs(lc.status().state, State.DOWN)
        _StubUpstream.healthy = True
        self.assertIs(lc.status().state, State.READY)

    def test_down_stays_down_while_engine_absent(self):
        lc = self.make()
        lc._RECONCILE_INTERVAL = 0.0
        for _ in range(3):
            self.assertIs(lc.status().state, State.DOWN)


class TestIdleReaper(LifecycleTestBase):
    def test_status_reports_idle_seconds_and_profile(self):
        _StubUpstream.healthy = True
        self.write_start("#!/usr/bin/env bash\nexit 0\n")
        self.write_stop()
        lc = self.make()
        lc.mark_used()
        st = lc.status()
        d = st.as_dict()
        self.assertIsNotNone(d["idle_seconds"])
        self.assertIn("profile", d)

    def test_reaper_never_releases_while_a_request_is_in_flight(self):
        _StubUpstream.healthy = True
        lc = self.make(idle_ttl=1)
        lc.begin_use()
        lc._last_used = time.time() - 100
        st = lc.status().as_dict()
        self.assertEqual(st["in_flight"], 1)
        self.assertEqual(st["ttl_remaining_seconds"], 1, "the TTL does not run during a request")
        lc.end_use()
        st = lc.status().as_dict()
        self.assertEqual(st["in_flight"], 0)
        self.assertLessEqual(st["ttl_remaining_seconds"], 1)


class TestNoBootAutoload(unittest.TestCase):
    def test_construction_never_runs_the_start_script(self):
        # NO auto-start at boot: constructing the lifecycle with the engine
        # absent must leave everything untouched (no script execution).
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "start.sh").write_text("#!/usr/bin/env bash\ntouch started-marker\nexit 1\n")
            (d / "start.sh").chmod(0o755)
            (d / "stop.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
            (d / "stop.sh").chmod(0o755)
            reg = load_fixture_registry()
            lc = GxMaxLifecycle(
                d, api_base="http://127.0.0.1:1/v1", model_id="m", registry=reg, idle_ttl=0,
            )
            lc.shutdown()
            self.assertFalse((d / "started-marker").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
