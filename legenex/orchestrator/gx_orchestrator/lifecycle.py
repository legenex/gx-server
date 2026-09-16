"""gx-max cluster acquisition, with a single-writer state machine.

gx-max is the only tier that needs BOTH nodes. Bringing it up evicts every
other model, so acquisition must be serialised: concurrent callers queue behind
one acquisition rather than each launching their own.

States
------
    DOWN      -> nothing running; nodes are free for normal workloads
    ACQUIRING -> a single worker thread is running gx-max-start.sh
    READY     -> the engine is healthy and serving
    RELEASING -> a worker thread is running gx-max-stop.sh

Transitions are guarded by one lock plus a condition variable. Callers block on
the condition rather than polling, and every waiter is woken on a state change.
"""

from __future__ import annotations

import collections
import enum
import json
import logging
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("gx.lifecycle")


class State(str, enum.Enum):
    DOWN = "down"
    ACQUIRING = "acquiring"
    READY = "ready"
    RELEASING = "releasing"


class AcquisitionError(RuntimeError):
    """gx-max could not be brought up. Never downgrade silently -- raise."""


#: Fine-grained, READ-ONLY progress within ACQUIRING / RELEASING. It is derived
#: from the section markers gx-max-start.sh / gx-max-stop.sh already print, so
#: it never influences a transition -- `State` above stays the only thing the
#: state machine acts on. Exposed for operators (control UI Jobs page).
PHASE_IDLE = "idle"
PHASE_SERVING = "serving"
PHASE_FAILED = "failed"

#: (compiled pattern, phase) in the order the scripts print them. First match
#: on a line wins.
_START_MARKERS: tuple[tuple["re.Pattern[str]", str], ...] = tuple(
    (re.compile(p), ph)
    for p, ph in (
        (r"=== gx-max preflight ===", "preflight"),
        (r"=== draining conflicting GPU work ===", "draining"),
        (r"=== cluster-takeover admission", "admission"),
        (r"=== starting rank1 on node2 ===", "loading_rank1"),
        (r"=== starting rank0 on node1 ===", "loading_rank0"),
        (r"=== waiting for gx-max to become healthy", "warming"),
        (r"=== gx-max READY", "ready"),
        (r"gx-max start failed .* running the two-node unwind", "unwinding"),
        (r"start aborted before any rank was launched", "restoring"),
    )
)
_STOP_MARKERS: tuple[tuple["re.Pattern[str]", str], ...] = tuple(
    (re.compile(p), ph)
    for p, ph in (
        (r"draining: waiting", "draining_requests"),
        (r"stopping rank0 on node1", "stopping_ranks"),
        (r"MemAvailable after release", "memory_recovery"),
        (r"restoring normal single-node workloads", "restoring"),
        (r"gx-max released; both nodes are back", "released"),
    )
)
_STARTUP_SECONDS_RE = re.compile(r"gx-max READY on \S+ after (\d+)s")

#: Lines kept in memory for /lifecycle/gx-max/events.
_EVENT_BUFFER = 400
#: Job records kept (memory and the optional history file).
_HISTORY_KEEP = 25


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

    def as_dict(self) -> dict:
        now = time.time()
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
        }


