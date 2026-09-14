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

    # ----------------------------------------------------------------- status
    def status(self) -> LifecycleStatus:
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

        log.error("gx-max acquisition failed: %s", err)
        with self._cv:
            self._last_error = err
        self._set_state(State.DOWN, "acquisition failed")

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
