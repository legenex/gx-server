"""HTTP server for the control UI (V4.1 rebuild).

Security boundaries (ARCHITECTURE-V41.md section 12):

* Binds loopback + this host's Tailscale address only; never a wildcard.
* Every /api route except /api/health, /api/ready, /api/login and
  /api/session requires a valid server-side session.
* Every state-changing request (POST) additionally requires the session's
  CSRF token in X-CSRF-Token and a same-origin Origin/Referer, and the
  session cookie is HttpOnly + SameSite=Strict.
* Strict CSP (no inline script, no third-party origins), frame-ancestors
  none, nosniff, no-referrer, no caching of API responses.
* Request bodies are size-limited and JSON-only; responses never contain an
  upstream credential.
* Retired V3 endpoints answer 410 Gone with a JSON hint, so old frontends
  degrade loudly instead of silently.

The media / music / voice / call / live / flows / playground stacks are
PERMANENTLY RETIRED (old code: git tag pre-deepseek-v41-rebuild-20260927).
"""

from __future__ import annotations

import gzip
import hashlib
import secrets
import json
import logging
import mimetypes
import os
import stat
import re
import socket
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from collections.abc import Callable

from . import __version__
from . import logs as logstreams
from .actions import ActionRefused, ActionRunner
from .agentos_adapter import AgentOSAdapter
from .agents_view import agents_view, tasks_view
from .auth import COOKIE_NAME, SECURE_COOKIE_NAME, AuthError, LoginThrottle, PasswordStore, SessionManager, csrf_ok
from .config import UIConfig
from .docs import DocLibrary
from .filemanager import FileManager, FileManagerError, PURGE_TOKEN
from .models import ResultLog, live_state, read_registry, registry_view
from .netview import NetView
from .projects_scanner import ProjectsScanner
from .recovery_view import RecoveryView
from .redact import redact
from .requests_view import RequestsError, cancel as req_cancel, history as req_history, retry as req_retry, status as req_status
from .resources import AdmissionBlocked, ResourceError, ResourceController
from .services import Cluster
from .setup import KEY_RE as _setup_key_re
from . import setup as client_setup
from . import views
from .api_keys import KeyError_, KeyManager
from .api_keys import probe as key_probe
from .storage_scanner import StorageScanner
from .sse import SSEHub
from .updates_view import UpdatesView

log = logging.getLogger("gx.ui")

MAX_BODY = 64 * 1024
#: The file manager upload cap (ARCHITECTURE-V41.md section 7: 512 MB).
MAX_UPLOAD = 512 * 1024 * 1024
UPLOAD_PATH = "/api/files/upload"
#: Raw-body uploads, streamed by their route after authentication.
UPLOAD_PATHS = frozenset({UPLOAD_PATH})
#: Client-side routes that fall back to index.html (V4.1 page set).
_SPA_ROUTE = re.compile(
    r"/(dashboard|models|performance|requests|agents|tasks|projects|files|storage|logs|network|"
    r"updates|settings|recovery|jobs|keys|login|docs|cluster|resources|backup|setup)"
    r"(/[a-z0-9_\-]{0,64}){0,2}"
)
#: Retired Build V3 prefixes: loud 410 Gone with a JSON hint.
RETIRED_PREFIXES = (
    "/api/playground", "/api/media", "/api/music", "/api/voice", "/api/creative",
    "/api/manager", "/v1/music", "/v1/voice", "/v1/flows", "/v1/assets", "/v1/flow-runs",
    "/api/setup/openwebui",
)
_IP_RE = re.compile(r"^[0-9a-fA-F:.]{2,45}$")

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
    "media-src 'self' blob:; connect-src 'self'; font-src 'self'; object-src 'none'; "
    "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENIED",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}

mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("image/svg+xml", ".svg")
mimetypes.add_type("application/manifest+json", ".webmanifest")


class StaticFiles:
    """The `web/` tree, cached in memory but re-read whenever a file changes.

    Every lookup stats the file and rebuilds the entry when its mtime, size
    or inode changed; a path that did not exist at start-up is picked up the
    first time it is requested, and a deleted file becomes a 404.

    Set `GX_UI_STATIC_FREEZE=1` to keep the start-up snapshot.
    """

    Entry = tuple[bytes, bytes | None, str, str]

    def __init__(self, root: Path, *, freeze: bool | None = None) -> None:
        self.root = Path(root).resolve()
        self.freeze = (os.environ.get("GX_UI_STATIC_FREEZE") == "1") if freeze is None else freeze
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[tuple[int, int, int] | None, StaticFiles.Entry | None]] = {}
        self.reload()

    def reload(self) -> None:
        with self._lock:
            self._cache.clear()
        for path in sorted(self.root.rglob("*")):
            if path.is_file() and not path.name.startswith("."):
                self.get("/" + path.relative_to(self.root).as_posix())

    @property
    def files(self) -> dict[str, StaticFiles.Entry]:
        with self._lock:
            return {k: v for k, (_sig, v) in self._cache.items() if v is not None}

    def _path_of(self, path: str) -> Path | None:
        if not path.startswith("/") or "\x00" in path:
            return None
        parts = [seg for seg in path[1:].split("/") if seg]
        if not parts or any(seg in (".", "..") or seg.startswith(".") for seg in parts):
            return None
        candidate = self.root.joinpath(*parts)
        try:
            candidate.resolve().relative_to(self.root)
        except (OSError, ValueError):
            return None
        return candidate

    @staticmethod
    def _build(path: Path, data: bytes) -> StaticFiles.Entry:
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/json", "image/svg+xml",
                                                  "application/manifest+json"):
            ctype += "; charset=utf-8"
        gz = gzip.compress(data, 6) if len(data) > 1024 and not ctype.startswith("image/png") else None
        return (data, gz, ctype, '"' + hashlib.sha256(data).hexdigest()[:20] + '"')

    def get(self, path: str) -> StaticFiles.Entry | None:
        with self._lock:
            cached = self._cache.get(path)
        if cached is not None and self.freeze:
            return cached[1]
        target = self._path_of(path)
        if target is None:
            return None
        try:
            st = target.stat()
            if not stat.S_ISREG(st.st_mode):
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
            data = target.read_bytes()
        except OSError:
            return cached[1] if cached else None
        entry = self._build(target, data)
        with self._lock:
            self._cache[path] = (sig, entry)
        return entry


