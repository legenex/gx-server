"""GX-Playground: the creative web application on gx10-01 (D-037).

    browser --> gx-playground (:8090, loopback + Tailscale)
                  |- static single-page app (web/), strict CSP
                  '- allow-listed API paths --> gx-control-ui (127.0.0.1:8088)
                                                   '- media router / gx-music on gx10-02 (fabric)

The Playground has no business logic and no credentials of its own. Every API
call is forwarded to the Control Center backend, which owns authentication
(scrypt, server-side sessions, CSRF), the Media Library, the job queues and
Resource Control. The session cookie is host-scoped, so one sign-in covers
both applications. The proxy adds two headers the backend trusts only from
loopback: a shared token (0600 file) and the real client address, so login
throttling and audit entries see the real client.

Only the paths in ALLOW are forwarded. The browser never reaches gx10-02, the
LiteLLM master key, Docker or a shell.
"""

from __future__ import annotations

import gzip
import hashlib
import http.client
import json
import logging
import mimetypes
import os
import re
import secrets
import socket
import ssl
import stat as stat_module
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_COMMON = Path(__file__).resolve().parents[2] / "common"
if _COMMON.is_dir() and str(_COMMON) not in sys.path:
    sys.path.insert(0, str(_COMMON))

from gxcommon.metrics import Metrics  # noqa: E402

from . import __version__  # noqa: E402
from . import tls as tlsmod  # noqa: E402
from .tunnel import (  # noqa: E402
    RT_PATH, Authorizer, Limits, Pump, TunnelError, TunnelRegistry, accept_for, parse_head, read_head,
    ticket_from_query, upstream_request, validate_client_upgrade)

log = logging.getLogger("gx.playground")

PKG = Path(__file__).resolve().parent
WEB = Path(os.environ.get("GX_PG_STATIC_DIR", str(PKG.parent / "web")))
UPSTREAM = os.environ.get("GX_PG_UPSTREAM", "http://127.0.0.1:8088")
TOKEN_FILE = Path(os.environ.get("GX_PG_PROXY_TOKEN_FILE",
                                 "/srv/projects/gx-cluster/secrets/control-ui/proxy-token"))
CONTROL_URL = os.environ.get("GX_PG_CONTROL_URL", "http://100.105.214.61:8088/")
#: The LiteLLM gateway clients use (shown on the Settings page; never proxied).
GATEWAY_URL = os.environ.get("GX_PG_GATEWAY_URL", "http://100.105.214.61:4000/v1")
MAX_BODY = 160 * 1024 * 1024
#: Optional HTTPS listener (plt.md section 2): 0 disables it.
TLS_PORT = int(os.environ.get("GX_PG_TLS_PORT", "0") or 0)
TLS_DIR = Path(os.environ.get("GX_PG_TLS_DIR", str(tlsmod.DEFAULT_DIR)))
METRICS = Metrics("gx-playground", node="gx10-01",
                  file=os.environ.get("GX_PG_METRICS_FILE") or None)

