"""Build V3 CAL routes: Call Agents (session) and the public gx-call API.

Registered by importing this module at the end of server.py. Session routes
(`/api/call/*`) inherit authentication, CSRF and the same-origin check. The
public API (`/v1/call/*`) is authenticated with a gateway (LiteLLM virtual)
key that allows `gx-call`, never accepts a session cookie, and only shows a
key the calls it created. Agents are managed by signed-in users only; API
clients may read enabled agents and start calls with them.
"""

from __future__ import annotations

import json
import re

from . import call_agents as ca
from . import call_intake as ci
from .calls import CallError
from .redact import redact
from .realtime import RealtimeError
from .server import MAX_BODY, Handler, _q, public_api, route

AGENT = r"(?P<agent_id>agt_[0-9a-f]{24})"
SESSION = r"(?P<sid>call_[0-9a-f]{32})"


def _user(h: Handler) -> str:
    assert h.session is not None  # noqa: S101 - guaranteed for session routes
    return h.session.username


def _owner(h: Handler) -> str:
    return f"user:{_user(h)}"


def _mine(h: Handler, sid: str) -> None:
    if h.app.calls.owner_of(sid) != _owner(h):
        raise CallError("no such call", 404, "not_found")


def _errors(fn):
    """Map this feature's errors onto the server's JSON error shape."""
    def wrapper(h: Handler, **kw):
        try:
            return fn(h, **kw)
        except ca.AgentError as exc:
            h._json(exc.status, {"error": {"message": redact(str(exc)), "code": exc.code}})
        except CallError as exc:
            h._json(exc.status, {"error": {"message": redact(str(exc)), "code": exc.code}})
        except (ci.StateError, RealtimeError) as exc:
            h._json(getattr(exc, "status", 400), {"error": {"message": redact(str(exc)),
                                                            "code": getattr(exc, "code", "invalid_request")}})
    wrapper.__name__ = fn.__name__
    return wrapper


# ================================================================ catalogue
@route("GET", r"/api/call/catalog")
@_errors
def api_call_catalog(h: Handler) -> None:
    h._json(200, {
        "tools": [{"name": n, "label": t["label"], "summary": t["summary"], "on_hold": t["on_hold"]}
                  for n, t in ca.TOOL_CATALOG.items()],
        "max_tools": ca.MAX_TOOLS_PER_AGENT, "voices": list(ca.VOICES), "use_cases": list(ca.USE_CASES),
        "statuses": list(ca.STATUSES), "modes": list(ca.MODES), "webhook_events": list(ca.WEBHOOK_EVENTS),
        "post_call_when": list(ca.POST_CALL_WHEN), "text_limits": ca.TEXT_LIMITS,
        "mva_schema": ci.MVA_SCHEMA, "mva_required": ci.MVA_REQUIRED_DEFAULT,
        "mva_optional": ci.MVA_OPTIONAL_DEFAULT, "templates": {u: ca.default_config(u) for u in ca.USE_CASES},
        "states": ci.US_STATES,
    })


@route("GET", r"/api/call/model")
@_errors
def api_call_model(h: Handler) -> None:
    info = h.app.calls.model()
    reg = (h.app.manager.registry().get("aliases") or {}).get("gx-call") or {}
    h._json(200, {**info, "registry": reg})


# =================================================================== agents
@route("GET", r"/api/call/agents")
@_errors
def api_call_agents(h: Handler) -> None:
    status = _q(h, "status") or None
    if status and status not in ca.STATUSES:
        raise ValueError("unknown status")
    h._json(200, {"agents": h.app.call_agents.list(include_archived=_q(h, "archived") == "1", status=status)})


@route("POST", r"/api/call/agents")
@_errors
def api_call_agent_create(h: Handler) -> None:
    body = h._body(MAX_BODY)
    cfg = body.get("config") if isinstance(body.get("config"), dict) else ca.default_config(
        str(body.get("template") or "intakepilot_mva") if body.get("template") in ca.USE_CASES else "intakepilot_mva")
    agent = h.app.call_agents.create(cfg, user=_user(h), note=str(body.get("note") or "created")[:200])
    h._json(201, agent)


