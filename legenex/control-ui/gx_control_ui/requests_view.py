"""The Requests page: a read-mostly relay to the orchestrator scheduler.

PRIVACY: the scheduler does not store prompt bodies and neither do we. The
records relayed here are metadata only (id, project, agent, task, priority,
profile, reasoning, state, timestamps, token counts, timing, error strings).
This module must never read, store or forward a prompt; if a future scheduler
field ever appears here it is dropped by the field allow-list below.

Contract (ARCHITECTURE-V41.md section 4): the orchestrator exposes
GET /scheduler/status, GET /scheduler/history, POST /scheduler/cancel and
POST /scheduler/retry. When the orchestrator does not answer, every view says
so honestly ({"available": false, "reason": ...}) — no fabricated queue.
"""

from __future__ import annotations

import re
import time
from typing import Any

from .config import UIConfig
from .util import HTTPError, bearer, http_json

#: Fields a scheduler record may contribute to the browser. Anything else
#: (a future prompt field, headers, bodies) is dropped before rendering.
RECORD_FIELDS = ("id", "project", "agent", "task", "priority", "profile", "reasoning", "state",
                 "enqueue_ts", "start_ts", "done_ts", "timeout_at", "prompt_tokens",
                 "completion_tokens", "cached_tokens", "ttft_ms", "tps", "error")
STATES = ("queued", "active", "done", "error", "cancelled", "timeout")
_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")
MAX_HISTORY = 500


class RequestsError(Exception):
    def __init__(self, message: str, status: int = 502, code: str = "scheduler") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def _headers(cfg: UIConfig) -> dict[str, str]:
    return bearer(cfg.secret("GX_ORCHESTRATOR_API_KEY"))


def _clean(record: Any) -> dict | None:
    if not isinstance(record, dict):
        return None
    return {k: record.get(k) for k in RECORD_FIELDS if k in record}


def status(cfg: UIConfig) -> dict:
    """The live queue snapshot (queue depth, active generations, per-profile caps)."""
    if cfg.offline:
        return {"available": False, "reason": "offline mode"}
    try:
        code, body = http_json("GET", f"{cfg.orchestrator_base}/scheduler/status",
                               headers=_headers(cfg), timeout=5)
    except HTTPError as exc:
        return {"available": False, "reason": exc.message}
    if not 200 <= code < 300 or not isinstance(body, dict):
        return {"available": False, "reason": f"orchestrator answered HTTP {code}", "status": code}
    out = dict(body)
    out["available"] = True
    out["checked_at"] = time.time()
    return out


def history(cfg: UIConfig, *, project: str = "", agent: str = "", profile: str = "",
            reasoning: str = "", state: str = "", since: float | None = None,
            until: float | None = None, limit: int = 200) -> dict:
    """Scheduler history with filters. Filtering is done here so the orchestrator
    contract stays minimal; the record allow-list keeps privacy guarantees."""
    limit = max(1, min(int(limit or 200), MAX_HISTORY))
    if cfg.offline:
        return {"available": False, "reason": "offline mode", "records": []}
    try:
        code, body = http_json("GET", f"{cfg.orchestrator_base}/scheduler/history?limit={MAX_HISTORY}",
                               headers=_headers(cfg), timeout=6)
    except HTTPError as exc:
        return {"available": False, "reason": exc.message, "records": []}
    if not 200 <= code < 300:
        return {"available": False, "reason": f"orchestrator answered HTTP {code}", "records": []}
    raw = body if isinstance(body, list) else (body.get("records") or body.get("history") or [])
    records = [c for c in (_clean(r) for r in raw[:MAX_HISTORY]) if c is not None]
    if state and state in STATES:
        records = [r for r in records if r.get("state") == state]
    for key, value in (("project", project), ("agent", agent), ("profile", profile),
                       ("reasoning", reasoning)):
        if value:
            records = [r for r in records if str(r.get(key) or "").lower() == value.lower()]
    if since is not None:
        records = [r for r in records if float(r.get("enqueue_ts") or 0) >= since]
    if until is not None:
        records = [r for r in records if float(r.get("enqueue_ts") or 0) <= until]
    records.sort(key=lambda r: -float(r.get("enqueue_ts") or 0))
    return {"available": True, "records": records[:limit], "count": len(records),
            "filters": {"project": project, "agent": agent, "profile": profile,
                        "reasoning": reasoning, "state": state},
            "note": "metadata only; prompt bodies are never stored or exposed",
            "checked_at": time.time()}


def cancel(cfg: UIConfig, request_id: str) -> dict:
    if not _ID_RE.fullmatch(request_id or ""):
        raise RequestsError("invalid request id", 400, "invalid_id")
    if cfg.offline:
        raise RequestsError("offline mode", 503, "offline")
    try:
        code, body = http_json("POST", f"{cfg.orchestrator_base}/scheduler/cancel",
                               body={"id": request_id}, headers=_headers(cfg), timeout=30)
    except HTTPError as exc:
        raise RequestsError(exc.message, 502) from exc
    if not 200 <= code < 300:
        raise RequestsError(f"orchestrator answered HTTP {code}: {str(body)[:200]}", code)
    return body if isinstance(body, dict) else {"ok": True}


def retry(cfg: UIConfig, request_id: str) -> dict:
    if not _ID_RE.fullmatch(request_id or ""):
        raise RequestsError("invalid request id", 400, "invalid_id")
    if cfg.offline:
        raise RequestsError("offline mode", 503, "offline")
    try:
        code, body = http_json("POST", f"{cfg.orchestrator_base}/scheduler/retry",
                               body={"id": request_id}, headers=_headers(cfg), timeout=60)
    except HTTPError as exc:
        raise RequestsError(exc.message, 502) from exc
    if not 200 <= code < 300:
        raise RequestsError(f"orchestrator answered HTTP {code}: {str(body)[:200]}", code)
    return body if isinstance(body, dict) else {"ok": True}
