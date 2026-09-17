"""Flow documents: runtime schema validation and typed-edge rules (FLO).

A flow document is untrusted input (browser, public API, AI generation). It
is validated completely here before it is stored or executed:

* structure, identifiers, sizes and positions;
* every node type exists in the catalogue and every config value matches its
  field specification (type, range, length, options, pattern); unknown keys
  are refused;
* every edge joins existing ports, the source's resolved type is accepted by
  the target port, a single-input port has at most one edge, no self-loops,
  no duplicates, and the graph is acyclic.

"Readiness" (required inputs connected or filled, Library assets chosen,
node types available) is reported separately: an unfinished flow can be
saved, but not run.
"""

from __future__ import annotations

import copy
import json
import math
import re
from dataclasses import dataclass
from typing import Any

from . import catalog as cat
from .graph import CycleError, Graph

SCHEMA_VERSION = 1
MAX_NODES = 150
MAX_EDGES = 400
MAX_VARIABLES = 64
MAX_DOC_BYTES = 900 * 1024
ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,40}$")
VAR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
KV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_ \-]{0,63}$")
ASSET_RE = re.compile(r"^a_[0-9a-f]{24}$")
DYNAMIC_OPTION_RE = re.compile(r"^[A-Za-z0-9_.:/@+\-]{0,128}$")
SECRET_RE = re.compile(cat.SECRET_NAME)
HEADER_RE = re.compile(cat.HEADER_NAME)
FORBIDDEN_HEADERS = frozenset({"host", "content-length", "transfer-encoding", "connection", "cookie", "upgrade",
                               "proxy-authorization", "te", "trailer", "x-gx-proxy-token", "x-gx-forwarded-for"})
STRUCT_TYPES = ("string", "number", "boolean", "list")
NODE_KEYS = frozenset({"id", "type", "label", "position", "config", "disabled", "locked", "notes"})
EDGE_KEYS = frozenset({"id", "source", "source_port", "target", "target_port"})
DOC_KEYS = frozenset({"schema", "name", "description", "nodes", "edges", "variables", "viewport"})


class FlowValidationError(ValueError):
    """The document is malformed. ``issues`` lists every problem found."""

    def __init__(self, issues: list[dict[str, Any]]) -> None:
        self.issues = issues[:50]
        first = issues[0]["message"] if issues else "invalid flow"
        more = f" (and {len(issues) - 1} more)" if len(issues) > 1 else ""
        super().__init__(f"{first}{more}")


@dataclass
class _Ctx:
    issues: list[dict[str, Any]]

    def add(self, message: str, *, code: str = "invalid", node: str | None = None, edge: str | None = None,
            field: str | None = None) -> None:
        item: dict[str, Any] = {"message": message, "code": code}
        if node:
            item["node_id"] = node
        if edge:
            item["edge_id"] = edge
        if field:
            item["field"] = field
        self.issues.append(item)


def _str(value: Any, limit: int) -> bool:
    return isinstance(value, str) and len(value) <= limit and "\x00" not in value


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


