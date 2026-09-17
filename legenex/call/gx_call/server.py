"""HTTP + WebSocket ingress of gx-call (stdlib). The ONLY network surface.

    GET    /health                                   open liveness + PLT pending-memory contract
    GET    /v1/call/model                            identity, capabilities, lifecycle, memory
    POST   /v1/call/load | /v1/call/unload           lifecycle ({"if_idle": bool, "reason": str})
    GET    /v1/call/sessions                         sessions known to this process
    POST   /v1/call/sessions                         create (Control Center only) -> join token + upstream path
    GET    /v1/call/sessions/{id}
    GET    /v1/call/sessions/{id}/events?after=&wait=   long-poll the event log
    POST   /v1/call/sessions/{id}/events             push state/transfer/notice events to the caller
    POST   /v1/call/sessions/{id}/tool-results       answer a tool call
    POST   /v1/call/sessions/{id}/end
    GET    /v1/call/sessions/{id}/recording?track=caller|agent
    DELETE /v1/call/sessions/{id}/recording
    GET    /v1/call/sessions/{id}/ws?join=<token>    WebSocket upgrade (from the Playground tunnel)

Every route except /health needs ``Authorization: Bearer <gx-call key>``.
"""

from __future__ import annotations

import hmac
import json
import logging
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__
from .errors import AuthError, CallError, NotFoundError, TooLargeError, ValidationError
from .service import CallService
from .ws import WebSocket, accept_key, valid_client_key

log = logging.getLogger("gx_call.http")
access = logging.getLogger("gx_call.access")

JSON_LIMIT = 256 * 1024
SESSION_PATH = re.compile(r"^/v1/call/sessions/(call_[0-9a-f]{32})(/events|/tool-results|/end|/recording|/ws)?$")