@route("GET", rf"/api/call/agents/{AGENT}")
@_errors
def api_call_agent(h: Handler, agent_id: str) -> None:
    version = _q(h, "version")
    agent = h.app.call_agents.get(agent_id, int(version) if version else None)
    agent["compiled_prompt"] = ca.compile_prompt(agent["config"])
    agent["compiled_tools"] = ca.compile_tools(agent["config"])
    h._json(200, agent)


@route("POST", rf"/api/call/agents/{AGENT}")
@_errors
def api_call_agent_save(h: Handler, agent_id: str) -> None:
    body = h._body(MAX_BODY)
    if not isinstance(body.get("config"), dict):
        raise ValueError("config is required")
    base = body.get("base_version")
    agent = h.app.call_agents.save(agent_id, body["config"], user=_user(h),
                                   base_version=base if isinstance(base, int) else None,
                                   note=str(body.get("note") or "")[:200] or None)
    h._json(200, agent)


@route("POST", rf"/api/call/agents/{AGENT}/status")
@_errors
def api_call_agent_status(h: Handler, agent_id: str) -> None:
    body = h._body(MAX_BODY)
    h._json(200, h.app.call_agents.set_status(agent_id, user=_user(h), status=body.get("status") or None,
                                              mode=body.get("mode") or None))


@route("POST", rf"/api/call/agents/{AGENT}/clone")
@_errors
def api_call_agent_clone(h: Handler, agent_id: str) -> None:
    body = h._body(MAX_BODY)
    version = body.get("version")
    h._json(201, h.app.call_agents.clone(agent_id, user=_user(h), name=body.get("name") or None,
                                         version=version if isinstance(version, int) else None))


@route("GET", rf"/api/call/agents/{AGENT}/versions")
@_errors
def api_call_agent_versions(h: Handler, agent_id: str) -> None:
    items = h.app.call_agents.versions(agent_id)
    for v in items:
        v["metrics"] = h.app.calls.version_metrics(agent_id, v["version"])
    h._json(200, {"versions": items})


@route("GET", r"/api/call/compare")
@_errors
def api_call_compare(h: Handler) -> None:
    def ref(name: str) -> tuple[str, int]:
        m = re.fullmatch(r"(agt_[0-9a-f]{24})@([0-9]{1,7})", _q(h, name))
        if not m:
            raise ValueError(f"{name} must look like agt_<id>@<version>")
        return m.group(1), int(m.group(2))

    h._json(200, h.app.calls.compare(ref("a"), ref("b")))


@route("POST", r"/api/call/preview")
@_errors
def api_call_preview(h: Handler) -> None:
    """Validate an unsaved config and show exactly what the model would receive."""
    body = h._body(MAX_BODY)
    cfg = ca.validate_config(body.get("config"))
    h._json(200, {"ok": True, "compiled_prompt": ca.compile_prompt(cfg), "compiled_tools": ca.compile_tools(cfg),
                  "prompt_chars": len(ca.compile_prompt(cfg))})


# ============================================================= integrations
@route("GET", r"/api/call/integrations/secrets")
@_errors
def api_call_secrets(h: Handler) -> None:
    h._json(200, {"secrets": h.app.call_secrets.list()})


@route("POST", r"/api/call/integrations/secrets")
@_errors
def api_call_secret_set(h: Handler) -> None:
    body = h._body(8192)  # the value goes to the server only and is never returned
    res = h.app.call_secrets.set(str(body.get("name") or ""), body.get("value"))
    h.app.actions.audit(user=_user(h), ip=h._client_ip(), action="call.secret.set", outcome="ok", name=res["name"])
    h._json(200, res)