# ------------------------------------------------------------- field values
def clean_value(f: cat.Field, value: Any, ctx: _Ctx, node_id: str) -> Any:
    """Validate one config value; returns the normalised value."""
    where = {"node": node_id, "field": f.id}
    label = f"{f.label}"
    if value is None:
        return None
    kind = f.kind
    if kind in ("text", "textarea"):
        limit = f.max_length or 4000
        if not _str(value, limit):
            ctx.add(f"{label} must be text of at most {limit} characters", **where)
            return None
        if f.pattern and value and not re.fullmatch(f.pattern, value):
            ctx.add(f"{label} has an invalid format", **where)
            return None
        return value
    if kind == "select":
        if not isinstance(value, str):
            ctx.add(f"{label} must be one of the listed options", **where)
            return None
        if f.source:
            if not DYNAMIC_OPTION_RE.fullmatch(value):
                ctx.add(f"{label} is not a valid choice", **where)
                return None
            return value
        if value not in {v for v, _ in f.options}:
            ctx.add(f"{label} must be one of: {', '.join(v for v, _ in f.options)}", **where)
            return None
        return value
    if kind in ("number", "seed"):
        if not _finite(value):
            ctx.add(f"{label} must be a number", **where)
            return None
        if (f.integer or kind == "seed") and float(value) != int(value):
            ctx.add(f"{label} must be a whole number", **where)
            return None
        if (f.min is not None and value < f.min) or (f.max is not None and value > f.max):
            ctx.add(f"{label} must be between {f.min:g} and {f.max:g}", **where)
            return None
        return int(value) if (f.integer or kind == "seed") else float(value)
    if kind == "boolean":
        if not isinstance(value, bool):
            ctx.add(f"{label} must be true or false", **where)
            return None
        return value
    if kind == "asset":
        if not isinstance(value, str) or not ASSET_RE.fullmatch(value):
            ctx.add(f"{label} must be a Library asset id", **where)
            return None
        return value
    if kind == "tags":
        limit = f.max_length or 12
        if not isinstance(value, list) or len(value) > limit or not all(_str(t, 40) and t.strip() for t in value):
            ctx.add(f"{label}: at most {limit} tags of up to 40 characters", **where)
            return None
        return [t.strip() for t in value]
    if kind == "keyvalue":
        limit = f.max_length or 32
        if not isinstance(value, list) or len(value) > limit:
            ctx.add(f"{label}: at most {limit} entries", **where)
            return None
        out = []
        seen = set()
        for item in value:
            if not isinstance(item, dict) or set(item) - {"key", "value"}:
                ctx.add(f"{label}: each entry needs a key and a value", **where)
                return None
            key, val = item.get("key"), item.get("value", "")
            if not isinstance(key, str) or not KV_KEY_RE.fullmatch(key) or not _str(val, 2000):
                ctx.add(f"{label}: invalid entry {str(key)[:40]!r}", **where)
                return None
            if key in seen:
                ctx.add(f"{label}: duplicate key {key!r}", **where)
                return None
            if f.id == "schema" and val not in STRUCT_TYPES:
                ctx.add(f"{label}: {key} must be one of {', '.join(STRUCT_TYPES)}", **where)
                return None
            seen.add(key)
            out.append({"key": key, "value": val})
        return out
    if kind == "headers":
        limit = f.max_length or 8
        if not isinstance(value, list) or len(value) > limit:
            ctx.add(f"{label}: at most {limit} headers", **where)
            return None
        out = []
        for item in value:
            if not isinstance(item, dict) or set(item) - {"header", "secret"}:
                ctx.add(f"{label}: each entry needs a header name and a secret name", **where)
                return None
            header, secret = item.get("header"), item.get("secret")
            if not isinstance(header, str) or not HEADER_RE.fullmatch(header) \
                    or header.lower() in FORBIDDEN_HEADERS:
                ctx.add(f"{label}: header {str(header)[:40]!r} is not allowed", **where)
                return None
            if not isinstance(secret, str) or not SECRET_RE.fullmatch(secret):
                ctx.add(f"{label}: secret names are letters, digits, _ and -", **where)
                return None
            out.append({"header": header, "secret": secret})
        return out
    ctx.add(f"{label}: unsupported field kind", **where)
    return None


def clean_config(nt: cat.NodeType, config: Any, ctx: _Ctx, node_id: str) -> dict[str, Any]:
    if config is None:
        config = {}
    if not isinstance(config, dict):
        ctx.add("config must be an object", node=node_id)
        return {}
    unknown = sorted(set(config) - {f.id for f in nt.fields})
    if unknown:
        ctx.add(f"{nt.label}: unknown setting(s) {', '.join(unknown[:5])}", node=node_id, code="unknown_field")
    out: dict[str, Any] = {}
    for f in nt.fields:
        if f.id in config:
            value = clean_value(f, config[f.id], ctx, node_id)
            if value is not None:
                out[f.id] = value
    return out


