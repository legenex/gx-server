"""AI flow creation: a natural-language request -> an editable, valid graph.

gx-auto (or another text alias) receives the live node catalogue and the
typed-edge rules and must answer with a JSON graph (response_format
json_schema). The answer is NEVER trusted: it is normalised (ids, unknown
settings dropped with a warning) and validated with the same code as every
other flow (``schema.validate_document``). Validation problems go back to the
model for a bounded number of repair rounds; if it still fails, the request
fails with the issues instead of storing a broken graph. Nothing is saved or
executed here; the user edits the draft first.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from . import catalog as cat
from .schema import ID_RE, FlowValidationError, readiness, validate_document
from .services import LLMClient, NodeFailure, parse_json_answer
from .templates import auto_layout

MAX_PROMPT = 2000
REPAIRS = 2
ALLOWED_MODELS = ("gx-auto", "gx-fast", "gx-reason", "gx-mini")

SYSTEM = """You design node graphs for "Creative Flows", a visual pipeline editor on a private GPU cluster.
Reply with ONE JSON object only (no markdown), shaped like:
{"name": str, "description": str,
 "nodes": [{"id": str, "type": str, "label": str, "config": {field: value}}],
 "edges": [{"source": node id, "source_port": output port id, "target": node id, "target_port": input port id}],
 "variables": [{"key": str, "value": str}]}

Rules:
- Use ONLY the node types listed below, their exact port ids and field ids. Never invent types, ports or fields.
- Node ids: short snake_case words (letters, digits, _), unique.
- An edge is allowed only if the source port's type is one of the target port's accepted types. "any" outputs
  carry the type of the node's input. There are no implicit conversions (text->json needs
  ai.structured, json->text needs util.select).
- A port marked "single" takes at most one edge. Required inputs must be connected, or (where a field "fills"
  the port) the field must be filled in the config.
- Lists: ai.script_writer "visuals" and ai.scene_prompts "prompts" emit one item per scene; a node with a single
  input that receives N items runs N times (N images, N clips). Multi inputs (compose.concat) collect them all.
- Write complete, specific prompts in config (visual prompts describe subject, setting, camera, lighting).
- Prefer this production pattern for video ads: text.input (brief) -> ai.script_writer; narration -> voice.tts;
  visuals -> ai.prompt_enhancer (target image) -> image.generate -> video.i2v -> compose.concat;
  compose.add_voice (video + voice) -> compose.add_music (music.instrumental) -> compose.captions (call to action
  from util.select path "cta" on the script JSON, last_seconds 4) -> compose.export.
- Vertical social video: image.generate size 928x1664, video.i2v size 480x832, compose.export preset 1080x1920.
- Keep total generated video short (each clip 5 seconds, at most 4 clips) unless asked otherwise.
- Voices: use voice.tts with one of the listed voice ids (field voice_id) and a delivery style, or voice.design
  (description) connected to voice.tts "voice".
