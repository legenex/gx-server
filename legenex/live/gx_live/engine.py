"""Engine lifecycle (docker) and the loopback control client.

Load   = admission-guarded ``docker run`` of the engine container
         (``gx_orchestrator.resource_guard``: node flock, residency ledger,
         the 30 GiB reserve, other tenants' pending memory).
Unload = ``docker stop`` + ``rm`` + ledger release, then verified: container
         gone, ledger entry gone, no engine process left. Stopping the
         container is the only way to guarantee unified memory really comes
         back on GB10, so there is no "soft" unload.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .config import Config
from .errors import EngineError, ResourceWait

log = logging.getLogger("gx_live.engine")

UNLOADED, WAITING, LOADING, READY, UNLOADING, FAILED = "unloaded", "waiting", "loading", "ready", "unloading", "failed"


def meminfo(path: str = "/proc/meminfo") -> dict[str, float]:
    out: dict[str, float] = {}
    try:
        with open(path, encoding="ascii") as fh:
            for line in fh:
                k, v = line.split(":", 1)
                if k in {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"}:
                    out[k] = round(int(v.split()[0]) / 1048576, 2)
    except OSError:
        pass
    if "SwapTotal" in out and "SwapFree" in out:
        out["SwapUsed"] = round(out["SwapTotal"] - out["SwapFree"], 2)
    return out


class Docker:
    """Thin, injectable wrapper so tests never need a daemon."""

    def run(self, args: list[str], timeout: float = 120) -> subprocess.CompletedProcess:
        return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, check=False)

    def running(self, name: str) -> bool:
        r = self.run(["inspect", "-f", "{{.State.Running}}", name], timeout=20)
        return r.returncode == 0 and r.stdout.strip() == "true"

    def exists(self, name: str) -> bool:
        return self.run(["inspect", "-f", "{{.Id}}", name], timeout=20).returncode == 0

    def processes(self, name: str) -> int:
        """Processes still inside the container (0 when it is gone)."""
        r = self.run(["top", name, "-o", "pid"], timeout=20)
        if r.returncode != 0:
            return 0
        return max(0, len([ln for ln in r.stdout.splitlines() if ln.strip()]) - 1)


class Peers:
    """Other node-2 tenants' pending memory (plt.md section 5)."""

    def __init__(self, cfg: Config) -> None:
        if str(cfg.common_dir) not in sys.path:
            sys.path.insert(0, str(cfg.common_dir))
        from gxcommon.node2_tenants import PeerTenants  # noqa: PLC0415

        self.tenants = PeerTenants.from_env(exclude="gx-live")

    def pending_gib(self) -> float:
        try:
            return float(self.tenants.pending_gib(fresh=True))
        except Exception:  # noqa: BLE001 - a peer read must never break admission bookkeeping
            log.exception("peer health read failed")
            return 0.0

    def snapshot(self) -> dict:
        try:
            return self.tenants.snapshot()
        except Exception:  # noqa: BLE001
            return {}


