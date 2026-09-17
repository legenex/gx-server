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
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__

log = logging.getLogger("gx.playground")

PKG = Path(__file__).resolve().parent
WEB = Path(os.environ.get("GX_PG_STATIC_DIR", str(PKG.parent / "web")))
UPSTREAM = os.environ.get("GX_PG_UPSTREAM", "http://127.0.0.1:8088")
TOKEN_FILE = Path(os.environ.get("GX_PG_PROXY_TOKEN_FILE",
                                 "/srv/projects/gx-cluster/secrets/control-ui/proxy-token"))
CONTROL_URL = os.environ.get("GX_PG_CONTROL_URL", "http://100.105.214.61:8088/")
MAX_BODY = 160 * 1024 * 1024

#: (methods, path regex) forwarded to the backend. Everything else is 404.
ALLOW: tuple[tuple[frozenset[str], re.Pattern[str]], ...] = tuple(
    (frozenset(m.split(",")), re.compile(p)) for m, p in (
        ("GET", r"/api/(health|session)"),
        ("POST", r"/api/(login|logout)"),
        ("GET,POST", r"/api/media/(assets|jobs|options|upload|delete|zip)(/[A-Za-z0-9_\-]+){0,2}"),
        ("GET,POST", r"/api/music/(model|tags|jobs|upload)(/mus-[0-9a-f]{32}(/(lineage|cancel))?)?"),
        ("GET", r"/api/resources/(summary|explain/gx-[a-z]{3,6}|profile/plan)"),
        ("POST", r"/api/resources/profile"),
        ("GET", r"/api/creative/overview"),
        ("GET,POST,DELETE", r"/v1/music(/[A-Za-z0-9_\-]+){0,2}"),
    ))
REQUEST_HEADERS = ("cookie", "content-type", "content-length", "x-csrf-token", "accept", "accept-encoding",
                   "range", "if-none-match", "x-title", "x-filename")
RESPONSE_HEADERS = ("content-type", "content-length", "content-encoding", "content-range", "accept-ranges",
                    "content-disposition", "cache-control", "set-cookie", "etag", "vary", "location",
                    "retry-after", "last-modified")
SPA_ROUTE = re.compile(r"/(dashboard|images|video|music|library|history)(/[a-z0-9_\-]{0,64}){0,2}")

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
       "media-src 'self' blob:; connect-src 'self'; font-src 'self'; object-src 'none'; "
       "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("image/svg+xml", ".svg")
mimetypes.add_type("application/manifest+json", ".webmanifest")


def allowed(method: str, path: str) -> bool:
    return any(method in methods and rx.fullmatch(path) for methods, rx in ALLOW)


class Static:
    def __init__(self, root: Path) -> None:
        self.files: dict[str, tuple[bytes, bytes | None, str, str]] = {}
        for p in sorted(root.rglob("*")):
            if not p.is_file() or p.name.startswith("."):
                continue
            rel = "/" + p.relative_to(root).as_posix()
            data = p.read_bytes()
            ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/json", "image/svg+xml",
                                                      "application/manifest+json"):
                ctype += "; charset=utf-8"
            gz = gzip.compress(data, 6) if len(data) > 1024 and not ctype.startswith("image/png") else None
            self.files[rel] = (data, gz, ctype, '"' + hashlib.sha256(data).hexdigest()[:20] + '"')


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
    upstream = urllib.parse.urlsplit(UPSTREAM)

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        pass

    # ------------------------------------------------------------ output
    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self._status = status
        self.send_response(status)
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
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
                self._json(200, {"control_center_url": CONTROL_URL, "version": __version__})
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
            if path.startswith(("/api/", "/v1/")) or self._status >= 400:
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
        entry = self.static.files.get(path)
        if entry is None:
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        data, gz, ctype, etag = entry
        headers = {"ETag": etag, "Cache-Control": "no-cache"}
        if self.headers.get("If-None-Match") == etag:
            self._status = 304
            self.send_response(304)
            for k, v in SECURITY_HEADERS.items():
                self.send_header(k, v)
            self.send_header("ETag", etag)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if gz is not None and "gzip" in (self.headers.get("Accept-Encoding") or ""):
            headers.update({"Content-Encoding": "gzip", "Vary": "Accept-Encoding"})
            data = gz
        self._send(200, data, ctype, headers)

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
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
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


def build(hosts: list[str], port: int, static_dir: Path = WEB) -> list[Server]:
    handler = type("BoundHandler", (Handler,), {"static": Static(static_dir), "limiter": RateLimit()})
    servers = []
    for host in hosts:
        try:
            servers.append(Server((host, port), handler))
        except OSError as exc:
            log.warning("cannot bind %s:%s (%s)", host, port, exc)
    if not servers:
        raise RuntimeError(f"could not bind any of {hosts} on port {port}")
    return servers


def main() -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format='{"ts":"%(asctime)s","kind":"log","level":"%(levelname)s","msg":"%(message)s"}')
    hosts = resolve_hosts(os.environ.get("GX_PG_HOSTS", "127.0.0.1,tailscale"))
    port = int(os.environ.get("GX_PG_PORT", "8090"))
    servers = build(hosts, port)
    if not read_token():
        log.warning("proxy token %s not readable yet; logins are throttled per 127.0.0.1 until it exists",
                    TOKEN_FILE)
    threads = []
    for srv in servers:
        log.info("gx-playground %s listening on http://%s:%s", __version__, *srv.server_address[:2])
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
            srv.shutdown()
            srv.server_close()
    return 0
