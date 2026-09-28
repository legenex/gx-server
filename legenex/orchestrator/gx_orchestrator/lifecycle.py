"""gx-max (DeepSeek V4.1 Flash EXL3) lifecycle: a single-writer state machine.

The model is served by the Mia kit (submodule mia-dsv41, vLLM-based, API on
the head node's loopback :8888). Bringing it up takes over BOTH nodes, so
acquisition must be serialised: concurrent callers queue behind one
acquisition rather than each launching their own.

States
------
    DOWN      -> nothing running; nodes are free for normal workloads
    ACQUIRING -> a single worker thread is running the Mia start script
    READY     -> the engine is healthy, serving the right model id, and has
                 answered one REAL completion probe (17*19=323)
    RELEASING -> a worker thread is draining requests and running stop.sh

There is exactly ONE model. "Acquiring a profile" means launching the same
kit with a different env overlay (max_num_seqs / speculation / window), so a
profile switch while READY is a full drain + stop + start -- never a hot
reconfigure. NO auto-start at boot: this module never self-acquires; only an
explicit request (or operator) starts the engine.

Transitions are guarded by one lock plus a condition variable. Callers block
on the condition rather than polling, and every waiter is woken on a state
change.
"""

from __future__ import annotations

import collections
import enum
import json
import logging
import os
import re
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .profiles import ModelSpec, ProfileSpec, Registry, RuntimeSpec

log = logging.getLogger("gx.lifecycle")


class State(str, enum.Enum):
    DOWN = "down"
    ACQUIRING = "acquiring"
    READY = "ready"
    RELEASING = "releasing"


class AcquisitionError(RuntimeError):
    """gx-max could not be brought up. Never downgrade silently -- raise."""


#: Fine-grained, READ-ONLY progress within ACQUIRING / RELEASING. Derived from
#: the section markers gx-max-start.sh / gx-max-stop.sh print around the Mia
#: kit's own output, so it never influences a transition -- `State` stays the
#: only thing the state machine acts on. Exposed for operators (Jobs page).
PHASE_IDLE = "idle"
PHASE_SERVING = "serving"
PHASE_FAILED = "failed"

#: (compiled pattern, phase) in the order the wrapper scripts print them.
_START_MARKERS: tuple[tuple["re.Pattern[str]", str], ...] = tuple(
    (re.compile(p), ph)
    for p, ph in (
        (r"=== gx-max preflight ===", "preflight"),
        (r"=== profile overlay ===", "overlay"),
        (r"=== starting the Mia kit \(.*\) ===", "loading"),
        (r"=== waiting for the model to become healthy", "warming"),
        (r"=== gx-max READY", "ready"),
        (r"start failed .* running the two-node unwind", "unwinding"),
    )
)
_STOP_MARKERS: tuple[tuple["re.Pattern[str]", str], ...] = tuple(
    (re.compile(p), ph)
    for p, ph in (
        (r"draining: waiting", "draining_requests"),
        (r"stopping the Mia containers", "stopping_containers"),
        (r"MemAvailable after release", "memory_recovery"),
        (r"gx-max released; both nodes are back", "released"),
    )
)
_STARTUP_SECONDS_RE = re.compile(r"=== gx-max READY .* after (\d+)s ===")

#: Lines kept in memory for /lifecycle/gx-max/events.
_EVENT_BUFFER = 400
#: Job records kept (memory and the optional history file).
_HISTORY_KEEP = 25

#: MemAvailable must return to within this many GiB of the pre-start level on
#: BOTH nodes before a release is called clean. The engine pins ~105 GiB per
#: node; anything still resident is a leak, not noise.
_MEM_RETURN_TOLERANCE_GIB = 5.0

#: The completion probe: a real multiplication the model must answer. A proxy
#: that answers /health but cannot generate is NOT ready (this is the
#: project's "never declare healthy without real inference" rule).
_PROBE_QUESTION = "17*19="
_PROBE_EXPECT = "323"
_PROBE_MAX_TOKENS = 32


