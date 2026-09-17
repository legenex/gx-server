"""Resource Control (D-037): profiles, live resource map, admission
explanations, compatibility, pins and Maintenance mode.

This module does not replace any admission component. It EXPLAINS and
COORDINATES the ones that already enforce memory safety:

* gx_orchestrator.resource_guard (30 GiB reserve; gx-music, gx-safe-run),
* the node-2 media router's cold/warm memory admission (60/76/110/8 GiB),
* the gx-max cluster-takeover policy (>= 100 GiB free on both nodes),
* llama-swap's own lifecycle for gx-mini / gx-fast / gx-reason.

Every manual control maps to an existing sanctioned operation (ActionRunner,
the router's free path, the music supervisor's load/unload). Profiles are
priority and preemption preferences, persisted as files in each node's guard
directory so the node-2 components (music supervisor, media router) can read
them without calling back to gx10-01:

    state/guard/profile.json            {"profile": "auto", ...}   both nodes
    state/guard/pins.json               {"gx-music": {...}, ...}   per node
    state/guard/node{1,2}.maintenance-hold                          Maintenance
    state/guard/node2.gxmax-hold        written by gx-max-start.sh (read here)

Numbers shown to the user are the ones the enforcing component uses, taken
from measurements (TEST_RESULTS.md, the Stage A music evidence), never from
container RSS, which does not include the unified-memory CUDA pool (B-021).
"""

from __future__ import annotations

import json
import logging
import shlex
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from collections.abc import Callable

from .util import HTTPError, http, run, ssh_args

log = logging.getLogger("gx.ui.resources")

GIB = 2**30
RESERVE_GIB = 30.0
#: Memory a node reports as available when nothing heavy is loaded (measured
#: 2026-09-17: node 1 ~113-115 GiB with the control plane, node 2 ~113 GiB).
IDLE_CAPACITY_GIB = {"node1": 113.0, "node2": 113.0}

PROFILES: dict[str, dict[str, Any]] = {
    "auto": {"label": "Auto", "summary": "Default. The scheduler keeps text tiers resident, loads media and music "
             "on demand, frees idle work when something else needs the memory, and queues what cannot run yet.",
             "priority": [], "user_selectable": True},
    "text": {"label": "Text / Agent", "summary": "gx-mini, gx-fast and gx-reason come first. Image, video and "
             "music stay available but wait instead of unloading gx-reason.",
             "priority": ["gx-mini", "gx-fast", "gx-reason"], "user_selectable": True},
    "media": {"label": "Media", "summary": "gx-image and gx-video come first. An idle gx-reason or music engine "
              "on gx10-02 is unloaded when a media job needs the memory. gx10-01 is not touched.",
              "priority": ["gx-image", "gx-video"], "user_selectable": True},
    "music": {"label": "Music", "summary": "gx-music comes first. Idle ComfyUI weights are handed over through "
              "the media router, and an idle gx-reason is unloaded only if music still does not fit. Video "
              "waits while music is working.",
              "priority": ["gx-music"], "user_selectable": True},
    "max": {"label": "Max", "summary": "Prepares the whole cluster for gx-max through its existing takeover "
            "lifecycle: drain both nodes, verify memory, start rank 1, then rank 0. Everything else waits.",
            "priority": ["gx-max"], "user_selectable": True},
    "maintenance": {"label": "Maintenance", "summary": "No new heavy jobs start. Running jobs finish, idle "
                    "on-demand models are unloaded, and queued jobs wait. SSH, Tailscale, LiteLLM, the Control "
                    "Center, watchdogs and Git sync keep running. Use it for storage cleanup and model installs.",
                    "priority": [], "user_selectable": False},
}
PROFILE_IDS = tuple(PROFILES)
STATES = ("READY", "UNLOADED", "LOADING", "GENERATING", "WAITING", "DRAINING", "BLOCKED", "ERROR")


@dataclass(frozen=True)
class RuntimePolicy:
    alias: str
    node: str                 # node1 | node2 | both
    kind: str                 # text | image | video | music | cluster | router
    engine: str
    cold_gib: float           # what the ENFORCING component requires before a load
    footprint_gib: float      # measured node-level footprint while resident
    idle_ttl_s: int | None    # None = resident by policy
    residency: str            # resident | on-demand | takeover | routing
    priority: int             # higher wins in AUTO
    preemptible: bool         # may be unloaded to admit another tenant when idle
    queue_allowed: bool
    exclusive: str
    admission: str            # which component enforces it
    controls: tuple[str, ...]
    measured: str
    cold_start_s: str = ""
    variants: dict[str, float] = field(default_factory=dict)

    def public(self) -> dict:
        d = asdict(self)
        d["controls"] = list(self.controls)
        return d


POLICIES: dict[str, RuntimePolicy] = {
    "gx-mini": RuntimePolicy(
        "gx-mini", "node1", "text", "llama.cpp via llama-swap", 10.0, 10.0, None, "resident", 90, False, True,
        "coexists with gx-fast on gx10-01; drained only by gx-max", "resource guard (30 GiB reserve)",
        ("load", "unload"), "about 10 GiB (registry, 2026-09-17)", "seconds"),
    "gx-fast": RuntimePolicy(
        "gx-fast", "node1", "text", "vLLM via llama-swap", 25.0, 45.0, None, "resident", 80, False, True,
        "coexists with gx-mini on gx10-01; drained only by gx-max", "resource guard (30 GiB reserve)",
        ("load", "unload"), "41-49 GiB; mini + fast leave 46-48 GiB available (2026-09-17)", "about 4 minutes"),
    "gx-reason": RuntimePolicy(
        "gx-reason", "node2", "text", "vLLM via llama-swap (gx10-02)", 45.0, 44.0, 900, "on-demand", 60, True, True,
        "cannot share gx10-02 with a cold video job; fits with image or music", "resource guard (30 GiB reserve)",
        ("load", "unload", "drain", "pin", "unpin"), "114 -> 70 GiB available when loaded (2026-09-16)",
        "about 6-7 minutes"),
    "gx-image": RuntimePolicy(
        "gx-image", "node2", "image", "ComfyUI behind gx-media-router", 60.0, 57.0, 600, "on-demand", 50, True, True,
        "one ComfyUI generation at a time; image and video weights never stack", "media router (cold 60 / warm 8 GiB)",
        ("unload", "pin", "unpin"), "114 -> 57.5 GiB available during a cold generation (2026-09-17)",
        "weights load with the first job", {"edit": 60.0}),
    "gx-video": RuntimePolicy(
        "gx-video", "node2", "video", "ComfyUI behind gx-media-router", 76.0, 72.0, 600, "on-demand", 40, True, True,
        "needs most of gx10-02; waits while gx-reason is loaded", "media router (cold 76 / keyframe edit 110 / warm 8 GiB)",
        ("unload", "pin", "unpin"), "114 -> 42 GiB available for t2v/i2v; 7 GiB for keyframe edit (2026-09-17)",
        "weights load with the first job", {"keyframe_edit": 110.0}),
    "gx-music": RuntimePolicy(
        "gx-music", "node2", "music", "ACE-Step 1.5 XL (gx-music supervisor)", 32.0, 28.0, 600, "on-demand", 50, True,
        True, "coexists with gx-reason (measured); hands idle ComfyUI weights over through the router",
        "resource guard (32 GiB estimate + 30 GiB reserve)", ("load", "unload", "drain", "pin", "unpin"),
        "24-28 GiB loaded; minimum 37.7 GiB available next to gx-reason (Stage A)", "82-92 s"),
    "gx-max": RuntimePolicy(
        "gx-max", "both", "cluster", "SGLang TP=2 across both nodes", 100.0, 105.0, None, "takeover", 100, False,
        True, "takes over BOTH nodes; everything else is drained first", "gx-max takeover policy (>= 100 GiB per node)",
        ("load", "unload"), "load transient ~117 GiB/node absorbed by swap; steady 105 GiB/node", "about 9 minutes"),
    "gx-auto": RuntimePolicy(
        "gx-auto", "node1", "router", "gx-orchestrator classifier", 0.0, 0.0, None, "routing", 0, False, True,
        "routes to mini/fast/reason; never acquires gx-max", "none (routing only)", (),
        "no memory of its own"),
}
GENERATIVE = ("gx-mini", "gx-fast", "gx-reason", "gx-image", "gx-video", "gx-music", "gx-max")
NODE2_TENANTS = ("gx-reason", "gx-image", "gx-video", "gx-music")
PIN_ALIASES = tuple(a for a, p in POLICIES.items() if "pin" in p.controls)