# ----------------------------------------------------------------- types
def resolve_types(nodes: dict[str, dict], edges: list[dict], order: list[str]) -> dict[tuple[str, str], str | None]:
    """Resolved type of every output port: (node_id, port_id) -> type or None."""
    incoming: dict[tuple[str, str], list[dict]] = {}
    for e in edges:
        incoming.setdefault((e["target"], e["target_port"]), []).append(e)
    resolved: dict[tuple[str, str], str | None] = {}
    for nid in order:
        node = nodes[nid]
        nt = cat.NODES[node["type"]]
        for p in nt.outputs:
            typ: str | None = p.types[0]
            if typ == "any":
                typ = None
                if nt.type == "util.file_input":
                    typ = node["config"].get("asset_type") or None
                elif p.same_as:
                    for e in incoming.get((nid, p.same_as), []):
                        typ = resolved.get((e["source"], e["source_port"]))
                        if typ:
                            break
            resolved[(nid, p.id)] = typ
    return resolved


def edge_problem(source_type: str | None, target: cat.Port) -> str | None:
    """Why an edge of ``source_type`` into ``target`` is not allowed (None = ok)."""
    if source_type is None:
        return None  # an unresolved pass-through; re-checked once its input is connected
    if source_type not in target.types:
        accepted = " or ".join(target.types)
        return f"{target.label} accepts {accepted}, not {source_type}"
    return None


