"""The gx-cluster orchestrator HTTP service (V4.1).

Two responsibilities, deliberately kept separate:

  * ROUTING  -- the `gx-auto` alias picks a (profile, reasoning) pair for the
    ONE model (DeepSeek V4.1 Flash EXL3) and injects the matching
    chat_template_kwargs. Routing never starts or stops processes.
  * LIFECYCLE -- the `gx-max` alias guarantees the engine is up before a
    request is served. It NEVER falls back to a different model.

Every inference request is admitted through the request-level scheduler
(ARCHITECTURE-V41 §4): global capacity is the running profile's
max_num_seqs, priorities are strict, and one project cannot monopolise the
engine.

Served on 127.0.0.1:18900 by default: this is an internal control surface and
must not be exposed unauthenticated.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.parse
import uuid
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from dataclasses import asdict, dataclass, replace

from . import budget as B
from . import scheduler as SCHED
from .autoroute import decide as autoroute_decide
from .autoroute import request_fingerprint
from .config import CONFIG, Config
from .health import AliasState, ClusterHealth, TierStatus
from .lifecycle import AcquisitionError, GxMaxLifecycle, LifecycleStatus, State
from .profiles import Registry, RegistryError, load_registry
from .scheduler import Scheduler
from .upstream import UpstreamError, open_post

log = logging.getLogger("gx.server")

ROUTING_LOG = "gx.routing"

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,96}$")
_PRIORITY_RE = re.compile(r"^[a-z0-9-]{1,32}$")

#: The two public aliases -- anything else is a 400, never a fallback.
ALIAS_DIRECT = "gx-max"
ALIAS_AUTO = "gx-auto"


class RoutingJournal:
    """Append-only JSONL record of gx-auto decisions (no prompt text).

    One `decision` record when (profile, reasoning) is chosen and one
    `completed` record when the response has been relayed, both carrying the
    request id and the messages fingerprint so a caller can find ITS
    decision, not "the last line". Size-bounded: the file rotates to `.1`
    past `max_bytes`.
    """

    def __init__(self, path: Path, max_bytes: int = 20 * 1024 * 1024) -> None:
        self.path = path
        self.max_bytes = max_bytes
        self._lock = threading.Lock()

    def write(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, separators=(",", ":"), default=str) + "\n"
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size > self.max_bytes:
                    os.replace(self.path, self.path.with_suffix(self.path.suffix + ".1"))
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(line)
            except OSError:
                log.warning("routing journal write failed", exc_info=True)

    def find(self, *, request_id: str = "", fingerprint: str = "", limit: int = 50) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        with self._lock:
            try:
                lines = self.path.read_text(encoding="utf-8").splitlines()[-5000:]
            except OSError:
                return out
        for line in reversed(lines):
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if request_id and rec.get("request_id") != request_id:
                continue
            if fingerprint and rec.get("fingerprint") != fingerprint:
                continue
            out.append(rec)
            if len(out) >= limit:
                break
        return out


def profile_budget(payload: dict[str, Any], registry: Registry, profile_name: str, cfg: Config,
                   estimate: B.InputEstimate | None = None) -> B.ContextBudget:
    """The context budget of `payload` on the profile's served window."""
    spec = registry.profile(profile_name)
    return B.compute_budget(
        payload,
        model=f"{ALIAS_DIRECT}:{profile_name}",
        context_limit=spec.max_model_len,
        max_output_limit=cfg.gxmax_max_output,
        estimate=estimate,
    )


#: A deterministic request failure is never retried by the orchestrator, and
#: OpenAI SDKs honour this header, so clients do not retry it either.
NO_RETRY_HEADERS = {"x-should-retry": "false"}
#: At most one immediate correction after the engine reports its exact count.
MAX_ATTEMPTS = 2

_USAGE_PROMPT = re.compile(rb'"prompt_tokens"\s*:\s*(\d+)')
_USAGE_COMPLETION = re.compile(rb'"completion_tokens"\s*:\s*(\d+)')
#: vLLM reports cached tokens with --enable-prompt-tokens-details.
_USAGE_CACHED = re.compile(rb'"cached_tokens"\s*:\s*(\d+)')
_FIRST_TOKEN = re.compile(rb'"(content|reasoning_content|reasoning)"\s*:\s*"[^"]|"tool_calls"\s*:\s*\[')


@dataclass
class RelayOutcome:
    """What happened to one proxied request (journal + Control Center)."""

    status: int = 0
    attempts: int = 0
    retry_reason: str | None = None
    error_code: str | None = None
    error: str | None = None
    streamed: bool = False
    ttft_ms: float | None = None
    elapsed_ms: float = 0.0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cached_tokens: int | None = None
    tokens_per_s: float | None = None
    output_tokens: int | None = None
    clamped: bool = False

    @property
    def outcome(self) -> str:
        if self.status == 200 and not self.error:
            return "ok"
        return self.error_code or ("error" if self.status else "aborted")

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["outcome"] = self.outcome
        return out

    def scheduler_metrics(self) -> dict[str, Any]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cached_tokens": self.cached_tokens,
            "ttft_ms": self.ttft_ms,
            "tps": self.tokens_per_s,
        }


