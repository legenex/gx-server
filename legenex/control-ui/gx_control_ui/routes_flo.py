"""Creative Flows routes (D-040, FLO).

Browser routes (``/api/flows/...``, ``/api/flow-runs/...``) inherit the
server's session authentication, CSRF and same-origin checks. State-changing
operations are POST (the server accepts no other browser method).

The public API (``/v1/flows``, ``/v1/flow-runs``, ``/v1/assets``) is
authenticated with a LiteLLM virtual key exactly like ``/v1/music``; it never
accepts a session cookie. A key sees only its own flows, runs and the assets
its runs produced; running a flow requires the key to allow every alias the
flow's nodes use.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from .flows import UI_OWNER, FlowError
from .media_library import LibraryError
from .redact import redact
from .server import MAX_BODY_FLOWS, Handler, _q, public_api, route
from .util import HTTPError, bearer, http_json

FLOW = r"(?P<flow_id>flow_[0-9a-f]{24})"
RUN = r"(?P<run_id>frun_[0-9a-f]{24})"
NODE = r"(?P<node_id>[A-Za-z0-9_\-]{1,40})"
TPL = r"(?P<tid>tpl_[0-9a-f]{24}|builtin_[a-z0-9_]{2,40})"
SECRET = r"(?P<name>[A-Za-z][A-Za-z0-9_\-]{0,63})"
Route = Callable[..., None]


def _error(h: Handler, exc: FlowError) -> None:
    payload: dict[str, Any] = {"message": redact(str(exc)), "code": exc.code}
    if exc.issues:
        payload["issues"] = exc.issues[:50]
    h._json(exc.status, {"error": payload})


def flow_route(method: str, pattern: str) -> Callable[[Route], Route]:
    def deco(fn: Route) -> Route:
        def wrapped(h: Handler, **kw: str) -> None:
            try:
                fn(h, **kw)
            except FlowError as exc:
                _error(h, exc)
        wrapped.__name__ = fn.__name__
        return route(method, pattern)(wrapped)
    return deco


def _user(h: Handler) -> str:
    assert h.session is not None  # noqa: S101 - session routes only
    return str(h.session.username)


def _int(h: Handler, key: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(_q(h, key, str(default)) or default)))
    except ValueError:
        raise ValueError(f"{key} must be a whole number") from None


# ================================================================ browser
@flow_route("GET", r"/api/flows/catalog")
def api_flows_catalog(h: Handler) -> None:
    h._json(200, h.app.flows.catalog(), {"Cache-Control": "private, max-age=30"})


@flow_route("GET", r"/api/flows/options")
def api_flows_options(h: Handler) -> None:
    h._json(200, h.app.flows.options())


@flow_route("GET", r"/api/flows")
def api_flows_list(h: Handler) -> None:
    q = _q(h, "q")
    h._json(200, {"flows": h.app.flows.list(owner=None, q=q, limit=_int(h, "limit", 100, 1, 500))})


@flow_route("POST", r"/api/flows")
def api_flows_create(h: Handler) -> None:
    body = h._body(MAX_BODY_FLOWS)
    h._json(201, h.app.flows.create(body, owner=UI_OWNER, user=_user(h)))


@flow_route("POST", r"/api/flows/validate")
def api_flows_validate(h: Handler) -> None:
    h._json(200, h.app.flows.validate(h._body(MAX_BODY_FLOWS).get("graph")))


@flow_route("POST", r"/api/flows/ai/generate")
def api_flows_ai(h: Handler) -> None:
    key = f"flows-ai:{_user(h)}"
    from .routes_v2 import _rate_ok
    if not _rate_ok(h, key, 10):
        raise FlowError("at most 10 AI flow requests per minute", 429, "rate_limited")
    h._json(200, h.app.flows.ai_create(h._body(16 * 1024), user=_user(h)))


@flow_route("GET", r"/api/flows/templates")
def api_flow_templates(h: Handler) -> None:
    h._json(200, {"templates": h.app.flows.templates(owner=UI_OWNER)})


@flow_route("POST", r"/api/flows/templates")
def api_flow_template_save(h: Handler) -> None:
    h._json(201, h.app.flows.save_template(h._body(MAX_BODY_FLOWS), owner=UI_OWNER, user=_user(h)))


@flow_route("GET", rf"/api/flows/templates/{TPL}")
def api_flow_template(h: Handler, tid: str) -> None:
    h._json(200, h.app.flows.template(tid, owner=UI_OWNER))


@flow_route("POST", rf"/api/flows/templates/{TPL}/duplicate")
def api_flow_template_dup(h: Handler, tid: str) -> None:
    h._json(201, h.app.flows.duplicate_template(tid, h._body(4096), owner=UI_OWNER, user=_user(h)))


@flow_route("POST", rf"/api/flows/templates/{TPL}/delete")
def api_flow_template_delete(h: Handler, tid: str) -> None:
    if h._body(1024).get("confirm") is not True:
        raise ValueError("deleting a template must be confirmed")
    h.app.flows.delete_template(tid, owner=UI_OWNER, user=_user(h))
    h._json(200, {"deleted": tid})


@flow_route("GET", r"/api/flows/secrets")
def api_flow_secrets(h: Handler) -> None:
    h._json(200, {"secrets": h.app.flows.secrets()})


@flow_route("POST", r"/api/flows/secrets")
def api_flow_secret_set(h: Handler) -> None:
    body = h._body(8192)
    name, value = body.get("name"), body.get("value")
    if not isinstance(name, str) or not isinstance(value, str):
        raise ValueError("name and value are required")
    h.app.flows.set_secret(name, value, user=_user(h))
    h._json(200, {"secrets": h.app.flows.secrets()})


@flow_route("POST", rf"/api/flows/secrets/{SECRET}/delete")
def api_flow_secret_delete(h: Handler, name: str) -> None:
    h._body(1024)
    if not h.app.flows.delete_secret(name, user=_user(h)):
        raise FlowError("no such secret", 404, "not_found")
    h._json(200, {"secrets": h.app.flows.secrets()})


@flow_route("GET", rf"/api/flows/{FLOW}")
def api_flow_get(h: Handler, flow_id: str) -> None:
    h._json(200, h.app.flows.get(flow_id, owner=None))


@flow_route("POST", rf"/api/flows/{FLOW}")
def api_flow_update(h: Handler, flow_id: str) -> None:
    h._json(200, h.app.flows.update(flow_id, h._body(MAX_BODY_FLOWS), owner=None, user=_user(h)))


@flow_route("POST", rf"/api/flows/{FLOW}/delete")
def api_flow_delete(h: Handler, flow_id: str) -> None:
    if h._body(1024).get("confirm") is not True:
        raise ValueError("deleting a flow must be confirmed")
    h.app.flows.delete(flow_id, owner=None, user=_user(h))
    h._json(200, {"deleted": flow_id})


@flow_route("POST", rf"/api/flows/{FLOW}/duplicate")
def api_flow_duplicate(h: Handler, flow_id: str) -> None:
    h._body(1024)
    h._json(201, h.app.flows.duplicate(flow_id, owner=None, user=_user(h), new_owner=UI_OWNER))


@flow_route("GET", rf"/api/flows/{FLOW}/versions")
def api_flow_versions(h: Handler, flow_id: str) -> None:
    h._json(200, {"versions": h.app.flows.versions(flow_id, owner=None)})


@flow_route("GET", rf"/api/flows/{FLOW}/versions/(?P<version>[0-9]{{1,7}})")
def api_flow_version(h: Handler, flow_id: str, version: str) -> None:
    h._json(200, h.app.flows.version(flow_id, int(version), owner=None))


@flow_route("POST", rf"/api/flows/{FLOW}/versions/(?P<version>[0-9]{{1,7}})/restore")
def api_flow_restore(h: Handler, flow_id: str, version: str) -> None:
    h._body(1024)
    h._json(200, h.app.flows.restore(flow_id, int(version), owner=None, user=_user(h)))


@flow_route("POST", rf"/api/flows/{FLOW}/run")
def api_flow_run(h: Handler, flow_id: str) -> None:
    body = h._body(4096)
    h._json(202, h.app.flows.run(flow_id, body, owner=None, run_owner=UI_OWNER, user=_user(h)))


@flow_route("GET", rf"/api/flows/{FLOW}/runs")
def api_flow_runs(h: Handler, flow_id: str) -> None:
    h.app.flows.store.get_flow(flow_id)
    h._json(200, {"runs": h.app.flows.runs(flow_id, owner=None, limit=_int(h, "limit", 30, 1, 200))})


@flow_route("GET", r"/api/flow-runs")
def api_flow_runs_all(h: Handler) -> None:
    h._json(200, {"runs": h.app.flows.runs(None, owner=None, limit=_int(h, "limit", 30, 1, 200))})


@flow_route("GET", rf"/api/flow-runs/{RUN}")
def api_flow_run_get(h: Handler, run_id: str) -> None:
    h._json(200, h.app.flows.run_state(run_id, owner=None))


@flow_route("POST", rf"/api/flow-runs/{RUN}/cancel")
def api_flow_run_cancel(h: Handler, run_id: str) -> None:
    h._body(1024)
    h._json(200, h.app.flows.cancel(run_id, owner=None, user=_user(h)))


@flow_route("GET", rf"/api/flow-runs/{RUN}/nodes/{NODE}")
def api_flow_node(h: Handler, run_id: str, node_id: str) -> None:
    h._json(200, h.app.flows.node_detail(run_id, node_id, owner=None))


@flow_route("POST", rf"/api/flow-runs/{RUN}/nodes/{NODE}/cancel")
def api_flow_node_cancel(h: Handler, run_id: str, node_id: str) -> None:
    h._body(1024)
    h._json(200, h.app.flows.cancel_node(run_id, node_id, owner=None, user=_user(h)))


# ============================================================== public API
_KEY_RE = re.compile(r"^sk-[A-Za-z0-9_\-]{8,200}$")
_P_FLOW = re.compile(r"^/v1/flows/(flow_[0-9a-f]{24})(/run)?$")
_P_RUN = re.compile(r"^/v1/flow-runs/(frun_[0-9a-f]{24})(/cancel)?$")
_P_ASSET = re.compile(r"^/v1/assets/(a_[0-9a-f]{24})(/content)?$")
PUBLIC_PREFIXES = ("/v1/flows", "/v1/flow-runs", "/v1/assets")
CREATIVE = ("gx-image", "gx-video", "gx-music", "gx-voice", "gx-auto", "gx-fast", "gx-reason", "gx-mini")


def _key_models(h: Handler) -> tuple[dict | None, set[str] | None]:
    """(identity, allowed aliases or None = unrestricted) for the bearer key."""
    from .routes_v2 import _key_identity

    ident = _key_identity(h)
    if ident is None:
        return None, None
    models = ident.get("models")
    if models is None:
        # an identity cached before FLO's hook: look the models up once
        auth = h.headers.get("Authorization") or ""
        secret = auth[7:].strip()
        try:
            status, data = http_json("GET", f"{h.app.cfg.litellm_base}/key/info", headers=bearer(secret), timeout=8)
        except HTTPError:
            return None, None
        info = (data or {}).get("info") if isinstance(data, dict) and status == 200 else None
        models = (info or {}).get("models") or []
    allowed = None if not models or "all-proxy-models" in models else set(models)
    return ident, allowed


def _api_error(h: Handler, status: int, message: str, code: str, issues: list | None = None) -> None:
    err: dict[str, Any] = {"message": redact(message), "code": code, "retryable": status in (429, 503)}
    if issues:
        err["issues"] = issues[:50]
    h._json(status, {"error": err})


def _public_flow(flow: dict) -> dict:
    out = {k: flow.get(k) for k in ("id", "name", "description", "version", "created_at", "updated_at",
                                    "template_id", "graph", "readiness", "node_count", "node_types")}
    out = {k: v for k, v in out.items() if v is not None}
    out["links"] = {"self": f"/v1/flows/{flow['id']}", "run": f"/v1/flows/{flow['id']}/run"}
    return out


def _public_run(run: dict) -> dict:
    out = {k: v for k, v in run.items() if k not in ("owner", "user", "graph")}
    for node in (out.get("nodes") or {}).values():
        node.pop("cache_key", None)
    assets = (out.get("summary") or {}).get("assets") or []
    out["links"] = {"self": f"/v1/flow-runs/{run['id']}", "cancel": f"/v1/flow-runs/{run['id']}/cancel",
                    "assets": [f"/v1/assets/{a}" for a in assets]}
    return out


def _public_asset(asset: dict) -> dict:
    keep = ("id", "type", "ext", "media_type", "title", "created_at", "operation", "model_alias", "model_repo",
            "model_revision", "workflow", "prompt", "seed", "width", "height", "duration", "fps", "file_size",
            "sha256", "parent_id", "flow_id", "flow_run_id", "flow_node_id", "tags", "bpm", "lyrics")
    out = {k: asset.get(k) for k in keep}
    out["content_url"] = f"/v1/assets/{asset['id']}/content"
    return out


@public_api("/v1/flows")
@public_api("/v1/flow-runs")
@public_api("/v1/assets")
def public_flows(h: Handler, method: str, path: str) -> None:  # noqa: C901 - flat routing
    from .routes_v2 import _rate_ok

    if h._body_error is not None:
        _api_error(h, 413 if isinstance(h._body_error, OverflowError) else 400, str(h._body_error), "invalid_request")
        return
    ident, allowed = _key_models(h)
    if ident is None:
        _api_error(h, 401, "missing or invalid API key (create one in the Control Center > API Keys)", "unauthorized")
        return
    if allowed is not None and not allowed & set(CREATIVE):
        _api_error(h, 403, "this key does not allow any creative or text alias", "forbidden")
        return
    if not _rate_ok(h, "flows:" + ident["key"], 240):
        _api_error(h, 429, "rate limit: at most 240 flow API requests per minute per key", "rate_limited")
        return
    owner = f"key:{ident['key']}"
    user = f"key:{ident['name']}"
    flows = h.app.flows

    def body() -> dict:
        return h._body(MAX_BODY_FLOWS)

    try:
        if path == "/v1/flows":
            if method == "GET":
                h._json(200, {"data": [_public_flow(f) for f in flows.list(owner=owner, q=_q(h, "q"),
                                                                             limit=_int(h, "limit", 100, 1, 500))]})
            elif method == "POST":
                b = body()
                flows.check_assets(b.get("graph"), owner=owner, asset_id=b.get("asset_id"))
                created = flows.create(b, owner=owner, user=user)
                h._json(201, _public_flow(created), {"Location": f"/v1/flows/{created['id']}"})
            else:
                _api_error(h, 405, "method not allowed", "method_not_allowed")
            return
        m = _P_FLOW.match(path)
        if m:
            flow_id, sub = m.group(1), m.group(2)
            if sub == "/run":
                if method != "POST":
                    _api_error(h, 405, "method not allowed", "method_not_allowed")
                    return
                if not _rate_ok(h, "flows-run:" + ident["key"], 20):
                    _api_error(h, 429, "at most 20 flow runs per minute per key", "rate_limited")
                    return
                flow = flows.store.get_flow(flow_id, owner=owner)
                flows.check_assets(flow["graph"], owner=owner)
                run = flows.run(flow_id, body(), owner=owner, run_owner=owner, user=user, allowed_aliases=allowed)
                h._json(202, _public_run(run), {"Location": f"/v1/flow-runs/{run['id']}"})
            elif method == "GET":
                h._json(200, _public_flow(flows.get(flow_id, owner=owner)))
            elif method == "PUT":
                b = body()
                flows.check_assets(b.get("graph"), owner=owner)
                h._json(200, _public_flow(flows.update(flow_id, b, owner=owner, user=user)))
            elif method == "DELETE":
                flows.delete(flow_id, owner=owner, user=user)
                h._json(200, {"deleted": flow_id})
            else:
                _api_error(h, 405, "method not allowed", "method_not_allowed")
            return
        m = _P_RUN.match(path)
        if m:
            run_id, sub = m.group(1), m.group(2)
            if sub == "/cancel" and method == "POST":
                body()
                h._json(200, _public_run(flows.cancel(run_id, owner=owner, user=user)))
            elif sub is None and method == "GET":
                h._json(200, _public_run(flows.run_state(run_id, owner=owner)))
            else:
                _api_error(h, 405, "method not allowed", "method_not_allowed")
            return
        m = _P_ASSET.match(path)
        if m:
            asset_id, sub = m.group(1), m.group(2)
            if method != "GET":
                _api_error(h, 405, "method not allowed", "method_not_allowed")
                return
            if not flows.store.asset_owned(asset_id, owner):
                _api_error(h, 404, "no such asset for this key", "not_found")
                return
            asset = h.app.library.get(asset_id)
            if sub is None:
                h._json(200, _public_asset(asset))
                return
            fmt = _q(h, "format")
            file_path = h.app.library.file_path(asset, fmt if fmt and fmt != asset["ext"] else None)
            if not file_path.is_file():
                _api_error(h, 404, "the file is missing", "not_found")
                return
            ext = file_path.suffix.lstrip(".")
            ctype = {"wav": "audio/wav", "flac": "audio/flac", "mp3": "audio/mpeg"}.get(ext, asset["media_type"])
            h._send_file(file_path, ctype, {"Content-Disposition": f'attachment; filename="{asset_id}.{ext}"',
                                            "Cache-Control": "private, max-age=3600"})
            return
        _api_error(h, 404, f"no route for {method} {path}", "not_found")
    except FlowError as exc:
        _api_error(h, exc.status, str(exc), exc.code, exc.issues)
    except LibraryError as exc:
        _api_error(h, exc.status, str(exc), "library")
    except (ValueError, json.JSONDecodeError) as exc:
        _api_error(h, 400, str(exc), "invalid_request")
    except OverflowError as exc:
        _api_error(h, 413, str(exc), "too_large")