class ResourceError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------- pure logic
def enforced_need(alias: str, *, resident: bool, variant: str | None = None) -> float:
    """Memory the enforcing component wants to see available before it starts."""
    p = POLICIES[alias]
    if alias in ("gx-image", "gx-video"):
        if resident:
            return 8.0
        return p.variants.get(variant or "", p.cold_gib)
    if alias == "gx-max":
        return p.cold_gib
    return p.cold_gib + RESERVE_GIB


def admission_view(alias: str, avail_gib: float | None, residents: dict[str, dict], *,
                   variant: str | None = None, pins: set[str] | None = None,
                   holds: dict[str, bool] | None = None) -> dict:
    """Would `alias` be admitted now, and if not, what would make room?

    `residents` maps alias -> {"active": bool} for tenants currently holding
    memory on the same node. Pure: unit-tested, no I/O.
    """
    p = POLICIES[alias]
    pins = pins or set()
    holds = holds or {}
    out: dict[str, Any] = {"alias": alias, "node": p.node, "enforced_by": p.admission,
                           "reserve_gib": RESERVE_GIB if alias not in ("gx-image", "gx-video", "gx-max") else None}
    if holds.get("gxmax") and alias != "gx-max":
        return {**out, "allowed": False, "code": "gx_max_active", "reason": "gx-max owns the cluster; this "
                "starts again after gx-max is released", "blocking": ["gx-max"], "actions": []}
    if holds.get("maintenance"):
        return {**out, "allowed": False, "code": "maintenance", "reason": "Maintenance mode is on; new heavy "
                "work starts again when Maintenance ends", "blocking": [], "actions": []}
    resident = alias in residents
    if resident and alias not in ("gx-image", "gx-video"):
        return {**out, "allowed": True, "code": "resident", "reason": f"{alias} is already loaded",
                "need_gib": 0.0, "available_gib": avail_gib, "blocking": [], "actions": []}
    need = enforced_need(alias, resident=resident, variant=variant)
    out.update(need_gib=round(need, 1), available_gib=None if avail_gib is None else round(avail_gib, 1))
    if avail_gib is None:
        return {**out, "allowed": False, "code": "unknown", "reason": f"{p.node} memory is not readable right now",
                "blocking": [], "actions": []}
    others = {a: r for a, r in residents.items() if a != alias and a in POLICIES}
    if alias == "gx-reason" and "gx-video" in others:
        pass  # the reserve check below decides; video + reason is normally short
    if avail_gib >= need:
        return {**out, "allowed": True, "code": "fits",
                "reason": f"{avail_gib:.0f} GiB available, {need:.0f} GiB needed", "blocking": [], "actions": []}
    short = need - avail_gib
    # Smallest set of idle, unpinned, preemptible tenants that closes the gap.
    candidates = sorted(
        ((a, POLICIES[a].footprint_gib) for a, r in others.items()
         if POLICIES[a].preemptible and not r.get("active") and a not in pins),
        key=lambda x: -x[1])
    plan, freed = [], 0.0
    for a, gib in candidates:
        if freed >= short:
            break
        # image and video share one ComfyUI: freeing one frees the other too
        plan.append(a)
        freed += gib
    blocking = sorted(others, key=lambda a: -POLICIES[a].footprint_gib)
    held = ", ".join(f"{a} (~{POLICIES[a].footprint_gib:.0f} GiB"
                     f"{', busy' if others[a].get('active') else ''}{', pinned' if a in pins else ''})"
                     for a in blocking)
    reason = (f"{alias} needs about {need:.0f} GiB available on {_node_name(p.node)}; "
              f"{avail_gib:.0f} GiB is available now" + (f". Holding memory: {held}" if held else ""))
    actions = []
    if plan and freed >= short:
        actions.append({"id": "unload_and_continue", "unload": plan,
                        "label": f"Unload {' and '.join(plan)} and continue"})
    return {**out, "allowed": False, "code": "insufficient_memory", "short_gib": round(short, 1),
            "reason": reason, "blocking": blocking, "actions": actions}


def _node_name(node: str) -> str:
    return {"node1": "gx10-01", "node2": "gx10-02", "both": "both nodes"}.get(node, node)


