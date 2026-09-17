"""Build V3 LIV routes: the Live page (session) and the public gx-live API.

Registered by importing this module at the end of server.py. Browser routes
(`/api/live/*`) inherit authentication, CSRF and the same-origin check and
only ever see the signed-in user's own sessions. The public API (`/v1/live/*`)
is authenticated with a gateway (LiteLLM virtual) key that allows `gx-live`,
never accepts a session cookie, and only shows a key its own sessions.

The WebSocket itself is not here: the browser opens `/rt/live/<session_id>` on
the Playground, which asks `RealtimeRegistry.authorize` and tunnels it to
gx10-02 (plt.md section 1).
"""

from __future__ import annotations

import json
import re

from .live import LiveError, LiveManager
from .realtime import RealtimeError
from .redact import redact
from .server import MAX_BODY, Handler, _q, public_api, route

SESSION = r"(?P<sid>live_[0-9a-f]{32})"


def _user(h: Handler) -> str:
    assert h.session is not None  # noqa: S101 - guaranteed for session routes
    return h.session.username


def _owner(h: Handler) -> str:
    return f"user:{_user(h)}"


def _live(h: Handler) -> LiveManager:
    """``App.live``; the attribute is attached in ``App.__init__`` (server.py)."""
    return h.app.live  # type: ignore[attr-defined]


def _mine(h: Handler, sid: str) -> None:
    if _live(h).owner_of(sid) != _owner(h):
        raise LiveError("no such live session", 404, "not_found")


def _errors(fn):
    """Map this feature's errors onto the server's JSON error shape."""
    def wrapper(h: Handler, **kw):
        try:
            return fn(h, **kw)
        except LiveError as exc:
            h._json(exc.status, {"error": {"message": redact(str(exc)), "code": exc.code}})
        except RealtimeError as exc:
            h._json(exc.status, {"error": {"message": redact(str(exc)), "code": exc.code}})
    wrapper.__name__ = fn.__name__
    return wrapper


# =================================================================== browser
@route("GET", r"/api/live/model")
@_errors
def api_live_model(h: Handler) -> None:
    info = _live(h).model()
    reg = (h.app.manager.registry().get("aliases") or {}).get("gx-live") or {}
    h._json(200, {**info, "registry": reg})


@route("POST", r"/api/live/sessions")
@_errors
def api_live_session_create(h: Handler) -> None:
    body = h._body(MAX_BODY)
    res = _live(h).create_session(
        owner=_owner(h), via="playground" if h.via_playground() else "control-center",
        user_label=_user(h), config=body.get("config") if isinstance(body.get("config"), dict) else body)
    h._json(201, res)


@route("GET", r"/api/live/sessions")
@_errors
def api_live_sessions(h: Handler) -> None:
    h._json(200, {"sessions": _live(h).list_sessions(owner=_owner(h),
                                                     limit=int(_q(h, "limit", "30") or 30))})


@route("GET", rf"/api/live/sessions/{SESSION}")
@_errors
def api_live_session(h: Handler, sid: str) -> None:
    _mine(h, sid)
    if _q(h, "refresh") == "1":
        _live(h).refresh(sid)
    h._json(200, _live(h).session_view(sid))


@route("GET", rf"/api/live/sessions/{SESSION}/events")
@_errors
def api_live_session_events(h: Handler, sid: str) -> None:
    _mine(h, sid)
    h._json(200, _live(h).events(sid, after=int(_q(h, "after", "0") or 0)))


@route("POST", rf"/api/live/sessions/{SESSION}/turns")
@_errors
def api_live_session_turns(h: Handler, sid: str) -> None:
    _mine(h, sid)
    body = h._body(MAX_BODY)
    h._json(200, _live(h).record_turns(sid, body.get("turns"), owner=_owner(h)))


@route("POST", rf"/api/live/sessions/{SESSION}/end")
@_errors
def api_live_session_end(h: Handler, sid: str) -> None:
    _mine(h, sid)
    body = h._body(1024)
    reason = body.get("reason") if isinstance(body, dict) else None
    h._json(200, _live(h).end_session(sid, str(reason or "completed"), user_label=_user(h)))


@route("GET", rf"/api/live/sessions/{SESSION}/transcript")
@_errors
def api_live_transcript(h: Handler, sid: str) -> None:
    _mine(h, sid)
    h._json(200, _live(h).transcript(sid))


@route("POST", rf"/api/live/sessions/{SESSION}/transcript")
@_errors
def api_live_transcript_save(h: Handler, sid: str) -> None:
    _mine(h, sid)
    body = h._body(MAX_BODY)
    h._json(200, _live(h).save_transcript(sid, body.get("entries"), owner=_owner(h),
                                          user_label=_user(h)))


@route("POST", rf"/api/live/sessions/{SESSION}/delete-content")
@_errors
def api_live_delete_content(h: Handler, sid: str) -> None:
    _mine(h, sid)
    h._body(256)
    h._json(200, _live(h).delete_session_content(sid, user_label=_user(h)))


