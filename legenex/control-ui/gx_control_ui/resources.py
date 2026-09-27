"""Resource Control (V4.1): the guard protocol, the gx-max admission story.

The D-037 guard file protocol is KEPT unchanged (the orchestrator and any
node-side component read these files):

    state/guard/profile.json            {"profile": "auto", "serving": "...", ...}
    state/guard/pins.json              {"gx-max": {...}}
    state/guard/node{1,2}.maintenance-hold                       Maintenance
    state/guard/node2.gxmax-hold        written by the gx-max drain

What changed for V4.1: there are no per-tier on-demand models any more. The
unit of control is the gx-max lifecycle (one DeepSeek V4.1 Flash EXL3 pack,
about 105 GiB per node, both nodes), driven ONLY through the orchestrator's
sanctioned lifecycle API. Profiles in profile.json are:

    auto          default; gx-max stays wherever its lifecycle is
    max           gx-max READY (acquired through the lifecycle on set)
    maintenance   no new heavy work; in-flight work finishes

The serving profile (fast / balanced / swarm / deep / long / custom) is a
registry concept chosen at acquire time; Resource Control records it in
profile.json as "serving" for observability but never invents one.

Numbers shown are the enforcing component's (orchestrator resource guard:
105 GiB per rank + the 30 GiB reserve), never container RSS.
"""

from __future__ import annotations

import json
import logging
import shlex
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .models import profiles as registry_profiles, read_registry
from .util import run, ssh_args

log = logging.getLogger("gx.ui.resources")

GIB = 2**30
RESERVE_GIB = 30.0
#: V4.1 sizing (ARCHITECTURE-V41.md section 3): gx-max rank0/1 ~ 105 GiB.
GXL_RANK_GIB = 105.0
GXL_NEED_GIB = 105.0        # the takeover policy per node (same number, named)
#: Idle capacity measured on both nodes (2026-09-17): ~113 GiB usable.
IDLE_CAPACITY_GIB = {"node1": 113.0, "node2": 113.0}

GUARD_PROFILES: dict[str, dict[str, Any]] = {
    "auto": {"label": "Auto", "summary": "Default. The cluster idles with gx-max down; start it "
             "explicitly with a serving profile when work begins.", "user_selectable": True},
    "max": {"label": "Max", "summary": "gx-max READY through the orchestrator lifecycle: both nodes "
            "serve DeepSeek V4.1 Flash with the recorded serving profile.", "user_selectable": True},
    "maintenance": {"label": "Maintenance", "summary": "No new heavy work. Running requests finish, "
                    "gx-max is not (re)started, and cleanup / installs are safe. Control planes keep "
                    "running.", "user_selectable": False},
}
PROFILE_IDS = tuple(GUARD_PROFILES)
PIN_ALIASES = ("gx-max",)          # the only pinnable workload: keep gx-max up


class ResourceError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class AdmissionBlocked(ResourceError):
    def __init__(self, view: dict) -> None:
        super().__init__(view.get("reason") or "not admissible now", 409)
        self.view = view


def _g(value: Any) -> float | None:
    return None if value in (None, "") else round(float(value) / GIB, 2)