class TextMetrics:
    """Last request outcome per alias served through this orchestrator."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last: dict[str, dict[str, Any]] = {}

    def record(self, alias: str, record: dict[str, Any]) -> None:
        with self._lock:
            self._last[alias] = dict(record)

    def seed(self, records: list[dict[str, Any]]) -> None:
        for rec in reversed(records):
            if rec.get("event") == "completed" and rec.get("alias"):
                with self._lock:
                    self._last.setdefault(rec["alias"], rec)

    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {k: dict(v) for k, v in self._last.items()}

#: How lifecycle.State maps onto the shared AliasState vocabulary (see
#: health.py). RELEASING has no exact match; it is reported as LOADING (a
#: transition in progress) rather than inventing a seventh state.
_MAX_STATE_MAP: dict[State, AliasState] = {
    State.DOWN: AliasState.STOPPED,
    State.ACQUIRING: AliasState.QUEUED,
    State.READY: AliasState.READY,
    State.RELEASING: AliasState.LOADING,
}


def model_tier_status(st: LifecycleStatus, worker_ok: bool) -> TierStatus:
    """The model's serving state in the shared vocabulary: lifecycle state
    folded with worker reachability so a human sees 'needs node 2, which is
    offline' instead of a bare boolean. `usable` preserves the lifecycle
    semantics exactly: READY, DOWN and ACQUIRING are all attemptable;
    RELEASING is not."""
    alias_state = _MAX_STATE_MAP[st.state]
    usable = st.state in (State.READY, State.DOWN, State.ACQUIRING)
    if st.state is State.DOWN and not worker_ok:
        return TierStatus(alias_state, "node2_unavailable", usable=usable)
    if st.last_error:
        return TierStatus(alias_state, st.last_error, usable=usable)
    return TierStatus(alias_state, st.detail or alias_state.value, usable=usable)


def _attribution(headers: Any) -> dict[str, str]:
    """Attribution headers -> scheduler record fields (ARCHITECTURE-V41 §4)."""
    def h(name: str, default: str) -> str:
        value = (headers.get(name) or "").strip()
        return value[:128] if value else default

    priority = h("X-GX-Priority", SCHED.DEFAULT_PRIORITY).lower()
    if priority not in SCHED.PRIORITIES:
        priority = SCHED.DEFAULT_PRIORITY
    return {
        "project": h("X-GX-Project", SCHED.DEFAULT_PROJECT),
        "agent": h("X-GX-Agent", SCHED.DEFAULT_PROJECT),
        "task": h("X-GX-Task", ""),
        "priority": priority,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "gx-orchestrator/2.0"
    protocol_version = "HTTP/1.1"

    cfg: Config
    registry: Registry
    lifecycle: GxMaxLifecycle
    health: ClusterHealth
    scheduler: Scheduler
    journal: RoutingJournal
    metrics: TextMetrics

    # ---------------------------------------------------------------- helpers
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        log.info("%s - %s", self.address_string(), fmt % args)

    def _send_json(self, status: int, payload: Any, headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(
        self,
        status: int,
        message: str,
        code: str = "orchestrator_error",
        headers: dict[str, str] | None = None,
        **extra: Any,
    ) -> None:
        err = {"message": message, "type": code, "code": code, **extra}
        self._send_json(status, {"error": err}, headers)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _authorized(self) -> bool:
        """Constant-time bearer check; refuses everything when no key is configured."""
        expected = self.cfg.orchestrator_key()
        header = self.headers.get("Authorization") or ""
        scheme, _, token = header.partition(" ")
        ok = bool(expected) and scheme.lower() == "bearer" and hmac.compare_digest(
            token.strip().encode(), expected.encode())
        if not ok:
            self._send_error_json(401, "orchestrator authentication required", "unauthorized",
                                  {"WWW-Authenticate": "Bearer", "Connection": "close"})
            self.close_connection = True
        return ok

    # ------------------------------------------------------------------- GET
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"

        if path in ("/health", "/healthz", "/"):
            self._send_json(200, {"status": "ok", "service": "gx-orchestrator"})
            return

        if not self._authorized():
            return

        if path == "/health/detailed":
            self._send_json(200, self._detailed_status())
            return

        if path == "/text/status":
            self._send_json(200, self._text_status())
            return

        if path == "/v1/models":
            now = int(time.time())
            self._send_json(
                200,
                {
                    "object": "list",
                    "data": [
                        {
                            "id": alias,
                            "object": "model",
                            "created": now,
                            "owned_by": "gx-cluster",
                        }
                        for alias in (ALIAS_DIRECT, ALIAS_AUTO)
                    ],
                },
            )
            return

        if path == "/routing/decisions":
            # Read-only lookup of gx-auto decisions by request id or
            # fingerprint. Contains features and reasons, never prompt text.
            query = urllib.parse.parse_qs(self.path.partition("?")[2])
            rid = (query.get("request_id") or [""])[0]
            fp = (query.get("fingerprint") or [""])[0]
            try:
                limit = max(1, min(200, int((query.get("limit") or ["50"])[0])))
            except ValueError:
                self._send_error_json(400, "limit must be an integer", "invalid_request")
                return
            self._send_json(200, {"data": self.journal.find(request_id=rid, fingerprint=fp, limit=limit)})
            return

        if path == "/lifecycle/gx-max/status":
            self._send_json(200, self.lifecycle.status().as_dict())
            return

        if path == "/lifecycle/gx-max/events":
            # Read-only: recent lifecycle script output, the job in progress
            # and finished jobs. Changes nothing, starts nothing.
            query = urllib.parse.parse_qs(self.path.partition("?")[2])
            try:
                after = int((query.get("after") or ["0"])[0])
                limit = int((query.get("limit") or ["200"])[0])
            except ValueError:
                self._send_error_json(400, "after and limit must be integers", "invalid_request")
                return
            self._send_json(200, self.lifecycle.events(after=after, limit=limit))
            return

        if path == "/scheduler/status":
            self._send_json(200, self.scheduler.status())
            return

        if path == "/scheduler/history":
            query = urllib.parse.parse_qs(self.path.partition("?")[2])
            try:
                limit = max(1, min(1000, int((query.get("limit") or ["100"])[0])))
            except ValueError:
                self._send_error_json(400, "limit must be an integer", "invalid_request")
                return
            self._send_json(200, {"data": self.scheduler.history(limit=limit)})
            return

        self._send_error_json(404, f"no such path: {path}", "not_found")

    # ---------------------------------------------------------------- status
    def _model_block(self) -> dict[str, Any]:
        """The one model's state, shape and provenance (Control Center)."""
        st = self.lifecycle.status()
        reg = self.registry
        model = reg.production_model()
        runtime = reg.runtime(reg.alias(ALIAS_DIRECT).runtime)
        nodes = self.health.snapshot()
        return {
            "id": self.cfg.gxmax_model_id,
            "uncensored": model.uncensored,
            "quant": model.quant,
            "vision": model.vision,
            "tools": model.tools,
            "max_context": model.max_context,
            "runtime": runtime.name,
            "lifecycle": st.as_dict(),
            "state": model_tier_status(st, self.health.worker_ok()).as_dict(),
            "profile": st.profile,
            "profiles": {name: spec.as_dict() for name, spec in reg.profiles.items()},
            "nodes": nodes,
        }

    def _detailed_status(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "model": self._model_block(),
            "queue": self.scheduler.status(),
            "gateway": self.cfg.gateway_base,
        }

    def _text_status(self) -> dict[str, Any]:
        """New /text/status shape: model state, profile, queue snapshot,
        node health, and the last request outcome per alias."""
        last = self.metrics.snapshot()
        return {
            "model": self._model_block(),
            "queue": self.scheduler.status(),
            "aliases": {
                ALIAS_DIRECT: {"last_request": last.get(ALIAS_DIRECT)},
                ALIAS_AUTO: {"last_request": last.get(ALIAS_AUTO)},
            },
            "generated_at": time.time(),
        }

    # ------------------------------------------------------------------ POST
    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if not self._authorized():   # before the body is read or parsed
            return
        try:
            payload = self._read_json()
        except json.JSONDecodeError as exc:
            self._send_error_json(400, f"invalid JSON body: {exc}", "invalid_request", NO_RETRY_HEADERS)
            return
        if not isinstance(payload, dict):
            self._send_error_json(400, "request body must be a JSON object", "invalid_request", NO_RETRY_HEADERS)
            return

        if path == "/lifecycle/gx-max/acquire":
            self._post_acquire(payload)
            return

        if path == "/lifecycle/gx-max/release":
            self.lifecycle.release(force=bool(payload.get("force")))
            self._send_json(200, {"status": "released", **self.lifecycle.status().as_dict()})
            return

        if path == "/lifecycle/gx-max/restart":
            self._post_restart(payload)
            return

        if path == "/lifecycle/gx-max/drain":
            try:
                timeout = min(1800, max(0.0, float(payload.get("timeout") or 300)))
            except (TypeError, ValueError):
                timeout = 300.0
            remaining = self.scheduler.drain(timeout)
            self._send_json(200, {"drained": remaining == 0, "still_active": remaining,
                                  **self.scheduler.status()})
            return

        if path == "/scheduler/cancel":
            rid = str(payload.get("id") or "").strip()
            if not rid:
                self._send_error_json(400, "id is required", "invalid_request", NO_RETRY_HEADERS)
                return
            self._send_json(200, self.scheduler.cancel(rid, str(payload.get("reason") or "")))
            return

        if path == "/scheduler/retry":
            rid = str(payload.get("id") or "").strip()
            if not rid:
                self._send_error_json(400, "id is required", "invalid_request", NO_RETRY_HEADERS)
                return
            self._send_json(200, self.scheduler.retry(rid))
            return

        if path in ("/v1/chat/completions", "/v1/completions"):
            self._handle_inference(path, payload)
            return

        self._send_error_json(404, f"no such path: {path}", "not_found")

    def _post_acquire(self, payload: dict[str, Any]) -> None:
        profile = str(payload.get("profile") or "").strip() or None
        try:
            if profile:
                # Unknown names are a 400, never a silent default profile.
                self.registry.profile(profile)
            timeout = payload.get("timeout")
            self.lifecycle.acquire(
                profile,
                timeout=float(timeout) if isinstance(timeout, (int, float)) else None,
            )
        except RegistryError as exc:
            self._send_error_json(400, str(exc), "invalid_profile", NO_RETRY_HEADERS)
            return
        except AcquisitionError as exc:
            self._send_error_json(503, str(exc), "gx_max_unavailable")
            return
        self._send_json(200, {"status": "ready", **self.lifecycle.status().as_dict()})

    def _post_restart(self, payload: dict[str, Any]) -> None:
        profile = str(payload.get("profile") or "").strip() or None
        if profile:
            try:
                self.registry.profile(profile)
            except RegistryError as exc:
                self._send_error_json(400, str(exc), "invalid_profile", NO_RETRY_HEADERS)
                return
        self.lifecycle.release(force=bool(payload.get("force")))
        try:
            self.lifecycle.acquire(profile)
        except AcquisitionError as exc:
            self._send_error_json(503, str(exc), "gx_max_unavailable")
            return
        self._send_json(200, {"status": "ready", **self.lifecycle.status().as_dict()})

    # ------------------------------------------------------- inference routing
    def _request_id(self) -> str:
        for name in ("X-GX-Request-Id", "X-Request-Id", "X-LiteLLM-Call-Id"):
            value = (self.headers.get(name) or "").strip()
            if value and _REQUEST_ID_RE.match(value):
                return value
        return uuid.uuid4().hex

    def _finish(self, alias: str, record: dict[str, Any]) -> None:
        self.journal.write(record)
        self.metrics.record(alias, record)

    def _handle_inference(self, path: str, payload: dict[str, Any]) -> None:
        requested = str(payload.get("model") or "").strip()
        if requested not in (ALIAS_AUTO, ALIAS_DIRECT):
            self._send_error_json(
                400,
                f"orchestrator handles only '{ALIAS_AUTO}' and '{ALIAS_DIRECT}'; "
                f"got '{requested}'. Send other traffic to the LiteLLM gateway directly.",
                "invalid_model",
                NO_RETRY_HEADERS,
            )
            return
        if requested == ALIAS_AUTO:
            self._serve_auto(path, payload)
        else:
            self._serve_direct(path, payload)

    def _route_once(self, payload: dict[str, Any]) -> tuple[str, str, dict[str, Any], RegistryError | None]:
        """Resolve (profile, reasoning, decision-log-fields) for gx-auto.

        The decision is made ONCE per request and journaled; overrides are
        validated against the registry (an unknown X-GX-Profile is a 400, not
        a silent default).
        """
        decision = autoroute_decide(payload, headers=self.headers)
        try:
            self.registry.profile(decision.profile)
            self.registry.reasoning_kwargs(decision.reasoning)
        except RegistryError as exc:
            return decision.profile, decision.reasoning, decision.as_log_dict(), exc
        return decision.profile, decision.reasoning, decision.as_log_dict(), None

    def _direct_profile(self) -> str:
        """gx-max serves with the RUNNING profile (or the registry default
        while DOWN); an X-GX-Profile header overrides."""
        override = (self.headers.get("X-GX-Profile") or "").strip()
        if override:
            return override
        return self.lifecycle.current_profile or self.registry.default_profile().name

    def _direct_reasoning(self, profile_name: str) -> str:
        override = (self.headers.get("X-GX-Reasoning") or "").strip()
        if override:
            return override
        return self.registry.profile(profile_name).reasoning_default

    def _common_gate(
        self,
        *,
        alias: str,
        profile_name: str,
        reasoning: str,
        payload: dict[str, Any],
        request_id: str,
        log_fields: dict[str, Any] | None,
    ) -> bool:
        """Validate profile/reasoning, compute the budget, and refuse what
        cannot fit BEFORE any acquisition (never take over both nodes for a
        request that cannot fit the window). Returns True when the request
        may proceed."""
        try:
            spec = self.registry.profile(profile_name)
            kwargs = self.registry.reasoning_kwargs(reasoning)
        except RegistryError as exc:
            self._send_error_json(400, str(exc), "invalid_profile", NO_RETRY_HEADERS,
                                  **{"X-GX-Request-Id": request_id})
            return False
        budget = profile_budget(payload, self.registry, profile_name, self.cfg)
        if budget.status == B.STATUS_OVERFLOW:
            self._send_json(
                400,
                B.error_payload(budget, message=B.overflow_message(budget)),
                {**NO_RETRY_HEADERS, "X-GX-Request-Id": request_id},
            )
            return False
        self._gate_state = {"spec": spec, "kwargs": kwargs, "budget": budget}
        return True

    def _scheduler_submit(self, request_id: str, attr: dict[str, str],
                          profile_name: str, reasoning: str, payload: dict[str, Any]) -> "dict[str, Any] | None":
        record = {
            "id": request_id,
            "profile": profile_name,
            "reasoning": reasoning,
            "timeout": payload.get("gx_timeout"),
            **attr,
        }
        decision = self.scheduler.submit(record)
        if decision.get("state") == SCHED.DECISION_REJECTED:
            # Queue full: 429 WITH the position (ARCHITECTURE-V41 §4).
            self._send_error_json(
                429,
                f"queue full: {decision.get('reason')}",
                "queue_full",
                {
                    "Retry-After": "10",
                    "X-GX-Queue-Position": str(decision.get("position")),
                    **NO_RETRY_HEADERS,
                },
                queue_position=decision.get("position"),
            )
            return None
        return decision

    def _ensure_acquired(self, profile_name: str, attr: dict[str, str], request_id: str) -> bool:
        """Lifecycle gate. DOWN/ACQUIRING is handled by priority:

        * interactive -- trigger the acquisition (queued with an event) and
          let the request wait in the scheduler queue.
        * anything else -- a clear 503 now; no silent waits beyond the queue.
        """
        state = self.lifecycle.status().state
        if state is State.READY:
            return True
        if attr["priority"] != "interactive":
            self._send_error_json(
                503,
                f"the model is {state.value} (DeepSeek V4.1 Flash is the only model and is "
                "never substituted); retry when it is up, raise X-GX-Priority: interactive "
                "to start it automatically, or use the lifecycle endpoint",
                "model_down",
                NO_RETRY_HEADERS,
                lifecycle_state=state.value,
            )
            return False
        if state is State.DOWN:
            # Trigger, do not block: this request queues like any other and
            # is admitted once the engine is READY.
            self.lifecycle._event("server", f"interactive request {request_id} triggered acquire")
            threading.Thread(
                target=self._trigger_acquire, args=(profile_name,), daemon=True,
                name=f"gxmax-trigger-{request_id[:8]}",
            ).start()
        return True

    def _trigger_acquire(self, profile_name: str) -> None:
        try:
            self.lifecycle.acquire(profile_name)
        except AcquisitionError as exc:
            log.error("triggered acquire for %s failed: %s", profile_name, exc)

    def _relay_phase(self, path: str, payload: dict[str, Any], request_id: str,
                     alias: str, profile_name: str, reasoning: str,
                     decision_fields: dict[str, Any] | None, started: float) -> None:
        """Submit to the scheduler, wait for admission, relay, record.

        Split from the alias-specific front halves so gx-max and gx-auto
        share one well-tested relay path.
        """
        attr = _attribution(self.headers)
        gate = self._gate_state
        assert gate is not None
        budget: B.ContextBudget = gate["budget"]
        kwargs: dict[str, Any] = gate["kwargs"]

        if not self._ensure_acquired(profile_name, attr, request_id):
            return

        decision = self._scheduler_submit(request_id, attr, profile_name, reasoning, payload)
        if decision is None:
            return

        if decision.get("state") == SCHED.DECISION_QUEUED:
            # Wait for admission. The engine coming up (or a slot freeing)
            # promotes this record; a cancel or timeout wakes us too.
            state = self.scheduler.wait(request_id, timeout=self.cfg.upstream_timeout)
            if state not in (SCHED.STATE_ACTIVE, SCHED.STATE_CANCELLING):
                snap = self.scheduler.get(request_id) or {}
                self._send_error_json(
                    503,
                    f"request left the queue as '{state}'"
                    + (f": {snap.get('error', '')}" if snap.get("error") else ""),
                    f"scheduler_{state}" if state else "scheduler_timeout",
                    NO_RETRY_HEADERS,
                    **{"X-GX-Request-Id": request_id},
                )
                return

        # Queue wait for the gateway hook (x-gx-queue-wait-ms): how long this
        # request sat in the queue before admission. Only stamped when the
        # request WAS queued -- the hook treats an absent header as "direct
        # path, never queued", and that must stay meaningful.
        queue_wait_ms = None
        rec = self.scheduler.get(request_id)
        if decision.get("state") == SCHED.DECISION_QUEUED and rec and rec.get("start_ts") and rec.get("enqueue_ts"):
            queue_wait_ms = round((rec["start_ts"] - rec["enqueue_ts"]) * 1000, 1)

        # Admission implies capacity; make sure the engine itself is READY
        # (the acquire above may still be warming -- join it, bounded).
        try:
            self.lifecycle.acquire(profile_name)
        except AcquisitionError as exc:
            self.scheduler.record_error(request_id, str(exc)[:300])
            self._send_error_json(503, str(exc), "gx_max_unavailable")
            return

        self.lifecycle.begin_use()
        try:
            body = dict(payload)
            body["model"] = self.cfg.gxmax_model_id
            # The reasoning mapping is decided ONCE (autoroute or profile
            # default) and injected verbatim -- never per-chunk negotiation.
            body["chat_template_kwargs"] = kwargs
            url = f"{self.cfg.gxmax_base.rstrip('/')}{path[len('/v1'):]}"
            outcome = self._relay(
                url, body, budget=budget, request_id=request_id, started=started,
                alias=alias, profile_name=profile_name, reasoning=reasoning,
                record_id=request_id, queue_wait_ms=queue_wait_ms,
            )
        finally:
            self.lifecycle.end_use()

        # Scheduler bookkeeping + journal: metrics from the relay outcome.
        if outcome.status == 200 and not outcome.error:
            self.scheduler.record_finished(request_id, outcome.scheduler_metrics())
        else:
            self.scheduler.record_error(
                request_id,
                f"{outcome.error_code or 'relay_error'}: {outcome.error or outcome.status}",
            )
        done = {
            "event": "completed",
            "ts": time.time(),
            "request_id": request_id,
            "alias": alias,
            "profile": profile_name,
            "reasoning": reasoning,
            "context_limit": budget.context_limit,
            "estimated_input_tokens": budget.estimated_input_tokens,
            "requested_output_tokens": budget.requested_output_tokens,
            "safe_output_tokens": budget.safe_output_tokens,
            "budget_status": budget.status,
            **outcome.as_dict(),
        }
        done["elapsed_ms"] = round((time.monotonic() - started) * 1000, 1)
        if decision_fields:
            done["routing"] = decision_fields
        self._finish(alias, done)

    # ------------------------------------------------------------ gx-auto
    def _serve_auto(self, path: str, payload: dict[str, Any]) -> None:
        started = time.monotonic()
        request_id = self._request_id()
        fingerprint = request_fingerprint(payload)
        profile_name, reasoning, fields, err = self._route_once(payload)
        if err is not None:
            # Overrides that name nothing real are a 400, never a silent
            # default profile.
            self._send_error_json(400, str(err), "invalid_profile", NO_RETRY_HEADERS,
                                  **{"X-GX-Request-Id": request_id})
            return
        record: dict[str, Any] = {
            "event": "decision",
            "ts": time.time(),
            "request_id": request_id,
            "fingerprint": fingerprint,
            "stream": bool(payload.get("stream")),
            **fields,
        }
        # Every routing decision is logged, as required by the spec.
        logging.getLogger(ROUTING_LOG).info(json.dumps(record))
        self.journal.write(record)

        if not self._common_gate(
            alias=ALIAS_AUTO, profile_name=profile_name, reasoning=reasoning,
            payload=payload, request_id=request_id, log_fields=None,
        ):
            return
        self._relay_phase(
            path, payload, request_id, ALIAS_AUTO, profile_name, reasoning,
            fields, started,
        )

    # ------------------------------------------------------------ gx-max
    def _serve_direct(self, path: str, payload: dict[str, Any]) -> None:
        started = time.monotonic()
        request_id = self._request_id()
        try:
            profile_name = self._direct_profile()
            reasoning = self._direct_reasoning(profile_name)
        except RegistryError as exc:
            # An X-GX-Profile/X-GX-Reasoning header that names nothing real
            # is a 400, never a silent default (and never a crash).
            self._send_error_json(400, str(exc), "invalid_profile", NO_RETRY_HEADERS,
                                  **{"X-GX-Request-Id": request_id})
            return
        if not self._common_gate(
            alias=ALIAS_DIRECT, profile_name=profile_name, reasoning=reasoning,
            payload=payload, request_id=request_id, log_fields=None,
        ):
            return
        self._relay_phase(
            path, payload, request_id, ALIAS_DIRECT, profile_name, reasoning,
            None, started,
        )

    # ------------------------------------------------------------------ relay
    def _send_upstream_error(self, exc: UpstreamError, request_id: str) -> str:
        """Relay an upstream HTTP error with its own status and body."""
        headers = {"X-GX-Request-Id": request_id} if request_id else {}
        if 400 <= exc.status < 500:
            headers.update(NO_RETRY_HEADERS)
        code = "upstream_error"
        try:
            body = json.loads(exc.body)
        except (json.JSONDecodeError, ValueError):
            body = None
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            code = str(body["error"].get("code") or body["error"].get("type") or code)
            body["error"].setdefault("retryable", exc.status >= 500)
            self._send_json(exc.status, body, headers)
        else:
            self._send_error_json(exc.status, exc.body[:2000], code, headers, retryable=exc.status >= 500)
        return code

    def _relay(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        budget: B.ContextBudget,
        headers: dict[str, str] | None = None,
        request_id: str = "",
        started: "float | None" = None,
        alias: str = "",
        profile_name: str = "",
        reasoning: str = "",
        record_id: str = "",
        queue_wait_ms: "float | None" = None,
    ) -> RelayOutcome:
        """Relay one request with its budget applied.

        The upstream connection is opened BEFORE anything is written to the
        client, so every upstream refusal reaches the client as a real HTTP
        error. A context refusal that carries the engine's exact input count
        is corrected ONCE, immediately; nothing is ever resent unchanged.
        A scheduler cancel aborts a stream mid-flight (checked per chunk).
        """
        started = started if started is not None else time.monotonic()
        out = RelayOutcome(streamed=bool(payload.get("stream")))
        current = budget
        body = B.apply_budget(payload, budget)
        resp = None
        while resp is None:
            out.attempts += 1
            out.output_tokens = current.output_tokens
            out.clamped = bool(current.clamped)
            try:
                resp = open_post(url, body, headers=headers, timeout=self.cfg.upstream_timeout)
            except UpstreamError as exc:
                engine = B.parse_context_error(exc.body) if 400 <= exc.status < 500 else None
                out.elapsed_ms = (time.monotonic() - started) * 1000
                if engine is not None and current is not None:
                    fixed = B.corrected_output(engine, current)
                    if fixed is not None and out.attempts < MAX_ATTEMPTS:
                        log.warning(
                            "the engine refused the context (engine input %s, window %s); "
                            "retrying once with output %s instead of %s",
                            engine.input_tokens, engine.context_limit, fixed,
                            current.output_tokens,
                        )
                        current = replace(current, output_tokens=fixed, clamped=True,
                                          safe_output_tokens=fixed, status=B.STATUS_CLAMPED)
                        body = B.apply_budget(payload, current)
                        out.retry_reason = "engine_context_count"
                        continue
                    out.status, out.error_code = 400, B.ERROR_CODE
                    out.error = exc.body[:300]
                    self._send_json(
                        400,
                        B.error_payload(
                            current,
                            message=B.overflow_message(
                                current,
                                f"The engine reported {engine.input_tokens} input tokens for a "
                                f"{engine.context_limit or current.context_limit}-token window."
                                if engine.input_tokens else "",
                            ),
                            engine=engine,
                            attempts=out.attempts,
                            elapsed_ms=out.elapsed_ms,
                        ),
                        {**NO_RETRY_HEADERS, "X-GX-Request-Id": request_id},
                    )
                    return out
                log.error("upstream %s failed: %s", url, exc)
                out.status = exc.status
                out.error = exc.body[:300]
                out.error_code = self._send_upstream_error(exc, request_id)
                return out
            except Exception as exc:  # noqa: BLE001 - connection refused, timeout, DNS
                log.error("upstream %s unreachable: %r", url, exc)
                out.status, out.error_code, out.error = 502, "bad_gateway", repr(exc)[:300]
                out.elapsed_ms = (time.monotonic() - started) * 1000
                self._send_error_json(502, f"upstream unreachable: {exc!r}", "bad_gateway",
                                      {"X-GX-Request-Id": request_id} if request_id else None)
                return out

        extra_headers = {
            "X-GX-Routed-To": alias,
            "X-GX-Profile": profile_name,
            "X-GX-Reasoning": reasoning,
        }
        if request_id:
            extra_headers["X-GX-Request-Id"] = request_id
        extra_headers["X-GX-Context-Limit"] = str(current.context_limit)
        extra_headers["X-GX-Output-Tokens"] = str(current.output_tokens or "")
        extra_headers["X-GX-Output-Clamped"] = "true" if current.clamped else "false"
        if out.retry_reason:
            extra_headers["X-GX-Retry-Reason"] = out.retry_reason
        # The gateway budget hook reads exactly this header (absent = the
        # request was never queued, a direct-path serve).
        if queue_wait_ms is not None:
            extra_headers["x-gx-queue-wait-ms"] = str(queue_wait_ms)

        with resp:
            if out.streamed:
                self._relay_stream(resp, out, extra_headers, started, record_id)
            else:
                data = resp.read()
                out.status = resp.status
                out.elapsed_ms = (time.monotonic() - started) * 1000
                self._send_bytes(resp.status, data, extra_headers)
                self._usage(out, data)
        if out.completion_tokens and out.elapsed_ms:
            gen_ms = out.elapsed_ms - (out.ttft_ms or 0.0)
            if gen_ms > 0:
                out.tokens_per_s = round(out.completion_tokens / (gen_ms / 1000), 1)
        return out

    def _send_bytes(self, status: int, data: bytes, headers: dict[str, str]) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    @staticmethod
    def _usage(out: RelayOutcome, data: bytes) -> None:
        m = _USAGE_PROMPT.findall(data)
        if m:
            out.prompt_tokens = int(m[-1])
        m = _USAGE_COMPLETION.findall(data)
        if m:
            out.completion_tokens = int(m[-1])
        m = _USAGE_CACHED.findall(data)
        if m:
            out.cached_tokens = int(m[-1])

    def _relay_stream(self, resp, out: RelayOutcome, headers: dict[str, str], started: float,
                      record_id: str = "") -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        out.status = 200
        tail = b""
        client_gone = False
        cancelled = False

        def write(chunk: bytes) -> None:
            self.wfile.write(f"{len(chunk):X}\r\n".encode())
            self.wfile.write(chunk)
            self.wfile.write(b"\r\n")
            self.wfile.flush()

        try:
            while True:
                chunk = resp.read1(65536) if hasattr(resp, "read1") else resp.read(8192)
                if not chunk:
                    break
                if out.ttft_ms is None and _FIRST_TOKEN.search(tail[-256:] + chunk):
                    out.ttft_ms = round((time.monotonic() - started) * 1000, 1)
                tail = (tail + chunk)[-8192:]
                try:
                    write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    client_gone = True
                    out.error, out.error_code = "client disconnected", "client_disconnected"
                    break
                # An admin cancel marks the record cancelling; abort the
                # relay as soon as we see it (checked per chunk, cheap).
                if record_id and self.scheduler.is_cancelling(record_id):
                    cancelled = True
                    out.error, out.error_code = "cancelled by request", "cancelled"
                    break
        except Exception as exc:  # noqa: BLE001 - upstream died mid-stream
            log.error("upstream stream failed after %d bytes: %r", len(tail), exc)
            out.error, out.error_code = repr(exc)[:300], "upstream_stream_error"
            event = {"error": {"message": f"upstream stream failed: {exc!r}",
                               "type": "upstream_stream_error", "code": "upstream_stream_error"}}
            try:
                write(f"data: {json.dumps(event)}\n\n".encode())
            except OSError:
                client_gone = True
        if not client_gone:
            try:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except OSError:
                pass
        if out.error or cancelled:
            self.close_connection = True
        out.elapsed_ms = (time.monotonic() - started) * 1000
        self._usage(out, tail)


