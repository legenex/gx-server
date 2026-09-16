"""The seven public aliases: static facts plus live state.

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

CATALOG: dict[str, dict[str, Any]] = {
    "gx-mini": {
        "purpose": "Fast, cheap everyday chat, extraction, classification and vision. Always hot.",
        "model": "Qwen3.5-4B (Q4_K_M GGUF) + BF16 mmproj",
        "engine": "llama.cpp (llama-swap managed)",
        "nodes": ["gx10-01"],
        "context": 65536, "max_output": 4096,
        "vision": True, "tools": True, "reasoning": False,
        "startup": "Resident (ttl 0). Loaded at all times; cold start ~10 s if it was unloaded.",
        "resource": "~10 GiB on gx10-01 (resident group, 14 GiB cap).",
        "endpoint": "LiteLLM gx-mini -> gx-llama-swap-node01 -> 127.0.0.1:19001",
        "controls": ["load", "unload", "restart"],
        "measured": "~48 tok/s; vision verified (shapes, digits).",
    },
    "gx-fast": {
        "purpose": "Agentic / tool-using workhorse with vision and long answers.",
        "model": "nvidia/Qwen3.6-35B-A3B-NVFP4",
        "engine": "vLLM (jstarkg/vllm-gb10-flashnext:0.28-sm121-r6, llama-swap managed)",
        "nodes": ["gx10-01"],
        "context": 65536, "max_output": 8192,
        "vision": True, "tools": True, "reasoning": False,
        "startup": "On demand. First request cold-starts vLLM (a few minutes); unloads after 30 min idle.",
        "resource": "~66% GPU pool on gx10-01 (heavy group, one at a time).",
        "endpoint": "LiteLLM gx-fast -> gx-llama-swap-node01 -> vLLM",
        "controls": ["load", "unload", "restart"],
        "measured": "Correct arithmetic; tool call parsed (qwen3_xml).",
    },
    "gx-reason": {
        "purpose": "Hard reasoning, maths and careful coding. Emits reasoning_content.",
        "model": "nvidia/Qwen3.6-27B-NVFP4 (dense 27B)",
        "engine": "vLLM (llama-swap managed on gx10-02, reached over the RoCE fabric)",
        "nodes": ["gx10-02"],
        "context": 65536, "max_output": 16384,
        "vision": True, "tools": True, "reasoning": True,
        "startup": "On demand. Cold start measured 401 s; unloads after 15 min idle.",
        "resource": "~44 GiB on gx10-02 (MemAvailable 114 -> 70 GiB).",
        "endpoint": "LiteLLM gx-reason -> 192.168.100.11:28080 (fabric) -> vLLM",
        "controls": ["load", "unload", "restart"],
        "measured": "12.4 tok/s; bat-and-ball and multi-step problems correct.",
    },
    "gx-max": {
        "purpose": "The largest model: long, difficult, high-stakes work. Takes over BOTH nodes.",
        "model": "nvidia/DeepSeek-V4-Flash-0731-NVFP4",
        "engine": "SGLang (lmsysorg/sglang:dev-v4f-2dgx-v2), TP=2, nnodes=2",
        "nodes": ["gx10-01 (rank 0)", "gx10-02 (rank 1)"],
        "context": 327680, "max_output": 16384,
        "vision": False, "tools": True, "reasoning": True,
        "startup": "Sanctioned orchestrator lifecycle only: drain -> admission -> rank1 -> rank0 -> "
                   "health. Cold load ~508-559 s. Released after 30 min idle.",
        "resource": "Exclusive two-node takeover. Normal models on both nodes are drained first. "
                    "Load transient: node 1 MemAvailable ~2.5-3.3 GiB and swap up to ~64 GiB for a few "
                    "seconds; node 2 swap ~51-55 GiB. Steady: ~15-18 GiB free per node.",
        "endpoint": "LiteLLM gx-max -> gx-orchestrator :18900 -> SGLang :30000 (rank 0)",
        "controls": ["load", "unload", "restart", "force_release"],
        "measured": "~41-45 tok/s long generation; traffic on both ConnectX rails.",
        "topology": {"tp": 2, "nnodes": 2, "rank0": "gx10-01", "rank1": "gx10-02",
                     "dist_init_addr": "192.168.100.10:5000", "image": "lmsysorg/sglang:dev-v4f-2dgx-v2"},
    },
    "gx-auto": {
        "purpose": "Let the orchestrator pick mini / fast / reason per request. Never acquires gx-max.",
        "model": "Routing alias (gx-orchestrator classifier)",
        "engine": "gx-orchestrator -> LiteLLM",
        "nodes": ["gx10-01 (router)"],
        "context": 24576, "max_output": 8192,
        "vision": True, "tools": True, "reasoning": False,
        "startup": "Always available while the orchestrator runs. Uses gx-max only if it is already READY.",
        "resource": "None of its own; the chosen tier's footprint applies.",
        "endpoint": "LiteLLM gx-auto -> gx-orchestrator :18900",
        "controls": [],
        "measured": "Routes to mini and reason correctly; gx-max-worthy prompts are downgraded.",
    },
    "gx-image": {
        "purpose": "Text-to-image generation.",
        "model": "Qwen-Image-2512 fp8 (+ 4-step Lightning LoRA by default)",
        "engine": "ComfyUI behind gx-media-router (gx10-02)",
        "nodes": ["gx10-02"],
        "context": None, "max_output": None,
        "vision": False, "tools": False, "reasoning": False,
        "startup": "On demand. The first generation loads weights; one generation at a time.",
        "resource": "Up to ~60 GiB on gx10-02 while warm; unloadable.",
        "endpoint": "LiteLLM /v1/images/generations -> 192.168.100.11:18800 (fabric)",
        "controls": ["unload"],
        "measured": "1024x1024 in ~26 s (Lightning); 1328x1328 ~13 s warm.",
    },
    "gx-video": {
        "purpose": "Text-to-video generation (short clips).",
        "model": "Wan 2.2 T2V-A14B fp8 (two experts + 4-step LoRAs)",
        "engine": "ComfyUI behind gx-media-router (gx10-02), asynchronous jobs",
        "nodes": ["gx10-02"],
        "context": None, "max_output": None,
        "vision": False, "tools": False, "reasoning": False,
        "startup": "On demand, asynchronous: submit -> poll -> fetch MP4.",
        "resource": "Up to ~80 GiB on gx10-02 while warm; unloadable.",
        "endpoint": "gx-media-router POST /v1/videos (fabric)",
        "controls": ["unload"],
        "measured": "33-49 frames @16 fps, 640x640, ~48-57 s.",
    },
}


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

    for alias in ALL_ALIASES:
        info = dict(CATALOG[alias])
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
