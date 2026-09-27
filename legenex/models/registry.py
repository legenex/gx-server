"""Loader + validator for the GX Cluster V4.1 registry (schema 2).

The registry (``registry.json`` next to this module) is the single source of
truth for nodes, runtimes, model packs, public aliases, serving profiles and
reasoning levels (docs/ARCHITECTURE-V41.md section 2). Dashboard,
orchestrator and gateway must read from it, so this module gives every
consumer one dependency-free way to load it and check it.

Pure standard library, and NO I/O at import time: importing this module never
touches the filesystem, so it is safe inside any hook or service.

Usage::

    from registry import RegistryError, load
    try:
        reg = load("/path/to/registry.json")
    except RegistryError as exc:
        ...  # str(exc) lists every problem found, one per line

``validate(data)`` returns the list of error strings (empty == valid) for
callers that prefer not to trap exceptions; ``loads(text)`` parses and
validates a JSON string.

Rules implemented (schema 2 contract):

* ``schema`` must be exactly 2.
* ``cluster`` names the head and worker, both of which must be nodes.
* Every node carries role/user/LAN + Tailscale IPs, both fabric rail IPs,
  HCAs and an SSH target. All IPs are dotted-quad IPv4.
* Every runtime is a pinned Mia-style kit: 40-hex commit, image, loopback
  OpenAI ``api``, served model id, start/stop/status commands.
* Every model is a pinned Hugging Face pack (``org/name`` source, 40-hex
  revision, absolute paths, quant, vision/tools flags, positive window).
  ``serving_notes`` values must be sane numbers for their key.
* Every alias references an existing model and runtime, and its mode is
  ``direct`` or ``auto``.
* The six profiles (fast/balanced/swarm/deep/long/custom) exist;
  ``max_num_seqs`` is 1..4 (custom may be a [lo, hi] pair), ``max_model_len``
  is within the documented bounds, ``reasoning_default`` is a real level.
* The reasoning mapping covers every level exactly once, and
  ``numeric_range`` is an ascending integer pair.
"""

from __future__ import annotations

import ipaddress
import json
import re
from typing import Any

SCHEMA_VERSION = 2
#: Profile names the contract requires (section 2).
PROFILE_NAMES = ("fast", "balanced", "swarm", "deep", "long", "custom")
#: Alias modes the orchestrator understands.
ALIAS_MODES = ("direct", "auto")
#: Serving knobs the Mia/vLLM overlay reads, with their validators.
_SERVING_NOTE_RULES = {
    "gpu_mem_util": ("float01",),
    "kv_bytes": ("int_pos",),
    "max_num_batched_tokens": ("int_pos",),
    "vllm_sparse_indexer_max_logits_mb": ("int_pos",),
}
#: Documented max_model_len bounds (section 2 profiles + custom range).
_MAX_MODEL_LEN_MIN, _MAX_MODEL_LEN_MAX = 8192, 600000
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_SOURCE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class RegistryError(ValueError):
    """Raised by load()/loads() when the registry violates the schema-2 contract.

    ``errors`` holds every problem found (str(exc.) joins them, one per line),
    so one bad field does not hide the rest.
    """

    def __init__(self, errors: list[str]) -> None:
        self.errors = list(errors)
        super().__init__("invalid registry:\n  " + "\n  ".join(self.errors))


# ---------------------------------------------------------------------------
# Small typed checks — each returns an error string or None
# ---------------------------------------------------------------------------


def _is_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_ipv4(where: str, value: Any) -> str | None:
    if not _is_str(value):
        return f"{where}: expected an IPv4 address string, got {value!r}"
    try:
        ipaddress.IPv4Address(value.strip())
    except ipaddress.AddressValueError:
        return f"{where}: {value!r} is not a valid IPv4 address"
    return None


def _check_hex40(where: str, value: Any) -> str | None:
    if not _is_str(value) or not _HEX40.match(value):
        return f"{where}: expected a 40-character lowercase hex git revision, got {value!r}"
    return None


def _check_path(where: str, value: Any) -> str | None:
    if not _is_str(value) or not value.startswith("/"):
        return f"{where}: expected an absolute path, got {value!r}"
    return None


def _check_url(where: str, value: Any) -> str | None:
    if not _is_str(value) or not value.startswith(("http://", "https://")):
        return f"{where}: expected an http(s) URL, got {value!r}"
    return None