class App:
    """Everything a request handler needs, built once."""

    def __init__(self, cfg: UIConfig) -> None:
        self.cfg = cfg
        self.started = time.time()
        self.store = PasswordStore(cfg.password_file)
        self.acceptance = PasswordStore(cfg.acceptance_file)
        self.sessions = SessionManager(cfg.session_idle_seconds, cfg.session_max_seconds)
        self.throttle = LoginThrottle()
        self.cluster = Cluster(cfg)
        self.results = ResultLog(cfg.state_dir / "model-results.json")
        self.actions = ActionRunner(cfg, self.cluster, self.results)
        self.docs = DocLibrary(cfg.docs_dir)
        self.static = StaticFiles(cfg.static_dir)
        self.filemanager = FileManager(cfg, audit=self.actions.audit)
        self.agentos = AgentOSAdapter(cfg)
        self.projects = ProjectsScanner(cfg)
        self.storage_scanner = StorageScanner(cfg)
        self.netview = NetView(cfg)
        self.updates = UpdatesView(cfg)
        self.recovery = RecoveryView(cfg)
        self.sse = SSEHub(cfg, self.cluster)
        self.resources = ResourceController(cfg, self.cluster, self.actions, audit=self.actions.audit)
        self.actions.maintenance = self.resources.maintenance
        self.actions.filemanager = self.filemanager
        self.actions.updates = self.updates
        self.keys = KeyManager(cfg.litellm_base, lambda: cfg.secret("LITELLM_MASTER_KEY"))
        self.api_rate: dict[str, list[float]] = {}
        self._gen_seen: int | None = None
        self._gen_lock = threading.Lock()

    def generation(self) -> int:
        try:
            gen = self.store.generation()
        except (AuthError, ValueError, OSError):
            gen = -1
        with self._gen_lock:
            if self._gen_seen is not None and gen != self._gen_seen:
                self.sessions.clear()
            self._gen_seen = gen
        return gen


Route = tuple[str, "re.Pattern[str]", Callable[..., None], str]  # method, pattern, fn, access


