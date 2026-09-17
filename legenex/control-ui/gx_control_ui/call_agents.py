"""Call Agents: versioned configuration objects for gx-call (Build V3 CAL).

An agent is NOT a model copy. Every agent shares the one gx-call model
instance on gx10-02; the agent only decides what that model is told (system
prompt), which real tools it may call, which structured intake it fills and
what happens around the call (transfer, webhooks, post-call actions,
recording, retention).

Every save creates a new immutable version (``call_agent_versions``); the
agent row keeps a stable ``agent_id`` (``agt_<24 hex>``), a status
(draft / enabled / disabled / archived), a mode (test / production) and the
current version number. Sessions always record the exact version they used.
"""

from __future__ import annotations

import copy
import difflib
import hashlib
import json
import re
import secrets
import time
from typing import Any, Callable

from . import call_intake as ci
from .netguard import BlockedURL, check_url

AGENT_ID_RE = re.compile(r"^agt_[0-9a-f]{24}$")
STATUSES = ("draft", "enabled", "disabled", "archived")
MODES = ("test", "production")
USE_CASES = ("intakepilot_mva", "general")
SECRET_NAME_RE = re.compile(r"^[a-z][a-z0-9_\-]{1,40}$")
INTEGRATION_NAME_RE = re.compile(r"^[a-z][a-z0-9_\-]{1,40}$")
WEBHOOK_EVENTS = ("call.started", "call.ended", "intake.updated", "intake.completed", "transfer.requested",
                  "tool.send_webhook")
POST_CALL_WHEN = ("always", "completed", "qualified", "intake_complete", "transferred")
MAX_PROMPT_CHARS = 12000
VOICES = ("Aria",)

#: The tools an agent may be allowed to use. Every one of them is executed by
#: the Control Center (calls.py) and changes real state or calls a real,
#: configured integration. Descriptions are what the model reads.
TOOL_CATALOG: dict[str, dict] = {
    "update_intake_fields": {
        "label": "Update intake fields",
        "summary": "Writes caller answers into the authoritative structured state (validated server-side).",
        "description": "Save information the caller has given into the intake record. Call this every time the "
                       "caller provides one or more intake answers. Use only values the caller actually said.",
        "parameters": {"type": "object", "properties": {
            "fields": {"type": "object", "description": "Field name to value, for example "
                                                        "{\"caller_name\": \"Jane Doe\", \"accident_state\": \"TX\"}"}},
            "required": ["fields"]},
        "on_hold": ["Let me note that down."],
    },
    "check_business_hours": {
        "label": "Check business hours",
        "summary": "Answers whether the office is open now, from the agent's configured hours.",
        "description": "Check whether the office is open right now and when it opens next.",
        "parameters": {"type": "object", "properties": {}},
        "on_hold": ["Let me check our hours."],
    },
    "lookup_accident_state_rules": {
        "label": "Look up state accident rules",
        "summary": "General filing deadline, negligence rule and insurance system for a US state (local table).",
        "description": "Look up general motor vehicle accident rules for a US state: the usual injury filing "
                       "deadline, the negligence rule and whether the state is no-fault. Not legal advice.",
        "parameters": {"type": "object", "properties": {
            "state": {"type": "string", "description": "US state name or two-letter code as the caller said it"},
            "accident_date": {"type": "string", "description": "Accident date if known, YYYY-MM-DD"}},
            "required": ["state"]},
        "on_hold": ["Let me look up the rules for that state."],
    },
    "request_warm_transfer": {
        "label": "Request warm transfer",
        "summary": "Asks for a human specialist: sets the transfer state, notifies the configured destination "
                   "with a summary, and the integrator bridges the call.",
        "description": "Request a warm transfer to a human specialist when the caller qualifies or asks for a "
                       "person. Give a one-sentence reason and a short summary for the specialist.",
        "parameters": {"type": "object", "properties": {
            "reason": {"type": "string", "description": "Why the caller is being transferred"},
            "summary": {"type": "string", "description": "Short summary of the caller's situation"}},
            "required": ["reason"]},
        "on_hold": ["Let me get a specialist for you, please stay on the line."],
    },
    "send_webhook": {
        "label": "Send webhook",
        "summary": "Posts the current intake record to the agent's configured webhook(s) (SSRF-safe, signed).",
        "description": "Send the current intake record to the office system, for example when the caller asks "
                       "for a callback. Give the reason.",
        "parameters": {"type": "object", "properties": {
            "reason": {"type": "string", "description": "Why the record is being sent"}},
            "required": ["reason"]},
        "on_hold": ["One moment while I send that over."],
    },
    "end_call": {
        "label": "End call",
        "summary": "Ends the call a few seconds after the agent's goodbye and records the outcome.",
        "description": "End the call after you have said goodbye. Give the outcome.",
        "parameters": {"type": "object", "properties": {
            "outcome": {"type": "string", "description": "intake_complete, callback_requested, not_interested, "
                                                         "wrong_number, disqualified or voicemail"}},
            "required": ["outcome"]},
        "on_hold": [],
    },
}
MAX_TOOLS_PER_AGENT = 5  # the model card recommends at most 5 tools per session

