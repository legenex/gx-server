"""Typed model switching: dsv4-flash-primary <-> dsv41-rollback (D-053).

One switch is a fixed sequence. Every step is recorded in the switch record,
the record is appended to a persistent JSONL history, and any failure after
the current model was stopped rolls back to it automatically:

    authorize (handler) -> validate request -> maintenance window
    -> deployment lock (flock, non-blocking) -> in-flight check -> pause new
    admissions -> drain -> record the previous deployment and its health
    -> stop it (its kit's sanctioned stop path, via lifecycle.release)
    -> resource preflight (MemAvailable per node vs the target's boot need,
       Maintenance holds) -> start the target -> validate (both ranks, served
       identity through the gateway, completion, streaming, structured tool
       call, tool-result continuation, fabric) -> persist the new active
       deployment -> resume admissions.
    On failure after the stop: release the target, start the previous one,
    validate it, record `rolled_back` (or `rollback_failed`, loudly).

Disruptive switches run only in the owner's maintenance window 01:00-05:00
SAST (UTC+2, no DST); outside it a switch needs override_window=true AND a
non-empty emergency_reason, both written to the history.

Nothing here kills a process: stopping and starting go through the
deployment's lifecycle, which runs the kit's own stop.sh / start.sh.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import json
import logging
import hmac
import os
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

log = logging.getLogger("gx.switch")

SAST = dt.timezone(dt.timedelta(hours=2), "SAST")
WINDOW_START_HOUR = 1
WINDOW_END_HOUR = 5

#: Record states.
RUNNING = "running"
SUCCEEDED = "succeeded"
ABORTED = "aborted"            # refused before the current model was stopped
ROLLED_BACK = "rolled_back"    # target failed; previous deployment restored
ROLLBACK_FAILED = "rollback_failed"
INTERRUPTED = "interrupted"    # the orchestrator died mid-switch


class SwitchError(Exception):
    """A switch request refused before anything changed."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def in_maintenance_window(now: "dt.datetime | None" = None) -> bool:
    """True between 01:00 and 05:00 SAST."""
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    local = now.astimezone(SAST)
    return WINDOW_START_HOUR <= local.hour < WINDOW_END_HOUR


class SwitchLock:
    """flock(2) on a file: one switch at a time, across threads AND processes.
    A crashed holder's lock dies with its file descriptor."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fh: Any = None

    def try_acquire(self, holder: str) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+", encoding="utf-8")  # noqa: SIM115 - held across the switch
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        fh.seek(0)
        fh.truncate()
        fh.write(json.dumps({"holder": holder, "pid": os.getpid(), "since": time.time()}))
        fh.flush()
        self._fh = fh
        return True

    def holder(self) -> str:
        try:
            return json.loads(self.path.read_text(encoding="utf-8") or "{}").get("holder", "")
        except (OSError, ValueError):
            return ""

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is not None:
            with contextlib.suppress(OSError):
                fh.seek(0)
                fh.truncate()
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            fh.close()


class SwitchHistory:
    """Append-only JSONL; `running` entries left by a dead process read as interrupted."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def append(self, record: Mapping[str, Any]) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")
                fh.flush()
                os.fsync(fh.fileno())

    def read(self, limit: int = 50) -> list[dict[str, Any]]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        latest: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            sid = str(rec.get("id"))
            if sid not in latest:
                order.append(sid)
            latest[sid] = rec
        out = [latest[s] for s in order][-max(1, limit):]
        return out

    def mark_interrupted(self) -> list[dict[str, Any]]:
        """At startup: a switch still `running` belonged to a dead process.
        Returns the records it marked (empty list = nothing to reconcile)."""
        marked = []
        for rec in self.read(limit=1000):
            if rec.get("state") == RUNNING:
                rec = dict(rec, state=INTERRUPTED, ended=time.time(),
                           error="orchestrator restarted during the switch; check both nodes")
                self.append(rec)
                marked.append(rec)
        return marked


#: Each deployment's kit stop path, for the operator action text.
def _stop_hint(registry: Any, name: str) -> str:
    rt = registry.runtime(registry.deployment(name).runtime)
    return f"{rt.kit_path or rt.submodule}/stop.sh"


