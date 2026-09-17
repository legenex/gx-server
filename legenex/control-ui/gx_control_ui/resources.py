"""Resource Control (D-037): profiles, live resource map, admission
explanations, compatibility, pins and Maintenance mode.

This module does not replace any admission component. It EXPLAINS and
COORDINATES the ones that already enforce memory safety:

* gx_orchestrator.resource_guard (30 GiB reserve; gx-music, gx-safe-run),
* the node-2 media router's memory admission (D-038: growth 57/72/107 GiB
  cold, measured growth warm, always plus the 30 GiB reserve, minus what
  gx-music has not taken yet),
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
#: The most node 2 ever reports available (measured 113-117 GiB): a job whose
#: growth plus the reserve is larger can never run and is not queued.
NODE2_MAX_AVAILABLE_GIB = 117.0

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
    #: False until the owning workstream published a measured footprint (plt.md
    #: section 7); the numbers above are then 0 and never used for arithmetic.
    measured_ok: bool = True

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
        "gx-image", "node2", "image", "ComfyUI behind gx-media-router", 57.0, 57.0, 600, "on-demand", 50, True, True,
        "one ComfyUI generation at a time; image and video weights never stack",
        "media router: 57 GiB + 30 GiB reserve",
        ("unload", "pin", "unpin"), "114 -> 57.5 GiB available during a cold generation (2026-09-17)",
        "weights load with the first job", {"edit": 57.0}),
    "gx-video": RuntimePolicy(
        "gx-video", "node2", "video", "ComfyUI behind gx-media-router", 72.0, 72.0, 600, "on-demand", 40, True, True,
        "needs most of gx10-02: waits for gx-reason and gx-music to unload (a cold video plus the 30 GiB "
        "reserve leaves no room for either)",
        "media router: 72 GiB + 30 GiB reserve (a keyframe edit, 107 GiB, cannot run)",
        ("unload", "pin", "unpin"), "114 -> 42 GiB available for t2v/i2v; 7 GiB for keyframe edit (2026-09-17)",
        "weights load with the first job", {"keyframe_edit": 107.0}),
    "gx-music": RuntimePolicy(
        "gx-music", "node2", "music", "ACE-Step 1.5 XL (gx-music supervisor)", 32.0, 26.0, 600, "on-demand", 50, True,
        True, "coexists with gx-reason (measured); never with a cold video (the 30 GiB reserve); hands idle "
        "ComfyUI weights over through the router, and the router unloads it when idle for a video",
        "resource guard: 32 GiB + 30 GiB reserve, plus any media load in progress",
        ("load", "unload", "drain", "pin", "unpin"),
        "24-27 GiB loaded (115 -> 88-90 GiB available, 2026-09-17); minimum 37.7 GiB available next to "
        "gx-reason (Stage A)", "82-107 s"),
    "gx-max": RuntimePolicy(
        "gx-max", "both", "cluster", "SGLang TP=2 across both nodes", 100.0, 105.0, None, "takeover", 100, False,
        True, "takes over BOTH nodes; everything else is drained first", "gx-max takeover policy (>= 100 GiB per node)",
        ("load", "unload"), "load transient ~117 GiB/node absorbed by swap; steady 105 GiB/node", "about 9 minutes"),
    "gx-auto": RuntimePolicy(
        "gx-auto", "node1", "router", "gx-orchestrator classifier", 0.0, 0.0, None, "routing", 0, False, True,
        "routes to mini/fast/reason; never acquires gx-max", "none (routing only)", (),
        "no memory of its own"),
}
# ------------------------------------------------ Build V3 node-2 supervisors
#: Static facts of gx-voice / gx-call / gx-live. Their memory numbers come ONLY
#: from the measured footprint in the registry (apply_measurements); until then
#: Resource Control says "not measured yet" and leaves admission to the
#: supervisor, which enforces the 30 GiB reserve itself (plt.md section 5).
V3_SERVICES: dict[str, dict[str, Any]] = {
    "gx-voice": {"kind": "voice", "engine": "Qwen3-TTS 1.7B (gx-voice supervisor)", "idle_ttl_s": 600,
                 "priority": 45, "exclusive": "coexists with the other node-2 tenants while the 30 GiB reserve "
                 "holds; one voice job at a time"},
    "gx-call": {"kind": "realtime", "engine": "NemotronLabs VoiceChat 11B (gx-call supervisor)", "idle_ttl_s": 600,
                "priority": 55, "exclusive": "one live call at a time; a live call is never interrupted by the "
                "scheduler (only by gx-max or an explicit unload)"},
    "gx-live": {"kind": "realtime", "engine": "MiniCPM-o 4.5 (gx-live supervisor)", "idle_ttl_s": 600,
                "priority": 55, "exclusive": "one live session at a time; a live session is never interrupted by "
                "the scheduler (only by gx-max or an explicit unload)"},
}


def v3_policy(alias: str, fp: dict | None = None) -> RuntimePolicy:
    """The RuntimePolicy of a Build V3 supervisor from its measured footprint (or none)."""
    meta = V3_SERVICES[alias]
    ok = isinstance(fp, dict) and isinstance(fp.get("cold_gib"), (int, float)) \
        and isinstance(fp.get("resident_gib"), (int, float))
    cold = float(fp["cold_gib"]) if ok else 0.0  # type: ignore[index]
    resident = float(fp["resident_gib"]) if ok else 0.0  # type: ignore[index]
    startup = f"{fp.get('startup_s'):.0f} s (measured)" if ok and isinstance(fp.get("startup_s"), (int, float)) \
        else "not measured yet"  # type: ignore[union-attr]
    measured = (f"{resident:.0f} GiB loaded, {cold:.0f} GiB to start (measured {fp.get('measured')})"  # type: ignore[union-attr]
                if ok else "not measured yet")
    return RuntimePolicy(
        alias, "node2", meta["kind"], meta["engine"], cold, resident, meta["idle_ttl_s"], "on-demand",
        meta["priority"], True, True, meta["exclusive"],
        f"{alias} supervisor: its growth + other tenants' pending memory + 30 GiB reserve",
        ("load", "unload", "drain", "pin", "unpin"), measured, startup, measured_ok=ok)


for _alias in V3_SERVICES:
    POLICIES[_alias] = v3_policy(_alias)

GENERATIVE = ("gx-mini", "gx-fast", "gx-reason", "gx-image", "gx-video", "gx-music", "gx-voice", "gx-call",
              "gx-live", "gx-max")
NODE2_TENANTS = ("gx-reason", "gx-image", "gx-video", "gx-music", "gx-voice", "gx-call", "gx-live")
V3_ALIASES = tuple(V3_SERVICES)
PIN_ALIASES = tuple(a for a, p in POLICIES.items() if "pin" in p.controls)


def apply_measurements(aliases: dict) -> None:
    """Refresh the Build V3 policies from the registry's measured footprints."""
    for alias in V3_SERVICES:
        spec = aliases.get(alias) if isinstance(aliases, dict) else None
        fp = spec.get("measured_footprint") if isinstance(spec, dict) else None
        if POLICIES[alias].measured_ok != bool(fp) or fp:
            POLICIES[alias] = v3_policy(alias, fp)