TEXT_LIMITS = {
    "name": 120, "description": 600, "system_instructions": 4000, "personality": 600,
    "opening_greeting": 300, "company": 120, "brand": 120, "qualification_flow": 2000,
    "objection_handling": 1500, "conversation_rules": 1500, "prohibited_behaviour": 1500,
    "transfer_rules": 800, "business_hours_behaviour": 500, "voicemail_behaviour": 500,
    "fallback_behaviour": 500, "knowledge": 6000, "recording_notice": 300, "version_note": 200,
}


class AgentError(ValueError):
    def __init__(self, message: str, status: int = 400, code: str = "invalid_agent") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def new_agent_id() -> str:
    return "agt_" + secrets.token_hex(12)


def default_config(use_case: str = "intakepilot_mva") -> dict:
    """A complete, working IntakePilot MVA agent (the Call Agents page's template)."""
    mva = use_case == "intakepilot_mva"
    return {
        "name": "IntakePilot MVA intake" if mva else "New call agent",
        "description": "Answers motor vehicle accident enquiries, collects the intake and warm-transfers "
                       "qualified callers." if mva else "",
        "model": "gx-call",
        "voice": "Aria",
        "use_case": use_case,
        "company": "IntakePilot" if mva else "",
        "brand": "IntakePilot Injury Help Line" if mva else "",
        "system_instructions": (
            "You are the intake specialist for an injury law office. You take calls from people who were in a "
            "motor vehicle accident, collect the intake details and help them get to an attorney quickly."
            if mva else "You are a helpful phone agent."),
        "personality": "Warm, calm and efficient. Short sentences. Never rushed.",
        "opening_greeting": ("Thank you for calling the IntakePilot injury help line. I am here to help after "
                             "your accident. May I have your name?") if mva else "Hello, how can I help you today?",
        "qualification_flow": (
            "1. Ask for the caller's name and a phone number. 2. Ask when and in which state the accident "
            "happened. 3. Ask what happened and who was at fault. 4. Ask about injuries and treatment. 5. Ask if "
            "they already have an attorney. A caller qualifies when the accident happened within the state's "
            "filing deadline, someone else was at least partly at fault, the caller was injured and has no "
            "attorney.") if mva else "",
        "required_fields": list(ci.MVA_REQUIRED_DEFAULT) if mva else [],
        "optional_fields": list(ci.MVA_OPTIONAL_DEFAULT) if mva else [],
        "objection_handling": ("If the caller is unsure about calling a lawyer, explain that the consultation "
                               "is free and there is no obligation.") if mva else "",
        "conversation_rules": ("Ask one question at a time. Confirm phone numbers by repeating them. Save each "
                               "answer with update_intake_fields as soon as you hear it."),
        "prohibited_behaviour": ("Never give legal advice or promise an outcome. Never invent facts. Never ask "
                                 "for social security or bank numbers."),
        "tool_permissions": (["update_intake_fields", "lookup_accident_state_rules", "request_warm_transfer",
                              "check_business_hours", "end_call"] if mva else ["end_call"]),
        "tool_on_hold": {},
        "transfer_rules": ("Transfer qualified callers, and anyone who asks for a person, after the required "
                           "fields are saved.") if mva else "",
        "transfer_destination": {"type": "queue", "value": "intake-specialists", "integration": None},
        "business_hours": {"timezone": "America/New_York",
                           "days": {d: [["08:00", "20:00"]] for d in ("mon", "tue", "wed", "thu", "fri")},
                           "closed_dates": []},
        "business_hours_behaviour": "Outside business hours, take the intake and promise a callback next business day.",
        "voicemail_behaviour": "If you reach voicemail, leave a short message with the callback number and end the call.",
        "fallback_behaviour": "If you cannot help, offer a callback and end the call politely.",
        "knowledge": ("The office offers free consultations. Attorneys work on contingency, so there is no fee "
                      "unless the case is won.") if mva else "",
        "webhooks": [],
        "crm": [],
        "leaddistro": {"enabled": False, "integration": None, "campaign": ""},
        "post_call_actions": [],
        "structured_output_schema": copy.deepcopy(ci.MVA_SCHEMA) if mva else {
            "type": "object", "properties": {"caller_name": {"type": "string", "maxLength": 120},
                                             "notes": {"type": "string", "maxLength": 2000},
                                             "disposition": ci.MVA_SCHEMA["properties"]["disposition"]}},
        "tags": ["intakepilot", "mva"] if mva else [],
        "recording": {"enabled": False, "notice": "This call may be recorded for quality and training."},
        "retention_days": 30,
        "max_call_minutes": 20,
    }