#: (methods, path regex) forwarded to the backend. Everything else is 404.
ALLOW: tuple[tuple[frozenset[str], re.Pattern[str]], ...] = tuple(
    (frozenset(m.split(",")), re.compile(p)) for m, p in (
        ("GET", r"/api/(health|session)"),
        ("POST", r"/api/(login|logout)"),
        ("GET,POST", r"/api/media/(assets|jobs|options|upload|delete|zip)(/[A-Za-z0-9_\-]+){0,2}"),
        ("GET,POST", r"/api/music/(model|tags|jobs|upload)(/mus-[0-9a-f]{32}(/(lineage|cancel))?)?"),
        # Build V3 MUS: conditioning preview, Build with AI / Improve, reference analysis
        ("POST", r"/api/music/(preview|ai/build|ai/improve|reference/analyze)"),
        ("GET", r"/api/music/reference/[0-9a-f]{24}"),
        # Build V3 WAN: Wan 2.2 LoRA library, presets, workflow preview, generation, history
        ("GET", r"/api/video/(config|loras|loras/l_[0-9a-f]{16}|presets|presets/wp_[0-9a-f]{16}|generations"
                r"|generations/[0-9a-f]{16}(/workflow)?|errors)"),
        ("POST", r"/api/video/(loras/rescan|loras/order|loras/l_[0-9a-f]{16}|pairs(/remove|/restore)?|presets"
                 r"|presets/wp_[0-9a-f]{16}(/(duplicate|delete|resolve))?|workflow|generate|jobs/[0-9a-f]{16}/cancel)"),
        ("GET", r"/api/resources/(summary|explain/gx-[a-z]{3,6}|profile/plan)"),
        ("POST", r"/api/resources/profile"),
        ("GET", r"/api/creative/overview"),
        # Build V3 platform (PLT): Models, Logs, Settings pages and realtime session list
        ("GET", r"/api/(catalog|activity|realtime/sessions)"),
        ("GET,POST", r"/api/preferences"),
        # Build V3 VOI: Voice Studio (session) and the public voice API (gateway key)
        ("GET", r"/api/voice/(model|voices|jobs)"),
        ("POST", r"/api/voice/(voices|jobs|upload)"),
        ("GET", r"/api/voice/voices/(vc_[0-9a-f]{24}|preset:[a-z_]{2,16})(/versions)?"),
        ("POST", r"/api/voice/voices/vc_[0-9a-f]{24}(/delete)?"),
        ("GET", r"/api/voice/jobs/vj_[0-9a-f]{32}(/takes/[0-3]/audio)?"),
        ("POST", r"/api/voice/jobs/vj_[0-9a-f]{32}/(cancel|delete|takes/[0-3]/save)"),
        ("GET,POST", r"/v1/voice/(model|voices|jobs|uploads|speech|design|clone|dialogue)"),
        ("GET", r"/v1/voice/voices/(vc_[0-9a-f]{24}|preset:[a-z_]{2,16})"),
        ("GET,POST", r"/v1/voice/jobs/vj_[0-9a-f]{32}(/cancel|/takes/[0-3]/(content|save))?"),
        ("GET,POST,DELETE", r"/v1/music(/[A-Za-z0-9_\-]+){0,2}"),
    ))
REQUEST_HEADERS = ("cookie", "content-type", "content-length", "x-csrf-token", "accept", "accept-encoding",
                   "range", "if-none-match", "x-title", "x-filename")
RESPONSE_HEADERS = ("content-type", "content-length", "content-encoding", "content-range", "accept-ranges",
                    "content-disposition", "cache-control", "set-cookie", "etag", "vary", "location",
                    "retry-after", "last-modified")
SPA_ROUTE = re.compile(r"/(dashboard|images|video|music|voice|library|history|models|logs|settings)"
                       r"(/[a-z0-9_\-]{0,64}){0,2}")

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
       "media-src 'self' blob:; connect-src 'self'; font-src 'self'; object-src 'none'; "
       "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
#: Everything denied. The app document (index.html) additionally allows the
#: microphone and camera for itself: the Playground is one hash-routed
#: document, and only the Live and Call Agents pages call getUserMedia.
PERMISSIONS_DENY = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
PERMISSIONS_APP = "camera=(self), microphone=(self), geolocation=(), payment=(), usb=()"
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": PERMISSIONS_DENY,
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}
LIMITS = Limits.from_env()
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("image/svg+xml", ".svg")
mimetypes.add_type("application/manifest+json", ".webmanifest")


def allowed(method: str, path: str) -> bool:
    return any(method in methods and rx.fullmatch(path) for methods, rx in ALLOW)


