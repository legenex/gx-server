"""HTTP + WebSocket ingress of the gx-live supervisor (stdlib). The only network surface.

    GET  /health                                         open: D-038 / plt.md §5 contract
    GET  /v1/live/model                                  identity, capabilities, lifecycle, memory
    POST /v1/live/load | /v1/live/unload                 lifecycle (Resource Control, gx-max drain)
    POST /v1/live/sessions                               create (Control Center only)
    GET  /v1/live/sessions/{id}                          summary + metrics (no content)
    POST /v1/live/sessions/{id}/end
    GET  /v1/live/sessions/{id}/tool-calls?wait=         long poll for the tool executor
    POST /v1/live/sessions/{id}/tool-calls/{call_id}     progress | result
    GET  /v1/live/sessions/{id}/ws?join=                 WebSocket (the Playground tunnel)

Everything except /health needs ``Authorization: Bearer <gx-live key>``.
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
from typing import Any

from . import __version__
from .errors import AuthError, ForbiddenError, LiveError, NotFoundError, TooLargeError, ValidationError
from .service import LiveService

log = logging.getLogger("gx_live.http")
access = logging.getLogger("gx_live.access")

JSON_LIMIT = 64 * 1024
SESSION_PATH = re.compile(
    r"^/v1/live/sessions/(live_[0-9a-f]{32})(/end|/ws|/tool-calls|/tool-calls/(call_[0-9a-f]{16}))?$")


class Handler(BaseHTTPRequestHandler):
    server_version = f"gx-live/{__version__}"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    service: LiveService
    api_key: str
    rtws: Any

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: A003
        return

    # ------------------------------------------------------------- output --
    def _json(self, status: int, payload: object, extra: dict | None = None) -> None:
        body = json.dumps(payload, separators=(",", ":"), default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self._status = status

    def _error(self, exc: LiveError) -> None:
        extra = {"Retry-After": "15"} if exc.status == 503 else None
        self._json(exc.status, exc.payload(), extra)

    # -------------------------------------------------------------- input --
    def _auth(self) -> None:
        header = self.headers.get("Authorization", "")
        token = header[7:] if header.startswith("Bearer ") else ""
        if not token or not hmac.compare_digest(token.encode(), self.api_key.encode()):
            raise AuthError("missing or invalid API key")

    def _body(self) -> Any:
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            raise ValidationError("chunked bodies are not supported")
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError as exc:
            raise ValidationError("invalid Content-Length") from exc
        if length < 0:
            raise ValidationError("invalid Content-Length")
        if length > JSON_LIMIT:
            self.close_connection = True
            raise TooLargeError("request body larger than 64 KiB")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        ctype = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise ValidationError("Content-Type must be application/json")
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise ValidationError("body is not valid JSON") from exc

    def _query(self) -> dict[str, str]:
        return {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query,
                                                          max_num_fields=8).items()}

    # ------------------------------------------------------------ routing --
    def _dispatch(self) -> None:
        self._t0 = time.time()
        self._status = 0
        path = urllib.parse.urlsplit(self.path).path
        s = self.service
        try:
            if self.command in ("GET", "HEAD") and path == "/health":
                return self._json(200, s.health())
            self._auth()
            method = self.command
            if method == "GET" and path == "/v1/live/model":
                return self._json(200, s.model_info())
            if method == "POST" and path == "/v1/live/load":
                self._body()
                return self._json(202, s.request_load())
            if method == "POST" and path == "/v1/live/unload":
                return self._json(200, s.request_unload(self._body()))
            if method == "POST" and path == "/v1/live/sessions":
                return self._json(201, s.create(self._body()))
            m = SESSION_PATH.match(path)
            if not m:
                raise NotFoundError(f"no route for {method} {path}")
            sid, sub, call_id = m.group(1), m.group(2), m.group(3)
            if sub is None and method == "GET":
                return self._json(200, s.summary(sid))
            if sub == "/end" and method == "POST":
                body = self._body()
                reason = body.get("reason") if isinstance(body, dict) else None
                reason = reason if reason in ("completed", "abandoned", "failed", "timeout") else "completed"
                return self._json(200, s.end(sid, reason))
            if sub == "/tool-calls" and method == "GET":
                q = self._query()
                try:
                    wait = float(q.get("wait", "0"))
                except ValueError as exc:
                    raise ValidationError("wait must be a number of seconds") from exc
                return self._json(200, s.claim_tool_calls(sid, wait))
            if call_id and method == "POST":
                return self._json(200, s.tool_update(sid, call_id, self._body()))
            if sub == "/ws" and method == "GET":
                return self._websocket(sid)
            raise NotFoundError(f"no route for {method} {path}")
        except LiveError as exc:
            self.close_connection = True
            if exc.status >= 500:
                log.error("%s %s -> %s %s", self.command, path, exc.code, exc.message)
            if not self._status:
                self._error(exc)
        except Exception:  # noqa: BLE001
            self.close_connection = True
            log.exception("unhandled error on %s %s", self.command, path)
            if not self._status:
                self._error(LiveError("internal error"))
        finally:
            access.info(json.dumps({"method": self.command, "path": _safe_path(path), "status": self._status,
                                    "ms": round((time.time() - self._t0) * 1000, 1),
                                    "client": self.client_address[0]}))

    def _websocket(self, sid: str) -> None:
        if (self.headers.get("X-GX-Session") or "") != sid:
            raise ForbiddenError("X-GX-Session must name the session in the path")
        token = self._query().get("join", "")
        sess = self.service.check_join(sid, token)
        try:
            ws = self.rtws.accept(self, max_message=4 * 1024 * 1024)
        except self.rtws.HandshakeError as exc:
            raise ValidationError(str(exc), code="bad_upgrade") from exc
        self._status = 101
        self.service.serve_client(sess, ws)

    do_GET = do_POST = do_HEAD = _dispatch  # noqa: N815

    def do_PUT(self) -> None:  # noqa: N802
        self._t0 = time.time()
        self._status = 0
        self.close_connection = True
        self._error(LiveError("method not allowed", code="method_not_allowed"))

    do_DELETE = do_PATCH = do_OPTIONS = do_PUT


def _safe_path(path: str) -> str:
    """Access-log path without anything token-like (the join token is in the query only)."""
    return path[:120]


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64


def build_servers(service: LiveService, api_key: str, binds: tuple[str, ...], port: int, rtws: Any) -> list[Server]:
    handler = type("BoundHandler", (Handler,), {"service": service, "api_key": api_key, "rtws": rtws})
    return [Server((b, port), handler) for b in binds]


def serve_forever(servers: list[Server]) -> None:
    threads = [threading.Thread(target=s.serve_forever, daemon=True, name=f"http-{s.server_address[0]}")
               for s in servers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