class GuardAdapter:
    """Binds the shared node admission guard (legenex/orchestrator)."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        if str(cfg.orchestrator_dir) not in sys.path:
            sys.path.insert(0, str(cfg.orchestrator_dir))
        from gx_orchestrator import resource_guard as rg  # noqa: PLC0415

        self.rg = rg

    def launch(self, start: Callable[[], None], *, extra_gib: float, on_admitted: Callable[[], None]) -> None:
        rg, c = self.rg, self.cfg
        try:
            with rg.guard_launch(c.node, c.workload, rg.WorkloadClass.MEDIUM,
                                 c.engine_estimate_gib + max(0.0, extra_gib),
                                 state_dir=c.guard_dir, reserve_gib=c.reserve_gib, lock_timeout=30) as (_, ledger):
                on_admitted()
                start()
                ledger.add(c.workload, node=c.node, workload_class=rg.WorkloadClass.MEDIUM,
                           estimated_gib=c.engine_estimate_gib, container=c.engine_container)
        except rg.AdmissionRefused as exc:
            raise ResourceWait(_human_refusal(exc.result.reason), code="insufficient_memory",
                               details={"guard": exc.result.reason[:300]}) from exc
        except rg.NodeLockBusy as exc:
            raise ResourceWait("another model is being started on gx10-02", code="node_busy") from exc

    def register(self) -> None:
        rg, c = self.rg, self.cfg
        ledger = rg.ResidencyLedger(Path(c.guard_dir) / f"{c.node}-residency.json")
        ledger.add(c.workload, node=c.node, workload_class=rg.WorkloadClass.MEDIUM,
                   estimated_gib=c.engine_estimate_gib, container=c.engine_container)

    def release(self) -> None:
        try:
            self.rg.release_workload(Path(self.cfg.guard_dir), self.cfg.node, self.cfg.workload)
        except Exception:  # noqa: BLE001 - release must never mask a teardown
            log.exception("ledger release failed (reconcile will clear it)")

    def listed(self) -> bool:
        path = Path(self.cfg.guard_dir) / f"{self.cfg.node}-residency.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return isinstance(data, dict) and self.cfg.workload in data


def _human_refusal(reason: str) -> str:
    low = reason.lower()
    if "maintenance" in low:
        return "the cluster is in Maintenance mode; gx-live starts when Maintenance ends"
    if "reserve" in low:
        return "waiting for memory on gx10-02 (other models are using it)"
    if "large/exclusive" in low:
        return "waiting for another large model on gx10-02 to finish"
    return "waiting for resources on gx10-02"


class EngineController:
    def __init__(self, cfg: Config, docker: Docker | None = None, guard: Any = None, peers: Any = None,
                 clock: Callable[[], float] = time.time, mem: Callable[[], dict] = meminfo,
                 metrics: Any = None) -> None:
        self.cfg = cfg
        self.docker = docker or Docker()
        self.guard = guard if guard is not None else GuardAdapter(cfg)
        self.peers = peers if peers is not None else Peers(cfg)
        self.clock = clock
        self.mem = mem
        self.metrics = metrics
        self._lock = threading.RLock()
        self.state = UNLOADED
        self.state_detail = ""
        self.waiting: dict | None = None
        self.loaded_at: float | None = None
        self.last_load_seconds: float | None = None
        self.last_unload: dict | None = None
        self.last_activity = clock()
        self.admit_avail_gib: float | None = None
        self.min_avail_gib: float | None = None
        self.resident_gib: float | None = None
        self._key = self._engine_key()
        self._envfile = cfg.state_dir / "engine.env"
        self._write_envfile()
        self._listeners: list[Callable[[dict], None]] = []

    # ---------------------------------------------------------- secrets --
    def _engine_key(self) -> str:
        path = self.cfg.engine_key_file
        try:
            key = path.read_text(encoding="utf-8").strip()
            if len(key) >= 32:
                return key
        except OSError:
            pass
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        key = secrets.token_urlsafe(32)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(key + "\n")
        return key

    @property
    def key(self) -> str:
        return self._key

    def _write_envfile(self) -> None:
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self._envfile, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(f"GX_LIVE_ENGINE_KEY={self._key}\n")

    # ------------------------------------------------------- listeners --
    def subscribe(self, fn: Callable[[dict], None]) -> None:
        self._listeners.append(fn)

    def _set(self, state: str, detail: str = "", waiting: dict | None = None) -> None:
        self.state, self.state_detail, self.waiting = state, detail, waiting
        view = self.public_state()
        for fn in list(self._listeners):
            try:
                fn(view)
            except Exception:  # noqa: BLE001
                log.exception("state listener failed")

    def public_state(self) -> dict:
        return {"state": self.state, "reason": self.state_detail, "waiting": self.waiting,
                "load_ms": int(self.last_load_seconds * 1000) if self.state == READY and self.last_load_seconds
                else None}

    # ----------------------------------------------------------- policy --
    def gxmax_block_reason(self) -> str | None:
        """Is gx-max acquiring or holding node 2? (same signals as gx-music)"""
        if self.docker.exists(self.cfg.gxmax_rank_container):
            return "gx-max is running on the cluster; gx-live resumes when it is released"
        try:
            pid = int(self.cfg.gxmax_deadman_pidfile.read_text().strip())
            os.kill(pid, 0)
            return "gx-max is running on the cluster; gx-live resumes when it is released"
        except (OSError, ValueError):
            pass
        if self.cfg.control_plane_container and not self.docker.running(self.cfg.control_plane_container):
            return "gx10-02 is drained (gx-max or maintenance); gx-live resumes when it is restored"
        try:
            age = self.clock() - self.cfg.gxmax_hold_file.stat().st_mtime
        except OSError:
            return None
        if age <= self.cfg.gxmax_hold_ttl_s:
            return "gx-max is starting on the cluster; gx-live resumes when it is released"
        log.warning("ignoring stale gx-max hold (age %.0fs, no rank container)", age)
        return None

    def maintenance_reason(self) -> str | None:
        if self.cfg.maintenance_hold_file.exists():
            return "the cluster is in Maintenance mode; gx-live resumes when Maintenance ends"
        return None

    def policy_block(self) -> tuple[str, str] | None:
        block = self.gxmax_block_reason()
        if block:
            return "gx_max_active", block
        block = self.maintenance_reason()
        if block:
            return "maintenance", block
        return None

    def pinned(self) -> bool:
        try:
            data = json.loads(self.cfg.pins_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return isinstance(data, dict) and isinstance(data.get("gx-live"), dict)

    def pin_honoured(self) -> bool:
        """A pin never overrides gx-max, Maintenance or the memory reserve."""
        if not self.pinned() or self.policy_block():
            return False
        return self.mem().get("MemAvailable", 0.0) >= self.cfg.reserve_gib

    # --------------------------------------------------------- memory --
    def pending_gib(self) -> float:
        est = self.cfg.engine_estimate_gib
        if self.state == LOADING:
            if self.admit_avail_gib is None:
                return est
            now = self.mem().get("MemAvailable")
            consumed = 0.0 if now is None else max(0.0, self.admit_avail_gib - now)
            return round(max(0.0, est - consumed), 1)
        if self.state == READY:
            return round(max(0.0, est - (self.resident_gib or 0.0)), 1)
        return 0.0

    def memory_view(self) -> dict:
        return {"estimate_gib": self.cfg.engine_estimate_gib,
                "resident_gib": self.resident_gib if self.state == READY else 0.0,
                "pending_gib": self.pending_gib(), "reserve_gib": self.cfg.reserve_gib,
                "min_available_during_load_gib": self.min_avail_gib}

    def sample_memory(self) -> None:
        """Called at 1 Hz by the supervisor while loading."""
        now = self.mem().get("MemAvailable")
        if now is not None and (self.min_avail_gib is None or now < self.min_avail_gib):
            self.min_avail_gib = now

    def touch(self) -> None:
        self.last_activity = self.clock()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "state": self.state, "detail": self.state_detail, "waiting": self.waiting,
                "container": self.cfg.engine_container, "loaded_at": self.loaded_at,
                "last_load_seconds": self.last_load_seconds, "last_unload": self.last_unload,
                "idle_seconds": round(self.clock() - self.last_activity, 1),
                "idle_unload_after_s": self.cfg.idle_unload_s, "pinned": self.pinned(),
                "blocked_by": (self.policy_block() or (None, None))[1], "memory": self.memory_view(),
            }

    # ------------------------------------------------------- reconcile --
    def reconcile(self) -> None:
        """Adopt or forget a container left by a previous supervisor."""
        with self._lock:
            if self.state in (LOADING, UNLOADING, WAITING):
                return
            if self.docker.running(self.cfg.engine_container):
                try:
                    h = self.health(timeout=3)
                except EngineError:
                    h = {}
                if h.get("ready"):
                    if self.state != READY:
                        self.loaded_at = self.loaded_at or self.clock()
                        self.resident_gib = self.resident_gib or h.get("gpu_allocated_gib")
                        self.guard.register()
                        self._set(READY, "adopted running engine")
                    return
                if self.state == READY:
                    log.warning("engine stopped answering; removing it")
                    self._teardown("reconcile: unhealthy")
                    self._set(FAILED, "the live model stopped responding")
                elif not h:
                    log.warning("engine container present but not ready and not being loaded; removing it")
                    self._teardown("reconcile: unowned leftover")
                return
            if self.state == READY:
                self.guard.release()
                self._set(UNLOADED, "the engine container disappeared")
            elif self.docker.exists(self.cfg.engine_container):
                self.docker.run(["rm", "-f", self.cfg.engine_container])

    # ------------------------------------------------------------ load --
    def ensure_loaded(self) -> float | None:
        """Block until the engine is ready. Returns load seconds, or None if it was
        already loaded. Raises ResourceWait (caller retries) or EngineError."""
        deadline = self.clock() + self.cfg.engine_start_timeout + 120
        while self.state in (LOADING, UNLOADING):
            if self.clock() > deadline:
                raise EngineError("the live model is stuck in a lifecycle transition", retryable=True)
            time.sleep(0.5)
        with self._lock:
            if self.state == READY and self.docker.running(self.cfg.engine_container):
                self.touch()
                return None
            block = self.policy_block()
            if block:
                self._set(WAITING, block[1], {"code": block[0], "reason": block[1]})
                raise ResourceWait(block[1], code=block[0])
            self.reconcile()
            if self.state == READY:
                return None
            started = self.clock()
            before = self.mem().get("MemAvailable")
            extra = self.peers.pending_gib()
            try:
                self.guard.launch(self._start_container, extra_gib=extra, on_admitted=self._admitted)
            except ResourceWait as wait:
                avail = self.mem().get("MemAvailable")
                need = self.cfg.engine_estimate_gib + extra + self.cfg.reserve_gib
                reason = wait.reason
                if wait.code == "insufficient_memory":
                    reason = (f"Waiting for memory on gx10-02: gx-live needs about "
                              f"{self.cfg.engine_estimate_gib:.0f} GiB plus the {self.cfg.reserve_gib:.0f} GiB reserve"
                              + (f" plus {extra:.0f} GiB that other models have not taken yet" if extra >= 0.5 else "")
                              + f", so {need:.0f} GiB must be available"
                              + (f"; {avail:.0f} GiB is" if avail is not None else ""))
                details = {"required_gib": round(need, 1), "available_gib": avail,
                           "pending_gib": round(extra, 1), "reserve_gib": self.cfg.reserve_gib}
                self._set(WAITING, reason, {"code": wait.code, "reason": reason, **details})
                self._metric("admission.wait", alias="gx-live", reason_code=wait.code, reason=reason,
                             required_gib=round(need, 1), available_gib=avail, pending_gib=round(extra, 1),
                             outcome="waiting")
                raise ResourceWait(reason, code=wait.code, details=details) from None
            self._set(LOADING, "loading MiniCPM-o 4.5 into memory")
            self.min_avail_gib = before
        try:
            self._wait_ready(started)
        except Exception as exc:
            with self._lock:
                self._teardown("load failed")
                if isinstance(exc, ResourceWait):
                    self._set(WAITING, exc.reason, {"code": exc.code, "reason": exc.reason})
                else:
                    self._set(FAILED, "the live model failed to load")
            self._metric("model.load", alias="gx-live", outcome="failed",
                         duration_ms=int((self.clock() - started) * 1000),
                         error_code=getattr(exc, "code", type(exc).__name__))
            raise
        with self._lock:
            self.loaded_at = self.clock()
            self.last_load_seconds = round(self.loaded_at - started, 1)
            now = self.mem().get("MemAvailable")
            self.sample_memory()
            if self.admit_avail_gib is not None and now is not None:
                self.resident_gib = round(max(0.0, self.admit_avail_gib - now), 1)
            self.touch()
            self._set(READY, "")
            self._metric("model.load", alias="gx-live", outcome="ok",
                         duration_ms=int(self.last_load_seconds * 1000), startup_s=self.last_load_seconds,
                         footprint_gib=self.resident_gib, mem_available_before_gib=before,
                         mem_available_min_gib=self.min_avail_gib)
            return self.last_load_seconds

    def _admitted(self) -> None:
        self.admit_avail_gib = self.mem().get("MemAvailable")
        self.resident_gib = None

    def _start_container(self) -> None:
        c = self.cfg
        self.docker.run(["rm", "-f", c.engine_container], timeout=60)
        args = [
            "run", "-d", "--name", c.engine_container, "--init",
            "--label", "gx.workload=gx-live", "--label", "gx.managed-by=gx-live-supervisor",
            "--device", "nvidia.com/gpu=all",
            "--shm-size", "4g",
            "--memory", c.engine_memory_cap,
            "--oom-score-adj", "900",
            "--user", f"{os.getuid()}:{os.getgid()}",
            "--network", "bridge",
            "-p", f"127.0.0.1:{c.engine_port}:{c.engine_port}",
            "--env-file", str(self._envfile),
            "-e", f"GX_LIVE_ENGINE_PORT={c.engine_port}",
            "-e", "GX_LIVE_ENGINE_HOST=0.0.0.0",
            "-e", "GX_LIVE_MODEL_DIR=/models/model",
            "-v", f"{c.model_dir}:/models/model:ro",
            "-v", f"{c.engine_dir / 'gx_live_engine'}:/opt/gx-live/gx_live_engine:ro",
            c.model.image,
        ]
        r = self.docker.run(args, timeout=120)
        if r.returncode != 0:
            log.error("docker run failed: %s", r.stderr.strip()[-2000:])
            raise EngineError("the live engine container could not be started")

    def _wait_ready(self, started: float) -> None:
        deadline = started + self.cfg.engine_start_timeout
        while self.clock() < deadline:
            self.sample_memory()
            block = self.gxmax_block_reason()
            if block:
                log.warning("gx-max claimed gx10-02 while gx-live was loading; aborting the load")
                raise ResourceWait(block, code="gx_max_active")
            if not self.docker.running(self.cfg.engine_container):
                self._log_tail("engine exited during load")
                raise EngineError("the live engine stopped while loading the model")
            try:
                h = self.health(timeout=3)
                if h.get("ready"):
                    return
                if h.get("error"):
                    self._log_tail("engine reported a load error")
                    raise EngineError("the live model failed to load")
            except EngineError as exc:
                if "failed to load" in exc.message:
                    raise
            time.sleep(1)
        self._log_tail("engine load timeout")
        raise EngineError("the live model took too long to load")

    def _log_tail(self, why: str) -> None:
        r = self.docker.run(["logs", "--tail", "60", self.cfg.engine_container], timeout=30)
        log.error("%s; engine log tail:\n%s%s", why, r.stdout[-6000:], r.stderr[-6000:])

    # ---------------------------------------------------------- unload --
    def unload(self, reason: str) -> dict:
        """Tear the engine down and verify that its memory really came back."""
        with self._lock:
            before = self.mem()
            t0 = self.clock()
            was = self.state
            self._set(UNLOADING, reason)
            self._teardown(reason)
            verified = self._verify_gone()
            after = self.mem()
            self.loaded_at = None
            self.admit_avail_gib = None
            self.resident_gib = None
            self._set(UNLOADED, "")
            self.last_unload = {
                "reason": reason, "at": self.clock(), "seconds": round(self.clock() - t0, 1), "was": was,
                "mem_available_before_gib": before.get("MemAvailable"),
                "mem_available_after_gib": after.get("MemAvailable"),
                "returned_gib": (round(after["MemAvailable"] - before["MemAvailable"], 1)
                                 if "MemAvailable" in after and "MemAvailable" in before else None),
                **verified,
            }
            log.info("engine unloaded: %s", json.dumps(self.last_unload))
            self._metric("model.unload", alias="gx-live", reason=_unload_reason(reason), outcome="ok"
                         if verified["verified"] else "failed", duration_ms=int((self.clock() - t0) * 1000))
            return self.last_unload

    def _verify_gone(self) -> dict:
        deadline = self.clock() + 20
        while True:
            gone = not self.docker.exists(self.cfg.engine_container)
            procs = 0 if gone else self.docker.processes(self.cfg.engine_container)
            listed = self.guard.listed()
            if (gone and not listed) or self.clock() > deadline:
                break
            time.sleep(1)
        # unified memory is handed back asynchronously; give the kernel a moment
        time.sleep(2)
        return {"container_gone": gone, "ledger_released": not listed, "engine_processes": procs,
                "verified": gone and not listed and procs == 0}

    def _teardown(self, reason: str) -> None:
        name = self.cfg.engine_container
        if self.docker.exists(name):
            self.docker.run(["stop", "-t", "15", name], timeout=60)
            self.docker.run(["rm", "-f", name], timeout=60)
        self.guard.release()
        log.info("engine torn down (%s)", reason)

    def _metric(self, event: str, **fields: Any) -> None:
        if self.metrics is not None:
            try:
                self.metrics.emit(event, **fields)
            except Exception:  # noqa: BLE001
                log.exception("metric emit failed")

    # ------------------------------------------------------------- HTTP --
    def health(self, timeout: float = 5) -> dict:
        url = f"http://127.0.0.1:{self.cfg.engine_port}/health"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self._key}"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - fixed loopback URL
                body = json.load(r)
        except (OSError, ValueError) as exc:
            raise EngineError("the live engine is not reachable", retryable=True) from exc
        return body if isinstance(body, dict) else {}


def _unload_reason(reason: str) -> str:
    low = reason.lower()
    for key in ("idle", "gxmax", "maintenance", "manual", "shutdown", "evicted"):
        if key in low.replace("gx-max", "gxmax"):
            return key
    return "manual"
