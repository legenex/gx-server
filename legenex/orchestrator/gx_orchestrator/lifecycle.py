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

import enum
import json
import logging
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


@dataclass
class LifecycleStatus:
    state: State
    since: float
    last_used: float | None
    waiters: int
    detail: str = ""
    last_error: str = ""

    def as_dict(self) -> dict:
        now = time.time()
        return {
            "state": self.state.value,
            "seconds_in_state": round(now - self.since, 1),
            "idle_seconds": round(now - self.last_used, 1) if self.last_used else None,
            "waiters": self.waiters,
            "detail": self.detail,
            "last_error": self.last_error,
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
    ) -> None:
        self._dir = Path(lifecycle_dir)
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
            elif observed is State.DOWN and healthy:
                log.info("gx-max: adopted an engine started outside the orchestrator")
                self._last_used = time.time()
                self._set_state(State.READY, "adopted an externally started engine")

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
            proc = subprocess.run(
                ["bash", str(script)],
                capture_output=True,
                text=True,
                timeout=self._acquire_timeout,
            )
            if proc.returncode == 0:
                # The start script already waits for health, but the engine can
                # take a moment longer to answer our probe (the script polls
                # from a separate process). A transient probe miss must not
                # discard a successful start, so settle for a short window.
                if self._await_health(self._SETTLE_SECONDS):
                    with self._cv:
                        self._last_used = time.time()
                    self._set_state(State.READY, "engine healthy")
                    return
                err = (
                    f"gx-max-start.sh exited 0 but the engine did not answer "
                    f"/health within {self._SETTLE_SECONDS}s"
                )
            else:
                tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-15:]
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

        args = ["bash", str(self._dir / "gx-max-stop.sh")]
        if force:
            args.append("--force")
        if not restore:
            args.append("--no-restore")
        try:
            proc = subprocess.run(args, capture_output=True, text=True, timeout=900)
            if proc.returncode != 0:
                log.warning("gx-max-stop.sh exited %s: %s", proc.returncode, proc.stderr[-500:])
        except Exception as exc:  # noqa: BLE001
            log.error("gx-max release failed: %r", exc)
        finally:
            with self._cv:
                self._last_used = None
            self._set_state(State.DOWN, "released")

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
