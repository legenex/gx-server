"""Engine lifecycle (docker), node-2 policy and the loopback engine client.

Load   = admission-guarded ``docker run`` of the engine container.
Unload = ``docker stop`` + ``rm`` + ledger release. There is no soft unload:
removing the container is the only way to be sure unified memory really comes
back on GB10.
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
from typing import Any
from collections.abc import Callable

from .config import ENGINE_MODELS, ENGINE_WORK, VARIANTS, Config
from .errors import EngineError, ResourceWait

log = logging.getLogger("gx_voice.engine")

UNLOADED, LOADING, READY, UNLOADING, FAILED = "unloaded", "loading", "ready", "unloading", "failed"
WORKLOAD_NAME = "gx-voice"


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

    def run(self, args: list[str], timeout: float = 120, stdin: bytes | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(["docker", *args], capture_output=True, timeout=timeout, check=False,
                              input=stdin, text=stdin is None)

    def running(self, name: str) -> bool:
        r = self.run(["inspect", "-f", "{{.State.Running}}", name], timeout=20)
        return r.returncode == 0 and str(r.stdout).strip() == "true"

    def exists(self, name: str) -> bool:
        return self.run(["inspect", "-f", "{{.Id}}", name], timeout=20).returncode == 0


class EngineController:
    def __init__(self, cfg: Config, docker: Docker | None = None, guard: GuardAdapter | None = None,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep) -> None:
        self.cfg = cfg
        self.docker = docker or Docker()
        self.guard = guard or GuardAdapter(cfg)
        self.guard.on_admitted = self._record_admission
        self.clock = clock
        self.sleep = sleep
        self._lock = threading.RLock()
        self.state = UNLOADED
        self.state_detail = ""
        self.loaded_at: float | None = None
        self.last_load_seconds: float | None = None
        self.last_unload: dict | None = None
        self.last_activity = clock()
        self.admit_avail_gib: float | None = None
        self.resident_gib: float | None = None
        self.last_wait: dict | None = None
        self.variants_loaded: list[str] = []
        self._key = self._engine_key()
        self._envfile = cfg.state_dir / "engine.env"
        self._write_envfile()

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

    def _write_envfile(self) -> None:
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self._envfile, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(f"GX_VOICE_ENGINE_KEY={self._key}\n")

    # ----------------------------------------------------------- policy --
    def gxmax_block_reason(self) -> str | None:
        """Is gx-max acquiring or holding node 2? (the same four signals as gx-music)"""
        if self.docker.exists(self.cfg.gxmax_rank_container):
            return "gx-max is running on the cluster; voice resumes when it is released"
        try:
            pid = int(self.cfg.gxmax_deadman_pidfile.read_text().strip())
            os.kill(pid, 0)
            return "gx-max is running on the cluster; voice resumes when it is released"
        except (OSError, ValueError):
            pass
        if self.cfg.control_plane_container and not self.docker.running(self.cfg.control_plane_container):
            return "the media node is drained (gx-max or maintenance); voice resumes when it is restored"
        try:
            age = self.clock() - self.cfg.gxmax_hold_file.stat().st_mtime
        except OSError:
            return None
        if age <= self.cfg.gxmax_hold_ttl_s:
            return "gx-max is starting on the cluster; voice resumes when it is released"
        log.warning("ignoring stale gx-max hold %s (age %.0fs)", self.cfg.gxmax_hold_file, age)
        return None

    def maintenance_reason(self) -> str | None:
        if self.cfg.maintenance_hold_file.exists():
            return "the cluster is in Maintenance mode; voice resumes when Maintenance ends"
        return None

    def policy_block_reason(self) -> tuple[str, str] | None:
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
        return isinstance(data, dict) and isinstance(data.get(WORKLOAD_NAME), dict)

    def pin_honoured(self) -> bool:
        """A pin never overrides gx-max, Maintenance or the memory reserve."""
        if not self.pinned() or self.policy_block_reason():
            return False
        return meminfo().get("MemAvailable", 0.0) >= self.cfg.reserve_gib

    # ------------------------------------------------------------ state --
    def touch(self) -> None:
        self.last_activity = self.clock()

    def _record_admission(self) -> None:
        self.admit_avail_gib = meminfo().get("MemAvailable")
        self.resident_gib = None

    def pending_gib(self) -> float:
        """Growth MemAvailable does not show yet (D-038, published on /health)."""
        est = self.cfg.engine_estimate_gib
        if self.state == LOADING:
            if self.admit_avail_gib is None:
                return est
            now = meminfo().get("MemAvailable")
            consumed = 0.0 if now is None else max(0.0, self.admit_avail_gib - now)
            return round(max(0.0, est - consumed), 1)
        if self.state == READY:
            return round(max(0.0, est - (self.resident_gib or 0.0)), 1)
        return 0.0

    def memory_view(self) -> dict:
        return {"estimate_gib": self.cfg.engine_estimate_gib, "resident_gib": self.resident_gib,
                "pending_gib": self.pending_gib(), "reserve_gib": self.cfg.reserve_gib}

    def snapshot(self) -> dict:
        with self._lock:
            block = self.policy_block_reason()
            return {"state": self.state, "detail": self.state_detail, "container": self.cfg.engine_container,
                    "variants_loaded": list(self.variants_loaded), "max_resident": self.cfg.engine_max_resident,
                    "loaded_at": self.loaded_at, "last_load_seconds": self.last_load_seconds,
                    "last_unload": self.last_unload, "idle_seconds": round(self.clock() - self.last_activity, 1),
                    "idle_unload_after_s": self.cfg.idle_unload_s, "pinned": self.pinned(),
                    "pin_honoured": self.pin_honoured() if self.state == READY else False,
                    "blocked_by": block[1] if block else None, "memory": self.memory_view(),
                    "last_wait": self.last_wait}

    def reconcile(self) -> None:
        """Adopt or forget a container left by a previous supervisor."""
        with self._lock:
            if self.state in (LOADING, UNLOADING):
                return
            running = self.docker.running(self.cfg.engine_container)
            if running:
                try:
                    h = self.health()
                except EngineError:
                    h = {}
                if h.get("ready"):
                    self.state, self.state_detail = READY, "adopted running engine"
                    self.variants_loaded = list(h.get("loaded") or [])
                    self.loaded_at = self.loaded_at or self.clock()
                    self.guard.register()
                    return
                log.warning("engine container present but not ready; removing it")
                self._teardown("reconcile: unhealthy leftover")
                self.state = UNLOADED
            elif self.state in (READY, LOADING):
                self.state, self.state_detail = UNLOADED, "engine container disappeared"
                self.variants_loaded = []
                self.guard.release()
            elif self.docker.exists(self.cfg.engine_container):
                self.docker.run(["rm", "-f", self.cfg.engine_container])

    # ------------------------------------------------------------- load --
    def ensure_loaded(self, on_phase: Callable[[str], None] = lambda _: None) -> float | None:
        """Start the engine if needed. Returns seconds spent, or None if it was running."""
        deadline = self.clock() + self.cfg.engine_start_timeout + 120
        while self.state in (LOADING, UNLOADING):
            if self.clock() > deadline:
                raise EngineError("the voice model is stuck in a lifecycle transition", retryable=True)
            self.sleep(1)
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
            self._admit_and_start()
            self.state, self.state_detail = LOADING, "starting the voice engine"
            on_phase("loading")
        try:
            self._wait_ready(started)
        except BaseException:
            with self._lock:
                self._teardown("load failed")
                self.state, self.state_detail = FAILED, "the voice engine failed to start"
                self.variants_loaded = []
            raise
        with self._lock:
            self.state, self.state_detail = READY, ""
            self.loaded_at = self.clock()
            self.last_load_seconds = round(self.loaded_at - started, 1)
            self.measure_resident()
            self.touch()
            return self.last_load_seconds

    def measure_resident(self) -> None:
        """What the engine really holds, from MemAvailable at admission (D-038)."""
        now = meminfo().get("MemAvailable")
        if self.admit_avail_gib is not None and now is not None:
            grown = max(0.0, self.admit_avail_gib - now)
            self.resident_gib = round(min(self.cfg.engine_estimate_gib, max(self.resident_gib or 0.0, grown)), 1)

    def peers(self) -> list[dict]:
        """Other node-2 tenants' open /health: busy flag and pending growth (D-038)."""
        out = []
        for url in self.cfg.peer_health_urls:
            try:
                with urllib.request.urlopen(f"{url}/health", timeout=3) as r:
                    body = json.load(r)
            except (OSError, ValueError):
                out.append({"url": url, "reachable": False, "pending_gib": 0.0})
                continue
            if not isinstance(body, dict):
                continue
            mem = body.get("memory") if isinstance(body.get("memory"), dict) else {}
            pending = mem.get("pending_gib")
            pending = float(pending) if isinstance(pending, (int, float)) and pending > 0 else 0.0
            loaded = mem.get("loaded_gib")
            out.append({"url": url, "reachable": True, "service": body.get("service") or "gx-media-router",
                        "busy": bool(body.get("busy")), "held_by": body.get("held_by"),
                        "engine": body.get("engine"), "active_jobs": body.get("active_jobs"),
                        "loaded_gib": float(loaded) if isinstance(loaded, (int, float)) else None,
                        "resident_alias": body.get("resident_alias"), "pending_gib": pending})
        return out

    def profile(self) -> str:
        try:
            value = json.loads(self.cfg.profile_file.read_text(encoding="utf-8")).get("profile")
        except (OSError, ValueError, AttributeError):
            return "auto"
        return value if isinstance(value, str) else "auto"

    def make_room(self, peers: list[dict], extra: float) -> str | None:
        """Free IDLE memory of other node-2 tenants through their own lifecycle
        paths (D-038). Returns what was freed, or None. Never touches a busy
        tenant, a pinned one, or anything under the Music/Maintenance/Max profile."""
        c = self.cfg
        if not c.evict_idle_peers:
            return None
        profile = self.profile()
        if profile in ("maintenance", "max"):
            return None
        need = c.engine_estimate_gib + extra + c.reserve_gib
        avail = meminfo().get("MemAvailable") or 0.0
        music = next((p for p in peers if p.get("service") == "gx-music" and p.get("reachable")), None)
        idle_music = (music and profile != "music" and music.get("engine") == "ready" and not music.get("busy")
                      and not music.get("active_jobs") and (music.get("loaded_gib") or 0.0) + avail >= need)
        if idle_music and self._unload_idle_music():
            return "gx-music (idle)"
        if c.media_router_container and profile != "media":
            try:
                r = self.docker.run(["exec", c.media_router_container, "python", "-m",
                                     "gx_media_router.free_node"], timeout=60)
                lines = str(r.stdout).strip().splitlines() if r.returncode == 0 else []
                body = json.loads(lines[-1]) if lines else {}
            except (OSError, ValueError, subprocess.TimeoutExpired):
                body = {}
            if body.get("freed"):
                log.info("media router freed idle ComfyUI weights %s before loading gx-voice", body.get("models"))
                return "idle ComfyUI weights"
        return None

    def _unload_idle_music(self) -> bool:
        try:
            key = self.cfg.music_key_file.read_text(encoding="utf-8").strip()
        except OSError:
            return False
        req = urllib.request.Request(f"{self.cfg.music_url}/v1/music/unload", method="POST",
                                     data=json.dumps({"if_idle": True}).encode(),
                                     headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                body = json.load(r)
        except (OSError, ValueError):
            return False
        ok = isinstance(body, dict) and body.get("container_gone") is True and not body.get("noop")
        if ok:
            log.info("unloaded idle gx-music to make room for gx-voice: %s", body)
        return ok

    def _admit_and_start(self) -> None:
        tried = False
        while True:
            peers = self.peers()
            extra = sum(p["pending_gib"] for p in peers)
            try:
                self.guard.launch(self._start_container, extra_gib=extra)
                self.last_wait = None
                return
            except ResourceWait as wait:
                if wait.code == "insufficient_memory":
                    if not tried:
                        tried = True
                        freed = self.make_room(peers, extra)
                        if freed:
                            self.sleep(5)  # let unified memory come back, then re-read it
                            continue
                    wait = ResourceWait(self._memory_wait_reason(peers, extra), code=wait.code)
                self.last_wait = {"at": self.clock(), "code": wait.code, "reason": wait.reason, "peers": peers}
                raise wait from None

    def _memory_wait_reason(self, peers: list[dict], extra: float) -> str:
        c = self.cfg
        avail = meminfo().get("MemAvailable")
        need = c.engine_estimate_gib + extra + c.reserve_gib
        numbers = (f"gx-voice needs about {c.engine_estimate_gib:.0f} GiB plus the {c.reserve_gib:.0f} GiB reserve"
                   + (f" plus {extra:.0f} GiB that other jobs on gx10-02 have not taken yet" if extra >= 0.5 else "")
                   + f", so {need:.0f} GiB must be available" + (f"; {avail:.0f} GiB is" if avail is not None else ""))
        for p in peers:
            held = str(p.get("held_by") or "")
            if p.get("busy") and held.startswith("video"):
                return f"Waiting for gx-video to finish on gx10-02: {numbers}"
            if p.get("busy") and held.startswith("image"):
                return f"Waiting for gx-image to finish on gx10-02: {numbers}"
            if p.get("busy") and p.get("service") == "gx-music":
                return f"Waiting for gx-music to finish on gx10-02: {numbers}"
        resident = next((p["resident_alias"] for p in peers if p.get("resident_alias")), None)
        if resident:
            return f"Waiting for {resident} to release gx10-02 memory: {numbers}"
        return f"Waiting for gx10-02 memory (another model holds it): {numbers}"

    def _start_container(self) -> None:
        c = self.cfg
        self.docker.run(["rm", "-f", c.engine_container], timeout=60)
        args = [
            "run", "-d", "--name", c.engine_container, "--init",
            "--label", "gx.workload=gx-voice", "--label", "gx.managed-by=gx-voice-supervisor",
            "--device", "nvidia.com/gpu=all",
            "--shm-size", "2g",
            "--memory", c.engine_memory_cap,
            "--oom-score-adj", "900",
            "--user", f"{os.getuid()}:{os.getgid()}",
            "-p", f"127.0.0.1:{c.engine_port}:{c.engine_port}",
            "--env-file", str(self._envfile),
            "-e", "GX_VOICE_ENGINE_HOST=0.0.0.0",
            "-e", f"GX_VOICE_ENGINE_PORT={c.engine_port}",
            "-e", f"GX_VOICE_MAX_RESIDENT={c.engine_max_resident}",
            "-e", f"GX_VOICE_WORK={ENGINE_WORK}",
            "-e", f"GX_VOICE_MODELS={ENGINE_MODELS}",
            "-v", f"{c.models_dir}:{ENGINE_MODELS}:ro",
            "-v", f"{c.data_root}:{ENGINE_WORK}",
            c.image,
        ]
        r = self.docker.run(args, timeout=120)
        if r.returncode != 0:
            log.error("docker run failed: %s", str(r.stderr).strip()[-2000:])
            raise EngineError("the voice engine container could not be started")

    def _wait_ready(self, started: float) -> None:
        deadline = started + self.cfg.engine_start_timeout
        while self.clock() < deadline:
            block = self.gxmax_block_reason()
            if block:
                log.warning("gx-max claimed node 2 while gx-voice was starting; aborting")
                raise ResourceWait(block, code="gx_max_active")
            if not self.docker.running(self.cfg.engine_container):
                self._log_tail("engine exited during start")
                raise EngineError("the voice engine stopped while starting")
            try:
                if self.health(timeout=3).get("ready"):
                    return
            except EngineError:
                pass
            self.sleep(1)
        self._log_tail("engine start timeout")
        raise EngineError("the voice engine took too long to start")

    def _log_tail(self, why: str) -> None:
        r = self.docker.run(["logs", "--tail", "60", self.cfg.engine_container], timeout=30)
        log.error("%s; engine log tail:\n%s%s", why, str(r.stdout)[-6000:], str(r.stderr)[-6000:])

    # ----------------------------------------------------------- unload --
    def unload(self, reason: str) -> dict:
        with self._lock:
            before = meminfo()
            t0 = self.clock()
            self.state, self.state_detail = UNLOADING, reason
            self._teardown(reason)
            self.sleep(3)  # unified memory is returned asynchronously
            after = meminfo()
            self.state, self.state_detail = UNLOADED, ""
            self.loaded_at = None
            self.admit_avail_gib = None
            self.resident_gib = None
            self.variants_loaded = []
            self.last_unload = {
                "reason": reason, "at": self.clock(), "seconds": round(self.clock() - t0, 1),
                "mem_available_before_gib": before.get("MemAvailable"),
                "mem_available_after_gib": after.get("MemAvailable"),
                "container_gone": not self.docker.exists(self.cfg.engine_container),
            }
            log.info("engine unloaded: %s", json.dumps(self.last_unload))
            return self.last_unload

    def _teardown(self, reason: str) -> None:
        name = self.cfg.engine_container
        if self.docker.exists(name):
            self.docker.run(["stop", "-t", "20", name], timeout=60)
            self.docker.run(["rm", "-f", name], timeout=60)
        self.guard.release()
        log.info("engine torn down (%s)", reason)

    # ------------------------------------------------------------- HTTP --
    def _call(self, method: str, path: str, body: dict | None = None, timeout: float = 30) -> Any:
        url = f"http://127.0.0.1:{self.cfg.engine_port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self._key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as exc:
            try:
                err = (json.loads(exc.read()[:4000] or b"{}").get("error") or {})
            except ValueError:
                err = {}
            log.error("engine %s %s -> HTTP %s: %s", method, path, exc.code, err)
            code = str(err.get("code") or "generation_failed")
            if exc.code == 400:
                raise EngineError(f"the voice model rejected the request: {str(err.get('message'))[:200]}",
                                  code="invalid_request") from exc
            if exc.code == 507:
                raise EngineError("the media node ran out of GPU memory while speaking; try a shorter script",
                                  code="out_of_memory", retryable=True) from exc
            raise EngineError("the voice model could not render this line", code=code, retryable=True) from exc
        except (OSError, ValueError) as exc:
            raise EngineError("the voice engine is not reachable", code="engine_unreachable",
                              retryable=True) from exc

    def health(self, timeout: float = 5) -> dict:
        return self._call("GET", "/health", timeout=timeout)

    def synthesize(self, params: dict) -> dict:
        result = self._call("POST", "/synthesize", params, timeout=self.cfg.utterance_timeout_s)
        variant = params.get("variant")
        with self._lock:
            if variant in self.variants_loaded:
                self.variants_loaded.remove(variant)
            self.variants_loaded.append(variant)
            self.variants_loaded = self.variants_loaded[-self.cfg.engine_max_resident:]
            if self.state == READY:
                self.measure_resident()
        self.touch()
        return result

    def load_variant(self, variant: str) -> dict:
        if variant not in VARIANTS:
            raise EngineError("unknown voice model variant", code="invalid_request")
        result = self._call("POST", "/variants/load", {"variant": variant}, timeout=self.cfg.engine_start_timeout)
        with self._lock:
            self.variants_loaded = list(result.get("loaded") or [])
            self.measure_resident()
        self.touch()
        return result

    # ------------------------------------------------------------ media --
    def media_tool(self, tool: str, args: list[str], timeout: float = 300) -> subprocess.CompletedProcess:
        """ffmpeg/ffprobe: inside the running engine when there is one (fast),
        otherwise in a throwaway, network-less, GPU-less container of the same image."""
        c = self.cfg
        if self.state == READY and self.docker.running(c.engine_container):
            return self.docker.run(["exec", c.engine_container, tool, *args], timeout=timeout)
        return self.docker.run([
            "run", "--rm", "--network", "none", "--user", f"{os.getuid()}:{os.getgid()}",
            "--memory", "2g", "--cpus", "4", "--entrypoint", tool,
            "-v", f"{c.data_root}:{ENGINE_WORK}", c.image, *args,
        ], timeout=timeout)

    def probe(self, container_path: str) -> dict:
        r = self.media_tool("ffprobe", ["-v", "error", "-print_format", "json", "-show_streams",
                                        "-show_format", container_path], timeout=60)
        try:
            info = json.loads(r.stdout) if r.returncode == 0 else None
        except ValueError:
            info = None
        streams = [s for s in (info or {}).get("streams", []) if s.get("codec_type") == "audio"]
        if not streams:
            raise EngineError("the audio file could not be decoded", code="invalid_source")
        s = streams[0]
        duration = float((info or {}).get("format", {}).get("duration") or s.get("duration") or 0)
        return {"duration_s": round(duration, 3), "sample_rate": int(s.get("sample_rate") or 0),
                "channels": int(s.get("channels") or 0), "codec": s.get("codec_name")}


class GuardAdapter:
    """Binds the shared node admission guard (legenex/orchestrator)."""

    on_admitted: Callable[[], None] | None = None

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        if str(cfg.orchestrator_dir) not in sys.path:
            sys.path.insert(0, str(cfg.orchestrator_dir))
        from gx_orchestrator import resource_guard as rg  # noqa: PLC0415

        self.rg = rg

    def launch(self, start: Callable[[], None], extra_gib: float = 0.0) -> None:
        """MemAvailable - (estimate + other tenants' pending growth) must keep the reserve."""
        rg, c = self.rg, self.cfg
        try:
            with rg.guard_launch(c.node, WORKLOAD_NAME, rg.WorkloadClass.SMALL,
                                 c.engine_estimate_gib + max(0.0, extra_gib),
                                 state_dir=c.guard_dir, reserve_gib=c.reserve_gib, lock_timeout=30) as (_, ledger):
                if self.on_admitted is not None:
                    self.on_admitted()
                start()
                ledger.add(WORKLOAD_NAME, node=c.node, workload_class=rg.WorkloadClass.SMALL,
                           estimated_gib=c.engine_estimate_gib, container=c.engine_container)
        except rg.AdmissionRefused as exc:
            raise ResourceWait(_human_refusal(exc.result.reason)) from exc
        except rg.NodeLockBusy as exc:
            raise ResourceWait("another model is being started on this node", code="node_busy") from exc

    def register(self) -> None:
        rg, c = self.rg, self.cfg
        ledger = rg.ResidencyLedger(Path(c.guard_dir) / f"{c.node}-residency.json")
        ledger.add(WORKLOAD_NAME, node=c.node, workload_class=rg.WorkloadClass.SMALL,
                   estimated_gib=c.engine_estimate_gib, container=c.engine_container)

    def release(self) -> None:
        try:
            self.rg.release_workload(Path(self.cfg.guard_dir), self.cfg.node, WORKLOAD_NAME)
        except Exception:  # noqa: BLE001 - release must never mask a teardown
            log.exception("ledger release failed (reconcile will clear it)")


def _human_refusal(reason: str) -> str:
    if "maintenance" in reason.lower():
        return "the cluster is in Maintenance mode; voice resumes when Maintenance ends"
    if "reserve" in reason:
        return "waiting for memory on the media node (other models are using it)"
    if "large/exclusive" in reason:
        return "waiting for another large model on the media node to finish"
    return "waiting for resources on the media node"
