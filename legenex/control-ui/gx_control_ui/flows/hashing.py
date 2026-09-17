"""Stable, canonical cache keys for node executions.

key = sha256(canonical JSON of {node type, node type version, engine version,
config without presentation-only fields, the model/backend identity, and a
fingerprint of every input value}).

Canonical JSON: sorted keys, no whitespace, UTF-8, floats that are whole
numbers written as integers (1.0 == 1), NaN/Infinity refused. Media inputs are
fingerprinted by asset id AND content hash, so a replaced file never matches.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

ENGINE_VERSION = 1
#: Settings that do not change what a node produces.
PRESENTATION_FIELDS = frozenset({"title", "favourite"})


def _normalise(value: Any) -> Any:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite number in a cache key")
        return int(value) if value.is_integer() else value
    if isinstance(value, dict):
        return {str(k): _normalise(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalise(v) for v in value]
    raise TypeError(f"cannot hash {type(value).__name__}")


def canonical(value: Any) -> str:
    return json.dumps(_normalise(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def value_fingerprint(value: dict[str, Any], asset_sha: dict[str, str | None]) -> dict[str, Any]:
    """What identifies a port value for caching."""
    kind = value.get("type")
    if kind == "text":
        return {"t": "text", "h": hashlib.sha256(str(value.get("text", "")).encode("utf-8")).hexdigest()}
    if kind == "json":
        return {"t": "json", "h": digest(value.get("data"))}
    if kind in ("image", "video", "audio"):
        aid = str(value.get("asset_id"))
        return {"t": kind, "id": aid, "sha": asset_sha.get(aid)}
    if kind == "voice":
        return {"t": "voice", "id": value.get("voice_id"), "rev": value.get("revision")}
    if kind == "lora":
        return {"t": "lora", "id": value.get("preset_id"), "rev": value.get("revision"),
                "scale": value.get("scale")}
    return {"t": str(kind), "h": digest(value)}


def node_key(node_type: str, type_version: int, config: dict[str, Any], identity: dict[str, Any],
             inputs: dict[str, list[dict[str, Any]]], asset_sha: dict[str, str | None],
             variables: dict[str, str] | None = None) -> str:
    material = {
        "engine": ENGINE_VERSION,
        "type": node_type,
        "version": type_version,
        "config": {k: v for k, v in sorted(config.items()) if k not in PRESENTATION_FIELDS},
        "identity": identity,
        "inputs": {port: [value_fingerprint(v, asset_sha) for v in values] for port, values in sorted(inputs.items())},
        "variables": variables or {},
    }
    return digest(material)