class Handler(BaseHTTPRequestHandler):
    server_version = "gx-control-ui"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    app: App
    routes: list[Route] = []

    # ------------------------------------------------------------ plumbing
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        pass  # replaced by structured access logging in _finish()

    def _client_ip(self) -> str:
        return self.client_address[0] if self.client_address else "?"

    def _cookie_token(self) -> str | None:
        raw = self.headers.get("Cookie") or ""
        found: dict[str, str] = {}
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k in (COOKIE_NAME, SECURE_COOKIE_NAME) and k not in found:
                found[k] = v
        if self._is_https() and found.get(SECURE_COOKIE_NAME):
            return found[SECURE_COOKIE_NAME]
        return found.get(COOKIE_NAME)

    def _is_https(self) -> bool:
        return self.headers.get("X-Forwarded-Proto", "").lower() == "https"

    def _cookie(self, token: str, max_age: int) -> str:
        https = self._is_https()
        secure = self.app.cfg.cookie_secure == "always" or (self.app.cfg.cookie_secure == "auto" and https)
        parts = [f"{COOKIE_NAME}={token}", "HttpOnly", "SameSite=Strict", "Path=/", f"Max-Age={max_age}"]
        if secure:
            parts.append("Secure")
        return "; ".join(parts)

    def _send(self, status: int, body: bytes, ctype: str, extra: dict[str, Any] | None = None) -> None:
        self._status = status
        self.send_response(status)
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            for item in (v if isinstance(v, list) else [v]):
                self.send_header(k, item)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: Any, extra: dict[str, Any] | None = None) -> None:
        body = json.dumps(payload, default=str, separators=(",", ":")).encode("utf-8")
        headers: dict[str, Any] = {"Cache-Control": "no-store"}
        headers.update(extra or {})
        if len(body) > 2048 and "gzip" in (self.headers.get("Accept-Encoding") or ""):
            body = gzip.compress(body, 5)
            headers["Content-Encoding"] = "gzip"
            headers["Vary"] = "Accept-Encoding"
        self._send(status, body, "application/json; charset=utf-8", headers)

    def _error(self, status: int, message: str, code: str = "error") -> None:
        self._json(status, {"error": {"message": redact(message), "code": code}})

    def _consume_body(self, path: str) -> None:
        """Read the whole request body up front (bounded)."""
        self._raw = b""
        self._body_error: Exception | None = None
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self.close_connection = True
            self._body_error = ValueError("invalid Content-Length")
            return
        if path in UPLOAD_PATHS:
            # Streamed to a temp file by the route itself (after
            # authentication and CSRF), never read into memory here.
            self.close_connection = True
            self._upload_length = length
            if length <= 0 or length > MAX_UPLOAD:
                self.close_connection = True
                self._body_error = OverflowError(f"upload must be 1 byte to {MAX_UPLOAD // 2**20} MiB")
            return
        if length < 0 or length > MAX_BODY:
            self.close_connection = True
            self._body_error = OverflowError(f"request body exceeds {MAX_BODY} bytes")
            return
        if length:
            self._raw = self.rfile.read(length)

    def _body(self, limit: int) -> dict:
        if self._body_error is not None:
            raise self._body_error
        if len(self._raw) > limit:
            raise OverflowError(f"request body exceeds {limit} bytes")
        if not self._raw:
            return {}
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise ValueError("Content-Type must be application/json")
        data = json.loads(self._raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("JSON body must be an object")
        return data

    def _same_origin(self) -> bool:
        host = self.headers.get("Host") or ""
        origin = self.headers.get("Origin")
        if origin:
            return urllib.parse.urlsplit(origin).netloc == host
        referer = self.headers.get("Referer")
        if referer:
            return urllib.parse.urlsplit(referer).netloc == host
        # Non-browser clients send neither; the CSRF token still applies.
        return True

    # ------------------------------------------------------------- dispatch
    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._reject_method()

    def do_PATCH(self) -> None:  # noqa: N802
        self._reject_method()

    def do_DELETE(self) -> None:  # noqa: N802
        self._reject_method()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._reject_method()

    def _reject_method(self) -> None:
        self.close_connection = True
        self._t0 = time.time()
        self._status = 405
        self._error(405, "method not allowed", "method_not_allowed")
        self._finish(None)

    def _dispatch(self, method: str) -> None:
        self._t0 = time.time()
        self._status = 0
        self.session = None
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        self.query = urllib.parse.parse_qs(parsed.query, max_num_fields=20)
        self._raw, self._body_error = b"", None
        if method == "POST":
            self._consume_body(path)
        try:
            if path.startswith(RETIRED_PREFIXES):
                self._gone(path)
                return
            if not path.startswith("/api/"):
                self._static(path)
                return
            for r_method, pattern, fn, access in self.routes:
                m = pattern.fullmatch(path)
                if not m or r_method != method:
                    continue
                if access != "public":
                    gen = self.app.generation()
                    self.session = self.app.sessions.get(self._cookie_token(), gen)
                    if self.session is None:
                        self._error(401, "authentication required", "unauthenticated")
                        return
                    if method == "POST":
                        if not self._same_origin():
                            self._error(403, "cross-origin request refused", "bad_origin")
                            return
                        if not csrf_ok(self.session, self.headers.get("X-CSRF-Token")):
                            self._error(403, "missing or invalid CSRF token", "csrf")
                            return
                elif method == "POST" and not self._same_origin():
                    self._error(403, "cross-origin request refused", "bad_origin")
                    return
                fn(self, **m.groupdict())
                return
            known = any(p.fullmatch(path) for _, p, _, _ in self.routes)
            self._error(405 if known else 404, "method not allowed" if known else "not found",
                        "method_not_allowed" if known else "not_found")
        except (ValueError, json.JSONDecodeError) as exc:
            self._error(400, str(exc), "invalid_request")
        except OverflowError as exc:
            self._error(413, str(exc), "too_large")
        except ActionRefused as exc:
            self._error(exc.status, str(exc), "refused")
        except AdmissionBlocked as exc:
            self._json(exc.status, {"error": {"message": redact(str(exc)), "code": "admission"},
                                    "admission": exc.view})
        except (KeyError_, ResourceError, FileManagerError, RequestsError) as exc:
            self._error(exc.status, str(exc), getattr(exc, "code", type(exc).__name__.lower().rstrip("_")))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001
            log.exception("unhandled error on %s %s", method, path)
            if not self._status:
                self._error(500, f"internal error: {type(exc).__name__}", "internal")
        finally:
            self._finish(path)

    def _gone(self, path: str) -> None:
        """410 Gone with a JSON hint for every retired Build V3 endpoint."""
        hint = ("This endpoint belonged to the retired pre-V4.1 stack (media, music, voice, "
                "call, live, flows, playground, per-tier model manager). Old code lives under "
                "git tag pre-deepseek-v41-rebuild-20260927; the V4.1 API is documented in "
                "state/WORKER-C-REPORT.md.")
        self._json(410, {"error": {"message": hint, "code": "gone"}, "retired": path})

    def _send_file(self, path: Path, ctype: str, extra: dict[str, str] | None = None) -> None:
        size = path.stat().st_size
        self._status = 200
        self.send_response(200)
        for k, v in {**SECURITY_HEADERS, "Accept-Ranges": "bytes", **(extra or {})}.items():
            self.send_header(k, v)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.end_headers()
        if self.command == "HEAD":
            return
        with path.open("rb") as fh:
            while True:
                chunk = fh.read(1 << 20)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def _finish(self, path: str | None) -> None:
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "kind": "access",
            "method": self.command, "path": path, "status": int(getattr(self, "_status", 0) or 0),
            "ms": round((time.time() - getattr(self, "_t0", time.time())) * 1000),
            "ip": self._client_ip(),
            "user": getattr(getattr(self, "session", None), "username", None),
        }
        if ACCESS_LOG and path and (path.startswith("/api/") or int(entry["status"] or 0) >= 400):
            print(json.dumps(entry), flush=True)

    # --------------------------------------------------------------- static
    def _static(self, path: str) -> None:
        files = self.app.static
        if path == "/" or _SPA_ROUTE.fullmatch(path):
            path = "/index.html"
        entry = files.get(path)
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
            headers["Content-Encoding"] = "gzip"
            headers["Vary"] = "Accept-Encoding"
            data = gz
        self._send(200, data, ctype, headers)