class Handler(BaseHTTPRequestHandler):
    server_version = f"gx-call/{__version__}"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    service: CallService
    api_key: str

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: A003
        return

    # ----------------------------------------------------------- output --
    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, val in (extra or {}).items():
            self.send_header(k, val)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self._status = status

    def _json(self, status: int, payload: object, extra: dict | None = None) -> None:
        self._send(status, json.dumps(payload, separators=(",", ":")).encode(), "application/json", extra)

    def _error(self, exc: CallError) -> None:
        self._json(exc.status, exc.payload(), {"Retry-After": "15"} if exc.status == 503 else None)

    # ------------------------------------------------------------ input --
    def _auth(self) -> None:
        header = self.headers.get("Authorization", "")
        token = header[7:] if header.startswith("Bearer ") else ""
        if not token or not hmac.compare_digest(token.encode(), self.api_key.encode()):
            raise AuthError("missing or invalid API key")

    def _body(self) -> dict:
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            raise ValidationError("chunked bodies are not supported; send Content-Length")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValidationError("invalid Content-Length") from exc
        if length < 0:
            raise ValidationError("invalid Content-Length")
        if length > JSON_LIMIT:
            self.close_connection = True
            raise TooLargeError("request body larger than 256 KiB")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            raise ValidationError("Content-Type must be application/json")
        try:
            body = json.loads(raw)
        except ValueError as exc:
            raise ValidationError("body is not valid JSON") from exc
        if not isinstance(body, dict):
            raise ValidationError("body must be a JSON object")
        return body

    def _query(self) -> dict[str, str]:
        return {k: val[0] for k, val in urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).items()}

    # ---------------------------------------------------------- routing --
    def _dispatch(self) -> None:
        self._t0 = time.time()
        self._status = 0
        path = urllib.parse.urlsplit(self.path).path
        try:
            if self.command in ("GET", "HEAD") and path == "/health":
                return self._json(200, {**self.service.health(), "version": __version__})
            self._auth()
            m = SESSION_PATH.match(path)
            if m and m.group(2) == "/ws" and self.command == "GET":
                return self._upgrade(m.group(1))
            handler = self._route(path, m)
            if handler is None:
                raise NotFoundError(f"no route for {self.command} {path}")
            handler()
        except CallError as exc:
            self.close_connection = True
            if exc.status >= 500:
                log.error("%s %s -> %s %s", self.command, path, exc.code, exc.message)
            self._error(exc)
        except Exception:  # noqa: BLE001
            self.close_connection = True
            log.exception("unhandled error on %s %s", self.command, path)
            self._error(CallError("internal error"))
        finally:
            access.info(json.dumps({"method": self.command, "path": re.sub(r"join=[^&]+", "join=***", path),
                                    "status": self._status, "ms": round((time.time() - self._t0) * 1000, 1),
                                    "client": self.client_address[0]}))

    def _route(self, path: str, m: re.Match | None):  # noqa: ANN202
        s, cmd = self.service, self.command
        simple = {
            ("GET", "/v1/call/model"): lambda: self._json(200, s.model_info()),
            ("POST", "/v1/call/load"): lambda: self._json(200, s.load()),
            ("POST", "/v1/call/unload"): self._unload,
            ("GET", "/v1/call/sessions"): lambda: self._json(200, {"data": s.list()}),
            ("POST", "/v1/call/sessions"): lambda: self._json(201, s.create(self._body())),
        }
        if (cmd, path) in simple:
            return simple[(cmd, path)]
        if not m:
            return None
        sid, sub = m.group(1), m.group(2)
        if sub is None and cmd == "GET":
            return lambda: self._json(200, s.get(sid))
        if sub == "/events" and cmd == "GET":
            return lambda: self._events(sid)
        if sub == "/events" and cmd == "POST":
            return lambda: self._json(200, s.inject(sid, self._body()))
        if sub == "/tool-results" and cmd == "POST":
            return lambda: self._json(200, s.tool_result(sid, self._body()))
        if sub == "/end" and cmd == "POST":
            return lambda: self._json(200, s.end(sid, _reason(self._body().get("reason"))))
        if sub == "/recording" and cmd == "GET":
            return lambda: self._recording(sid)
        if sub == "/recording" and cmd == "DELETE":
            return lambda: self._json(200, s.delete_recording(sid))
        return None

    # --------------------------------------------------------- handlers --
    def _unload(self) -> None:
        body = self._body()
        self._json(200, self.service.unload(if_idle=body.get("if_idle") is True,
                                            reason=_reason(body.get("reason") or "manual")))

    def _events(self, sid: str) -> None:
        q = self._query()
        try:
            after = max(0, int(q.get("after", "0")))
            wait = max(0.0, min(30.0, float(q.get("wait", "0"))))
        except ValueError as exc:
            raise ValidationError("after and wait must be numbers") from exc
        self._json(200, self.service.events_after(sid, after, wait))

    def _recording(self, sid: str) -> None:
        track = self._query().get("track", "")
        path = self.service.recording_path(sid, track)
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self._status = 200
        with path.open("rb") as fh:
            while chunk := fh.read(1 << 20):
                self.wfile.write(chunk)

    def _upgrade(self, sid: str) -> None:
        h = self.headers
        if (h.get("Upgrade", "").lower() != "websocket" or "upgrade" not in h.get("Connection", "").lower()
                or h.get("Sec-WebSocket-Version") != "13" or not valid_client_key(h.get("Sec-WebSocket-Key", ""))):
            raise ValidationError("a WebSocket upgrade is required", code="upgrade_required")
        token = self._query().get("join", "")
        session = self.service.check_join(sid, h.get("X-GX-Session"), token)
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept_key(h["Sec-WebSocket-Key"]))
        self.end_headers()
        self.wfile.flush()
        self._status = 101
        self.close_connection = True
        ws = WebSocket(self.connection, self.rfile, mask_outgoing=False, require_masked=True)
        self.service.attach(session, ws)

    do_GET = do_POST = do_DELETE = do_HEAD = _dispatch  # noqa: N815


def _reason(value: object) -> str:
    text = str(value or "ended")
    return re.sub(r"[^a-z0-9_]", "_", text.lower())[:40] or "ended"


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64


def build_servers(service: CallService, api_key: str, binds: tuple[str, ...], port: int) -> list[Server]:
    handler = type("BoundHandler", (Handler,), {"service": service, "api_key": api_key})
    return [Server((b, port), handler) for b in binds]


def serve_forever(servers: list[Server]) -> None:
    threads = [threading.Thread(target=s.serve_forever, daemon=True, name=f"http-{s.server_address[0]}")
               for s in servers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