def pair_verdict(a: str, b: str, *, capacity: dict[str, float], residents: dict[str, set[str]],
                 profile: str) -> dict:
    """Compatibility of two aliases, computed from placement and the enforced
    numbers against the node's CURRENT idle capacity. Pure."""
    pa, pb = POLICIES[a], POLICIES[b]
    why: list[str] = []
    if "gx-max" in (a, b):
        other = b if a == "gx-max" else a
        why.append("gx-max is a takeover of both nodes: its admission requires at least 100 GiB free on "
                   "each node, so every other model is drained first.")
        why.append(f"{other} starts again automatically after gx-max is released.")
        return {"a": a, "b": b, "verdict": "exclusive", "summary": "Mutually exclusive (cluster takeover)",
                "why": why}
    if pa.node != pb.node:
        why.append(f"{a} runs on {_node_name(pa.node)} and {b} on {_node_name(pb.node)}: two separate "
                   "121 GiB nodes that never share memory.")
        return {"a": a, "b": b, "verdict": "coexist", "summary": "Safe to coexist (different nodes)", "why": why}
    node = pa.node
    cap = capacity.get(node, IDLE_CAPACITY_GIB.get(node, 113.0))
    if {a, b} == {"gx-image", "gx-video"}:
        why.append("Both run on the same ComfyUI engine, one generation at a time. The router frees image "
                   "weights before loading video weights and the other way round, so they never stack.")
        return {"a": a, "b": b, "verdict": "serialized", "summary": "Serialized (one engine, one job at a time)",
                "why": why}
    if node == "node1":
        total = pa.footprint_gib + pb.footprint_gib
        ok = cap - total >= 0
        why.append(f"Measured together: {pa.footprint_gib:.0f} + {pb.footprint_gib:.0f} GiB on gx10-01 "
                   f"(about {cap:.0f} GiB usable), leaving {cap - total:.0f} GiB.")
        return {"a": a, "b": b, "verdict": "coexist" if ok else "exclusive",
                "summary": "Safe to coexist (both resident by policy)" if ok else "Do not fit together", "why": why}

    def fits(first: RuntimePolicy, second: str) -> tuple[bool, float, float]:
        left = cap - first.footprint_gib
        need = enforced_need(second, resident=False)
        return left >= need, left, need

    ab, left_a, need_b = fits(pa, b)
    ba, left_b, need_a = fits(pb, a)
    why.append(f"gx10-02 idle capacity now: about {cap:.0f} GiB.")
    why.append(f"{a} loaded (~{pa.footprint_gib:.0f} GiB) leaves {left_a:.0f} GiB; {b} needs {need_b:.0f} GiB "
               f"({POLICIES[b].admission}) -> {'fits' if ab else 'does not fit'}.")
    why.append(f"{b} loaded (~{pb.footprint_gib:.0f} GiB) leaves {left_b:.0f} GiB; {a} needs {need_a:.0f} GiB "
               f"({POLICIES[a].admission}) -> {'fits' if ba else 'does not fit'}.")
    others = sorted(residents.get(node, set()) - {a, b})
    if others:
        why.append(f"Also loaded right now: {', '.join(others)}; the live admission subtracts them too.")
    if ab and ba:
        verdict, summary = "coexist", "Safe to coexist (measured memory allows both orders)"
    elif ab or ba:
        first, second = (a, b) if ab else (b, a)
        verdict = "scheduled"
        summary = f"Scheduler dependent: {second} can join {first}, not the other way round"
        why.append(f"When {second} is loaded first, {first} waits (or the scheduler hands {second}'s idle "
                   f"memory over, depending on the profile: now {PROFILES[profile]['label']}).")
    else:
        verdict, summary = "exclusive", "Mutually exclusive on gx10-02 (not enough memory for both)"
        why.append("The scheduler unloads the idle one or queues the new job; they never run together.")
    return {"a": a, "b": b, "verdict": verdict, "summary": summary, "why": why}


