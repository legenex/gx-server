"""Node-level admission control and resource ownership for gx-cluster.

Born from the incident of 2026-09-14 (see coordination/BLOCKERS.md B-012):
node 2 was wedged when a worker started a second ~77 GB llama.cpp container
(a CPU-only diagnostic) while gx-reason (also ~77-95 GiB, mmap'd) was still
resident on a 121 GiB node. The worker bypassed llama-swap's own model-group
exclusivity by invoking `docker run` directly instead of going through
lifecycle tooling. Because mmap pages are reclaimable, the OOM killer never
fired -- the node thrashed indefinitely instead (userspace unresponsive,
kernel/ICMP still alive).

This module is the ONE place that knows:

  * what workload classes exist and how big they measure
    (`WORKLOAD_SIZING`, sourced from MODELS.md / CURRENT_STATE.md /
    BLOCKERS.md B-011's measured VmRSS);
  * how much memory a node may commit before a launch must be REFUSED, not
    just warned about (`compute_admission`, `check_admission`);
  * which workload currently owns a node's large/exclusive slot
    (`ResidencyLedger`, reconciled against `docker inspect` on every read so
    a dead process/missing container/rebooted node never leaves a
    permanently-stuck record);
  * how to serialise concurrent launch attempts across BOTH Python threads
    *and* separate OS processes/shell scripts (`NodeLock`, backed by
    flock(2) on a plain file -- the same kernel primitive a bash `flock`
    invocation on the same path contends for).

Design goal -- make the safe path structurally hard to bypass. Nothing can
stop a human or an agent from typing `docker run` directly. What this module
provides is that every SANCTIONED lifecycle entry point --
`legenex/lifecycle/gx-max-start.sh`, `legenex/lifecycle/gx-safe-run.sh` (the
wrapper an operator/agent should reach for instead of a bare `docker run` for
any ad hoc/diagnostic large container), and any Python caller in
`gx_orchestrator` -- all resolve to the SAME lock file and the SAME
admission arithmetic in this one module. There is exactly one sanctioned
code path, and it is documented as "the way" in
`legenex/lifecycle/resource-guard.sh`.

Stdlib-only, consistent with D-003: this sits on the recovery path and must
not depend on pip, a venv, a wheel build, or a container registry.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import enum
import fcntl
import json
import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Iterator

log = logging.getLogger("gx.guard")

# ---------------------------------------------------------------------------
# Workload classes and the measured sizing table
# ---------------------------------------------------------------------------


class WorkloadClass(str, enum.Enum):
    """How much of a node's budget a workload is allowed to assume it owns.

    SMALL / MEDIUM workloads are allowed to coexist (llama-swap's own
    persistent/non-exclusive groups already enforce that for gx-mini
    alongside a heavy model). LARGE and EXCLUSIVE workloads may never share
    a node with another LARGE or EXCLUSIVE workload -- that is exactly the
    rule the 2026-09-14 incident violated.
    """

    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"
    EXCLUSIVE = "exclusive"


#: Classes that must never share a node with another instance of themselves,
#: or with each other.
NODE_EXCLUSIVE_CLASSES: tuple[WorkloadClass, ...] = (WorkloadClass.LARGE, WorkloadClass.EXCLUSIVE)


@dataclasses.dataclass(frozen=True)
class WorkloadSpec:
    name: str
    node: str  # "node1" | "node2" | "both"
    workload_class: WorkloadClass
    estimated_gib: float
    notes: str = ""


#: Measured/documented footprints. Sources: MODELS.md, CURRENT_STATE.md,
#: coordination/BLOCKERS.md B-011 (measured VmRSS during the gx-reason
#: investigation) and legenex/gateway/README.md's memory-budget section.
#: This is the SINGLE table both the Python orchestrator and every bash
#: lifecycle script consult -- nobody re-derives these numbers by hand, and a
#: caller launching something not in this table must pass an explicit
#: `--estimated-gib`, never a guess baked into the guard.
WORKLOAD_SIZING: dict[str, WorkloadSpec] = {
    "gx-mini": WorkloadSpec(
        "gx-mini", "node1", WorkloadClass.SMALL, 10.0,
        "Qwen3.5-4B Q4_K_M + BF16 mmproj, llama.cpp. Always-hot resident tier.",
    ),
    "gx-fast": WorkloadSpec(
        "gx-fast", "node1", WorkloadClass.MEDIUM, 25.0,
        "~30-40B MoE on vLLM, --gpu-memory-utilization 0.66 of a ~121GiB node. "
        "Up to two instances have been discussed but the heavy group is "
        "swap:true (exclusive-one-at-a-time) today.",
    ),
    "gx-reason": WorkloadSpec(
        "gx-reason", "node2", WorkloadClass.LARGE, 45.0,
        "nvidia/Qwen3.6-27B-NVFP4 on vLLM, replacing the broken "
        "Qwen3.5-122B-A10B GGUF/llama.cpp combination (B-011, isolated to a "
        "llama.cpp CUDA/GDN kernel bug for this hybrid architecture, not the "
        "checkpoint). 20.42 GiB of weights + a 0.35 vLLM pool. 45 GiB is "
        "CONFIRMED BY MEASUREMENT 2026-09-16, no longer just a ceiling: "
        "loading it moved node2 from 114 -> 70 GiB MemAvailable, i.e. a real "
        "~44 GiB node-level footprint. Note this figure comes from "
        "/proc/meminfo, which is what compute_admission() reads -- the "
        "container's own memory cgroup reports only ~11 GiB because the CUDA "
        "pool is not charged to it on this hardware (B-021), so never size "
        "this from `docker stats`. Still owns node2 exclusively (D-007) -- "
        "never co-scheduled with ComfyUI or any other large/exclusive "
        "workload.",
    ),
    "gx-max-rank0": WorkloadSpec(
        "gx-max-rank0", "node1", WorkloadClass.EXCLUSIVE, 105.0,
        "SGLang TP=2 rank0, DeepSeek V4 Flash NVFP4. Takes over node1. "
        "This is the STEADY-STATE residency the ledger records once serving "
        "(--mem-fraction-static 0.80 x 121.63 GiB = 97.3 GiB static pool plus "
        "process/driver overhead). It is NOT admitted with the ordinary "
        "estimate+reserve formula: gx-max uses compute_takeover_admission() "
        "(D-025). The ~117 GiB load-phase peak is a transient absorbed by "
        "/swapfile-sglang (the 2026-09-14 verified run went to 63/63 GB swap "
        "and recovered) and is policed live by gx-max-safety.sh, not by "
        "admission arithmetic. History: 90 -> 95 -> 117 (peak, which made "
        "gx-max mathematically unlaunchable, B-022) -> 105 (steady, D-025).",
    ),
    "gx-max-rank1": WorkloadSpec(
        "gx-max-rank1", "node2", WorkloadClass.EXCLUSIVE, 105.0,
        "SGLang TP=2 rank1. Takes over node2. See gx-max-rank0's note. rank1 "
        "carries a node-local watchdog (legenex/lifecycle/rank1-deadman.sh) "
        "that applies the same gx-max-safety.sh rules on node 2 and removes "
        "rank1 if rank0 disappears (B-020).",
    ),
    "comfyui": WorkloadSpec(
        "comfyui", "node2", WorkloadClass.MEDIUM, 44.0,
        "ComfyUI media pipelines (gx-image/gx-video). Budgeted <=22-44GiB "
        "resident per node02.yaml/D-007; must not run at LARGE size while "
        "gx-reason is resident.",
    ),
}

#: Whole-node ceiling. 128 GB advertised, ~121 GiB actually usable (measured
#: via /proc/meminfo MemTotal on gx10-01: 127535340 kB). Overridable for
#: tests and for the CLI via --node-total-gib.
DEFAULT_NODE_TOTAL_GIB = 121.0

#: Minimum memory that must remain available after a launch, per the human's
#: explicit requirement of 2026-09-14: never budget a node down to the wire.
#: This is a REFUSAL threshold, not a warning.
DEFAULT_RESERVE_GIB = 30.0


# ---------------------------------------------------------------------------
# Live measurement
# ---------------------------------------------------------------------------


def read_mem_available_gib(meminfo_path: "str | os.PathLike[str]" = "/proc/meminfo") -> float:
    """Real, current MemAvailable in GiB -- never a cached or assumed value.

    MemAvailable (not MemFree) is the kernel's own estimate of what is
    actually reclaimable without swapping, and is what the human's
    requirement names explicitly: "compute actual MemAvailable (from
    /proc/meminfo, not just 'free')".
    """
    try:
        with open(meminfo_path, "r", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    kib = int(line.split()[1])
                    return kib / (1024.0 * 1024.0)
    except (OSError, ValueError, IndexError) as exc:
        raise RuntimeError(f"could not read MemAvailable from {meminfo_path}: {exc}") from exc
    raise RuntimeError(f"{meminfo_path} has no MemAvailable line")


def _docker_is_running(container: str) -> bool:
    """True iff `container` exists and is currently running."""
    try:
        out = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", container],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return out.returncode == 0 and out.stdout.strip() == "true"
    except (OSError, subprocess.SubprocessError):
        return False


# ---------------------------------------------------------------------------
# Admission arithmetic
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class AdmissionResult:
    allowed: bool
    reason: str
    node: str
    numbers: dict

    def as_log_dict(self) -> dict:
        return {"allowed": self.allowed, "reason": self.reason, "node": self.node, **self.numbers}


def compute_admission(
    node: str,
    estimated_gib: float,
    *,
    current_residency_gib: float,
    mem_available_gib: float,
    reserve_gib: float = DEFAULT_RESERVE_GIB,
    node_total_gib: float = DEFAULT_NODE_TOTAL_GIB,
) -> AdmissionResult:
    """The hard safety guard. Two INDEPENDENT checks must both pass.

    1. INTENT-based: ledger residency + the new workload + the reserve must
       fit inside the node's total. Catches memory that has not been
       reclaimed *yet* (a model mid-unload, pages still being freed).
    2. MEASUREMENT-based: actual MemAvailable right now, minus the new
       workload, must still leave at least the reserve. Catches anything the
       ledger does not know about -- a diagnostic container started outside
       this module, another process on the host, page-cache pressure --
       which is exactly the failure mode of the 2026-09-14 incident (B-012).

    Refusing outright on either failure -- never merely logging a warning
    and proceeding -- is a deliberate, human-mandated requirement.
    """
    projected_total = current_residency_gib + estimated_gib + reserve_gib
    intent_ok = projected_total <= node_total_gib

    projected_available_after = mem_available_gib - estimated_gib
    measured_ok = projected_available_after >= reserve_gib

    numbers = {
        "estimated_gib": round(estimated_gib, 1),
        "current_residency_gib": round(current_residency_gib, 1),
        "reserve_gib": round(reserve_gib, 1),
        "node_total_gib": round(node_total_gib, 1),
        "projected_total_gib": round(projected_total, 1),
        "mem_available_gib": round(mem_available_gib, 1),
        "projected_available_after_gib": round(projected_available_after, 1),
    }

    if not intent_ok:
        return AdmissionResult(
            False,
            f"refused: ledger residency {current_residency_gib:.1f}GiB + new "
            f"{estimated_gib:.1f}GiB + reserve {reserve_gib:.1f}GiB = "
            f"{projected_total:.1f}GiB exceeds node total {node_total_gib:.1f}GiB",
            node,
            numbers,
        )
    if not measured_ok:
        return AdmissionResult(
            False,
            f"refused: live MemAvailable {mem_available_gib:.1f}GiB - new "
            f"{estimated_gib:.1f}GiB leaves {projected_available_after:.1f}GiB, "
            f"below the {reserve_gib:.1f}GiB reserve floor",
            node,
            numbers,
        )
    return AdmissionResult(
        True,
        f"admitted: {projected_total:.1f}GiB projected of {node_total_gib:.1f}GiB "
        f"node total; MemAvailable leaves {projected_available_after:.1f}GiB "
        f"(reserve floor {reserve_gib:.1f}GiB)",
        node,
        numbers,
    )


# ---------------------------------------------------------------------------
# gx-max cluster-takeover admission (D-025)
# ---------------------------------------------------------------------------
#
# gx-max is not "one more workload on a node". It is an exclusive takeover of
# BOTH nodes whose memory has three distinct phases, and the ordinary
# `estimated + reserve <= node` formula above conflates them:
#
#   1. PRE-LAUNCH CLEAN STATE  -- what must be true before rank1 starts.
#   2. STARTUP TRANSIENT       -- ~117 GiB per node while weights stage;
#                                 absorbed by /swapfile-sglang and released.
#                                 Monitored live (gx-max-safety.sh), NOT
#                                 admitted against: 117 + 30 > 121.63 always.
#   3. STEADY-STATE RESIDENCY  -- what the ledger records once serving.
#
# The 30 GiB reserve still applies to every ordinary single-node tier via
# compute_admission(); only this takeover path uses the policy below.


@dataclasses.dataclass(frozen=True)
class TakeoverPolicy:
    clean_start_min_avail_gib: float = 100.0
    min_swap_free_gib: float = 40.0
    max_psi_full_avg10: float = 5.0
    require_swapfile: bool = True
    #: Documentation only: the measured load envelope. Never an admission term.
    startup_transient_gib: float = 117.0


@dataclasses.dataclass(frozen=True)
class NodeFacts:
    """A clean-start snapshot of one node (gx-max-safety.sh gxs_clean_start_facts)."""

    avail_mib: int
    swap_free_mib: int
    swap_total_mib: int
    swapfile_active: bool
    psi_full10: float

    @classmethod
    def parse(cls, line: str) -> "NodeFacts":
        kv = dict(part.split("=", 1) for part in line.split() if "=" in part)
        try:
            return cls(
                avail_mib=int(kv["avail_mib"]),
                swap_free_mib=int(kv["swap_free_mib"]),
                swap_total_mib=int(kv["swap_total_mib"]),
                swapfile_active=kv["swapfile_active"] == "1",
                psi_full10=float(kv["psi_full10"]),
            )
        except (KeyError, ValueError) as exc:
            raise ValueError(f"malformed node facts {line!r}: {exc}") from exc


def read_node_facts(
    swapfile: str = "/swapfile-sglang",
    *,
    meminfo_path: "str | os.PathLike[str]" = "/proc/meminfo",
    swaps_path: "str | os.PathLike[str]" = "/proc/swaps",
    psi_path: "str | os.PathLike[str]" = "/proc/pressure/memory",
) -> NodeFacts:
    """Python twin of gx-max-safety.sh's gxs_clean_start_facts, for this host."""
    mem = {}
    with open(meminfo_path, encoding="ascii") as fh:
        for line in fh:
            key, _, rest = line.partition(":")
            if key in ("MemAvailable", "SwapFree", "SwapTotal"):
                mem[key] = int(rest.split()[0]) // 1024
    active = False
    try:
        with open(swaps_path, encoding="ascii") as fh:
            active = any(line.split()[:1] == [swapfile] for line in list(fh)[1:])
    except OSError:
        pass
    psi_full = 0.0
    try:
        with open(psi_path, encoding="ascii") as fh:
            for line in fh:
                if line.startswith("full"):
                    psi_full = float(line.split()[1].split("=")[1])
    except (OSError, IndexError, ValueError):
        pass
    return NodeFacts(mem["MemAvailable"], mem["SwapFree"], mem["SwapTotal"], active, psi_full)