class Static:
    """The `web/` tree, cached in memory but re-read whenever a file changes.

    The Playground is deployed by editing this checkout in place, so a snapshot
    taken once at start-up silently serves yesterday's JavaScript to the browser
    while the source on disk is current (B-028). Every lookup therefore stats the
    file and rebuilds the entry when its mtime, size or inode changed, and a path
    that was not in the tree at start-up is picked up the first time it is asked
    for. An unchanged file costs one `stat()`; the body, the gzip copy and the
    ETag come from the cache.

    Set `GX_PG_STATIC_FREEZE=1` to keep the start-up snapshot (used by tests that
    assert on a fixed ETag).
    """

    __slots__ = ("root", "freeze", "_lock", "_cache")

    #: (data, gzipped or None, content type, ETag)
    Entry = tuple[bytes, bytes | None, str, str]

    def __init__(self, root: Path, *, freeze: bool | None = None) -> None:
        self.root = root.resolve()
        self.freeze = (os.environ.get("GX_PG_STATIC_FREEZE") == "1") if freeze is None else freeze
        self._lock = threading.Lock()
        #: path -> (stat signature, entry). A signature of None means "known absent".
        self._cache: dict[str, tuple[tuple[int, int, int] | None, Static.Entry | None]] = {}
        for p in sorted(self.root.rglob("*")):
            if p.is_file() and not p.name.startswith("."):
                self.get("/" + p.relative_to(self.root).as_posix())

    @property
    def files(self) -> dict[str, "Static.Entry"]:
        """The currently cached entries. Kept for callers that enumerate the tree."""
        with self._lock:
            return {k: v for k, (_sig, v) in self._cache.items() if v is not None}

    def _path_of(self, path: str) -> Path | None:
        """The file `path` names, or None when it escapes the tree or is hidden."""
        if not path.startswith("/") or "\x00" in path:
            return None
        parts = [seg for seg in path[1:].split("/") if seg]
        if not parts or any(seg in (".", "..") or seg.startswith(".") for seg in parts):
            return None
        p = self.root.joinpath(*parts)
        try:
            if p.resolve().relative_to(self.root) is None:  # pragma: no cover - defensive
                return None
        except (OSError, ValueError):
            return None
        return p

    @staticmethod
    def _build(p: Path, data: bytes) -> "Static.Entry":
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/json", "image/svg+xml",
                                                  "application/manifest+json"):
            ctype += "; charset=utf-8"
        gz = gzip.compress(data, 6) if len(data) > 1024 and not ctype.startswith("image/png") else None
        return (data, gz, ctype, '"' + hashlib.sha256(data).hexdigest()[:20] + '"')

    def get(self, path: str) -> "Static.Entry | None":
        with self._lock:
            cached = self._cache.get(path)
        if cached is not None and self.freeze:
            return cached[1]
        p = self._path_of(path)
        if p is None:
            return None
        try:
            st = p.stat()
            if not stat_module.S_ISREG(st.st_mode):
                raise OSError
            sig = (st.st_mtime_ns, st.st_size, st.st_ino)
        except OSError:
            if cached is not None:
                with self._lock:
                    self._cache[path] = (None, None)
            return None
        if cached is not None and cached[0] == sig:
            return cached[1]
        try:
            data = p.read_bytes()
        except OSError:
            return cached[1] if cached else None
        entry = self._build(p, data)
        with self._lock:
            self._cache[path] = (sig, entry)
        return entry


class RateLimit:
    def __init__(self, per_minute: int = 900) -> None:
        self.per_minute = per_minute
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def ok(self, ip: str) -> bool:
        now = time.time()
        with self._lock:
            hits = [t for t in self._hits.get(ip, []) if now - t < 60]
            if len(hits) >= self.per_minute:
                self._hits[ip] = hits
                return False
            hits.append(now)
            self._hits[ip] = hits
            if len(self._hits) > 4096:
                self._hits = {k: v for k, v in self._hits.items() if v and now - v[-1] < 60}
            return True


def read_token() -> str:
    try:
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