# =============================================================== routes
ACCESS_LOG = os.environ.get("GX_UI_ACCESS_LOG", "1") != "0"


def route(method: str, pattern: str, access: str = "session"):
    def deco(fn):
        Handler.routes.append((method, re.compile(pattern), fn, access))
        return fn
    return deco


def _q(h: Handler, key: str, default: str = "") -> str:
    return (h.query.get(key) or [default])[0]


def _float(h: Handler, key: str) -> float | None:
    raw = _q(h, key)
    try:
        return float(raw) if raw else None
    except ValueError:
        raise ValueError(f"{key} must be a number")


@route("GET", r"/api/health", "public")
def api_health(h: Handler) -> None:
    h._json(200, {"status": "ok", "service": "gx-control-ui", "version": __version__})


@route("GET", r"/api/ready", "public")
def api_ready(h: Handler) -> None:
    problems = []
    if not h.app.store.configured():
        problems.append("no admin password configured")
    if "/index.html" not in h.app.static.files:
        problems.append("static assets missing")
    if not h.app.docs.pages():
        problems.append("documentation missing")
    h._json(200 if not problems else 503, {"ready": not problems, "problems": problems,
                                           "uptime_seconds": round(time.time() - h.app.started)})


@route("GET", r"/api/session", "public")
def api_session(h: Handler) -> None:
    sess = h.app.sessions.get(h._cookie_token(), h.app.generation())
    if sess is None:
        h._json(200, {"authenticated": False, "configured": h.app.store.configured()})
        return
    h._json(200, {"authenticated": True, "user": sess.username, "csrf": sess.csrf,
                  "expires_in": round(min(h.app.cfg.session_idle_seconds,
                                          h.app.cfg.session_max_seconds - (time.time() - sess.created)))})


@route("POST", r"/api/login", "public")
def api_login(h: Handler) -> None:
    ip = h._client_ip()
    wait = h.app.throttle.blocked(ip)
    if wait > 0:
        h.app.actions.audit(user=None, ip=ip, action="login", outcome="throttled")
        h._error(429, f"too many failed logins; try again in {int(wait) + 1} s", "throttled")
        return
    body = h._body(4096)
    username = body.get("username")
    password = body.get("password")
    if not isinstance(username, str) or not isinstance(password, str) or len(password) > 1024:
        h._error(400, "username and password are required", "invalid_request")
        return
    if not h.app.store.configured():
        h._error(503, "no admin password is configured; run gx-ui-passwd on gx10-01", "not_configured")
        return
    is_acceptance = username.strip().lower() == h.app.cfg.ACCEPTANCE_USER
    if is_acceptance:
        # The acceptance account exists for automated live tests on gx10-01
        # and is refused from anywhere but the loopback interface (D-035).
        ok = ip in ("127.0.0.1", "::1") and h.app.acceptance.check(username, password)
    else:
        ok = h.app.store.check(username, password)
    if not ok:
        h.app.throttle.failure(ip)
        h.app.actions.audit(user=username[:32], ip=ip, action="login", outcome="failed")
        h._error(401, "invalid username or password", "bad_credentials")
        return
    h.app.throttle.success(ip)
    token, sess = h.app.sessions.create(username.strip().lower(), h.app.generation(), ip)
    h.app.actions.audit(user=sess.username, ip=ip, action="login", outcome="ok")
    h.session = sess
    h._json(200, {"authenticated": True, "user": sess.username, "csrf": sess.csrf},
            {"Set-Cookie": h._cookie(token, h.app.cfg.session_max_seconds)})


@route("POST", r"/api/logout")
def api_logout(h: Handler) -> None:
    assert h.session is not None  # noqa: S101 - guaranteed by _dispatch for session routes
    h.app.sessions.destroy(h._cookie_token())
    h.app.actions.audit(user=h.session.username, ip=h._client_ip(), action="logout", outcome="ok")
    cookies = [h._cookie("", 0)]
    if h._is_https():
        cookies.append(f"{COOKIE_NAME}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")
    h._json(200, {"authenticated": False}, {"Set-Cookie": cookies})