def reconcile_interrupted(records: list[dict[str, Any]], *, registry: Any, store: Any,
                          running: Callable[[str], Mapping[str, "bool | None"]],
                          history: "SwitchHistory",
                          ready: "Callable[[str], tuple[bool, str]] | None" = None) -> dict[str, Any]:
    """Startup reconciliation of switches cut short by an orchestrator restart (A9).

    Looks at which deployment's rank containers are actually running and
    either completes the active pointer (exactly one deployment fully up) or
    marks the switch ``needs_operator`` with the exact next action. It never
    starts or stops anything. Returns {"outcome", "action", "latch"} where a
    non-empty latch means request-driven starts must stay off.
    """
    if not records:
        return {"outcome": "none", "action": "", "latch": ""}
    observed = {name: dict(running(name)) for name in registry.deployments}
    full = [n for n, st in observed.items() if st.get("node1") is True and st.get("node2") is True]
    partial = [n for n, st in observed.items() if n not in full and any(v is True for v in st.values())]
    unknown = [n for n, st in observed.items() if any(v is None for v in st.values())]
    pointer = (store.read() or {}).get("deployment") or registry.initial_active_deployment
    latch = ""
    if unknown:
        outcome = "needs_operator"
        action = (f"rank state unknown for {unknown} (probe errors); check `docker ps` on both nodes, "
                  "then restart gx-orchestrator")
        latch = action
    elif len(full) == 1 and not partial and full[0] == pointer:
        outcome, action = "pointer_confirmed", f"{pointer} is running and active; nothing to do"
    elif len(full) == 1 and not partial:
        # Containers alone prove nothing: the pointer moves only to an engine
        # that answers its ready probe with the right served id AND passed the
        # switch's validation before the restart (review follow-up 1).
        name = full[0]
        ok, detail = ready(name) if ready is not None else (False, "no ready probe configured")
        validated = any(r.get("target") == name and r.get("checks")
                        and all(c.get("ok") for c in r["checks"]) for r in records)
        if ok and validated:
            store.write(name, switch_id=str(records[-1].get("id", "")), actor="startup-reconcile")
            outcome, action = "pointer_completed", f"active pointer set to {name}: ready ({detail}) and validated"
        else:
            outcome = "needs_operator"
            action = (f"{name} rank containers are up but not proven (ready probe: {detail}; validation "
                      f"record: {'passed' if validated else 'none'}). Either stop it with "
                      f"{_stop_hint(registry, name)} and restart gx-orchestrator ({pointer} stays active), or "
                      f"restart gx-orchestrator and run POST /models/activate "
                      f'{{"target":"{name}","confirm":true}} to switch to it properly')
            latch = action
    elif not full and not partial:
        outcome = "needs_operator"
        action = (f"no model is running; the active pointer is {pointer}. Start it with "
                  f"POST /lifecycle/gx-max/acquire, or switch with POST /models/activate")
    else:
        extra = [n for n in full + partial if n != pointer] or full + partial
        outcome = "needs_operator"
        action = ("more than one kit (or a single rank) is up: " + ", ".join(
            f"{n} ranks={observed[n]}" for n in full + partial)
            + "; stop the one that should not run with " + " / ".join(_stop_hint(registry, n) for n in extra)
            + ", then restart gx-orchestrator")
        latch = action
    for rec in records:
        history.append(dict(rec, reconcile={"outcome": outcome, "action": action, "observed": observed,
                                            "pointer": pointer, "at": time.time()}))
    log.error("interrupted model switch reconciled: %s -- %s", outcome, action)
    return {"outcome": outcome, "action": action, "latch": latch}


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail[:500]}


@dataclass
class SwitchRecord:
    id: str
    target: str
    previous: str
    actor: str
    forced: bool = False
    emergency_reason: str = ""
    state: str = RUNNING
    started: float = field(default_factory=time.time)
    ended: "float | None" = None
    previous_healthy: "bool | None" = None
    in_flight_at_start: dict[str, int] = field(default_factory=dict)
    steps: list[dict[str, Any]] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)
    rollback_checks: list[dict[str, Any]] = field(default_factory=list)
    flags: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def step(self, name: str, ok: bool = True, detail: str = "") -> None:
        self.steps.append({"step": name, "ok": ok, "at": round(time.time(), 1), "detail": detail[:500]})

    def as_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


#: validator(deployment_name, validation_token) -> list[Check]; preflight likewise.
#: The token lets the validator's own requests through the paused scheduler.
Validator = Callable[[str, str], "list[Check]"]