class Handler(BaseHTTPRequestHandler):
    server_version = "gx-playground"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    static: Static
    limiter: RateLimit
    tunnels: TunnelRegistry
    authorize: Authorizer
    scheme = "http"
    #: Slow or idle clients cannot hold a request thread forever (headers, bodies).
    timeout = 120
    upstream = urllib.parse.urlsplit(UPSTREAM)

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        pass

    # ------------------------------------------------------------ output
    def _security_headers(self, app_document: bool = False) -> None:
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, PERMISSIONS_APP if app_document and k == "Permissions-Policy" else v)

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None,
              app_document: bool = False) -> None:
        self._status = status
        self.send_response(status)
        self._security_headers(app_document)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: dict) -> None:
        self._send(status, json.dumps(payload).encode(), "application/json; charset=utf-8",
                   {"Cache-Control": "no-store"})

    def _error(self, status: int, message: str, code: str) -> None:
        self.close_connection = True
        self._json(status, {"error": {"message": message, "code": code}})

    # ------------------------------------------------------------ dispatch
    def _client_ip(self) -> str:
        return self.client_address[0] if self.client_address else "?"

    def _same_origin(self) -> bool:
        host = self.headers.get("Host") or ""
        for name in ("Origin", "Referer"):
            value = self.headers.get(name)
            if value:
                return urllib.parse.urlsplit(value).netloc == host
        return None  # type: ignore[return-value]

    def _handle(self) -> None:
        self._t0 = time.time()
        self._status = 0
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        method = "GET" if self.command == "HEAD" else self.command
        try:
            if not self.limiter.ok(self._client_ip()):
                self._error(429, "too many requests; slow down", "rate_limited")
                return
            if path == "/pg/health":
                self._json(200, {"status": "ok", "service": "gx-playground", "version": __version__,
                                 "upstream": self._upstream_health()})
                return
            if path == "/pg/config":
                self._json(200, {"control_center_url": CONTROL_URL, "gateway_url": GATEWAY_URL,
                                 "version": __version__,
                                 "tls": self.tls_info(), "realtime": {"enabled": LIMITS.enabled,
                                                                      "idle_s": LIMITS.idle_s,
                                                                      "max_frame": LIMITS.max_frame}})
                return
            if path == "/pg/ca.crt":
                self._ca_certificate()
                return
            if path.startswith("/rt/"):
                if method != "GET" or self.command == "HEAD":
                    self._error(405, "method not allowed", "method_not_allowed")
                    return
                self._realtime(path, parsed.query)
                return
            if path.startswith("/api/") or path.startswith("/v1/"):
                if not allowed(method, path):
                    self._error(404, "not found", "not_found")
                    return
                if method in ("POST", "DELETE"):
                    same = self._same_origin()
                    if same is False or (same is None and path.startswith("/api/")):
                        self._error(403, "cross-origin request refused", "bad_origin")
                        return
                self._proxy(method, path, parsed.query)
                return
            if method != "GET":
                self._error(405, "method not allowed", "method_not_allowed")
                return
            self._static(path)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:  # noqa: BLE001
            log.exception("unhandled error on %s %s", self.command, path)
            if not self._status:
                self._error(502, "the Playground could not reach its backend", "bad_gateway")
        finally:
            if path.startswith(("/api/", "/v1/", "/rt/")) or self._status >= 400:
                print(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "kind": "access",
                                  "method": self.command, "path": path, "status": self._status,
                                  "ms": round((time.time() - self._t0) * 1000), "ip": self._client_ip()}),
                      flush=True)

    do_GET = do_POST = do_DELETE = do_HEAD = _handle  # noqa: N815

    def do_PUT(self) -> None:  # noqa: N802
        self._t0 = time.time()
        self._error(405, "method not allowed", "method_not_allowed")

    do_PATCH = do_OPTIONS = do_PUT

    # ------------------------------------------------------------ static
    def _static(self, path: str) -> None:
        if path == "/" or SPA_ROUTE.fullmatch(path):
            path = "/index.html"
        app_document = path == "/index.html"
        entry = self.static.get(path)
        if entry is None:
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        data, gz, ctype, etag = entry
        headers = {"ETag": etag, "Cache-Control": "no-cache"}
        if self.headers.get("If-None-Match") == etag:
            self._status = 304
            self.send_response(304)
            self._security_headers(app_document)
            self.send_header("ETag", etag)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if gz is not None and "gzip" in (self.headers.get("Accept-Encoding") or ""):
            headers.update({"Content-Encoding": "gzip", "Vary": "Accept-Encoding"})
            data = gz
        self._send(200, data, ctype, headers, app_document=app_document)

    # ------------------------------------------------------------- HTTPS helper
    @staticmethod
    def tls_info() -> dict:
        info = tlsmod.info(TLS_DIR) if TLS_PORT else {"configured": False}
        info["port"] = TLS_PORT or None
        info["enabled"] = bool(TLS_PORT) and info.get("configured", False)
        return info

    def _ca_certificate(self) -> None:
        """The local CA's PUBLIC certificate, so users can trust the HTTPS listener once."""
        path = tlsmod.Paths(TLS_DIR).ca_crt
        try:
            data = path.read_bytes()
        except OSError:
            self._error(404, "the HTTPS listener is not set up on this host", "not_found")
            return
        if b"PRIVATE KEY" in data or not data.startswith(b"-----BEGIN CERTIFICATE-----"):
            self._error(500, "refusing to serve an unexpected CA file", "internal")
            return
        self._send(200, data, "application/x-x509-ca-cert", {
            "Content-Disposition": 'attachment; filename="gx-playground-ca.crt"', "Cache-Control": "no-cache"})

    # ------------------------------------------------------------- realtime
    def _realtime(self, path: str, query: str) -> None:
        self.close_connection = True
        m = RT_PATH.fullmatch(path)
        if not m or not m.group(2).startswith(m.group(1) + "_"):
            self._error(404, "not found", "not_found")
            return
        if not LIMITS.enabled:
            self._error(503, "realtime connections are switched off", "realtime_disabled")
            return
        service, session_id = m.groups()
        t0 = time.monotonic()
        request_id = secrets.token_hex(8)
        fields: dict = {"alias": f"gx-{service}", "session_id": session_id, "request_id": request_id,
                        "scheme": self.scheme}
        tunnel = None
        upstream = None
        try:
            key, protocols = validate_client_upgrade(self.headers)
            ticket = ticket_from_query(query)
            grant = self.authorize(service=service, session_id=session_id, ticket=ticket,
                                   cookie=self.headers.get("Cookie"), origin=self.headers.get("Origin"),
                                   host=self.headers.get("Host"), scheme=self.scheme,
                                   client_ip=self._client_ip())
            fields.update(owner=grant.get("owner"), user=grant.get("user"), via=grant.get("via"))
            tunnel = self.tunnels.acquire(service, session_id, str(grant.get("owner")), LIMITS)
            target = grant["target"]
            try:
                upstream = socket.create_connection((target["host"], target["port"]),
                                                    timeout=LIMITS.connect_timeout_s)
            except OSError as exc:
                raise TunnelError(502, "upstream_unavailable",
                                  f"gx-{service} on gx10-02 is not reachable right now") from exc
            upstream.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            upstream.sendall(upstream_request(grant, key, protocols, session_id, request_id))
            head, rest = read_head(upstream, time.monotonic() + LIMITS.handshake_timeout_s)
            status, up_headers = parse_head(head)
            fields["upstream_status"] = status
            if status != 101:
                code = {404: (404, "not_found"), 409: (409, "session_busy"), 410: (410, "session_ended"),
                        429: (429, "too_many_connections"), 503: (503, "upstream_busy")}.get(
                    status, (502, "upstream_refused"))
                raise TunnelError(code[0], code[1], f"gx-{service} refused the connection (HTTP {status})")
            if up_headers.get("upgrade", "").lower() != "websocket" or \
                    up_headers.get("sec-websocket-accept") != accept_for(key) or \
                    up_headers.get("sec-websocket-extensions"):
                raise TunnelError(502, "upstream_refused", f"gx-{service} sent an invalid upgrade")
            chosen = up_headers.get("sec-websocket-protocol")
            if chosen and chosen not in protocols:
                raise TunnelError(502, "upstream_refused", f"gx-{service} chose an unoffered subprotocol")
            lines = ["HTTP/1.1 101 Switching Protocols", "Upgrade: websocket", "Connection: Upgrade",
                     f"Sec-WebSocket-Accept: {accept_for(key)}"]
            if chosen:
                lines.append(f"Sec-WebSocket-Protocol: {chosen}")
            self.wfile.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
            self.wfile.flush()
            self._status = 101
            METRICS.emit("tunnel.open", duration_ms=round((time.monotonic() - t0) * 1000), **fields)
            deadline = time.monotonic() + min(LIMITS.max_s, int(grant.get("max_seconds") or LIMITS.max_s))
            pump = Pump(self.connection, upstream, tunnel, LIMITS, deadline)
            self.connection.setblocking(False)
            early = bytearray()
            while True:
                try:
                    chunk = self.rfile.read1(65536)
                except (BlockingIOError, ssl.SSLWantReadError, ssl.SSLWantWriteError):
                    break
                if not chunk:
                    break
                early += chunk
            fields["close_reason"] = pump.run(first_to_client=rest, first_to_upstream=bytes(early))
            fields.update(bytes_in=pump.bytes_in, bytes_out=pump.bytes_out,
                          frames_in=pump.client_frames.frames, frames_out=pump.server_frames.frames)
        except TunnelError as exc:
            fields.update(close_reason=f"refused: {exc.code}")
            if not self._status:
                self._error(exc.status, str(exc), exc.code)
        except OSError as exc:
            fields.update(close_reason=f"socket error: {type(exc).__name__}")
            if not self._status:
                self._error(502, "the realtime connection failed", "upstream_unavailable")
        finally:
            if upstream is not None:
                try:
                    upstream.close()
                except OSError:
                    pass
            if tunnel is not None:
                self.tunnels.release(tunnel)
            METRICS.emit("tunnel.close" if self._status == 101 else "tunnel.refused",
                         duration_ms=round((time.monotonic() - t0) * 1000),
                         outcome="ok" if self._status == 101 else "refused", status=self._status, **fields)

    # ------------------------------------------------------------- proxy
    def _upstream_health(self) -> dict:
        try:
            conn = http.client.HTTPConnection(self.upstream.hostname, self.upstream.port or 80, timeout=3)
            conn.request("GET", "/api/health")
            res = conn.getresponse()
            body = json.loads(res.read() or b"{}")
            conn.close()
            return {"ok": res.status == 200, "version": body.get("version")}
        except (OSError, ValueError):
            return {"ok": False}

    def _proxy(self, method: str, path: str, query: str) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._error(400, "invalid Content-Length", "invalid_request")
            return
        if length < 0 or length > MAX_BODY:
            self._error(413, "request body too large", "too_large")
            return
        if (self.headers.get("Transfer-Encoding") or "").lower() == "chunked":
            self._error(411, "send a Content-Length", "length_required")
            return
        headers = {k: v for k, v in self.headers.items() if k.lower() in REQUEST_HEADERS}
        if path.startswith("/v1/"):
            auth = self.headers.get("Authorization")
            if auth:
                headers["Authorization"] = auth
            headers.pop("Cookie", None)  # the public API is key-only
            headers.pop("cookie", None)
        headers["X-GX-Proxy-Token"] = read_token()
        headers["X-GX-Forwarded-For"] = self._client_ip()
        headers["X-GX-Forwarded-Proto"] = self.scheme
        headers["Host"] = f"{self.upstream.hostname}:{self.upstream.port or 80}"
        timeout = 900 if length > 1_000_000 or path.endswith(("/upload", "/uploads", "/zip")) else 300
        conn = http.client.HTTPConnection(self.upstream.hostname, self.upstream.port or 80, timeout=timeout)
        try:
            conn.putrequest(method, path + (f"?{query}" if query else ""), skip_host=True,
                            skip_accept_encoding=True)
            for k, v in headers.items():
                conn.putheader(k, v)
            if "Content-Length" not in headers and "content-length" not in {h.lower() for h in headers}:
                conn.putheader("Content-Length", str(length))
            conn.endheaders()
            remaining = length
            while remaining > 0:
                chunk = self.rfile.read(min(1 << 20, remaining))
                if not chunk:
                    break
                conn.send(chunk)
                remaining -= len(chunk)
            res = conn.getresponse()
        except OSError:
            conn.close()
            self.close_connection = True
            self._error(502, "the Control Center backend is not reachable", "bad_gateway")
            return
        self._status = res.status
        self.send_response(res.status)
        self._security_headers()
        has_length = False
        for k, v in res.getheaders():
            lk = k.lower()
            if lk in RESPONSE_HEADERS:
                self.send_header(k, v)
                has_length = has_length or lk == "content-length"
        if not has_length:
            self.close_connection = True
            self.send_header("Connection", "close")
        self.end_headers()
        try:
            if self.command != "HEAD":
                while True:
                    chunk = res.read(1 << 20)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        finally:
            conn.close()


