"""Engine lifecycle (docker, admission-guarded) for the gx-call supervisor.

Load   = admission-guarded ``docker run`` of ``gx-call-engine`` + wait for the
         engine's /health to report ``ready`` (weights loaded and warmed up).
Unload = ``docker stop`` + ``rm`` + ledger release, then MemAvailable is
         re-read. Stopping the container is the only way to be sure unified
         memory really comes back on GB10.
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
from typing import Callable

from .config import Config
from .errors import EngineError, ResourceWait

log = logging.getLogger("gx_call.engine")

UNLOADED, LOADING, READY, UNLOADING, FAILED = "unloaded", "loading", "ready", "unloading", "failed"


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


class EngineController:
    def __init__(self, cfg: Config, docker: Docker | None = None, guard: "GuardAdapter | None" = None,
                 peers=None, clock: Callable[[], float] = time.time,  # noqa: ANN001
                 mem: Callable[[], dict] = meminfo, metrics=None) -> None:  # noqa: ANN001
        self.cfg = cfg
        self.docker = docker or Docker()
        self.guard = guard or GuardAdapter(cfg)
        self.guard.on_admitted = self._record_admission
        self.peers = peers
        self.clock = clock
        self.mem = mem
        self.metrics = metrics
        self._lock = threading.RLock()
        self.state = UNLOADED
        self.detail = ""
        self.loaded_at: float | None = None
        self.last_load_seconds: float | None = None
        self.last_load: dict | None = None
        self.last_unload: dict | None = None
        self.last_error: str | None = None
        self.last_wait: dict | None = None
        self.last_activity = clock()
        self.admit_avail_gib: float | None = None
        self.min_avail_gib: float | None = None
        self.resident_gib: float | None = None
        self.session_live = False
        self._key = self._engine_key()
        self._envfile = cfg.state_dir / "engine.env"
        self._write_envfile()

    # ---------------------------------------------------------- secrets --
    @property
    def key(self) -> str:
        return self._key

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

    def _write_envfile(self) -> None:
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self._envfile, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(f"GX_CALL_ENGINE_KEY={self._key}\n")

    # ----------------------------------------------------------- policy --
    def gxmax_block_reason(self) -> str | None:
        """gx-max acquiring or holding node 2 (same signals as gx-music)."""
        c = self.cfg
        if self.docker.exists(c.gxmax_rank_container):
            return "gx-max is running on the cluster; calls resume when it is released"
        try:
            pid = int(c.gxmax_deadman_pidfile.read_text().strip())
            os.kill(pid, 0)
            return "gx-max is running on the cluster; calls resume when it is released"
        except (OSError, ValueError):
            pass
        if c.control_plane_container and not self.docker.running(c.control_plane_container):
            return "gx10-02 is drained (gx-max or maintenance); calls resume when it is restored"
        try:
            age = self.clock() - c.gxmax_hold_file.stat().st_mtime
        except OSError:
            return None
        if age <= c.gxmax_hold_ttl_s:
            return "gx-max is starting on the cluster; calls resume when it is released"
        log.warning("ignoring stale gx-max hold %s (age %.0fs)", c.gxmax_hold_file, age)
        return None

    def maintenance_reason(self) -> str | None:
        if self.cfg.maintenance_hold_file.exists():
            return "the cluster is in Maintenance mode; calls resume when Maintenance ends"
        return None

    def policy_block_reason(self) -> tuple[str, str] | None:
        block = self.gxmax_block_reason()
        if block:
            return "gx_max_active", block
        block = self.maintenance_reason()
        if block:
            return "maintenance", block
        return None

    def pin(self) -> dict | None:
        try:
            data = json.loads(self.cfg.pins_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        entry = data.get("gx-call")
        return entry if isinstance(entry, dict) else None

    def pinned(self) -> bool:
        return self.pin() is not None

    def pin_honoured(self) -> bool:
        """A pin keeps the model warm, never above gx-max, Maintenance or the reserve."""
        if not self.pinned() or self.policy_block_reason():
            return False
        return self.mem().get("MemAvailable", 0.0) >= self.cfg.reserve_gib

    # ------------------------------------------------------------ state --
    def pending_gib(self) -> float:
        """Growth MemAvailable does not show yet (PLT section 5)."""
        est = self.cfg.engine_estimate_gib
        if self.state == LOADING:
            if self.admit_avail_gib is None:
                return est
            now = self.mem().get("MemAvailable")
            consumed = 0.0 if now is None else max(0.0, self.admit_avail_gib - now)
            return round(max(0.0, est - consumed), 1)
        if self.state == READY and self.session_live:
            return float(os.environ.get("GX_CALL_SESSION_GROWTH_GIB", "2"))
        return 0.0

    def memory_view(self) -> dict:
        return {"estimate_gib": self.cfg.engine_estimate_gib,
                "resident_gib": self.resident_gib if self.state == READY else 0.0,
                "pending_gib": self.pending_gib(), "reserve_gib": self.cfg.reserve_gib,
                "min_mem_available_during_load_gib": self.min_avail_gib}

    def snapshot(self) -> dict:
        with self._lock:
            block = self.policy_block_reason()
            return {
                "state": self.state, "detail": self.detail, "container": self.cfg.engine_container,
                "loaded_at": self.loaded_at, "last_load_seconds": self.last_load_seconds,
                "last_load": self.last_load, "last_unload": self.last_unload, "last_error": self.last_error,
                "idle_seconds": round(self.clock() - self.last_activity, 1),
                "idle_unload_after_s": self.cfg.idle_unload_s,
                "pinned": self.pinned(), "pin_honoured": self.pin_honoured() if self.state == READY else None,
                "blocked_by": block[1] if block else None, "waiting": self.last_wait,
                "memory": self.memory_view(),
            }

    def touch(self) -> None:
        self.last_activity = self.clock()

    def _record_admission(self) -> None:
        self.admit_avail_gib = self.mem().get("MemAvailable")
        self.min_avail_gib = self.admit_avail_gib
        self.resident_gib = None

    def engine_health(self, timeout: float = 3.0) -> dict:
        url = f"http://127.0.0.1:{self.cfg.engine_port}/health"
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310 - fixed loopback URL
                body = json.load(r)
                return body if isinstance(body, dict) else {}
        except (OSError, ValueError):
            return {}

    def reconcile(self) -> None:
        """Adopt a healthy engine left by a previous supervisor, or remove a broken one."""
        with self._lock:
            if self.state in (LOADING, UNLOADING):
                return
            if self.docker.running(self.cfg.engine_container):
                health = self.engine_health()
                if health.get("state") in ("ready", "busy"):
                    if self.state != READY:
                        self.state, self.detail = READY, "adopted running engine"
                        self.loaded_at = self.loaded_at or self.clock()
                        self.guard.register()
                    return
                if health.get("state") == "loading" and self.state == UNLOADED:
                    log.warning("engine container is loading without an owner; removing it")
                self._teardown("reconcile: engine without owner or unhealthy")
                self.state, self.detail = UNLOADED, ""
            elif self.state in (READY, LOADING):
                self.state, self.detail = UNLOADED, "engine container disappeared"
                self.guard.release()
            elif self.docker.exists(self.cfg.engine_container):
                self.docker.run(["rm", "-f", self.cfg.engine_container])

    # ------------------------------------------------------------- load --
    def ensure_loaded(self, on_status: Callable[[str], None] = lambda _: None) -> float | None:
        """Return seconds spent loading, or None if it was already loaded. Raises ResourceWait."""
        deadline = self.clock() + self.cfg.engine_start_timeout + 120
        while self.state in (LOADING, UNLOADING):
            if self.clock() > deadline:
                raise EngineError("the voice model is stuck in a lifecycle transition", retryable=True)
            on_status(self.detail or self.state)
            time.sleep(1)
        with self._lock:
            if self.state == READY and self.docker.running(self.cfg.engine_container):
                self.touch()
                return None
            block = self.policy_block_reason()
            if block:
                raise ResourceWait(block[1], code=block[0])
            self.reconcile()
            if self.state == READY:
                return None
            started = self.clock()
            before = self.mem().get("MemAvailable")
            self._admit_and_start()
            self.state, self.detail = LOADING, "loading NemotronLabs VoiceChat 11B"
            self.last_error = None
        on_status("loading")
        try:
            self._wait_ready(started, on_status)
        except Exception as exc:
            with self._lock:
                self._teardown("load failed")
                self.state = FAILED
                self.detail = "the voice model failed to load"
                self.last_error = getattr(exc, "message", None) or type(exc).__name__
            if self.metrics:
                self.metrics.emit("model.load", alias="gx-call", outcome="failed",
                                  duration_ms=round((self.clock() - started) * 1000))
            raise
        with self._lock:
            self.state, self.detail = READY, ""
            self.loaded_at = self.clock()
            self.last_load_seconds = round(self.loaded_at - started, 1)
            now = self.mem().get("MemAvailable")
            if self.admit_avail_gib is not None and now is not None:
                self.resident_gib = round(max(0.0, self.admit_avail_gib - now), 1)
            self.last_load = {"at": self.loaded_at, "seconds": self.last_load_seconds,
                              "mem_available_before_gib": before, "mem_available_after_gib": now,
                              "mem_available_min_gib": self.min_avail_gib, "resident_gib": self.resident_gib}
            self.touch()
        if self.metrics:
            self.metrics.emit("model.load", alias="gx-call", outcome="ok",
                              duration_ms=round(self.last_load_seconds * 1000), startup_s=self.last_load_seconds,
                              footprint_gib=self.resident_gib, mem_available_before_gib=before,
                              mem_available_min_gib=self.min_avail_gib)
        return self.last_load_seconds

    def _peer_pending(self) -> float:
        if self.peers is None:
            return 0.0
        try:
            return float(self.peers.pending_gib(fresh=True))
        except Exception:  # noqa: BLE001 - an unreadable peer never blocks by itself
            return 0.0

    def _admit_and_start(self) -> None:
        extra = self._peer_pending()
        try:
            self.guard.launch(self._start_container, extra_gib=extra)
            self.last_wait = None
        except ResourceWait as wait:
            if wait.code == "insufficient_memory":
                c = self.cfg
                avail = self.mem().get("MemAvailable")
                need = c.engine_estimate_gib + extra + c.reserve_gib
                reason = (f"Waiting for gx10-02 memory: gx-call needs about {c.engine_estimate_gib:.0f} GiB "
                          f"plus the {c.reserve_gib:.0f} GiB reserve"
                          + (f" plus {extra:.0f} GiB other tenants are still loading" if extra >= 0.5 else "")
                          + f", so {need:.0f} GiB must be available"
                          + (f"; {avail:.0f} GiB is" if avail is not None else ""))
                wait = ResourceWait(reason, code=wait.code)
            self.last_wait = {"at": self.clock(), "code": wait.code, "reason": wait.reason}
            if self.metrics:
                self.metrics.emit("admission.wait", alias="gx-call", outcome="waiting", reason_code=wait.code,
                                  reason=wait.reason, required_gib=self.cfg.engine_estimate_gib,
                                  available_gib=self.mem().get("MemAvailable"), pending_gib=extra)
            raise wait from None

    def _start_container(self) -> None:
        c = self.cfg
        self.docker.run(["rm", "-f", c.engine_container], timeout=60)
        args = [
            "run", "-d", "--name", c.engine_container, "--init",
            "--label", "gx.workload=gx-call", "--label", "gx.managed-by=gx-call-supervisor",
            "--device", "nvidia.com/gpu=all",
            "--shm-size", "8g",
            "--memory", c.engine_memory_cap,
            "--oom-score-adj", "900",
            "--user", f"{os.getuid()}:{os.getgid()}",
            "-p", f"127.0.0.1:{c.engine_port}:{c.engine_port}",
            "--env-file", str(self._envfile),
            "-e", f"GX_CALL_ENGINE_PORT={c.engine_port}",
            "-e", f"GX_CALL_CHUNK_S={c.engine_chunk_s}",
            "-e", f"GX_CALL_SPEAKER={c.model.voice}",
            "-e", f"GX_CALL_MAX_SESSION_S={c.max_session_s + 60}",
            "-e", f"GX_CALL_TOOL_TIMEOUT_S={c.tool_timeout_s + 2}",
            "-v", f"{c.model_dir}:/models/voicechat:ro",
            "-v", f"{c.hf_cache_dir / 'hub'}:/work/cache/hf/hub:ro",
            c.model.image,
        ]
        r = self.docker.run(args, timeout=120)
        if r.returncode != 0:
            log.error("docker run failed: %s", r.stderr.strip()[-2000:])
            raise EngineError("the voice engine container could not be started")

    def _wait_ready(self, started: float, on_status: Callable[[str], None]) -> None:
        deadline = started + self.cfg.engine_start_timeout
        while self.clock() < deadline:
            avail = self.mem().get("MemAvailable")
            if avail is not None:
                self.min_avail_gib = avail if self.min_avail_gib is None else min(self.min_avail_gib, avail)
            block = self.gxmax_block_reason()
            if block:
                log.warning("gx-max claimed gx10-02 while gx-call was loading; aborting the load")
                raise ResourceWait(block, code="gx_max_active")
            if not self.docker.running(self.cfg.engine_container):
                self._log_tail("engine exited during load")
                raise EngineError("the voice engine stopped while loading the model")
            health = self.engine_health(timeout=2)
            if health.get("state") == "ready":
                return
            if health.get("state") == "failed":
                self._log_tail("engine reported a failed load")
                raise EngineError("the voice model failed to load")
            elapsed = int(self.clock() - started)
            self.detail = f"loading NemotronLabs VoiceChat 11B ({elapsed} s)"
            on_status(self.detail)
            time.sleep(1)
        self._log_tail("engine load timeout")
        raise EngineError("the voice model took too long to load")

    def _log_tail(self, why: str) -> None:
        r = self.docker.run(["logs", "--tail", "80", self.cfg.engine_container], timeout=30)
        log.error("%s; engine log tail:\n%s%s", why, r.stdout[-8000:], r.stderr[-8000:])

    # ----------------------------------------------------------- unload --
    def unload(self, reason: str, kind: str = "manual") -> dict:
        with self._lock:
            before = self.mem()
            t0 = self.clock()
            self.state, self.detail = UNLOADING, reason
            self._teardown(reason)
            time.sleep(3)  # unified memory is returned asynchronously
            after = self.mem()
            self.state, self.detail = UNLOADED, ""
            self.loaded_at = None
            self.admit_avail_gib = None
            self.resident_gib = None
            self.session_live = False
            self.last_unload = {
                "reason": reason, "kind": kind, "at": self.clock(), "seconds": round(self.clock() - t0, 1),
                "mem_available_before_gib": before.get("MemAvailable"),
                "mem_available_after_gib": after.get("MemAvailable"),
                "container_gone": not self.docker.exists(self.cfg.engine_container),
                "ledger_released": self.guard.released(),
            }
            log.info("engine unloaded: %s", json.dumps(self.last_unload))
        if self.metrics:
            self.metrics.emit("model.unload", alias="gx-call", outcome="ok", reason=kind,
                              duration_ms=round(self.last_unload["seconds"] * 1000))
        return self.last_unload

    def _teardown(self, reason: str) -> None:
        name = self.cfg.engine_container
        if self.docker.exists(name):
            self.docker.run(["stop", "-t", "20", name], timeout=90)
            self.docker.run(["rm", "-f", name], timeout=60)
        self.guard.release()
        log.info("engine torn down (%s)", reason)


class GuardAdapter:
    """Binds the shared node admission guard (legenex/orchestrator)."""

    on_admitted: Callable[[], None] | None = None

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        if str(cfg.orchestrator_dir) not in sys.path:
            sys.path.insert(0, str(cfg.orchestrator_dir))
        from gx_orchestrator import resource_guard as rg  # noqa: PLC0415

        self.rg = rg

    @property
    def workload(self) -> str:
        return self.cfg.engine_container

    def launch(self, start: Callable[[], None], extra_gib: float = 0.0) -> None:
        rg, c = self.rg, self.cfg
        try:
            with rg.guard_launch(c.node, self.workload, rg.WorkloadClass.MEDIUM,
                                 c.engine_estimate_gib + max(0.0, extra_gib),
                                 state_dir=c.guard_dir, reserve_gib=c.reserve_gib, lock_timeout=30) as (_, ledger):
                if self.on_admitted is not None:
                    self.on_admitted()
                start()
                ledger.add(self.workload, node=c.node, workload_class=rg.WorkloadClass.MEDIUM,
                           estimated_gib=c.engine_estimate_gib, container=c.engine_container)
        except rg.AdmissionRefused as exc:
            reason = str(exc.result.reason)
            if "maintenance" in reason.lower():
                raise ResourceWait("the cluster is in Maintenance mode", code="maintenance") from exc
            raise ResourceWait("waiting for memory on gx10-02") from exc
        except rg.NodeLockBusy as exc:
            raise ResourceWait("another model is being started on gx10-02", code="node_busy") from exc

    def register(self) -> None:
        rg, c = self.rg, self.cfg
        ledger = rg.ResidencyLedger(Path(c.guard_dir) / f"{c.node}-residency.json")
        ledger.add(self.workload, node=c.node, workload_class=rg.WorkloadClass.MEDIUM,
                   estimated_gib=c.engine_estimate_gib, container=c.engine_container)

    def release(self) -> None:
        try:
            self.rg.release_workload(Path(self.cfg.guard_dir), self.cfg.node, self.workload)
        except Exception:  # noqa: BLE001 - release must never mask a teardown
            log.exception("ledger release failed (reconcile will clear it)")

    def released(self) -> bool:
        try:
            data = json.loads((Path(self.cfg.guard_dir) / f"{self.cfg.node}-residency.json").read_text())
        except (OSError, ValueError):
            return True
        entries = data.get("workloads", data) if isinstance(data, dict) else {}
        return self.workload not in entries