def compute_takeover_admission(
    node: str,
    facts: NodeFacts,
    *,
    other_exclusive_residents: "list[str]",
    policy: TakeoverPolicy = TakeoverPolicy(),
) -> AdmissionResult:
    """Pure decision: may gx-max take over `node` right now?

    Every refusal names the one condition that failed, so an operator never
    has to guess which half of a two-node launch said no.
    """
    avail_gib = facts.avail_mib / 1024.0
    swap_free_gib = facts.swap_free_mib / 1024.0
    numbers = {
        "policy": "gx-max-takeover",
        "mem_available_gib": round(avail_gib, 1),
        "clean_start_min_avail_gib": policy.clean_start_min_avail_gib,
        "swap_free_gib": round(swap_free_gib, 1),
        "min_swap_free_gib": policy.min_swap_free_gib,
        "swapfile_active": facts.swapfile_active,
        "psi_full_avg10": facts.psi_full10,
        "startup_transient_gib": policy.startup_transient_gib,
    }
    checks = [
        (not other_exclusive_residents,
         f"refused: {node} still hosts large/exclusive resident(s) {other_exclusive_residents}; drain first"),
        (facts.swapfile_active or not policy.require_swapfile,
         f"refused: /swapfile-sglang is not active on {node} (L-8); the load transient needs it"),
        (swap_free_gib >= policy.min_swap_free_gib,
         f"refused: {node} swap free {swap_free_gib:.1f}GiB < {policy.min_swap_free_gib:.1f}GiB needed to absorb the load transient"),
        (avail_gib >= policy.clean_start_min_avail_gib,
         f"refused: {node} MemAvailable {avail_gib:.1f}GiB < clean-start minimum {policy.clean_start_min_avail_gib:.1f}GiB; something is still resident"),
        (facts.psi_full10 <= policy.max_psi_full_avg10,
         f"refused: {node} is already under memory pressure (PSI full avg10 {facts.psi_full10} > {policy.max_psi_full_avg10})"),
    ]
    for ok, reason in checks:
        if not ok:
            return AdmissionResult(False, reason, node, numbers)
    return AdmissionResult(
        True,
        f"admitted: {node} clean ({avail_gib:.1f}GiB available, {swap_free_gib:.1f}GiB swap free, swapfile active)",
        node,
        numbers,
    )


