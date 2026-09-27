"""AgentOS Control Center adapter (read-only, honest).

Facts verified against the AgentOS source (state/AGENTOS-MAP.md, 2026-09-27):

* The Control Center runs on gx10-01 at http://127.0.0.1:4173 (config-driven),
  same-host, no auth (private net + same-origin enforced there).
* READ-ONLY endpoints the dashboard may use: /api/health, /api/hermes
  (coarse gateway running/stopped), /api/kanban (board counts + cards),
  /api/projects, /api/overview, /api/buzz, /api/routing, /api/provider-health.
* NOT SUPPORTED upstream, so the dashboard does not show them (no fabricated
  buttons): fine-grained agent states, per-agent metrics, pause/resume/cancel,
  task dependency graphs.

When the Control Center does not answer, every snapshot says
{"connected": false, "reason": ...} — never a placeholder agent or task.
"""

from __future__ import annotations

import time
from typing import Any

from .config import UIConfig
from .util import HTTPError, http_json

#: Operations the upstream API verifiably supports. pause/resume/cancel are
#: NOT among them (AGENTOS-MAP.md) and must never appear in a UI.
SUPPORTED_CONTROLS: tuple[str, ...] = ()

#: Endpoints probed for the snapshot (all GET, all read-only).
SNAPSHOT_ENDPOINTS = ("hermes", "kanban", "projects", "overview", "buzz")


class AgentOSAdapter:
    def __init__(self, cfg: UIConfig) -> None:
        self.cfg = cfg
        self.base = cfg.agentos_base.rstrip("/")
        #: cache: (stamp, snapshot) — AgentOS is a user app; do not hammer it.
        self._cache: tuple[float, dict] = (0.0, {})
        self._ttl = 10.0

    # ------------------------------------------------------------ helpers
    def _get(self, path: str, timeout: float = 3.0) -> tuple[int, Any]:
        return http_json("GET", f"{self.base}{path}", timeout=timeout)

    def probe_health(self) -> dict:
        """Liveness only. Returns {"connected": bool, "reason": str}."""
        if self.cfg.offline:
            return {"connected": False, "reason": "offline mode", "base": self.base}
        try:
            code, body = self._get("/api/health")
        except HTTPError as exc:
            return {"connected": False, "reason": exc.message, "base": self.base}
        ok = 200 <= code < 300
        return {"connected": ok, "reason": "" if ok else f"HTTP {code}",
                "base": self.base, "health": body if isinstance(body, dict) else None}

    # ------------------------------------------------------------ snapshot
    def snapshot(self, max_age: float | None = None) -> dict:
        """The Agents / Tasks / Projects pages' AgentOS data.

        Shape:
          {"connected": bool, "reason": str, "base": str, "checked_at": ts,
           "supported_controls": [],          # always empty until upstream grows
           "hermes": {...} | None,            # gateways running/stopped (coarse)
           "kanban": {...} | None,            # boards + counts + cards
           "projects": {...} | None,          # project descriptors + registry
           "overview": {...} | None,          # system/vps/routing/attention
           "buzz": {...} | None}
        """
        ttl = self._ttl if max_age is None else max_age
        if self._cache[0] and time.time() - self._cache[0] < ttl:
            return self._cache[1]
        out: dict[str, Any] = {"connected": False, "reason": "", "base": self.base,
                               "supported_controls": list(SUPPORTED_CONTROLS),
                               "generated_at": time.time()}
        for key in SNAPSHOT_ENDPOINTS:
            out[key] = None
        if self.cfg.offline:
            out["reason"] = "offline mode"
            self._cache = (time.time(), out)
            return out
        health = self.probe_health()
        if not health["connected"]:
            out["reason"] = health["reason"]
            self._cache = (time.time(), out)
            return out
        out["connected"] = True
        for key in SNAPSHOT_ENDPOINTS:
            try:
                code, body = self._get(f"/api/{key}")
                out[key] = body if 200 <= code < 300 and isinstance(body, (dict, list)) else \
                    {"unavailable": True, "status": code}
            except HTTPError as exc:
                out[key] = {"unavailable": True, "reason": exc.message}
        out["checked_at"] = time.time()
        self._cache = (out["generated_at"], out)
        return out
