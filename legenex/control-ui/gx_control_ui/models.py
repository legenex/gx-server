"""The eight public aliases: static facts plus live state.

Static facts are taken from the reviewed configuration files
(`legenex/gateway/litellm/config.yaml`, `llama-swap/node0{1,2}.yaml`,
`legenex/lifecycle/gx-max.conf`) and the measured values in TEST_RESULTS.md.
Live state is derived only from real probes -- a container existing is never
reported as "healthy".
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from .services import ALL_ALIASES, Cluster

#: Behaviour that is a property of the alias, not of the model bound to it.
ALIAS_ROLE: dict[str, dict[str, Any]] = {
    "gx-mini": {
        "purpose": "Fastest tier: chat, extraction, classification, simple vision, lightweight tools. Always hot.",
        "endpoint": "LiteLLM gx-mini -> gx-llama-swap-node01 -> 127.0.0.1:19001",
        "controls": ["load", "unload", "restart"],
    },
    "gx-fast": {
        "purpose": "Primary interactive coding / tools / agent tier (Kilo Code). Kept warm.",
        "endpoint": "LiteLLM gx-fast -> gx-llama-swap-node01 -> vLLM",
        "controls": ["load", "unload", "restart"],
    },
    "gx-reason": {
        "purpose": "Single-node deep reasoning: hard maths, architecture, difficult debugging.",
        "endpoint": "LiteLLM gx-reason -> 192.168.100.11:28080 (fabric) -> vLLM",
        "controls": ["load", "unload", "restart"],
    },
    "gx-max": {
        "purpose": "The largest model: explicit hardest work. Takes over BOTH nodes.",
        "endpoint": "LiteLLM gx-max -> gx-orchestrator :18900 -> SGLang :30000 (rank 0)",
        "controls": ["load", "unload", "restart", "force_release"],
        "topology": {"tp": 2, "nnodes": 2, "rank0": "gx10-01", "rank1": "gx10-02",
                     "dist_init_addr": "192.168.100.10:5000", "image": "lmsysorg/sglang:dev-v4f-2dgx-v2"},
    },
    "gx-auto": {
        "purpose": "Let the orchestrator pick mini / fast / reason per request (Kilo-aware). Uses gx-max only if it "
                   "is already running.",
        "endpoint": "LiteLLM gx-auto -> gx-orchestrator :18900",
        "controls": [],
    },
    "gx-image": {
        "purpose": "Uncensored text-to-image, instruction image editing and variations.",
        "endpoint": "LiteLLM /v1/images/generations and /v1/images/edits -> 192.168.100.11:18800 (fabric)",
        "controls": ["unload"],
    },
    "gx-video": {
        "purpose": "Uncensored text-to-video, image-to-video and video editing (asynchronous jobs).",
        "endpoint": "LiteLLM /v1/videos, /v1/videos/edits -> 192.168.100.11:18800 (fabric)",
        "controls": ["unload"],
    },
    "gx-music": {
        "purpose": "Music: songs with lyrics and vocals, instrumentals, style tags, remix, repaint and extend "
                   "(ACE-Step 1.5 XL, asynchronous jobs).",
        "endpoint": "GX-Playground / music API on gx10-01 -> 192.168.100.11:18820 (fabric) -> ACE-Step",
        "controls": ["load", "unload"],
        "task": "music-generation",
    },
}

REGISTRY_PATH = Path(__file__).resolve().parents[2] / "models" / "registry.json"


def catalog(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """Model-card facts: alias role + the bound model from legenex/models/registry.json."""
    try:
        reg = json.loads((path or REGISTRY_PATH).read_text(encoding="utf-8")).get("aliases", {})
    except (OSError, ValueError):
        reg = {}
    out: dict[str, dict[str, Any]] = {}
    for alias, role in ALIAS_ROLE.items():
        spec = reg.get(alias, {})
        node = spec.get("node") or ""
        components = spec.get("components") or []
        model = spec.get("repository")
        if not model and components:
            model = "; ".join(c.get("repository") or c.get("file") for c in components if c.get("kind") == "checkpoint")
        out[alias] = {
            **role,
            "model": model or spec.get("runtime") or "—",
            "repository": spec.get("repository"),
            "revision": spec.get("revision"),
            "engine": spec.get("runtime", "—"),
            "nodes": [n.strip() for n in node.split("+")] if node else [],
            "context": spec.get("context"),
            "max_output": spec.get("max_output"),
            "vision": bool(spec.get("vision")),
            "tools": bool(spec.get("tools")),
            "reasoning": spec.get("reasoning") or False,
            "startup": spec.get("startup", "—"),
            "resource": spec.get("memory", "—"),
            "measured": spec.get("measured", "—"),
            "parameters": spec.get("parameters"),
            "active_parameters": spec.get("active_parameters"),
            "quantization": spec.get("quantization"),
            "uncensored": spec.get("uncensored"),
            "licence": spec.get("licence"),
            "family": spec.get("family"),
            "path": spec.get("path"),
            "interim": bool(spec.get("interim")),
            "target": spec.get("target"),
            "previous": spec.get("previous"),
            "components": components,
            "task": spec.get("task") or role.get("task") or ("chat" if alias not in ("gx-image", "gx-video")
                                                              else "media-generation"),
            "capabilities": spec.get("capabilities"),
            "not_supported": spec.get("not_supported"),
            "image": spec.get("image"),
            "runtime_repository": spec.get("runtime_repository"),
            "runtime_revision": spec.get("runtime_revision"),
        }
    return out


#: Back-compatible name used by older callers and tests.
CATALOG = catalog()


class ResultLog:
    """Last health / inference / load result per alias, persisted outside Git."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        try:
            self._data: dict = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._data = {}

    def record(self, alias: str, kind: str, ok: bool, detail: str = "", **extra: Any) -> None:
        with self._lock:
            entry = {"ok": ok, "at": time.time(), "detail": detail[:300], **extra}
            self._data.setdefault(alias, {})[kind] = entry
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self._data, indent=1), encoding="utf-8")
                tmp.replace(self.path)
            except OSError:
                pass

    def get(self, alias: str) -> dict:
        with self._lock:
            return dict(self._data.get(alias, {}))