def _check_serving_note(key: str, value: Any) -> str | None:
    where = f"serving_notes.{key}"
    rule = _SERVING_NOTE_RULES.get(key)
    if rule is None:
        return None  # unknown future knob: presence is enough
    kind = rule[0]
    if kind == "int_pos":
        if not _is_int(value) or value <= 0:
            return f"{where}: expected a positive integer, got {value!r}"
    elif kind == "float01":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.0 < float(value) <= 1.0:
            return f"{where}: expected a float in (0, 1], got {value!r}"
    return None


# ---------------------------------------------------------------------------
# Section validators
# ---------------------------------------------------------------------------


def _validate_cluster(data: dict, errors: list[str], nodes: list[str]) -> None:
    cluster = data.get("cluster")
    if not isinstance(cluster, dict):
        errors.append("cluster: expected an object")
        return
    for key in ("name", "head", "worker"):
        if not _is_str(cluster.get(key)):
            errors.append(f"cluster.{key}: expected a non-empty string, got {cluster.get(key)!r}")
    head, worker = cluster.get("head"), cluster.get("worker")
    if _is_str(head) and head not in nodes:
        errors.append(f"cluster.head: {head!r} is not a node in 'nodes'")
    if _is_str(worker) and worker not in nodes:
        errors.append(f"cluster.worker: {worker!r} is not a node in 'nodes'")


def _validate_nodes(data: dict, errors: list[str]) -> list[str]:
    nodes = data.get("nodes")
    if not isinstance(nodes, dict) or not nodes:
        errors.append("nodes: expected a non-empty object")
        return []
    for name, node in nodes.items():
        where = f"nodes[{name!r}]"
        if not isinstance(node, dict):
            errors.append(f"{where}: expected an object")
            continue
        if node.get("role") not in ("head", "worker"):
            errors.append(f"{where}.role: expected 'head' or 'worker', got {node.get('role')!r}")
        if not _is_str(node.get("user")):
            errors.append(f"{where}.user: expected a non-empty string, got {node.get('user')!r}")
        for key in ("lan_ip", "tailscale_ip"):
            err = _check_ipv4(f"{where}.{key}", node.get(key))
            if err:
                errors.append(err)
        fabric = node.get("fabric")
        if not isinstance(fabric, dict):
            errors.append(f"{where}.fabric: expected an object with rail1/rail2")
        else:
            for key in ("rail1", "rail2"):
                err = _check_ipv4(f"{where}.fabric.{key}", fabric.get(key))
                if err:
                    errors.append(err)
        hcas = node.get("hcas")
        if not isinstance(hcas, list) or not hcas or not all(_is_str(h) for h in hcas):
            errors.append(f"{where}.hcas: expected a non-empty list of interface names, got {hcas!r}")
        if not _is_str(node.get("ssh")):
            errors.append(f"{where}.ssh: expected a non-empty SSH host, got {node.get('ssh')!r}")
    return list(nodes)


def _validate_runtimes(data: dict, errors: list[str]) -> list[str]:
    runtimes = data.get("runtimes")
    if not isinstance(runtimes, dict) or not runtimes:
        errors.append("runtimes: expected a non-empty object")
        return []
    for name, rt in runtimes.items():
        where = f"runtimes[{name!r}]"
        if not isinstance(rt, dict):
            errors.append(f"{where}: expected an object")
            continue
        for key in ("kind", "submodule", "image", "served_model_id", "start", "stop", "status"):
            if not _is_str(rt.get(key)):
                errors.append(f"{where}.{key}: expected a non-empty string, got {rt.get(key)!r}")
        err = _check_hex40(f"{where}.commit", rt.get("commit"))
        if err:
            errors.append(err)
        err = _check_url(f"{where}.api", rt.get("api"))
        if err:
            errors.append(err)
    return list(runtimes)


