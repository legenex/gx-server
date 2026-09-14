"""Tests for the gx-max lifecycle state machine.

These use a temporary directory containing fake gx-max-start.sh / gx-max-stop.sh
scripts plus a stub health endpoint, so the real cluster is never touched.
"""

from __future__ import annotations

import http.server
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gx_orchestrator.lifecycle import AcquisitionError, GxMaxLifecycle, State  # noqa: E402


class _StubHealth(http.server.BaseHTTPRequestHandler):
    healthy = False

    def do_GET(self):  # noqa: N802
        self.send_response(200 if type(self).healthy else 503)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):  # noqa: A003
        pass


class LifecycleTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        _StubHealth.healthy = False
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _StubHealth)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.health_url = f"http://127.0.0.1:{self.port}/health"

    def tearDown(self) -> None:
        self.srv.shutdown()
        self.tmp.cleanup()

    def write_start(self, body: str) -> None:
        p = self.dir / "gx-max-start.sh"
        p.write_text(body)
        p.chmod(0o755)

    def write_stop(self, body: str = "#!/usr/bin/env bash\nexit 0\n") -> None:
        p = self.dir / "gx-max-stop.sh"
        p.write_text(body)
        p.chmod(0o755)


class TestAcquire(LifecycleTestBase):
    def test_starts_down_when_engine_absent(self):
        self.write_start("#!/usr/bin/env bash\nexit 1\n")
        self.write_stop()
        lc = GxMaxLifecycle(self.dir, self.health_url, idle_ttl=0)
        self.assertIs(lc.status().state, State.DOWN)
        lc.shutdown()

    def test_adopts_already_running_engine(self):
        _StubHealth.healthy = True
        self.write_start("#!/usr/bin/env bash\nexit 0\n")
        self.write_stop()
        lc = GxMaxLifecycle(self.dir, self.health_url, idle_ttl=0)
        self.assertIs(lc.status().state, State.READY)
        lc.shutdown()

    def test_successful_acquisition(self):
        # The fake start script flips the stub health endpoint to healthy.
        self.write_start(
            "#!/usr/bin/env bash\n"
            f"curl -s -m 2 -X GET http://127.0.0.1:{self.port}/ >/dev/null 2>&1 || true\n"
            "exit 0\n"
        )
        self.write_stop()
        lc = GxMaxLifecycle(self.dir, self.health_url, idle_ttl=0, acquire_timeout=20)

        def flip():
            time.sleep(0.4)
            _StubHealth.healthy = True

        threading.Thread(target=flip, daemon=True).start()
        time.sleep(0.6)
        lc.acquire(timeout=15)
        self.assertIs(lc.status().state, State.READY)
        lc.shutdown()

    def test_failed_acquisition_raises_and_never_downgrades(self):
        self.write_start("#!/usr/bin/env bash\necho 'boom: rank1 died' >&2\nexit 3\n")
        self.write_stop()
        lc = GxMaxLifecycle(self.dir, self.health_url, idle_ttl=0, acquire_timeout=15)
        with self.assertRaises(AcquisitionError) as ctx:
            lc.acquire(timeout=12)
        self.assertIn("boom", str(ctx.exception))
        self.assertIs(lc.status().state, State.DOWN)
        lc.shutdown()

    def test_concurrent_acquire_starts_script_once(self):
        marker = self.dir / "invocations"
        self.write_start(
            "#!/usr/bin/env bash\n"
            f"echo x >> {marker}\n"
            "sleep 1\n"
            "exit 0\n"
        )
        self.write_stop()
        lc = GxMaxLifecycle(self.dir, self.health_url, idle_ttl=0, acquire_timeout=20)

        def flip():
            time.sleep(1.2)
            _StubHealth.healthy = True

        threading.Thread(target=flip, daemon=True).start()

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
        lc.shutdown()


class TestRelease(LifecycleTestBase):
    def test_release_returns_to_down(self):
        _StubHealth.healthy = True
        self.write_start("#!/usr/bin/env bash\nexit 0\n")
        self.write_stop("#!/usr/bin/env bash\nexit 0\n")
        lc = GxMaxLifecycle(self.dir, self.health_url, idle_ttl=0)
        self.assertIs(lc.status().state, State.READY)
        lc.release()
        self.assertIs(lc.status().state, State.DOWN)
        lc.shutdown()

    def test_release_when_down_is_a_noop(self):
        self.write_start("#!/usr/bin/env bash\nexit 0\n")
        self.write_stop()
        lc = GxMaxLifecycle(self.dir, self.health_url, idle_ttl=0)
        lc.release()
        self.assertIs(lc.status().state, State.DOWN)
        lc.shutdown()


class TestIdleReaper(LifecycleTestBase):
    def test_status_reports_idle_seconds(self):
        _StubHealth.healthy = True
        self.write_start("#!/usr/bin/env bash\nexit 0\n")
        self.write_stop()
        lc = GxMaxLifecycle(self.dir, self.health_url, idle_ttl=0)
        lc.mark_used()
        st = lc.status()
        self.assertIsNotNone(st.idle_seconds if hasattr(st, "idle_seconds") else st.last_used)
        self.assertIn("idle_seconds", st.as_dict())
        lc.shutdown()


if __name__ == "__main__":
    unittest.main(verbosity=2)
