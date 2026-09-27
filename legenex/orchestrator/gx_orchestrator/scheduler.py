"""Request-level admission scheduler (ARCHITECTURE-V41 §4).

The server is a ThreadingHTTPServer, so every method here must be safe under
concurrent callers. The scheduler owns ONE decision: given the engine's
global capacity (the running profile's max_num_seqs), which queued request
may occupy the next active slot, and in what order.

Fairness rules:

* STRICT PRIORITY -- interactive(0) > critical-review(1) > orchestrator(2) >
  normal-worker(3) > background(4). A lower-number priority queued behind a
  higher-number active request is never promoted ahead of capacity; a
  higher-priority submit is never demoted by earlier lower-priority submits.
* ROUND-ROBIN WITHIN PRIORITY across PROJECTS -- two projects both at
  priority 3 alternate, so one agent swarm cannot starve another project
  even when it fills the queue first.

Persistence: the queue (queued + active records) is written to queue.json
with an atomic tmp+rename on EVERY mutation, and restored at startup. Any
record found `active` at restore belongs to a request whose control-plane
thread died with the process: it is marked error "control-plane restart" --
never silently resumed. Finished records go to a JSONL history ring buffer
(cap 5000) that survives restarts.

Timeout: every record carries a soft deadline (default 600s, per-request
override). A reaper thread expires both queued and active items, freeing the
slot for the next promotion.
"""

from __future__ import annotations

import collections
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

log = logging.getLogger("gx.scheduler")

#: Priority vocabulary -> sort key (lower = served first).
PRIORITIES: dict[str, int] = {
    "interactive": 0,
    "critical-review": 1,
    "orchestrator": 2,
    "normal-worker": 3,
    "background": 4,
}
DEFAULT_PRIORITY = "normal-worker"
DEFAULT_PROJECT = "unknown"

#: Record states. `cancelling` is an ACTIVE record whose cancel was requested:
#: the relaying thread owns the socket, so it must observe the mark and stop.
STATE_QUEUED = "queued"
STATE_ACTIVE = "active"
STATE_CANCELLING = "cancelling"
STATE_DONE = "done"
STATE_ERROR = "error"
STATE_CANCELLED = "cancelled"
STATE_TIMEOUT = "timeout"
#: Everything that will never run again.
_TERMINAL = (STATE_DONE, STATE_ERROR, STATE_CANCELLED, STATE_TIMEOUT)
#: Decision states handed back by submit() / wait().
DECISION_ACTIVE = "active"
DECISION_QUEUED = "queued"
DECISION_REJECTED = "rejected"

#: Metrics keys accepted by record_finished().
_METRIC_KEYS = (
    "prompt_tokens", "completion_tokens", "cached_tokens", "ttft_ms", "tps",
)


def valid_priority(name: Any) -> str:
    text = str(name or "").strip().lower()
    return text if text in PRIORITIES else DEFAULT_PRIORITY


