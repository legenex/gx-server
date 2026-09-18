"""Clients for the Build V3 node-2 supervisors (gx-voice, gx-call, gx-live).

Resource Control uses exactly the sanctioned lifecycle paths each supervisor
publishes (plt.md section 5 and the specialists' contracts):

    GET  /health                              open: state, busy, pinned, memory.pending_gib ...
    POST /v1/<svc>/load                       bearer key; loads through the node-2 guard
    POST /v1/<svc>/unload {"if_idle": bool}   bearer key; 409 while busy / pinned

Keys are read per call from their 0600 files and never leave this process.
Addresses are fabric-only (L-3).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .redact import redact
from .util import HTTPError, http


@dataclass(frozen=True)
class ServiceSpec:
    alias: str
    prefix: str          # URL segment: /v1/<prefix>/...
    port: int
    container: str       # engine container (drain verification)
    ledger: str          # residency-ledger entry name
    label: str


SPECS: dict[str, ServiceSpec] = {
    "gx-voice": ServiceSpec("gx-voice", "voice", 18830, "gx-voice-engine", "gx-voice", "Voice (Qwen3-TTS)"),
    "gx-call": ServiceSpec("gx-call", "call", 18840, "gx-call-engine", "gx-call-engine", "Call agents (VoiceChat 11B)"),
    "gx-live": ServiceSpec("gx-live", "live", 18850, "gx-live-engine", "gx-live-engine", "Live (MiniCPM-o 4.5)"),
}
#: supervisor health state -> Resource Control state
STATE_MAP = {"unloaded": "UNLOADED", "waiting": "WAITING", "loading": "LOADING", "ready": "READY",
             "busy": "GENERATING", "unloading": "DRAINING", "failed": "ERROR", "error": "ERROR"}


class ServiceError(Exception):
    def __init__(self, message: str, status: int = 503) -> None:
        super().__init__(message)
        self.status = status


class Node2Service:
    def __init__(self, spec: ServiceSpec, base: str, key_file: Path, *, timeout: float = 3.0) -> None:
        self.spec = spec
        self.base = base.rstrip("/")
        self.key_file = Path(key_file)
        self.timeout = timeout
        self._cache: tuple[float, dict] = (0.0, {})

    @property
    def configured(self) -> bool:
        try:
            return len(self.key_file.read_text(encoding="utf-8").strip()) >= 16
        except OSError:
            return False

    def _key(self) -> str:
        try:
            key = self.key_file.read_text(encoding="utf-8").strip()
        except OSError:
            raise ServiceError(f"{self.spec.alias} is not configured on gx10-01 (no service key)") from None
        if len(key) < 16:
            raise ServiceError(f"{self.spec.alias} service key on gx10-01 is invalid")
        return key

    def health(self, *, max_age: float = 3.0) -> dict:
        """The open /health body, or {"reachable": False, "error": ...}. Cached briefly."""
        now = time.monotonic()
        if self._cache[1] and now - self._cache[0] < max_age:
            return self._cache[1]
        try:
            res = http("GET", f"{self.base}/health", timeout=self.timeout)
            body = res.json() if res.status == 200 else None
        except (HTTPError, ValueError) as exc:
            body = None
            error = str(exc)
        else:
            error = f"HTTP {res.status}" if body is None else ""
        out = {**body, "reachable": True} if isinstance(body, dict) else {"reachable": False,
                                                                           "error": redact(error)[:200]}
        self._cache = (now, out)
        return out

    def invalidate(self) -> None:
        self._cache = (0.0, {})

    def _post(self, op: str, body: dict, timeout: float) -> dict:
        try:
            res = http("POST", f"{self.base}/v1/{self.spec.prefix}/{op}", body=body,
                       headers={"Authorization": f"Bearer {self._key()}"}, timeout=timeout)
        except HTTPError:
            raise ServiceError(f"{self.spec.alias} on gx10-02 is not reachable") from None
        finally:
            self.invalidate()
        try:
            data = res.json()
        except ValueError:
            data = None
        if 200 <= res.status < 300:
            return data if isinstance(data, dict) else {}
        err = (data or {}).get("error") if isinstance(data, dict) else None
        message = err.get("message") if isinstance(err, dict) else f"HTTP {res.status}"
        raise ServiceError(redact(str(message))[:300], res.status if res.status in (400, 409, 503) else 502)

    def load(self) -> dict:
        return self._post("load", {}, timeout=900)

    def unload(self, *, if_idle: bool, reason: str = "manual") -> dict:
        return self._post("unload", {"if_idle": if_idle, "reason": reason}, timeout=180)

    @staticmethod
    def unloaded(info: Any) -> bool:
        """Did an unload answer prove the engine is gone?"""
        return isinstance(info, dict) and (bool(info.get("container_gone")) or bool(info.get("noop")))


def build(cfg: Any) -> dict[str, Node2Service]:
    out = {}
    for alias, spec in SPECS.items():
        base = getattr(cfg, f"{spec.prefix}_base", None) or f"http://192.168.100.11:{spec.port}"
        out[alias] = Node2Service(spec, base, Path(cfg.secrets_root) / alias / "api-key")
    return out


def runtime_view(alias: str, health: dict, *, gx_busy: bool, maint: bool) -> dict:
    """Resource Control runtime entry for a node-2 supervisor (pure)."""
    _mem_raw = health.get("memory")
    mem: dict = _mem_raw if isinstance(_mem_raw, dict) else {}
    if gx_busy:
        state, detail = "BLOCKED", "drained while gx-max owns the cluster"
    elif not health.get("reachable"):
        state, detail = "ERROR", f"{alias} supervisor on gx10-02 is not reachable"
    else:
        raw = str(health.get("state") or health.get("engine") or "unknown").lower()
        state = STATE_MAP.get(raw, "ERROR")
        waiting = health.get("waiting") if isinstance(health.get("waiting"), dict) else None
        detail = (waiting or {}).get("reason") or health.get("blocked_by") or {
            "UNLOADED": "loads with the next request", "READY": "loaded", "LOADING": "loading",
            "GENERATING": "working", "DRAINING": "unloading", "WAITING": "waiting for memory",
            "ERROR": f"supervisor reports {raw}"}[state]
        if maint and state in ("UNLOADED", "WAITING"):
            state, detail = "BLOCKED", "Maintenance mode: loads are refused"
    queue = health.get("queue")
    if isinstance(queue, dict):
        queue = int(queue.get("active") or 0)
    try:
        pending = max(0.0, float(mem.get("pending_gib") or 0.0))
    except (TypeError, ValueError):
        pending = 0.0
    return {"state": state, "detail": str(detail)[:300], "queue": int(queue or 0),
            "active_sessions": int(health.get("active_sessions") or 0),
            "pending_gib": pending, "resident_gib": mem.get("resident_gib"),
            "estimate_gib": mem.get("estimate_gib"), "idle_seconds": health.get("idle_seconds"),
            "supervisor_pinned": bool(health.get("pinned")), "reachable": bool(health.get("reachable")),
            "version": health.get("version")}


def dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)[:200]
