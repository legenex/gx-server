"""Creative-facing model catalogue for GX-Playground > Models (Build V3, PLT).

Read-only. Facts come from ``legenex/models/registry.json`` (repository,
revision, runtime, quantization, context, capabilities, measured memory,
startup) and live state from Resource Control. Nothing here reveals a path
under ``secrets/``, a key or an internal URL; admin actions are links to the
Control Center.

Other workstreams may add a section (for example the Wan LoRA library) with
``app.catalog_extras[name] = fn`` where ``fn(app) -> dict`` returns small,
non-secret, JSON-safe data (plt.md section 8).
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .footprints import measured_footprint

log = logging.getLogger("gx.ui.catalog")

#: The eleven public aliases (L-10 amended by D-040), in display order.
PUBLIC_ALIASES = ("gx-mini", "gx-fast", "gx-reason", "gx-max", "gx-auto", "gx-image", "gx-video", "gx-music",
                  "gx-voice", "gx-call", "gx-live")
GROUP = {"gx-mini": "text", "gx-fast": "text", "gx-reason": "text", "gx-max": "text", "gx-auto": "text",
         "gx-image": "create", "gx-video": "create", "gx-music": "create", "gx-voice": "create",
         "gx-call": "realtime", "gx-live": "realtime"}
PURPOSE = {
    "gx-mini": "Fast chat, extraction and simple vision.",
    "gx-fast": "Interactive coding, tools and agents.",
    "gx-reason": "Deep single-node reasoning.",
    "gx-max": "The largest model; takes over both nodes on request.",
    "gx-auto": "Picks mini, fast or reason per request.",
    "gx-image": "Text-to-image, instruction edits, masks and variations.",
    "gx-video": "Text-to-video, image-to-video and video edits, with Wan 2.2 LoRAs.",
    "gx-music": "Songs, instrumentals, remix, repaint and extend.",
    "gx-voice": "Text-to-speech, voice design and voice cloning.",
    "gx-call": "Realtime voice agents for calls.",
    "gx-live": "Realtime camera and microphone conversations.",
}
ENDPOINT = {
    "gx-mini": "Gateway /v1/chat/completions", "gx-fast": "Gateway /v1/chat/completions",
    "gx-reason": "Gateway /v1/chat/completions", "gx-max": "Gateway /v1/chat/completions",
    "gx-auto": "Gateway /v1/chat/completions", "gx-image": "Gateway /v1/images/*",
    "gx-video": "Gateway /v1/videos", "gx-music": "Playground /v1/music/*",
    "gx-voice": "Gateway /v1/audio/speech and Playground /v1/voice/*",
    "gx-call": "Playground /v1/call/* and wss /rt/call/<session>",
    "gx-live": "Playground /v1/live/* and wss /rt/live/<session>",
}
_FIELDS = ("repository", "revision", "runtime", "family", "quantization", "context", "max_output", "parameters",
           "active_parameters", "licence", "startup", "memory", "measured", "task", "image", "runtime_repository",
           "runtime_revision", "interim")
_COMPONENT_FIELDS = ("role", "kind", "repository", "revision", "licence", "base_match", "name", "label",
                     "default", "status")
_STATE_WORD = {"READY": "Ready", "UNLOADED": "Ready (loads on demand)", "LOADING": "Loading",
               "GENERATING": "Working", "WAITING": "Waiting", "DRAINING": "Unloading", "BLOCKED": "Paused",
               "ERROR": "Unavailable"}


def _file_name(path: Any) -> str | None:
    """Only the file name of a model component (never a host path)."""
    if not isinstance(path, str) or not path:
        return None
    return path.replace("\\", "/").rsplit("/", 1)[-1][:160]


def _capabilities(alias: str, spec: dict) -> list[str]:
    caps = spec.get("capabilities")
    if isinstance(caps, list):
        return [str(c)[:80] for c in caps][:40]
    out = []
    for flag, label in (("vision", "vision"), ("tools", "tool calling"), ("reasoning", "reasoning")):
        if spec.get(flag):
            out.append(label)
    return out


def _components(spec: dict) -> list[dict]:
    out = []
    for c in (spec.get("components") or [])[:40]:
        if not isinstance(c, dict):
            continue
        item = {k: c.get(k) for k in _COMPONENT_FIELDS if c.get(k) not in (None, "")}
        name = _file_name(c.get("file"))
        if name:
            item["file"] = name
        out.append(item)
    return out


def _variants(spec: dict) -> list[dict]:
    raw = spec.get("variants")
    items = raw.values() if isinstance(raw, dict) else raw if isinstance(raw, list) else []
    out = []
    for v in list(items)[:20]:
        if not isinstance(v, dict):
            continue
        item = {k: v.get(k) for k in ("id", "label", "family", "repository", "revision", "licence", "capabilities",
                                      "workflows", "default", "status", "measured", "base", "resolution")
                if v.get(k) not in (None, "")}
        out.append(item)
    return out


def build_catalog(app: Any) -> dict:
    registry = app.manager.registry() if hasattr(app, "manager") else {}
    aliases = registry.get("aliases") or {}
    try:
        snap = app.resources.snapshot()
    except Exception as exc:  # noqa: BLE001 - the catalogue still shows the static facts
        log.warning("catalogue: resource snapshot failed: %s", exc)
        snap = {"runtimes": {}}
    runtimes = snap.get("runtimes") or {}
    control = app.cfg.public_control_url.rstrip("/")
    models = []
    for alias in PUBLIC_ALIASES:
        spec = aliases.get(alias)
        if not isinstance(spec, dict):
            spec = {}
        rt = runtimes.get(alias) or {}
        state = rt.get("state")
        item: dict[str, Any] = {
            "alias": alias,
            "group": GROUP[alias],
            "purpose": PURPOSE[alias],
            "endpoint": ENDPOINT[alias],
            "node": spec.get("node") or rt.get("node"),
            "registered": bool(spec),
            **{k: spec.get(k) for k in _FIELDS if spec.get(k) not in (None, "")},
            "capabilities": _capabilities(alias, spec),
            "not_supported": [str(x)[:80] for x in (spec.get("not_supported") or [])][:20],
            "components": _components(spec),
            "variants": _variants(spec),
            "state": state,
            "state_label": _STATE_WORD.get(state or "", "Not installed yet" if not spec else "Unknown"),
            "state_detail": rt.get("detail"),
            "queue": rt.get("queue"),
            "footprint": measured_footprint(alias, spec),
            "admin_url": f"{control}/#/models",
            "resources_url": f"{control}/#/resources",
        }
        models.append(item)
    extras: dict[str, Any] = {}
    for name, fn in sorted((getattr(app, "catalog_extras", None) or {}).items()):
        try:
            extras[name] = fn(app)
        except Exception as exc:  # noqa: BLE001
            log.warning("catalogue extra %s failed: %s", name, exc)
            extras[name] = {"error": "not available right now"}
    workflows = sorted((registry.get("workflow_models") or {}).items())
    return {"generated_at": time.time(), "models": models, "workflows": [{"workflow": w, "alias": a}
                                                                         for w, a in workflows],
            "extras": extras, "profile": (snap.get("profile") or {}).get("label"),
            "control_center_url": control}