@dataclass
class Record:
    """One admitted-or-waiting request (ARCHITECTURE-V41 §4 field list)."""

    id: str
    project: str
    agent: str
    task: str
    priority: str
    profile: str
    reasoning: str
    state: str = STATE_QUEUED
    enqueue_ts: float = 0.0
    start_ts: "float | None" = None
    done_ts: "float | None" = None
    timeout_at: float = 0.0
    prompt_tokens: "int | None" = None
    completion_tokens: "int | None" = None
    cached_tokens: "int | None" = None
    ttft_ms: "float | None" = None
    tps: "float | None" = None
    error: str = ""

    #: Set when promoted to active; the relaying thread waits on it.
    ready_event: "threading.Event | None" = field(default=None, repr=False, compare=False)

    def as_dict(self) -> dict[str, Any]:
        # Manual (not dataclasses.asdict): the ready_event carries a lock and
        # must never be copied or persisted.
        return {
            f: getattr(self, f)
            for f in self.__dataclass_fields__
            if f != "ready_event"
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Record":
        known = {f for f in cls.__dataclass_fields__ if f != "ready_event"}
        return cls(**{k: v for k, v in data.items() if k in known})

    def public(self) -> dict[str, Any]:
        """Safe for /scheduler/status: the full record, no live objects."""
        out = self.as_dict()
        if self.enqueue_ts:
            out["queued_seconds"] = round(max(0.0, (self.start_ts or time.time()) - self.enqueue_ts), 1)
        return out


class AdmissionRejected(RuntimeError):
    """The queue is full. The server answers 429 with the position."""


class Scheduler:
    """Thread-safe admission queue in front of the single model."""

    def __init__(
        self,
        *,
        queue_path: Path,
        history_path: Path,
        capacity: int = 1,
        per_project_active_cap: int = 2,
        per_project_queued_cap: int = 8,
        global_queued_cap: int = 32,
        default_timeout: float = 600.0,
        history_cap: int = 5000,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._queue_path = Path(queue_path)
        self._history_path = Path(history_path)
        self._capacity = max(1, int(capacity))
        self._proj_active_cap = max(1, per_project_active_cap)
        self._proj_queued_cap = max(1, per_project_queued_cap)
        self._global_queued_cap = max(1, global_queued_cap)
        self._default_timeout = default_timeout
        self._history_cap = history_cap
        self._clock = clock

        self._lock = threading.RLock()
        self._idle = threading.Condition(self._lock)
        self._records: dict[str, Record] = {}
        self._order: list[str] = []  # queue order, FIFO within (priority, project) turn
        #: Round-robin cursor per priority level: next project to serve.
        self._rr: dict[int, str] = {}
        self._history: "collections.deque[dict]" = collections.deque(maxlen=history_cap)

        self._stopping = threading.Event()
        self._reaper = threading.Thread(target=self._reap_loop, name="gx-sched-reaper", daemon=True)

        self._restore()
        self._reaper.start()

    # ------------------------------------------------------------- persistence
    def _persist(self) -> None:
        """Atomic tmp+rename write of the live queue (queued + active)."""
        with self._lock:
            live = [
                r for r in self._records.values()
                if r.state in (STATE_QUEUED, STATE_ACTIVE, STATE_CANCELLING)
            ]
            payload = {
                "records": [r.as_dict() for r in live],
                "capacity": self._capacity,
                "written_at": self._clock(),
            }
        try:
            self._queue_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._queue_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
            os.replace(tmp, self._queue_path)
        except OSError:
            log.warning("scheduler queue persist failed", exc_info=True)

    def _restore(self) -> None:
        """Load queue.json. Active records died with the old process: they are
        marked error 'control-plane restart' rather than resumed, and their
        history entries are written so the operator can see the cutoff."""
        try:
            payload = json.loads(self._queue_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        records = payload.get("records") if isinstance(payload, Mapping) else None
        if not isinstance(records, list):
            return
        now = self._clock()
        for raw in records:
            if not isinstance(raw, Mapping):
                continue
            try:
                rec = Record.from_dict(raw)
            except TypeError:
                log.warning("scheduler restore: skipping malformed record %r", raw)
                continue
            if rec.state in (STATE_ACTIVE, STATE_CANCELLING):
                rec.state = STATE_ERROR
                rec.error = "control-plane restart"
                rec.done_ts = now
                self._history.append(rec.as_dict())
                self._append_history(rec.as_dict())
                log.warning("scheduler restore: %s was active at restart; marked error", rec.id)
                continue
            rec.ready_event = threading.Event()
            self._records[rec.id] = rec
            self._order.append(rec.id)
        log.info("scheduler restore: %d queued record(s) recovered", len(self._order))

    def _append_history(self, entry: dict[str, Any]) -> None:
        """Append one finished record to the JSONL ring buffer (best effort).

        The FILE is the durable ring: once it holds more than `history_cap`
        lines it is compacted (atomic rewrite keeping the newest cap), so it
        can never grow without bound.
        """
        try:
            self._history_path.parent.mkdir(parents=True, exist_ok=True)
            with self._history_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, separators=(",", ":"), default=str) + "\n")
            self._compact_history_if_needed()
        except OSError:
            log.debug("scheduler history append failed", exc_info=True)

    def _compact_history_if_needed(self) -> None:
        try:
            with self._history_path.open(encoding="utf-8") as fh:
                lines = fh.readlines()
        except OSError:
            return
        if len(lines) <= self._history_cap:
            return
        keep = lines[-self._history_cap:]
        tmp = self._history_path.with_suffix(".tmp")
        try:
            tmp.write_text("".join(keep), encoding="utf-8")
            os.replace(tmp, self._history_path)
        except OSError:
            log.debug("scheduler history compaction failed", exc_info=True)

    def _load_history_file(self) -> None:
        """Seed the in-memory ring from the durable JSONL at startup so
        /scheduler/history answers across a control-plane restart."""
        try:
            lines = self._history_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in lines[-self._history_cap:]:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict):
                self._history.append(entry)

    def _notify(self) -> None:
        """Wake drain() waiters (and any condition waiters) safely."""
        with self._idle:
            self._idle.notify_all()

    # ---------------------------------------------------------------- capacity
    def set_capacity(self, capacity: int) -> None:
        """Set the global active capacity (the running profile's max_num_seqs).

        Called on acquire / profile switch. A capacity increase immediately
        promotes queued records; a decrease lets the change apply naturally as
        active records finish (running requests are never preempted).
        """
        with self._lock:
            self._capacity = max(1, int(capacity))
            promoted = self._promote_locked()
        self._persist()
        if promoted:
            self._notify()

    @property
    def capacity(self) -> int:
        with self._lock:
            return self._capacity

    # ------------------------------------------------------------------ counts
    def _active_of(self, project: str) -> int:
        return sum(
            1 for r in self._records.values()
            if r.project == project and r.state in (STATE_ACTIVE, STATE_CANCELLING)
        )

    def _queued_of(self, project: str) -> int:
        return sum(
            1 for r in self._records.values()
            if r.project == project and r.state is STATE_QUEUED
        )

    def _active_count(self) -> int:
        return sum(
            1 for r in self._records.values()
            if r.state in (STATE_ACTIVE, STATE_CANCELLING)
        )

    def _queued_count(self) -> int:
        return sum(1 for r in self._records.values() if r.state is STATE_QUEUED)

    # ------------------------------------------------------------------ submit
    def submit(self, record: Mapping[str, Any]) -> dict[str, Any]:
        """Admit a request: active if a slot is free, else queued.

        Returns a decision dict:
          {"state": "active"}                       -- relay now
          {"state": "queued", "position": N}        -- wait; N is 1-based
          {"state": "rejected", "reason": ..., "position": N}
              -- queue full; the server answers 429 WITH the position.
        """
        rec = Record(
            id=str(record.get("id") or uuid.uuid4().hex),
            project=str(record.get("project") or DEFAULT_PROJECT) or DEFAULT_PROJECT,
            agent=str(record.get("agent") or DEFAULT_PROJECT),
            task=str(record.get("task") or ""),
            priority=valid_priority(record.get("priority")),
            profile=str(record.get("profile") or ""),
            reasoning=str(record.get("reasoning") or ""),
            state=STATE_QUEUED,
            enqueue_ts=self._clock(),
            timeout_at=self._clock() + float(record.get("timeout") or self._default_timeout),
        )
        rec.ready_event = threading.Event()
        with self._lock:
            if rec.id in self._records:
                return {"state": DECISION_REJECTED, "reason": f"duplicate id {rec.id}", "position": 0}
            if self._queued_count() >= self._global_queued_cap:
                return {
                    "state": DECISION_REJECTED,
                    "reason": (
                        f"global queue full ({self._queued_count()}/{self._global_queued_cap})"
                    ),
                    # Position beyond the cap: the caller learns where it WOULD sit.
                    "position": self._queued_count() + 1,
                }
            if self._queued_of(rec.project) >= self._proj_queued_cap:
                return {
                    "state": DECISION_REJECTED,
                    "reason": (
                        f"project '{rec.project}' queue full "
                        f"({self._queued_of(rec.project)}/{self._proj_queued_cap})"
                    ),
                    "position": self._queued_count() + 1,
                }
            self._records[rec.id] = rec
            promoted = self._promote_locked()
            if rec.state is STATE_ACTIVE:
                decision = {"state": DECISION_ACTIVE, "id": rec.id}
            else:
                # Position among same-or-better priority ahead of it (the
                # only number that is meaningful to a waiting caller).
                ahead = sum(
                    1 for other in self._records.values()
                    if other.state is STATE_QUEUED
                    and PRIORITIES.get(other.priority, 99) <= PRIORITIES.get(rec.priority, 99)
                )
                decision = {"state": DECISION_QUEUED, "id": rec.id, "position": ahead}
        self._persist()
        if promoted:
            self._notify()
        return decision

    # ----------------------------------------------------------------- promote
    def _promote_locked(self) -> "list[str]":
        """Fill free active slots. Strict priority first; within one priority
        level, round-robin across projects so no project monopolises the
        level. Projects at their active cap are skipped, not dropped."""
        promoted: list[str] = []
        while self._active_count() < self._capacity:
            chosen: "Record | None" = None
            best_prio = None
            # Candidates ordered by priority; within a priority, rotate the
            # starting project so consecutive slots go to different projects.
            for prio in sorted(PRIORITIES.values()):
                candidates = [
                    r for r in self._records.values()
                    if r.state is STATE_QUEUED
                    and PRIORITIES.get(r.priority, 99) == prio
                    and self._active_of(r.project) < self._proj_active_cap
                ]
                if not candidates:
                    continue
                # Round-robin: prefer a project other than the one that was
                # served last at this priority level. If only that project has
                # candidates, it is served again (correctly -- there is no one
                # else to alternate with).
                start = self._rr.get(prio)
                ordered = candidates
                if start is not None:
                    for idx, cand in enumerate(candidates):
                        if cand.project != start:
                            ordered = candidates[idx:] + candidates[:idx]
                            break
                chosen = ordered[0]
                best_prio = prio
                break
            if chosen is None:
                break
            chosen.state = STATE_ACTIVE
            chosen.start_ts = self._clock()
            assert chosen.ready_event is not None
            chosen.ready_event.set()
            if best_prio is not None:
                self._rr[best_prio] = chosen.project
            promoted.append(chosen.id)
        return promoted

    # ------------------------------------------------------------- wait / state
    def wait(self, record_id: str, timeout: "float | None" = None) -> str:
        """Block until `record_id` is active or terminal.

        Returns the terminal state string, or STATE_ACTIVE when promoted. A
        record that vanished (unknown id) answers STATE_ERROR immediately --
        callers must never hang on a lost id.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                rec = self._records.get(record_id)
                if rec is None:
                    return STATE_ERROR
                if rec.state in (STATE_ACTIVE, STATE_CANCELLING):
                    return rec.state
                if rec.state in _TERMINAL:
                    return rec.state
                event = rec.ready_event
            if event is None:
                return STATE_ERROR
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            if event.wait(timeout=remaining):
                continue
            with self._lock:
                rec = self._records.get(record_id)
                state = rec.state if rec else STATE_ERROR
            return state

    def get(self, record_id: str) -> "dict[str, Any] | None":
        with self._lock:
            rec = self._records.get(record_id)
            return rec.public() if rec else None

    def is_cancelling(self, record_id: str) -> bool:
        with self._lock:
            rec = self._records.get(record_id)
            return bool(rec and rec.state is STATE_CANCELLING)

    # ----------------------------------------------------------------- finish
    def record_finished(self, record_id: str, metrics: "Mapping[str, Any] | None" = None) -> None:
        """Mark a relayed request done, store its metrics, free the slot."""
        metrics = metrics or {}
        with self._lock:
            rec = self._records.get(record_id)
            if rec is None:
                return
            for key in _METRIC_KEYS:
                if key in metrics and metrics[key] is not None:
                    setattr(rec, key, metrics[key])
            # A cancel that raced the completion still wins for the record
            # state -- the caller asked for it -- but the metrics are kept.
            rec.state = STATE_CANCELLED if rec.state is STATE_CANCELLING else STATE_DONE
            rec.done_ts = self._clock()
            entry = rec.as_dict()
            del self._records[record_id]
            self._history.append(entry)
            self._promote_locked()
        self._persist()
        self._append_history(entry)
        self._notify()

    def record_error(self, record_id: str, err: str) -> None:
        with self._lock:
            rec = self._records.get(record_id)
            if rec is None:
                return
            rec.state = STATE_ERROR
            rec.error = str(err)[:500]
            rec.done_ts = self._clock()
            entry = rec.as_dict()
            del self._records[record_id]
            self._history.append(entry)
            self._promote_locked()
        self._persist()
        self._append_history(entry)
        self._notify()

    # ------------------------------------------------------------------ cancel
    def cancel(self, record_id: str, reason: str = "") -> dict[str, Any]:
        """Cancel by id. Queued: removed at once. Active: marked cancelling --
        the relaying thread checks is_cancelling() and aborts the relay; the
        record is finalised by its own record_finished/record_error path."""
        with self._lock:
            rec = self._records.get(record_id)
            if rec is None:
                return {"cancelled": False, "reason": "unknown or already finished"}
            if rec.state is STATE_QUEUED:
                rec.state = STATE_CANCELLED
                rec.error = str(reason or "cancelled")[:500]
                rec.done_ts = self._clock()
                entry = rec.as_dict()
                del self._records[record_id]
                self._history.append(entry)
                self._promote_locked()
                outcome = {"cancelled": True, "state": STATE_CANCELLED}
                # Wake the request thread parked in wait(): its record is gone.
                if rec.ready_event is not None:
                    rec.ready_event.set()
            elif rec.state is STATE_ACTIVE:
                rec.state = STATE_CANCELLING
                rec.error = str(reason or "cancel requested")[:500]
                outcome = {"cancelled": True, "state": STATE_CANCELLING}
            else:
                return {"cancelled": False, "reason": f"already {rec.state}"}
        self._persist()
        if outcome.get("state") == STATE_CANCELLED:
            self._append_history(entry)
            self._notify()
        return outcome

    def retry(self, record_id: str) -> dict[str, Any]:
        """Re-queue a terminal record with the same id, fresh timestamps."""
        with self._lock:
            old = self._records.pop(record_id, None)
            if old is None:
                from_history = None
                for entry in reversed(self._history):
                    if entry.get("id") == record_id:
                        from_history = entry
                        break
                if from_history is None:
                    return {"retried": False, "reason": "unknown id"}
                source = dict(from_history)
            else:
                source = old.as_dict()
            if source.get("state") not in _TERMINAL:
                return {"retried": False, "reason": f"still {source.get('state')}"}
        source.pop("ready_event", None)
        source.update({
            "state": STATE_QUEUED,
            "error": "",
            "start_ts": None, "done_ts": None,
            "prompt_tokens": None, "completion_tokens": None,
            "cached_tokens": None, "ttft_ms": None, "tps": None,
        })
        return self.submit(source)

    # ---------------------------------------------------------------- reaper
    def _reap_loop(self) -> None:
        while not self._stopping.wait(1.0):
            try:
                self.reap()
            except Exception as exc:  # noqa: BLE001 - the reaper must never die
                log.error("scheduler reaper error: %r", exc)

    def reap(self) -> "list[str]":
        """Expire queued+active records past their soft deadline."""
        now = self._clock()
        expired: list[str] = []
        entries: list[dict[str, Any]] = []
        with self._lock:
            for rec in list(self._records.values()):
                if rec.state in (STATE_QUEUED, STATE_ACTIVE, STATE_CANCELLING) and rec.timeout_at <= now:
                    rec.state = STATE_TIMEOUT
                    rec.error = f"soft timeout exceeded ({round(now - rec.enqueue_ts)}s)"
                    rec.done_ts = now
                    entries.append(rec.as_dict())
                    if rec.ready_event is not None:
                        rec.ready_event.set()  # wake a thread parked in wait()
                    del self._records[rec.id]
                    self._history.append(entries[-1])
                    expired.append(rec.id)
            if expired:
                self._promote_locked()
        if expired:
            self._persist()
            for entry in entries:
                self._append_history(entry)
            self._notify()
        return expired

    # ------------------------------------------------------------------ drain
    def drain(self, timeout: float = 300.0) -> int:
        """Wait until no active records remain (used before a release/switch).

        Returns the number still active when `timeout` elapsed. New submits
        are NOT blocked by this method -- callers that need a quiesced queue
        stop submitting first (the lifecycle release path does).
        """
        deadline = time.monotonic() + timeout
        with self._idle:
            while self._active_count() > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._idle.wait(timeout=min(remaining, 5.0))
            return self._active_count()

    # ------------------------------------------------------------------ status
    def status(self) -> dict[str, Any]:
        """Snapshot for the dashboard: depth, active, per-project counts,
        oldest wait, and every live record's state."""
        now = self._clock()
        with self._lock:
            live = [r.public() for r in self._records.values()]
            queued = [r for r in live if r["state"] == STATE_QUEUED]
            active = [r for r in live if r["state"] in (STATE_ACTIVE, STATE_CANCELLING)]
            projects: dict[str, dict[str, int]] = {}
            for r in live:
                slot = projects.setdefault(r["project"], {"active": 0, "queued": 0})
                if r["state"] in (STATE_ACTIVE, STATE_CANCELLING):
                    slot["active"] += 1
                elif r["state"] == STATE_QUEUED:
                    slot["queued"] += 1
            oldest = min((r["enqueue_ts"] for r in queued), default=None)
            return {
                "capacity": self._capacity,
                "active": len(active),
                "queued": len(queued),
                "projects": projects,
                "oldest_wait_seconds": round(now - oldest, 1) if oldest else 0.0,
                "limits": {
                    "per_project_active": self._proj_active_cap,
                    "per_project_queued": self._proj_queued_cap,
                    "global_queued": self._global_queued_cap,
                    "default_timeout": self._default_timeout,
                },
                "records": live,
                "generated_at": now,
            }

    def history(self, limit: int = 100) -> list[dict[str, Any]]:
        """Recent finished records, newest last."""
        with self._lock:
            return [dict(e) for e in list(self._history)[-max(1, limit):]]

    def shutdown(self) -> None:
        self._stopping.set()