class ModelSwitcher:
    """The switch state machine. Dependencies are injected (fakes in tests)."""

    def __init__(
        self,
        *,
        manager: Any,
        scheduler: Any,
        history: SwitchHistory,
        lock: SwitchLock,
        validator: Validator,
        preflight: Validator,
        drain_timeout: float = 600.0,
        acquire_timeout: "float | None" = None,
        clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc),
        run_async: bool = True,
        configure_gateway: "Callable[[str], None] | None" = None,
    ) -> None:
        self._manager = manager
        self._scheduler = scheduler
        self._history = history
        self._lock = lock
        self._validate = validator
        self._preflight = preflight
        self._drain_timeout = drain_timeout
        self._acquire_timeout = acquire_timeout
        self._clock = clock
        self._async = run_async
        self._configure_gateway = configure_gateway
        self._current: "SwitchRecord | None" = None
        self._mutex = threading.Lock()
        self._token = ""

    def validation_token_ok(self, presented: str) -> bool:
        """True for the running switch's own validation requests only."""
        token = self._token
        return bool(token and presented) and hmac.compare_digest(token.encode(), presented.encode())

    # ----------------------------------------------------------------- views
    def current(self) -> "dict[str, Any] | None":
        with self._mutex:
            return self._current.as_dict() if self._current else None

    def history(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._history.read(limit)

    # --------------------------------------------------------------- request
    def request(self, payload: Mapping[str, Any], *, actor: str) -> dict[str, Any]:
        """Validate and start a switch; raises SwitchError when refused.

        Three separate, explicit overrides (D-053 A7), each audited:
        ``override_window`` (+ ``emergency_reason``) runs outside 01:00-05:00
        SAST; ``cut_in_flight`` proceeds when requests did not drain;
        ``reactivate`` restarts the deployment that is already active.
        """
        if "force" in payload:
            raise SwitchError(400, "force_removed",
                              "use the explicit flags override_window (+ emergency_reason), "
                              "cut_in_flight and reactivate instead of force")
        target = str(payload.get("target") or "").strip()
        registry_deps = self._manager_deployments()
        if target not in registry_deps:
            raise SwitchError(400, "unknown_target", f"target must be one of {sorted(registry_deps)}")
        if payload.get("confirm") is not True:
            raise SwitchError(400, "confirm_required", "a switch needs \"confirm\": true")
        expiry = payload.get("not_after")
        if expiry is not None:
            now = self._clock().timestamp()
            if type(expiry) is not int or not now < expiry <= now + 120:
                raise SwitchError(400, "expired_action", "not_after must be an unexpired integer epoch within 120 seconds")
        override = payload.get("override_window") is True
        cut = payload.get("cut_in_flight") is True
        reactivate = payload.get("reactivate") is True
        reason = str(payload.get("emergency_reason") or "").strip()[:500]
        if override and not reason:
            raise SwitchError(400, "reason_required", "override_window needs a non-empty emergency_reason (audited)")
        if target == self._manager.active_name and not reactivate:
            raise SwitchError(409, "already_active", f"{target} is already the active deployment "
                                                     "(reactivate=true restarts it)")
        in_window = in_maintenance_window(self._clock())
        if not in_window and not override:
            raise SwitchError(423, "outside_window",
                              "disruptive switches run only 01:00-05:00 SAST; outside it pass "
                              "override_window=true with an emergency_reason")
        sid = uuid.uuid4().hex[:12]
        if not self._lock.try_acquire(f"switch {sid} -> {target} by {actor}"):
            raise SwitchError(409, "switch_in_progress",
                              f"another switch holds the deployment lock ({self._lock.holder() or 'unknown'})")
        try:
            rec = SwitchRecord(id=sid, target=target, previous=self._manager.active_name, actor=actor,
                               forced=override or cut or reactivate, emergency_reason=reason)
            rec.flags = {"override_window": override, "cut_in_flight": cut, "reactivate": reactivate}
            if expiry is not None:
                rec.flags['not_after'] = expiry
            if not in_window:
                rec.step("window", True, f"outside the window: OVERRIDDEN by {actor}: {reason}")
            else:
                rec.step("window", True, "inside the 01:00-05:00 SAST maintenance window")
            rec.step("lock", True, str(self._lock.path))
            with self._mutex:
                self._current = rec
            self._token = secrets.token_urlsafe(24)
            self._history.append(rec.as_dict())
            if self._async:
                threading.Thread(target=self._run, args=(rec,), name=f"gx-switch-{sid}", daemon=True).start()
        except Exception as exc:
            # Never leave the deployment lock held by a switch that did not start (A8).
            self._token = ""
            self._lock.release()
            raise SwitchError(500, "switch_start_failed", f"could not start the switch: {exc!r}") from exc
        if not self._async:
            self._run(rec)
        return rec.as_dict()

    def _manager_deployments(self) -> Mapping[str, Any]:
        return self._manager._registry.deployments  # noqa: SLF001 - same package

    # ------------------------------------------------------------------- run
    def _run(self, rec: SwitchRecord) -> None:
        stopped_previous = False
        old = None
        try:
            status = self._scheduler.status()
            rec.in_flight_at_start = {"active": int(status.get("active", 0)), "queued": int(status.get("queued", 0))}
            rec.step("in_flight", True, f"{rec.in_flight_at_start['active']} active, "
                                        f"{rec.in_flight_at_start['queued']} queued")
            self._scheduler.pause(f"model switch {rec.id} to {rec.target} in progress; retry after it")
            remaining = self._scheduler.drain(self._drain_timeout)
            if remaining and not rec.flags.get("cut_in_flight"):
                rec.step("drain", False, f"{remaining} request(s) still active after {self._drain_timeout}s")
                self._finish(rec, ABORTED, "in-flight requests did not drain; nothing was stopped "
                                           "(cut_in_flight=true cuts them)")
                return
            rec.step("drain", True, f"{remaining} still active (cut_in_flight)" if remaining else "drained")

            # Re-check immediately before any disruption, after queue/drain waits.
            # A queued owner action cannot wake after recovery's expiry fence.
            expiry = rec.flags.get('not_after')
            if expiry is not None and self._clock().timestamp() >= expiry:
                self._finish(rec, ABORTED, 'owner action expired before disruption; previous model untouched')
                return

            old = self._manager.lifecycle
            # No request may (re)start the outgoing deployment from here on (A1).
            if hasattr(old, "latch"):
                old.latch(f"switch {rec.id} to {rec.target}")
            rec.previous_healthy = old.status().state.value == "ready"
            rec.step("previous", True, f"{rec.previous} healthy={rec.previous_healthy}")
            stopped_previous = True  # from here a failure must restore the previous model
            old.release(force=bool(remaining))
            st = old.status()
            if st.state.value != "down":
                raise RuntimeError(f"previous deployment did not stop (state {st.state.value})")
            rec.step("stop_previous", True, f"{rec.previous} released via its stop path")

            checks = self._preflight(rec.target, self._token)
            failed = [c for c in checks if not c.ok]
            rec.step("preflight", not failed, "; ".join(f"{c.name}: {c.detail}" for c in checks))
            if failed:
                raise RuntimeError("target preflight failed: " + "; ".join(c.name for c in failed))

            lc = self._manager.activate(rec.target)
            lc.acquire(None, timeout=self._acquire_timeout, bypass_backoff=True)
            rec.step("start_target", True, f"{rec.target} READY")

            if self._configure_gateway is not None:
                self._configure_gateway(rec.target)
                rec.step("gateway_config", True, f"{rec.target} loaded metadata and identity verified")

            checks = self._validate(rec.target, self._token)
            rec.checks = [c.as_dict() for c in checks]
            failed = [c for c in checks if not c.ok]
            if failed:
                raise RuntimeError("validation failed: " + "; ".join(f"{c.name} ({c.detail[:120]})" for c in failed))
            rec.step("validate", True, f"{len(checks)} checks passed")
            self._manager.persist(rec.target, switch_id=rec.id, actor=rec.actor)
            self._finish(rec, SUCCEEDED)
        except Exception as exc:  # noqa: BLE001 - every failure is recorded
            log.error("switch %s failed: %r", rec.id, exc)
            rec.error = str(exc)[:1000]
            if not stopped_previous:
                if old is not None and hasattr(old, "unlatch"):
                    old.unlatch()
                self._finish(rec, ABORTED, rec.error)
            else:
                self._rollback(rec)
        finally:
            self._token = ""
            self._scheduler.resume()
            self._lock.release()

    def _rollback(self, rec: SwitchRecord) -> None:
        rec.step("rollback", True, f"restoring {rec.previous}")
        try:
            current = self._manager.lifecycle
            if self._manager.active_name != rec.previous or rec.flags.get("reactivate"):
                if self._manager.active_name != rec.previous:
                    try:
                        current.release(force=True)
                    except Exception as exc:  # noqa: BLE001
                        rec.step("release_target", False, repr(exc))
                # A fresh lifecycle object: the latched one stays dead.
                self._manager.activate(rec.previous)
            else:
                # The target never started (e.g. preflight failed): reuse the
                # previous lifecycle, which the switch latched.
                if hasattr(current, "unlatch"):
                    current.unlatch()
            self._manager.lifecycle.acquire(None, timeout=self._acquire_timeout, bypass_backoff=True)
            if self._configure_gateway is not None:
                self._configure_gateway(rec.previous)
                rec.step("rollback_gateway_config", True, f"{rec.previous} loaded metadata and identity verified")
            checks = self._validate(rec.previous, self._token)
            rec.rollback_checks = [c.as_dict() for c in checks]
            failed = [c for c in checks if not c.ok]
            if failed:
                raise RuntimeError("rollback validation failed: " + "; ".join(c.name for c in failed))
            self._manager.persist(rec.previous, switch_id=rec.id, actor=f"auto-rollback ({rec.actor})")
            self._finish(rec, ROLLED_BACK, rec.error)
        except Exception as exc:  # noqa: BLE001
            log.critical("switch %s: ROLLBACK FAILED: %r", rec.id, exc)
            self._finish(rec, ROLLBACK_FAILED, f"{rec.error} | rollback: {exc}")

    def _finish(self, rec: SwitchRecord, state: str, error: str = "") -> None:
        rec.state = state
        rec.ended = time.time()
        if error:
            rec.error = error[:1000]
        self._history.append(rec.as_dict())
        log.info("switch %s -> %s: %s", rec.id, rec.target, state)