def _swap_entry(svc: dict, key: str, alias: str) -> tuple[str | None, bool]:
    probe = svc.get(key) or {}
    if not probe.get("ok"):
        return None, False
    for m in (probe.get("body") or {}).get("data", []) or []:
        if m.get("id") == alias:
            return str((m.get("status") or {}).get("value", "")).lower(), True
    return None, True


#: Outcomes that are the caller's problem, not the model's (D-039).
_CLIENT_OUTCOMES = {
    "ok", "context_length_exceeded", "client_disconnected", "invalid_request", "invalid_model",
    "BadRequestError", "ContextWindowExceededError", "HTTPException", "AuthenticationError",
    "UnprocessableEntityError", "NotFoundError", "PermissionDeniedError",
}
#: A server-side failure this recent marks a loaded alias as degraded.
DEGRADED_WINDOW_S = 900


def _is_server_failure(rec: dict | None) -> bool:
    if not rec or rec.get("outcome") in _CLIENT_OUTCOMES:
        return False
    try:
        status = int(rec.get("status") or 0)
    except (TypeError, ValueError):
        status = 0
    return not (400 <= status < 500)


def text_live(alias: str, svc: dict, now: float | None = None) -> dict:
    """Budget, routing and last-request facts for one text alias (D-039).

    Merges the orchestrator's view (gx-auto traffic and direct gx-max) with
    the gateway hook's view (direct gx-mini / gx-fast / gx-reason traffic),
    keeping whichever request is newest. No prompt text is involved.
    """
    now = now or time.time()
    probe = svc.get("text_status") or {}
    body = probe.get("body") if probe.get("ok") and isinstance(probe.get("body"), dict) else {}
    entry = dict((body.get("aliases") or {}).get(alias) or {})
    gw = svc.get("gateway_text") or {}
    gw_last = (gw.get("by_alias") or {}).get(alias)
    orch_last = entry.get("last_request")
    candidates = [r for r in (orch_last, gw_last) if isinstance(r, dict)]
    last = max(candidates, key=lambda r: float(r.get("ts") or 0)) if candidates else None
    entry["last_request"] = last
    entry["orchestrator_last"] = orch_last
    entry["gateway_last"] = gw_last
    entry["recent_gateway_failures"] = (gw.get("recent_failures") or {}).get(alias, 0)
    entry["available"] = bool(probe.get("ok"))
    age = now - float(last.get("ts") or 0) if last else None
    entry["last_request_age_s"] = round(age, 1) if age is not None else None
    entry["degraded"] = bool(last and _is_server_failure(last) and age is not None and age < DEGRADED_WINDOW_S)
    return entry


