import io
import json
import threading
import unittest
from http.server import HTTPServer
from urllib.request import Request, urlopen

from support import TempEnv

from gx_control_ui import views
from gx_control_ui.server import App, Handler
from gx_control_ui.util import epoch


class Perf(unittest.TestCase):
    """Smoke-tests for the endpoints the UI polls: the cached views must
    render in < 150 ms so the dashboard stays fluid."""

    def test_fast_views(self):
        env = TempEnv()
        self.addCleanup(env.cleanup)
        app = App(env.cfg)
        for name in ("overview", "nodes", "cluster", "jobs", "system"):
            fn = getattr(views, name)
            fn(app)  # warm the caches
            start = epoch()
            fn(app)
            self.assertLess(epoch() - start, 0.15, f"{name} view too slow")

    def test_assets_are_served_quickly(self):
        env = TempEnv()
        self.addCleanup(env.cleanup)
        app = App(env.cfg)
        server = HTTPServer(("127.0.0.1", 0), lambda *a, **k: Handler(*a, app=app, **k))
        port = server.server_address[1]
        th = threading.Thread(target=server.serve_forever, daemon=True)
        th.start()
        self.addCleanup(server.shutdown)
        start = epoch()
        for path in ("/", "/js/app.js", "/api/overview"):
            try:
                with urlopen(Request(f"http://127.0.0.1:{port}{path}"), timeout=2) as r:
                    r.read()
            except Exception:
                pass  # 401/403 is fine: measuring latency, not status
        self.assertLess(epoch() - start, 1.0)


if __name__ == "__main__":
    unittest.main()