@dataclass
class LifecycleStatus:
    state: State
    since: float
    last_used: float | None
    waiters: int
    detail: str = ""
    last_error: str = ""
    phase: str = PHASE_IDLE
    phase_since: float = 0.0
    last_startup_seconds: int | None = None
    idle_ttl: int = 0
    in_flight: int = 0
    profile: str = ""

    def as_dict(self) -> dict:
        now = time.time()
        ttl_remaining = None
        if self.state is State.READY and self.idle_ttl > 0 and self.last_used:
            if self.in_flight:
                ttl_remaining = self.idle_ttl
            else:
                ttl_remaining = max(0, round(self.idle_ttl - (now - self.last_used)))
        return {
            "state": self.state.value,
            "seconds_in_state": round(now - self.since, 1),
            "idle_seconds": round(now - self.last_used, 1) if self.last_used else None,
            "waiters": self.waiters,
            "detail": self.detail,
            "last_error": self.last_error,
            # Additive, read-only progress detail (never drives a transition).
            "phase": self.phase,
            "phase_seconds": round(now - self.phase_since, 1) if self.phase_since else None,
            "last_startup_seconds": self.last_startup_seconds,
            "idle_ttl": self.idle_ttl,
            # Keep-warm: requests in progress hold the engine; the TTL counts
            # from the end of the last one.
            "in_flight": self.in_flight,
            "ttl_remaining_seconds": ttl_remaining,
            # The serving profile (empty while DOWN/never-acquired).
            "profile": self.profile,
        }


