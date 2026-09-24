"""The gx-cluster orchestrator HTTP service.

Two responsibilities, deliberately kept separate (see ARCHITECTURE.md):

  * ROUTING  -- the `gx-auto` alias picks a tier and forwards to the LiteLLM
    gateway. Routing never starts or stops processes.
  * LIFECYCLE -- the `gx-max` alias guarantees the two-node engine is up before
    a request is served, acquiring both nodes if needed. It NEVER falls back to
    a different model.

Served on 127.0.0.1:18900 by default: this is an internal control surface and
must not be exposed unauthenticated.
"""

from __future__ import annotations

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
from .classifier import request_fingerprint, route
from .config import CONFIG, Config
from .health import AliasState, TierHealth, TierStatus
from .lifecycle import AcquisitionError, GxMaxLifecycle, LifecycleStatus, State
from .tiers import TIERS, ROUTABLE, Tier
from .upstream import UpstreamError, open_post
from . import dual_worker

log = logging.getLogger("gx.server")

ROUTING_LOG = "gx.routing"

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,96}$")


class RoutingJournal:
    """Append-only JSONL record of gx-auto decisions (no prompt text).

    One `decision` record when a tier is chosen and one `completed` record
    when the response has been relayed, both carrying the request id and the
    messages fingerprint so a caller can find ITS decision, not "the last
    line". Size-bounded: the file rotates to `.1` past `max_bytes`.
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


def tier_budget(payload: dict[str, Any], tier: Tier, estimate: B.InputEstimate | None = None) -> B.ContextBudget:
    """The context budget of `payload` on `tier`'s served window (D-039)."""
    spec = TIERS[tier]
    return B.compute_budget(
        payload,
        model=tier.value,
        context_limit=spec.max_context,
        max_output_limit=spec.max_output,
        estimate=estimate,
    )


def clamp_output_budget(payload: dict[str, Any], tier: Tier) -> dict[str, Any]:
    """A copy of `payload` whose output budget fits `tier`'s window."""
    return B.apply_budget(payload, tier_budget(payload, tier))


#: A deterministic request failure is never retried by the orchestrator, and
#: OpenAI SDKs honour this header, so clients do not retry it either.
NO_RETRY_HEADERS = {"x-should-retry": "false"}
#: At most one immediate correction after the engine reports its exact count.
MAX_ATTEMPTS = 2

_USAGE_PROMPT = re.compile(rb'"prompt_tokens"\s*:\s*(\d+)')
_USAGE_COMPLETION = re.compile(rb'"completion_tokens"\s*:\s*(\d+)')
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