def _text(cfg: dict, key: str, *, required: bool = False) -> str:
    value = cfg.get(key, "")
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise AgentError(f"{key} must be text")
    value = value.strip()
    if required and not value:
        raise AgentError(f"{key} is required")
    if len(value) > TEXT_LIMITS[key]:
        raise AgentError(f"{key} is longer than {TEXT_LIMITS[key]} characters")
    return value


def _url(value: object, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AgentError(f"{where}: a URL is required")
    try:
        check_url(value.strip(), allow_http=False)
    except BlockedURL as exc:
        raise AgentError(f"{where}: {exc}") from None
    return value.strip()


def _integration(item: object, where: str, *, events: bool) -> dict:
    if not isinstance(item, dict):
        raise AgentError(f"{where} must be an object")
    name = item.get("name", "")
    if not isinstance(name, str) or not INTEGRATION_NAME_RE.match(name):
        raise AgentError(f"{where}: name must be lower-case letters, digits, - or _")
    secret_ref = item.get("secret_ref") or None
    if secret_ref is not None and (not isinstance(secret_ref, str) or not SECRET_NAME_RE.match(secret_ref)):
        raise AgentError(f"{where}: secret_ref must name a stored integration secret")
    out = {"name": name, "url": _url(item.get("url"), where), "secret_ref": secret_ref,
           "enabled": item.get("enabled", True) is not False}
    if events:
        ev = item.get("events") or ["call.ended"]
        if not isinstance(ev, list) or not ev or any(e not in WEBHOOK_EVENTS for e in ev):
            raise AgentError(f"{where}: events must be a subset of {', '.join(WEBHOOK_EVENTS)}")
        out["events"] = sorted(set(ev))
    return out


def validate_config(raw: object) -> dict:
    """Return a normalised agent config or raise AgentError (server-side, every save)."""
    if not isinstance(raw, dict):
        raise AgentError("the agent must be a JSON object")
    if len(json.dumps(raw)) > 128 * 1024:
        raise AgentError("the agent configuration is larger than 128 KiB")
    cfg: dict[str, Any] = {}
    for key in TEXT_LIMITS:
        if key in ("version_note",):
            continue
        if key == "recording_notice":
            continue
        cfg[key] = _text(raw, key, required=key in ("name", "system_instructions"))
    if raw.get("model", "gx-call") != "gx-call":
        raise AgentError("model must be gx-call (the only realtime voice-agent model)")
    cfg["model"] = "gx-call"
    if raw.get("voice", "Aria") not in VOICES:
        raise AgentError(f"voice must be one of {', '.join(VOICES)} (the voices the model ships)")
    cfg["voice"] = raw.get("voice", "Aria")
    use_case = raw.get("use_case", "general")
    if use_case not in USE_CASES:
        raise AgentError(f"use_case must be one of {', '.join(USE_CASES)}")
    cfg["use_case"] = use_case
    try:
        schema = ci.check_schema(raw.get("structured_output_schema") or copy.deepcopy(ci.MVA_SCHEMA))
    except ci.StateError as exc:
        raise AgentError(f"structured output schema: {exc}") from None
    cfg["structured_output_schema"] = schema
    fields = ci.schema_fields(schema)
    for key in ("required_fields", "optional_fields"):
        value = raw.get(key) or []
        if not isinstance(value, list) or any(not isinstance(f, str) or f not in fields for f in value):
            raise AgentError(f"{key} must list fields of the structured output schema")
        cfg[key] = list(dict.fromkeys(value))
    if set(cfg["required_fields"]) & set(cfg["optional_fields"]):
        raise AgentError("a field cannot be both required and optional")
    perms = raw.get("tool_permissions") or []
    if not isinstance(perms, list) or any(p not in TOOL_CATALOG for p in perms):
        raise AgentError(f"tool_permissions must be a subset of {', '.join(TOOL_CATALOG)}")
    perms = list(dict.fromkeys(perms))
    if len(perms) > MAX_TOOLS_PER_AGENT:
        raise AgentError(f"at most {MAX_TOOLS_PER_AGENT} tools per agent (the model's tested limit)")
    cfg["tool_permissions"] = perms
    on_hold = raw.get("tool_on_hold") or {}
    if not isinstance(on_hold, dict) or any(k not in TOOL_CATALOG for k in on_hold):
        raise AgentError("tool_on_hold must map tool names to phrases")
    cfg["tool_on_hold"] = {}
    for tool, phrases in on_hold.items():
        if not isinstance(phrases, list) or len(phrases) > 4 or not all(
                isinstance(p, str) and 0 < len(p.strip()) <= 200 for p in phrases):
            raise AgentError(f"tool_on_hold.{tool}: up to 4 phrases of at most 200 characters")
        cfg["tool_on_hold"][tool] = [p.strip() for p in phrases]
    dest = raw.get("transfer_destination") or {"type": "queue", "value": "", "integration": None}
    if not isinstance(dest, dict) or dest.get("type") not in ("queue", "phone", "webhook"):
        raise AgentError("transfer_destination.type must be queue, phone or webhook")
    value = dest.get("value") or ""
    if not isinstance(value, str) or len(value) > 120:
        raise AgentError("transfer_destination.value must be text of at most 120 characters")
    if dest["type"] == "phone" and value and ci.normalize_phone(value) is None:
        raise AgentError("transfer_destination.value must be a phone number")
    integ = dest.get("integration") or None
    cfg["transfer_destination"] = {"type": dest["type"],
                                   "value": ci.normalize_phone(value) if dest["type"] == "phone" and value else value,
                                   "integration": integ}
    try:
        cfg["business_hours"] = ci.check_hours(raw.get("business_hours"))
    except ci.StateError as exc:
        raise AgentError(f"business hours: {exc}") from None
    hooks = raw.get("webhooks") or []
    crm = raw.get("crm") or []
    if not isinstance(hooks, list) or len(hooks) > 5 or not isinstance(crm, list) or len(crm) > 3:
        raise AgentError("at most 5 webhooks and 3 CRM integrations")
    cfg["webhooks"] = [_integration(h, f"webhooks[{i}]", events=True) for i, h in enumerate(hooks)]
    cfg["crm"] = [_integration(c, f"crm[{i}]", events=False) for i, c in enumerate(crm)]
    names = [x["name"] for x in cfg["webhooks"] + cfg["crm"]]
    ld = raw.get("leaddistro") or {}
    if not isinstance(ld, dict):
        raise AgentError("leaddistro must be an object")
    cfg["leaddistro"] = {"enabled": ld.get("enabled") is True, "campaign": str(ld.get("campaign") or "")[:80],
                         "integration": None}
    if cfg["leaddistro"]["enabled"]:
        cfg["leaddistro"]["integration"] = _integration(ld.get("integration"), "leaddistro.integration",
                                                        events=False)
        names.append(cfg["leaddistro"]["integration"]["name"])
    if len(names) != len(set(names)):
        raise AgentError("integration names must be unique within an agent")
    if integ is not None and integ not in names:
        raise AgentError("transfer_destination.integration must name one of this agent's integrations")
    actions = raw.get("post_call_actions") or []
    if not isinstance(actions, list) or len(actions) > 8:
        raise AgentError("at most 8 post-call actions")
    cfg["post_call_actions"] = []
    for i, act in enumerate(actions):
        if not isinstance(act, dict) or act.get("type") not in ("webhook", "crm", "leaddistro"):
            raise AgentError(f"post_call_actions[{i}].type must be webhook, crm or leaddistro")
        target = act.get("target")
        if act["type"] != "leaddistro" and target not in names:
            raise AgentError(f"post_call_actions[{i}].target must name one of this agent's integrations")
        when = act.get("when", "always")
        if when not in POST_CALL_WHEN:
            raise AgentError(f"post_call_actions[{i}].when must be one of {', '.join(POST_CALL_WHEN)}")
        cfg["post_call_actions"].append({"type": act["type"], "target": target, "when": when})
    if "send_webhook" in perms and not any(h["enabled"] for h in cfg["webhooks"]):
        raise AgentError("send_webhook needs at least one enabled webhook")
    tags = raw.get("tags") or []
    if not isinstance(tags, list) or len(tags) > 12 or any(
            not isinstance(t, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_\-]{0,31}", t) for t in tags):
        raise AgentError("tags: up to 12 lower-case tags")
    cfg["tags"] = sorted(set(tags))
    rec = raw.get("recording") or {}
    if not isinstance(rec, dict):
        raise AgentError("recording must be an object")
    notice = rec.get("notice") or ""
    if not isinstance(notice, str) or len(notice) > TEXT_LIMITS["recording_notice"]:
        raise AgentError("recording.notice must be text")
    cfg["recording"] = {"enabled": rec.get("enabled") is True, "notice": notice.strip()}
    if cfg["recording"]["enabled"] and not cfg["recording"]["notice"]:
        raise AgentError("recording needs a consent notice the agent reads at the start of the call")
    for key, lo, hi, default in (("retention_days", 1, 3650, 30), ("max_call_minutes", 1, 30, 20)):
        value = raw.get(key, default)
        if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
            raise AgentError(f"{key} must be a whole number from {lo} to {hi}")
        cfg[key] = value
    prompt = compile_prompt(cfg)
    if len(prompt) > MAX_PROMPT_CHARS:
        raise AgentError(f"the compiled instructions are {len(prompt)} characters; the limit is {MAX_PROMPT_CHARS}")
    return cfg


def _ascii(text: str) -> str:
    table = {"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": " - ",
             "…": "...", " ": " "}
    text = "".join(table.get(c, c) for c in text)
    return text.encode("ascii", "ignore").decode("ascii")


def compile_prompt(cfg: dict) -> str:
    """The system message the model receives (tools are appended by the engine's template)."""
    sections: list[str] = [cfg["system_instructions"]]
    who = " ".join(x for x in (f"You work for {cfg['company']}." if cfg.get("company") else "",
                               f"Introduce the line as {cfg['brand']}." if cfg.get("brand") else "") if x)
    if who:
        sections.append(who)
    if cfg.get("personality"):
        sections.append(f"Personality: {cfg['personality']}")
    greeting = cfg.get("opening_greeting")
    if cfg.get("recording", {}).get("enabled"):
        notice = cfg["recording"]["notice"]
        greeting = f"{notice} {greeting}".strip()
    if greeting:
        sections.append(f"Start the call right away by saying: \"{greeting}\"")
    if cfg.get("qualification_flow"):
        sections.append(f"Qualification flow: {cfg['qualification_flow']}")
    fields = ci.schema_fields(cfg["structured_output_schema"])
    if cfg.get("required_fields"):
        sections.append("Required intake fields (collect all of them): " + ", ".join(
            f"{f} ({fields[f].get('title', f)})" for f in cfg["required_fields"]))
    if cfg.get("optional_fields"):
        sections.append("Optional fields (collect when natural): " + ", ".join(cfg["optional_fields"]))
    if "update_intake_fields" in cfg.get("tool_permissions", []):
        enums = [f"{f}: {' / '.join(map(str, spec['enum']))}" for f, spec in fields.items()
                 if "enum" in spec and f in set(cfg["required_fields"]) | set(cfg["optional_fields"])]
        if enums:
            sections.append("Allowed values: " + "; ".join(enums))
    for key, label in (("conversation_rules", "Conversation rules"), ("objection_handling", "Objections"),
                       ("prohibited_behaviour", "Never do this"), ("transfer_rules", "Transfers"),
                       ("business_hours_behaviour", "Outside business hours"),
                       ("voicemail_behaviour", "Voicemail"), ("fallback_behaviour", "Fallback"),
                       ("knowledge", "Facts you may use")):
        if cfg.get(key):
            sections.append(f"{label}: {cfg[key]}")
    sections.append("DO NOT interrupt the caller while they are speaking; let them finish their turn. Keep answers "
                    "short. Tool-call arguments must be values the caller spoke; if one is missing, ask.")
    return _ascii("\n\n".join(s.strip() for s in sections if s.strip()))


def compile_tools(cfg: dict) -> list[dict]:
    tools = []
    for name in cfg.get("tool_permissions", []):
        spec = TOOL_CATALOG[name]
        tools.append({"name": name, "description": spec["description"], "parameters": spec["parameters"],
                      "on_hold": cfg.get("tool_on_hold", {}).get(name) or spec["on_hold"]})
    return tools


def compile_for_engine(agent_id: str, version: int, cfg: dict) -> dict:
    return {"agent_id": agent_id, "version": version, "name": cfg["name"],
            "system_prompt": compile_prompt(cfg), "tools": compile_tools(cfg)}


def config_sha(cfg: dict) -> str:
    return hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()


def diff_configs(a: dict, b: dict) -> list[dict]:
    """Field-level diff; long texts get a unified diff."""
    out = []
    for key in sorted(set(a) | set(b)):
        va, vb = a.get(key), b.get(key)
        if va == vb:
            continue
        entry: dict[str, Any] = {"field": key}
        if isinstance(va, str) and isinstance(vb, str) and (len(va) > 80 or len(vb) > 80):
            entry["unified"] = "\n".join(difflib.unified_diff(va.splitlines(), vb.splitlines(), "before",
                                                              "after", lineterm="", n=1))
        else:
            entry["before"], entry["after"] = va, vb
        out.append(entry)
    return out


class AgentStore:
    """call_agents / call_agent_versions on the application database."""

    def __init__(self, connect: Callable, audit: Callable[..., None] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.connect = connect
        self.audit = (lambda **_: None) if audit is None else audit
        self.clock = clock

    @staticmethod
    def _row(r: Any) -> dict:
        d = dict(r)
        d["tags"] = json.loads(d.get("tags") or "[]")
        return d

    def list(self, *, include_archived: bool = False, status: str | None = None) -> list[dict]:
        sql = "SELECT * FROM call_agents"
        args: list = []
        where = []
        if status:
            where.append("status = ?")
            args.append(status)
        elif not include_archived:
            where.append("status != 'archived'")
        if where:
            sql += " WHERE " + " AND ".join(where)
        with self.connect() as con:
            rows = [self._row(r) for r in con.execute(sql + " ORDER BY updated_at DESC", args)]
        return rows

    def get(self, agent_id: str, version: int | None = None) -> dict:
        if not AGENT_ID_RE.match(agent_id or ""):
            raise AgentError("no such agent", 404, "not_found")
        with self.connect() as con:
            row = con.execute("SELECT * FROM call_agents WHERE agent_id = ?", (agent_id,)).fetchone()
            if row is None:
                raise AgentError("no such agent", 404, "not_found")
            agent = self._row(row)
            v = version or agent["current_version"]
            ver = con.execute("SELECT * FROM call_agent_versions WHERE agent_id = ? AND version = ?",
                              (agent_id, v)).fetchone()
            if ver is None:
                raise AgentError("no such agent version", 404, "not_found")
        agent["version"] = v
        agent["config"] = json.loads(ver["config"])
        agent["config_sha256"] = ver["config_sha256"]
        agent["version_note"] = ver["note"]
        agent["version_created_at"] = ver["created_at"]
        agent["version_created_by"] = ver["created_by"]
        return agent

    def versions(self, agent_id: str) -> list[dict]:
        self.get(agent_id)
        with self.connect() as con:
            return [dict(r) for r in con.execute(
                "SELECT agent_id, version, config_sha256, note, created_at, created_by FROM call_agent_versions "
                "WHERE agent_id = ? ORDER BY version DESC", (agent_id,))]

    def create(self, raw: dict, *, user: str, status: str = "draft", mode: str = "test",
               cloned_from: str | None = None, note: str | None = None) -> dict:
        cfg = validate_config(raw)
        if status not in STATUSES or mode not in MODES:
            raise AgentError("invalid status or mode")
        agent_id = new_agent_id()
        now = self.clock()
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                con.execute("INSERT INTO call_agents (agent_id, name, status, mode, current_version, tags, use_case, "
                            "created_at, updated_at, created_by, updated_by, cloned_from) "
                            "VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)",
                            (agent_id, cfg["name"], status, mode, json.dumps(cfg["tags"]), cfg["use_case"], now, now,
                             user, user, cloned_from))
                con.execute("INSERT INTO call_agent_versions (agent_id, version, config, config_sha256, note, "
                            "created_at, created_by) VALUES (?, 1, ?, ?, ?, ?, ?)",
                            (agent_id, json.dumps(cfg, sort_keys=True), config_sha(cfg), (note or "created")[:200],
                             now, user))
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
        self.audit(user=user, action="call.agent.create", outcome="ok", agent_id=agent_id)
        return self.get(agent_id)

    def save(self, agent_id: str, raw: dict, *, user: str, base_version: int | None = None,
             note: str | None = None) -> dict:
        """New version (idempotent: an unchanged config returns the current version)."""
        cfg = validate_config(raw)
        current = self.get(agent_id)
        if current["status"] == "archived":
            raise AgentError("restore the agent before editing it", 409, "archived")
        if base_version is not None and base_version != current["current_version"]:
            raise AgentError(f"the agent was changed meanwhile (now version {current['current_version']}); "
                             "reload and try again", 409, "version_conflict")
        if config_sha(cfg) == current["config_sha256"]:
            return {**current, "unchanged": True}
        now = self.clock()
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                row = con.execute("SELECT current_version FROM call_agents WHERE agent_id = ?", (agent_id,)).fetchone()
                version = int(row["current_version"]) + 1
                con.execute("INSERT INTO call_agent_versions (agent_id, version, config, config_sha256, note, "
                            "created_at, created_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (agent_id, version, json.dumps(cfg, sort_keys=True), config_sha(cfg),
                             (note or "")[:200] or None, now, user))
                con.execute("UPDATE call_agents SET current_version = ?, name = ?, tags = ?, use_case = ?, "
                            "updated_at = ?, updated_by = ? WHERE agent_id = ?",
                            (version, cfg["name"], json.dumps(cfg["tags"]), cfg["use_case"], now, user, agent_id))
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
        self.audit(user=user, action="call.agent.save", outcome="ok", agent_id=agent_id, version=version)
        return self.get(agent_id)

    def set_status(self, agent_id: str, *, user: str, status: str | None = None, mode: str | None = None) -> dict:
        agent = self.get(agent_id)
        status = status or agent["status"]
        mode = mode or agent["mode"]
        if status not in STATUSES or mode not in MODES:
            raise AgentError("invalid status or mode")
        if status == "enabled":
            validate_config(agent["config"])  # never enable a config that no longer validates
        with self.connect() as con:
            con.execute("UPDATE call_agents SET status = ?, mode = ?, updated_at = ?, updated_by = ? "
                        "WHERE agent_id = ?", (status, mode, self.clock(), user, agent_id))
        self.audit(user=user, action="call.agent.status", outcome="ok", agent_id=agent_id, status=status, mode=mode)
        return self.get(agent_id)

    def clone(self, agent_id: str, *, user: str, name: str | None = None, version: int | None = None) -> dict:
        src = self.get(agent_id, version)
        cfg = copy.deepcopy(src["config"])
        cfg["name"] = (name or f"{cfg['name']} (copy)")[:TEXT_LIMITS["name"]]
        return self.create(cfg, user=user, cloned_from=f"{agent_id}@{src['version']}",
                           note=f"cloned from {agent_id} version {src['version']}")
