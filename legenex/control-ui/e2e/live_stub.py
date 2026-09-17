"""Build V3 LIV: the REAL gx-live supervisor with a stub engine, for the E2E suite.

The browser tests exercise the whole live path — Playground tunnel, gx-live
HTTP and WebSocket API, tool bridge, session record — without a GPU, Docker or
MiniCPM-o 4.5. The engine is replaced by ``legenex/live/tests`` stub, which
speaks the real engine protocol: it answers a typed message with captions and
24 kHz PCM, and asks for a ``delegate_to_gx`` tool call when the message is
"use a tool".

    from live_stub import LiveStub
    live = LiveStub()
    ...  TempEnv(live_base=live.url, rt_live_target=f"127.0.0.1:{live.port}")
    (secrets_root / "gx-live" / "api-key").write_text(live.key)
"""

from __future__ import annotations

import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
LIVE_DIR = REPO / "legenex" / "live"
for extra in (LIVE_DIR, LIVE_DIR / "tests", REPO / "legenex" / "common"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from gxcommon import rtws  # noqa: E402
from gx_live.engine import EngineController  # noqa: E402
from gx_live.server import build_servers  # noqa: E402
from gx_live.service import LiveService  # noqa: E402
from test_gx_live import (  # noqa: E402
    FakeDocker, FakeGuard, FakePeers, StubEngineHandler, fake_key, make_config,
)


class LiveStub:
    """A real gx-live supervisor on loopback, with the stub engine."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        StubEngineHandler.key = fake_key()
        StubEngineHandler.sessions = []
        StubEngineHandler.received = []
        handler = type("E2EEngine", (StubEngineHandler,), {})
        self._engine_srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._engine_srv.daemon_threads = True
        threading.Thread(target=self._engine_srv.serve_forever, daemon=True).start()
        self.cfg = make_config(root, engine_port=self._engine_srv.server_address[1], idle_unload_s=300,
                               reconnect_grace_s=10)
        self.cfg.engine_key_file.write_text(StubEngineHandler.key)
        self.key = (root / "secrets" / "api-key").read_text().strip()
        engine = EngineController(self.cfg, docker=FakeDocker(), guard=FakeGuard(), peers=FakePeers(),
                                  mem=lambda: {"MemAvailable": 100.0, "MemTotal": 121.0, "SwapTotal": 63.0,
                                               "SwapFree": 60.0})
        self.service = LiveService(self.cfg, engine)
        self._servers = build_servers(self.service, self.key, ("127.0.0.1",), 0, rtws)
        self.port = self._servers[0].server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        for s in self._servers:
            threading.Thread(target=s.serve_forever, daemon=True).start()
        self.service.start()

    def close(self) -> None:
        self.service.stop()
        for s in self._servers:
            s.shutdown()
            s.server_close()
        self._engine_srv.shutdown()
        self._engine_srv.server_close()
        self._tmp.cleanup()
