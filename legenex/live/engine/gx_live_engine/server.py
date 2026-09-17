"""Engine HTTP/WebSocket endpoint (inside the container, published on host loopback only).

    GET /health        bearer engine key -> {ready, loading, error, gpu, session}
    GET /v1/session    bearer engine key + X-GX-Session, WebSocket upgrade

Only the gx-live supervisor talks to this. One session at a time: a new
session connection replaces the previous one.
"""

from __future__ import annotations

import hmac
import json
import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from gxcommon import rtws

from .session import EngineSession
from .vad import StreamingVad, silero_prob

log = logging.getLogger("gx_live_engine.server")
SESSION_RE = re.compile(r"^live_[0-9a-f]{32}$|^probe_[0-9a-z]{1,32}$")


class State:
    def __init__(self, runtime: Any, key: str) -> None:
        self.runtime = runtime
        self.key = key
        self.loading = True
        self.error: str | None = None
        self.lock = threading.Lock()
        self.current: tuple[EngineSession, Any] | None = None


class Handler(BaseHTTPRequestHandler):
    server_version = "gx-live-engine"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    state: State

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        return

    def _json(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _authorised(self) -> bool:
        auth = self.headers.get("Authorization") or ""
        token = auth[7:] if auth.startswith("Bearer ") else ""
        return bool(token) and hmac.compare_digest(token.encode(), self.state.key.encode())

    def do_GET(self) -> None:  # noqa: N802
        if not self._authorised():
            self._json(401, {"error": {"code": "unauthorized", "message": "engine key required"}})
            return
        st = self.state
        if self.path == "/health":
            rt = st.runtime
            cur = st.current
            self._json(200, {"ready": bool(rt.loaded), "loading": st.loading, "error": st.error,
                             "load_seconds": rt.load_seconds, **{f"gpu_{k}": v for k, v in rt.gpu().items()},
                             "session": cur[0].sid if cur else None})
            return
        if self.path != "/v1/session":
            self._json(404, {"error": {"code": "not_found", "message": "no such route"}})
            return
        sid = self.headers.get("X-GX-Session") or ""
        if not SESSION_RE.match(sid):
            self._json(400, {"error": {"code": "invalid_session", "message": "X-GX-Session is required"}})
            return
        if not st.runtime.loaded:
            self._json(503, {"error": {"code": "not_ready", "message": "the model is still loading"}})
            return
        try:
            ws = rtws.accept(self, max_message=4 * 1024 * 1024)
        except rtws.HandshakeError as exc:
            self._json(exc.status, {"error": {"code": "bad_upgrade", "message": str(exc)}})
            return
        self._serve(sid, ws)

    def _serve(self, sid: str, ws: Any) -> None:
        st = self.state
        first = ws.recv()
        try:
            start = json.loads(first.text()) if first is not None and first.is_text else {}
        except ValueError:
            start = {}
        if start.get("type") != "engine.session.start" or start.get("session_id") != sid:
            ws.close(1008, "expected engine.session.start")
            return
        config = start.get("config") if isinstance(start.get("config"), dict) else {}
        tools = start.get("tools") if isinstance(start.get("tools"), list) else []
        vad_cfg = config.get("vad") or {}
        prob, reset = silero_prob()
        vad = StreamingVad(prob, reset=reset, threshold=float(vad_cfg.get("threshold", 0.5)),
                           silence_ms=int(vad_cfg.get("silence_ms", 700)))

        def send_json(ev: dict) -> None:
            try:
                ws.send_text(json.dumps(ev, separators=(",", ":"), ensure_ascii=False))
            except rtws.WebSocketError:
                pass

        def send_binary(data: bytes) -> None:
            try:
                ws.send_binary(data)
            except rtws.WebSocketError:
                pass

        sess = EngineSession(st.runtime, sid, config, tools, send_json=send_json, send_binary=send_binary, vad=vad)
        sess.client_attached = bool(start.get("client_attached", True))
        with st.lock:
            old = st.current
            st.current = (sess, ws)
        if old is not None:
            old[0].stop()
            old[1].close(4409, "replaced by a new session")
            old[0].worker.join(timeout=30)
        sess.start()
        log.info("session %s started", sid)
        try:
            while True:
                msg = ws.recv()
                if msg is None:
                    break
                if msg.is_text:
                    try:
                        ev = json.loads(msg.text())
                    except ValueError:
                        continue
                    if isinstance(ev, dict):
                        sess.on_event(ev)
                        if ev.get("type") == "engine.session.end":
                            break
                else:
                    sess.on_binary(msg.data)
        finally:
            sess.stop()
            with st.lock:
                if st.current and st.current[0] is sess:
                    st.current = None
            ws.close(1000, "session closed")
            log.info("session %s closed", sid)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def build(host: str, port: int, state: State) -> Server:
    handler = type("BoundHandler", (Handler,), {"state": state})
    return Server((host, port), handler)
