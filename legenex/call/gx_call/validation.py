"""Validation of what the Control Center sends when it creates a call session.

The Control Center compiles a Call Agent (a configuration object, see
docs/18-call-agents.md) into the small, model-facing structure checked here.
Everything is bounded; nothing is interpreted as code.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .errors import ValidationError

SESSION_RE = re.compile(r"^call_[0-9a-f]{32}$")
AGENT_ID_RE = re.compile(r"^agt_[0-9a-f]{24}$")
TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
OWNER_RE = re.compile(r"^[A-Za-z0-9:_\-.]{1,80}$")
MAX_TOOLS = 8  # the model card recommends at most 5 per session; hard cap 8
MAX_PROMPT = 16000
MAX_SCHEMA_BYTES = 4096


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict
    on_hold: list[str] = field(default_factory=list)

    def model_view(self) -> dict:
        return {"name": self.name, "description": self.description, "parameters": self.parameters}


@dataclass
class SessionSpec:
    session_id: str
    agent_id: str
    agent_version: int
    agent_name: str
    owner: str
    system_prompt: str
    tools: list[ToolSpec]
    record: bool
    max_duration_s: int
    mode: str

    def engine_config(self, tool_timeout_s: float) -> dict:
        return {
            "type": "session.configure", "session_id": self.session_id,
            "system_prompt": self.system_prompt,
            "tools": [t.model_view() for t in self.tools],
            "on_hold": {t.name: t.on_hold for t in self.tools if t.on_hold},
            "tool_timeout_s": tool_timeout_s,
        }

    def public(self) -> dict:
        return {"session_id": self.session_id, "agent_id": self.agent_id, "agent_version": self.agent_version,
                "agent_name": self.agent_name, "tools": [t.name for t in self.tools], "record": self.record,
                "max_duration_s": self.max_duration_s, "mode": self.mode}


def _str(body: dict, key: str, limit: int, *, required: bool = True, default: str = "") -> str:
    value = body.get(key, default)
    if value is None and not required:
        return default
    if not isinstance(value, str):
        raise ValidationError(f"'{key}' must be a string")
    value = value.strip()
    if required and not value:
        raise ValidationError(f"'{key}' is required")
    if len(value) > limit:
        raise ValidationError(f"'{key}' is longer than {limit} characters")
    return value


def _schema(value: object, name: str) -> dict:
    if value is None:
        return {"type": "object", "properties": {}}
    if not isinstance(value, dict) or value.get("type") != "object":
        raise ValidationError(f"tool {name}: parameters must be a JSON Schema object with type 'object'")
    if len(json.dumps(value)) > MAX_SCHEMA_BYTES:
        raise ValidationError(f"tool {name}: parameters schema is larger than {MAX_SCHEMA_BYTES} bytes")
    props = value.get("properties", {})
    if not isinstance(props, dict):
        raise ValidationError(f"tool {name}: properties must be an object")
    return value


def session_spec(body: object, *, max_session_s: int) -> SessionSpec:
    if not isinstance(body, dict):
        raise ValidationError("request body must be a JSON object")
    sid = _str(body, "session_id", 40)
    if not SESSION_RE.match(sid):
        raise ValidationError("session_id must look like call_<32 hex>")
    agent = body.get("agent")
    if not isinstance(agent, dict):
        raise ValidationError("'agent' must be an object")
    agent_id = _str(agent, "agent_id", 40)
    if not AGENT_ID_RE.match(agent_id):
        raise ValidationError("agent.agent_id must look like agt_<24 hex>")
    version = agent.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or not 1 <= version <= 1_000_000:
        raise ValidationError("agent.version must be a positive integer")
    owner = _str(body, "owner", 80)
    if not OWNER_RE.match(owner):
        raise ValidationError("owner must be an opaque id")
    tools_raw = agent.get("tools") or []
    if not isinstance(tools_raw, list) or len(tools_raw) > MAX_TOOLS:
        raise ValidationError(f"agent.tools must be a list of at most {MAX_TOOLS} tools")
    tools: list[ToolSpec] = []
    seen: set[str] = set()
    for raw in tools_raw:
        if not isinstance(raw, dict):
            raise ValidationError("each tool must be an object")
        name = _str(raw, "name", 42)
        if not TOOL_NAME_RE.match(name) or name in seen:
            raise ValidationError(f"invalid or duplicate tool name {name!r}")
        seen.add(name)
        on_hold = raw.get("on_hold") or []
        if not isinstance(on_hold, list) or len(on_hold) > 4 or not all(
                isinstance(p, str) and 0 < len(p.strip()) <= 200 for p in on_hold):
            raise ValidationError(f"tool {name}: on_hold must be up to 4 phrases of at most 200 characters")
        tools.append(ToolSpec(name=name, description=_str(raw, "description", 500, required=False),
                              parameters=_schema(raw.get("parameters"), name),
                              on_hold=[p.strip() for p in on_hold]))
    max_duration = body.get("max_duration_s", max_session_s)
    if not isinstance(max_duration, int) or isinstance(max_duration, bool) or not 30 <= max_duration <= max_session_s:
        raise ValidationError(f"max_duration_s must be between 30 and {max_session_s}")
    mode = body.get("mode", "test")
    if mode not in ("test", "production"):
        raise ValidationError("mode must be test or production")
    return SessionSpec(
        session_id=sid, agent_id=agent_id, agent_version=version,
        agent_name=_str(agent, "name", 120), owner=owner,
        system_prompt=_str(agent, "system_prompt", MAX_PROMPT),
        tools=tools, record=body.get("record") is True, max_duration_s=max_duration, mode=mode,
    )


INJECTABLE_EVENTS = frozenset({"state.updated", "transfer.updated", "notice", "agent.config"})


def injected_event(body: object) -> dict:
    """An event the Control Center pushes to the caller's stream."""
    if not isinstance(body, dict):
        raise ValidationError("request body must be a JSON object")
    kind = body.get("type")
    if kind not in INJECTABLE_EVENTS:
        raise ValidationError(f"type must be one of {sorted(INJECTABLE_EVENTS)}")
    if len(json.dumps(body)) > 32 * 1024:
        raise ValidationError("event is larger than 32 KiB")
    return dict(body)


def tool_result(body: object) -> tuple[str, str, bool, dict]:
    if not isinstance(body, dict):
        raise ValidationError("request body must be a JSON object")
    call_id = _str(body, "call_id", 40)
    if not re.fullmatch(r"tc_[0-9a-f]{16}", call_id):
        raise ValidationError("invalid call_id")
    output = body.get("output")
    if not isinstance(output, str):
        output = json.dumps(output)
    if len(output) > 4000:
        raise ValidationError("tool output is larger than 4000 characters")
    ok = body.get("ok", True) is True
    extra = body.get("event") if isinstance(body.get("event"), dict) else {}
    if len(json.dumps(extra)) > 32 * 1024:
        raise ValidationError("event is larger than 32 KiB")
    return call_id, output, ok, extra
