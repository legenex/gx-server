"""Managed Wan 2.2 workflow generation: user LoRAs on the two expert branches.

A template declares its expert branches in ``_gx.lora_chains``::

    "lora_chains": {
      "high": {"after": "5", "into": ["7.model"]},
      "low":  {"after": "6", "into": ["8.model"]}
    }

``after`` is the node whose MODEL output feeds the chain (the base LightX2V
loader), ``into`` the inputs that consumed it (ModelSamplingSD3). For N user
LoRAs on a branch the generator inserts N ``LoraLoaderModelOnly`` nodes::

    UNETLoader(high) -> base LoRA 5 -> U1 -> U2 -> ... -> ModelSamplingSD3 7 -> KSampler 12
    UNETLoader(low)  -> base LoRA 6 -> V1 -> V2 -> ... -> ModelSamplingSD3 8 -> KSampler 13

The high branch never receives a low-noise file and vice versa, and the two
chains never share a node. Node ids are deterministic (``1000 + i`` for high,
``2000 + i`` for low) so two builds of the same request are byte-identical and
diffs are readable. Zero LoRAs leave the template graph untouched.

Every file name must come from the LoRA catalogue (lora_catalog.py) and pass
its checks; nothing here builds a path.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass

from .errors import ValidationError
from .lora_catalog import LoraCatalog

BRANCHES = ("high", "low")
NODE_BASE = {"high": 1000, "low": 2000}
MAX_PER_BRANCH = 8
MIN_STRENGTH = 0.0
MAX_STRENGTH = 1.5
LOADER_CLASS = "LoraLoaderModelOnly"
GENERATOR_VERSION = "gx-wan-lora/1"


class LoraRequestError(ValidationError):
    """A LoRA selection the router refuses; ``code`` says which rule."""

    def __init__(self, message: str, code: str, *, param: str = "loras") -> None:
        super().__init__(message, param=param)
        self.code = code


@dataclass(frozen=True)
class LoraUse:
    name: str
    strength: float
    shared: bool = False
    allow_unknown: bool = False


@dataclass(frozen=True)
class Chains:
    high: tuple[LoraUse, ...] = ()
    low: tuple[LoraUse, ...] = ()

    def branch(self, name: str) -> tuple[LoraUse, ...]:
        return self.high if name == "high" else self.low

    @property
    def empty(self) -> bool:
        return not self.high and not self.low

    def public(self) -> dict:
        return {b: [{"name": u.name, "strength": u.strength, **({"shared": True} if u.shared else {})}
                    for u in self.branch(b)] for b in BRANCHES}


def parse_chain_spec(meta: object, graph: dict) -> dict[str, tuple[str, tuple[tuple[str, str], ...]]]:
    """Validate a template's ``_gx.lora_chains`` block at load time."""
    if meta is None:
        return {}
    if not isinstance(meta, dict) or set(meta) != set(BRANCHES):
        raise ValueError("_gx.lora_chains must declare exactly 'high' and 'low'")
    out: dict[str, tuple[str, tuple[tuple[str, str], ...]]] = {}
    for branch in BRANCHES:
        spec = meta[branch]
        after = str(spec.get("after", "")) if isinstance(spec, dict) else ""
        if after not in graph:
            raise ValueError(f"_gx.lora_chains.{branch}.after {after!r} is not a node")
        into: list[tuple[str, str]] = []
        for target in spec.get("into") or []:
            node_id, _, field = str(target).partition(".")
            node = graph.get(node_id)
            if node is None or node.get("inputs", {}).get(field) != [after, 0]:
                raise ValueError(f"_gx.lora_chains.{branch}.into {target!r} does not consume node {after}")
            into.append((node_id, field))
        if not into:
            raise ValueError(f"_gx.lora_chains.{branch}.into is empty")
        out[branch] = (after, tuple(into))
    highs = {n for n, _ in out["high"][1]}
    lows = {n for n, _ in out["low"][1]}
    if highs & lows or out["high"][0] == out["low"][0]:
        raise ValueError("_gx.lora_chains: the high and low branches must not share nodes")
    return out


