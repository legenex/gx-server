"""Tests for the READ-ONLY observability added to the gx-max lifecycle:
phase tracking from script markers, the event buffer, the job history and
the /lifecycle/gx-max/events endpoint. None of it may change a transition.
"""

from __future__ import annotations

import http.client
import json
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gx_orchestrator.lifecycle import (  # noqa: E402
    PHASE_FAILED,
    PHASE_IDLE,
    PHASE_SERVING,
    AcquisitionError,
    GxMaxLifecycle,
    State,
)
from tests.test_lifecycle import LifecycleTestBase, _StubHealth  # noqa: E402

_START_OK = """#!/usr/bin/env bash
log() { printf '[%s] %s\\n' "$(date -Is)" "$*" >&2; }
log "=== gx-max preflight ==="
log "=== draining conflicting GPU work ==="
log "=== cluster-takeover admission (node1 + node2) ==="
log "=== starting rank1 on node2 ==="
log "=== starting rank0 on node1 ==="
log "=== waiting for gx-max to become healthy (timeout 1800s) ==="
touch "$(dirname "$0")/healthy-now"
sleep 0.5
log "=== gx-max READY on http://127.0.0.1:30000/v1 after 512s ==="
log "ENGINE_PHASES container_to_args=16.0 weights=370.9 cuda_graphs=73.6 health_ready=545.0"
exit 0
"""

_STOP_OK = """#!/usr/bin/env bash
log() { printf '[%s] %s\\n' "$(date -Is)" "$*" >&2; }
log "stopping rank0 on node1"
log "MemAvailable after release: node1=110GiB node2=112GiB"
log "restoring normal single-node workloads"
log "gx-max released; both nodes are back to normal operating state"
exit 0
"""


class TestPhases(LifecycleTestBase):
    def _flip_when_marker(self) -> None:
        marker = self.dir / "healthy-now"

        def watch() -> None:
            deadline = time.time() + 15
            while time.time() < deadline:
                if marker.exists():
                    _StubHealth.healthy = True
                    return
                time.sleep(0.05)

        threading.Thread(target=watch, daemon=True).start()

    def test_acquire_records_phases_history_and_startup_seconds(self):
        self.write_start(_START_OK)
        self.write_stop(_STOP_OK)
        hist = self.dir / "state" / "gx-max-history.json"
        evlog = self.dir / "lifecycle.log"
        lc = GxMaxLifecycle(self.dir, self.health_url, idle_ttl=0, acquire_timeout=20,
                            events_log=evlog, history_path=hist)
        self.assertEqual(lc.status().phase, PHASE_IDLE)
        self._flip_when_marker()
        lc.acquire(timeout=18)

        st = lc.status()
        self.assertIs(st.state, State.READY)
        self.assertEqual(st.phase, PHASE_SERVING)
        self.assertEqual(st.last_startup_seconds, 512)
        self.assertEqual(st.as_dict()["last_startup_seconds"], 512)

        ev = lc.events()
        self.assertIsNone(ev["active_job"])
        job = ev["history"][-1]
        self.assertEqual(job["kind"], "acquire")
        self.assertEqual(job["outcome"], "ready")
        phases = [p["phase"] for p in job["phases"]]
        self.assertEqual(
            phases,
            ["preflight", "draining", "admission", "loading_rank1", "loading_rank0",
             "warming", "ready", "serving"],
        )
        # D-039: every phase has a duration and the engine breakdown is kept.
        self.assertTrue(all(isinstance(p.get("seconds"), float) and p["seconds"] >= 0 for p in job["phases"]))
        self.assertEqual(job["engine_phases"]["weights"], 370.9)
        self.assertEqual(job["engine_phases"]["health_ready"], 545.0)
        lines = [e["line"] for e in ev["events"]]
        self.assertTrue(any("starting rank1" in line for line in lines))
        self.assertIn("starting rank1 on node2", evlog.read_text())

        # history survives a restart of the orchestrator
        lc.shutdown()
        lc2 = GxMaxLifecycle(self.dir, self.health_url, idle_ttl=0, history_path=hist)
        self.assertEqual(lc2.status().last_startup_seconds, 512)
        lc2.shutdown()

    def test_rank_order_rank1_before_rank0(self):
        self.write_start(_START_OK)
        self.write_stop(_STOP_OK)
        lc = GxMaxLifecycle(self.dir, self.health_url, idle_ttl=0, acquire_timeout=20)
        self._flip_when_marker()
        lc.acquire(timeout=18)
        phases = [p["phase"] for p in lc.events()["history"][-1]["phases"]]
        self.assertLess(phases.index("loading_rank1"), phases.index("loading_rank0"))
        lc.shutdown()

    def test_failed_acquire_marks_failed_phase_and_keeps_error(self):
        self.write_start("#!/usr/bin/env bash\necho '=== gx-max preflight ===' >&2\n"
                         "echo 'FATAL: node1 admission REFUSED gx-max-rank0: swap' >&2\nexit 1\n")
        self.write_stop()
        lc = GxMaxLifecycle(self.dir, self.health_url, idle_ttl=0, acquire_timeout=15)
        with self.assertRaises(AcquisitionError) as ctx:
            lc.acquire(timeout=12)
        # stderr is merged into the error tail exactly as before
        self.assertIn("admission REFUSED", str(ctx.exception))
        st = lc.status()
        self.assertIs(st.state, State.DOWN)
        self.assertEqual(st.phase, PHASE_FAILED)
        job = lc.events()["history"][-1]
        self.assertEqual(job["outcome"], "failed")
        self.assertIn("admission REFUSED", job["error"])
        lc.shutdown()

    def test_release_records_phases(self):
        _StubHealth.healthy = True
        self.write_start("#!/usr/bin/env bash\nexit 0\n")
        self.write_stop(_STOP_OK)
        lc = GxMaxLifecycle(self.dir, self.health_url, idle_ttl=0)
        self.assertEqual(lc.status().phase, PHASE_SERVING)  # adopted
        _StubHealth.healthy = False
        lc.release()
        st = lc.status()
        self.assertIs(st.state, State.DOWN)
        self.assertEqual(st.phase, PHASE_IDLE)
        job = lc.events()["history"][-1]
        self.assertEqual(job["kind"], "release")
        self.assertEqual(job["outcome"], "released")
        self.assertEqual(
            [p["phase"] for p in job["phases"]],
            ["stopping_ranks", "memory_recovery", "restoring", "released", PHASE_IDLE],
        )
        lc.shutdown()

    def test_forced_release_is_labelled(self):
        _StubHealth.healthy = True
        self.write_start("#!/usr/bin/env bash\nexit 0\n")
        args_file = self.dir / "args"
        self.write_stop(f'#!/usr/bin/env bash\necho "$@" > {args_file}\nexit 0\n')
        lc = GxMaxLifecycle(self.dir, self.health_url, idle_ttl=0)
        _StubHealth.healthy = False
        lc.release(force=True)
        self.assertEqual(lc.events()["history"][-1]["kind"], "release_forced")
        self.assertIn("--force", args_file.read_text())
        lc.shutdown()

    def test_events_after_and_limit(self):
        self.write_start("#!/usr/bin/env bash\nfor i in $(seq 1 30); do echo line-$i; done\nexit 1\n")
        self.write_stop()
        lc = GxMaxLifecycle(self.dir, self.health_url, idle_ttl=0, acquire_timeout=15)
        with self.assertRaises(AcquisitionError):
            lc.acquire(timeout=12)
        ev = lc.events(limit=5)
        self.assertEqual(len(ev["events"]), 5)
        after = lc.events(after=ev["seq"])
        self.assertEqual(after["events"], [])
        self.assertEqual(lc.events(limit=100000)["events"][0]["seq"], 1)
        lc.shutdown()

    def test_timeout_kills_script_and_reports(self):
        self.write_start("#!/usr/bin/env bash\necho started >&2\nsleep 30\n")
        self.write_stop()
        lc = GxMaxLifecycle(self.dir, self.health_url, idle_ttl=0, acquire_timeout=2)
        t0 = time.time()
        with self.assertRaises(AcquisitionError) as ctx:
            lc.acquire(timeout=10)
        self.assertLess(time.time() - t0, 10)
        self.assertIn("exceeded", str(ctx.exception))
        lc.shutdown()

    def test_corrupt_history_file_is_ignored(self):
        hist = self.dir / "h.json"
        hist.write_text("{not json")
        self.write_start("#!/usr/bin/env bash\nexit 1\n")
        self.write_stop()
        lc = GxMaxLifecycle(self.dir, self.health_url, idle_ttl=0, history_path=hist)
        self.assertEqual(lc.events()["history"], [])
        lc.shutdown()