# =============================================================== public API
_PUBLIC_SESSION = re.compile(
    r"^/v1/live/sessions/(live_[0-9a-f]{32})(/ticket|/end|/events|/transcript|/turns)?$")


def _api_error(h: Handler, status: int, message: str, code: str) -> None:
    h._json(status, {"error": {"message": redact(message), "code": code,
                              "retryable": status in (429, 502, 503)}})


def _links(sid: str) -> dict:
    return {"self": f"/v1/live/sessions/{sid}", "events": f"/v1/live/sessions/{sid}/events",
            "transcript": f"/v1/live/sessions/{sid}/transcript",
            "ticket": f"/v1/live/sessions/{sid}/ticket", "end": f"/v1/live/sessions/{sid}/end"}


@public_api("/v1/live")
def public_live(h: Handler, method: str, path: str) -> None:  # noqa: C901 - flat routing
    """`/v1/live/*` for API clients (a gateway key that allows gx-live)."""
    from .routes_v2 import _key_identity, _rate_ok

    if h._body_error is not None:
        raise h._body_error
    ident = _key_identity(h)
    if ident is None:
        _api_error(h, 401, "missing or invalid API key (create one in the Control Center > API Keys)",
                   "unauthorized")
        return
    models = ident.get("models") or []
    if models and "gx-live" not in models and "all-proxy-models" not in models:
        _api_error(h, 403, "this key does not allow gx-live", "forbidden")
        return
    if not _rate_ok(h, "live:" + ident["key"], 240):
        _api_error(h, 429, "rate limit: at most 240 live API requests per minute per key", "rate_limited")
        return
    owner = f"key:{ident['key']}"
    label = f"key:{ident['name']}"
    live = _live(h)
    try:
        if method == "GET" and path == "/v1/live/model":
            info = live.model()
            h._json(200, {k: info.get(k) for k in ("alias", "task", "node", "protocol", "identity",
                                                   "capabilities", "limits", "tools",
                                                   "delegation_models")}
                    | {"state": (info.get("health") or {}).get("state")})
            return
        if method == "GET" and path == "/v1/live/sessions":
            h._json(200, {"data": live.list_sessions(owner=owner, limit=int(_q(h, "limit", "30") or 30))})
            return
        if method == "POST" and path == "/v1/live/sessions":
            if not _rate_ok(h, "live-create:" + ident["key"], 20):
                _api_error(h, 429, "at most 20 new live sessions per minute per key", "rate_limited")
                return
            body = h._body(MAX_BODY)
            res = live.create_session(owner=owner, via="api", user_label=label,
                                      config=body.get("config") if isinstance(body.get("config"), dict)
                                      else body)
            sid = res["session_id"]
            ticket = h.app.realtime.issue_ticket(sid, owner=owner)
            res["ws_path"] = f"/rt/live/{sid}?ticket={ticket}"
            res["ticket_expires_in"] = 60
            res["links"] = _links(sid)
            h._json(201, res, {"Location": f"/v1/live/sessions/{sid}"})
            return
        m = _PUBLIC_SESSION.match(path)
        if not m:
            _api_error(h, 404, f"no route for {method} {path}", "not_found")
            return
        sid, sub = m.group(1), m.group(2)
        try:
            if live.owner_of(sid) != owner:
                raise LiveError("no such live session", 404, "not_found")
        except LiveError:
            _api_error(h, 404, "no such live session for this key", "not_found")
            return
        if sub is None and method == "GET":
            view = live.session_view(sid, include_content=False)
            view["links"] = _links(sid)
            h._json(200, view)
        elif sub == "/ticket" and method == "POST":
            h._body(256)
            h._json(200, {"ws_path": f"/rt/live/{sid}?ticket="
                                     f"{h.app.realtime.issue_ticket(sid, owner=owner)}",
                          "ticket_expires_in": 60})
        elif sub == "/events" and method == "GET":
            h._json(200, live.events(sid, after=int(_q(h, "after", "0") or 0)))
        elif sub == "/turns" and method == "POST":
            body = h._body(MAX_BODY)
            h._json(200, live.record_turns(sid, body.get("turns"), owner=owner))
        elif sub == "/transcript" and method == "GET":
            h._json(200, live.transcript(sid))
        elif sub == "/transcript" and method == "POST":
            body = h._body(MAX_BODY)
            h._json(200, live.save_transcript(sid, body.get("entries"), owner=owner, user_label=label))
        elif sub == "/end" and method == "POST":
            body = h._body(1024)
            reason = str((body or {}).get("reason") or "completed")
            h._json(200, live.end_session(sid, reason, user_label=label))
        else:
            _api_error(h, 405, "method not allowed", "method_not_allowed")
    except LiveError as exc:
        _api_error(h, exc.status, str(exc), exc.code)
    except RealtimeError as exc:
        _api_error(h, exc.status, str(exc), exc.code)
    except (ValueError, json.JSONDecodeError) as exc:
        _api_error(h, 400, str(exc), "invalid_request")
    except OverflowError as exc:
        _api_error(h, 413, str(exc), "too_large")