@route("GET", r"/api/overview")
def api_overview(h: Handler) -> None:
    h._json(200, views.overview(h.app))


@route("GET", r"/api/nodes")
def api_nodes(h: Handler) -> None:
    h._json(200, views.nodes(h.app))


@route("GET", r"/api/cluster")
def api_cluster(h: Handler) -> None:
    h._json(200, views.cluster(h.app))


@route("GET", r"/api/models")
def api_models(h: Handler) -> None:
    reg = read_registry(h.app.cfg.registry_path)
    h._json(200, {"models": live_state(h.app.cluster, reg),
                  "registry": registry_view(h.app.cluster, h.app.cfg),
                  "actions": [s.public() for s in h.app.actions.registry.values()
                              if s.name.startswith("gxmax")],
                  "running_jobs": h.app.actions.running()})


@route("GET", r"/api/jobs")
def api_jobs(h: Handler) -> None:
    h._json(200, views.jobs(h.app))


@route("GET", r"/api/actions")
def api_actions(h: Handler) -> None:
    h._json(200, {"actions": [s.public() for s in h.app.actions.registry.values()],
                  "jobs": h.app.actions.jobs()})


@route("GET", r"/api/actions/jobs/(?P<job_id>[a-f0-9]{16})")
def api_action_job(h: Handler, job_id: str) -> None:
    job = h.app.actions.job(job_id)
    if job is None:
        h._error(404, "no such job", "not_found")
    else:
        h._json(200, job)


@route("POST", r"/api/actions/(?P<name>[a-z0-9_.\-]{3,64})")
def api_action_run(h: Handler, name: str) -> None:
    assert h.session is not None  # noqa: S101
    body = h._body(MAX_BODY)
    spec = h.app.actions.registry.get(name)
    allowed_args = set(spec.args) if spec else set()
    extra = {k for k in body if k not in allowed_args and k != "confirm"}
    if extra:
        raise ActionRefused(f"unknown argument for {name}: {', '.join(sorted(extra))[:120]}", 400)
    args = {k: body[k] for k in body if k in allowed_args}
    job = h.app.actions.submit(name, user=h.session.username, ip=h._client_ip(),
                               confirm=body.get("confirm"), args=args)
    h._json(202, job.as_dict())


@route("GET", r"/api/logs")
def api_logs(h: Handler) -> None:
    h._json(200, {"streams": [s.as_dict() for s in logstreams.streams(h.app.cfg)],
                  "limits": {"min": logstreams.MIN_LINES, "max": logstreams.MAX_LINES,
                             "default": logstreams.DEFAULT_LINES}})


@route("GET", r"/api/logs/(?P<stream_id>[a-z0-9\-]{2,40})")
def api_log(h: Handler, stream_id: str) -> None:
    lines = (h.query.get("lines") or [str(logstreams.DEFAULT_LINES)])[0]
    q = (h.query.get("q") or [""])[0]
    try:
        data = logstreams.read_stream(h.app.cfg, stream_id, lines, q)
    except KeyError:
        h._error(404, "unknown log stream", "not_found")
        return
    if (h.query.get("format") or [""])[0] == "text":
        text = "\n".join(data["lines"]) + "\n"
        fname = f"gx-{stream_id}-{time.strftime('%Y%m%d-%H%M%S')}.log"
        h._send(200, text.encode("utf-8"), "text/plain; charset=utf-8",
                {"Content-Disposition": f'attachment; filename="{fname}"', "Cache-Control": "no-store"})
        return
    h._json(200, data)


@route("GET", r"/api/system")
def api_system(h: Handler) -> None:
    h._json(200, views.system(h.app))


@route("GET", r"/api/docs")
def api_docs(h: Handler) -> None:
    q = (h.query.get("q") or [""])[0]
    if q:
        h._json(200, {"results": h.app.docs.search(q)})
    else:
        h._json(200, {"pages": h.app.docs.index(),
                      "gateway_url": h.app.cfg.public_gateway_url})


@route("GET", r"/api/docs/(?P<slug>[a-z0-9\-]{1,64})")
def api_doc(h: Handler, slug: str) -> None:
    page = h.app.docs.get(slug)
    if page is None:
        h._error(404, "no such page", "not_found")
    else:
        h._json(200, page)


# ============================================================ requests
@route("GET", r"/api/requests")
def api_requests(h: Handler) -> None:
    snap = req_status(h.app.cfg)
    hist = req_history(h.app.cfg,
                       project=_q(h, "project"), agent=_q(h, "agent"), profile=_q(h, "profile"),
                       reasoning=_q(h, "reasoning"), state=_q(h, "state"),
                       since=_float(h, "since"), until=_float(h, "until"),
                       limit=int(_q(h, "limit", "200") or 200))
    h._json(200, {"status": snap, "history": hist})


