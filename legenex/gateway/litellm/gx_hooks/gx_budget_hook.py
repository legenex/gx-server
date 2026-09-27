"""LiteLLM proxy hook: attribution forwarding, context guard, text metrics (V4.1).

Loaded by LiteLLM from `litellm_settings.callbacks` in ../config.yaml, inside
the gx-litellm container. For the two public aliases (gx-max, gx-auto) it does:

1. PRE-CALL — attribution forwarding: captures the X-GX-* headers from the
   incoming request (project / agent / task / intent / priority / profile /
   reasoning) and forwards them to the orchestrator via
   ``data["forwarded_headers"]`` (and stashes a copy in
   ``data["metadata"]["gx_attribution"]`` for the metrics line — the same
   metadata mechanism the previous D-039 hook used to carry its budget).
   The orchestrator's scheduler and autoroute read them for priority,
   profile and reasoning selection.
2. PRE-CALL — context guard: applies the SAME budget the orchestrator uses
   (gx_orchestrator.budget, mounted read-only at /app/gx_lib) so a request
   whose input certainly cannot fit the served window is refused with a
   structured 400 before it enters the queue; an oversized `max_tokens` is
   clamped. The old per-tier clamps (gx-mini/gx-fast/gx-reason) are gone
   with those tiers; the orchestrator still re-budgets authoritatively.
3. LOGGING: one JSON line per request with timing, token counts, the
   attribution fields and the orchestrator-reported queue wait — never
   prompt or completion text — in $GX_TEXT_METRICS_LOG (default
   /var/log/gx-text/gateway-text.jsonl). The Control Center reads it for
   "last TTFT / tokens per second / outcome / queue wait".

Any failure inside this hook is logged and ignored: it must never break
serving. Imports of gx_orchestrator are guarded: the orchestrator is being
refactored for V4.1, and if its budget interface drifts the guard degrades
to pass-through instead of raising into the request path.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from litellm.integrations.custom_logger import CustomLogger

log = logging.getLogger("gx.budget_hook")

#: The two public aliases (registry schema 2). Everything else is passed through.
ALIASES = {"gx-max", "gx-auto"}

#: Attribution headers forwarded to the orchestrator (scheduler + autoroute).
ATTRIB_HEADERS = (
    "X-GX-Project", "X-GX-Agent", "X-GX-Task", "X-GX-Intent",
    "X-GX-Priority", "X-GX-Profile", "X-GX-Reasoning",
)
#: Response header the orchestrator stamps with the queue wait in milliseconds.
QUEUE_WAIT_HEADER = "x-gx-queue-wait-ms"

#: Default served window of the production pack (registry
#: dsv41-flash-exl3-uncensored.max_context = 262144). Only used when the
#: registry cannot be read; the registry value wins when it can.
DEFAULT_CONTEXT_LIMIT = 262144
#: Advertised output allowance for the guard (mirrors config.yaml model_info).
DEFAULT_MAX_OUTPUT = 16384

METRICS_LOG = Path(os.environ.get("GX_TEXT_METRICS_LOG", "/var/log/gx-text/gateway-text.jsonl"))
METRICS_MAX_BYTES = 10 * 1024 * 1024
_CHAT_CALLS = {"completion", "acompletion", "text_completion", "atext_completion"}
_write_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Optional dependency: the orchestrator's budget module (mounted at
# /app/gx_lib in the container). V4.1 refactor note: tiers.py is gone; only
# budget.py is used, and even that is imported defensively — if the interface
# drifts during the orchestrator refactor, the guard turns itself off instead
# of breaking every request.
# ---------------------------------------------------------------------------
_LIB = os.environ.get("GX_HOOK_LIB", "/app/gx_lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

try:  # pragma: no cover - exercised inside the container only
    from gx_orchestrator import budget as B  # noqa: E402
except Exception:  # noqa: BLE001 - drift must degrade, never raise
    B = None  # type: ignore[assignment]
    log.warning("gx_orchestrator.budget unavailable; context guard disabled")

_registry_cache: dict[str, Any] | None = None


def _load_registry() -> dict[str, Any] | None:
    """The alias -> served-window map from the registry, or None.

    The registry path comes from GX_REGISTRY (see .env.sample). Reading it is
    best-effort: on any problem the guard falls back to
    GX_MAX_CONTEXT_TOKENS, then to the 262144 default.
    """
    global _registry_cache
    if _registry_cache is not None:
        return _registry_cache
    path = os.environ.get("GX_REGISTRY", "")
    result: dict[str, Any] = {}
    if path:
        try:
            with open(path, encoding="utf-8") as fh:
                reg = json.load(fh)
            for alias, spec in (reg.get("aliases") or {}).items():
                model = (reg.get("models") or {}).get(spec.get("model")) or {}
                if isinstance(model.get("max_context"), int):
                    result[str(alias)] = model["max_context"]
        except (OSError, ValueError, AttributeError):
            log.warning("registry unreadable at %s; using fallback context limit", path)
    _registry_cache = result
    return result


def _context_limit(alias: str) -> int:
    limits = _load_registry()
    if isinstance(limits.get(alias), int):
        return int(limits[alias])
    env = os.environ.get("GX_MAX_CONTEXT_TOKENS", "")
    try:
        return int(env) if env else DEFAULT_CONTEXT_LIMIT
    except ValueError:
        return DEFAULT_CONTEXT_LIMIT


# ---------------------------------------------------------------------------
# Header helpers
# ---------------------------------------------------------------------------


def _header_value(headers: Any, name: str) -> str | None:
    """Fetch `name` from a headers mapping, case-insensitively."""
    if headers is None:
        return None
    try:
        value = headers.get(name)
        if value is not None:
            return value
        for key, candidate in getattr(headers, "items", dict)():
            if str(key).lower() == name.lower():
                return candidate
    except Exception:  # noqa: BLE001 - never break serving over a header
        return None
    return None


def _incoming_headers(data: dict[str, Any]) -> Any:
    """The incoming request's headers, from wherever this LiteLLM build puts them.

    Follows the same metadata mechanism the previous hook used for its budget:
    LiteLLM's proxy stores the original request headers in
    ``data["metadata"]["headers"]``; some builds expose the FastAPI request
    instead under ``proxy_server_request``. Both are tried, defensively.
    """
    meta = data.get("metadata")
    if isinstance(meta, dict):
        headers = meta.get("headers")
        if headers is not None:
            return headers
        inner = meta.get("requester_metadata")
        if isinstance(inner, dict) and inner.get("headers") is not None:
            return inner["headers"]
    proxy_request = data.get("proxy_server_request")
    if proxy_request is not None:
        headers = getattr(proxy_request, "headers", None)
        if headers is not None:
            return headers
    return None


def _capture_attribution(data: dict[str, Any]) -> dict[str, str]:
    """The X-GX-* attribution headers present on this request (bounded strings)."""
    headers = _incoming_headers(data)
    out: dict[str, str] = {}
    for name in ATTRIB_HEADERS:
        value = _header_value(headers, name)
        if value is not None and str(value).strip():
            out[name] = str(value)[:120]
    return out


# ---------------------------------------------------------------------------
# Metrics line
# ---------------------------------------------------------------------------


def _seconds(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.timestamp()
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _append(record: dict[str, Any]) -> None:
    line = json.dumps(record, separators=(",", ":"), default=str) + "\n"
    with _write_lock:
        try:
            METRICS_LOG.parent.mkdir(parents=True, exist_ok=True)
            if METRICS_LOG.exists() and METRICS_LOG.stat().st_size > METRICS_MAX_BYTES:
                os.replace(METRICS_LOG, METRICS_LOG.with_suffix(".jsonl.1"))
            with METRICS_LOG.open("a", encoding="utf-8") as fh:
                fh.write(line)
            os.chmod(METRICS_LOG, 0o644)
        except OSError:
            log.warning("gx text metrics write failed", exc_info=True)


def _queue_wait_ms(response_obj: Any, kwargs: dict) -> float | None:
    """The orchestrator's queue-wait report, from the upstream response headers.

    LiteLLM parks upstream response headers in
    ``response_obj._hidden_params["additional_headers"]``; the orchestrator
    stamps `x-gx-queue-wait-ms` there. Absent header -> None (not 0): a
    direct-path request was never queued.
    """
    try:
        hidden = getattr(response_obj, "_hidden_params", None) or {}
        headers = hidden.get("additional_headers") or {}
        raw = _header_value(headers, QUEUE_WAIT_HEADER)
        if raw is None:
            return None
        return round(float(raw), 1)
    except Exception:  # noqa: BLE001 - metrics must never break serving
        return None


# ---------------------------------------------------------------------------
# The hook
# ---------------------------------------------------------------------------


class GxBudgetHook(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):  # noqa: ANN001
        try:
            alias = str(data.get("model") or "")

            # 1. Attribution forwarding — for every call type, both aliases.
            attribution = _capture_attribution(data)
            meta = data.setdefault("metadata", {})
            if isinstance(meta, dict):
                meta["gx_attribution"] = attribution
            # LiteLLM forwards these headers with the upstream OpenAI call;
            # the orchestrator reads them for scheduler priority, profile and
            # reasoning (gx-auto autoroute), defaulting each to "unknown".
            data["forwarded_headers"] = dict(attribution)

            # 2. Context guard — chat calls on the two aliases only.
            if alias not in ALIASES or call_type not in _CHAT_CALLS or B is None:
                return data
            if not isinstance(data.get("messages"), list):
                return data
            budget = B.compute_budget(
                data, model=alias,
                context_limit=_context_limit(alias),
                max_output_limit=DEFAULT_MAX_OUTPUT,
            )
        except Exception:  # noqa: BLE001 - never break serving
            log.warning("gx hook pre-call failed; forwarding unchanged", exc_info=True)
            return data

        if isinstance(meta, dict):
            meta["gx_budget"] = budget.as_dict()
        if budget.status == B.STATUS_OVERFLOW:
            from fastapi import HTTPException  # noqa: PLC0415 - available in the proxy image

            _append({
                "ts": time.time(), "alias": alias, "event": "refused",
                "outcome": B.ERROR_CODE, "status": 400, "elapsed_ms": 0.0,
                **dict(B.iter_budget_fields(budget)),
            })
            raise HTTPException(
                status_code=400,
                detail=B.error_payload(budget, message=B.overflow_message(budget))["error"],
                headers={"x-should-retry": "false"},
            )
        if budget.status == B.STATUS_TIGHT:
            # The engine's exact count decides; do not shrink the caller's
            # budget on a pessimistic guess (its refusal comes back at once).
            return data
        data.update(B.apply_budget(data, budget))
        return data

    def _record(self, kwargs: dict, status: str, response_obj: Any, start_time: Any, end_time: Any) -> None:
        try:
            slo = kwargs.get("standard_logging_object") or {}
            alias = slo.get("model_group") or kwargs.get("model") or ""
            if alias not in ALIASES:
                return
            start = _seconds(slo.get("startTime")) or _seconds(start_time)
            end = _seconds(slo.get("endTime")) or _seconds(end_time)
            first = _seconds(slo.get("completionStartTime"))
            stream = bool(slo.get("stream") or kwargs.get("stream"))
            elapsed = (end - start) * 1000 if start and end else None
            ttft = (first - start) * 1000 if (stream and first and start and first >= start) else None
            completion = slo.get("completion_tokens") or None
            tps = None
            if completion and end:
                gen_s = end - (first if (stream and first) else start or end)
                if gen_s > 0:
                    tps = round(completion / gen_s, 1)
            params = slo.get("model_parameters") or {}
            meta = (kwargs.get("litellm_params") or {}).get("metadata") or {}
            attribution = meta.get("gx_attribution") if isinstance(meta, dict) else None
            attribution = attribution if isinstance(attribution, dict) else {}
            budget = meta.get("gx_budget") if isinstance(meta, dict) else None
            err = slo.get("error_information") or {}
            record = {
                "ts": time.time(),
                "event": "request",
                "alias": alias,
                "call_id": slo.get("litellm_call_id") or kwargs.get("litellm_call_id"),
                "outcome": "ok" if status == "success" else (err.get("error_class") or "error"),
                "status": 200 if status == "success" else err.get("error_code"),
                "error": (slo.get("error_str") or "")[:300] or None,
                "stream": stream,
                "elapsed_ms": round(elapsed, 1) if elapsed is not None else None,
                "ttft_ms": round(ttft, 1) if ttft is not None else None,
                "queue_wait_ms": _queue_wait_ms(response_obj, kwargs),
                # Attribution (privacy-safe: header values only, bounded at
                # capture time; never prompt or completion text).
                "project": attribution.get("X-GX-Project") or "unknown",
                "agent": attribution.get("X-GX-Agent") or "unknown",
                "task": attribution.get("X-GX-Task") or "unknown",
                "profile": attribution.get("X-GX-Profile") or None,
                "reasoning": attribution.get("X-GX-Reasoning") or None,
                "prompt_tokens": slo.get("prompt_tokens") or None,
                "completion_tokens": completion,
                "tokens_per_s": tps,
                "requested_output_tokens": params.get("max_tokens") or params.get("max_completion_tokens"),
            }
            if isinstance(budget, dict):
                for key in ("context_limit", "estimated_input_tokens", "tool_schema_tokens",
                            "safe_output_tokens", "output_tokens", "clamped", "status"):
                    record[f"budget_{key}" if key == "status" else key] = budget.get(key)
                record["requested_output_tokens"] = budget.get("requested_output_tokens")
            _append(record)
        except Exception:  # noqa: BLE001 - never break serving
            log.warning("gx text metrics record failed", exc_info=True)

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):  # noqa: ANN001
        self._record(kwargs, "success", response_obj, start_time, end_time)

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):  # noqa: ANN001
        self._record(kwargs, "failure", response_obj, start_time, end_time)


proxy_handler_instance = GxBudgetHook()
