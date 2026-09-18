"""The eleven public aliases: static facts plus live state.

`legenex/models/registry.json` is the single source of truth for WHICH aliases
exist (L-10 as amended by D-036 and D-040) and for the model bound to each one;
`ALIAS_ROLE` below only adds what is a property of the alias rather than of the
model. The rest of the static facts come from the reviewed configuration files
(`legenex/gateway/litellm/config.yaml`, `llama-swap/node0{1,2}.yaml`,
`legenex/lifecycle/gx-max.conf`) and the measured values in TEST_RESULTS.md.
Live state is derived only from real probes -- a container existing is never
reported as "healthy".

Two live states, not one: for the node-2 services (gx-music, gx-voice, gx-call,
gx-live) the supervisor is a resident process and the engine is weights it
loads on demand. An idle engine is READY, never offline.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from .node2_services import SPECS as NODE2_SPECS
from .services import Cluster
from .util import HTTPError, TTLCache, http_json

#: What a card is. Media and audio/realtime aliases are services with an
#: on-demand engine; the LLM aliases are llama-swap / lifecycle managed.
LLM, MEDIA, AUDIO = "llm", "media", "audio-realtime"

#: Behaviour that is a property of the alias, not of the model bound to it.
ALIAS_ROLE: dict[str, dict[str, Any]] = {
    "gx-mini": {
        "purpose": "Fastest tier: chat, extraction, classification, simple vision, lightweight tools. Always hot.",
        "endpoint": "LiteLLM gx-mini -> gx-llama-swap-node01 -> 127.0.0.1:19001",
        "controls": ["load", "unload", "restart"],
        "kind": LLM,
    },
    "gx-fast": {
        "purpose": "Primary interactive coding / tools / agent tier (Kilo Code). Kept warm.",
        "endpoint": "LiteLLM gx-fast -> gx-llama-swap-node01 -> vLLM",
        "controls": ["load", "unload", "restart"],
        "kind": LLM,
    },
    "gx-reason": {
        "purpose": "Single-node deep reasoning: hard maths, architecture, difficult debugging.",
        "endpoint": "LiteLLM gx-reason -> 192.168.100.11:28080 (fabric) -> vLLM",
        "controls": ["load", "unload", "restart"],
        "kind": LLM,
    },
    "gx-max": {
        "purpose": "The largest model: explicit hardest work. Takes over BOTH nodes.",
        "endpoint": "LiteLLM gx-max -> gx-orchestrator :18900 -> SGLang :30000 (rank 0)",
        "controls": ["load", "unload", "restart", "force_release"],
        "kind": LLM,
        "topology": {"tp": 2, "nnodes": 2, "rank0": "gx10-01", "rank1": "gx10-02",
                     "dist_init_addr": "192.168.100.10:5000", "image": "lmsysorg/sglang:dev-v4f-2dgx-v2"},
    },
    "gx-auto": {
        "purpose": "Let the orchestrator pick mini / fast / reason per request (Kilo-aware). Uses gx-max only if it "
                   "is already running.",
        "endpoint": "LiteLLM gx-auto -> gx-orchestrator :18900",
        "controls": [],
        "kind": LLM,
    },
    "gx-image": {
        "purpose": "Uncensored text-to-image, instruction image editing and variations.",
        "endpoint": "LiteLLM /v1/images/generations and /v1/images/edits -> 192.168.100.11:18800 (fabric)",
        "controls": ["unload"],
        "kind": MEDIA,
        "playground": "#/images",
    },
    "gx-video": {
        "purpose": "Uncensored text-to-video, image-to-video and video editing (asynchronous jobs).",
        "endpoint": "LiteLLM /v1/videos, /v1/videos/edits -> 192.168.100.11:18800 (fabric)",
        "controls": ["unload"],
        "kind": MEDIA,
        "playground": "#/video",
    },
    "gx-music": {
        "purpose": "Music: songs with lyrics and vocals, instrumentals, style tags, remix, repaint and extend "
                   "(ACE-Step 1.5 XL, asynchronous jobs).",
        "endpoint": "GX-Playground / music API on gx10-01 -> 192.168.100.11:18820 (fabric) -> ACE-Step",
        "controls": ["load", "unload"],
        "task": "music-generation",
        "kind": MEDIA,
        "playground": "#/music",
    },
    "gx-voice": {
        "purpose": "Text to speech: preset and saved voices, voice design, authorized reference cloning "
                   "(Qwen3-TTS, asynchronous jobs).",
        "endpoint": "GX-Playground /v1/voice/* on gx10-01 and the gateway's POST /v1/audio/speech "
                    "-> 192.168.100.11:18830 (fabric) -> Qwen3-TTS",
        "controls": ["load", "unload"],
        "kind": AUDIO,
        "playground": "#/voice",
    },
    "gx-call": {
        "purpose": "Realtime voice agents: speech to speech with turn taking, barge-in, tool calls, transcripts "
                   "and recordings.",
        "endpoint": "GX-Playground Call Agents over the WebSocket tunnel -> 192.168.100.11:18840 (fabric) "
                    "-> VoiceChat",
        "controls": ["load", "unload"],
        "kind": AUDIO,
        "playground": "#/call",
    },
    "gx-live": {
        "purpose": "Realtime multimodal conversation: speech with barge-in, live camera vision and tools.",
        "endpoint": "GX-Playground Live over the WebSocket tunnel -> 192.168.100.11:18850 (fabric) -> MiniCPM-o",
        "controls": ["load", "unload"],
        "kind": AUDIO,
        "playground": "#/live",
    },
}
#: Default task per kind when the registry entry does not name one.
_DEFAULT_TASK = {LLM: "chat", MEDIA: "media-generation", AUDIO: "audio-generation"}

REGISTRY_PATH = Path(__file__).resolve().parents[2] / "models" / "registry.json"


def catalog(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """Model-card facts: the registry entry + the alias role.

    The registry is the list: every alias it binds gets a card, in its order,
    so a new alias never has to be added twice. An alias that has a role but no
    registry entry (a half-finished binding) still gets a card, last, rather
    than disappearing from the page.
    """
    try:
        reg = json.loads((path or REGISTRY_PATH).read_text(encoding="utf-8")).get("aliases", {})
    except (OSError, ValueError):
        reg = {}
    if not isinstance(reg, dict):
        reg = {}
    out: dict[str, dict[str, Any]] = {}
    for alias in list(reg) + [a for a in ALIAS_ROLE if a not in reg]:
        role = ALIAS_ROLE.get(alias, {})
        spec = reg.get(alias) or {}
        kind = role.get("kind") or (AUDIO if str(spec.get("task", "")).startswith("realtime") else MEDIA)
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
            "task": spec.get("task") or role.get("task") or _DEFAULT_TASK[kind],
            "capabilities": spec.get("capabilities"),
            "not_supported": spec.get("not_supported"),
            "image": spec.get("image"),
            "runtime_repository": spec.get("runtime_repository"),
            "runtime_revision": spec.get("runtime_revision"),
            #: What the card is, so the page can show the right fields (D-040).
            "kind": kind,
            #: Which GX-Playground page serves this alias, as a hash route.
            "playground": role.get("playground"),
            #: Node-2 supervisor facts (port / unit / container / health URL).
            "service": spec.get("service") or _service_facts(alias),
            #: Other weights this alias's engine needs before it can serve.
            "depends_on": spec.get("depends_on") or [],
            "measured_footprint": spec.get("measured_footprint"),
            "notes": spec.get("notes"),
        }
    return out


def _service_facts(alias: str) -> dict[str, Any] | None:
    """Supervisor facts for an alias whose registry entry omits them.

    Ports and container names come from `node2_services.SPECS`, which is what
    Resource Control actually talks to; nothing is hard-coded twice.
    """
    spec = NODE2_SPECS.get(alias)
    if spec is None:
        return None
    return {"port": spec.port, "unit": f"{alias}.service", "container": spec.container, "label": spec.label}


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
    raw = probe.get("body")
    body: dict = raw if probe.get("ok") and isinstance(raw, dict) else {}
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


def _tenant_bases(cfg: Any) -> dict[str, str]:
    """alias -> supervisor base URL for the node-2 services.

    gx-music predates the Build V3 service specs (D-036), so it is named here;
    the rest come from `node2_services.SPECS`. Addresses are the configured
    fabric ones, never Tailscale (L-3).
    """
    bases = {"gx-music": cfg.music_base}
    for alias, spec in NODE2_SPECS.items():
        bases[alias] = getattr(cfg, f"{spec.prefix}_base", None) or f"http://192.168.100.11:{spec.port}"
    return bases


_tenant_lock = threading.Lock()
_tenant_caches: dict[str, TTLCache] = {}


def _probe_tenant(base: str) -> dict:
    """GET <supervisor>/health, shaped like a `services.py` probe."""
    now = time.time()
    try:
        status, body = http_json("GET", f"{base.rstrip('/')}/health", timeout=3)
        return {"ok": 200 <= status < 300, "status": status, "body": body, "checked_at": now}
    except HTTPError as exc:
        return {"ok": False, "status": 0, "error": exc.message, "checked_at": now}


def _tenant_cache(base: str, ttl: float) -> TTLCache:
    """One stale-while-revalidate cache per supervisor, so a dark node-2
    service never stalls the Models page (the same rule as `Cluster`)."""
    with _tenant_lock:
        cache = _tenant_caches.get(base)
        if cache is None:
            cache = TTLCache(lambda: _probe_tenant(base), ttl, first_wait=4)
            _tenant_caches[base] = cache
        return cache


def tenant_probes(cluster: Cluster, svc: dict) -> dict[str, dict]:
    """One probe-shaped /health per node-2 supervisor.

    The cached service snapshot already carries gx-music (and anything a later
    snapshot publishes as `tenant_<alias>`); the rest are read here behind the
    same short TTL. Offline runs never touch the network.
    """
    out: dict[str, dict] = {}
    for alias, base in _tenant_bases(cluster.cfg).items():
        cached = svc.get(f"tenant_{alias}") or (svc.get("music") if alias == "gx-music" else None)
        if isinstance(cached, dict):
            out[alias] = cached
        elif getattr(cluster.cfg, "offline", False):
            out[alias] = {"ok": False, "status": 0, "error": "offline"}
        else:
            probe = _tenant_cache(base, cluster.cfg.service_ttl).get()
            out[alias] = probe if isinstance(probe, dict) else {"ok": False, "status": 0, "error": "no probe yet"}
    return out


#: Supervisor engine state -> (card state, engine state, detail). READY is the
#: supervisor; the engine is LOADED only while the weights are resident.
_ENGINE_STATES: dict[str, tuple[str, str, str]] = {
    "ready": ("loaded", "LOADED", "weights resident"),
    "busy": ("loaded", "BUSY", "working"),
    "generating": ("loaded", "BUSY", "generating"),
    "running": ("loaded", "BUSY", "working"),
    "loading": ("loading", "LOADING", "loading the weights"),
    "waiting": ("loading", "QUEUED", "waiting for memory on gx10-02"),
    "queued": ("loading", "QUEUED", "queued"),
    "unloading": ("unloading", "BUSY", "unloading the engine"),
    "unloaded": ("ready", "UNLOADED", ""),
    "stopped": ("ready", "UNLOADED", ""),
    "": ("ready", "UNLOADED", ""),
    "failed": ("error", "ERROR", "the last engine load failed"),
    "error": ("error", "ERROR", "the supervisor reports an error"),
}


def tenant_state(alias: str, probe: dict, *, gxmax_state: str, startup: str = "") -> dict:
    """Card state for one on-demand node-2 service (pure).

    An idle engine is READY, not offline: the supervisor is up and the weights
    load with the next request. Only an unreachable supervisor, a failed load
    or a gx-max takeover is a problem the user has to act on.
    """
    raw_body = probe.get("body")
    body: dict = raw_body if isinstance(raw_body, dict) else {}
    mem = body.get("memory") if isinstance(body.get("memory"), dict) else {}
    engine_raw = str(body.get("state") or body.get("engine") or "").lower()
    out = {"health": body, "engine_raw": engine_raw, "memory": mem,
           "active_sessions": body.get("active_sessions"), "active_jobs": body.get("active_jobs"),
           "version": body.get("version"), "checked_at": probe.get("checked_at")}
    if gxmax_state in ("ready", "acquiring", "releasing"):
        return {**out, "state": "unavailable", "detail": f"engine held off: gx-max is {gxmax_state}",
                "service_state": "BLOCKED", "engine_state": "BLOCKED"}
    if not probe.get("ok"):
        return {**out, "state": "unavailable", "service_state": "ERROR", "engine_state": "ERROR",
                "detail": f"the {alias} supervisor on gx10-02 is not reachable"}
    state, engine_state, detail = _ENGINE_STATES.get(engine_raw, ("error", "ERROR", f"unknown state {engine_raw!r}"))
    if engine_state == "UNLOADED":
        detail = f"on demand — the engine loads with the next request{f' ({startup})' if startup else ''}"
    return {**out, "state": state, "detail": detail, "service_state": "READY", "engine_state": engine_state}


def _cold_hint(info: dict) -> str:
    """"about 104 s cold" from the measured footprint, or "" when nothing was measured."""
    fp = info.get("measured_footprint") or {}
    seconds = fp.get("startup_s")
    return f"about {round(float(seconds))} s cold" if isinstance(seconds, (int, float)) else ""


#: Card state -> the two-part state when a branch does not set one itself.
_ENGINE_FROM_STATE = {"loaded": "LOADED", "degraded": "LOADED", "loading": "LOADING", "unloading": "BUSY",
                      "unloaded": "UNLOADED", "ready": "READY", "error": "ERROR", "unavailable": "ERROR"}


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

    cards = catalog()
    tenants = tenant_probes(cluster, svc)
    out = []
    for alias, card in cards.items():
        info = dict(card)
        state, detail = "unavailable", ""
        service_state: str | None = None
        engine_state: str | None = None
        extra: dict[str, Any] = {}

        if alias in ("gx-mini", "gx-fast", "gx-reason"):
            node = "node1" if alias != "gx-reason" else "node2"
            raw, reachable = _swap_entry(svc, f"swap_{node}", alias)
            if gxmax_state in ("ready", "acquiring", "releasing"):
                state, detail = "unavailable", f"drained: gx-max is {gxmax_state}"
                service_state = engine_state = "BLOCKED"
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
        elif alias in tenants:
            # gx-music, gx-voice, gx-call, gx-live: a resident supervisor with
            # an on-demand engine. Idle is READY, never offline.
            ts = tenant_state(alias, tenants[alias], gxmax_state=gxmax_state, startup=_cold_hint(info))
            state, detail = ts["state"], ts["detail"]
            service_state, engine_state = ts["service_state"], ts["engine_state"]
            container = ((info.get("service") or {}).get("container")) or alias
            extra["supervisor"] = {"health": ts["health"], "container": c2.get(container),
                                   "engine_state": engine_state, "service_state": service_state,
                                   "memory": ts["memory"], "version": ts["version"],
                                   "active_sessions": ts["active_sessions"], "active_jobs": ts["active_jobs"],
                                   "checked_at": ts["checked_at"]}
            if alias == "gx-music":  # the shape the music job view has always read
                extra["music"] = {"supervisor": ts["health"], "container": c2.get("gx-music")}
        elif alias in ("gx-image", "gx-video"):
            comfy = (media_body or {}).get("comfyui") or {}
            if gxmax_state in ("ready", "acquiring", "releasing"):
                state, detail = "unavailable", f"drained: gx-max is {gxmax_state}"
                service_state = engine_state = "BLOCKED"
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
        else:
            # A registry binding with no probe wired up yet: say so, never guess.
            state, detail = "unavailable", "no live probe for this alias yet"

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
            # Service and weights are two different questions (D-040): the
            # supervisor can be READY while the engine is UNLOADED.
            "service_state": service_state or ("ERROR" if state in ("unavailable", "error") else "READY"),
            "engine_state": engine_state or _ENGINE_FROM_STATE.get(state, "ERROR"),
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