"""


def catalogue_prompt(available: dict[str, bool]) -> str:
    lines = []
    for n in cat.NODES.values():
        if not n.available or not available.get(n.type, True):
            continue
        ins = ", ".join(f"{p.id}:{'|'.join(p.types)}{'' if p.multiple else ' single'}"
                        f"{' required' if p.required else ''}" for p in n.inputs) or "-"
        outs = ", ".join(f"{p.id}:{p.types[0]}" for p in n.outputs) or "-"
        fields = []
        for f in n.fields:
            desc = f.id
            if f.kind == "select" and f.options and not f.source:
                desc += "=" + "/".join(v for v, _ in f.options if v)[:120]
            elif f.kind == "number":
                desc += f"({f.min:g}..{f.max:g})"
            elif f.kind in ("tags", "keyvalue", "headers"):
                desc += f"[{f.kind}]"
            if f.fills:
                desc += f" fills {f.fills}"
            if f.required:
                desc += "*"
            fields.append(desc)
        lines.append(f"- {n.type}: {n.description} IN[{ins}] OUT[{outs}] FIELDS[{', '.join(fields)}]")
    return "\n".join(lines)


ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "description": {"type": "string"},
        "nodes": {"type": "array", "minItems": 1, "maxItems": 40, "items": {
            "type": "object",
            "properties": {"id": {"type": "string"}, "type": {"type": "string"}, "label": {"type": "string"},
                           "config": {"type": "object"}},
            "required": ["id", "type", "config"]}},
        "edges": {"type": "array", "maxItems": 80, "items": {
            "type": "object",
            "properties": {"source": {"type": "string"}, "source_port": {"type": "string"},
                           "target": {"type": "string"}, "target_port": {"type": "string"}},
            "required": ["source", "source_port", "target", "target_port"]}},
        "variables": {"type": "array", "items": {
            "type": "object", "properties": {"key": {"type": "string"}, "value": {"type": "string"}},
            "required": ["key", "value"]}},
    },
    "required": ["name", "nodes", "edges"],
}


def _slug(text: str, used: set[str]) -> str:
    base = re.sub(r"[^A-Za-z0-9_\-]+", "_", text or "node").strip("_")[:32] or "node"
    candidate, i = base, 2
    while candidate in used:
        candidate = f"{base[:28]}_{i}"
        i += 1
    used.add(candidate)
    return candidate


def normalise(answer: Any, *, voices: list[str]) -> tuple[dict[str, Any], list[str]]:
    """Model answer -> a candidate document (+ warnings). Structure only."""
    warnings: list[str] = []
    if not isinstance(answer, dict):
        raise ValueError("the answer is not a JSON object")
    raw_nodes = answer.get("nodes")
    raw_edges = answer.get("edges") or []
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise ValueError("the answer has no nodes")
    if not isinstance(raw_edges, list):
        raise ValueError("edges must be a list")
    used: set[str] = set()
    idmap: dict[str, str] = {}
    nodes = []
    for raw in raw_nodes[:40]:
        if not isinstance(raw, dict):
            continue
        ntype = raw.get("type")
        nt = cat.NODES.get(ntype) if isinstance(ntype, str) else None
        if nt is None:
            warnings.append(f"dropped a node of unknown type {str(ntype)[:40]!r}")
            continue
        if not nt.available:
            warnings.append(f"dropped {nt.label}: {nt.unavailable_reason}")
            continue
        old = str(raw.get("id") or nt.type)
        new = old if ID_RE.fullmatch(old) and old not in used else _slug(old, used)
        used.add(new)
        idmap[old] = new
        raw_config = raw.get("config")
        config: dict[str, Any] = raw_config if isinstance(raw_config, dict) else {}
        known = {f.id for f in nt.fields}
        dropped = sorted(set(config) - known)
        if dropped:
            warnings.append(f"{nt.label}: ignored unknown setting(s) {', '.join(dropped[:5])}")
        clean = {k: v for k, v in config.items() if k in known}
        voice_field = nt.field("voice_id")
        if voice_field is not None and clean.get("voice_id") and clean["voice_id"] not in voices:
            warnings.append(f"{nt.label}: voice {str(clean['voice_id'])[:40]!r} does not exist; choose one")
            clean.pop("voice_id")
        raw_label = raw.get("label")
        label = raw_label if isinstance(raw_label, str) else ""
        nodes.append({"id": new, "type": nt.type, "label": label[:80], "config": clean,
                      "position": {"x": 0.0, "y": 0.0}, "disabled": False, "locked": False, "notes": ""})
    edges = []
    for i, raw in enumerate(raw_edges[:80]):
        if not isinstance(raw, dict):
            continue
        src, dst = idmap.get(str(raw.get("source"))), idmap.get(str(raw.get("target")))
        if src is None or dst is None:
            warnings.append("dropped a connection to a node that does not exist")
            continue
        edges.append({"id": f"e{i + 1}", "source": src, "source_port": str(raw.get("source_port") or ""),
                      "target": dst, "target_port": str(raw.get("target_port") or "")})
    variables = {}
    for item in answer.get("variables") or []:
        if isinstance(item, dict) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", str(item.get("key"))):
            variables[str(item["key"])] = str(item.get("value", ""))[:2000]
    raw_name, raw_desc = answer.get("name"), answer.get("description")
    name = raw_name if isinstance(raw_name, str) and raw_name.strip() else "AI flow"
    description = raw_desc if isinstance(raw_desc, str) else ""
    return {"schema": 1, "name": name.strip()[:120], "description": description[:2000], "nodes": nodes,
            "edges": edges, "variables": variables, "viewport": {"x": 40.0, "y": 40.0, "zoom": 0.7}}, warnings


def _drop_bad(doc: dict[str, Any], issues: list[dict[str, Any]], warnings: list[str]) -> dict[str, Any]:
    """Last resort after the repair rounds: remove what is still invalid."""
    bad_edges = {i.get("edge_id") for i in issues if i.get("edge_id")}
    bad_fields = {(i.get("node_id"), i.get("field")) for i in issues if i.get("field")}
    for issue in issues:
        warnings.append(f"removed: {issue['message']}")
    doc["edges"] = [e for e in doc["edges"] if e["id"] not in bad_edges]
    for node in doc["nodes"]:
        for nid, fid in bad_fields:
            if nid == node["id"]:
                node["config"].pop(fid, None)
    return doc


def generate(llm: LLMClient, prompt: str, *, model: str = "gx-auto", voices: list[dict[str, Any]] | None = None,
             available: dict[str, bool] | None = None, check: Callable[[], None] | None = None) -> dict[str, Any]:
    """Build a validated draft graph from ``prompt``."""
    prompt = (prompt or "").strip()
    if not prompt:
        raise NodeFailure("describe the flow you want", code="missing_prompt")
    if len(prompt) > MAX_PROMPT:
        raise NodeFailure(f"the request is longer than {MAX_PROMPT} characters", code="too_long")
    if model not in ALLOWED_MODELS:
        raise NodeFailure(f"model must be one of {', '.join(ALLOWED_MODELS)}", code="bad_model")
    voice_ids = [str(v.get("id")) for v in voices or [] if v.get("id")]
    voice_lines = "\n".join(f"- {v.get('id')}: {v.get('name') or ''} {v.get('description') or ''}".strip()
                            for v in (voices or [])[:30]) or "- (gx-voice is not available: do not use voice nodes)"
    catalogue = catalogue_prompt(available or {})
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": f"NODE TYPES:\n{catalogue}\n\nVOICES:\n{voice_lines}\n\nREQUEST:\n{prompt}"},
    ]
    warnings: list[str] = []
    attempts = 0
    usage: list[Any] = []
    last_issues: list[dict[str, Any]] = []
    doc: dict[str, Any] | None = None
    meta: dict[str, Any] = {}
    for attempt in range(REPAIRS + 1):
        if check is not None:
            check()
        attempts = attempt + 1
        text, meta = llm.chat(model, messages, temperature=0.4, max_tokens=6000, schema=ANSWER_SCHEMA,
                              timeout=900)
        usage.append(meta.get("usage"))
        try:
            answer = parse_json_answer(text)
            doc, step_warnings = normalise(answer, voices=voice_ids)
        except ValueError as exc:
            problem = str(exc)
            last_issues = [{"message": problem, "code": "invalid_json"}]
            messages = messages[:2] + [{"role": "assistant", "content": text[:8000]},
                                       {"role": "user", "content": f"Invalid: {problem}. Reply with the complete "
                                                                   "corrected JSON object only."}]
            doc = None
            continue
        try:
            clean = validate_document(auto_layout(doc))
        except FlowValidationError as exc:
            last_issues = exc.issues
            listing = "\n".join(f"- {i['message']}" for i in exc.issues[:15])
            messages = messages[:2] + [
                {"role": "assistant", "content": json.dumps(answer)[:12000]},
                {"role": "user", "content": f"The graph is not valid:\n{listing}\nFix every problem and reply "
                                            "with the complete corrected JSON object only."}]
            continue
        warnings = step_warnings
        return _result(clean, warnings, model, attempts, meta, usage)
    if doc is None:
        raise NodeFailure(f"{model} did not produce a usable graph: "
                          f"{'; '.join(i['message'] for i in last_issues[:3])}", code="ai_invalid")
    fixed = _drop_bad(doc, last_issues, warnings)
    try:
        clean = validate_document(auto_layout(fixed))
    except FlowValidationError as exc:
        raise NodeFailure("the generated graph is still invalid after repairs: "
                          + "; ".join(i["message"] for i in exc.issues[:3]), code="ai_invalid") from None
    return _result(clean, warnings, model, attempts, meta, usage)


def _result(doc: dict[str, Any], warnings: list[str], model: str, attempts: int, meta: dict[str, Any],
            usage: list[Any]) -> dict[str, Any]:
    return {"graph": doc, "warnings": warnings[:30], "model": model,
            "model_used": meta.get("routed_to") or meta.get("model_used"), "attempts": attempts,
            "usage": usage, "readiness": readiness(doc)}