def _strength(value: object, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LoraRequestError(f"{where}: strength must be a number", "lora_invalid_strength")
    number = float(value)
    if not MIN_STRENGTH <= number <= MAX_STRENGTH:
        raise LoraRequestError(f"{where}: strength must be between {MIN_STRENGTH} and {MAX_STRENGTH}",
                               "lora_invalid_strength")
    return round(number, 4)


def parse_request(body: object) -> Chains:
    """``{"high": [{"name", "strength", "shared"?, "allow_unknown"?}], "low": [...]}``."""
    if body is None:
        return Chains()
    if not isinstance(body, dict) or not set(body) <= set(BRANCHES):
        raise LoraRequestError("loras must be an object with 'high' and/or 'low' lists", "lora_invalid_request")
    chains: dict[str, tuple[LoraUse, ...]] = {}
    for branch in BRANCHES:
        items = body.get(branch) or []
        if not isinstance(items, list):
            raise LoraRequestError(f"loras.{branch} must be a list", "lora_invalid_request")
        if len(items) > MAX_PER_BRANCH:
            raise LoraRequestError(f"at most {MAX_PER_BRANCH} LoRAs per expert branch", "lora_too_many")
        uses = []
        for i, item in enumerate(items):
            where = f"loras.{branch}[{i}]"
            if not isinstance(item, dict) or not set(item) <= {"name", "strength", "shared", "allow_unknown"}:
                raise LoraRequestError(f"{where} must be {{name, strength, shared?, allow_unknown?}}",
                                       "lora_invalid_request")
            name = item.get("name")
            if not isinstance(name, str) or not 1 <= len(name) <= 512 or "\x00" in name \
                    or not name.lower().endswith(".safetensors"):
                raise LoraRequestError(f"{where}: name must be a .safetensors name from the LoRA catalogue",
                                       "lora_unsupported_file")
            if name.startswith("/") or "\\" in name or any(p in ("", ".", "..") for p in name.split("/")):
                raise LoraRequestError(f"{where}: {name!r} is not a catalogue name", "lora_not_found")
            for flag in ("shared", "allow_unknown"):
                if flag in item and not isinstance(item[flag], bool):
                    raise LoraRequestError(f"{where}: {flag} must be true or false", "lora_invalid_request")
            uses.append(LoraUse(name, _strength(item.get("strength"), where), bool(item.get("shared")),
                                bool(item.get("allow_unknown"))))
        chains[branch] = tuple(uses)
    return Chains(**chains)


def validate(chains: Chains, catalog: LoraCatalog) -> None:
    """Every name exists, is usable, is compatible (or explicitly allowed) and
    sits on the branch its noise class allows."""
    if chains.empty:
        return
    if catalog.loader_available is False:
        raise LoraRequestError("ComfyUI does not offer the LoraLoaderModelOnly node, so LoRAs cannot be applied",
                               "lora_loader_unavailable")
    names = {b: [u.name for u in chains.branch(b)] for b in BRANCHES}
    for branch in BRANCHES:
        if len(set(names[branch])) != len(names[branch]):
            raise LoraRequestError(f"the {branch}-noise chain lists the same LoRA twice", "lora_duplicate")
    shared = set(names["high"]) & set(names["low"])
    for branch in BRANCHES:
        other = "low" if branch == "high" else "high"
        for use in chains.branch(branch):
            entry = catalog.get(use.name)
            if entry is None:
                raise LoraRequestError(f"LoRA file {use.name!r} is not in the LoRA catalogue (rescan?)",
                                       "lora_not_found")
            if not entry.valid:
                raise LoraRequestError(f"LoRA file {use.name!r} is not a valid safetensors file: {entry.error}",
                                       "lora_invalid_file")
            if entry.comfy_visible is False:
                raise LoraRequestError(f"ComfyUI does not list LoRA {use.name!r}; rescan once it is visible",
                                       "lora_not_visible")
            if entry.compatibility == "incompatible":
                raise LoraRequestError(f"LoRA {use.name!r} is not compatible with Wan 2.2 T2V-A14B: "
                                       f"{entry.compatibility_reason}", "lora_incompatible")
            if entry.compatibility != "compatible" and not use.allow_unknown:
                raise LoraRequestError(f"LoRA {use.name!r} has unknown compatibility "
                                       f"({entry.compatibility_reason}); allow it explicitly to use it",
                                       "lora_unknown_compatibility")
            if entry.noise == other:
                raise LoraRequestError(f"LoRA {use.name!r} is a {other}-noise file and cannot go on the "
                                       f"{branch}-noise branch", "lora_branch_mismatch")
            if use.name in shared:
                twin = next(u for u in chains.branch(other) if u.name == use.name)
                if not (use.shared and twin.shared):
                    raise LoraRequestError(f"LoRA {use.name!r} is on both branches; that must be an explicit "
                                           "'apply to both' choice", "lora_shared_not_allowed")
                if entry.noise in BRANCHES:
                    raise LoraRequestError(f"LoRA {use.name!r} is a {entry.noise}-noise file; it cannot be "
                                           "applied to both branches", "lora_branch_mismatch")


def insert(graph: dict, spec: dict[str, tuple[str, tuple[tuple[str, str], ...]]], chains: Chains) -> dict:
    """Return a new graph with the chains inserted (the input is not modified)."""
    out = copy.deepcopy(graph)
    if chains.empty:
        return out
    if not spec:
        raise LoraRequestError("this workflow does not support LoRAs", "lora_unsupported_workflow")
    for branch in BRANCHES:
        after, into = spec[branch]
        source: list = [after, 0]
        for i, use in enumerate(chains.branch(branch)):
            node_id = str(NODE_BASE[branch] + i)
            if node_id in out:
                raise LoraRequestError(f"workflow already has a node {node_id}", "workflow_invalid_graph")
            out[node_id] = {
                "class_type": LOADER_CLASS,
                "inputs": {"model": list(source), "lora_name": use.name, "strength_model": use.strength},
                "_meta": {"title": f"GX user LoRA {i + 1} ({branch} noise)"},
            }
            source = [node_id, 0]
        for node_id, field in into:
            out[node_id]["inputs"][field] = list(source)
    check_graph(out, spec)
    return out


def _upstream(graph: dict, start: list) -> list[str]:
    """Node ids along the MODEL input chain from ``start`` back to its loader."""
    seen: list[str] = []
    ref = start
    while isinstance(ref, list) and len(ref) == 2 and str(ref[0]) in graph and len(seen) < 64:
        node_id = str(ref[0])
        seen.append(node_id)
        ref = graph[node_id]["inputs"].get("model")
    return seen


def check_graph(graph: dict, spec: dict) -> None:
    """Structural invariants: every reference resolves, and each branch's model
    path contains no node of the other branch."""
    for node_id, node in graph.items():
        for field, value in node.get("inputs", {}).items():
            if isinstance(value, list) and len(value) == 2 and isinstance(value[1], int) \
                    and isinstance(value[0], str) and value[0] not in graph:
                raise LoraRequestError(f"node {node_id}.{field} references missing node {value[0]}",
                                       "workflow_invalid_graph")
    if not spec:
        return
    paths = {}
    for branch in BRANCHES:
        node_id, field = spec[branch][1][0]
        paths[branch] = set(_upstream(graph, graph[node_id]["inputs"][field]))
    if paths["high"] & paths["low"]:
        raise LoraRequestError("the high and low model paths share a node", "workflow_invalid_graph")
    for branch in BRANCHES:
        for node_id in paths[branch]:
            base = NODE_BASE["low" if branch == "high" else "high"]
            if node_id.isdigit() and base <= int(node_id) < base + 1000:
                raise LoraRequestError(f"{branch} model path contains a node of the other branch",
                                       "workflow_invalid_graph")


def version(template_name: str, template_graph: dict) -> str:
    """``gx-wan-lora/1+<template>@<sha12>``: changes whenever the template does."""
    digest = hashlib.sha256(json.dumps(template_graph, sort_keys=True).encode()).hexdigest()[:12]
    return f"{GENERATOR_VERSION}+{template_name}@{digest}"


def summary(graph: dict, spec: dict) -> dict:
    """High/low chains as applied (in order), read back from the built graph."""
    out: dict[str, list[dict]] = {}
    for branch in BRANCHES:
        if not spec:
            out[branch] = []
            continue
        node_id, field = spec[branch][1][0]
        chain = [n for n in reversed(_upstream(graph, graph[node_id]["inputs"][field]))
                 if graph[n]["class_type"] == LOADER_CLASS]
        out[branch] = [{"node": n, "lora_name": graph[n]["inputs"]["lora_name"],
                        "strength": graph[n]["inputs"]["strength_model"],
                        "base": not (n.isdigit() and int(n) >= 1000)} for n in chain]
        unet = next((graph[n]["inputs"].get("unet_name") for n in _upstream(graph, graph[node_id]["inputs"][field])
                     if graph[n]["class_type"] == "UNETLoader"), None)
        out[f"{branch}_model"] = unet  # type: ignore[assignment]
    return out
