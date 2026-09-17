"""Hermetic test helpers: temp dirs, offline config, stub upstream servers.

Nothing here touches the real cluster. Fake credentials are assembled at run
time so no credential-shaped literal exists in the repository (the autosync
secret gate would rightly refuse it).
"""

from __future__ import annotations

import http.server
import json
import os
import secrets
import sys
import tempfile
import threading
from pathlib import Path

UI_DIR = Path(__file__).resolve().parents[1]
REPO = UI_DIR.parents[1]
sys.path.insert(0, str(UI_DIR))
os.environ.setdefault("GX_UI_ACCESS_LOG", "0")

from gx_control_ui.config import UIConfig  # noqa: E402


def fake_key(prefix: str = "sk-") -> str:
    return prefix + secrets.token_hex(24)


class TempEnv:
    """Temp state/secret/log dirs plus a UIConfig pointing at them."""

    def __init__(self, **overrides) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.root = root
        for d in ("secrets", "state", "logs", "guard", "srvlogs", "media", "hf"):
            (root / d).mkdir()
        (root / "guard" / "node1-residency.json").write_text("{}")
        (root / "guard" / "node2-residency.json").write_text("{}")
        params = dict(
            hosts=("127.0.0.1",), port=0, repo_root=REPO, static_dir=UI_DIR / "web",
            docs_dir=UI_DIR / "docs", secret_dir=root / "secrets", state_dir=root / "state",
            log_dir=root / "logs", guard_dir=root / "guard", gx_state_root=root,
            srv_logs=root / "srvlogs", offline=True, media_dir=root / "media",
            hf_token_file=root / "hf" / "token", metrics_dir=root / "logs" / "metrics",
            secrets_root=root / "secrets",
            orchestrator_base="http://127.0.0.1:9", litellm_base="http://127.0.0.1:9",
            node1_swap_base="http://127.0.0.1:9", node2_swap_base="http://127.0.0.1:9",
            media_base="http://127.0.0.1:9", gxmax_base="http://127.0.0.1:9",
            music_base="http://127.0.0.1:9", music_key_file=root / "secrets" / "music-key",
            voice_base="http://127.0.0.1:9", voice_key_file=root / "secrets" / "voice-key",
            public_playground_url="http://127.0.0.1:8090/", public_control_url="http://127.0.0.1:8088/",
        )
        params.update(overrides)
        self.cfg = UIConfig(**params)

    def cleanup(self) -> None:
        self._tmp.cleanup()


class StubUpstream:
    """A tiny JSON HTTP server whose routes are plain dicts.

    routes[(method, path)] = (status, body) or a callable(handler, body) -> (status, body)
    Every request is recorded in .calls as (method, path, headers, body).
    """

    def __init__(self, routes: dict) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str, dict, object]] = []
        stub = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):  # noqa: A003
                pass

            def _handle(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw) if raw else None
                except ValueError:
                    body = raw
                path = self.path.split("?")[0]
                stub.calls.append((method, self.path, dict(self.headers), body))
                route = stub.routes.get((method, path))
                if route is None:
                    status, payload = 404, {"error": "no route"}
                elif callable(route):
                    status, payload = route(self, body)
                else:
                    status, payload = route
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json" if not isinstance(payload, bytes) else "video/mp4")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                self._handle("GET")

            def do_POST(self):  # noqa: N802
                self._handle("POST")

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class env_vars:
    """Context manager to set environment variables temporarily."""

    def __init__(self, **values) -> None:
        self.values = values
        self.saved: dict[str, str | None] = {}

    def __enter__(self):
        for k, v in self.values.items():
            self.saved[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