def _validate_models(data: dict, errors: list[str]) -> list[str]:
    models = data.get("models")
    if not isinstance(models, dict) or not models:
        errors.append("models: expected a non-empty object")
        return []
    for name, model in models.items():
        where = f"models[{name!r}]"
        if not isinstance(model, dict):
            errors.append(f"{where}: expected an object")
            continue
        source = model.get("source")
        if not _is_str(source) or not _SOURCE.match(source):
            errors.append(f"{where}.source: expected an 'org/name' repository id, got {source!r}")
        err = _check_hex40(f"{where}.revision", model.get("revision"))
        if err:
            errors.append(err)
        for key in ("path", "engram_dir"):
            err = _check_path(f"{where}.{key}", model.get(key))
            if err:
                errors.append(err)
        if not isinstance(model.get("uncensored"), bool):
            errors.append(f"{where}.uncensored: expected a boolean, got {model.get('uncensored')!r}")
        for key in ("quant",):
            if not _is_str(model.get(key)):
                errors.append(f"{where}.{key}: expected a non-empty string, got {model.get(key)!r}")
        for key in ("vision", "tools"):
            if not isinstance(model.get(key), bool):
                errors.append(f"{where}.{key}: expected a boolean, got {model.get(key)!r}")
        ctx = model.get("max_context")
        if not _is_int(ctx) or ctx <= 0:
            errors.append(f"{where}.max_context: expected a positive integer, got {ctx!r}")
        notes = model.get("serving_notes")
        if notes is not None:
            if not isinstance(notes, dict):
                errors.append(f"{where}.serving_notes: expected an object")
            else:
                for key, value in notes.items():
                    err = _check_serving_note(key, value)
                    if err:
                        errors.append(f"{where}.{err}")
    return list(models)


def _validate_aliases(data: dict, errors: list[str], models: list[str], runtimes: list[str]) -> None:
    aliases = data.get("aliases")
    if not isinstance(aliases, dict) or not aliases:
        errors.append("aliases: expected a non-empty object")
        return
    for name, alias in aliases.items():
        where = f"aliases[{name!r}]"
        if not isinstance(alias, dict):
            errors.append(f"{where}: expected an object")
            continue
        if alias.get("model") not in models:
            errors.append(f"{where}.model: references unknown model {alias.get('model')!r}")
        if alias.get("runtime") not in runtimes:
            errors.append(f"{where}.runtime: references unknown runtime {alias.get('runtime')!r}")
        if alias.get("mode") not in ALIAS_MODES:
            errors.append(f"{where}.mode: expected one of {ALIAS_MODES}, got {alias.get('mode')!r}")
        if not _is_str(alias.get("description")):
            errors.append(f"{where}.description: expected a non-empty string, got {alias.get('description')!r}")


def _validate_profiles(data: dict, errors: list[str], levels: list[str]) -> None:
    profiles = data.get("profiles")
    if not isinstance(profiles, dict):
        errors.append("profiles: expected an object")
        return
    for name in PROFILE_NAMES:
        if name not in profiles:
            errors.append(f"profiles[{name!r}]: required profile missing")
    for name, profile in profiles.items():
        where = f"profiles[{name!r}]"
        if not isinstance(profile, dict):
            errors.append(f"{where}: expected an object")
            continue
        bounded = bool(profile.get("bounded"))
        # max_num_seqs: int in 1..4, or for bounded profiles a [lo, hi] pair.
        seqs = profile.get("max_num_seqs")
        if bounded:
            ok = (
                isinstance(seqs, list) and len(seqs) == 2
                and all(_is_int(v) and 1 <= v <= 4 for v in seqs)
                and seqs[0] <= seqs[1]
            )
            if not ok:
                errors.append(f"{where}.max_num_seqs: expected a [lo, hi] pair within 1..4, got {seqs!r}")
        elif not _is_int(seqs) or not 1 <= seqs <= 4:
            errors.append(f"{where}.max_num_seqs: expected an integer in 1..4, got {seqs!r}")
        # max_model_len: documented bounds (custom may be a [lo, hi] pair).
        length = profile.get("max_model_len")
        if bounded:
            ok = (
                isinstance(length, list) and len(length) == 2
                and all(_is_int(v) and _MAX_MODEL_LEN_MIN <= v <= _MAX_MODEL_LEN_MAX for v in length)
                and length[0] <= length[1]
            )
            if not ok:
                errors.append(
                    f"{where}.max_model_len: expected a [lo, hi] pair within "
                    f"{_MAX_MODEL_LEN_MIN}..{_MAX_MODEL_LEN_MAX}, got {length!r}"
                )
        elif not _is_int(length) or not _MAX_MODEL_LEN_MIN <= length <= _MAX_MODEL_LEN_MAX:
            errors.append(
                f"{where}.max_model_len: expected an integer in "
                f"{_MAX_MODEL_LEN_MIN}..{_MAX_MODEL_LEN_MAX}, got {length!r}"
            )
        # spec_method is optional (custom omits it; long omits dspark_tokens
        # and inherits the shipped default k=3) per the section-2 contract.
        method = profile.get("spec_method")
        if method is not None and method not in ("dspark", "none"):
            errors.append(f"{where}.spec_method: expected 'dspark' or 'none', got {method!r}")
        tokens = profile.get("dspark_tokens")
        if tokens is not None and (not _is_int(tokens) or tokens <= 0):
            errors.append(f"{where}.dspark_tokens: expected a positive integer, got {tokens!r}")
        default = profile.get("reasoning_default")
        if default not in levels:
            errors.append(f"{where}.reasoning_default: {default!r} is not a reasoning level")
        if not _is_str(profile.get("target")):
            errors.append(f"{where}.target: expected a non-empty string, got {profile.get('target')!r}")