class TestEventsEndpoint(LifecycleTestBase):
    def _server(self, lc):
        from gx_orchestrator.config import Config
        from gx_orchestrator.health import TierHealth
        from gx_orchestrator.server import Handler
        import http.server

        cfg = Config(hosts=("127.0.0.1",), port=0)
        handler = type("H", (Handler,), {"cfg": cfg, "lifecycle": lc, "health": TierHealth(cfg)})
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        return srv.server_address[1]

    def _get(self, port, path):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", path)
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read() or b"{}")

    def test_events_endpoint_is_read_only_and_validates(self):
        self.write_start("#!/usr/bin/env bash\nexit 1\n")
        self.write_stop()
        lc = GxMaxLifecycle(self.dir, self.health_url, idle_ttl=0)
        port = self._server(lc)
        status, body = self._get(port, "/lifecycle/gx-max/events?limit=10")
        self.assertEqual(status, 200)
        self.assertEqual(set(body), {"seq", "events", "active_job", "history"})
        self.assertIs(lc.status().state, State.DOWN)  # nothing was started
        status, body = self._get(port, "/lifecycle/gx-max/events?after=abc")
        self.assertEqual(status, 400)
        status, body = self._get(port, "/lifecycle/gx-max/status")
        self.assertEqual(status, 200)
        self.assertIn("phase", body)
        self.assertIn("last_startup_seconds", body)
        lc.shutdown()


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestInFlightKeepWarm(LifecycleTestBase):
    def test_reaper_never_releases_while_a_request_is_in_flight(self):
        self.write_start(_START_OK)
        self.write_stop(_STOP_OK)
        lc = GxMaxLifecycle(self.dir, self.health_url, idle_ttl=1, acquire_timeout=20)
        _StubHealth.healthy = True
        lc._state = State.READY
        lc.begin_use()
        lc._last_used = time.time() - 100
        st = lc.status().as_dict()
        self.assertEqual(st["in_flight"], 1)
        self.assertEqual(st["ttl_remaining_seconds"], 1, "the TTL does not run during a request")
        lc.end_use()
        st = lc.status().as_dict()
        self.assertEqual(st["in_flight"], 0)
        self.assertLessEqual(st["ttl_remaining_seconds"], 1)
        self.assertGreaterEqual(st["idle_seconds"], 0)
        lc.shutdown()