# --------------------------------------------------------------- document
def validate_document(doc: Any) -> dict[str, Any]:
    """Validate and normalise a flow document. Raises FlowValidationError."""
    ctx = _Ctx([])
    if not isinstance(doc, dict):
        raise FlowValidationError([{"message": "a flow must be a JSON object", "code": "invalid"}])
    try:
        size = len(json.dumps(doc))
    except (TypeError, ValueError):
        raise FlowValidationError([{"message": "the flow is not valid JSON data", "code": "invalid"}]) from None
    if size > MAX_DOC_BYTES:
        raise FlowValidationError([{"message": f"the flow is larger than {MAX_DOC_BYTES // 1024} KiB",
                                    "code": "too_large"}])
    unknown = sorted(set(doc) - DOC_KEYS)
    if unknown:
        ctx.add(f"unknown flow key(s): {', '.join(unknown[:5])}")
    name = doc.get("name", "Untitled flow")
    if not _str(name, 120) or not str(name).strip():
        ctx.add("name must be 1-120 characters", field="name")
        name = "Untitled flow"
    description = doc.get("description", "")
    if not _str(description, 2000):
        ctx.add("description must be at most 2000 characters", field="description")
        description = ""
    variables = doc.get("variables") or {}
    clean_vars: dict[str, str] = {}
    if not isinstance(variables, dict) or len(variables) > MAX_VARIABLES:
        ctx.add(f"variables must be an object with at most {MAX_VARIABLES} entries", field="variables")
    else:
        for k, v in variables.items():
            if not VAR_RE.fullmatch(str(k)) or not _str(v, 2000):
                ctx.add(f"variable {str(k)[:40]!r} is invalid (letters, digits, _; text up to 2000)",
                        field="variables")
                continue
            clean_vars[k] = v
    viewport = doc.get("viewport") or {"x": 0, "y": 0, "zoom": 1}
    if not (isinstance(viewport, dict) and set(viewport) <= {"x", "y", "zoom"}
            and all(_finite(viewport.get(k, 0)) and abs(viewport.get(k, 0)) < 1e7 for k in ("x", "y"))
            and _finite(viewport.get("zoom", 1)) and 0.02 <= viewport.get("zoom", 1) <= 8):
        ctx.add("viewport is invalid", field="viewport")
        viewport = {"x": 0, "y": 0, "zoom": 1}

    raw_nodes = doc.get("nodes") or []
    raw_edges = doc.get("edges") or []
    if not isinstance(raw_nodes, list) or len(raw_nodes) > MAX_NODES:
        raise FlowValidationError([{"message": f"a flow has at most {MAX_NODES} nodes", "code": "too_large"}])
    if not isinstance(raw_edges, list) or len(raw_edges) > MAX_EDGES:
        raise FlowValidationError([{"message": f"a flow has at most {MAX_EDGES} connections", "code": "too_large"}])

    nodes: dict[str, dict] = {}
    for raw in raw_nodes:
        if not isinstance(raw, dict):
            ctx.add("every node must be an object")
            continue
        nid = raw.get("id")
        if not isinstance(nid, str) or not ID_RE.fullmatch(nid):
            ctx.add("node ids are 1-40 letters, digits, _ or -", code="bad_id")
            continue
        if nid in nodes:
            ctx.add(f"duplicate node id {nid}", node=nid, code="duplicate")
            continue
        extra = sorted(set(raw) - NODE_KEYS)
        if extra:
            ctx.add(f"unknown node key(s): {', '.join(extra[:5])}", node=nid)
        ntype = raw.get("type")
        if ntype not in cat.NODES:
            ctx.add(f"unknown node type {str(ntype)[:60]!r}", node=nid, code="unknown_type")
            continue
        nt = cat.NODES[ntype]
        pos = raw.get("position") or {"x": 0, "y": 0}
        if not (isinstance(pos, dict) and set(pos) <= {"x", "y"}
                and all(_finite(pos.get(k, 0)) and abs(pos.get(k, 0)) <= 1e6 for k in ("x", "y"))):
            ctx.add("node position must be finite x/y within +/-1e6", node=nid)
            pos = {"x": 0, "y": 0}
        label = raw.get("label") or ""
        if not _str(label, 80):
            ctx.add("node name must be at most 80 characters", node=nid, field="label")
            label = ""
        notes = raw.get("notes") or ""
        if not _str(notes, 2000):
            ctx.add("node notes must be at most 2000 characters", node=nid, field="notes")
            notes = ""
        flags = {}
        for flag in ("disabled", "locked"):
            value = raw.get(flag, False)
            if not isinstance(value, bool):
                ctx.add(f"{flag} must be true or false", node=nid)
                value = False
            flags[flag] = value
        node = {"id": nid, "type": ntype, "label": label.strip(), "notes": notes,
                "position": {"x": float(pos.get("x", 0)), "y": float(pos.get("y", 0))},
                "config": clean_config(nt, raw.get("config"), ctx, nid), **flags}
        nodes[nid] = node

    edges: list[dict] = []
    seen_ids: set[str] = set()
    seen_links: set[tuple[str, str, str, str]] = set()
    single_used: set[tuple[str, str]] = set()
    for raw in raw_edges:
        if not isinstance(raw, dict) or set(raw) - EDGE_KEYS:
            ctx.add("every connection needs id, source, source_port, target and target_port", code="bad_edge")
            continue
        eid = raw.get("id")
        if not isinstance(eid, str) or not ID_RE.fullmatch(eid) or eid in seen_ids:
            ctx.add("connection ids must be unique (1-40 letters, digits, _ or -)", code="bad_edge")
            continue
        seen_ids.add(eid)
        src, sport, dst, dport = (raw.get(k) for k in ("source", "source_port", "target", "target_port"))
        if src not in nodes or dst not in nodes:
            ctx.add("a connection refers to a node that does not exist", edge=eid, code="bad_edge")
            continue
        if src == dst:
            ctx.add("a node cannot be connected to itself", edge=eid, code="self_loop")
            continue
        s_nt, d_nt = cat.NODES[nodes[src]["type"]], cat.NODES[nodes[dst]["type"]]
        if not isinstance(sport, str) or s_nt.output(sport) is None:
            ctx.add(f"{s_nt.label} has no output {str(sport)[:40]!r}", edge=eid, code="bad_port")
            continue
        target_port = d_nt.input(dport) if isinstance(dport, str) else None
        if target_port is None:
            ctx.add(f"{d_nt.label} has no input {str(dport)[:40]!r}", edge=eid, code="bad_port")
            continue
        src, sport, dst, dport = str(src), str(sport), str(dst), str(dport)
        link = (src, sport, dst, dport)
        if link in seen_links:
            ctx.add("duplicate connection", edge=eid, code="duplicate")
            continue
        seen_links.add(link)
        if not target_port.multiple:
            if (dst, dport) in single_used:
                ctx.add(f"{d_nt.label}: '{target_port.label}' takes a single connection", edge=eid,
                        code="port_full")
                continue
            single_used.add((dst, dport))
        edges.append({"id": eid, "source": src, "source_port": sport, "target": dst, "target_port": dport})

    graph = Graph(list(nodes), [(e["source"], e["target"]) for e in edges])
    try:
        order = graph.topo_order()
    except CycleError as exc:
        ctx.add(f"the flow has a cycle through {', '.join(exc.nodes[:6])}; flows must be acyclic",
                code="cycle", node=exc.nodes[0])
        raise FlowValidationError(ctx.issues) from None
    resolved = resolve_types(nodes, edges, order)
    for e in edges:
        d_nt = cat.NODES[nodes[e["target"]]["type"]]
        port = d_nt.input(e["target_port"])
        assert port is not None  # noqa: S101 - checked above
        problem = edge_problem(resolved.get((e["source"], e["source_port"])), port)
        if problem:
            ctx.add(f"Cannot connect: {problem}. Add a conversion node in between.", edge=e["id"],
                    code="type_mismatch")
    if ctx.issues:
        raise FlowValidationError(ctx.issues)
    return {"schema": SCHEMA_VERSION, "name": str(name).strip(), "description": description,
            "nodes": [nodes[n] for n in nodes], "edges": edges, "variables": clean_vars,
            "viewport": {"x": float(viewport.get("x", 0)), "y": float(viewport.get("y", 0)),
                         "zoom": float(viewport.get("zoom", 1))}}