def build_env_overlay(
    profile: ProfileSpec,
    model: ModelSpec,
    runtime: RuntimeSpec,
    *,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The env overlay for one (profile, model) launch of the Mia kit.

    Pure function of registry facts -- no defaults from thin air: every value
    comes from the ProfileSpec, the ModelSpec's serving_notes, the runtime's
    served id, or the fixed fabric pin below. The Mia kit's start.sh reads
    exactly these env names (see state/MIA-RUNTIME.md "Launch env knobs").
    """
    env: dict[str, str] = {
        # Identical id on head and worker; verified against /v1/models later.
        "SERVED_MODEL_NAME": runtime.served_model_id,
        # Profile shape.
        "MAX_NUM_SEQS": str(profile.max_num_seqs),
        "MAX_MODEL_LEN": str(profile.max_model_len),
        "SPEC_METHOD": profile.spec_method,
        # Weights + Engram tables come from the registry model entry. The
        # Engram shards (47+48) are NEVER copied into the EXL3 tree.
        "MODEL_HOST": model.path,
        "ENGRAM_DIR": model.engram_dir,
        # Proven fabric pin (state/EVIDENCE.md): per-NIC GID index, both rails.
        "NCCL_IB_GID_INDEX": "3",
        # Worker weight sync over LAN SSH (NFS is not running on this cluster
        # and there is no sudo to start it; rsync needs neither).
        "WEIGHT_SYNC": "rsync",
    }
    if profile.spec_method == "dspark" and profile.dspark_tokens:
        env["DSPARK_TOKENS"] = str(profile.dspark_tokens)
    # KV / batching settings from the registry model's serving_notes. Absent
    # notes mean the Mia kit's own defaults are used, verbatim.
    notes = model.serving_notes
    if "gpu_mem_util" in notes:
        env["GPU_MEM_UTIL"] = str(notes["gpu_mem_util"])
    if "kv_bytes" in notes:
        env["KV_CACHE_MEMORY_BYTES"] = str(notes["kv_bytes"])
    if "max_num_batched_tokens" in notes:
        env["MAX_NUM_BATCHED_TOKENS"] = str(notes["max_num_batched_tokens"])
    if "vllm_sparse_indexer_max_logits_mb" in notes:
        env["DSV41_SPARSE_INDEXER_MAX_LOGITS_MB"] = str(notes["vllm_sparse_indexer_max_logits_mb"])
    if extra:
        env.update({k: str(v) for k, v in extra.items()})
    return env


def default_liveness_probe(api_base: str, model_id: str, *, timeout: float = 10.0) -> bool:
    """Cheap, ongoing liveness: two facts, neither touching the generation queue.

    1. GET {api_base-without-/v1}/health answers 2xx.
    2. GET {api_base}/models lists `model_id` (a proxy up with the wrong
       model is a config fault, not a ready engine).

    Deliberately does NOT send a completion request (see `default_ready_probe`
    for why that matters here): this is what `GxMaxLifecycle._reconcile()` polls
    every `_RECONCILE_INTERVAL` seconds, including inline on real user request
    paths, so it must never be able to queue behind real inference traffic.
    """
    base = api_base.rstrip("/")
    try:
        with urllib.request.urlopen(f"{base.removesuffix('/v1')}/health", timeout=timeout) as resp:
            if not 200 <= resp.status < 300:
                return False
    except Exception:
        return False
    try:
        with urllib.request.urlopen(f"{base}/models", timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
        ids = [str(m.get("id")) for m in body.get("data") or []]
        return model_id in ids
    except Exception:
        return False


def default_ready_probe(api_base: str, model_id: str, *, timeout: float = 10.0) -> bool:
    """READY means three facts, all verified live:

    1-2. Everything `default_liveness_probe` checks (health, model id).
    3. ONE real completion (17*19=323, thinking off, 32 tokens) succeeds.

    This is deliberately more expensive than `default_liveness_probe`: it
    competes for the same generation queue as real requests, so it belongs at
    a boot-time/one-shot transition (an operator's acquire, or adopting an
    engine at orchestrator startup) where nothing else is contending for that
    queue yet -- never on the recurring background health check (B-1 finding,
    2026-09-29 independent review: this probe reused for `_reconcile()`, which
    real inference requests trigger inline, could legitimately queue behind
    concurrent real traffic, time out, and get a perfectly healthy engine
    marked DOWN -- so the *next* real user request was hard-refused with no
    fallback. See `GxMaxLifecycle.__init__`'s `liveness_probe` parameter.)
    """
    if not default_liveness_probe(api_base, model_id, timeout=timeout):
        return False
    base = api_base.rstrip("/")
    payload = json.dumps({
        "model": model_id,
        "messages": [{"role": "user", "content": _PROBE_QUESTION}],
        "chat_template_kwargs": {"enable_thinking": False},
        "max_tokens": _PROBE_MAX_TOKENS,
        "stream": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/chat/completions", data=payload, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
        choices = body.get("choices") or []
        content = str(((choices[0] or {}).get("message") or {}).get("content") or "")
        return _PROBE_EXPECT in content
    except Exception:
        return False


class GxMaxLifecycle:
    """Serialised acquire/release for the two-node DeepSeek V4.1 engine."""

    def __init__(
        self,
        runtime_dir: Path,
        *,
        api_base: str,
        model_id: str,
        registry: Registry,
        idle_ttl: int = 1800,
        acquire_timeout: int = 1800,
        events_log: Path | None = None,
        history_path: Path | None = None,
        ready_probe: Callable[[], bool] | None = None,
        liveness_probe: Callable[[], bool] | None = None,
        read_mem_gib: Callable[[str], "float | None"] | None = None,
        container_running: Callable[[str], bool] | None = None,
        drain_hook: Callable[[float], int] | None = None,
        on_ready: Callable[[str, int], None] | None = None,
        extra_env: Mapping[str, str] | None = None,
        settle_seconds: float = 90.0,
        mem_return_wait_s: float = 60.0,
    ) -> None:
        self._dir = Path(runtime_dir)
        # Observability only: a plain append-only log of every script line and
        # a small JSON job history. Both optional (None in the unit tests).
        self._events_log = Path(events_log) if events_log else None
        self._history_path = Path(history_path) if history_path else None
        self._events: collections.deque[dict] = collections.deque(maxlen=_EVENT_BUFFER)
        self._event_seq = 0
        self._phase = PHASE_IDLE
        self._phase_since = 0.0
        self._job: dict | None = None
        self._history: list[dict] = self._load_history()
        self._api_base = api_base
        self._model_id = model_id
        self._registry = registry
        self._idle_ttl = idle_ttl
        self._acquire_timeout = acquire_timeout
        self._extra_env = dict(extra_env or {})
        #: How long to keep probing after a successful start before giving
        #: up (the script's own health wait plus a margin for the stronger
        #: model-id + completion probe). Injectable so tests stay hermetic.
        self._settle_seconds = settle_seconds
        #: How long to keep re-checking memory return after stop.sh exits.
        self._mem_return_wait_s = mem_return_wait_s

        # Pluggable host facts -- fakes in tests, real I/O here.
        self._ready_probe = ready_probe or (lambda: default_ready_probe(api_base, model_id))
        # Ongoing background health (_reconcile) uses this instead of _ready_probe:
        # a caller that injects only `ready_probe` (every existing test) gets the
        # exact same fake for both, unchanged; production (neither injected) gets
        # the cheap health+model-id check instead of a real completion request
        # competing with live user traffic for the generation queue.
        self._liveness_probe = liveness_probe or ready_probe or (lambda: default_liveness_probe(api_base, model_id))
        self._read_mem_gib = read_mem_gib or _default_read_mem_gib
        self._container_running = container_running or _default_container_running
        #: drain_hook(seconds) waits for in-flight requests (the scheduler)
        #: and returns how many are still active. None => no drainable load.
        self._drain_hook = drain_hook
        #: on_ready(profile_name, max_num_seqs) notifies the scheduler that
        #: the admission capacity changed (profile switch or first boot).
        self._on_ready = on_ready

        self._cv = threading.Condition(threading.RLock())
        self._state = State.DOWN
        self._since = time.time()
        self._last_used: float | None = None
        self._waiters = 0
        self._in_flight = 0
        self._detail = ""
        self._last_error = ""
        self._profile = ""
        self._worker: threading.Thread | None = None
        self._stopping = threading.Event()
        self._last_reconcile = 0.0
        #: MemAvailable per node just before the current/last acquire.
        self._pre_start_mem: dict[str, "float | None"] = {}

        # Adopt reality at startup: the engine may already be running (an
        # operator ran the wrapper by hand). This is NOT an auto-start -- no
        # script is ever run here; only an already-READY engine is adopted.
        if self._ready_probe():
            self._state = State.READY
            self._last_used = time.time()
            self._profile = registry.default_profile().name
            self._detail = "adopted an already-running engine at startup"
            self._phase, self._phase_since = PHASE_SERVING, time.time()
            log.info("gx-max: adopted already-running engine")
            self._fire_on_ready()

        self._reaper = threading.Thread(target=self._idle_reaper, name="gxmax-ttl", daemon=True)
        self._reaper.start()

    #: Minimum interval between reconciliation probes.
    _RECONCILE_INTERVAL = 10.0

    # -------------------------------------------------------------- plumbing
    def _fire_on_ready(self) -> None:
        if self._on_ready is None:
            return
        try:
            max_seqs = self._registry.profile(self._profile).max_num_seqs if self._profile else 1
            self._on_ready(self._profile, max_seqs)
        except Exception as exc:  # noqa: BLE001 - observability must never kill a transition
            log.warning("on_ready callback failed: %r", exc)

    # ------------------------------------------------------------ reconcile
    def _reconcile(self) -> None:
        """Keep the state machine honest about an engine managed elsewhere.

        READY -> DOWN when the engine went away behind our back (an operator
        running the stop wrapper, a crash, a node reboot); DOWN -> READY when
        a healthy engine appears that this process did not start. Never
        touches ACQUIRING/RELEASING: a worker thread owns those.
        """
        with self._cv:
            if self._state not in (State.READY, State.DOWN):
                return
            if time.time() - self._last_reconcile < self._RECONCILE_INTERVAL:
                return
            self._last_reconcile = time.time()
            observed = self._state

        # Probe outside the lock: it does network I/O. Cheap liveness only (health +
        # model id) -- never the full ready_probe here, which sends a real completion
        # that would compete with live user traffic for the same generation queue.
        healthy = self._liveness_probe()

        with self._cv:
            if self._state is not observed:
                return
            if observed is State.READY and not healthy:
                log.warning("gx-max disappeared while marked READY; marking DOWN")
                self._last_used = None
                self._profile = ""
                self._set_state(State.DOWN, "engine vanished (torn down externally)")
                self._set_phase(PHASE_IDLE)
                self._event("reconcile", "engine vanished while READY; marked DOWN")
            elif observed is State.DOWN and healthy:
                log.info("gx-max: adopted an engine started outside the orchestrator")
                self._last_used = time.time()
                self._profile = self._registry.default_profile().name
                self._set_state(State.READY, "adopted an externally started engine")
                self._set_phase(PHASE_SERVING)
                self._event("reconcile", "adopted an engine started outside the orchestrator")
                self._fire_on_ready()

    # ----------------------------------------------------------------- status
    def status(self) -> LifecycleStatus:
        self._reconcile()
        with self._cv:
            return LifecycleStatus(
                state=self._state,
                since=self._since,
                last_used=self._last_used,
                waiters=self._waiters,
                detail=self._detail,
                last_error=self._last_error,
                phase=self._phase,
                phase_since=self._phase_since,
                last_startup_seconds=self._last_startup_seconds(),
                idle_ttl=self._idle_ttl,
                in_flight=self._in_flight,
                profile=self._profile,
            )

    def is_ready(self) -> bool:
        self._reconcile()
        with self._cv:
            return self._state is State.READY

    @property
    def current_profile(self) -> str:
        with self._cv:
            return self._profile

    def mark_used(self) -> None:
        """Record activity so the idle reaper does not release under load."""
        with self._cv:
            self._last_used = time.time()

    def begin_use(self) -> None:
        """A request is being served: the reaper must not release."""
        with self._cv:
            self._in_flight += 1
            self._last_used = time.time()

    def end_use(self) -> None:
        with self._cv:
            self._in_flight = max(0, self._in_flight - 1)
            self._last_used = time.time()

    def _set_state(self, state: State, detail: str = "") -> None:
        with self._cv:
            if state is not self._state:
                log.info("gx-max: %s -> %s (%s)", self._state.value, state.value, detail or "-")
            self._state = state
            self._since = time.time()
            self._detail = detail
            self._cv.notify_all()

    # ---------------------------------------------------------------- acquire
    def acquire(self, profile_name: str | None = None, timeout: float | None = None) -> str:
        """Ensure the model is serving (in `profile_name` if given).

        Blocks until READY. Raises AcquisitionError on failure -- it must
        NEVER fall back to a different model. A profile switch while READY is
        a full drain + stop + start with the new profile's env overlay.
        Returns the serving profile name.
        """
        profile = self._registry.profile(profile_name or self._registry.default_profile().name)
        deadline = time.time() + (timeout if timeout is not None else self._acquire_timeout)

        # Never hand back a stale READY: if the engine died, re-acquire it.
        self._reconcile()

        with self._cv:
            self._waiters += 1
        try:
            while True:
                with self._cv:
                    if self._state is State.READY and self._profile == profile.name:
                        self._last_used = time.time()
                        return profile.name

                    if (
                        self._state is State.READY
                        and self._profile
                        and self._profile != profile.name
                    ):
                        # Profile switch: drain, stop, then start with the new
                        # overlay. The state machine does the sequencing; the
                        # waiters all ride the same transitions.
                        log.info(
                            "gx-max profile switch %s -> %s requested; cycling the engine",
                            self._profile, profile.name,
                        )
                        self._event("switch", f"profile switch {self._profile} -> {profile.name}")
                        self._start_release(force=False, detail=f"switch to {profile.name}")
                        # Loop again; we are now DOWN (or RELEASING->DOWN soon).

                    if self._state is State.DOWN:
                        # We are the one who starts it.
                        self._last_error = ""
                        self._profile = profile.name
                        self._set_state(State.ACQUIRING, f"starting profile '{profile.name}'")
                        self._begin_job("acquire")
                        self._worker = threading.Thread(
                            target=self._do_acquire, args=(profile,), name="gxmax-acquire", daemon=True
                        )
                        self._worker.start()

                    remaining = deadline - time.time()
                    if remaining <= 0:
                        raise AcquisitionError(
                            f"timed out after {self._acquire_timeout}s waiting for gx-max "
                            f"(state={self._state.value}, detail={self._detail}, "
                            f"last_error={self._last_error or 'none'})"
                        )
                    # Wait for any state change.
                    self._cv.wait(timeout=min(remaining, 10.0))

                    if self._state is State.DOWN and self._last_error:
                        raise AcquisitionError(self._last_error)
        finally:
            with self._cv:
                self._waiters -= 1

    def _do_acquire(self, profile: ProfileSpec) -> None:
        model = self._registry.production_model()
        runtime = self._registry.runtime(self._registry.alias("gx-max").runtime)
        overlay = build_env_overlay(profile, model, runtime, extra=self._extra_env)

        # Record the pre-start memory floor for the release-time check.
        self._pre_start_mem = {n: self._read_mem_gib(n) for n in ("node1", "node2")}

        script = self._dir / "start.sh"
        try:
            log.info("gx-max: running %s (profile %s)", script, profile.name)
            proc = self._run_streaming(
                ["bash", str(script)], self._acquire_timeout, _START_MARKERS, "acquire",
                cwd=self._dir, extra_env=overlay,
            )
            if proc.returncode == 0:
                # start.sh waits for its own health, but READY here is a
                # stronger fact (model id + a real completion), so settle
                # for a short window before declaring failure: a transient
                # probe miss must not discard a successful start.
                if self._await_ready(self._settle_seconds):
                    with self._cv:
                        self._last_used = time.time()
                        self._profile = profile.name
                    self._set_phase(PHASE_SERVING)
                    self._end_job("ready")
                    self._set_state(State.READY, f"profile '{profile.name}' healthy")
                    self._event("ready", f"profile {profile.name} passed the completion probe")
                    self._fire_on_ready()
                    return
                err = (
                    f"start.sh exited 0 but the engine did not pass the readiness probe "
                    f"(health + /v1/models + completion) within {self._settle_seconds}s"
                )
            else:
                tail = (proc.stdout or "").strip().splitlines()[-15:]
                err = f"start.sh exited {proc.returncode}: " + " | ".join(tail)
        except subprocess.TimeoutExpired:
            err = f"start.sh exceeded {self._acquire_timeout}s"
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller verbatim
            err = f"start.sh failed: {exc!r}"

        # The kit can fail AFTER a container was launched (docker run -d is
        # detached from the script). Left alone that is ~105 GiB leaked per
        # node with no lease and nothing watching it. Unwind unconditionally
        # on every failure path, best-effort, before reporting the ORIGINAL
        # error to the caller.
        err = self._cleanup_after_failed_acquire(err)

        log.error("gx-max acquisition failed: %s", err)
        with self._cv:
            self._last_error = err
        self._set_phase(PHASE_FAILED)
        self._end_job("failed", error=err)
        with self._cv:
            self._profile = ""
        self._set_state(State.DOWN, "acquisition failed")

    def _await_ready(self, seconds: float) -> bool:
        deadline = time.time() + seconds
        while True:
            if self._ready_probe():
                return True
            if time.time() >= deadline:
                return False
            time.sleep(2.0)

    def _cleanup_after_failed_acquire(self, original_err: str) -> str:
        """Best-effort unwind via stop.sh --force. Never raises and never
        masks `original_err` -- it only appends a note when the cleanup
        itself could not be confirmed."""
        stop_script = self._dir / "stop.sh"
        try:
            proc = subprocess.run(
                ["bash", str(stop_script), "--force"],
                capture_output=True, text=True, timeout=300, cwd=self._dir,
            )
            if proc.returncode != 0:
                tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-10:]
                log.error("gx-max post-failure cleanup exited %s: %s", proc.returncode, " | ".join(tail))
                return (
                    original_err + " [cleanup after failure also failed, exit "
                    f"{proc.returncode} -- a container may still be running, check "
                    "`docker ps` on both nodes]"
                )
            log.info("gx-max post-failure cleanup: stop.sh --force completed")
            self._event("cleanup", "post-failure cleanup: stop.sh --force completed")
        except Exception as exc:  # noqa: BLE001
            log.error("gx-max post-failure cleanup raised: %r", exc)
            return (
                original_err + f" [cleanup after failure raised {exc!r} -- a container "
                "may still be running, check `docker ps` on both nodes]"
            )
        return original_err

    # ---------------------------------------------------------------- release
    def release(self, *, force: bool = False) -> None:
        """Tear the engine down and hand both nodes back."""
        with self._cv:
            if self._state in (State.DOWN, State.RELEASING):
                return
        self._start_release(force=force, detail="forced" if force else "graceful drain")
        # Wait for the RELEASING worker to finish (bounded: it has its own
        # timeouts). Callers of /lifecycle/release get a settled answer.
        deadline = time.time() + 900
        while time.time() < deadline:
            with self._cv:
                if self._state is State.DOWN:
                    return
            time.sleep(0.2)

    def _start_release(self, *, force: bool, detail: str) -> None:
        """Flip to RELEASING and launch the release worker (idempotent)."""
        with self._cv:
            if self._state in (State.DOWN, State.RELEASING):
                return
            self._set_state(State.RELEASING, detail)
            self._begin_job("release_forced" if force else "release")
        threading.Thread(
            target=self._do_release, args=(force,), name="gxmax-release", daemon=True
        ).start()

    def _do_release(self, force: bool) -> None:
        outcome, rel_err = "released", ""
        try:
            # Drain in-flight requests through the scheduler hook first. A
            # forced release skips the wait but still asks the scheduler so
            # its bookkeeping can mark what was cut short.
            if self._drain_hook is not None:
                try:
                    remaining = self._drain_hook(0.0 if force else 300.0)
                    if remaining:
                        self._event(
                            "drain",
                            f"{remaining} request(s) still active after the drain window"
                            + (" (forced)" if force else ""),
                        )
                except Exception as exc:  # noqa: BLE001 - draining must not block release
                    log.warning("drain hook failed: %r", exc)
            elif not force:
                self._event("drain", "no drain hook configured; proceeding")

            args = ["bash", str(self._dir / "stop.sh")]
            if force:
                args.append("--force")
            proc = self._run_streaming(args, 900, _STOP_MARKERS, "release", cwd=self._dir)
            if proc.returncode != 0:
                log.warning("stop.sh exited %s: %s", proc.returncode, (proc.stdout or "")[-500:])
                outcome, rel_err = "release_warning", f"stop.sh exited {proc.returncode}"

            # Verify BOTH containers exited and memory returned to the
            # pre-start level (+- the tolerance) on BOTH nodes.
            mem_ok = self._await_memory_return()
            if not mem_ok:
                outcome = "release_warning" if outcome == "released" else outcome
                rel_err = (rel_err + "; " if rel_err else "") + (
                    "memory did not return to the pre-start level on both nodes "
                    f"(+/- {_MEM_RETURN_TOLERANCE_GIB} GiB)"
                )
        except Exception as exc:  # noqa: BLE001
            log.error("gx-max release failed: %r", exc)
            outcome, rel_err = "release_error", repr(exc)
        finally:
            with self._cv:
                self._last_used = None
                self._profile = ""
            self._set_state(State.DOWN, "released")
            self._set_phase(PHASE_IDLE)
            self._end_job(outcome, error=rel_err)

    def _await_memory_return(self) -> bool:
        """True when both nodes' MemAvailable is back within tolerance.

        Gives the kernel up to `mem_return_wait_s` to actually reclaim the
        unified-memory allocations after the containers exit.
        """
        deadline = time.time() + self._mem_return_wait_s
        while True:
            ok = all(
                self._container_running(node) is False for node in ("node1", "node2")
            )
            if ok:
                for node, pre in self._pre_start_mem.items():
                    if pre is None:
                        continue  # no baseline (probe failed pre-start): cannot judge
                    now = self._read_mem_gib(node)
                    if now is None or abs(now - pre) > _MEM_RETURN_TOLERANCE_GIB:
                        ok = False
                        break
            if ok or time.time() >= deadline:
                self._event(
                    "memory",
                    "MemAvailable after release: node1={}GiB node2={}GiB (pre-start {} / {})".format(
                        self._read_mem_gib("node1"), self._read_mem_gib("node2"),
                        self._pre_start_mem.get("node1"), self._pre_start_mem.get("node2"),
                    ),
                )
                return ok
            time.sleep(5.0)

    # ------------------------------------------------------- observability
    def _set_phase(self, phase: str) -> None:
        with self._cv:
            if phase != self._phase:
                self._phase = phase
                self._phase_since = time.time()
                if self._job is not None:
                    self._job.setdefault("phases", []).append(
                        {"phase": phase, "at": round(self._phase_since, 1)}
                    )

    def _event(self, source: str, line: str) -> None:
        now = time.time()
        with self._cv:
            self._event_seq += 1
            self._events.append({"seq": self._event_seq, "ts": round(now, 3),
                                 "source": source, "line": line[:2000]})
        if self._events_log is not None:
            try:
                with self._events_log.open("a", encoding="utf-8") as fh:
                    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now))
                    fh.write(f"{stamp} [{source}] {line}\n")
            except OSError:
                log.debug("could not append to %s", self._events_log, exc_info=True)

    def _begin_job(self, kind: str) -> None:
        with self._cv:
            self._job = {"kind": kind, "started": round(time.time(), 1), "phases": []}
        self._event("orchestrator", f"--- {kind} started ---")

    def _end_job(self, outcome: str, error: str = "") -> None:
        with self._cv:
            job, self._job = self._job, None
            if job is None:
                return
            job["ended"] = round(time.time(), 1)
            job["elapsed_seconds"] = round(job["ended"] - job["started"], 1)
            # How long each phase took, so a slow start is attributable.
            phases = job.get("phases") or []
            for cur, nxt in zip(phases, phases[1:] + [{"at": job["ended"]}]):
                cur["seconds"] = round(nxt["at"] - cur["at"], 1)
            job["outcome"] = outcome
            if error:
                job["error"] = error[:1000]
            self._history.append(job)
            del self._history[:-_HISTORY_KEEP]
            snapshot = list(self._history)
        self._event("orchestrator", f"--- {job['kind']} finished: {outcome} ---")
        self._save_history(snapshot)

    def _load_history(self) -> list[dict]:
        if self._history_path is None:
            return []
        try:
            data = json.loads(self._history_path.read_text(encoding="utf-8"))
            return [j for j in data if isinstance(j, dict)][-_HISTORY_KEEP:]
        except (OSError, ValueError, TypeError):
            return []

    def _save_history(self, history: list[dict]) -> None:
        if self._history_path is None:
            return
        try:
            self._history_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._history_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(history, indent=1), encoding="utf-8")
            os.replace(tmp, self._history_path)
        except OSError:
            log.warning("could not persist gx-max job history to %s", self._history_path)

    def _last_startup_seconds(self) -> int | None:
        with self._cv:
            for job in reversed(self._history):
                if job.get("kind") == "acquire" and job.get("startup_seconds"):
                    return int(job["startup_seconds"])
        return None

    def events(self, after: int = 0, limit: int = 200) -> dict:
        """Read-only view for operators: recent script lines, the job in
        progress, and finished jobs (newest last)."""
        limit = max(1, min(int(limit), _EVENT_BUFFER))
        with self._cv:
            lines = [e for e in self._events if e["seq"] > after][-limit:]
            job = dict(self._job) if self._job else None
            history = [dict(j) for j in self._history]
            seq = self._event_seq
        if job is not None:
            job["elapsed_seconds"] = round(time.time() - job["started"], 1)
        return {"seq": seq, "events": lines, "active_job": job, "history": history}

    def _run_streaming(
        self,
        args: list[str],
        timeout: float,
        markers: "tuple[tuple[re.Pattern[str], str], ...]",
        source: str,
        *,
        cwd: Path | None = None,
        extra_env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess:
        """Run a lifecycle script, publishing each output line as it appears.

        The child runs with cwd = the runtime directory and an env that is
        os.environ MERGED with the profile overlay (overlay keys win) -- the
        Mia kit's documented env-driven configuration path.
        """
        env = dict(os.environ)
        if extra_env:
            env.update({k: str(v) for k, v in extra_env.items()})
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            cwd=str(cwd) if cwd else None,
            env=env,
            # Own process group so a timeout can kill the WHOLE tree: the
            # Mia kit spawns docker compose / docker logs children that
            # outlive the shell and would hold the pipe open otherwise.
            start_new_session=True,
        )
        captured: list[str] = []

        def pump() -> None:
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.rstrip("\n")
                captured.append(line)
                del captured[:-400]
                for pattern, phase in markers:
                    if pattern.search(line):
                        self._set_phase(phase)
                        break
                m = _STARTUP_SECONDS_RE.search(line)
                if m:
                    with self._cv:
                        if self._job is not None:
                            self._job["startup_seconds"] = int(m.group(1))
                self._event(source, line)

        reader = threading.Thread(target=pump, name=f"gxmax-{source}-out", daemon=True)
        reader.start()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            # Kill the process GROUP (shell + docker children). The pump
            # thread is a daemon we deliberately do NOT block on: a wedged
            # child could hold the pipe open far past the deadline, and
            # closing the fd under the reader can stall too. Give it a
            # short grace period for the buffered lines, then move on.
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                proc.kill()
            proc.wait()
            reader.join(timeout=1.0)
            raise
        reader.join(timeout=5)
        proc.stdout.close()
        return subprocess.CompletedProcess(args, proc.returncode, "\n".join(captured), "")

    # ------------------------------------------------------------ idle reaper
    def _idle_reaper(self) -> None:
        """Release the engine after it has been idle for longer than the TTL."""
        while not self._stopping.wait(30.0):
            if self._idle_ttl <= 0:
                continue
            with self._cv:
                ready = self._state is State.READY
                idle_for = (time.time() - self._last_used) if self._last_used else 0.0
                waiters = self._waiters
                busy = self._in_flight
            if ready and waiters == 0 and busy == 0 and idle_for > self._idle_ttl:
                log.info("gx-max idle %.0fs > TTL %ss; releasing both nodes", idle_for, self._idle_ttl)
                try:
                    self.release()
                except Exception as exc:  # noqa: BLE001
                    log.error("idle release failed: %r", exc)

    def shutdown(self) -> None:
        self._stopping.set()


# ---------------------------------------------------------------------------
# Real host facts (fakes injected in the tests)
# ---------------------------------------------------------------------------

def _default_read_mem_gib(node: str) -> "float | None":
    """MemAvailable in GiB. node1: local /proc/meminfo. node2: over SSH."""
    if node == "node1":
        try:
            with open("/proc/meminfo", encoding="ascii") as fh:
                for line in fh:
                    if line.startswith("MemAvailable:"):
                        return int(line.split()[1]) / (1024.0 * 1024.0)
        except (OSError, ValueError, IndexError):
            return None
        return None
    from .config import CONFIG

    try:
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
             CONFIG.node2_ssh, "awk '/MemAvailable/{print $2/1048576}' /proc/meminfo"],
            capture_output=True, text=True, timeout=20,
        )
        return round(float(proc.stdout.strip()), 1) if proc.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _default_container_running(node: str) -> bool:
    """True while the node's Mia container is running (rank0 here, rank1 via SSH)."""
    from .config import CONFIG

    if node == "node1":
        cmd = ["docker", "inspect", "-f", "{{.State.Running}}", CONFIG.head_container]
    else:
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
               CONFIG.node2_ssh,
               f"docker inspect -f {{{{.State.Running}}}} {CONFIG.worker_container}"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "true"