def build_servers(cfg: Config | None = None) -> tuple[list[ThreadingHTTPServer], GxMaxLifecycle]:
    """Build one HTTP server per configured bind address.

    A single lifecycle manager, scheduler, registry and journal are SHARED
    across all binds: acquisition and admission are global, not per socket.
    """
    cfg = cfg or CONFIG
    registry = load_registry(cfg.registry_path)

    scheduler = Scheduler(
        queue_path=cfg.scheduler_dir / "queue.json",
        history_path=cfg.scheduler_dir / "history.jsonl",
        capacity=registry.default_profile().max_num_seqs,
        per_project_active_cap=cfg.per_project_active_cap,
        per_project_queued_cap=cfg.per_project_queued_cap,
        global_queued_cap=cfg.global_queued_cap,
        default_timeout=cfg.request_timeout,
        history_cap=cfg.history_cap,
    )
    runtime = registry.runtime(registry.alias(ALIAS_DIRECT).runtime)
    lifecycle = GxMaxLifecycle(
        cfg.runtime_dir,
        api_base=runtime.api,
        model_id=runtime.served_model_id,
        registry=registry,
        idle_ttl=cfg.gxmax_idle_ttl,
        acquire_timeout=cfg.gxmax_acquire_timeout,
        events_log=cfg.log_dir / "gx-max-lifecycle.log",
        history_path=cfg.state_dir / "orchestrator" / "gx-max-history.json",
        drain_hook=scheduler.drain,
        on_ready=lambda profile, max_seqs: scheduler.set_capacity(max_seqs),
        extra_env={"SERVED_MODEL_NAME": runtime.served_model_id},
    )
    health = ClusterHealth(cfg, registry)
    journal = RoutingJournal(cfg.log_dir / "gx-auto-routing.jsonl")
    metrics = TextMetrics()
    metrics.seed(journal.find(limit=200))
    handler = type(
        "BoundHandler",
        (Handler,),
        {
            "cfg": cfg,
            "registry": registry,
            "lifecycle": lifecycle,
            "health": health,
            "scheduler": scheduler,
            "journal": journal,
            "metrics": metrics,
        },
    )
    servers: list[ThreadingHTTPServer] = []
    for host in cfg.hosts:
        try:
            httpd = ThreadingHTTPServer((host, cfg.port), handler)
        except OSError as exc:
            # A missing docker bridge must not stop the orchestrator booting.
            log.warning("cannot bind %s:%s (%s); skipping this address", host, cfg.port, exc)
            continue
        httpd.daemon_threads = True
        servers.append(httpd)
    if not servers:
        raise RuntimeError(f"could not bind any of {cfg.hosts!r} on port {cfg.port}")
    return servers, lifecycle


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
        stream=sys.stdout,
    )
    cfg = CONFIG
    try:
        servers, lifecycle = build_servers(cfg)
    except RegistryError as exc:
        # A broken registry is a stop-the-line fault: never serve with
        # guessed profiles or model ids.
        log.error("FATAL: %s", exc)
        return 2
    for srv in servers:
        log.info("gx-orchestrator listening on http://%s:%s", *srv.server_address[:2])
    log.info("  model api=%s  model id=%s  idle_ttl=%ss",
             cfg.gxmax_base, cfg.gxmax_model_id, cfg.gxmax_idle_ttl)

    threads = [threading.Thread(target=s.serve_forever, daemon=True, name=f"http-{s.server_address[0]}") for s in servers]
    for t in threads:
        t.start()
    try:
        while any(t.is_alive() for t in threads):
            for t in threads:
                t.join(timeout=1.0)
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        for srv in servers:
            srv.shutdown()
            srv.server_close()
        lifecycle.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