@route("POST", r"/api/requests/(?P<request_id>[A-Za-z0-9_\-]{1,128})/cancel")
def api_request_cancel(h: Handler, request_id: str) -> None:
    assert h.session is not None  # noqa: S101
    result = req_cancel(h.app.cfg, request_id)
    h.app.actions.audit(user=h.session.username, ip=h._client_ip(), action="requests.cancel",
                       outcome="ok", request_id=request_id)
    h._json(200, result)


@route("POST", r"/api/requests/(?P<request_id>[A-Za-z0-9_\-]{1,128})/retry")
def api_request_retry(h: Handler, request_id: str) -> None:
    assert h.session is not None  # noqa: S101
    result = req_retry(h.app.cfg, request_id)
    h.app.actions.audit(user=h.session.username, ip=h._client_ip(), action="requests.retry",
                       outcome="ok", request_id=request_id)
    h._json(200, result)


# ============================================================== agents
@route("GET", r"/api/agents")
def api_agents(h: Handler) -> None:
    h._json(200, agents_view(h.app.cfg, h.app.agentos, h.app.cluster.scheduler_snapshot(max_age=10)))


@route("GET", r"/api/agents/tasks")
def api_agent_tasks(h: Handler) -> None:
    h._json(200, tasks_view(h.app.cfg, h.app.agentos, h.app.cluster.scheduler_snapshot(max_age=10)))


# ============================================================ projects
@route("GET", r"/api/projects")
def api_projects(h: Handler) -> None:
    h._json(200, h.app.projects.scan(h.app.cluster.scheduler_snapshot(max_age=10)))


@route("POST", r"/api/projects/(?P<name>[A-Za-z0-9._\-]{1,120})/size")
def api_project_size(h: Handler, name: str) -> None:
    assert h.session is not None  # noqa: S101
    h._json(200, h.app.projects.refresh_size(name))


# ================================================== file manager + trash
@route("GET", r"/api/files/roots")
def api_files_roots(h: Handler) -> None:
    h._json(200, {"roots": list(h.app.cfg.file_roots),
                  "protected": list(h.app.cfg.file_protected),
                  "max_upload_bytes": MAX_UPLOAD})


@route("GET", r"/api/files/browse")
def api_files_browse(h: Handler) -> None:
    h._json(200, h.app.filemanager.browse(_q(h, "path")))


@route("GET", r"/api/files/search")
def api_files_search(h: Handler) -> None:
    h._json(200, h.app.filemanager.search(_q(h, "root"), _q(h, "q"),
                                          int(_q(h, "limit", "200") or 200)))


@route("GET", r"/api/files/dirsize")
def api_files_dirsize(h: Handler) -> None:
    h._json(200, h.app.filemanager.dirsize(_q(h, "path")))


@route("GET", r"/api/files/preview")
def api_files_preview(h: Handler) -> None:
    h._json(200, h.app.filemanager.preview(_q(h, "path")))


@route("GET", r"/api/files/download")
def api_files_download(h: Handler) -> None:
    path = h.app.filemanager.download(_q(h, "path"))
    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    h._send_file(path, ctype, {
        "Content-Disposition": f'attachment; filename="{path.name[:120]}"',
        "Cache-Control": "no-store"})


@route("POST", r"/api/files/upload")
def api_files_upload(h: Handler) -> None:
    assert h.session is not None  # noqa: S101
    if h._body_error is not None:
        raise h._body_error
    target_dir = urllib.parse.unquote(h.headers.get("X-Path") or "")[:400]
    filename = urllib.parse.unquote(h.headers.get("X-Filename") or "")[:200]

    def writer(tmp: Path) -> int:
        if h._body_error is not None:
            raise h._body_error
        remaining = getattr(h, "_upload_length", 0)
        written = 0
        with tmp.open("wb") as fh:
            while remaining > 0:
                chunk = h.rfile.read(min(1 << 20, remaining))
                if not chunk:
                    raise ValueError("the upload was interrupted")
                fh.write(chunk)
                remaining -= len(chunk)
                written += len(chunk)
        return written

    h._json(200, h.app.filemanager.upload(target_dir, filename, writer))


@route("POST", r"/api/files/rename")
def api_files_rename(h: Handler) -> None:
    assert h.session is not None  # noqa: S101
    body = h._body(MAX_BODY)
    h._json(200, h.app.filemanager.rename(str(body.get("path") or ""), str(body.get("name") or "")))


@route("POST", r"/api/files/move")
def api_files_move(h: Handler) -> None:
    assert h.session is not None  # noqa: S101
    body = h._body(MAX_BODY)
    h._json(200, h.app.filemanager.move(str(body.get("path") or ""), str(body.get("to") or "")))


@route("POST", r"/api/files/mkdir")
def api_files_mkdir(h: Handler) -> None:
    assert h.session is not None  # noqa: S101
    h._json(200, h.app.filemanager.mkdir(h._body(MAX_BODY).get("path", "")))


@route("POST", r"/api/files/delete")
def api_files_delete(h: Handler) -> None:
    assert h.session is not None  # noqa: S101
    body = h._body(MAX_BODY)
    result = h.app.filemanager.delete(str(body.get("path") or ""), user=h.session.username)
    h._json(200, result)


@route("GET", r"/api/files/trash")
def api_files_trash(h: Handler) -> None:
    h._json(200, h.app.filemanager.trash_list())


