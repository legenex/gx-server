"""HTTP server for the control UI.

Security boundaries (ARCHITECTURE.md "Control UI"):

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
"""

from __future__ import annotations

import gzip
import hashlib
import secrets
import json
import logging
import mimetypes
import os
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
from .auth import COOKIE_NAME, AuthError, LoginThrottle, PasswordStore, SessionManager, csrf_ok
from .config import UIConfig
from .docs import DocLibrary
from .models import ResultLog, live_state
from .playground import Playground, PlaygroundError
from .redact import redact
from .services import Cluster
from . import views
from .api_keys import KeyError_, KeyManager
from .api_keys import probe as key_probe
from .hf import HFClient, HFError
from .media_jobs import IMAGE_SIZES, KIND_LABEL, VIDEO_SIZES, JobError, MediaJobs, RouterClient, import_upload
from .media_library import LibraryError, MediaLibrary, MediaTools, read_range
from .model_manager import ManagerError, ModelManager, gateway_probe

log = logging.getLogger("gx.ui")

MAX_BODY = 64 * 1024
#: Client-side routes that fall back to index.html. Anything else that is not
#: a known asset is a plain 404.
_SPA_ROUTE = re.compile(
    r"/(dashboard|models|create|library|manager|keys|runtime|cluster|jobs|logs|playground|docs|settings)"
    r"(/[a-z0-9_\-]{0,64}){0,2}"
)
ACCESS_LOG = os.environ.get("GX_UI_ACCESS_LOG", "1") != "0"
MAX_BODY_PLAYGROUND = 12 * 1024 * 1024
MAX_UPLOAD = 150 * 1024 * 1024
UPLOAD_PATH = "/api/media/upload"

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
    "media-src 'self' blob:; connect-src 'self'; font-src 'self'; object-src 'none'; "
    "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
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
    """Whitelisted, in-memory static assets (read once at startup)."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.files: dict[str, tuple[bytes, bytes | None, str, str]] = {}
        self.reload()

    def reload(self) -> None:
        files = {}
        for path in sorted(self.root.rglob("*")):
            if not path.is_file() or path.name.startswith("."):
                continue
            rel = "/" + path.relative_to(self.root).as_posix()
            data = path.read_bytes()
            ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/json", "image/svg+xml",
                                                      "application/manifest+json"):
                ctype += "; charset=utf-8"
            gz = gzip.compress(data, 6) if len(data) > 1024 and not ctype.startswith("image/png") else None
            etag = '"' + hashlib.sha256(data).hexdigest()[:20] + '"'
            files[rel] = (data, gz, ctype, etag)
        self.files = files

    def get(self, path: str):
        return self.files.get(path)


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
        self.playground = Playground(cfg, self.cluster, self.results)
        self.docs = DocLibrary(cfg.docs_dir)
        self.static = StaticFiles(cfg.static_dir)
        self.library = MediaLibrary(cfg.media_dir, MediaTools(enabled=not cfg.offline))
        self.media = MediaJobs(
            self.library, RouterClient(cfg.media_base, lambda: cfg.secret("GX_MEDIA_API_KEY")),
            results=self.results, model_identity=self.workflow_identity, audit=self.actions.audit)
        self.hf = HFClient(cfg.hf_token_file)
        self.manager = ModelManager(cfg, self.cluster, self.hf, audit=self.actions.audit)
        self.keys = KeyManager(cfg.litellm_base, lambda: cfg.secret("LITELLM_MASTER_KEY"))
        self.zips: dict[str, tuple[Path, float, str]] = {}
        self._zip_lock = threading.Lock()
        self._gen_seen: int | None = None
        self._gen_lock = threading.Lock()

    def workflow_identity(self, workflow: str) -> dict:
        """Which model repository/revision produced media from `workflow`."""
        reg = self.manager.registry()
        alias = (reg.get("workflow_models") or {}).get(workflow)
        spec = (reg.get("aliases") or {}).get(alias or "", {})
        comps = spec.get("components") or []
        main = next((c for c in comps if c.get("kind") == "checkpoint" and (
            ("edit" in workflow) == ("edit" in c.get("role", "")))), comps[0] if comps else {})
        if workflow.startswith("wan22-i2v") or workflow.startswith("wan22-v2v-keyframe"):
            main = next((c for c in comps if "image-to-video" in c.get("role", "")), main)
        return {"repository": main.get("repository"), "revision": main.get("revision"),
                "components": [{k: c.get(k) for k in ("role", "repository", "revision", "file")} for c in comps]}

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
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE_NAME:
                return v
        return None

    def _is_https(self) -> bool:
        return self.headers.get("X-Forwarded-Proto", "").lower() == "https"

    def _cookie(self, token: str, max_age: int) -> str:
        secure = self.app.cfg.cookie_secure == "always" or (
            self.app.cfg.cookie_secure == "auto" and self._is_https())
        parts = [f"{COOKIE_NAME}={token}", "HttpOnly", "SameSite=Strict", "Path=/", f"Max-Age={max_age}"]
        if secure:
            parts.append("Secure")
        return "; ".join(parts)

    def _send(self, status: int, body: bytes, ctype: str, extra: dict[str, str] | None = None) -> None:
        self._status = status
        self.send_response(status)
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: Any, extra: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, default=str, separators=(",", ":")).encode("utf-8")
        headers = {"Cache-Control": "no-store"}
        headers.update(extra or {})
        if len(body) > 2048 and "gzip" in (self.headers.get("Accept-Encoding") or ""):
            body = gzip.compress(body, 5)
            headers["Content-Encoding"] = "gzip"
            headers["Vary"] = "Accept-Encoding"
        self._send(status, body, "application/json; charset=utf-8", headers)

    def _error(self, status: int, message: str, code: str = "error") -> None:
        self._json(status, {"error": {"message": redact(message), "code": code}})

    def _consume_body(self, path: str) -> None:
        """Read the whole request body up front (bounded).

        Every POST body is consumed before anything is answered, otherwise an
        unread body stays in a keep-alive socket and is parsed as the start of
        the next request. An over-limit body is not read; the connection is
        closed after the error instead.
        """
        self._raw = b""
        self._body_error: Exception | None = None
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self.close_connection = True
            self._body_error = ValueError("invalid Content-Length")
            return
        if path == UPLOAD_PATH:
            # Uploads are streamed to a temp file by the route itself (after
            # authentication and CSRF), never read into memory here. The body
            # may stay unread (refused request), so never reuse the socket.
            self.close_connection = True
            self._upload_length = length
            if length <= 0 or length > MAX_UPLOAD:
                self.close_connection = True
                self._body_error = OverflowError(f"upload must be 1 byte to {MAX_UPLOAD // 2**20} MiB")
            return
        limit = MAX_BODY_PLAYGROUND if path == "/api/playground/chat" else MAX_BODY
        if length < 0 or length > limit:
            self.close_connection = True
            self._body_error = OverflowError(f"request body exceeds {limit} bytes")
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

    do_DELETE = do_PATCH = do_PUT

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
        except PlaygroundError as exc:
            self._error(exc.status, str(exc), "playground")
        except ActionRefused as exc:
            self._error(exc.status, str(exc), "refused")
        except (LibraryError, JobError, ManagerError, HFError, KeyError_) as exc:
            self._error(exc.status, str(exc), type(exc).__name__.lower().rstrip("_"))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001
            log.exception("unhandled error on %s %s", method, path)
            if not self._status:
                self._error(500, f"internal error: {type(exc).__name__}", "internal")
        finally:
            self._finish(path)

    def _send_file(self, path: Path, ctype: str, extra: dict[str, str] | None = None) -> None:
        """Stream a file from disk, with single-range support (video seeking)."""
        size = path.stat().st_size
        headers = {"Accept-Ranges": "bytes", **(extra or {})}
        rng = parse_range(self.headers.get("Range"), size)
        if rng == "invalid":
            self._send(416, b"", "text/plain", {**headers, "Content-Range": f"bytes */{size}"})
            return
        if rng is not None and not isinstance(rng, str):
            start, end = rng
            self._send(206, read_range(path, start, end), ctype,
                       {**headers, "Content-Range": f"bytes {start}-{end}/{size}"})
            return
        self._status = 200
        self.send_response(200)
        for k, v in {**SECURITY_HEADERS, **headers}.items():
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
def route(method: str, pattern: str, access: str = "session"):
    def deco(fn):
        Handler.routes.append((method, re.compile(pattern), fn, access))
        return fn
    return deco


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
    h._json(200, {"authenticated": False}, {"Set-Cookie": h._cookie("", 0)})


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
    models = live_state(h.app.cluster, h.app.results)
    specs = {name: s.public() for name, s in h.app.actions.registry.items() if name.startswith("model.")}
    for m in models:
        m["actions"] = {k.rsplit(".", 1)[1]: v for k, v in specs.items()
                        if k.startswith(f"model.{m['alias']}.")}
    h._json(200, {"models": models, "running_jobs": h.app.actions.running()})


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
    job = h.app.actions.submit(name, user=h.session.username, ip=h._client_ip(),
                               confirm=body.get("confirm"))
    h._json(202, job.as_dict())


@route("POST", r"/api/models/(?P<alias>gx-[a-z]{3,6})/(?P<op>load|unload|restart|force_release)")
def api_model_op(h: Handler, alias: str, op: str) -> None:
    assert h.session is not None  # noqa: S101
    body = h._body(MAX_BODY)
    job = h.app.actions.submit(f"model.{alias}.{op}", user=h.session.username, ip=h._client_ip(),
                               confirm=body.get("confirm"))
    h._json(202, job.as_dict())


@route("GET", r"/api/logs")
def api_logs(h: Handler) -> None:
    h._json(200, {"streams": [s.as_dict() for s in logstreams.STREAMS],
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


@route("GET", r"/api/playground/config")
def api_pg_config(h: Handler) -> None:
    from . import playground as pg
    h._json(200, {"chat_models": pg.CHAT_ALIASES, "vision_models": pg.VISION_ALIASES,
                  "image_sizes": pg.IMAGE_SIZES, "video_sizes": pg.VIDEO_SIZES,
                  "gateway_url": h.app.cfg.public_gateway_url,
                  "media_url": "http://192.168.100.11:18800/v1 (fabric; from gx10-01 only)",
                  "gxmax_state": h.app.cluster.gxmax_state(),
                  "sample_tool": pg.SAMPLE_TOOL})


@route("POST", r"/api/playground/chat")
def api_pg_chat(h: Handler) -> None:
    body = h._body(MAX_BODY_PLAYGROUND)
    if body.get("stream"):
        gen = h.app.playground.chat_stream(body)
        h._status = 200
        h.send_response(200)
        for k, v in SECURITY_HEADERS.items():
            h.send_header(k, v)
        h.send_header("Content-Type", "text/event-stream; charset=utf-8")
        h.send_header("Cache-Control", "no-store")
        h.send_header("X-Accel-Buffering", "no")
        h.send_header("Transfer-Encoding", "chunked")
        h.end_headers()
        try:
            for chunk in gen:
                h.wfile.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
                h.wfile.flush()
            h.wfile.write(b"0\r\n\r\n")
            h.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            gen.close()
        return
    h._json(200, h.app.playground.chat(body))


@route("POST", r"/api/playground/image")
def api_pg_image(h: Handler) -> None:
    h._json(200, h.app.playground.image(h._body(MAX_BODY)))


@route("POST", r"/api/playground/video")
def api_pg_video(h: Handler) -> None:
    h._json(202, h.app.playground.video_submit(h._body(MAX_BODY)))


@route("GET", r"/api/playground/video/(?P<job_id>[A-Za-z0-9\-]{1,64})")
def api_pg_video_status(h: Handler, job_id: str) -> None:
    h._json(200, h.app.playground.video_status(job_id))


@route("GET", r"/api/playground/video/(?P<job_id>[A-Za-z0-9\-]{1,64})/content")
def api_pg_video_content(h: Handler, job_id: str) -> None:
    data, ctype = h.app.playground.video_content(job_id)
    headers = {"Cache-Control": "private, max-age=3600", "Accept-Ranges": "bytes",
               "Content-Disposition": f'inline; filename="gx-video-{job_id}.mp4"'}
    # Browsers only allow seeking in media served with byte-range support.
    rng = parse_range(h.headers.get("Range"), len(data))
    if rng is None:
        h._send(200, data, ctype, headers)
    elif rng == "invalid":
        h._send(416, b"", "text/plain", {**headers, "Content-Range": f"bytes */{len(data)}"})
    else:
        start, end = rng
        h._send(206, data[start:end + 1], ctype, {**headers, "Content-Range": f"bytes {start}-{end}/{len(data)}"})



# ================================================================== media
def _q(h: Handler, key: str, default: str = "") -> str:
    return (h.query.get(key) or [default])[0]


@route("GET", r"/api/media/assets")
def api_media_assets(h: Handler) -> None:
    fav = _q(h, "favourite")
    h._json(200, h.app.library.search(
        q=_q(h, "q"), type_=_q(h, "type"), model=_q(h, "model"), operation=_q(h, "operation"),
        favourite=None if fav == "" else fav in ("1", "true"), sort=_q(h, "sort", "newest"),
        limit=int(_q(h, "limit", "60") or 60), offset=int(_q(h, "offset", "0") or 0)))


@route("GET", r"/api/media/assets/(?P<asset_id>a_[0-9a-f]{24})")
def api_media_asset(h: Handler, asset_id: str) -> None:
    h._json(200, h.app.library.get(asset_id, lineage=True))


@route("GET", r"/api/media/assets/(?P<asset_id>a_[0-9a-f]{24})/file")
def api_media_file(h: Handler, asset_id: str) -> None:
    asset = h.app.library.get(asset_id)
    path = h.app.library.file_path(asset)
    if not path.is_file():
        h._error(404, "the file is missing from the library store", "not_found")
        return
    disposition = "attachment" if _q(h, "download") == "1" else "inline"
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", (asset.get("title") or asset_id))[:60] or asset_id
    h._send_file(path, asset["media_type"], {
        "Content-Disposition": f'{disposition}; filename="{base}.{asset["ext"]}"',
        "Cache-Control": "private, max-age=86400"})


@route("GET", r"/api/media/assets/(?P<asset_id>a_[0-9a-f]{24})/thumbnail")
def api_media_thumb(h: Handler, asset_id: str) -> None:
    asset = h.app.library.get(asset_id)
    thumb = h.app.library.thumb_path(asset_id)
    if thumb.is_file():
        h._send_file(thumb, "image/jpeg", {"Cache-Control": "private, max-age=86400"})
    elif asset["type"] == "image":
        h._send_file(h.app.library.file_path(asset), asset["media_type"], {"Cache-Control": "private, max-age=3600"})
    else:
        h._error(404, "no thumbnail", "not_found")


@route("POST", r"/api/media/assets/(?P<asset_id>a_[0-9a-f]{24})")
def api_media_update(h: Handler, asset_id: str) -> None:
    body = h._body(MAX_BODY)
    fav = body.get("favourite")
    if fav is not None and not isinstance(fav, bool):
        raise ValueError("favourite must be true or false")
    title = body.get("title")
    if title is not None and not isinstance(title, str):
        raise ValueError("title must be text")
    h._json(200, h.app.library.update(asset_id, title=title, favourite=fav))


@route("POST", r"/api/media/delete")
def api_media_delete(h: Handler) -> None:
    assert h.session is not None  # noqa: S101
    body = h._body(MAX_BODY)
    ids = body.get("ids")
    if body.get("confirm") is not True:
        raise ValueError("deleting media must be confirmed")
    if not isinstance(ids, list):
        raise ValueError("ids must be a list")
    result = h.app.library.delete([str(i) for i in ids])
    h.app.actions.audit(user=h.session.username, ip=h._client_ip(), action="media.delete", outcome="ok",
                        deleted=result["deleted"])
    h._json(200, result)


@route("POST", r"/api/media/zip")
def api_media_zip(h: Handler) -> None:
    assert h.session is not None  # noqa: S101
    body = h._body(MAX_BODY)
    ids = body.get("ids")
    if not isinstance(ids, list):
        raise ValueError("ids must be a list")
    path, count = h.app.library.build_zip([str(i) for i in ids])
    token = secrets.token_urlsafe(24)
    with h.app._zip_lock:
        now = time.time()
        for t, (p, created, _) in list(h.app.zips.items()):
            if now - created > 900:
                p.unlink(missing_ok=True)
                h.app.zips.pop(t, None)
        h.app.zips[token] = (path, now, h.session.username)
    h._json(200, {"token": token, "count": count, "bytes": path.stat().st_size,
                  "url": f"/api/media/zip/{token}"})


@route("GET", r"/api/media/zip/(?P<token>[A-Za-z0-9_\-]{20,64})")
def api_media_zip_get(h: Handler, token: str) -> None:
    assert h.session is not None  # noqa: S101
    with h.app._zip_lock:
        entry = h.app.zips.pop(token, None)
    if entry is None or entry[2] != h.session.username:
        h._error(404, "this download link has expired", "not_found")
        return
    path = entry[0]
    try:
        h._send_file(path, "application/zip", {
            "Content-Disposition": f'attachment; filename="gx-media-{time.strftime("%Y%m%d-%H%M%S")}.zip"',
            "Cache-Control": "no-store"})
    finally:
        path.unlink(missing_ok=True)


@route("POST", r"/api/media/upload")
def api_media_upload(h: Handler) -> None:
    if h._body_error is not None:
        raise h._body_error
    ctype = (h.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if not (ctype.startswith("image/") or ctype.startswith("video/")):
        raise ValueError("upload an image or a video")
    title = urllib.parse.unquote(h.headers.get("X-Title") or "")[:200] or None
    tmp = h.app.library.tmp_file(".upload")
    remaining = getattr(h, "_upload_length", 0)
    try:
        with tmp.open("wb") as fh:
            while remaining > 0:
                chunk = h.rfile.read(min(1 << 20, remaining))
                if not chunk:
                    raise ValueError("the upload was interrupted")
                fh.write(chunk)
                remaining -= len(chunk)
        asset = import_upload(h.app.library, tmp, ctype, title)
    finally:
        tmp.unlink(missing_ok=True)
    assert h.session is not None  # noqa: S101
    h.app.actions.audit(user=h.session.username, ip=h._client_ip(), action="media.upload", outcome="ok",
                        asset=asset["id"], bytes=asset["file_size"])
    h._json(200, asset)


@route("GET", r"/api/media/options")
def api_media_options(h: Handler) -> None:
    h._json(200, {"kinds": KIND_LABEL, "image_sizes": IMAGE_SIZES, "video_sizes": VIDEO_SIZES,
                  "stats": h.app.library.stats()})


@route("POST", r"/api/media/jobs")
def api_media_job_submit(h: Handler) -> None:
    assert h.session is not None  # noqa: S101
    if h.app.cluster.gxmax_state() in ("acquiring", "ready", "releasing"):
        h._error(409, "gx-max holds the cluster; media generation is available again after it is released",
                 "refused")
        return
    h._json(202, h.app.media.submit(h._body(MAX_BODY), user=h.session.username, ip=h._client_ip()))


@route("GET", r"/api/media/jobs")
def api_media_jobs(h: Handler) -> None:
    h._json(200, {"jobs": h.app.media.list()})


@route("GET", r"/api/media/jobs/(?P<job_id>[0-9a-f]{16})")
def api_media_job(h: Handler, job_id: str) -> None:
    h._json(200, h.app.media.get(job_id))


# ========================================================== model manager
@route("GET", r"/api/manager/inventory")
def api_mm_inventory(h: Handler) -> None:
    inv = h.app.manager.inventory()
    inv["tests"] = h.app.manager.last_tests()
    inv["jobs"] = h.app.manager.jobs()
    inv["models"] = live_state(h.app.cluster, h.app.results)
    h._json(200, inv)


@route("GET", r"/api/manager/search")
def api_mm_search(h: Handler) -> None:
    h._json(200, {"results": h.app.hf.search(_q(h, "q"), limit=int(_q(h, "limit", "30") or 30),
                                             sort=_q(h, "sort", "downloads"))})


@route("POST", r"/api/manager/lookup")
def api_mm_lookup(h: Handler) -> None:
    h._json(200, h.app.manager.lookup(str(h._body(MAX_BODY).get("ref", ""))))


@route("POST", r"/api/manager/plan")
def api_mm_plan(h: Handler) -> None:
    h._json(200, h.app.manager.plan(h._body(MAX_BODY)))


def _mm_op(name: str):
    def fn(h: Handler) -> None:
        assert h.session is not None  # noqa: S101
        method = getattr(h.app.manager, name)
        h._json(202 if name in ("stage", "test", "assign", "rollback", "delete") else 200,
                method(h._body(MAX_BODY), user=h.session.username))
    return fn


for _op in ("stage", "test", "assign", "rollback", "accept", "delete"):
    route("POST", rf"/api/manager/{_op}")(_mm_op(_op))


@route("GET", r"/api/manager/jobs")
def api_mm_jobs(h: Handler) -> None:
    h._json(200, {"jobs": h.app.manager.jobs()})


@route("GET", r"/api/manager/jobs/(?P<job_id>[0-9a-f]{16})")
def api_mm_job(h: Handler, job_id: str) -> None:
    h._json(200, h.app.manager.job(job_id))


@route("POST", r"/api/manager/probe")
def api_mm_probe(h: Handler) -> None:
    alias = h._body(MAX_BODY).get("alias")
    if alias not in ("gx-mini", "gx-fast", "gx-reason"):
        raise ValueError("probe gx-mini, gx-fast or gx-reason (gx-max has its own page controls)")
    result = gateway_probe(h.app.cfg, alias)
    h.app.results.record(alias, "inference", bool(result.get("ok")), result.get("answer") or
                         result.get("error") or "", latency_ms=int((result.get("seconds") or 0) * 1000))
    h._json(200, result)


@route("GET", r"/api/manager/hf-token")
def api_hf_token(h: Handler) -> None:
    h._json(200, h.app.hf.token_state())


@route("POST", r"/api/manager/hf-token")
def api_hf_token_set(h: Handler) -> None:
    assert h.session is not None  # noqa: S101
    body = h._body(4096)
    if body.get("clear"):
        h._json(200, h.app.manager.clear_token(user=h.session.username))
    else:
        h._json(200, h.app.manager.set_token(str(body.get("token") or ""), user=h.session.username))


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
    model = body.get("model", "gx-mini")
    if not isinstance(secret, str) or not re.fullmatch(r"sk-[A-Za-z0-9_\-]{8,200}", secret):
        raise ValueError("paste a gateway key (sk-...)")
    if model not in ("gx-mini", "gx-fast", "gx-reason", "gx-auto"):
        raise ValueError("test with a text alias")
    h._json(200, key_probe(h.app.cfg.litellm_base, secret, model))


def parse_range(header: str | None, size: int):
    """Single `bytes=` range -> (start, end) inclusive; None = whole body;
    "invalid" = unsatisfiable. Multi-range requests get the whole body."""
    if not header or not header.startswith("bytes=") or "," in header or size == 0:
        return None
    first, _, last = header[6:].strip().partition("-")
    try:
        if first == "":
            n = int(last)
            if n <= 0:
                return "invalid"
            return max(0, size - n), size - 1
        start = int(first)
        end = int(last) if last else size - 1
    except ValueError:
        return None
    if start >= size or end < start:
        return "invalid"
    return start, min(end, size - 1)


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
    for d in (cfg.state_dir, cfg.log_dir):
        d.mkdir(parents=True, exist_ok=True)
    app, servers = build(cfg)
    if not app.store.configured():
        log.warning("no admin password configured; run legenex/control-ui/scripts/gx-ui-passwd")
    threads = []
    for srv in servers:
        host, port = str(srv.server_address[0]), srv.server_address[1]
        log.info("gx-control-ui %s listening on http://%s:%s", __version__, host, port)
        t = threading.Thread(target=srv.serve_forever, daemon=True, name=f"http-{host}")
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
