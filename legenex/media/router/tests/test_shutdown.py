"""SIGTERM stops the router promptly (D-039).

The old handler called server.shutdown() on the thread running
serve_forever(), which deadlocks: every `docker stop` waited out the full
60-second grace period and ended in SIGKILL, adding a minute to each gx-max
drain.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROUTER = Path(__file__).resolve().parents[1]
WORKFLOWS = ROUTER.parent / "workflows"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestSigterm(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_sigterm_exits_within_seconds(self):
        port = _free_port()
        env = {
            **os.environ,
            "PYTHONPATH": str(ROUTER),
            "GX_MEDIA_BIND": "127.0.0.1",
            "GX_MEDIA_PORT": str(port),
            "GX_MEDIA_WORKFLOW_DIR": str(WORKFLOWS),
            "GX_COMFY_URL": "http://127.0.0.1:9",
            "GX_MEDIA_API_KEY": "test-key",
            "GX_MEDIA_INPUT_DIR": self.tmp.name,
            "GX_MEDIA_GUARD_DIR": "",
            "GX_MEDIA_MUSIC_URL": "",
        }
        proc = subprocess.Popen([sys.executable, "-m", "gx_media_router"], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                        break
                except OSError:
                    if proc.poll() is not None:
                        self.fail(proc.stdout.read().decode()[-2000:])
                    time.sleep(0.2)
            else:
                self.fail("router did not start listening")
            t0 = time.monotonic()
            proc.send_signal(signal.SIGTERM)
            rc = proc.wait(timeout=15)
            self.assertLess(time.monotonic() - t0, 5)
            self.assertEqual(rc, 0)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stdout.close()


if __name__ == "__main__":
    unittest.main()