@route("POST", r"/api/call/integrations/secrets/(?P<name>[a-z][a-z0-9_\-]{1,40})/delete")
@_errors
def api_call_secret_delete(h: Handler, name: str) -> None:
    h._body(256)
    res = h.app.call_secrets.delete(name)
    h.app.actions.audit(user=_user(h), ip=h._client_ip(), action="call.secret.delete", outcome="ok", name=name)
    h._json(200, res)


# ================================================================= sessions
@route("POST", r"/api/call/sessions")
@_errors
def api_call_session_create(h: Handler) -> None:
    body = h._body(MAX_BODY)
    agent_id = str(body.get("agent_id") or "")
    if not ca.AGENT_ID_RE.match(agent_id):
        raise ValueError("agent_id is required")
    version = body.get("version")
    record = body.get("record")
    res = h.app.calls.create_session(
        agent_id=agent_id, owner=_owner(h), via="playground" if h.via_playground() else "control-center",
        user_label=_user(h), version=version if isinstance(version, int) else None,
        record=record if isinstance(record, bool) else None)
    h._json(201, res)


@route("GET", r"/api/call/sessions")
@_errors
def api_call_sessions(h: Handler) -> None:
    agent_id = _q(h, "agent_id") or None
    if agent_id and not ca.AGENT_ID_RE.match(agent_id):
        raise ValueError("invalid agent_id")
    h._json(200, {"sessions": h.app.calls.list_sessions(owner=_owner(h), agent_id=agent_id,
                                                        limit=int(_q(h, "limit", "30") or 30))})


@route("GET", rf"/api/call/sessions/{SESSION}")
@_errors
def api_call_session(h: Handler, sid: str) -> None:
    _mine(h, sid)
    h._json(200, h.app.calls.session_view(sid))


@route("GET", rf"/api/call/sessions/{SESSION}/events")
@_errors
def api_call_session_events(h: Handler, sid: str) -> None:
    _mine(h, sid)
    h._json(200, h.app.calls.events(sid, after=int(_q(h, "after", "0") or 0)))


@route("POST", rf"/api/call/sessions/{SESSION}/end")
@_errors
def api_call_session_end(h: Handler, sid: str) -> None:
    _mine(h, sid)
    h._body(256)
    h._json(200, h.app.calls.end_session(sid, "operator_ended", user_label=_user(h)))


@route("POST", rf"/api/call/sessions/{SESSION}/transfer")
@_errors
def api_call_session_transfer(h: Handler, sid: str) -> None:
    _mine(h, sid)
    body = h._body(MAX_BODY)
    h._json(200, h.app.calls.transfer(sid, str(body.get("status") or ""), note=str(body.get("note") or ""),
                                      source="operator", user_label=_user(h)))


@route("POST", rf"/api/call/sessions/{SESSION}/state")
@_errors
def api_call_session_state(h: Handler, sid: str) -> None:
    _mine(h, sid)
    body = h._body(MAX_BODY)
    base = body.get("base_revision")
    h._json(200, h.app.calls.update_state(sid, body.get("fields"), source="operator",
                                          base_revision=base if isinstance(base, int) else None))


@route("POST", rf"/api/call/sessions/{SESSION}/delete-content")
@_errors
def api_call_session_delete(h: Handler, sid: str) -> None:
    _mine(h, sid)
    h._body(256)
    h._json(200, h.app.calls.delete_session_content(sid, user_label=_user(h)))


# =============================================================== public API
_PUBLIC_SESSION = re.compile(
    r"^/v1/call/sessions/(call_[0-9a-f]{32})(/ticket|/state|/transcript|/tools|/events|/transfer|/end|/result)?$")
_PUBLIC_AGENT = re.compile(r"^/v1/call/agents/(agt_[0-9a-f]{24})$")


def _api_error(h: Handler, status: int, message: str, code: str) -> None:
    h._json(status, {"error": {"message": redact(message), "code": code, "retryable": status in (429, 502, 503)}})