def check_edge(doc: dict, source: str, source_port: str, target: str, target_port: str) -> str | None:
    """Would adding this connection be valid? (used by the canvas and the AI)."""
    trial = copy.deepcopy(doc)
    trial["edges"] = list(trial.get("edges", [])) + [
        {"id": "zz_probe_edge", "source": source, "source_port": source_port, "target": target,
         "target_port": target_port}]
    try:
        validate_document(trial)
    except FlowValidationError as exc:
        return exc.issues[0]["message"]
    return None


def readiness(doc: dict, only: set[str] | None = None) -> list[dict[str, Any]]:
    """What stops nodes from running (a valid document assumed)."""
    issues: list[dict[str, Any]] = []
    connected = {(e["target"], e["target_port"]) for e in doc["edges"]}
    for node in doc["nodes"]:
        if only is not None and node["id"] not in only:
            continue
        if node.get("disabled"):
            continue
        nt = cat.NODES[node["type"]]
        name = node.get("label") or nt.label
        if not nt.available:
            issues.append({"node_id": node["id"], "code": "unavailable",
                           "message": f"{name} cannot run: {nt.unavailable_reason}"})
            continue
        fills = {f.fills: f for f in nt.fields if f.fills}
        for p in nt.inputs:
            if (node["id"], p.id) in connected:
                continue
            filler = fills.get(p.id)
            if filler is not None and str(node["config"].get(filler.id) or "").strip():
                continue
            needed = p.required or (filler is not None and (filler.required or nt.type in _NEEDS_TEXT))
            if needed:
                hint = f" or fill in '{filler.label}'" if filler is not None else ""
                issues.append({"node_id": node["id"], "code": "missing_input", "port": p.id,
                               "message": f"{name}: connect '{p.label}'{hint}"})
        for f in nt.fields:
            if f.fills or not f.required:
                continue
            value = node["config"].get(f.id, f.default)
            if value is None or (isinstance(value, str) and not value.strip()) or value == []:
                issues.append({"node_id": node["id"], "code": "missing_field", "field": f.id,
                               "message": f"{name}: '{f.label}' is required"})
    return issues


#: Nodes whose optional prompt input must then be filled from the field.
_NEEDS_TEXT = frozenset({"image.generate", "image.edit", "video.generate", "video.t2v", "voice.tts",
                         "voice.dialogue", "compose.captions", "compose.subtitles", "ai.script_writer"})