# ------------------------------------------------------------ controller
class ResourceController:
    def __init__(self, cfg, cluster, actions, *, music=None, media=None, audit: Callable[..., None] | None = None,
                 node2_writer: Callable[[str, str], bool] | None = None, start_thread: bool = True) -> None:
        self.cfg = cfg
        self.cluster = cluster
        self.actions = actions
        self.music = music
        self.media = media
        self.audit = audit or (lambda **kw: None)
        self._node2_writer = node2_writer or self._ssh_write
        self._lock = threading.RLock()
        self._reason_activity: dict[str, float] = {}
        self._keepalive_at = 0.0
        self._music_model: tuple[float, dict] = (0.0, {})
        self.last_tick_error: str | None = None
        if start_thread and not cfg.offline:
            threading.Thread(target=self._loop, name="resource-control", daemon=True).start()

    # ======================================================== state files
    @property
    def guard(self) -> Path:
        return Path(self.cfg.guard_dir)

    def _read_json(self, name: str, default: Any) -> Any:
        try:
            return json.loads((self.guard / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    def _write_local(self, name: str, data: Any | None) -> None:
        self.guard.mkdir(parents=True, exist_ok=True)
        path = self.guard / name
        if data is None:
            path.unlink(missing_ok=True)
            return
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True) if not isinstance(data, str) else data,
                       encoding="utf-8")
        tmp.replace(path)

    def _ssh_write(self, name: str, content: str) -> bool:
        """Write (or, with empty content and a *-hold name, remove) one file in
        node 2's guard directory. Fixed command; `name` is from a fixed set."""
        if name not in ("profile.json", "pins.json", "node2.maintenance-hold"):
            raise ResourceError("refused: not a guard file")
        g = shlex.quote(str(self.guard))
        target = f"{g}/{name}"
        if name.endswith("-hold"):
            cmd = (f"mkdir -p {g} && date -Is > {target} && test -f {target}" if content
                   else f"rm -f {target} && test ! -e {target}")
            res = run(ssh_args(self.cfg.node2_ssh, 10) + [cmd], timeout=30)
        else:
            cmd = f"umask 022; mkdir -p {g} && cat > {target}.tmp && mv -f {target}.tmp {target}"
            res = run(ssh_args(self.cfg.node2_ssh, 10) + [cmd], timeout=30, input_text=content)
        if not res.ok:
            log.warning("node2 guard write %s failed: %s", name, res.out[-200:])
        self.cluster.node2.invalidate()
        return res.ok

    def profile(self) -> dict:
        data = self._read_json("profile.json", {})
        if not isinstance(data, dict) or data.get("profile") not in PROFILE_IDS:
            data = {"profile": "auto", "since": None, "by": None}
        return data

    def pins(self) -> dict[str, dict]:
        pins: dict[str, dict] = {}
        n2 = ((self.cluster.node2.get() or {}).get("guard") or {}).get("pins") or {}
        local = self._read_json("pins.json", {})
        for source in (n2, local):
            if isinstance(source, dict):
                for alias, meta in source.items():
                    if alias in PIN_ALIASES and isinstance(meta, dict):
                        pins[alias] = meta
        return pins

    def holds(self) -> dict[str, dict]:
        n1 = ((self.cluster.node1.get() or {}).get("guard") or {}).get("holds") or {}
        n2 = ((self.cluster.node2.get() or {}).get("guard") or {}).get("holds") or {}
        return {"node1": n1, "node2": n2}

    def maintenance(self) -> bool:
        return (self.guard / "node1.maintenance-hold").exists() or \
            bool((self.holds().get("node2") or {}).get("maintenance"))

    # ============================================================ live data
    def _music_info(self) -> dict:
        now = time.time()
        if self.music is None or self.cfg.offline:
            return {}
        if now - self._music_model[0] < 4:
            return self._music_model[1]
        try:
            info = self.music.model()
        except Exception as exc:  # noqa: BLE001 - surfaced as data
            info = {"error": str(exc)}
        self._music_model = (now, info)
        return info

    def _node_mem(self, facts: dict) -> dict:
        mem = (facts or {}).get("memory") or {}
        total = mem.get("SwapTotal") or 0
        psi = (((facts or {}).get("psi") or {}).get("memory") or {}).get("full") or {}
        return {
            "reachable": bool(facts.get("reachable", True)) and not facts.get("offline"),
            "mem_total_gib": _g(mem.get("MemTotal")),
            "mem_available_gib": _g(mem.get("MemAvailable")),
            "swap_total_gib": _g(total),
            "swap_used_gib": _g(total - (mem.get("SwapFree") or 0)) if total else None,
            "psi_full_avg10": psi.get("avg10"),
            "disk": (facts or {}).get("disk"),
        }

    def snapshot(self) -> dict:
        """Everything the Resource Map, the compatibility view and the
        Playground widget show. Read-only."""
        n1 = self.cluster.node1.get() or {}
        n2 = self.cluster.node2.get() or {}
        svc = self.cluster.services.get() or {}
        gx_state = self.cluster.gxmax_state()
        profile = self.profile()
        pins = self.pins()
        holds = self.holds()
        gxmax_hold = bool((holds["node2"].get("gxmax") or {}).get("active"))
        maint = bool(holds["node1"].get("maintenance") or holds["node2"].get("maintenance"))
        gx_busy = gx_state in ("acquiring", "ready", "releasing")
        media = (svc.get("media") or {}).get("body") if (svc.get("media") or {}).get("ok") else None
        media = media if isinstance(media, dict) else None
        music = self._music_info()
        engine = music.get("engine") or {}
        creative = self.media.snapshot() if self.media is not None and hasattr(self.media, "snapshot") else {}

        runtimes: dict[str, dict] = {}

        def swap_state(key: str, alias: str) -> tuple[str, str]:
            probe = svc.get(key) or {}
            if not probe.get("ok"):
                return "ERROR", "llama-swap unreachable"
            for r in (probe.get("body") or {}).get("running") or []:
                if r.get("model") == alias:
                    st = str(r.get("state", "")).lower()
                    if st == "ready":
                        return "READY", "loaded"
                    if st in ("starting",):
                        return "LOADING", "starting"
                    if st in ("stopping", "shutdown"):
                        return "DRAINING", "stopping"
                    return "ERROR", st or "unknown"
            return "UNLOADED", "starts on the next request"

        for alias in ("gx-mini", "gx-fast"):
            state, detail = swap_state("swap_node1_running", alias)
            if gx_busy:
                state, detail = "BLOCKED", f"drained while gx-max is {gx_state}"
            runtimes[alias] = {"state": state, "detail": detail}
        state, detail = swap_state("swap_node2_running", "gx-reason")
        if gx_busy or gxmax_hold:
            state, detail = "BLOCKED", "drained while gx-max owns the cluster"
        elif maint and state != "READY":
            state, detail = "BLOCKED", "Maintenance mode: loads are refused"
        activity = self._reason_activity.get("running")
        runtimes["gx-reason"] = {"state": "GENERATING" if state == "READY" and activity else state,
                                 "detail": detail, "inflight": activity}

        # media: one engine for image and video
        resident_alias = media.get("resident_alias") if media else None
        busy_holder = str(media.get("held_by") or "") if media else ""
        for alias, prefix in (("gx-image", "image"), ("gx-video", "video")):
            waiting = [j for j in creative.get("jobs", []) if j.get("alias") == alias and j.get("waiting")]
            if gx_busy or gxmax_hold:
                st, dt = "BLOCKED", "drained while gx-max owns the cluster"
            elif media is None:
                st, dt = "ERROR", "media router unreachable"
            elif busy_holder.startswith(prefix):
                st, dt = "GENERATING", f"job {busy_holder}"
            elif waiting:
                st, dt = "WAITING", waiting[0]["waiting"].get("reason", "")
            elif maint:
                st, dt = "BLOCKED", "Maintenance mode: new jobs wait"
            elif resident_alias == alias:
                st, dt = "READY", "weights loaded (warm)"
            else:
                st, dt = "UNLOADED", "loads with the next job"
            runtimes[alias] = {"state": st, "detail": dt,
                               "queue": sum(1 for j in creative.get("jobs", []) if j.get("alias") == alias
                                            and not j.get("done"))}
        if media is not None and media.get("video_queue_depth"):
            runtimes["gx-video"]["queue"] = runtimes["gx-video"].get("queue", 0) + int(media["video_queue_depth"])

        # music
        m_state = str(engine.get("state") or ("error" if music.get("error") else "unknown"))
        queue = music.get("queue") or {}
        jobs = music.get("jobs") or {}
        m_map = {"ready": "READY", "unloaded": "UNLOADED", "loading": "LOADING", "unloading": "DRAINING",
                 "failed": "ERROR"}
        st = m_map.get(m_state, "ERROR")
        dt = engine.get("detail") or ""
        if queue.get("current_job") and st == "READY":
            st, dt = "GENERATING", f"job {queue['current_job']}"
        elif engine.get("blocked_by") and st != "READY":
            st, dt = "BLOCKED" if not queue.get("active") else "WAITING", engine["blocked_by"]
        elif queue.get("active") and st in ("UNLOADED", "ERROR") and not music.get("error"):
            st, dt = "WAITING", "queued music job is waiting for memory or the model"
        if music.get("error"):
            st, dt = "ERROR", "music service unreachable"
        runtimes["gx-music"] = {"state": st, "detail": dt, "queue": queue.get("active", 0),
                                "last_load_seconds": engine.get("last_load_seconds"),
                                "idle_seconds": engine.get("idle_seconds"), "jobs": jobs}

        gx_map = {"down": "UNLOADED", "acquiring": "LOADING", "ready": "READY", "releasing": "DRAINING"}
        runtimes["gx-max"] = {"state": gx_map.get(gx_state, "ERROR"), "detail": gx_state}
        orch_ok = (svc.get("orchestrator") or {}).get("ok")
        runtimes["gx-auto"] = {"state": "READY" if orch_ok else "ERROR",
                               "detail": "routes to mini / fast / reason" if orch_ok else "orchestrator unreachable"}

        for alias, r in runtimes.items():
            p = POLICIES[alias]
            r.update({"alias": alias, "node": p.node, "pinned": alias in pins, "pin": pins.get(alias),
                      "footprint_gib": p.footprint_gib, "residency": p.residency, "idle_ttl_s": p.idle_ttl_s,
                      "controls": list(p.controls)})
            if alias in pins:
                r["pin_state"] = self._pin_state(alias, r, n1 if p.node == "node1" else n2, holds, gx_busy)

        nodes = {"node1": {**self._node_mem({**n1, "reachable": True}), "name": "gx10-01",
                           "ledger": ((n1.get("guard") or {}).get("ledger")) or {},
                           "holds": holds["node1"],
                           "runtimes": ["gx-mini", "gx-fast", "gx-max"]},
                 "node2": {**self._node_mem(n2), "name": "gx10-02",
                           "ledger": ((n2.get("guard") or {}).get("ledger")) or {},
                           "holds": holds["node2"],
                           "runtimes": ["gx-reason", "gx-image", "gx-video", "gx-music", "gx-max"]}}
        return {
            "generated_at": time.time(),
            "profile": {**profile, **PROFILES[profile["profile"]]},
            "profiles": [{"id": k, **v} for k, v in PROFILES.items()],
            "maintenance": maint,
            "gxmax": {"state": gx_state, "hold": gxmax_hold},
            "nodes": nodes,
            "runtimes": runtimes,
            "pins": pins,
            "queue": {"creative": creative.get("counts", {}), "music": queue.get("active", 0),
                      "media_router_video": (media or {}).get("video_queue_depth", 0)},
            "media_router": {k: (media or {}).get(k) for k in ("version", "busy", "held_by", "resident_models",
                                                               "resident_alias", "idle_seconds",
                                                               "idle_free_seconds", "policy", "last_refusal")},
            "policies": {a: p.public() for a, p in POLICIES.items()},
            "reserve_gib": RESERVE_GIB,
        }

    def _pin_state(self, alias: str, runtime: dict, facts: dict, holds: dict, gx_busy: bool) -> dict:
        avail = _g(((facts or {}).get("memory") or {}).get("MemAvailable"))
        if gx_busy:
            return {"honoured": False, "reason": "suspended: gx-max owns the cluster"}
        if any(h.get("maintenance") for h in holds.values()):
            return {"honoured": False, "reason": "suspended: Maintenance mode"}
        if avail is not None and avail < RESERVE_GIB:
            return {"honoured": False, "reason": f"suspended: only {avail:.0f} GiB available (reserve 30 GiB)"}
        if runtime.get("state") not in ("READY", "GENERATING"):
            return {"honoured": False, "reason": "not loaded; the pin applies once it is"}
        return {"honoured": True, "reason": "kept loaded past the idle timer"}

    # ========================================================== admission
    def residents(self, snap: dict, node: str) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for alias in NODE2_TENANTS if node == "node2" else ("gx-mini", "gx-fast"):
            r = snap["runtimes"].get(alias) or {}
            if r.get("state") in ("READY", "GENERATING", "LOADING", "DRAINING"):
                out[alias] = {"active": r.get("state") in ("GENERATING", "LOADING")}
        # ComfyUI holds one model set: the resident alias, not both
        return out

    def admission(self, alias: str, *, variant: str | None = None, snap: dict | None = None) -> dict:
        if alias not in POLICIES:
            raise ResourceError("unknown alias", 404)
        snap = snap or self.snapshot()
        p = POLICIES[alias]
        if alias == "gx-max":
            views = {}
            for node in ("node1", "node2"):
                avail = snap["nodes"][node]["mem_available_gib"]
                views[node] = {"available_gib": avail, "idle_capacity_gib": IDLE_CAPACITY_GIB[node]}
            state = snap["gxmax"]["state"]
            allowed = state == "down" and not snap["maintenance"]
            reason = ("gx-max drains both nodes first (graceful), then requires >= 100 GiB available on each"
                      if allowed else ("Maintenance mode is on" if snap["maintenance"] else f"gx-max is {state}"))
            return {"alias": alias, "allowed": allowed, "code": "takeover", "reason": reason, "nodes": views,
                    "enforced_by": p.admission, "will_drain": [a for a in GENERATIVE if a != "gx-max"
                                                                and snap["runtimes"][a]["state"] in
                                                                ("READY", "GENERATING", "LOADING")],
                    "actions": []}
        node = "node1" if p.node == "node1" else "node2"
        holds = {"gxmax": snap["gxmax"]["hold"] or snap["gxmax"]["state"] in ("acquiring", "ready", "releasing"),
                 "maintenance": snap["maintenance"]}
        view = admission_view(alias, snap["nodes"][node]["mem_available_gib"], self.residents(snap, node),
                              variant=variant, pins=set(snap["pins"]), holds=holds)
        view["state"] = snap["runtimes"][alias]["state"]
        return view

    def compatibility(self, snap: dict | None = None) -> dict:
        snap = snap or self.snapshot()
        capacity, residents = {}, {}
        for node in ("node1", "node2"):
            res = self.residents(snap, node)
            residents[node] = set(res)
            avail = snap["nodes"][node]["mem_available_gib"]
            if avail is None:
                capacity[node] = IDLE_CAPACITY_GIB[node]
            else:
                # what the node would have with nothing on-demand loaded
                held = sum(POLICIES[a].footprint_gib for a in res if POLICIES[a].residency != "resident")
                capacity[node] = min(IDLE_CAPACITY_GIB[node] + 4, avail + held) if node == "node2" else \
                    avail + sum(POLICIES[a].footprint_gib for a in res)
        pairs = []
        for i, a in enumerate(GENERATIVE):
            for b in GENERATIVE[i + 1:]:
                pairs.append(pair_verdict(a, b, capacity=capacity, residents=residents,
                                          profile=snap["profile"]["profile"]))
        return {"generated_at": time.time(), "aliases": list(GENERATIVE), "capacity_gib": capacity,
                "pairs": pairs, "legend": {
                    "coexist": "Safe to coexist", "scheduled": "Scheduler dependent",
                    "serialized": "Serialized", "exclusive": "Mutually exclusive"}}

    def explain(self, alias: str, variant: str | None = None) -> dict:
        """'Why am I waiting?' for a queued job of `alias`. Never an ETA."""
        snap = self.snapshot()
        view = self.admission(alias, variant=variant, snap=snap)
        if view.get("code") == "gx_max_active" or snap["gxmax"]["state"] in ("acquiring", "ready", "releasing"):
            return {"code": "gx_max_active", "reason": "Waiting for gx-max to release the cluster",
                    "detail": f"gx-max is {snap['gxmax']['state']}", "next": "starts automatically after release"}
        if view.get("code") == "maintenance":
            return {"code": "maintenance", "reason": "Waiting for Maintenance mode to finish",
                    "detail": "an administrator put the cluster into Maintenance", "next": "starts when it ends"}
        busy = [a for a in NODE2_TENANTS if snap["runtimes"][a]["state"] == "GENERATING" and a != alias]
        if alias in ("gx-image", "gx-video") and snap["runtimes"]["gx-image"]["state"] == "GENERATING" \
                or alias in ("gx-image", "gx-video") and snap["runtimes"]["gx-video"]["state"] == "GENERATING":
            return {"code": "engine_busy", "reason": "Waiting for the current media job to finish",
                    "detail": "ComfyUI runs one generation at a time", "next": "starts right after it"}
        if view.get("allowed"):
            return {"code": "starting", "reason": "Starting", "detail": view.get("reason", ""),
                    "next": "loading the model if needed"}
        blocking = view.get("blocking") or []
        profile = snap["profile"]["profile"]
        if blocking:
            first = blocking[0]
            nxt = "unloads automatically when it is idle"
            if first in snap["pins"]:
                nxt = f"{first} is pinned; unpin or unload it in Resource Control"
            elif profile == "text" and first == "gx-reason":
                nxt = "Text profile keeps gx-reason loaded; it unloads after 15 minutes idle"
            elif busy:
                nxt = f"{', '.join(busy)} is working; the scheduler waits for it"
            reason = f"Waiting for {first} to unload" if first != alias else "Waiting for enough gx10-02 memory"
        else:
            reason, nxt = f"Waiting for enough {_node_name(POLICIES[alias].node)} memory", "retries automatically"
        return {"code": view.get("code"), "reason": reason, "detail": view.get("reason"),
                "need_gib": view.get("need_gib"), "available_gib": view.get("available_gib"),
                "blocking": blocking, "next": nxt, "actions": view.get("actions", [])}

    # ============================================= creative scheduling hooks
    def creative_gate(self, alias: str, variant: str | None = None) -> dict | None:
        """None when a creative job may be submitted now; else a wait reason.
        May first free idle tenants, as the active profile allows."""
        snap = self.snapshot()
        view = self.admission(alias, variant=variant, snap=snap)
        if alias in ("gx-image", "gx-video") and (snap["media_router"].get("busy")):
            # The router queues behind its own slot; not a memory question.
            return None if view.get("code") not in ("gx_max_active", "maintenance") else self.explain(alias, variant)
        if view.get("allowed"):
            return None
        if view.get("code") != "insufficient_memory":
            return self.explain(alias, variant)
        profile = snap["profile"]["profile"]
        plan = (view.get("actions") or [{}])[0].get("unload") or []
        allowed = self.preemptable(profile, alias, plan, snap)
        if allowed and len(allowed) == len(plan):
            done = self.free_tenants(allowed, user="scheduler", reason=f"{PROFILES[profile]['label']} profile: "
                                     f"making room for {alias}")
            if done:
                return {"code": "freeing", "reason": f"Unloading {' and '.join(allowed)} to make room",
                        "detail": view.get("reason"), "next": "starts when the memory is back"}
        return self.explain(alias, variant)

    def preemptable(self, profile: str, alias: str, plan: list[str], snap: dict) -> list[str]:
        """Which idle tenants the profile lets the scheduler unload for `alias`."""
        out = []
        for victim in plan:
            if victim in snap["pins"]:
                continue
            if profile == "text" and victim == "gx-reason":
                continue
            if profile in ("maintenance", "max"):
                continue
            if victim == "gx-reason":
                idle_for = time.time() - self._reason_activity.get("last_active", 0)
                if self._reason_activity.get("running"):
                    continue
                if profile == "auto" and idle_for < 300:
                    continue  # AUTO: only a gx-reason idle for 5 minutes
                if profile == "music" and alias != "gx-music":
                    continue
            if victim == "gx-music" and profile == "music":
                continue
            if victim in ("gx-image", "gx-video") and profile == "media":
                continue
            out.append(victim)
        return out

    def free_tenants(self, aliases: list[str], *, user: str, reason: str) -> list[str]:
        done = []
        for alias in aliases:
            ok, msg = self._unload(alias)
            self.audit(user=user, ip="", action=f"resources.free.{alias}", outcome="ok" if ok else "failed",
                       reason=reason, detail=msg[:200])
            if ok:
                done.append(alias)
        self.cluster.invalidate()
        return done

    def _unload(self, alias: str) -> tuple[bool, str]:
        if alias == "gx-reason":
            if self.reason_inflight():
                return False, "gx-reason has requests in flight"
            return self.cluster.swap_unload("gx-reason")
        if alias in ("gx-image", "gx-video"):
            return self.media_free()
        if alias == "gx-music" and self.music is not None:
            try:
                info = self.music.lifecycle("unload", user="scheduler")
                return True, json.dumps(info)[:200]
            except Exception as exc:  # noqa: BLE001
                return False, str(exc)
        return False, f"{alias} has no scheduler unload"

    def media_free(self) -> tuple[bool, str]:
        """Router-mediated free (keeps the router's residency record true)."""
        key = self.cfg.secret("GX_MEDIA_API_KEY")
        try:
            res = http("POST", f"{self.cfg.media_base}/v1/admin/free", body={},
                       headers={"Authorization": f"Bearer {key}"} if key else {}, timeout=45)
        except HTTPError as exc:
            return False, exc.message
        try:
            body = res.json()
        except ValueError:
            body = {}
        return res.status == 200 and bool(body.get("freed")), json.dumps(body)[:200]

    def reason_inflight(self) -> int | None:
        """Running + waiting requests on gx-reason (vLLM metrics, read inside
        the container over SSH; never through llama-swap, which would reset
        its idle timer)."""
        if self.cfg.offline:
            return None
        script = ("docker exec gx-reason python3 -c \"import urllib.request as u;"
                  "print(u.urlopen('http://127.0.0.1:25800/metrics',timeout=3).read().decode())\" 2>/dev/null"
                  " | awk '/^vllm:num_requests_(running|waiting)[{ ]/{s+=$NF} END{print s+0}'")
        res = run(ssh_args(self.cfg.node2_ssh, 5) + [script], timeout=15)
        try:
            return int(float(res.out.strip().splitlines()[-1])) if res.ok and res.out.strip() else None
        except (ValueError, IndexError):
            return None

    # ============================================================ profiles
    def plan_profile(self, target: str, snap: dict | None = None) -> dict:
        if target not in PROFILE_IDS:
            raise ResourceError("unknown profile")
        snap = snap or self.snapshot()
        current = snap["profile"]["profile"]
        rt = snap["runtimes"]
        loaded = [a for a in GENERATIVE if rt[a]["state"] in ("READY", "GENERATING", "LOADING")]
        active = [a for a in GENERATIVE if rt[a]["state"] in ("GENERATING", "LOADING")]
        queued = {a: rt[a].get("queue", 0) for a in ("gx-image", "gx-video", "gx-music") if rt[a].get("queue")}
        stays, may_drain, now_drain, conflicts = list(loaded), [], [], []
        if target == "maintenance":
            now_drain = [a for a in loaded if a in NODE2_TENANTS and a not in active]
            stays = [a for a in loaded if a not in now_drain]
            may_drain = [a for a in active if a in NODE2_TENANTS]
            if active:
                conflicts.append(f"{', '.join(active)} will finish the current work first, then unload")
        elif target == "max":
            now_drain = [a for a in loaded if a != "gx-max"]
            stays = []
            if active:
                conflicts.append(f"Active work on {', '.join(active)} is stopped by the gx-max drain")
        elif target == "media":
            may_drain = [a for a in ("gx-reason", "gx-music") if a in loaded]
        elif target == "music":
            may_drain = [a for a in ("gx-image", "gx-video", "gx-reason") if a in loaded]
        if current == "max" and target != "max" and snap["gxmax"]["state"] in ("acquiring", "ready"):
            now_drain.append("gx-max")
            conflicts.append("gx-max is released (graceful: in-flight requests finish first)")
        if current == "maintenance" and target != "maintenance":
            conflicts.append("Maintenance ends: queued jobs start again; nothing is loaded eagerly")
        needs_confirm = target in ("max",) or bool(now_drain and (active or "gx-max" in now_drain)) \
            or (target == "maintenance" and bool(active))
        return {"from": current, "to": target, "label": PROFILES[target]["label"],
                "summary": PROFILES[target]["summary"], "stays": stays, "may_drain": may_drain,
                "drains_now": now_drain, "active": active, "queued": queued, "conflicts": conflicts,
                "needs_confirm": needs_confirm,
                "confirm_phrase": "gx-max" if target == "max" else None}

    def set_profile(self, target: str, *, user: str, ip: str = "", confirm: Any = None,
                    source: str = "control-center") -> dict:
        plan = self.plan_profile(target)
        if source == "playground" and target == "maintenance":
            raise ResourceError("Maintenance is an advanced Control Center operation", 403)
        if plan["needs_confirm"]:
            expected = plan["confirm_phrase"] or True
            if confirm != expected:
                raise ResourceError("this profile change affects running work and must be confirmed"
                                    + (f" (type {plan['confirm_phrase']})" if plan["confirm_phrase"] else ""), 409)
        current = plan["from"]
        steps: list[str] = []
        with self._lock:
            if target == "max" and self.cluster.gxmax_state() == "down":
                job = self.actions.submit("model.gx-max.load", user=user, ip=ip, confirm="gx-max")
                steps.append(f"gx-max acquire started (job {job.id})")
            if current == "max" and target != "max" and self.cluster.gxmax_state() in ("ready",):
                job = self.actions.submit("model.gx-max.unload", user=user, ip=ip, confirm=True)
                steps.append(f"gx-max release started (job {job.id})")
            record = {"profile": target, "since": time.time(), "by": user, "previous": current, "source": source}
            self._write_local("profile.json", record)
            if not self._node2_writer("profile.json", json.dumps(record, sort_keys=True)):
                steps.append("WARNING: gx10-02 did not receive the profile file (retrying in the background)")
            if target == "maintenance":
                steps += self.enter_maintenance(user=user)
            elif current == "maintenance" or self.maintenance():
                steps += self.exit_maintenance(user=user)
        self.audit(user=user, ip=ip, action="resources.profile", outcome="ok", previous=current, profile=target,
                   source=source)
        self.cluster.invalidate()
        return {"profile": target, "steps": steps, "plan": plan}

    def enter_maintenance(self, *, user: str) -> list[str]:
        steps = []
        self._write_local("node1.maintenance-hold", time.strftime("%Y-%m-%dT%H:%M:%S%z") + f" {user}\n")
        steps.append("gx10-01 maintenance hold set")
        steps.append("gx10-02 maintenance hold set" if self._node2_writer("node2.maintenance-hold", "1")
                     else "WARNING: could not set the gx10-02 maintenance hold")
        # The music supervisor and the media router react to the hold on their
        # own (next tick). gx-reason is unloaded now if it is idle.
        snap = self.snapshot()
        if snap["runtimes"]["gx-reason"]["state"] == "READY":
            ok, msg = self._unload("gx-reason")
            steps.append("gx-reason unloaded" if ok else f"gx-reason left loaded: {msg}")
        ok, _ = self.media_free()
        steps.append("idle ComfyUI weights freed through the router" if ok
                     else "ComfyUI: nothing to free now (or a job is finishing; the router frees it after)")
        return steps

    def exit_maintenance(self, *, user: str) -> list[str]:
        self._write_local("node1.maintenance-hold", None)
        ok = self._node2_writer("node2.maintenance-hold", "")
        return ["gx10-01 maintenance hold removed",
                "gx10-02 maintenance hold removed" if ok else "WARNING: could not remove the gx10-02 hold"]

    # ============================================================== pins
    def set_pin(self, alias: str, on: bool, *, user: str, ip: str = "") -> dict:
        if alias not in PIN_ALIASES:
            raise ResourceError(f"{alias} cannot be pinned")
        node = POLICIES[alias].node
        pins = self.pins()
        node_pins = {a: m for a, m in pins.items() if POLICIES[a].node == node}
        if on:
            node_pins[alias] = {"by": user, "since": time.time()}
        else:
            node_pins.pop(alias, None)
        content = json.dumps(node_pins, sort_keys=True)
        ok = self._node2_writer("pins.json", content) if node == "node2" else True
        if node == "node1":
            self._write_local("pins.json", node_pins)
        if not ok:
            raise ResourceError("gx10-02 did not accept the pin change", 503)
        self.audit(user=user, ip=ip, action=f"resources.{'pin' if on else 'unpin'}", outcome="ok", alias=alias)
        self.cluster.node2.invalidate()
        return {"alias": alias, "pinned": on}

    # ===================================================== manual controls
    def control(self, alias: str, op: str, *, user: str, ip: str = "", confirm: Any = None) -> dict:
        """LOAD / UNLOAD / DRAIN / PIN / UNPIN through the sanctioned paths."""
        if alias not in POLICIES:
            raise ResourceError("unknown alias", 404)
        policy = POLICIES[alias]
        if op not in policy.controls:
            raise ResourceError(f"{op} is not available for {alias}")
        if op in ("pin", "unpin"):
            return self.set_pin(alias, op == "pin", user=user, ip=ip)
        snap = self.snapshot()
        if op == "load":
            view = self.admission(alias, snap=snap)
            if alias == "gx-max":
                job = self.actions.submit("model.gx-max.load", user=user, ip=ip, confirm=confirm)
                return {"started": job.as_dict(with_output=False)}
            if not view.get("allowed"):
                if confirm == "unload_and_continue" and view.get("actions"):
                    victims = view["actions"][0]["unload"]
                    busy = [v for v in victims if snap["runtimes"][v]["state"] in ("GENERATING", "LOADING")]
                    if busy:
                        raise ResourceError(f"{', '.join(busy)} is working; it is not interrupted", 409)
                    freed = self.free_tenants(victims, user=user, reason=f"manual: load {alias}")
                    if len(freed) != len(victims):
                        raise ResourceError(f"could not unload {', '.join(set(victims) - set(freed))}", 409)
                    time.sleep(5)
                    self.cluster.invalidate()
                    view = self.admission(alias)
                if not view.get("allowed"):
                    raise AdmissionBlocked(view)
            if alias == "gx-music":
                job = self._background(f"Load {alias}", user,
                                       lambda: self.music.lifecycle("load", user=user))
                return {"started": job}
            if alias in ("gx-mini", "gx-fast", "gx-reason"):
                job = self.actions.submit(f"model.{alias}.load", user=user, ip=ip)
                return {"started": job.as_dict(with_output=False)}
            raise ResourceError(f"{alias} loads with its next job")
        if op in ("unload", "drain"):
            state = snap["runtimes"][alias]["state"]
            if alias == "gx-max":
                job = self.actions.submit("model.gx-max.unload", user=user, ip=ip, confirm=True)
                return {"started": job.as_dict(with_output=False)}
            if op == "unload" and state in ("GENERATING",) and confirm is not True:
                raise ResourceError(f"{alias} is generating; use DRAIN to let it finish first", 409)
            if op == "drain":
                job = self._background(f"Drain {alias}", user, lambda: self._drain(alias))
                return {"started": job}
            if alias in ("gx-mini", "gx-fast", "gx-reason"):
                job = self.actions.submit(f"model.{alias}.unload", user=user, ip=ip, confirm=True)
                return {"started": job.as_dict(with_output=False)}
            if alias in ("gx-image", "gx-video"):
                ok, msg = self.media_free()
                self.audit(user=user, ip=ip, action=f"resources.unload.{alias}", outcome="ok" if ok else "refused")
                if not ok:
                    raise ResourceError(f"the media router did not free ComfyUI now: {msg}", 409)
                return {"done": True, "detail": msg}
            if alias == "gx-music":
                return {"done": True, "detail": self.music.lifecycle("unload", user=user)}
        raise ResourceError("unsupported operation")

    def _drain(self, alias: str) -> dict:
        """Wait (bounded) for active work to finish, then unload."""
        deadline = time.time() + 1800
        while time.time() < deadline:
            state = self.snapshot()["runtimes"][alias]["state"]
            busy = state in ("GENERATING", "LOADING", "WAITING")
            if alias == "gx-reason":
                busy = busy or bool(self.reason_inflight())
            if not busy:
                ok, msg = self._unload(alias)
                return {"ok": ok, "detail": msg}
            time.sleep(10)
            self.cluster.invalidate()
        return {"ok": False, "detail": "still busy after 30 minutes"}

    _bg: dict[str, dict] = {}

    def _background(self, label: str, user: str, fn: Callable[[], Any]) -> dict:
        import secrets as _secrets

        job = {"id": _secrets.token_hex(8), "label": label, "user": user, "state": "running",
               "started": time.time(), "result": None}
        self._bg[job["id"]] = job

        def runner() -> None:
            try:
                job["result"] = fn()
                job["state"] = "succeeded" if not (isinstance(job["result"], dict)
                                                   and job["result"].get("ok") is False) else "failed"
            except Exception as exc:  # noqa: BLE001
                job["result"] = {"error": str(exc)}
                job["state"] = "failed"
            job["ended"] = time.time()
            self.audit(user=user, ip="", action=f"resources.{label}", outcome=job["state"])
            self.cluster.invalidate()
        threading.Thread(target=runner, daemon=True, name="resource-op").start()
        return dict(job)

    def background_job(self, job_id: str) -> dict:
        job = self._bg.get(job_id)
        if job is None:
            raise ResourceError("no such operation", 404)
        return dict(job)

    # ============================================================ ticking
    def _loop(self) -> None:
        while True:
            try:
                self.tick()
                self.last_tick_error = None
            except Exception as exc:  # noqa: BLE001
                self.last_tick_error = f"{type(exc).__name__}: {exc}"
                log.warning("resource tick failed: %s", self.last_tick_error)
            time.sleep(20)

    def tick(self) -> None:
        snap = self.snapshot()
        # gx-reason activity, only while it is loaded (never wakes it up)
        if snap["runtimes"]["gx-reason"]["state"] in ("READY", "GENERATING"):
            n = self.reason_inflight()
            self._reason_activity["running"] = n or 0
            if n:
                self._reason_activity["last_active"] = time.time()
            self._reason_activity.setdefault("last_active", time.time())
        else:
            self._reason_activity = {}
        # MAX profile ends when gx-max is down and no acquire is running
        prof = snap["profile"]["profile"]
        if prof == "max" and snap["gxmax"]["state"] == "down" and not any(
                j["action"].startswith("model.gx-max") for j in self.actions.running()):
            since = snap["profile"].get("since") or 0
            if time.time() - since > 60:
                record = {"profile": "auto", "since": time.time(), "by": "scheduler", "previous": "max",
                          "source": "gx-max released"}
                self._write_local("profile.json", record)
                self._node2_writer("profile.json", json.dumps(record, sort_keys=True))
        # node 2 missed a profile write: re-assert
        n2prof = ((self.cluster.node2.get() or {}).get("guard") or {}).get("profile") or {}
        local = self.profile()
        if (self.cluster.node2.get() or {}).get("reachable") and n2prof.get("profile") != local["profile"] \
                and local.get("since"):
            self._node2_writer("profile.json", json.dumps(local, sort_keys=True))
        # pin keep-alive for gx-reason: llama-swap's idle timer is reset by a
        # proxied health request, sent only while the model is already READY.
        if "gx-reason" in snap["pins"] and snap["runtimes"]["gx-reason"]["state"] == "READY":
            pin = snap["runtimes"]["gx-reason"].get("pin_state") or {}
            if pin.get("honoured") and time.time() - self._keepalive_at > 300:
                self._keepalive_at = time.time()
                try:
                    http("GET", f"{self.cfg.node2_swap_base}/upstream/gx-reason/health",
                         headers=self.cluster.swap_headers(), timeout=10)
                except HTTPError:
                    pass


class AdmissionBlocked(ResourceError):
    def __init__(self, view: dict) -> None:
        super().__init__(view.get("reason") or "not admissible now", 409)
        self.view = view


def _g(value: Any) -> float | None:
    return None if value in (None, "") else round(float(value) / GIB, 2)