@route("POST", r"/api/files/trash/restore")
def api_files_trash_restore(h: Handler) -> None:
    assert h.session is not None  # noqa: S101
    body = h._body(MAX_BODY)
    h._json(200, h.app.filemanager.restore(str(body.get("id") or ""), user=h.session.username))


@route("POST", r"/api/files/trash/purge")
def api_files_trash_purge(h: Handler) -> None:
    """Direct purge (same typed token as the action); both paths audit."""
    assert h.session is not None  # noqa: S101
    body = h._body(MAX_BODY)
    if body.get("confirm") != PURGE_TOKEN:
        raise FileManagerError(f"type '{PURGE_TOKEN}' to purge the trash", 400, "confirm")
    h._json(200, h.app.filemanager.purge(user=h.session.username))


# ============================================================== storage
@route("GET", r"/api/storage")
def api_storage(h: Handler) -> None:
    h._json(200, h.app.storage_scanner.view(h.app.cluster))


@route("POST", r"/api/storage/scan")
def api_storage_scan(h: Handler) -> None:
    assert h.session is not None  # noqa: S101
    h._json(200, h.app.storage_scanner.view(h.app.cluster, fresh=True))


# ============================================================== network
@route("GET", r"/api/network")
def api_network(h: Handler) -> None:
    h._json(200, h.app.netview.view())


@route("GET", r"/api/network/diagnostics")
def api_network_diagnostics(h: Handler) -> None:
    h._json(200, h.app.netview.diagnostics())


# =============================================================== updates
@route("GET", r"/api/updates")
def api_updates(h: Handler) -> None:
    h._json(200, h.app.updates.view())


@route("POST", r"/api/updates/check")
def api_updates_check(h: Handler) -> None:
    assert h.session is not None  # noqa: S101
    h._json(200, h.app.updates.check())


# ============================================================== recovery
@route("GET", r"/api/recovery")
def api_recovery(h: Handler) -> None:
    h._json(200, h.app.recovery.view(h.app.cluster))


# ================================================================== SSE
@route("GET", r"/api/stream")
def api_stream(h: Handler) -> None:
    q = h.app.sse.subscribe()
    h._status = 200
    h.send_response(200)
    for k, v in SECURITY_HEADERS.items():
        h.send_header(k, v)
    h.send_header("Content-Type", "text/event-stream; charset=utf-8")
    h.send_header("Cache-Control", "no-store")
    h.send_header("X-Accel-Buffering", "no")
    h.send_header("Connection", "close")
    h.close_connection = True
    h.end_headers()
    try:
        h.wfile.write(b"retry: 5000\n\n")
        h.wfile.flush()
        import queue as _queue
        while True:
            try:
                event = q.get(timeout=25)
            except _queue.Empty:
                h.wfile.write(b": keepalive\n\n")
                h.wfile.flush()
                continue
            data = json.dumps(event, separators=(",", ":"))
            h.wfile.write(f"event: {event['type']}\ndata: {data}\n\n".encode())
            h.wfile.flush()
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    finally:
        h.app.sse.unsubscribe(q)


# ============================================================ resources
@route("GET", r"/api/resources")
def api_resources(h: Handler) -> None:
    snap = h.app.resources.snapshot()
    snap["admission"] = h.app.resources.admission(snap)
    h._json(200, snap)


@route("GET", r"/api/resources/profile/plan")
def api_profile_plan(h: Handler) -> None:
    target = _q(h, "to")
    from .resources import PROFILE_IDS
    if target not in PROFILE_IDS:
        raise ValueError("unknown profile")
    h._json(200, h.app.resources.plan_profile(target))


@route("POST", r"/api/resources/profile")
def api_profile_set(h: Handler) -> None:
    assert h.session is not None  # noqa: S101
    body = h._body(MAX_BODY)
    target = body.get("profile")
    from .resources import PROFILE_IDS
    if target not in PROFILE_IDS:
        raise ValueError("unknown profile")
    h._json(200, h.app.resources.set_profile(target, user=h.session.username, ip=h._client_ip(),
                                             confirm=body.get("confirm")))


@route("POST", r"/api/resources/gxmax/(?P<op>start|stop|restart|drain)")
def api_resource_gxmax(h: Handler, op: str) -> None:
    assert h.session is not None  # noqa: S101
    body = h._body(MAX_BODY)
    job = h.app.actions.submit(f"gxmax_{op}", user=h.session.username, ip=h._client_ip(),
                               confirm=body.get("confirm"), args={"profile": str(body.get("profile") or "")}
                               if op in ("start", "restart") else None)
    h._json(202, job.as_dict(with_output=False))


@route("POST", r"/api/resources/gxmax/pin")
def api_resource_gxmax_pin(h: Handler) -> None:
    assert h.session is not None  # noqa: S101
    body = h._body(MAX_BODY)
    on = body.get("pinned") is True
    h._json(200, h.app.resources.set_pin("gx-max", on, user=h.session.username, ip=h._client_ip()))


# ================================================================ setup
@route("GET", r"/api/setup")
def api_setup(h: Handler) -> None:
    h._json(200, client_setup.setup_info(h.app.cfg))


