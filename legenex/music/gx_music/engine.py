"""Engine lifecycle (docker) and the loopback client for upstream ACE-Step.

Load = admission-guarded ``docker run`` of the engine container.
Unload = ``docker stop`` + ``rm`` + ledger release. There is no "soft unload"
that leaves weights in a live process: stopping the container is the only way
to guarantee unified memory really comes back on GB10.
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

from .config import ENGINE_TMP, Config
from .errors import EngineError, ResourceWait

log = logging.getLogger("gx_music.engine")

UNLOADED, LOADING, READY, UNLOADING, FAILED = "unloaded", "loading", "ready", "unloading", "failed"
WORKLOAD_NAME = "gx-music"


def meminfo() -> dict[str, float]:
    out: dict[str, float] = {}
    try:
        with open("/proc/meminfo", encoding="ascii") as fh:
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
    def __init__(self, cfg: Config, docker: Docker | None = None,
                 guard: "GuardAdapter | None" = None, clock: Callable[[], float] = time.time) -> None:
        self.cfg = cfg
        self.docker = docker or Docker()
        self.guard = guard or GuardAdapter(cfg)
        self.guard.on_admitted = self._record_admission
        self.clock = clock
        self._lock = threading.RLock()
        self.state = UNLOADED
        self.state_detail = ""
        self.loaded_at: float | None = None
        self.last_load_seconds: float | None = None
        self.last_unload: dict | None = None
        self.last_activity = clock()
        #: D-038: MemAvailable when the load was admitted, and how much the
        #: engine really took once ready (None until measured)
        self.admit_avail_gib: float | None = None
        self.loaded_gib: float | None = None
        self.last_wait: dict | None = None
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
            fh.write(f"ACESTEP_API_KEY={self._key}\n")

    # ----------------------------------------------------------- policy --
    def gxmax_block_reason(self) -> str | None:
        """Is gx-max acquiring or holding node 2?

        Signals, any of which blocks a load (and forces an unload):
          * the rank1 container exists in any state;
          * gx-max's node-2 deadman is alive;
          * the node-2 control plane (llama-swap) is not running -- gx-max-start's
            drain stops it first, so this covers the drain->launch window
            before any rank container exists (observed 2026-09-17 02:00);
          * an explicit hold file is fresh (written by the gx-max drain).
        """
        if self.docker.exists(self.cfg.gxmax_rank_container):
            return "gx-max is running on the cluster; music resumes when it is released"
        try:
            pid = int(self.cfg.gxmax_deadman_pidfile.read_text().strip())
            os.kill(pid, 0)
            return "gx-max is running on the cluster; music resumes when it is released"
        except (OSError, ValueError):
            pass
        if self.cfg.control_plane_container and not self.docker.running(self.cfg.control_plane_container):
            return "the media node is drained (gx-max or maintenance); music resumes when it is restored"
        hold = self.cfg.gxmax_hold_file
        try:
            age = self.clock() - hold.stat().st_mtime
        except OSError:
            return None
        if age <= self.cfg.gxmax_hold_ttl_s:
            return "gx-max is starting on the cluster; music resumes when it is released"
        log.warning("ignoring stale gx-max hold %s (age %.0fs, no rank container)", hold, age)
        return None

    def maintenance_reason(self) -> str | None:
        """Maintenance mode (D-036): no new engine loads; an idle engine is unloaded."""
        if self.cfg.maintenance_hold_file.exists():
            return "the cluster is in Maintenance mode; music resumes when Maintenance ends"
        return None

    def policy_block_reason(self) -> tuple[str, str] | None:
        """(code, reason) for anything that forbids a NEW engine load, gx-max first."""
        block = self.gxmax_block_reason()
        if block:
            return "gx_max_active", block
        block = self.maintenance_reason()
        if block:
            return "maintenance", block
        return None

    def pinned(self) -> bool:
        """Pinned in the Control Center: keep the engine past the idle timer."""
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
    def snapshot(self) -> dict:
        with self._lock:
            return {
                "state": self.state,
                "detail": self.state_detail,
                "container": self.cfg.engine_container,
                "loaded_at": self.loaded_at,
                "last_load_seconds": self.last_load_seconds,
                "last_unload": self.last_unload,
                "idle_seconds": round(self.clock() - self.last_activity, 1),
                "idle_unload_after_s": self.cfg.idle_unload_s,
                "pinned": self.pinned(),
                "pin_honoured": self.pin_honoured() if self.state == READY else None,
                "blocked_by": (self.policy_block_reason() or (None, None))[1],
                "memory": self.memory_view(),
                "last_wait": self.last_wait,
            }

    def pending_gib(self) -> float:
        """Growth MemAvailable does not show yet (published for the media router).

        loading: the estimate minus what has disappeared since admission;
        ready:   the estimate minus the measured resident size (generation headroom);
        otherwise nothing.
        """
        est = self.cfg.engine_estimate_gib
        if self.state == LOADING:
            if self.admit_avail_gib is None:
                return est
            now = meminfo().get("MemAvailable")
            consumed = 0.0 if now is None else max(0.0, self.admit_avail_gib - now)
            return round(max(0.0, est - consumed), 1)
        if self.state == READY:
            return round(max(0.0, est - (self.loaded_gib if self.loaded_gib is not None else 0.0)), 1)
        return 0.0

    def memory_view(self) -> dict:
        return {"estimate_gib": self.cfg.engine_estimate_gib, "loaded_gib": self.loaded_gib,
                "pending_gib": self.pending_gib(), "reserve_gib": self.cfg.reserve_gib}

    def touch(self) -> None:
        self.last_activity = self.clock()

    def _record_admission(self) -> None:
        self.admit_avail_gib = meminfo().get("MemAvailable")
        self.loaded_gib = None

    def is_loaded(self) -> bool:
        return self.state == READY

    def reconcile(self) -> None:
        """Adopt or forget a container left by a previous supervisor."""
        with self._lock:
            if self.state in (LOADING, UNLOADING):
                return  # a transition owned by another thread is in progress
            running = self.docker.running(self.cfg.engine_container)
            if running:
                try:
                    h = self.health()
                except EngineError:
                    h = {}
                if h.get("models_initialized"):
                    self.state, self.state_detail = READY, "adopted running engine"
                    self.loaded_at = self.loaded_at or self.clock()
                    self.guard.register()
                    return
                log.warning("engine container present but not ready; removing it")
                self._teardown("reconcile: unhealthy leftover")
            elif self.state in (READY, LOADING):
                self.state, self.state_detail = UNLOADED, "engine container disappeared"
                self.guard.release()
            elif self.docker.exists(self.cfg.engine_container):
                self.docker.run(["rm", "-f", self.cfg.engine_container])

    # ------------------------------------------------------------- load --
    def ensure_loaded(self, on_phase: Callable[[str], None] = lambda _: None) -> float | None:
        """Return seconds spent loading, or None if it was already loaded."""
        deadline = self.clock() + self.cfg.engine_start_timeout + 120
        while self.state in (LOADING, UNLOADING):  # another thread owns a transition
            if self.clock() > deadline:
                raise EngineError("the music model is stuck in a lifecycle transition", retryable=True)
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
            self._admit_and_start()
            self.state, self.state_detail = LOADING, "loading ACE-Step weights"
            on_phase("loading")
        try:
            self._wait_ready(started)
        except Exception:
            with self._lock:
                self._teardown("load failed")
                self.state, self.state_detail = FAILED, "the music model failed to load"
            raise
        with self._lock:
            self.state, self.state_detail = READY, ""
            self.loaded_at = self.clock()
            self.last_load_seconds = round(self.loaded_at - started, 1)
            now = meminfo().get("MemAvailable")
            if self.admit_avail_gib is not None and now is not None:
                self.loaded_gib = round(min(self.cfg.engine_estimate_gib,
                                            max(0.0, self.admit_avail_gib - now)), 1)
            self.touch()
            return self.last_load_seconds

    def media_state(self) -> dict:
        """The media router's open /health: busy, resident alias and the growth of
        its running job that is not in MemAvailable yet (D-038)."""
        url = self.cfg.media_router_url
        if not url:
            return {}
        try:
            with urllib.request.urlopen(f"{url}/health", timeout=3) as r:
                body = json.load(r)
        except (OSError, ValueError):
            return {"reachable": False}
        if not isinstance(body, dict):
            return {"reachable": False}
        mem = body.get("memory") if isinstance(body.get("memory"), dict) else {}
        pending = mem.get("pending_gib")
        if not isinstance(pending, (int, float)):
            # an older router: a cold job that has just started may not show yet
            pending = float(mem.get("need_gib", {}).get("video", 0) if body.get("busy") else 0) \
                if isinstance(mem.get("need_gib"), dict) else 0.0
        return {"reachable": True, "busy": bool(body.get("busy")), "held_by": body.get("held_by"),
                "resident_alias": body.get("resident_alias"), "pending_gib": max(0.0, float(pending)),
                "waiting": len(body.get("waiting") or [])}

    def _admit_and_start(self) -> None:
        tried_free = False
        while True:
            media = self.media_state()
            extra = float(media.get("pending_gib") or 0.0)
            try:
                self.guard.launch(self._start_container, extra_gib=extra)
                self.last_wait = None
                return
            except ResourceWait as wait:
                if wait.code == "insufficient_memory":
                    wait = ResourceWait(self._memory_wait_reason(media, extra), code=wait.code)
                    self.last_wait = {"at": self.clock(), "reason": wait.reason, "media": media}
                if self.cfg.evict_comfy and not tried_free and wait.code == "insufficient_memory" \
                        and self._free_media():
                    tried_free = True
                    time.sleep(5)
                    continue
                raise wait from None

    def _memory_wait_reason(self, media: dict, extra: float) -> str:
        """A specific, numeric reason for a music load that would break the reserve."""
        c = self.cfg
        avail = meminfo().get("MemAvailable")
        need = c.engine_estimate_gib + extra + c.reserve_gib
        numbers = (f"gx-music needs about {c.engine_estimate_gib:.0f} GiB plus the {c.reserve_gib:.0f} GiB reserve"
                   + (f" plus {extra:.0f} GiB that the running media job has not taken yet" if extra >= 0.5 else "")
                   + f", so {need:.0f} GiB must be available"
                   + (f"; {avail:.0f} GiB is" if avail is not None else ""))
        holder = str(media.get("held_by") or "")
        if media.get("busy") and holder.startswith("video"):
            return f"Waiting for gx-video to finish on gx10-02: {numbers}"
        if media.get("busy") and holder.startswith("image"):
            return f"Waiting for gx-image to finish on gx10-02: {numbers}"
        if media.get("resident_alias"):
            return f"Waiting for {media['resident_alias']} to release gx10-02 memory: {numbers}"
        return f"Waiting for gx10-02 memory (gx-reason or another tenant holds it): {numbers}"

    def _free_media(self) -> bool:
        """Ask the media router to hand over IDLE ComfyUI weights.

        Goes through the router's own free path (the one gx-reason's start uses),
        so the router's generation slot is respected and its resident-model
        bookkeeping stays true. Calling ComfyUI /free directly would leave the
        router judging the next cold image/video job as warm (its small warm growth
        instead of 57-72 GiB plus the reserve) and admitting it into memory that is
        not there.
        """
        name = self.cfg.media_router_container
        if not name:
            return False
        try:
            r = self.docker.run(["exec", name, "python", "-m", "gx_media_router.free_node"], timeout=60)
            lines = r.stdout.strip().splitlines() if r.returncode == 0 else []
            body = json.loads(lines[-1]) if lines else {}
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return False
        if body.get("freed"):
            log.info("media router freed idle ComfyUI weights %s before loading gx-music", body.get("models"))
            return True
        log.info("media router did not free ComfyUI (%s); waiting for memory", body.get("reason", "unreachable"))
        return False

    def _start_container(self) -> None:
        c = self.cfg
        self.docker.run(["rm", "-f", c.engine_container], timeout=60)
        args = [
            "run", "-d", "--name", c.engine_container, "--init",
            "--label", "gx.workload=gx-music", "--label", "gx.managed-by=gx-music-supervisor",
            "--device", "nvidia.com/gpu=all",
            "--shm-size", "8g",
            "--memory", c.engine_memory_cap,
            "--oom-score-adj", "900",
            "--user", f"{os.getuid()}:{os.getgid()}",
            "-p", f"127.0.0.1:{c.engine_port}:{c.engine_port}",
            "--env-file", str(self._envfile),
            "-e", "ACESTEP_API_HOST=0.0.0.0",
            "-e", f"ACESTEP_API_PORT={c.engine_port}",
            "-e", f"ACESTEP_CONFIG_PATH={c.model.dit_name}",
            "-e", f"ACESTEP_LM_MODEL_PATH={c.model.lm_name}",
            "-e", "ACESTEP_INIT_LLM=true",
            "-e", "ACESTEP_NO_INIT=false",  # upstream default is lazy load; we load eagerly
            "-e", f"ACESTEP_LM_BACKEND={c.engine_lm_backend}",
            "-e", "ACESTEP_CHECKPOINTS_DIR=/app/checkpoints",
            "-e", f"ACESTEP_TMPDIR={ENGINE_TMP}",
            # upstream only accepts absolute audio paths under tempfile.gettempdir()
            "-e", f"TMPDIR={ENGINE_TMP}", "-e", f"TEMP={ENGINE_TMP}", "-e", f"TMP={ENGINE_TMP}",
            "-e", "MPLCONFIGDIR=/work/cache/mpl",
            "-v", f"{c.checkpoints_dir}:/app/checkpoints",
            "-v", f"{c.data_root}:{ENGINE_TMP}",
            c.model.image,
        ]
        r = self.docker.run(args, timeout=120)
        if r.returncode != 0:
            log.error("docker run failed: %s", r.stderr.strip()[-2000:])
            raise EngineError("the music engine container could not be started")

    def _wait_ready(self, started: float) -> None:
        deadline = started + self.cfg.engine_start_timeout
        while self.clock() < deadline:
            block = self.gxmax_block_reason()
            if block:
                log.warning("gx-max claimed node 2 while gx-music was loading; aborting the load")
                raise ResourceWait(block, code="gx_max_active")
            if not self.docker.running(self.cfg.engine_container):
                self._log_tail("engine exited during load")
                raise EngineError("the music engine stopped while loading the model")
            try:
                h = self.health(timeout=5)
                if h.get("models_initialized") and h.get("llm_initialized"):
                    return
            except EngineError:
                pass
            time.sleep(3)
        self._log_tail("engine load timeout")
        raise EngineError("the music model took too long to load")

    def _log_tail(self, why: str) -> None:
        r = self.docker.run(["logs", "--tail", "60", self.cfg.engine_container], timeout=30)
        log.error("%s; engine log tail:\n%s%s", why, r.stdout[-6000:], r.stderr[-6000:])

    # ----------------------------------------------------------- unload --
    def unload(self, reason: str) -> dict:
        with self._lock:
            before = meminfo()
            t0 = self.clock()
            self.state, self.state_detail = UNLOADING, reason
            self._teardown(reason)
            # Unified memory is returned asynchronously; give it a moment.
            time.sleep(3)
            after = meminfo()
            self.state, self.state_detail = UNLOADED, ""
            self.loaded_at = None
            self.admit_avail_gib = None
            self.loaded_gib = None
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
            self.docker.run(["stop", "-t", "30", name], timeout=90)
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
                payload = json.load(r)
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:2000].decode("utf-8", "replace")
            log.error("engine %s %s -> HTTP %s: %s", method, path, exc.code, detail)
            raise EngineError(f"the music engine rejected the request (HTTP {exc.code})") from exc
        except (OSError, ValueError) as exc:
            raise EngineError("the music engine is not reachable", retryable=True) from exc
        if isinstance(payload, dict) and "code" in payload and "data" in payload:
            if payload.get("code") != 200:
                log.error("engine %s %s error payload: %s", method, path, str(payload)[:2000])
                raise EngineError("the music engine reported an error")
            return payload["data"]
        return payload

    def health(self, timeout: float = 5) -> dict:
        return self._call("GET", "/health", timeout=timeout)

    def submit(self, params: dict) -> str:
        body = dict(params)
        body["lm_backend"] = self.cfg.engine_lm_backend
        data = self._call("POST", "/release_task", body, timeout=60)
        task_id = data.get("task_id") if isinstance(data, dict) else None
        if not task_id:
            raise EngineError("the music engine did not accept the job")
        self.touch()
        return task_id

    def poll(self, task_id: str) -> dict:
        data = self._call("POST", "/query_result", {"task_id_list": [task_id]}, timeout=30)
        if not data:
            raise EngineError("the music engine lost track of the job")
        item = data[0]
        try:
            results = json.loads(item.get("result") or "[]")
        except ValueError:
            results = []
        self.touch()
        return {"status": item.get("status"), "results": results if isinstance(results, list) else [],
                "progress_text": item.get("progress_text") or ""}

    def create_sample(self, *, query: str, instrumental: bool, vocal_language: str,
                      temperature: float | None) -> dict:
        """The 5Hz LM's "Simple Mode" (upstream /v1/create_sample): caption,
        lyrics and metadata from a description, with an EXPLICIT instrumental
        flag (release_task's sample mode guesses it from the words instead)."""
        body = {"query": query, "instrumental": bool(instrumental), "vocal_language": vocal_language or "unknown"}
        if temperature is not None:
            body["temperature"] = temperature
        data = self._call("POST", "/v1/create_sample", body, timeout=900)
        if not isinstance(data, dict):
            raise EngineError("the music planner returned nothing", code="plan_failed", retryable=True)
        self.touch()
        return data

    def understand(self, container_path: str) -> str:
        """Queue ACE-Step's own audio understanding (audio -> 5Hz codes -> LM):
        caption, lyrics, BPM, key, time signature, language. Returns a task id."""
        return self.submit({
            "task_type": "text2music", "full_analysis_only": True, "src_audio_path": container_path,
            "prompt": "", "lyrics": "", "thinking": False, "use_cot_caption": False,
            "use_cot_language": False, "use_random_seed": False, "seed": 0,
            "model": self.cfg.model.dit_name, "audio_format": "wav32",
        })

    # ------------------------------------------------------------ media --
    def analysis_tool(self, container_path: str, timeout: float = 420) -> dict:
        """Measured acoustic analysis (analysis_dsp.py) in a throw-away,
        network-less, GPU-less container from the engine image."""
        c = self.cfg
        script = Path(__file__).resolve().with_name("analysis_dsp.py")
        r = self.docker.run([
            "run", "--rm", "--network", "none", "--user", f"{os.getuid()}:{os.getgid()}",
            "--memory", "4g", "--cpus", "4", "--read-only", "--tmpfs", "/tmp:rw,size=64m",
            "--label", "gx.workload=gx-music-analysis", "--entrypoint", c.analysis_python,
            "-v", f"{script}:/gxm/analysis_dsp.py:ro", "-v", f"{c.data_root}:{ENGINE_TMP}:ro",
            c.model.image, "/gxm/analysis_dsp.py", container_path,
        ], timeout=timeout)
        lines = (r.stdout or "").strip().splitlines()
        try:
            data = json.loads(lines[-1]) if lines else {}
        except ValueError:
            data = {}
        if r.returncode != 0 or not isinstance(data, dict) or "error" in data or "tempo" not in data:
            log.error("analysis helper failed rc=%s: %s %s", r.returncode, (r.stdout or "")[-800:],
                      (r.stderr or "")[-1500:])
            reason = data.get("error") if isinstance(data, dict) and isinstance(data.get("error"), str) else None
            raise EngineError(reason or "the audio could not be analysed", code="analysis_failed")
        return data

    def media_tool(self, tool: str, args: list[str], timeout: float = 300) -> subprocess.CompletedProcess:
        """Run ffmpeg/ffprobe in a throwaway, network-less, GPU-less container
        from the engine image, so transcoding never needs the model loaded."""
        c = self.cfg
        return self.docker.run([
            "run", "--rm", "--network", "none", "--user", f"{os.getuid()}:{os.getgid()}",
            "--memory", "4g", "--cpus", "4", "--entrypoint", tool,
            "-v", f"{c.data_root}:{ENGINE_TMP}", c.model.image, *args,
        ], timeout=timeout)

    def probe(self, container_path: str) -> dict:
        r = self.media_tool("ffprobe", ["-v", "error", "-print_format", "json", "-show_streams",
                                        "-show_format", container_path], timeout=60)
        if r.returncode != 0:
            raise EngineError("the audio file could not be decoded", code="invalid_source")
        try:
            info = json.loads(r.stdout)
        except ValueError as exc:
            raise EngineError("the audio file could not be decoded", code="invalid_source") from exc
        streams = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
        if not streams:
            raise EngineError("the file has no audio stream", code="invalid_source")
        s = streams[0]
        duration = float(info.get("format", {}).get("duration") or s.get("duration") or 0)
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
        """Admit the engine: MemAvailable - (estimate + other tenants' pending growth)
        must keep the reserve. The ledger records the engine's own estimate."""
        rg, c = self.rg, self.cfg
        try:
            with rg.guard_launch(c.node, WORKLOAD_NAME, rg.WorkloadClass.MEDIUM,
                                 c.engine_estimate_gib + max(0.0, extra_gib),
                                 state_dir=c.guard_dir, reserve_gib=c.reserve_gib, lock_timeout=30) as (_, ledger):
                if self.on_admitted is not None:
                    self.on_admitted()
                start()
                ledger.add(WORKLOAD_NAME, node=c.node, workload_class=rg.WorkloadClass.MEDIUM,
                           estimated_gib=c.engine_estimate_gib, container=c.engine_container)
        except rg.AdmissionRefused as exc:
            raise ResourceWait(_human_refusal(exc.result.reason)) from exc
        except rg.NodeLockBusy as exc:
            raise ResourceWait("another model is being started on this node", code="node_busy") from exc

    def register(self) -> None:
        rg, c = self.rg, self.cfg
        ledger = rg.ResidencyLedger(Path(c.guard_dir) / f"{c.node}-residency.json")
        ledger.add(WORKLOAD_NAME, node=c.node, workload_class=rg.WorkloadClass.MEDIUM,
                   estimated_gib=c.engine_estimate_gib, container=c.engine_container)

    def release(self) -> None:
        try:
            self.rg.release_workload(Path(self.cfg.guard_dir), self.cfg.node, WORKLOAD_NAME)
        except Exception:  # noqa: BLE001 - release must never mask a teardown
            log.exception("ledger release failed (reconcile will clear it)")


def _human_refusal(reason: str) -> str:
    if "maintenance" in reason.lower():
        return "the cluster is in Maintenance mode; music resumes when Maintenance ends"
    if "reserve" in reason:
        return "waiting for memory on the media node (other models are using it)"
    if "large/exclusive" in reason:
        return "waiting for another large model on the media node to finish"
    return "waiting for resources on the media node"