class ResourceController:
    def __init__(self, cfg, cluster, actions, *, audit: Callable[..., None] | None = None,
                 node2_writer: Callable[[str, str], bool] | None = None) -> None:
        self.cfg = cfg
        self.cluster = cluster
        self.actions = actions
        self.audit = audit or (lambda **kw: None)
        self._node2_writer = node2_writer or self._ssh_write
        self._lock = threading.RLock()

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
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
        tmp.replace(path)

    def _ssh_write(self, name: str, content: str) -> bool:
        """Write/remove one file in node 2's guard directory (fixed name set)."""
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

    # ============================================================ protocol
    def profile(self) -> dict:
        data = self._read_json("profile.json", {})
        if not isinstance(data, dict) or data.get("profile") not in PROFILE_IDS:
            data = {"profile": "auto", "since": None, "by": None}
        return data

    def pins(self) -> dict[str, dict]:
        pins: dict[str, dict] = {}
        local = self._read_json("pins.json", {})
        if isinstance(local, dict):
            for alias, meta in local.items():
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
        n1 = self.cluster.node1.get() or {}
        n2 = self.cluster.node2.get() or {}
        gx_state = self.cluster.gxmax_state()
        profile = self.profile()
        pins = self.pins()
        holds = self.holds()
        gxmax_hold = bool((holds["node2"].get("gxmax") or {}).get("active"))
        maint = bool(holds["node1"].get("maintenance") or holds["node2"].get("maintenance"))
        sched = self.cluster.scheduler_snapshot(max_age=10)
        reg = read_registry(self.cfg.registry_path)
        runtimes = reg.get("runtimes") or {}
        serving = profile.get("serving") or ((self._lifecycle_body() or {}).get("profile"))
        return {
            "generated_at": time.time(),
            "profile": {**profile, **GUARD_PROFILES[profile["profile"]]},
            "profiles": [{"id": k, **v} for k, v in GUARD_PROFILES.items()],
            "serving_profiles": registry_profiles(reg),
            "serving": serving,
            "maintenance": maint,
            "gxmax": {"state": gx_state, "hold": gxmax_hold,
                      "sizing": {"rank_gib": GXL_RANK_GIB, "need_gib": GXL_NEED_GIB,
                                 "reserve_gib": RESERVE_GIB}},
            "runtime": next(iter(runtimes.values()), None),
            "nodes": {
                "node1": {**self._node_mem({**n1, "reachable": True}), "name": "gx10-01",
                          "holds": holds["node1"], "runtimes": ["gx-max rank0"]},
                "node2": {**self._node_mem(n2), "name": "gx10-02",
                          "holds": holds["node2"], "runtimes": ["gx-max rank1"]},
            },
            "pins": pins,
            "queue": {"available": sched.get("available"),
                      "queued": self._count(sched, "queued"),
                      "active": self._count(sched, "active")},
            "reserve_gib": RESERVE_GIB,
        }

    def _lifecycle_body(self) -> dict | None:
        lc = self.cluster.lifecycle.get() or {}
        st = lc.get("status") or {}
        body = st.get("body")
        return body if st.get("ok") and isinstance(body, dict) else None

    @staticmethod
    def _count(snap: dict, state: str) -> int | None:
        if not snap.get("available"):
            return None
        for key in ("queue", "requests", "records"):
            raw = snap.get(key)
            if isinstance(raw, list):
                return sum(1 for r in raw if isinstance(r, dict) and r.get("state") == state)
        return None

    # ========================================================== admission
    def admission(self, snap: dict | None = None) -> dict:
        """Would gx-max be admitted now? The orchestrator guard decides; this
        view EXPLAINS it (need vs available per node)."""
        snap = snap or self.snapshot()
        nodes = {}
        allowed = True
        for node in ("node1", "node2"):
            avail = snap["nodes"][node]["mem_available_gib"]
            need = GXL_NEED_GIB + RESERVE_GIB
            node_ok = avail is not None and avail >= need
            allowed = allowed and node_ok and snap["nodes"][node]["reachable"]
            nodes[node] = {"available_gib": avail, "need_gib": need,
                           "idle_capacity_gib": IDLE_CAPACITY_GIB[node], "fits": node_ok}
        state = snap["gxmax"]["state"]
        reason = ""
        if state == "ready":
            allowed, reason = False, "gx-max is already READY"
        elif state != "down":
            allowed, reason = False, f"gx-max lifecycle is {state}"
        elif snap["maintenance"]:
            allowed, reason = False, "Maintenance mode is on"
        elif not all(n["reachable"] for n in snap["nodes"].values()):
            reason = "a node is unreachable over its management plane"
        return {"alias": "gx-max", "allowed": allowed, "reason": reason,
                "enforced_by": "gx-orchestrator resource guard (105 GiB + 30 GiB reserve per node)",
                "nodes": nodes, "reserve_gib": RESERVE_GIB}

    # ============================================================ profiles
    def plan_profile(self, target: str, snap: dict | None = None) -> dict:
        if target not in PROFILE_IDS:
            raise ResourceError("unknown profile")
        snap = snap or self.snapshot()
        current = snap["profile"]["profile"]
        gx = snap["gxmax"]["state"]
        conflicts: list[str] = []
        if target == "max" and gx == "ready":
            conflicts.append("gx-max is already READY (its serving profile changes only via RESTART)")
        if target == "maintenance" and snap["queue"].get("active"):
            conflicts.append("active requests finish first; no new heavy work starts")
        if current == "max" and target != "max" and gx in ("ready", "acquiring"):
            conflicts.append("gx-max is released (graceful: in-flight requests finish first)")
        needs_confirm = target == "max" or (current == "max" and target != "max")
        return {"from": current, "to": target, "label": GUARD_PROFILES[target]["label"],
                "summary": GUARD_PROFILES[target]["summary"], "conflicts": conflicts,
                "needs_confirm": needs_confirm,
                "confirm_phrase": "gx-max" if needs_confirm else None}

    def set_profile(self, target: str, *, user: str, ip: str = "", confirm: Any = None,
                    source: str = "control-center") -> dict:
        plan = self.plan_profile(target)
        if plan["needs_confirm"]:
            expected = plan["confirm_phrase"] or True
            if confirm != expected:
                raise ResourceError("this profile change affects running work and must be confirmed"
                                    + (f" (type {plan['confirm_phrase']})" if plan["confirm_phrase"]
                                       else ""), 409)
        current = plan["from"]
        steps: list[str] = []
        with self._lock:
            if target == "max" and self.cluster.gxmax_state() == "down":
                job = self.actions.submit("gxmax_start", user=user, ip=ip, confirm="gx-max")
                steps.append(f"gx-max acquire started (job {job.id})")
            if current == "max" and target != "max" and self.cluster.gxmax_state() == "ready":
                job = self.actions.submit("gxmax_stop", user=user, ip=ip, confirm=True)
                steps.append(f"gx-max release started (job {job.id})")
            record = {"profile": target, "since": time.time(), "by": user, "previous": current,
                      "source": source, "serving": self.profile().get("serving")}
            self._write_local("profile.json", record)
            if not self._node2_writer("profile.json", json.dumps(record, sort_keys=True)):
                steps.append("WARNING: gx10-02 did not receive the profile file")
            if target == "maintenance":
                steps += self.enter_maintenance(user=user)
            elif current == "maintenance" or self.maintenance():
                steps += self.exit_maintenance(user=user)
        self.audit(user=user, ip=ip, action="resources.profile", outcome="ok", previous=current,
                   profile=target, source=source)
        self.cluster.invalidate()
        return {"profile": target, "steps": steps, "plan": plan}

    def enter_maintenance(self, *, user: str) -> list[str]:
        self._write_local("node1.maintenance-hold", time.strftime("%Y-%m-%dT%H:%M:%S%z") + f" {user}\n")
        ok = self._node2_writer("node2.maintenance-hold", "1")
        return ["gx10-01 maintenance hold set",
                "gx10-02 maintenance hold set" if ok
                else "WARNING: could not set the gx10-02 maintenance hold"]

    def exit_maintenance(self, *, user: str) -> list[str]:
        self._write_local("node1.maintenance-hold", None)
        ok = self._node2_writer("node2.maintenance-hold", "")
        return ["gx10-01 maintenance hold removed",
                "gx10-02 maintenance hold removed" if ok
                else "WARNING: could not remove the gx10-02 hold"]

    # ============================================================== pins
    def set_pin(self, alias: str, on: bool, *, user: str, ip: str = "") -> dict:
        if alias not in PIN_ALIASES:
            raise ResourceError(f"{alias} cannot be pinned")
        pins = self.pins()
        if on:
            pins[alias] = {"by": user, "since": time.time()}
        else:
            pins.pop(alias, None)
        content = json.dumps(pins, sort_keys=True)
        self._write_local("pins.json", pins)
        ok = self._node2_writer("pins.json", content)
        if not ok:
            raise ResourceError("gx10-02 did not accept the pin change", 503)
        self.audit(user=user, ip=ip, action=f"resources.{'pin' if on else 'unpin'}", outcome="ok",
                   alias=alias)
        self.cluster.node2.invalidate()
        return {"alias": alias, "pinned": on}