@route("GET", r"/api/connections")
def api_connections(h: Handler) -> None:
    h._json(200, client_setup.connections_info(h.app.cfg, reveal=False))


@route("POST", r"/api/connections/key/reveal")
def api_connections_reveal(h: Handler) -> None:
    assert h.session is not None  # noqa: S101
    h._body(256)
    info = client_setup.connections_info(h.app.cfg, reveal=True)
    h.app.actions.audit(user=h.session.username, ip=h._client_ip(), action="connections.key.reveal",
                        outcome="ok" if info.get("api_key", {}).get("revealed") else "failed")
    h._json(200, {"api_key": info["api_key"]})


@route("POST", r"/api/connections/test")
def api_connections_test(h: Handler) -> None:
    assert h.session is not None  # noqa: S101
    body = h._body(4096)
    target = str(body.get("target") or "gateway")
    result = client_setup.test_live(h.app.cfg, target)
    h.app.actions.audit(user=h.session.username, ip=h._client_ip(), action=f"connections.test.{target}",
                        outcome="ok" if result.get("ok") else "failed")
    h._json(200, result)


@route("POST", r"/api/setup/test")
def api_setup_test(h: Handler) -> None:
    assert h.session is not None  # noqa: S101
    body = h._body(4096)
    client = body.get("client")
    result = client_setup.test_connection(h.app.cfg, str(client), body.get("secret"))
    h.app.actions.audit(user=h.session.username, ip=h._client_ip(), action=f"setup.test.{client}",
                        outcome="ok" if result.get("connected") else "failed")
    h._json(200, result)


# ================================================================ API keys
@route("GET", r"/api/keys")
def api_keys(h: Handler) -> None:
    h._json(200, {"keys": h.app.keys.list(), "gateway_url": h.app.cfg.public_gateway_url})


@route("POST", r"/api/keys")
def api_keys_create(h: Handler) -> None:
    assert h.session is not None  # noqa: S101
    created = h.app.keys.create(h._body(MAX_BODY), user=h.session.username)
    h.app.actions.audit(user=h.session.username, ip=h._client_ip(), action="keys.create", outcome="ok",
                        key=created.get("id"), name=created.get("name"), models=created.get("models"))
    h._json(200, created)


@route("POST", r"/api/keys/(?P<key_id>[0-9a-f]{32,128})/(?P<op>revoke|replace)")
def api_keys_op(h: Handler, key_id: str, op: str) -> None:
    assert h.session is not None  # noqa: S101
    body = h._body(MAX_BODY)
    if body.get("confirm") is not True:
        raise ValueError(f"{op} must be confirmed")
    result = h.app.keys.revoke(key_id) if op == "revoke" else h.app.keys.replace(key_id, user=h.session.username)
    h.app.actions.audit(user=h.session.username, ip=h._client_ip(), action=f"keys.{op}", outcome="ok", key=key_id)
    h._json(200, result)


@route("POST", r"/api/keys/test")
def api_keys_test(h: Handler) -> None:
    body = h._body(4096)
    secret = body.get("secret")
    model = body.get("model", "gx-auto")
    if not isinstance(secret, str) or not _setup_key_re.fullmatch(secret):
        raise ValueError("paste a gateway key (sk-...)")
    from .services import PUBLIC_ALIASES
    if model not in PUBLIC_ALIASES:
        raise ValueError("test with gx-max or gx-auto")
    h._json(200, key_probe(h.app.cfg.litellm_base, secret, model))


# ============================================================ bootstrap
class JsonFormatter(logging.Formatter):
    """One JSON object per line, credentials redacted."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(record.created)),
                 "kind": "log", "level": record.levelname, "logger": record.name,
                 "msg": redact(record.getMessage())}
        if record.exc_info:
            entry["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(entry)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def __init__(self, addr, handler):
        if ":" in addr[0]:
            self.address_family = socket.AF_INET6
        super().__init__(addr, handler)


from . import routes_backup  # noqa: E402,F401  (GX Backup status / run / verify)


def build(cfg: UIConfig) -> tuple[App, list[Server]]:
    app = App(cfg)
    handler = type("BoundHandler", (Handler,), {"app": app})
    servers = []
    for host in cfg.hosts:
        try:
            servers.append(Server((host, cfg.port), handler))
        except OSError as exc:
            log.warning("cannot bind %s:%s (%s)", host, cfg.port, exc)
    if not servers:
        raise RuntimeError(f"could not bind any of {cfg.hosts} on port {cfg.port}")
    return app, servers


def main() -> int:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    cfg = UIConfig()
    for d in (cfg.state_dir, cfg.log_dir, cfg.trash_root):
        Path(d).mkdir(parents=True, exist_ok=True)
    app, servers = build(cfg)
    if not app.store.configured():
        log.warning("no admin password configured; run legenex/control-ui/scripts/gx-ui-passwd")
    threads = []
    for httpd in servers[1:]:
        threads.append(threading.Thread(target=httpd.serve_forever, daemon=True))
    for t in threads:
        t.start()
    log.info("gx-control-ui %s listening on %s", __version__,
             ", ".join(f"{h}:{cfg.port}" for h in cfg.hosts))
    try:
        servers[0].serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
