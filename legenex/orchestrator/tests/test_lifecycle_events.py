"""Tests for the READ-ONLY observability of the gx-max lifecycle: phase
tracking from the wrapper-script markers, the event buffer, the job history
and the /lifecycle/gx-max/events endpoint. None of it may change a
transition. Markers now match the V4.1 thin wrappers around the Mia kit.
"""

from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from gx_orchestrator.lifecycle import (  # noqa: E402
    PHASE_FAILED,
    PHASE_IDLE,
    PHASE_SERVING,
    AcquisitionError,
    GxMaxLifecycle,
    State,
)
from tests.test_lifecycle import LifecycleTestBase, _StubUpstream  # noqa: E402

_START_OK = """#!/usr/bin/env bash
log() { printf '[%s] %s\\n' "$(date -Is)" "$*" >&2; }
log "=== gx-max preflight ==="
log "=== profile overlay ==="
log "=== starting the Mia kit (dsv41-exl3-head + dsv41-exl3-worker) ==="
log "=== waiting for the model to become healthy ==="
touch "$(dirname "$0")/healthy-now"
sleep 0.3
log "=== gx-max READY on http://127.0.0.1:8888/v1 after 512s ==="
exit 0
"""

_STOP_OK = """#!/usr/bin/env bash
log() { printf '[%s] %s\\n' "$(date -Is)" "$*" >&2; }
log "draining: waiting for in-flight requests"
log "stopping the Mia containers (dsv41-exl3-head, dsv41-exl3-worker)"
log "MemAvailable after release: node1=110GiB node2=112GiB"
log "gx-max released; both nodes are back to normal"
exit 0
"""


class TestPhases(LifecycleTestBase):
    def _flip_when_marker(self) -> None:
        marker = self.dir / "healthy-now"

        def watch() -> None:
            deadline = time.time() + 20
            while time.time() < deadline:
                if marker.exists():
                    _StubUpstream.healthy = True
                    return
                time.sleep(0.05)

        threading.Thread(target=watch, daemon=True).start()

    def test_acquire_records_phases_history_and_startup_seconds(self):
        self.write_start(_START_OK)
        self.write_stop(_STOP_OK)
        hist = self.dir / "state" / "gx-max-history.json"
        evlog = self.dir / "lifecycle.log"
        lc = self.make(events_log=evlog, history_path=hist)
        self.assertEqual(lc.status().phase, PHASE_IDLE)
        self._flip_when_marker()
        lc.acquire(timeout=18)

        st = lc.status()
        self.assertIs(st.state, State.READY)
        self.assertEqual(st.phase, PHASE_SERVING)
        self.assertEqual(st.last_startup_seconds, 512)
        self.assertEqual(st.as_dict()["last_startup_seconds"], 512)
        self.assertEqual(st.profile, "balanced")

        ev = lc.events()
        self.assertIsNone(ev["active_job"])
        job = ev["history"][-1]
        self.assertEqual(job["kind"], "acquire")
        self.assertEqual(job["outcome"], "ready")
        self.assertEqual(
            [p["phase"] for p in job["phases"]],
            ["preflight", "overlay", "loading", "warming", "ready", "serving"],
        )
        # Every phase has a duration.
        self.assertTrue(all(isinstance(p.get("seconds"), float) and p["seconds"] >= 0
                             for p in job["phases"]))
        lines = [e["line"] for e in ev["events"]]
        self.assertTrue(any("starting the Mia kit" in line for line in lines))
        self.assertIn("starting the Mia kit", evlog.read_text())

        # history survives a restart of the orchestrator
        lc.shutdown()
        lc2 = self.make(history_path=hist)
        self.assertEqual(lc2.status().last_startup_seconds, 512)
        lc2.shutdown()

    def test_failed_acquire_marks_failed_phase_and_keeps_error(self):
        self.write_start("#!/usr/bin/env bash\necho '=== gx-max preflight ===' >&2\n"
                         "echo 'FATAL: node1 admission REFUSED gx-max: swap' >&2\nexit 1\n")
        self.write_stop()
        lc = self.make()
        with self.assertRaises(AcquisitionError) as ctx:
            lc.acquire(timeout=12)
        self.assertIn("admission REFUSED", str(ctx.exception))
        st = lc.status()
        self.assertIs(st.state, State.DOWN)
        self.assertEqual(st.phase, PHASE_FAILED)
        job = lc.events()["history"][-1]
        self.assertEqual(job["outcome"], "failed")
        self.assertIn("admission REFUSED", job["error"])

    def test_release_records_phases(self):
        _StubUpstream.healthy = True
        self.write_start("#!/usr/bin/env bash\nexit 0\n")
        self.write_stop(_STOP_OK)
        lc = self.make()
        self.assertEqual(lc.status().phase, PHASE_SERVING)  # adopted
        _StubUpstream.healthy = False
        lc.release()
        st = lc.status()
        self.assertIs(st.state, State.DOWN)
        self.assertEqual(st.phase, PHASE_IDLE)
        job = lc.events()["history"][-1]
        self.assertEqual(job["kind"], "release")
        self.assertEqual(job["outcome"], "released")
        self.assertEqual(
            [p["phase"] for p in job["phases"]],
            ["draining_requests", "stopping_containers", "memory_recovery", "released", PHASE_IDLE],
        )

    def test_forced_release_is_labelled_and_passes_force(self):
        _StubUpstream.healthy = True
        self.write_start("#!/usr/bin/env bash\nexit 0\n")
        args_file = self.dir / "args"
        self.write_stop(f'#!/usr/bin/env bash\necho "$@" > {args_file}\nexit 0\n')
        lc = self.make()
        _StubUpstream.healthy = False
        lc.release(force=True)
        self.assertEqual(lc.events()["history"][-1]["kind"], "release_forced")
        self.assertIn("--force", args_file.read_text())

    def test_events_after_and_limit(self):
        self.write_start("#!/usr/bin/env bash\nfor i in $(seq 1 30); do echo line-$i; done\nexit 1\n")
        self.write_stop()
        lc = self.make()
        with self.assertRaises(AcquisitionError):
            lc.acquire(timeout=12)
        ev = lc.events(limit=5)
        self.assertEqual(len(ev["events"]), 5)
        after = lc.events(after=ev["seq"])
        self.assertEqual(after["events"], [])
        self.assertEqual(lc.events(limit=100000)["events"][0]["seq"], 1)

    def test_timeout_kills_script_and_reports(self):
        self.write_start("#!/usr/bin/env bash\necho started >&2\nsleep 30\n")
        self.write_stop()
        lc = self.make(acquire_timeout=2)
        t0 = time.time()
        with self.assertRaises(AcquisitionError) as ctx:
            lc.acquire(timeout=10)
        self.assertLess(time.time() - t0, 10)
        self.assertIn("exceeded", str(ctx.exception))

    def test_corrupt_history_file_is_ignored(self):
        hist = self.dir / "h.json"
        hist.write_text("{not json")
        self.write_start("#!/usr/bin/env bash\nexit 1\n")
        self.write_stop()
        lc = self.make(history_path=hist)
        self.assertEqual(lc.events()["history"], [])