# ------------------------------------------------------------ bootstrap
class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def __init__(self, addr, handler):
        if ":" in addr[0]:
            self.address_family = socket.AF_INET6
        super().__init__(addr, handler)


class TLSServer(Server):
    """HTTPS: the TLS handshake runs in the request thread (bounded), never in
    the accept loop, so a slow client cannot stall other connections."""

    handshake_timeout = 10.0

    def __init__(self, addr, handler, context: ssl.SSLContext):
        self.context = context
        super().__init__(addr, handler)

    def finish_request(self, request, client_address):
        request.settimeout(self.handshake_timeout)
        try:
            tls_sock = self.context.wrap_socket(request, server_side=True)
        except (OSError, ssl.SSLError):
            return
        try:
            tls_sock.settimeout(None)
            self.RequestHandlerClass(tls_sock, client_address, self)
        finally:
            try:
                tls_sock.close()
            except OSError:
                pass


def tailscale_ipv4() -> str | None:
    try:
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    ip = out.stdout.strip().splitlines()[0] if out.returncode == 0 and out.stdout.strip() else ""
    return ip if ip.startswith("100.") else None


def resolve_hosts(raw: str) -> list[str]:
    hosts = []
    for item in (h.strip() for h in raw.split(",")):
        if not item:
            continue
        if item in ("0.0.0.0", "::"):
            raise ValueError("GX_PG_HOSTS must not contain a wildcard address")
        if item == "tailscale":
            ts = tailscale_ipv4()
            if ts:
                hosts.append(ts)
            continue
        hosts.append(item)
    return list(dict.fromkeys(hosts))