def check_takeover_admission(
    node: str,
    name: str,
    facts: NodeFacts,
    *,
    state_dir: Path,
    policy: TakeoverPolicy = TakeoverPolicy(),
    is_running: "Callable[[str], bool] | None" = None,
) -> AdmissionResult:
    """Reconcile the ledger, then apply the takeover policy. No locking."""
    ledger = ResidencyLedger(Path(state_dir) / f"{node}-residency.json", is_running=is_running)
    others = ledger.exclusive_residents(node, exclude=name)
    result = compute_takeover_admission(node, facts, other_exclusive_residents=others, policy=policy)
    level = log.info if result.allowed else log.warning
    level("gx.guard: %s", json.dumps({"name": name, **result.as_log_dict()}))
    return result


# ---------------------------------------------------------------------------
# Residency ledger
# ---------------------------------------------------------------------------


class ResidencyLedger:
    """Durable record of what this module believes is resident on a node.

    Persisted as JSON so it survives orchestrator restarts and is visible to
    bash callers (`resource-guard.sh status`). Reconciled against reality on
    every read: an entry whose container `is_running` says is gone is
    dropped automatically. That IS the stale-state recovery for "process
    died / container missing / node rebooted / previous operation
    interrupted" -- there is no separate stale-lock cleanup step, because
    nothing here ever blocks on a stale ledger entry; it just gets
    reconciled away the next time anyone asks.
    """

    def __init__(self, path: Path, *, is_running: "Callable[[str], bool] | None" = None) -> None:
        self.path = Path(path)
        self._is_running = is_running or _docker_is_running

    def _load_raw(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError):
            log.warning("gx.guard: ledger at %s is unreadable/corrupt; treating as empty", self.path)
            return {}

    def _save_raw(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
        tmp.replace(self.path)  # atomic rename on the same filesystem

    def reconcile(self) -> dict:
        """Drop entries whose container is no longer running.

        Returns the reconciled {name: entry} mapping; also persists it if it
        changed.
        """
        data = self._load_raw()
        alive = {}
        for name, entry in data.items():
            container = entry.get("container", name)
            if self._is_running(container):
                alive[name] = entry
            else:
                log.info(
                    "gx.guard: reconcile dropping stale resident %s (container %s not running)",
                    name,
                    container,
                )
        if alive != data:
            self._save_raw(alive)
        return alive

    def total_gib(self, node: str, *, exclude: str = "") -> float:
        alive = self.reconcile()
        return sum(
            e["estimated_gib"] for n, e in alive.items() if e.get("node") == node and n != exclude
        )

    def exclusive_residents(self, node: str, *, exclude: str = "") -> list[str]:
        alive = self.reconcile()
        return [
            n
            for n, e in alive.items()
            if e.get("node") == node
            and n != exclude
            and e.get("class") in (WorkloadClass.LARGE.value, WorkloadClass.EXCLUSIVE.value)
        ]

    def add(
        self,
        name: str,
        *,
        node: str,
        workload_class: WorkloadClass,
        estimated_gib: float,
        container: "str | None" = None,
    ) -> None:
        data = self._load_raw()
        data[name] = {
            "node": node,
            "class": workload_class.value,
            "estimated_gib": estimated_gib,
            "container": container or name,
            "granted_at": time.time(),
            "granted_by_pid": os.getpid(),
            "granted_by_host": socket.gethostname(),
        }
        self._save_raw(data)
        log.info(
            "gx.guard: ledger += %s (class=%s estimated_gib=%.1f node=%s)",
            name,
            workload_class.value,
            estimated_gib,
            node,
        )

    def remove(self, name: str) -> None:
        data = self._load_raw()
        if name in data:
            del data[name]
            self._save_raw(data)
            log.info("gx.guard: ledger -= %s", name)


# ---------------------------------------------------------------------------
# check_admission -- the single arithmetic entry point (no locking)
# ---------------------------------------------------------------------------


def check_admission(
    node: str,
    name: str,
    workload_class: WorkloadClass,
    estimated_gib: float,
    *,
    state_dir: Path,
    reserve_gib: float = DEFAULT_RESERVE_GIB,
    node_total_gib: float = DEFAULT_NODE_TOTAL_GIB,
    meminfo_path: "str | os.PathLike[str]" = "/proc/meminfo",
    mem_available_gib: "float | None" = None,
    is_running: "Callable[[str], bool] | None" = None,
) -> AdmissionResult:
    """Reconcile the ledger and decide whether `name` may launch on `node`.

    Deliberately does NOT take or need a lock -- callers that need mutual
    exclusion across processes acquire `NodeLock`/`flock` on the same
    ``<state_dir>/<node>.lock`` path themselves (Python via `guard_launch`,
    bash via `resource-guard.sh`'s `gx_guard_run`) and call this function
    while holding it. Keeping the arithmetic lock-free means both callers
    share the exact same decision logic with nothing duplicated.
    """
    ledger = ResidencyLedger(Path(state_dir) / f"{node}-residency.json", is_running=is_running)
    alive = ledger.reconcile()
    current = sum(e["estimated_gib"] for n, e in alive.items() if e.get("node") == node and n != name)

    if workload_class in NODE_EXCLUSIVE_CLASSES:
        conflicting = [
            n
            for n, e in alive.items()
            if e.get("node") == node
            and n != name
            and e.get("class") in (WorkloadClass.LARGE.value, WorkloadClass.EXCLUSIVE.value)
        ]
        if conflicting:
            result = AdmissionResult(
                False,
                f"refused: {node} already hosts large/exclusive resident(s) "
                f"{conflicting} -- never co-schedule two large/exclusive workloads "
                f"on one node (see D-007 / BLOCKERS.md B-012)",
                node,
                {"current_residency_gib": round(current, 1)},
            )
            log.warning("gx.guard: %s", json.dumps({"name": name, "class": workload_class.value, **result.as_log_dict()}))
            return result

    avail = mem_available_gib if mem_available_gib is not None else read_mem_available_gib(meminfo_path)
    result = compute_admission(
        node,
        estimated_gib,
        current_residency_gib=current,
        mem_available_gib=avail,
        reserve_gib=reserve_gib,
        node_total_gib=node_total_gib,
    )
    level = log.info if result.allowed else log.warning
    level("gx.guard: %s", json.dumps({"name": name, "class": workload_class.value, **result.as_log_dict()}))
    return result


# ---------------------------------------------------------------------------
# NodeLock -- one authoritative workload owner per node at a time
# ---------------------------------------------------------------------------


class NodeLockBusy(RuntimeError):
    """Another process/thread currently holds the node lock."""


class NodeLock:
    """One authoritative workload owner per node at a time.

    Backed by flock(2) on a plain file. This is deliberately NOT an
    in-process ``threading.Lock``: `gx-max-start.sh`, `gx-safe-run.sh`, and
    any future bash lifecycle script all flock the SAME path, so a shell
    script invoked directly -- bypassing this Python process entirely --
    still contends for the same kernel-level lock the orchestrator uses.
    flock() is scoped to the inode, so a bash ``flock`` process and a Python
    ``fcntl.flock`` call on the same path are contending for the identical
    lock as far as the kernel is concerned; this is what makes the primitive
    safe across languages and processes, not just across threads.

    Stale-lock recovery is automatic and requires no extra code: flock is
    released by the kernel the instant the holding process exits or is
    killed -- even by SIGKILL, even if it never runs an `unlock`/`finally`
    block. There is no "lock file left behind after a crash" failure mode
    for flock, unlike a naive pidfile scheme.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextlib.contextmanager
    def acquire(self, *, timeout: "float | None" = 30.0, workload: str = "", poll_interval: float = 0.2) -> Iterator[None]:
        """Block up to `timeout` seconds (None = forever) for exclusive
        ownership of this node, then yield. Raises NodeLockBusy on timeout.
        """
        fh = open(self.path, "a+")
        deadline = None if timeout is None else time.monotonic() + timeout
        acquired = False
        try:
            while True:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except BlockingIOError:
                    if deadline is not None and time.monotonic() >= deadline:
                        held_by = self._peek_holder(fh)
                        raise NodeLockBusy(
                            f"node lock {self.path} busy"
                            + (f" (held by: {held_by})" if held_by else "")
                        )
                    time.sleep(poll_interval)
            fh.seek(0)
            fh.truncate()
            fh.write(json.dumps({"pid": os.getpid(), "host": socket.gethostname(), "workload": workload, "since": time.time()}))
            fh.flush()
            log.info("gx.guard: node lock %s acquired (workload=%s pid=%s)", self.path, workload, os.getpid())
            yield
        finally:
            if acquired:
                log.info("gx.guard: node lock %s released (workload=%s pid=%s)", self.path, workload, os.getpid())
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            fh.close()

    @staticmethod
    def _peek_holder(fh) -> str:
        try:
            fh.seek(0)
            return fh.read().strip()
        except OSError:
            return ""


# ---------------------------------------------------------------------------
# guard_launch -- THE sanctioned Python entry point
# ---------------------------------------------------------------------------


class AdmissionRefused(RuntimeError):
    """A launch was refused by the resource guard.

    Callers MUST let this propagate as a hard failure. Catching it to fall
    back to launching anyway defeats the entire point of this module.
    """

    def __init__(self, result: AdmissionResult) -> None:
        super().__init__(result.reason)
        self.result = result


@dataclasses.dataclass(frozen=True)
class GuardContext:
    node: str
    name: str
    workload_class: WorkloadClass
    estimated_gib: float
    admission: AdmissionResult


@contextlib.contextmanager
def guard_launch(
    node: str,
    name: str,
    workload_class: WorkloadClass,
    estimated_gib: float,
    *,
    state_dir: Path,
    reserve_gib: float = DEFAULT_RESERVE_GIB,
    node_total_gib: float = DEFAULT_NODE_TOTAL_GIB,
    meminfo_path: "str | os.PathLike[str]" = "/proc/meminfo",
    mem_available_gib: "float | None" = None,
    lock_timeout: "float | None" = 30.0,
    is_running: "Callable[[str], bool] | None" = None,
) -> Iterator[tuple[GuardContext, ResidencyLedger]]:
    """THE sanctioned Python entry point for any medium/large/exclusive launch.

    Acquires the node lock, reconciles the ledger, computes admission and --
    if refused -- raises `AdmissionRefused` WITHOUT running the caller's
    body at all. On success the caller's body runs while the lock is still
    held; the caller is expected to start its container and then call
    ``ledger.add(...)`` (the ledger is handed back for exactly that) before
    the `with` block ends, so residency is recorded before the lock
    releases.

    Never-bypass property: the caller's launch code is only reachable from
    inside this function, after `check_admission` has already said yes.
    There is no code path that reaches the caller's body with a "no".
    """
    lock = NodeLock(Path(state_dir) / f"{node}.lock")
    log.info(
        "gx.guard: request node=%s name=%s class=%s estimated_gib=%.1f",
        node,
        name,
        workload_class.value,
        estimated_gib,
    )
    with lock.acquire(timeout=lock_timeout, workload=name):
        result = check_admission(
            node,
            name,
            workload_class,
            estimated_gib,
            state_dir=state_dir,
            reserve_gib=reserve_gib,
            node_total_gib=node_total_gib,
            meminfo_path=meminfo_path,
            mem_available_gib=mem_available_gib,
            is_running=is_running,
        )
        if not result.allowed:
            raise AdmissionRefused(result)
        ledger = ResidencyLedger(Path(state_dir) / f"{node}-residency.json", is_running=is_running)
        yield GuardContext(node, name, workload_class, estimated_gib, result), ledger


def release_workload(state_dir: Path, node: str, name: str) -> None:
    """Remove `name` from the node's residency ledger (e.g. after a clean
    stop/drain). Reconciliation would eventually do this on its own once the
    container disappears, but an explicit release keeps the ledger accurate
    immediately and makes the intent visible in the logs.
    """
    ResidencyLedger(Path(state_dir) / f"{node}-residency.json").remove(name)


# ---------------------------------------------------------------------------
# CLI -- what bash calls, so the arithmetic lives in exactly one place
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python3 -m gx_orchestrator.resource_guard")
    p.add_argument("--state-dir", required=True, help="Directory holding <node>.lock / <node>-residency.json")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--node", required=True)
        sp.add_argument("--name", required=True)

    check = sub.add_parser(
        "check",
        help="Pure admission check (no locking -- caller must hold the node lock, e.g. via flock, itself)",
    )
    common(check)
    check.add_argument("--class", dest="workload_class", required=True, choices=[c.value for c in WorkloadClass])
    check.add_argument("--estimated-gib", type=float, required=True)
    check.add_argument("--reserve-gib", type=float, default=DEFAULT_RESERVE_GIB)
    check.add_argument("--node-total-gib", type=float, default=DEFAULT_NODE_TOTAL_GIB)
    check.add_argument("--meminfo-path", default="/proc/meminfo")

    tk = sub.add_parser(
        "takeover-check",
        help="gx-max cluster-takeover admission for one node (D-025); no locking",
    )
    common(tk)
    tk.add_argument("--facts", required=True, help="key=value line from gx-max-safety.sh gxs_clean_start_facts")
    tk.add_argument("--min-avail-gib", type=float, default=TakeoverPolicy.clean_start_min_avail_gib)
    tk.add_argument("--min-swap-free-gib", type=float, default=TakeoverPolicy.min_swap_free_gib)
    tk.add_argument("--max-psi-full", type=float, default=TakeoverPolicy.max_psi_full_avg10)

    register = sub.add_parser("register", help="Record a workload as resident (call after a successful launch)")
    common(register)
    register.add_argument("--class", dest="workload_class", required=True, choices=[c.value for c in WorkloadClass])
    register.add_argument("--estimated-gib", type=float, required=True)
    register.add_argument("--container", default=None)

    release = sub.add_parser("release", help="Remove a workload from the residency ledger")
    common(release)

    status = sub.add_parser("status", help="Dump the reconciled ledger for a node")
    status.add_argument("--node", required=True)

    return p


def _cli(argv: "list[str] | None" = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s %(message)s", stream=sys.stderr)
    args = _build_parser().parse_args(argv)
    state_dir = Path(args.state_dir)

    if args.cmd == "check":
        result = check_admission(
            args.node,
            args.name,
            WorkloadClass(args.workload_class),
            args.estimated_gib,
            state_dir=state_dir,
            reserve_gib=args.reserve_gib,
            node_total_gib=args.node_total_gib,
            meminfo_path=args.meminfo_path,
        )
        print(json.dumps(result.as_log_dict()))
        return 0 if result.allowed else 2

    if args.cmd == "takeover-check":
        try:
            facts = NodeFacts.parse(args.facts)
        except ValueError as exc:
            print(json.dumps({"allowed": False, "reason": f"refused: {exc}", "node": args.node}))
            return 2
        policy = TakeoverPolicy(
            clean_start_min_avail_gib=args.min_avail_gib,
            min_swap_free_gib=args.min_swap_free_gib,
            max_psi_full_avg10=args.max_psi_full,
        )
        result = check_takeover_admission(args.node, args.name, facts, state_dir=state_dir, policy=policy)
        print(json.dumps(result.as_log_dict()))
        return 0 if result.allowed else 2

    if args.cmd == "register":
        ledger = ResidencyLedger(state_dir / f"{args.node}-residency.json")
        ledger.add(
            args.name,
            node=args.node,
            workload_class=WorkloadClass(args.workload_class),
            estimated_gib=args.estimated_gib,
            container=args.container,
        )
        return 0

    if args.cmd == "release":
        release_workload(state_dir, args.node, args.name)
        return 0

    if args.cmd == "status":
        ledger = ResidencyLedger(state_dir / f"{args.node}-residency.json")
        print(json.dumps(ledger.reconcile(), indent=2, sort_keys=True))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(_cli())