def _public_agent(agent: dict) -> dict:
    cfg = agent["config"]
    return {"object": "call.agent", "agent_id": agent["agent_id"], "name": agent["name"],
            "version": agent["version"], "status": agent["status"], "mode": agent["mode"],
            "use_case": cfg["use_case"], "description": cfg["description"], "voice": cfg["voice"],
            "tags": agent["tags"], "tools": cfg["tool_permissions"],
            "required_fields": cfg["required_fields"], "optional_fields": cfg["optional_fields"],
            "structured_output_schema": cfg["structured_output_schema"],
            "recording": cfg["recording"]["enabled"], "max_call_minutes": cfg["max_call_minutes"],
            "updated_at": agent["updated_at"]}


def _public_session(view: dict) -> dict:
    out = {k: v for k, v in view.items() if k not in ("via",)}
    sid = view["session_id"]
    out["links"] = {"self": f"/v1/call/sessions/{sid}", "state": f"/v1/call/sessions/{sid}/state",
                    "transcript": f"/v1/call/sessions/{sid}/transcript", "events": f"/v1/call/sessions/{sid}/events",
                    "result": f"/v1/call/sessions/{sid}/result", "ticket": f"/v1/call/sessions/{sid}/ticket"}
    return out


@public_api("/v1/call")
def public_call(h: Handler, method: str, path: str) -> None:  # noqa: C901 - flat routing
    """`/v1/call/*` for API clients (gateway key with gx-call allowed)."""
    from .routes_v2 import _key_identity, _rate_ok

    if h._body_error is not None:
        raise h._body_error
    ident = _key_identity(h)
    if ident is None:
        _api_error(h, 401, "missing or invalid API key (create one in the Control Center > API Keys)",
                   "unauthorized")
        return
    models = ident.get("models") or []
    if models and "gx-call" not in models and "all-proxy-models" not in models:
        _api_error(h, 403, "this key does not allow gx-call", "forbidden")
        return
    if not _rate_ok(h, "call:" + ident["key"], 240):
        _api_error(h, 429, "rate limit: at most 240 call API requests per minute per key", "rate_limited")
        return
    owner = f"key:{ident['key']}"
    label = f"key:{ident['name']}"
    calls = h.app.calls
    try:
        if method == "GET" and path == "/v1/call/model":
            info = calls.model()
            h._json(200, {k: info.get(k) for k in ("alias", "task", "node", "identity", "runtime", "capabilities")}
                    | {"state": (info.get("health") or {}).get("state")})
            return
        if method == "GET" and path == "/v1/call/agents":
            h._json(200, {"data": [_public_agent(h.app.call_agents.get(a["agent_id"]))
                                   for a in h.app.call_agents.list(status="enabled")]})
            return
        m = _PUBLIC_AGENT.match(path)
        if m and method == "GET":
            agent = h.app.call_agents.get(m.group(1))
            if agent["status"] != "enabled":
                _api_error(h, 404, "no such enabled agent", "not_found")
                return
            h._json(200, _public_agent(agent))
            return
        if method == "GET" and path == "/v1/call/sessions":
            h._json(200, {"data": [_public_session(v) for v in calls.list_sessions(
                owner=owner, limit=int(_q(h, "limit", "30") or 30))]})
            return
        if method == "POST" and path == "/v1/call/sessions":
            if not _rate_ok(h, "call-create:" + ident["key"], 20):
                _api_error(h, 429, "at most 20 new calls per minute per key", "rate_limited")
                return
            body = h._body(MAX_BODY)
            agent_id = str(body.get("agent_id") or "")
            if not ca.AGENT_ID_RE.match(agent_id):
                raise ValueError("agent_id is required (agt_<24 hex>)")
            initial = body.get("initial_state")
            if initial is not None and not isinstance(initial, dict):
                raise ValueError("initial_state must be an object")
            record = body.get("record")
            res = calls.create_session(agent_id=agent_id, owner=owner, via="api", user_label=label,
                                       record=record if isinstance(record, bool) else None,
                                       external_ref=body.get("external_ref"), initial_state=initial,
                                       require_enabled=True)
            ticket = h.app.realtime.issue_ticket(res["session_id"], owner=owner)
            res["ws_path"] = f"/rt/call/{res['session_id']}?ticket={ticket}"
            res["ticket_expires_in"] = 60
            res["links"] = _public_session({"session_id": res["session_id"]})["links"]
            h._json(201, res, {"Location": f"/v1/call/sessions/{res['session_id']}"})
            return
        m = _PUBLIC_SESSION.match(path)
        if not m:
            _api_error(h, 404, f"no route for {method} {path}", "not_found")
            return
        sid, sub = m.group(1), m.group(2)
        try:
            if calls.owner_of(sid) != owner:
                raise CallError("no such call", 404, "not_found")
        except CallError:
            _api_error(h, 404, "no such call for this key", "not_found")
            return
        if sub is None and method == "GET":
            h._json(200, _public_session(calls.session_view(sid, include_content=False)))
        elif sub == "/ticket" and method == "POST":
            h._body(256)
            ticket = h.app.realtime.issue_ticket(sid, owner=owner)
            h._json(200, {"ws_path": f"/rt/call/{sid}?ticket={ticket}", "ticket_expires_in": 60})
        elif sub == "/state" and method == "GET":
            h._json(200, calls.get_state(sid))
        elif sub == "/state" and method == "POST":
            body = h._body(MAX_BODY)
            base = body.get("base_revision")
            h._json(200, calls.update_state(sid, body.get("fields"), source="api",
                                            base_revision=base if isinstance(base, int) else None))
        elif sub == "/transcript" and method == "GET":
            view = calls.session_view(sid)
            h._json(200, {"session_id": sid, "transcript": view.get("transcript", []),
                          "content_purged": bool(view["content_purged"])})
        elif sub == "/tools" and method == "GET":
            h._json(200, {"session_id": sid, "tools": calls.session_view(sid).get("tools", [])})
        elif sub == "/events" and method == "GET":
            h._json(200, calls.events(sid, after=int(_q(h, "after", "0") or 0)))
        elif sub == "/transfer" and method == "POST":
            body = h._body(MAX_BODY)
            h._json(200, calls.transfer(sid, str(body.get("status") or ""), note=str(body.get("note") or ""),
                                        source="api", user_label=label))
        elif sub == "/end" and method == "POST":
            body = h._body(MAX_BODY)
            reason = re.sub(r"[^a-z0-9_]", "_", str(body.get("reason") or "client_ended").lower())[:40]
            h._json(200, _public_session(calls.end_session(sid, reason, user_label=label)))
        elif sub == "/result" and method == "GET":
            view = calls.session_view(sid)
            if view["state"] != "ended":
                _api_error(h, 409, "the call has not ended yet", "not_ended")
                return
            h._json(200, {"session_id": sid, "state": view["state"], "disposition": view["disposition"],
                          "end_reason": view["end_reason"], "duration_s": view["duration_s"],
                          "agent_id": view["agent_id"], "agent_version": view["agent_version"],
                          "external_ref": view["external_ref"], "transfer": view["transfer"],
                          "metrics": view["metrics"], "result": view.get("result"),
                          "recording_asset_id": view["recording_asset_id"]})
        else:
            _api_error(h, 405, "method not allowed", "method_not_allowed")
    except (ca.AgentError, CallError) as exc:
        _api_error(h, exc.status, str(exc), exc.code)
    except RealtimeError as exc:
        _api_error(h, exc.status, str(exc), exc.code)
    except ci.StateError as exc:
        _api_error(h, 400, str(exc), "invalid_request")
    except (ValueError, json.JSONDecodeError) as exc:
        _api_error(h, 400, str(exc), "invalid_request")
    except OverflowError as exc:
        _api_error(h, 413, str(exc), "too_large")