def build(hosts: list[str], port: int, static_dir: Path = WEB, *, tls_port: int = 0,
          tls_dir: Path = TLS_DIR) -> list[Server]:
    up = urllib.parse.urlsplit(UPSTREAM)
    shared = {"static": Static(static_dir), "limiter": RateLimit(), "tunnels": TunnelRegistry(),
              "authorize": Authorizer(up.hostname or "127.0.0.1", up.port or 80, read_token)}
    handler = type("BoundHandler", (Handler,), dict(shared))
    servers: list[Server] = []
    for host in hosts:
        try:
            servers.append(Server((host, port), handler))
        except OSError as exc:
            log.warning("cannot bind %s:%s (%s)", host, port, exc)
    if not servers:
        raise RuntimeError(f"could not bind any of {hosts} on port {port}")
    if tls_port:
        try:
            context = tlsmod.server_context(tls_dir)
        except (OSError, ssl.SSLError) as exc:
            log.warning("HTTPS listener disabled: no usable certificate in %s (%s); run "
                        "scripts/tls-setup.sh", tls_dir, exc)
            return servers
        tls_handler = type("BoundTLSHandler", (Handler,), {**shared, "scheme": "https"})
        for host in hosts:
            try:
                servers.append(TLSServer((host, tls_port), tls_handler, context))
            except OSError as exc:
                log.warning("cannot bind %s:%s (%s)", host, tls_port, exc)
    return servers


def main() -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format='{"ts":"%(asctime)s","kind":"log","level":"%(levelname)s","msg":"%(message)s"}')
    hosts = resolve_hosts(os.environ.get("GX_PG_HOSTS", "127.0.0.1,tailscale"))
    port = int(os.environ.get("GX_PG_PORT", "8090"))
    servers = build(hosts, port, tls_port=TLS_PORT)
    if not read_token():
        log.warning("proxy token %s not readable yet; logins are throttled per 127.0.0.1 until it exists",
                    TOKEN_FILE)
    threads = []
    for srv in servers:
        log.info("gx-playground %s listening on %s://%s:%s", __version__,
                 "https" if isinstance(srv, TLSServer) else "http", *srv.server_address[:2])
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        threads.append(t)
    try:
        while any(t.is_alive() for t in threads):
            for t in threads:
                t.join(timeout=1.0)
    except KeyboardInterrupt:
        pass
    finally:
        for srv in servers:
            srv.RequestHandlerClass.tunnels.shutdown()
            srv.shutdown()
            srv.server_close()
    return 0