class GxMaxLifecycle:
    """Serialised acquire/release for the two-node gx-max engine."""

    def __init__(
        self,
        lifecycle_dir: Path,
        health_url: str,
        *,
        idle_ttl: int = 1800,
        acquire_timeout: int = 1800,
        events_log: Path | None = None,
        history_path: Path | None = None,
    ) -> None:
        self._dir = Path(lifecycle_dir)
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
        self._health_url = health_url
        self._idle_ttl = idle_ttl
        self._acquire_timeout = acquire_timeout

        self._cv = threading.Condition(threading.RLock())
        self._state = State.DOWN
        self._since = time.time()
        self._last_used: float | None = None
        self._waiters = 0
        self._detail = ""
        self._last_error = ""
        self._worker: threading.Thread | None = None
        self._stopping = threading.Event()
        self._last_reconcile = 0.0

        # Adopt reality at startup: the engine may already be running.
        if self._probe_health():
            self._state = State.READY
            self._last_used = time.time()
            self._detail = "adopted an already-running engine at startup"
            self._phase, self._phase_since = PHASE_SERVING, time.time()
            log.info("gx-max: adopted already-running engine")

        self._reaper = threading.Thread(target=self._idle_reaper, name="gxmax-ttl", daemon=True)
        self._reaper.start()

    #: How long to keep probing after a successful start before giving up.
    _SETTLE_SECONDS = 60
    #: Minimum interval between reconciliation probes, so status polling does
    #: not hammer the engine.
    _RECONCILE_INTERVAL = 10.0

    # ------------------------------------------------------------------ probe
    def _await_health(self, seconds: float) -> bool:
        """Poll /health until it answers or `seconds` elapse."""
        deadline = time.time() + seconds
        while True:
            if self._probe_health():
                return True
            if time.time() >= deadline:
                return False
            time.sleep(1.0)

    def _probe_health(self, timeout: float = 5.0) -> bool:
        url = self._health_url
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                return 200 <= resp.status < 300
        except Exception:
            return False

    # ------------------------------------------------------------ reconcile
    def _reconcile(self) -> None:
        """Keep the state machine honest about an engine managed elsewhere.

        READY -> DOWN when the engine has gone away behind our back (an
        operator running gx-max-stop.sh, a crash, a node reboot); otherwise
        requests would be proxied into a dead endpoint instead of re-acquiring.

        DOWN -> READY when a healthy engine is found that this process did
        not start (an operator running gx-max-start.sh directly). The startup
        adoption above only covered engines that predated the orchestrator;
        found 2026-09-16, `/health/detailed` reported gx-max "stopped" for a
        serving engine until the first request happened to adopt it.
        ACQUIRING/RELEASING are never touched: a worker thread owns those.
        """
        with self._cv:
            if self._state not in (State.READY, State.DOWN):
                return
            if time.time() - self._last_reconcile < self._RECONCILE_INTERVAL:
                return
            self._last_reconcile = time.time()
            observed = self._state

        # Probe outside the lock: it does network I/O.
        healthy = self._probe_health()

        with self._cv:
            # Re-check: the state may have moved while we were probing.
            if self._state is not observed:
                return
            if observed is State.READY and not healthy:
                log.warning("gx-max disappeared while marked READY; marking DOWN")
                self._last_used = None
                self._set_state(State.DOWN, "engine vanished (torn down externally)")
                self._set_phase(PHASE_IDLE)
                self._event("reconcile", "engine vanished while READY; marked DOWN")
            elif observed is State.DOWN and healthy:
                log.info("gx-max: adopted an engine started outside the orchestrator")
                self._last_used = time.time()
                self._set_state(State.READY, "adopted an externally started engine")
                self._set_phase(PHASE_SERVING)
                self._event("reconcile", "adopted an engine started outside the orchestrator")

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
            )

    def is_ready(self) -> bool:
        self._reconcile()
        with self._cv:
            return self._state is State.READY

    def mark_used(self) -> None:
        """Record activity so the idle reaper does not release under load."""
        with self._cv:
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
    def acquire(self, timeout: float | None = None) -> None:
        """Ensure gx-max is serving, starting it if necessary.

        Blocks until the engine is READY. Raises AcquisitionError on failure --
        it must NEVER fall back to a different model.
        """
        deadline = time.time() + (timeout if timeout is not None else self._acquire_timeout)

        # Never hand back a stale READY: if the engine died, re-acquire it.
        self._reconcile()

        with self._cv:
            self._waiters += 1
        try:
            while True:
                with self._cv:
                    if self._state is State.READY:
                        self._last_used = time.time()
                        return

                    if self._state is State.DOWN:
                        # We are the one who starts it.
                        self._last_error = ""
                        self._set_state(State.ACQUIRING, "starting both ranks")
                        self._begin_job("acquire")
                        self._worker = threading.Thread(
                            target=self._do_acquire, name="gxmax-acquire", daemon=True
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

    def _do_acquire(self) -> None:
        script = self._dir / "gx-max-start.sh"
        try:
            log.info("gx-max: running %s", script)
            proc = self._run_streaming(
                ["bash", str(script)], self._acquire_timeout, _START_MARKERS, "acquire"
            )
            if proc.returncode == 0:
                # The start script already waits for health, but the engine can
                # take a moment longer to answer our probe (the script polls
                # from a separate process). A transient probe miss must not
                # discard a successful start, so settle for a short window.
                if self._await_health(self._SETTLE_SECONDS):
                    with self._cv:
                        self._last_used = time.time()
                    # Finish the job record BEFORE the transition wakes the
                    # waiters, so anyone reading status sees it complete.
                    self._set_phase(PHASE_SERVING)
                    self._end_job("ready")
                    self._set_state(State.READY, "engine healthy")
                    return
                err = (
                    f"gx-max-start.sh exited 0 but the engine did not answer "
                    f"/health within {self._SETTLE_SECONDS}s"
                )
            else:
                tail = (proc.stdout or "").strip().splitlines()[-15:]
                err = f"gx-max-start.sh exited {proc.returncode}: " + " | ".join(tail)
        except subprocess.TimeoutExpired:
            err = f"gx-max-start.sh exceeded {self._acquire_timeout}s"
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller verbatim
            err = f"gx-max-start.sh failed: {exc!r}"

        # gx-max-start.sh can fail AFTER it already started one or both ranks
        # (rank0/rank1 are `docker run -d`, detached from the script's own
        # process -- killing or exiting the script does not stop them). Left
        # alone that is an ~80-90GiB leak per rank with no lease and nothing
        # watching it: exactly the unmanaged-residency shape that caused
        # B-012, just triggered by a hung/failed acquire instead of a second
        # manual launch. Unwind unconditionally on every failure path,
        # best-effort, before reporting the ORIGINAL error to the caller.
        err = self._cleanup_after_failed_acquire(err)

        log.error("gx-max acquisition failed: %s", err)
        with self._cv:
            self._last_error = err
        self._set_phase(PHASE_FAILED)
        self._end_job("failed", error=err)
        self._set_state(State.DOWN, "acquisition failed")

    def _cleanup_after_failed_acquire(self, original_err: str) -> str:
        """Best-effort unwind of any rank a failed acquire left running.

        Never raises and never masks `original_err` with a cleanup exception
        -- it only appends a note when the cleanup itself could not be
        confirmed, so the operator knows a rank may still be resident.
        """
        stop_script = self._dir / "gx-max-stop.sh"
        try:
            proc = subprocess.run(
                ["bash", str(stop_script), "--force"],
                capture_output=True,
                text=True,
                timeout=180,
            )
            if proc.returncode != 0:
                tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-10:]
                log.error(
                    "gx-max post-failure cleanup exited %s: %s", proc.returncode, " | ".join(tail)
                )
                return (
                    original_err + " [cleanup after failure also failed, exit "
                    f"{proc.returncode} -- a rank may still be running, check "
                    "`docker ps` on both nodes]"
                )
            log.info("gx-max post-failure cleanup: both ranks stopped, ledger released")
            self._event("cleanup", "post-failure cleanup: gx-max-stop.sh --force completed")
        except Exception as exc:  # noqa: BLE001
            log.error("gx-max post-failure cleanup raised: %r", exc)
            return (
                original_err + f" [cleanup after failure raised {exc!r} -- a rank "
                "may still be running, check `docker ps` on both nodes]"
            )
        return original_err

    # ---------------------------------------------------------------- release
    def release(self, *, force: bool = False, restore: bool = True) -> None:
        """Tear gx-max down and hand both nodes back to normal workloads."""
        with self._cv:
            if self._state in (State.DOWN, State.RELEASING):
                return
            self._set_state(State.RELEASING, "forced" if force else "graceful drain")
            self._begin_job("release_forced" if force else "release")

        args = ["bash", str(self._dir / "gx-max-stop.sh")]
        if force:
            args.append("--force")
        if not restore:
            args.append("--no-restore")
        outcome, rel_err = "released", ""
        try:
            proc = self._run_streaming(args, 900, _STOP_MARKERS, "release")
            if proc.returncode != 0:
                log.warning("gx-max-stop.sh exited %s: %s", proc.returncode, (proc.stdout or "")[-500:])
                outcome, rel_err = "release_warning", f"gx-max-stop.sh exited {proc.returncode}"
        except Exception as exc:  # noqa: BLE001
            log.error("gx-max release failed: %r", exc)
            outcome, rel_err = "release_error", repr(exc)
        finally:
            with self._cv:
                self._last_used = None
            self._set_state(State.DOWN, "released")
            self._set_phase(PHASE_IDLE)
            self._end_job(outcome, error=rel_err)


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
    ) -> subprocess.CompletedProcess:
        """Run a lifecycle script, publishing each output line as it appears.

        Same contract as the subprocess.run(..., timeout) call it replaces:
        the child is killed and TimeoutExpired raised when `timeout` elapses,
        and stdout (stderr merged in) is returned for the error tail.
        """
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            bufsize=1,
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
            proc.kill()
            proc.wait()
            reader.join(timeout=5)
            raise
        reader.join(timeout=5)
        return subprocess.CompletedProcess(args, proc.returncode, "\n".join(captured), "")

    # ------------------------------------------------------------ idle reaper
    def _idle_reaper(self) -> None:
        """Release gx-max after it has been idle for longer than the TTL."""
        while not self._stopping.wait(30.0):
            if self._idle_ttl <= 0:
                continue
            with self._cv:
                ready = self._state is State.READY
                idle_for = (time.time() - self._last_used) if self._last_used else 0.0
                waiters = self._waiters
            if ready and waiters == 0 and idle_for > self._idle_ttl:
                log.info("gx-max idle %.0fs > TTL %ss; releasing both nodes", idle_for, self._idle_ttl)
                try:
                    self.release()
                except Exception as exc:  # noqa: BLE001
                    log.error("idle release failed: %r", exc)

    def shutdown(self) -> None:
        self._stopping.set()