def _containers(facts: dict) -> dict[str, dict]:
    return {c["name"]: c for c in ((facts or {}).get("docker") or {}).get("containers", [])}


def live_state(cluster: Cluster, results: ResultLog) -> list[dict]:
    svc = cluster.services.get() or {}
    n1 = cluster.node1.get() or {}
    n2 = cluster.node2.get() or {}
    lc = cluster.lifecycle.get() or {}
    lc_status = ((lc.get("status") or {}).get("body")) or {}
    gxmax_state = lc_status.get("state", "unknown")
    orch = svc.get("orchestrator") or {}
    tiers = ((orch.get("body") or {}).get("tiers") or {}) if orch.get("ok") else {}
    c1, c2 = _containers(n1), _containers(n2)
    media = svc.get("media") or {}
    raw_media = media.get("body")
    media_body: dict = raw_media if media.get("ok") and isinstance(raw_media, dict) else {}
    out = []

    cards = catalog()
    for alias in ALL_ALIASES:
        info = dict(cards[alias])
        state, detail = "unavailable", ""
        extra: dict[str, Any] = {}

        if alias in ("gx-mini", "gx-fast", "gx-reason"):
            node = "node1" if alias != "gx-reason" else "node2"
            raw, reachable = _swap_entry(svc, f"swap_{node}", alias)
            if gxmax_state in ("ready", "acquiring", "releasing"):
                state, detail = "unavailable", f"drained: gx-max is {gxmax_state}"
            elif not reachable:
                state, detail = "unavailable", f"{node} llama-swap unreachable"
            elif raw in ("loaded", "ready"):
                state, detail = "loaded", raw
            elif raw in ("loading", "starting"):
                state, detail = "loading", raw
            elif raw in ("unloaded", "stopped"):
                state, detail = "unloaded", "starts on the next request"
            elif raw is None:
                state, detail = "unavailable", "not configured in llama-swap"
            else:
                state, detail = "error", raw
            tier = tiers.get(alias) or {}
            extra["orchestrator_view"] = tier
            extra["container"] = (c1 if node == "node1" else c2).get(alias)
            extra["admission"] = None
        elif alias == "gx-max":
            mapping = {"down": "unloaded", "acquiring": "loading", "ready": "loaded",
                       "releasing": "unloading"}
            state = mapping.get(gxmax_state, "unavailable")
            detail = lc_status.get("last_error") or lc_status.get("detail") or ""
            tier = tiers.get("gx-max") or {}
            if gxmax_state == "down" and tier.get("state") == "unavailable":
                state, detail = "unavailable", tier.get("reason", "")
            sglang = svc.get("sglang") or {}
            extra.update({
                "lifecycle": lc_status,
                "orchestrator_view": tier,
                "rank0": c1.get("gx-max-rank0"),
                "rank1": c2.get("gx-max-rank1"),
                "rank0_watcher": n1.get("gxmax_watcher"),
                "rank1_deadman": n2.get("gxmax_watcher"),
                "node1_lock": n1.get("guard_lock"),
                "node2_lock": n2.get("guard_lock"),
                "sglang_health": {"ok": sglang.get("ok"), "status": sglang.get("status"),
                                  "checked_at": sglang.get("checked_at")},
                "sglang_models": ((svc.get("sglang_models") or {}).get("body") or {}).get("data"),
                "sglang_info": (svc.get("sglang_info") or {}).get("body"),
                "ledger": cluster.guard.get(),
                "swap": {
                    "node1": _swap_summary(n1), "node2": _swap_summary(n2),
                },
                "rdma": {"node1": n1.get("rdma"), "node2": n2.get("rdma")},
            })
            if gxmax_state == "ready" and not sglang.get("ok"):
                state, detail = "error", "orchestrator READY but SGLang /health failing"
        elif alias == "gx-auto":
            if orch.get("ok"):
                usable = [t for t, v in tiers.items() if v.get("usable") and t != "gx-max"]
                state = "loaded" if usable else "unavailable"
                detail = f"routable now: {', '.join(sorted(usable)) or 'none'}"
            else:
                state, detail = "unavailable", "orchestrator unreachable"
            extra["orchestrator_view"] = tiers
        elif alias == "gx-music":
            music = svc.get("music") or {}
            raw_music = music.get("body")
            mbody: dict = raw_music if isinstance(raw_music, dict) else {}
            engine = mbody.get("engine")
            if gxmax_state in ("ready", "acquiring", "releasing"):
                state, detail = "unavailable", f"engine held off: gx-max is {gxmax_state}"
            elif not music.get("ok"):
                state, detail = "unavailable", "music supervisor on gx10-02 unreachable"
            elif engine == "ready":
                state, detail = "loaded", "ACE-Step loaded"
            elif engine in ("loading", "unloading"):
                state, detail = "loading" if engine == "loading" else "unloading", engine
            elif engine == "failed":
                state, detail = "error", "the last engine load failed"
            else:
                state, detail = "ready", "on demand (loads with the next job, ~90 s)"
            extra["music"] = {"supervisor": mbody, "container": c2.get("gx-music")}
        else:  # media
            comfy = (media_body or {}).get("comfyui") or {}
            if gxmax_state in ("ready", "acquiring", "releasing"):
                state, detail = "unavailable", f"drained: gx-max is {gxmax_state}"
            elif not media.get("ok"):
                state, detail = "unavailable", "media router unreachable"
            elif not comfy.get("reachable"):
                state, detail = "error", "ComfyUI not reachable from the router"
            elif media_body.get("busy"):
                state, detail = "loading", f"generating (held {media_body.get('held_for_seconds')} s)"
            else:
                state, detail = "ready", "on demand"
            extra["media"] = media_body
            extra["containers"] = {"router": c2.get("gx-media-router"), "comfyui": c2.get("gx-comfyui")}

        if alias in ("gx-mini", "gx-fast", "gx-reason", "gx-max", "gx-auto"):
            text = text_live(alias, svc)
            extra["text"] = text
            if text["degraded"] and state in ("loaded", "ready"):
                # Never show a healthy badge while requests are failing.
                last = text["last_request"] or {}
                state = "degraded"
                detail = (f"last request failed {int(text['last_request_age_s'] or 0)} s ago "
                          f"({last.get('outcome')}: {str(last.get('error') or '')[:120]})")

        info.update({
            "alias": alias,
            "state": state,
            "state_detail": detail,
            "results": results.get(alias),
            "live": extra,
        })
        out.append(info)
    return out


def _swap_summary(facts: dict) -> dict | None:
    mem = (facts or {}).get("memory") or {}
    if not mem.get("SwapTotal"):
        return None
    return {"total": mem["SwapTotal"], "used": mem["SwapTotal"] - (mem.get("SwapFree") or 0),
            "mem_available": mem.get("MemAvailable")}
