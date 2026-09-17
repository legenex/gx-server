"""LiteLLM proxy hook: context budgeting + text latency metrics (D-039).

Loaded by LiteLLM from `litellm_settings.callbacks` in ../config.yaml, inside
the gx-litellm container. It does two things for the text aliases:

1. PRE-CALL (gx-mini, gx-fast, gx-reason): applies the SAME budget the
   orchestrator uses (legenex/orchestrator/gx_orchestrator/budget.py, mounted
   read-only). A request whose input certainly cannot fit is refused with a
   structured 400 before it reaches an engine; an oversized `max_tokens` is
   clamped so input + output + margin fits the served window. gx-auto and
   gx-max are budgeted by the orchestrator itself and are left alone here.
2. LOGGING (all text aliases): one JSON line per request with timing and
   token counts, never prompt or completion text, in
   $GX_TEXT_METRICS_LOG (default /var/log/gx-text/gateway-text.jsonl). The
   Control Center reads it for "last TTFT / tokens per second / outcome".

Any failure inside this hook is logged and ignored: it must never break
serving.
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

_LIB = os.environ.get("GX_HOOK_LIB", "/app/gx_lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

from gx_orchestrator import budget as B  # noqa: E402
from gx_orchestrator.tiers import TIERS, Tier  # noqa: E402

log = logging.getLogger("gx.budget_hook")

#: Aliases budgeted here. gx-auto / gx-max go through the orchestrator.
BUDGETED = {Tier.MINI.value: Tier.MINI, Tier.FAST.value: Tier.FAST, Tier.REASON.value: Tier.REASON}
TEXT_ALIASES = {"gx-mini", "gx-fast", "gx-reason", "gx-max", "gx-auto"}
METRICS_LOG = Path(os.environ.get("GX_TEXT_METRICS_LOG", "/var/log/gx-text/gateway-text.jsonl"))
METRICS_MAX_BYTES = 10 * 1024 * 1024
_CHAT_CALLS = {"completion", "acompletion", "text_completion", "atext_completion"}
_write_lock = threading.Lock()


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


class GxBudgetHook(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):  # noqa: ANN001
        try:
            alias = str(data.get("model") or "")
            tier = BUDGETED.get(alias)
            if tier is None or call_type not in _CHAT_CALLS or not isinstance(data.get("messages"), list):
                return data
            spec = TIERS[tier]
            budget = B.compute_budget(
                data, model=alias, context_limit=spec.max_context, max_output_limit=spec.max_output
            )
        except Exception:  # noqa: BLE001 - never break serving
            log.warning("gx budget pre-call failed; forwarding unchanged", exc_info=True)
            return data

        meta = data.setdefault("metadata", {})
        if isinstance(meta, dict):
            meta["gx_budget"] = budget.as_dict()
        if budget.status == B.STATUS_OVERFLOW:
            from fastapi import HTTPException  # noqa: PLC0415 - available in the proxy image

            _append({
                "ts": time.time(), "alias": alias, "event": "refused", "outcome": B.ERROR_CODE,
                "status": 400, "elapsed_ms": 0.0, **dict(B.iter_budget_fields(budget)),
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
        slo = kwargs.get("standard_logging_object") or {}
        alias = slo.get("model_group") or kwargs.get("model") or ""
        if alias not in TEXT_ALIASES:
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

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):  # noqa: ANN001
        try:
            self._record(kwargs, "success", response_obj, start_time, end_time)
        except Exception:  # noqa: BLE001
            log.warning("gx text metrics (success) failed", exc_info=True)

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):  # noqa: ANN001
        try:
            self._record(kwargs, "failure", response_obj, start_time, end_time)
        except Exception:  # noqa: BLE001
            log.warning("gx text metrics (failure) failed", exc_info=True)


proxy_handler_instance = GxBudgetHook()