class ResourceError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------- pure logic
def growth_gib(alias: str, *, resident: bool, variant: str | None = None,
               warm_growth: float | None = None) -> float:
    """How much MemAvailable starting `alias` takes away (measured)."""
    p = POLICIES[alias]
    if alias in ("gx-image", "gx-video"):
        cold = p.variants.get(variant or "", p.cold_gib)
        # warm: the router's measured growth; unknown -> the full footprint (the
        # router then frees its own weights and loads them again)
        return warm_growth if resident and warm_growth is not None else cold
    return p.cold_gib


def enforced_need(alias: str, *, resident: bool, variant: str | None = None,
                  warm_growth: float | None = None) -> float:
    """MemAvailable the enforcing component wants to see before it starts:
    the growth plus the locked 30 GiB reserve (D-038). gx-max has its own
    takeover policy."""
    if alias == "gx-max":
        return POLICIES[alias].cold_gib
    return growth_gib(alias, resident=resident, variant=variant, warm_growth=warm_growth) + RESERVE_GIB


def admission_view(alias: str, avail_gib: float | None, residents: dict[str, dict], *,
                   variant: str | None = None, pins: set[str] | None = None,
                   holds: dict[str, bool] | None = None, warm_growth: float | None = None,
                   reclaim_gib: float = 0.0) -> dict:
    """Would `alias` be admitted now, and if not, what would make room?

    `residents` maps alias -> {"active": bool, "pending_gib": float} for
    tenants currently holding memory on the same node; ``pending_gib`` is
    memory a load or render has been granted but not taken yet. The rule is
    the enforcing components' (D-038): MemAvailable - pending - growth must
    keep the 30 GiB reserve. ``reclaim_gib`` is memory the enforcing component
    frees by itself before the job (the media router drops its own resident
    weights when the job needs other ones). Pure: unit-tested, no I/O.
    """
    p = POLICIES[alias]
    pins = pins or set()
    holds = holds or {}
    out: dict[str, Any] = {"alias": alias, "node": p.node, "enforced_by": p.admission,
                           "reserve_gib": RESERVE_GIB if alias != "gx-max" else None}
    if holds.get("gxmax") and alias != "gx-max":
        return {**out, "allowed": False, "code": "gx_max_active", "reason": "gx-max owns the cluster; this "
                "starts again after gx-max is released", "blocking": ["gx-max"], "actions": []}
    if holds.get("maintenance"):
        return {**out, "allowed": False, "code": "maintenance", "reason": "Maintenance mode is on; new heavy "
                "work starts again when Maintenance ends", "blocking": [], "actions": []}
    resident = alias in residents
    if not p.measured_ok:
        return {**out, "allowed": None, "code": "unmeasured", "available_gib": avail_gib,
                "reason": f"{alias}'s memory footprint is not measured yet; its supervisor on gx10-02 admits a load "
                          "only while the 30 GiB reserve holds (other tenants' pending memory included)",
                "blocking": [], "actions": []}
    if resident and alias not in ("gx-image", "gx-video"):
        return {**out, "allowed": True, "code": "resident", "reason": f"{alias} is already loaded",
                "need_gib": 0.0, "available_gib": avail_gib, "blocking": [], "actions": []}
    growth = growth_gib(alias, resident=resident, variant=variant, warm_growth=warm_growth)
    need = enforced_need(alias, resident=resident, variant=variant, warm_growth=warm_growth)
    out.update(need_gib=round(need, 1), growth_gib=round(growth, 1),
               available_gib=None if avail_gib is None else round(avail_gib, 1))
    if alias in ("gx-image", "gx-video") and need > NODE2_MAX_AVAILABLE_GIB:
        what = "a keyframe video edit" if variant == "keyframe_edit" else alias
        return {**out, "allowed": False, "code": "exceeds_node", "terminal": True,
                "reason": f"{what} needs about {growth:.0f} GiB plus the {RESERVE_GIB:.0f} GiB reserve "
                          f"({need:.0f} GiB); gx10-02 never has more than about {NODE2_MAX_AVAILABLE_GIB:.0f} GiB "
                          "available, so it cannot run (B-028). Use a strength below 0.5 for a video edit.",
                "blocking": [], "actions": []}
    if avail_gib is None:
        return {**out, "allowed": False, "code": "unknown", "reason": f"{p.node} memory is not readable right now",
                "blocking": [], "actions": []}
    others = {a: r for a, r in residents.items() if a != alias and a in POLICIES}
    # memory another tenant has been granted but not taken yet (a load or a render in progress)
    pending = round(sum(float(r.get("pending_gib") or 0.0) for r in others.values()), 1)
    effective = avail_gib - pending + max(0.0, reclaim_gib)
    out["pending_gib"] = pending
    if reclaim_gib:
        out["reclaim_gib"] = round(reclaim_gib, 1)
    if effective >= need:
        return {**out, "allowed": True, "code": "fits",
                "reason": f"{avail_gib:.0f} GiB available"
                          + (f" ({pending:.0f} GiB still to be taken by a running load)" if pending else "")
                          + (f" (+{reclaim_gib:.0f} GiB the media router frees first)" if reclaim_gib else "")
                          + (f", {growth:.0f} GiB + {RESERVE_GIB:.0f} GiB reserve needed" if alias != "gx-max"
                             else f", {need:.0f} GiB needed (takeover policy)"),
                "blocking": [], "actions": []}
    short = need - effective
    # Smallest set of idle, unpinned, preemptible tenants that closes the gap.
    candidates = sorted(
        ((a, POLICIES[a].footprint_gib) for a, r in others.items()
         if POLICIES[a].preemptible and POLICIES[a].measured_ok and not r.get("active") and a not in pins),
        key=lambda x: -x[1])
    plan, freed = [], 0.0
    for a, gib in candidates:
        if freed >= short:
            break
        # image and video share one ComfyUI: freeing one frees the other too
        plan.append(a)
        freed += gib
    blocking = sorted(others, key=lambda a: -POLICIES[a].footprint_gib)
    held = ", ".join(f"{a} ({f'~{POLICIES[a].footprint_gib:.0f} GiB' if POLICIES[a].measured_ok else 'size not measured'}"
                     f"{', busy' if others[a].get('active') else ''}{', pinned' if a in pins else ''})"
                     for a in blocking)
    reason = (f"{alias} needs {growth:.0f} GiB plus the {RESERVE_GIB:.0f} GiB reserve"
              + (f" plus {pending:.0f} GiB another load has not taken yet" if pending else "")
              + f" on {_node_name(p.node)}, so {need + pending:.0f} GiB must be available; {avail_gib:.0f} GiB is now"
              + (f". Holding memory: {held}" if held else ""))
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
    unmeasured = [x for x, px in ((a, pa), (b, pb)) if not px.measured_ok]
    if unmeasured:
        why.append(f"{' and '.join(unmeasured)}: memory footprint not measured yet, so no verdict is computed.")
        why.append("The node-2 supervisors still admit a load only while the 30 GiB reserve holds, counting "
                   "every other tenant's pending memory.")
        return {"a": a, "b": b, "verdict": "unknown", "summary": "Not measured yet", "why": why}
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
                 node2_writer: Callable[[str, str], bool] | None = None, start_thread: bool = True,
                 services: dict | None = None, registry: Callable[[], dict] | None = None) -> None:
        self.cfg = cfg
        self.cluster = cluster
        self.actions = actions
        self.music = music
        self.media = media
        #: Build V3 node-2 supervisors (gx-voice, gx-call, gx-live): alias -> Node2Service
        self.services = services or {}
        #: tests/E2E point the service clients at stubs and switch this on
        self.probe_services = not cfg.offline
        self._registry = registry or (lambda: {})
        self.audit = audit or (lambda **kw: None)
        self._node2_writer = node2_writer or self._ssh_write
        self._lock = threading.RLock()
        self._reason_activity: dict[str, float] = {}
        self._keepalive_at = 0.0
        self._music_model: tuple[float, dict] = (0.0, {})
        #: tests/E2E point the music client at a stub and switch this on
        self.probe_music = not cfg.offline
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
        if self.music is None or not self.probe_music:
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
        try:
            apply_measurements((self._registry() or {}).get("aliases") or {})
        except (OSError, ValueError) as exc:
            log.warning("registry not readable for measured footprints: %s", exc)

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
        raw_media_mem = (media or {}).get("memory")
        media_mem: dict = raw_media_mem if isinstance(raw_media_mem, dict) else {}
        if media is not None:
            owner = "gx-video" if busy_holder.startswith("video") else "gx-image" if busy_holder.startswith("image") \
                else resident_alias
            if owner in runtimes:
                runtimes[owner]["pending_gib"] = float(media_mem.get("pending_gib") or 0.0)
            if resident_alias in runtimes:
                runtimes[resident_alias]["warm_growth_gib"] = media_mem.get("warm_growth_gib")
            router_waiting = media.get("waiting") or []
            if router_waiting and runtimes["gx-video"]["state"] in ("UNLOADED", "READY"):
                runtimes["gx-video"]["state"] = "WAITING"
                runtimes["gx-video"]["detail"] = str(router_waiting[0].get("reason") or "")

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
        raw_m_mem = engine.get("memory")
        m_mem: dict = raw_m_mem if isinstance(raw_m_mem, dict) else {}
        runtimes["gx-music"] = {"state": st, "detail": dt, "queue": queue.get("active", 0),
                                "last_load_seconds": engine.get("last_load_seconds"),
                                "idle_seconds": engine.get("idle_seconds"), "jobs": jobs,
                                "pending_gib": float(m_mem.get("pending_gib") or 0.0)
                                if "pending_gib" in m_mem else (32.0 if st == "LOADING" else 0.0),
                                "loaded_gib": m_mem.get("loaded_gib")}

        # Build V3 supervisors: their open /health (never started by a probe)
        from .node2_services import runtime_view
        for alias in V3_ALIASES:
            client = self.services.get(alias)
            health = client.health() if client is not None and self.probe_services else \
                {"reachable": False, "error": "not probed"}
            runtimes[alias] = runtime_view(alias, health, gx_busy=gx_busy or gxmax_hold, maint=maint)
            if client is not None and not client.configured:
                runtimes[alias]["detail"] = f"{alias} is not installed on this cluster yet (no service key)"

        gx_map = {"down": "UNLOADED", "acquiring": "LOADING", "ready": "READY", "releasing": "DRAINING"}
        runtimes["gx-max"] = {"state": gx_map.get(gx_state, "ERROR"), "detail": gx_state}
        orch_ok = (svc.get("orchestrator") or {}).get("ok")
        runtimes["gx-auto"] = {"state": "READY" if orch_ok else "ERROR",
                               "detail": "routes to mini / fast / reason" if orch_ok else "orchestrator unreachable"}

        for alias, r in runtimes.items():
            p = POLICIES[alias]
            r.update({"alias": alias, "node": p.node, "pinned": alias in pins, "pin": pins.get(alias),
                      "footprint_gib": p.footprint_gib if p.measured_ok else None, "measured_ok": p.measured_ok,
                      "residency": p.residency, "idle_ttl_s": p.idle_ttl_s,
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
                           "runtimes": ["gx-reason", "gx-image", "gx-video", "gx-music", "gx-voice", "gx-call",
                                        "gx-live", "gx-max"]}}
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
                      **{a.removeprefix("gx-"): runtimes[a].get("queue", 0) for a in V3_ALIASES},
                      "media_router_video": (media or {}).get("video_queue_depth", 0)},
            "media_router": {k: (media or {}).get(k) for k in ("version", "busy", "held_by", "resident_models",
                                                               "resident_alias", "idle_seconds",
                                                               "idle_free_seconds", "policy", "last_refusal",
                                                               "last_eviction", "waiting", "memory")},
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
        media_resident = (snap.get("media_router") or {}).get("resident_alias")
        for alias in NODE2_TENANTS if node == "node2" else ("gx-mini", "gx-fast"):
            r = snap["runtimes"].get(alias) or {}
            held = r.get("state") in ("READY", "GENERATING", "LOADING", "DRAINING") or bool(r.get("pending_gib"))
            if alias in ("gx-image", "gx-video"):
                # the tile may say WAITING while the weights are still loaded
                held = held or media_resident == alias
            if held:
                out[alias] = {"active": r.get("state") in ("GENERATING", "LOADING"),
                              "pending_gib": float(r.get("pending_gib") or 0.0)}
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
        residents = self.residents(snap, node)
        warm, reclaim = None, 0.0
        if alias in ("gx-image", "gx-video"):
            router = snap.get("media_router") or {}
            on_router = router.get("resident_alias")
            if on_router == alias and variant != "keyframe_edit":
                warm = snap["runtimes"].get(alias, {}).get("warm_growth_gib")
            if on_router in ("gx-image", "gx-video") and warm is None:
                # the router frees its own resident weights before this job
                held = (router.get("memory") or {}).get("resident_held_gib")
                reclaim = float(held) if isinstance(held, (int, float)) else POLICIES[on_router].footprint_gib
            residents = {a: r for a, r in residents.items()
                         if a not in ("gx-image", "gx-video") or (a == alias and warm is not None)}
        view = admission_view(alias, snap["nodes"][node]["mem_available_gib"], residents,
                              variant=variant, pins=set(snap["pins"]), holds=holds,
                              warm_growth=warm, reclaim_gib=reclaim)
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
                    "serialized": "Serialized", "exclusive": "Mutually exclusive",
                    "unknown": "Not measured yet"}}

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
        if view.get("terminal"):
            return {"code": view.get("code"), "terminal": True, "reason": view.get("reason"),
                    "detail": view.get("reason"), "need_gib": view.get("need_gib"),
                    "available_gib": view.get("available_gib"), "reserve_gib": RESERVE_GIB, "blocking": [],
                    "next": "not retried", "actions": []}
        blocking = view.get("blocking") or []
        profile = snap["profile"]["profile"]
        if blocking:
            first = blocking[0]
            nxt = "unloads automatically when it is idle"
            if first in snap["pins"]:
                nxt = f"{first} is pinned; unpin or unload it in Resource Control"
            elif profile == "text" and first == "gx-reason":
                nxt = "Text profile keeps gx-reason loaded; it unloads after 20 minutes idle"
            elif busy:
                nxt = f"{', '.join(busy)} is working; the scheduler waits for it"
            elif profile == "music" and first == "gx-music":
                nxt = "Music profile keeps gx-music loaded; it unloads after 10 minutes idle"
            if first in V3_ALIASES and snap["runtimes"][first].get("active_sessions"):
                nxt = f"{first} has a live session; it is never interrupted by the scheduler"
            if first in ("gx-music",) + V3_ALIASES:
                reason = f"Waiting for {first} to release enough gx10-02 memory"
            else:
                reason = f"Waiting for {first} to unload" if first != alias else "Waiting for enough gx10-02 memory"
        else:
            reason, nxt = f"Waiting for enough {_node_name(POLICIES[alias].node)} memory", "retries automatically"
        return {"code": view.get("code"), "reason": reason, "detail": view.get("reason"),
                "need_gib": view.get("need_gib"), "available_gib": view.get("available_gib"),
                "reserve_gib": RESERVE_GIB, "pending_gib": view.get("pending_gib"),
                "blocking": blocking, "next": nxt, "actions": view.get("actions", [])}

    # ============================================= creative scheduling hooks
    def creative_gate(self, alias: str, variant: str | None = None) -> dict | None:
        """None when a creative job may be submitted now; else a wait reason.
        May first free idle tenants, as the active profile allows."""
        snap = self.snapshot()
        view = self.admission(alias, variant=variant, snap=snap)
        if view.get("terminal"):
            return self.explain(alias, variant)
        if alias in ("gx-image", "gx-video") and snap["media_router"].get("busy"):
            # D-038: never submit behind a running job without a memory decision;
            # the memory that job is still taking is only known once it is done.
            return {"code": "engine_busy", "reason": "Waiting for the current media job to finish",
                    "detail": f"gx10-02 is running {snap['media_router'].get('held_by') or 'a media job'}",
                    "next": "the memory check runs right after it", "reserve_gib": RESERVE_GIB}
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
            if victim in V3_ALIASES:
                rt = snap["runtimes"].get(victim) or {}
                if rt.get("active_sessions") or rt.get("queue"):
                    continue  # a live call/session or queued voice work is never taken away
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
                info = self.music.lifecycle("unload", user="scheduler", if_idle=True)
            except Exception as exc:  # noqa: BLE001
                return False, str(exc)
            # verified release: the supervisor removed the container (its ledger
            # release is part of the same teardown); memory is re-read by the gate
            gone = isinstance(info, dict) and bool(info.get("container_gone"))
            return gone, json.dumps(info)[:200]
        if alias in V3_ALIASES and alias in self.services:
            from .node2_services import Node2Service, ServiceError
            try:
                info = self.services[alias].unload(if_idle=True, reason="scheduler")
            except ServiceError as exc:
                return False, str(exc)
            return Node2Service.unloaded(info), json.dumps(info)[:200]
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
        queued = {a: rt[a].get("queue", 0) for a in ("gx-image", "gx-video", "gx-music") + V3_ALIASES
                  if rt[a].get("queue")}
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
            may_drain = [a for a in ("gx-reason", "gx-music") + V3_ALIASES if a in loaded]
        elif target == "music":
            may_drain = [a for a in ("gx-image", "gx-video", "gx-reason") + V3_ALIASES if a in loaded]
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
        for alias in V3_ALIASES:
            if snap["runtimes"][alias]["state"] == "READY" and alias in self.services:
                ok, msg = self._unload(alias)
                steps.append(f"{alias} unloaded" if ok else f"{alias} left loaded (it unloads itself when idle): "
                             f"{msg[:120]}")
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
        if alias in V3_ALIASES:
            self._service(alias)  # refuses before anything else when it is not installed
        snap = self.snapshot()
        if op == "load":
            view = self.admission(alias, snap=snap)
            if alias == "gx-max":
                job = self.actions.submit("model.gx-max.load", user=user, ip=ip, confirm=confirm)
                return {"started": job.as_dict(with_output=False)}
            # an unmeasured supervisor decides itself (it keeps the reserve; plt.md section 5)
            if not view.get("allowed") and view.get("code") != "unmeasured":
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
            if alias in V3_ALIASES:
                client = self._service(alias)
                job = self._background(f"Load {alias}", user, client.load)
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
            if alias in V3_ALIASES:
                from .node2_services import ServiceError
                if snap["runtimes"][alias].get("active_sessions") and confirm is not True:
                    raise ResourceError(f"{alias} has a live session; confirm to end it and unload", 409)
                try:
                    info = self._service(alias).unload(if_idle=False, reason="manual")
                except ServiceError as exc:
                    raise ResourceError(str(exc), exc.status) from exc
                self.audit(user=user, ip=ip, action=f"resources.unload.{alias}", outcome="ok")
                return {"done": True, "detail": info}
        raise ResourceError("unsupported operation")

    def _service(self, alias: str):
        client = self.services.get(alias)
        if client is None or not client.configured:
            raise ResourceError(f"{alias} is not installed on this cluster yet", 503)
        return client

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

        job: dict[str, Any] = {"id": _secrets.token_hex(8), "label": label, "user": user, "state": "running",
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