class TestEventsEndpoint(LifecycleTestBase):
    def _server(self, lc):
        from gx_orchestrator.scheduler import Scheduler
        from gx_orchestrator.server import Handler, RoutingJournal, TextMetrics

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        scheduler = Scheduler(
            queue_path=Path(tmp.name) / "queue.json",
            history_path=Path(tmp.name) / "history.jsonl",
            capacity=2,
        )
        self.addCleanup(scheduler.shutdown)

        class KeyedConfig:
            orchestrator_key = lambda self: "test-orchestrator-key"  # noqa: E731
            gxmax_model_id = "DeepSeek-v4.1-Flash-EXL3"
            gxmax_base = "http://127.0.0.1:8888/v1"
            gateway_base = "http://127.0.0.1:4000"
            upstream_timeout = 30

        handler = type("H", (Handler,), {
            "cfg": KeyedConfig(),
            "registry": self.registry,
            "lifecycle": lc,
            "health": _FakeHealth(),
            "scheduler": scheduler,
            "journal": RoutingJournal(Path(tmp.name) / "routing.jsonl"),
            "metrics": TextMetrics(),
        })
        srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        self.addCleanup(srv.server_close)
        return srv.server_address[1]

    def _get(self, port, path):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", path, headers={"Authorization": "Bearer test-orchestrator-key"})
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read() or b"{}")

    def test_events_endpoint_is_read_only_and_validates(self):
        self.write_start("#!/usr/bin/env bash\nexit 1\n")
        self.write_stop()
        lc = self.make()
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
        self.assertIn("profile", body)

    def test_scheduler_endpoints_are_wired(self):
        self.write_start("#!/usr/bin/env bash\nexit 1\n")
        self.write_stop()
        lc = self.make()
        port = self._server(lc)
        status, body = self._get(port, "/scheduler/status")
        self.assertEqual(status, 200)
        self.assertIn("capacity", body)
        self.assertIn("records", body)
        status, body = self._get(port, "/scheduler/history?limit=5")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"data": []})

    def test_text_status_has_the_new_shape(self):
        self.write_start("#!/usr/bin/env bash\nexit 1\n")
        self.write_stop()
        lc = self.make()
        port = self._server(lc)
        status, body = self._get(port, "/text/status")
        self.assertEqual(status, 200)
        self.assertIn("model", body)
        self.assertIn("queue", body)
        self.assertEqual(body["model"]["id"], "DeepSeek-v4.1-Flash-EXL3")
        self.assertTrue(body["model"]["uncensored"])
        self.assertIn("state", body["model"])
        self.assertIn("nodes", body["model"])

    def test_models_lists_exactly_the_two_aliases(self):
        self.write_start("#!/usr/bin/env bash\nexit 1\n")
        self.write_stop()
        lc = self.make()
        port = self._server(lc)
        status, body = self._get(port, "/v1/models")
        self.assertEqual(status, 200)
        self.assertEqual([m["id"] for m in body["data"]], ["gx-max", "gx-auto"])


class _FakeHealth:
    """Static health snapshot for the endpoint tests (no live probes)."""

    def snapshot(self):
        return {
            "head": {"healthy": True, "serves_model": True, "model_id": "m",
                     "detail": ""},
            "worker": {"ssh_reachable": True, "container_running": True,
                       "container": "dsv41-exl3-worker",
                       "fabric": {"192.168.100.11": True}, "detail": ""},
            "mem": {"node1_gib": 100.0, "node2_gib": 100.0},
            "checked": time.time(),
        }

    def worker_ok(self):
        return True

    def head_status(self):
        from gx_orchestrator.health import AliasState, TierStatus

        return TierStatus(AliasState.READY, "fake", usable=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