class TextMetrics:
    """Last request outcome per text alias served through this orchestrator."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last: dict[str, dict[str, Any]] = {}

    def record(self, alias: str, record: dict[str, Any]) -> None:
        with self._lock:
            self._last[alias] = dict(record)

    def seed(self, records: list[dict[str, Any]]) -> None:
        for rec in reversed(records):
            if rec.get("event") == "completed" and rec.get("tier"):
                with self._lock:
                    self._last.setdefault(rec["tier"], rec)

    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {k: dict(v) for k, v in self._last.items()}

#: How lifecycle.State maps onto the shared AliasState vocabulary (see
#: health.py). RELEASING has no exact match in that six-word vocabulary; it is
#: reported as LOADING (a transition in progress) rather than inventing a
#: seventh state just for gx-max.
_MAX_STATE_MAP: dict[State, AliasState] = {
    State.DOWN: AliasState.STOPPED,
    State.ACQUIRING: AliasState.QUEUED,
    State.READY: AliasState.READY,
    State.RELEASING: AliasState.LOADING,
}


def _gx_max_admission_blocked() -> str:
    """Return a reason string if gx-max could not be admitted from here, else "".

    gx-max is the one tier whose availability is not only a question of
    whether its process is running -- it is also whether it is allowed to
    start. Reporting `usable: true` for a tier that would be refused is a fake
    healthy state, which this project forbids (D-024).

    Since D-025 gx-max uses the cluster-takeover policy, not
    estimate+reserve. This probe checks the preconditions that DRAINING
    CANNOT FIX on node 1 -- /swapfile-sglang active, swap headroom for the
    load transient, no pre-existing memory pressure. It deliberately does not
    demand the drained-state MemAvailable: while gx-mini/gx-fast are loaded
    the node is not drained, and gx-max-start.sh drains before it judges
    that. The full two-node check runs, under both locks, at launch.

    Read-only, takes no lock, and a probe failure returns "" rather than
    inventing a fault.
    """
    try:
        from . import resource_guard as rg

        facts = rg.read_node_facts()
        result = rg.compute_takeover_admission(
            "node1",
            facts,
            other_exclusive_residents=[],
            policy=rg.TakeoverPolicy(clean_start_min_avail_gib=0.0),
        )
        return "" if result.allowed else f"admission_refused: {result.reason}"
    except Exception:  # noqa: BLE001 - never let a probe failure fake a fault
        log.debug("gx-max admission probe failed; reporting the lifecycle state as-is",
                  exc_info=True)
        return ""


def _max_tier_status(
    st: LifecycleStatus,
    node2_status: TierStatus,
    *,
    admission_blocked: "Callable[[], str] | None" = None,
) -> TierStatus:
    """Derive gx-max's TierStatus from its lifecycle state.

    This does NOT change gx-max's lifecycle semantics (DOWN stays a valid
    resting state, never a fault) -- it only reports that state through the
    same vocabulary as the other tiers, and folds in node 2 reachability so a
    human reading `gx status` sees "would need node 2, which is offline"
    instead of a bare false. `usable` is preserved EXACTLY from the original
    logic: READY, DOWN and ACQUIRING are all routable (gx-auto/direct gx-max
    may attempt acquisition from any of them); RELEASING is not.
    """
    alias_state = _MAX_STATE_MAP[st.state]
    usable = st.state in (State.READY, State.DOWN, State.ACQUIRING)

    if st.state is State.DOWN and node2_status.state is AliasState.UNAVAILABLE:
        # gx-max needs BOTH nodes (ARCHITECTURE.md L-2/L-6). DOWN is still a
        # valid resting state, but a human asking why it would fail right now
        # deserves the real reason, not a bare boolean. Checked BEFORE
        # admission: if node 2 is confirmed offline, that is the more
        # actionable answer, and the admission arithmetic is moot anyway.
        return TierStatus(alias_state, "node2_unavailable", usable=usable)

    if st.state is State.DOWN:
        # A DOWN gx-max still has to pass the admission guard to become READY.
        # If it cannot, say so, and do not call it usable.
        blocked = (admission_blocked or _gx_max_admission_blocked)()
        if blocked:
            return TierStatus(AliasState.UNAVAILABLE, blocked, usable=False)
    if st.last_error:
        return TierStatus(alias_state, st.last_error, usable=usable)
    return TierStatus(alias_state, st.detail or alias_state.value, usable=usable)


class Handler(BaseHTTPRequestHandler):
    server_version = "gx-orchestrator/1.1"
    protocol_version = "HTTP/1.1"

    cfg: Config
    lifecycle: GxMaxLifecycle
    health: TierHealth
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

    def _send_completed_chat(self, payload: dict[str, Any], headers: dict[str, str] | None, *, stream: bool) -> None:
        """Dual-worker is computed non-streaming; emit SSE when the client asked to stream."""
        if not stream:
            self._send_json(200, payload, headers)
            return
        content = ""
        try:
            content = str(((payload.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
        except (TypeError, AttributeError, IndexError):
            content = ""
        cid = str(payload.get("id") or f"chatcmpl-{uuid.uuid4()}")
        created = int(payload.get("created") or time.time())
        model = str(payload.get("model") or "gx-max")

        def chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
            return {
                "id": cid,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }

        frames = [chunk({"role": "assistant", "content": ""})]
        if content:
            frames.append(chunk({"content": content}))
        frames.append(chunk({}, finish="stop"))
        body = "".join(f"data: {json.dumps(frame)}\n\n" for frame in frames) + "data: [DONE]\n\n"
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

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

    def _auth_headers(self) -> dict[str, str]:
        key = self.cfg.gateway_key()
        return {"Authorization": f"Bearer {key}"} if key else {}

    # ------------------------------------------------------------------- GET
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"

        if path in ("/health", "/healthz", "/"):
            self._send_json(200, {"status": "ok", "service": "gx-orchestrator"})
            return

        if path == "/health/detailed":
            st = self.lifecycle.status()
            tiers = self.health.snapshot()
            max_status = _max_tier_status(st, tiers[Tier.REASON])
            tiers_out = {t.value: v.as_dict() for t, v in tiers.items()}
            tiers_out[Tier.MAX.value] = max_status.as_dict()
            self._send_json(
                200,
                {
                    "status": "ok",
                    "gx_max": st.as_dict(),
                    "tiers": tiers_out,
                    "gateway": self.cfg.gateway_base,
                },
            )
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
                            "id": alias.value,
                            "object": "model",
                            "created": now,
                            "owned_by": "gx-cluster",
                        }
                        for alias in (Tier.AUTO, *ROUTABLE)
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

        self._send_error_json(404, f"no such path: {path}", "not_found")

    def _text_status(self) -> dict[str, Any]:
        """Per-alias runtime + budget + last-request facts (Control Center)."""
        st = self.lifecycle.status()
        tiers = self.health.snapshot()
        states = {t.value: v.as_dict() for t, v in tiers.items()}
        states[Tier.MAX.value] = _max_tier_status(st, tiers[Tier.REASON]).as_dict()
        last = self.metrics.snapshot()
        out: dict[str, Any] = {}
        for tier in ROUTABLE:
            spec = TIERS[tier]
            out[tier.value] = {
                "node": spec.node,
                "context_limit": spec.max_context,
                "max_output": spec.max_output,
                "planning_output": spec.planning_output,
                "runtime": states.get(tier.value),
                "last_request": last.get(tier.value),
            }
        out[Tier.MAX.value]["lifecycle"] = st.as_dict()
        decisions = self.journal.find(limit=40)
        last_decision = next((d for d in decisions if d.get("event") == "decision"), None)
        out[Tier.AUTO.value] = {
            "last_request": last.get(Tier.AUTO.value),
            "last_decision": last_decision,
        }
        return {"aliases": out, "generated_at": time.time()}

    # ------------------------------------------------------------------ POST
    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        try:
            payload = self._read_json()
        except json.JSONDecodeError as exc:
            self._send_error_json(400, f"invalid JSON body: {exc}", "invalid_request", NO_RETRY_HEADERS)
            return
        if not isinstance(payload, dict):
            self._send_error_json(400, "request body must be a JSON object", "invalid_request", NO_RETRY_HEADERS)
            return

        if path == "/lifecycle/gx-max/acquire":
            try:
                self.lifecycle.acquire(timeout=payload.get("timeout"))
                self._send_json(200, {"status": "ready", **self.lifecycle.status().as_dict()})
            except AcquisitionError as exc:
                self._send_error_json(503, str(exc), "gx_max_unavailable")
            return

        if path == "/lifecycle/gx-max/release":
            self.lifecycle.release(
                force=bool(payload.get("force")),
                restore=payload.get("restore", True),
            )
            self._send_json(200, {"status": "released", **self.lifecycle.status().as_dict()})
            return

        if path in ("/v1/chat/completions", "/v1/completions"):
            self._handle_inference(path, payload)
            return

        self._send_error_json(404, f"no such path: {path}", "not_found")

    # ------------------------------------------------------- inference routing
    def _handle_inference(self, path: str, payload: dict[str, Any]) -> None:
        requested = str(payload.get("model") or "").strip()

        if requested == Tier.AUTO.value:
            self._serve_auto(path, payload)
        elif requested == Tier.MAX.value:
            self._serve_gx_max(path, payload, direct=True)
        else:
            self._send_error_json(
                400,
                f"orchestrator handles only '{Tier.AUTO.value}' and '{Tier.MAX.value}'; "
                f"got '{requested}'. Send other aliases to the LiteLLM gateway directly.",
                "invalid_model",
                NO_RETRY_HEADERS,
            )

    def _request_id(self) -> str:
        for name in ("X-GX-Request-Id", "X-Request-Id", "X-LiteLLM-Call-Id"):
            value = (self.headers.get(name) or "").strip()
            if value and _REQUEST_ID_RE.match(value):
                return value
        return uuid.uuid4().hex

    def _finish(self, alias: str, record: dict[str, Any]) -> None:
        self.journal.write(record)
        self.metrics.record(alias, record)

    def _serve_auto(self, path: str, payload: dict[str, Any]) -> None:
        started = time.monotonic()
        request_id = self._request_id()
        fingerprint = request_fingerprint(payload)
        tiers = self.health.snapshot()
        lc_status = self.lifecycle.status()
        max_status = _max_tier_status(lc_status, tiers.get(Tier.REASON) or tiers.get(Tier.CODE))

        # route() takes a plain tier->bool map; richer state lives in `tiers`
        # and `max_status` for /health/detailed and `gx status`.
        avail = {t: v.usable for t, v in tiers.items()}
        dual = self.cfg.gxmax_mode == "dual-worker"
        code_ok = bool((tiers.get(Tier.CODE) or tiers.get(Tier.FAST) or max_status).usable)
        avail[Tier.MAX] = code_ok if dual else max_status.usable
        max_ready = True if dual else lc_status.state is State.READY
        decision = route(payload, available=avail, busy={Tier.MAX: False if dual else lc_status.state is State.ACQUIRING})
        note = ""

        if decision.tier is Tier.MAX and not max_ready:
            # gx-auto may USE gx-max when it is already up. It must never
            # ACQUIRE DeepSeek. Dual-worker gx-max does not acquire SGLang.
            note = f"gx-max not running ({lc_status.state.value}); gx-auto does not acquire it"
            log.info("gx-auto selected gx-max but it is %s; routing to the best available "
                     "tier instead", lc_status.state.value)
            decision = route(payload, available=avail, busy={Tier.MAX: True})

        budget = tier_budget(payload, decision.tier, decision.features.estimate)
        record: dict[str, Any] = {
            "event": "decision",
            "ts": time.time(),
            "request_id": request_id,
            "fingerprint": fingerprint,
            "stream": bool(payload.get("stream")),
            **decision.as_log_dict(),
            "budget": budget.as_dict(),
        }
        if note:
            record["note"] = note
        # Every routing decision is logged, as required by the spec.
        logging.getLogger(ROUTING_LOG).info(json.dumps(record))
        self.journal.write(record)

        outcome = RelayOutcome(streamed=bool(payload.get("stream")))
        try:
            if decision.no_fit or budget.status == B.STATUS_OVERFLOW or (
                decision.tier is Tier.MAX and not max_ready
            ):
                # Nothing that is running can hold this input. Answer NOW with
                # the numbers, never with a retryable status.
                hint = ""
                if decision.tier is Tier.MAX and not decision.no_fit:
                    hint = (
                        f"Only gx-max ({TIERS[Tier.MAX].max_context} tokens) can hold it and gx-max is "
                        f"{lc_status.state.value}; gx-auto never starts the two-node tier. "
                        "Send it with model=gx-max to start it explicitly."
                    )
                outcome.status = 400
                outcome.error_code = B.ERROR_CODE
                outcome.elapsed_ms = (time.monotonic() - started) * 1000
                self._send_json(
                    400,
                    B.error_payload(
                        budget,
                        message=B.overflow_message(budget, hint),
                        elapsed_ms=outcome.elapsed_ms,
                        extra={"gx_max_state": lc_status.state.value},
                    ),
                    {**NO_RETRY_HEADERS, "X-GX-Request-Id": request_id},
                )
            elif decision.tier is Tier.MAX:
                if self.cfg.gxmax_mode == "dual-worker":
                    outcome = self._serve_dual_max(payload, budget=budget, request_id=request_id, started=started)
                else:
                    self.lifecycle.begin_use()
                    try:
                        outcome = self._relay(
                            f"{self.cfg.gxmax_base.rstrip('/')}/chat/completions",
                            {**payload, "model": self.cfg.gxmax_model_id},
                            routed_as=Tier.MAX,
                            budget=budget,
                            request_id=request_id,
                            started=started,
                        )
                    finally:
                        self.lifecycle.end_use()
            else:
                url = (
                    f"{self.cfg.gateway_base.rstrip('/')}/chat/completions"
                    if path == "/v1/chat/completions"
                    else f"{self.cfg.gateway_base.rstrip('/')}{path[len('/v1'):]}"
                )
                outcome = self._relay(
                    url,
                    {**payload, "model": decision.tier.value},
                    routed_as=decision.tier,
                    budget=budget,
                    headers=self._auth_headers(),
                    request_id=request_id,
                    started=started,
                )
        finally:
            done = {
                "event": "completed",
                "ts": time.time(),
                "request_id": request_id,
                "fingerprint": fingerprint,
                "tier": decision.tier.value,
                "via": Tier.AUTO.value,
                "summary": decision.summary(),
                "context_limit": budget.context_limit,
                "estimated_input_tokens": budget.estimated_input_tokens,
                "tool_schema_tokens": budget.tool_schema_tokens,
                "requested_output_tokens": budget.requested_output_tokens,
                "safe_output_tokens": budget.safe_output_tokens,
                "budget_status": budget.status,
                **outcome.as_dict(),
            }
            done["elapsed_ms"] = round((time.monotonic() - started) * 1000, 1)
            self._finish(Tier.AUTO.value, done)
            self.metrics.record(decision.tier.value, done)

    def _serve_gx_max(self, path: str, payload: dict[str, Any], *, direct: bool) -> None:
        """Serve a DIRECT gx-max request. Never downgrades -- fails loudly instead."""
        started = time.monotonic()
        request_id = self._request_id()
        budget = tier_budget(payload, Tier.MAX)
        outcome = RelayOutcome(streamed=bool(payload.get("stream")))
        try:
            if budget.status == B.STATUS_OVERFLOW:
                # Never take over both nodes for a request that cannot fit.
                outcome.status, outcome.error_code = 400, B.ERROR_CODE
                self._send_json(
                    400,
                    B.error_payload(budget, message=B.overflow_message(budget)),
                    {**NO_RETRY_HEADERS, "X-GX-Request-Id": request_id},
                )
                return
            if self.cfg.gxmax_mode == "dual-worker":
                outcome = self._serve_dual_max(payload, budget=budget, request_id=request_id, started=started)
            else:
                try:
                    self.lifecycle.acquire()
                except AcquisitionError as exc:
                    outcome.status, outcome.error_code, outcome.error = 503, "gx_max_unavailable", str(exc)[:300]
                    self._send_error_json(
                        503,
                        f"gx-max could not be brought up and will NOT be substituted with "
                        f"another model: {exc}",
                        "gx_max_unavailable",
                    )
                    return

                self.lifecycle.begin_use()
                try:
                    outcome = self._relay(
                        f"{self.cfg.gxmax_base.rstrip('/')}/chat/completions",
                        {**payload, "model": self.cfg.gxmax_model_id},
                        routed_as=Tier.MAX,
                        budget=budget,
                        request_id=request_id,
                        started=started,
                    )
                finally:
                    self.lifecycle.end_use()
        finally:
            done = {
                "event": "completed",
                "ts": time.time(),
                "request_id": request_id,
                "tier": Tier.MAX.value,
                "via": "direct",
                "context_limit": budget.context_limit,
                "estimated_input_tokens": budget.estimated_input_tokens,
                "tool_schema_tokens": budget.tool_schema_tokens,
                "requested_output_tokens": budget.requested_output_tokens,
                "safe_output_tokens": budget.safe_output_tokens,
                "budget_status": budget.status,
                **outcome.as_dict(),
            }
            done["elapsed_ms"] = round((time.monotonic() - started) * 1000, 1)
            self._finish(Tier.MAX.value, done)

    def _serve_dual_max(self, payload: dict[str, Any], *, budget, request_id: str, started: float) -> RelayOutcome:
        """Solver + independent reviewer using both gx-code workers."""
        key = self.cfg.gateway_key() or ""
        url = f"{self.cfg.gateway_base.rstrip('/')}/chat/completions"
        try:
            try:
                body = dual_worker.run_workflow(
                    gateway_chat_url=url,
                    api_key=key,
                    original=payload,
                    solver_model="gx-code-01",
                    reviewer_model="gx-code-02",
                    timeout=240,
                )
            except UpstreamError:
                body = dual_worker.run_workflow(
                    gateway_chat_url=url,
                    api_key=key,
                    original=payload,
                    solver_model="gx-code-01",
                    reviewer_model="gx-code-02",
                    timeout=240,
                )
            headers = {"X-GX-Request-Id": request_id, "X-GX-Max-Mode": "dual-worker"}
            self._send_completed_chat(body, headers, stream=bool(payload.get("stream")))
            elapsed = (time.monotonic() - started) * 1000
            return RelayOutcome(status=200, elapsed_ms=elapsed, streamed=bool(payload.get("stream")))
        except UpstreamError as exc:
            code = self._send_upstream_error(exc, request_id)
            return RelayOutcome(status=exc.status, error_code=code, error=str(exc)[:300], streamed=False)
        except Exception as exc:  # noqa: BLE001
            log.exception("gx-max dual-worker failed")
            self._send_error_json(503, f"gx-max dual-worker failed: {exc}", "gx_max_unavailable")
            return RelayOutcome(status=503, error_code="gx_max_unavailable", error=str(exc)[:300], streamed=False)

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
        routed_as: Tier,
        budget: B.ContextBudget | None,
        headers: dict[str, str] | None = None,
        request_id: str = "",
        started: float | None = None,
    ) -> RelayOutcome:
        """Relay one request with its budget applied.

        The upstream connection is opened BEFORE anything is written to the
        client, so every upstream refusal reaches the client as a real HTTP
        error. A context refusal that carries the engine's exact input count
        is corrected ONCE, immediately; nothing is ever resent unchanged.
        """
        started = started if started is not None else time.monotonic()
        out = RelayOutcome(streamed=bool(payload.get("stream")))
        current = budget
        body = B.apply_budget(payload, budget) if budget else dict(payload)
        resp = None
        while resp is None:
            out.attempts += 1
            out.output_tokens = current.output_tokens if current else None
            out.clamped = bool(current and current.clamped)
            try:
                resp = open_post(url, body, headers=headers, timeout=self.cfg.upstream_timeout)
            except UpstreamError as exc:
                engine = B.parse_context_error(exc.body) if 400 <= exc.status < 500 else None
                out.elapsed_ms = (time.monotonic() - started) * 1000
                if engine is not None and current is not None:
                    fixed = B.corrected_output(engine, current)
                    if fixed is not None and out.attempts < MAX_ATTEMPTS:
                        log.warning(
                            "%s refused the context (engine input %s, window %s); retrying once "
                            "with output %s instead of %s",
                            routed_as.value, engine.input_tokens, engine.context_limit, fixed,
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

        extra_headers = {"X-GX-Routed-To": routed_as.value}
        if request_id:
            extra_headers["X-GX-Request-Id"] = request_id
        if current is not None:
            extra_headers["X-GX-Context-Limit"] = str(current.context_limit)
            extra_headers["X-GX-Output-Tokens"] = str(current.output_tokens or "")
            extra_headers["X-GX-Output-Clamped"] = "true" if current.clamped else "false"
            if out.retry_reason:
                extra_headers["X-GX-Retry-Reason"] = out.retry_reason

        with resp:
            if out.streamed:
                self._relay_stream(resp, out, extra_headers, started)
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

    def _relay_stream(self, resp, out: RelayOutcome, headers: dict[str, str], started: float) -> None:
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
        if out.error:
            self.close_connection = True
        out.elapsed_ms = (time.monotonic() - started) * 1000
        self._usage(out, tail)


def build_servers(cfg: Config | None = None) -> tuple[list[ThreadingHTTPServer], GxMaxLifecycle]:
    """Build one HTTP server per configured bind address.

    A single lifecycle manager is SHARED across all binds: gx-max acquisition
    must be serialised globally, not per listening socket.
    """
    cfg = cfg or CONFIG
    lifecycle = GxMaxLifecycle(
        cfg.lifecycle_dir,
        health_url=f"{cfg.gxmax_base.rstrip('/').removesuffix('/v1')}/health",
        idle_ttl=cfg.gxmax_idle_ttl,
        acquire_timeout=cfg.gxmax_acquire_timeout,
        events_log=cfg.log_dir / "gx-max-lifecycle.log",
        history_path=cfg.state_dir / "orchestrator" / "gx-max-history.json",
    )
    journal = RoutingJournal(cfg.log_dir / "gx-auto-routing.jsonl")
    metrics = TextMetrics()
    metrics.seed(journal.find(limit=200))
    handler = type(
        "BoundHandler",
        (Handler,),
        {
            "cfg": cfg,
            "lifecycle": lifecycle,
            "health": TierHealth(cfg),
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
    servers, lifecycle = build_servers(cfg)
    for srv in servers:
        log.info("gx-orchestrator listening on http://%s:%s", *srv.server_address[:2])
    log.info("  gateway=%s  gx-max=%s  idle_ttl=%ss", cfg.gateway_base, cfg.gxmax_base, cfg.gxmax_idle_ttl)

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