def _validate_reasoning(data: dict, errors: list[str]) -> list[str]:
    reasoning = data.get("reasoning")
    if not isinstance(reasoning, dict):
        errors.append("reasoning: expected an object")
        return []
    levels = reasoning.get("levels")
    if not isinstance(levels, list) or not levels or not all(_is_str(l) for l in levels):
        errors.append("reasoning.levels: expected a non-empty list of level names")
        levels = []
    if len(set(levels)) != len(levels):
        errors.append("reasoning.levels: contains duplicate level names")
    mapping = reasoning.get("mapping")
    if not isinstance(mapping, dict):
        errors.append("reasoning.mapping: expected an object")
    else:
        if set(mapping) != set(levels):
            missing = sorted(set(levels) - set(mapping))
            extra = sorted(set(mapping) - set(levels))
            if missing:
                errors.append(f"reasoning.mapping: missing entries for levels {missing}")
            if extra:
                errors.append(f"reasoning.mapping: has entries for unknown levels {extra}")
        for level, params in mapping.items():
            if not isinstance(params, dict) or not params:
                errors.append(f"reasoning.mapping[{level!r}]: expected a non-empty object of chat_template_kwargs")
    rng = reasoning.get("numeric_range")
    if (
        not isinstance(rng, list) or len(rng) != 2
        or not all(_is_int(v) for v in rng) or rng[0] >= rng[1]
    ):
        errors.append(f"reasoning.numeric_range: expected an ascending [lo, hi] integer pair, got {rng!r}")
    return list(levels)


def _validate_capabilities(data: dict, errors: list[str]) -> None:
    caps = data.get("capabilities")
    if not isinstance(caps, dict):
        errors.append("capabilities: expected an object")
        return
    for key in ("vision", "tools", "structured_output", "reasoning"):
        if not isinstance(caps.get(key), bool):
            errors.append(f"capabilities.{key}: expected a boolean, got {caps.get(key)!r}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate(data: Any) -> list[str]:
    """Return every schema-2 violation in ``data`` (empty list == valid).

    Collects instead of failing fast so one report shows the whole picture.
    Unknown top-level keys (including ``_``-prefixed provenance comments)
    are allowed: the contract defines a minimum, not a maximum.
    """
    errors: list[str] = []
    if not isinstance(data, dict):
        return [f"registry: expected a JSON object at the top level, got {type(data).__name__}"]
    if data.get("schema") != SCHEMA_VERSION:
        errors.append(f"schema: expected {SCHEMA_VERSION}, got {data.get('schema')!r}")
    nodes = _validate_nodes(data, errors)
    _validate_cluster(data, errors, nodes)
    runtimes = _validate_runtimes(data, errors)
    models = _validate_models(data, errors)
    _validate_aliases(data, errors, models, runtimes)
    levels = _validate_reasoning(data, errors)
    _validate_profiles(data, errors, levels)
    _validate_capabilities(data, errors)
    return errors


def loads(text: str) -> dict:
    """Parse and validate a registry JSON string; raise RegistryError if invalid."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RegistryError([f"registry: not valid JSON ({exc})"]) from exc
    errors = validate(data)
    if errors:
        raise RegistryError(errors)
    return data


def load(path: str) -> dict:
    """Read, parse and validate the registry at ``path``; raise RegistryError if invalid."""
    try:
        with open(path, encoding="utf-8") as fh:
            return loads(fh.read())
    except OSError as exc:
        raise RegistryError([f"registry: cannot read {path}: {exc}"]) from exc
