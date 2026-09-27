"""Clients for the V4.1 control plane, plus cached snapshots of it.

Every function here is a thin, read-mostly call to an API that already
exists: the orchestrator (lifecycle + scheduler), the LiteLLM gateway, the
Mia runtime's loopback health endpoint. The old per-tier llama-swap /
media / SGLang probes are retired with their stacks.

Service inventory (ARCHITECTURE-V41.md section 6):
  * cluster-managed: litellm, litellm-db, orchestrator, control-ui,
    hostwatch, ts-proxy (gx10-01); gx10-02 runs nothing but the rank-1
    mirror of gx-max (no management surface).
  * unrelated user apps, shown but never managed: open-webui, agentos.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from . import hostfacts
from .config import PLACEHOLDER_SECRETS, UIConfig
from .redact import redact
from .util import HTTPError, TTLCache, bearer, http, http_json, run, ssh_args, tcp_state

#: The only public aliases.
PUBLIC_ALIASES = ("gx-max", "gx-auto")

#: Services that belong to the cluster (managed) vs user apps (shown only).
MANAGED_SERVICES = ("litellm", "litellm-db", "orchestrator", "control-ui", "hostwatch", "ts-proxy")
UNMANAGED_APPS = ("open-webui", "agentos")


def _probe(fn, *a, **kw) -> dict:
    t0 = time.time()
    try:
        status, body = fn(*a, **kw)
        return {"ok": 200 <= status < 300, "status": status, "body": body,
                "ms": round((time.time() - t0) * 1000), "checked_at": time.time()}
    except HTTPError as exc:
        return {"ok": False, "status": 0, "error": exc.message,
                "ms": round((time.time() - t0) * 1000), "checked_at": time.time()}


def _http_status(url: str, timeout: float = 4.0):
    res = http("GET", url, timeout=timeout)
    return res.status, {"status": res.status}


class Cluster:
    """All live reads, each behind its own TTL cache."""

    def __init__(self, cfg: UIConfig) -> None:
        self.cfg = cfg
        self.node1 = TTLCache(self._node1_facts, cfg.local_ttl)
        self.node2 = TTLCache(self._node2_facts, cfg.node2_ttl, first_wait=20)
        self.services = TTLCache(self._services, cfg.service_ttl)
        self.remote_git = TTLCache(self._remote_head, cfg.git_remote_ttl)
        self.guard = TTLCache(self._guard, cfg.local_ttl)
        self.lifecycle = TTLCache(self._lifecycle, 2.0)
        self._hostfacts_src = Path(hostfacts.__file__).read_text(encoding="utf-8")

    # ------------------------------------------------------------ keys
    def key(self, name: str) -> str | None:
        return self.cfg.secret(name)

    def litellm_headers(self) -> dict[str, str]:
        return bearer(self.key("LITELLM_MASTER_KEY"))

    def orch_headers(self) -> dict[str, str]:
        return bearer(self.key("GX_ORCHESTRATOR_API_KEY"))

    # ------------------------------------------------------------ nodes
    def _node1_facts(self) -> dict:
        if self.cfg.offline:
            return {"role": "node1", "offline": True}
        facts = hostfacts.collect("node1")
        facts["reachable"] = True
        return facts

    def _node2_facts(self) -> dict:
        if self.cfg.offline:
            return {"role": "node2", "offline": True, "reachable": False}
        t0 = time.time()
        res = run(ssh_args(self.cfg.node2_ssh) + ["python3", "-", "node2"], timeout=25,
                  input_text=self._hostfacts_src, merge_stderr=False)
        if res.ok:
            try:
                facts = json.loads(res.out)
                facts["reachable"] = True
                facts["ssh_ms"] = round((time.time() - t0) * 1000)
                return facts
            except ValueError:
                pass
        # Management plane failed. The fabric is the better liveness probe
        # during a userspace stall (B-020): a refused TCP connect proves the
        # kernel is alive even when Tailscale/SSH are dark.
        return {
            "role": "node2", "reachable": False, "ssh_rc": res.rc,
            "error": redact(res.out.strip()[-300:]) or "ssh failed",
            "fabric_probe": {p: tcp_state(p, 22, 2.0) for p in self.cfg.fabric_peers},
            "collected_at": time.time(),
        }

    # ---------------------------------------------------------- services
    def _services(self) -> dict:
        c = self.cfg
        if c.offline:
            return {"offline": True}
        orch_h = self.orch_headers()
        out: dict[str, Any] = {
            "orchestrator": _probe(http_json, "GET", f"{c.orchestrator_base}/health/detailed",
                                   headers=orch_h, timeout=4),
            "litellm_live": _probe(http_json, "GET", f"{c.litellm_base}/health/liveliness", timeout=4),
            "litellm_ready": _probe(http_json, "GET", f"{c.litellm_base}/health/readiness", timeout=4),
            # Unrelated user apps on gx10-01: probed so the dashboard can show
            # them, but never restarted or managed from here.
            "openwebui": _probe(_http_status, "http://127.0.0.1:3000/", timeout=4),
            "agentos": _probe(_http_status, f"{c.agentos_base}/api/health", timeout=4),
            # The scheduler: the queue depth + active generations the Overview
            # page shows. Worker A's contract: GET /scheduler/status.
            "scheduler": _probe(http_json, "GET", f"{c.orchestrator_base}/scheduler/status",
                                headers=orch_h, timeout=4),
            "collected_at": time.time(),
        }
        # The Mia runtime API is loopback-only and only answers while gx-max
        # is READY; while down this probe is expected to fail, never an alarm.
        gx = self.gxmax_state()
        if gx == "ready":
            out["mia"] = _probe(http_json, "GET", f"{c.mia_base}/health", timeout=3)
        else:
            out["mia"] = {"ok": False, "status": 0, "error": f"gx-max is {gx}; :8888 is not serving",
                          "checked_at": time.time()}
        return out

    def scheduler_ok(self) -> bool:
        """True when the orchestrator answered the last scheduler probe."""
        if self.cfg.offline:
            return False
        svc = self.services.get(max_age=10) or {}
        return bool((svc.get("scheduler") or {}).get("ok"))

    def scheduler_snapshot(self, max_age: float | None = None) -> dict:
        """The raw /scheduler/status body, or an honest unavailable state."""
        if self.cfg.offline:
            return {"available": False, "reason": "offline mode"}
        svc = self.services.get(max_age=max_age) or {}
        probe = svc.get("scheduler") or {}
        if probe.get("ok") and isinstance(probe.get("body"), dict):
            body = dict(probe["body"])
            body["available"] = True
            body["checked_at"] = probe.get("checked_at")
            return body
        return {"available": False, "reason": probe.get("error") or "orchestrator scheduler did not answer",
                "status": probe.get("status"), "checked_at": probe.get("checked_at")}

    # --------------------------------------------------------- lifecycle
    def _lifecycle(self) -> dict:
        if self.cfg.offline:
            return {"status": {"state": "down"}, "events": {"events": [], "history": []}}
        base = self.cfg.orchestrator_base
        h = self.orch_headers()
        return {
            "status": _probe(http_json, "GET", f"{base}/lifecycle/gx-max/status", headers=h, timeout=4),
            "events": _probe(http_json, "GET", f"{base}/lifecycle/gx-max/events?limit=400", headers=h, timeout=4),
        }

    def gxmax_state(self) -> str:
        st = (self.lifecycle.get(max_age=1.5) or {}).get("status") or {}
        body = st.get("body") if isinstance(st, dict) else None
        return (body or {}).get("state", "unknown") if isinstance(body, dict) else "unknown"

    # ---------------------------------------------------------------- git
    def _remote_head(self) -> dict:
        if self.cfg.offline:
            return {"ok": False, "offline": True}
        res = run(["git", "ls-remote", self.cfg.github_url, "refs/heads/main"], timeout=20)
        if res.ok and res.out.strip():
            return {"ok": True, "head": res.out.split()[0], "checked_at": time.time()}
        return {"ok": False, "error": redact(res.out.strip()[-200:]), "checked_at": time.time()}

    # --------------------------------------------------------------- guard
    def _guard(self) -> dict:
        out: dict[str, Any] = {}
        for node in ("node1", "node2"):
            path = self.cfg.guard_dir / f"{node}-residency.json"
            try:
                data = json.loads(path.read_text(encoding="utf-8") or "{}")
            except FileNotFoundError:
                data = {}
            except (OSError, ValueError):
                data = {"_error": "unreadable ledger"}
            out[node] = data
        out["node1_lock"] = hostfacts.lock_state(str(self.cfg.guard_dir / "node1.lock"))
        out["reserve_gib"] = 30
        return out

    # -------------------------------------------------------------- helpers
    def secret_hygiene(self) -> list[dict]:
        rows = []
        for name in ("LITELLM_MASTER_KEY", "GX_ORCHESTRATOR_API_KEY"):
            value = os.environ.get(name, "")
            if not value:
                state = "unset"
            elif value in PLACEHOLDER_SECRETS:
                state = "placeholder"
            elif len(value) < 24:
                state = "weak"
            else:
                state = "set"
            rows.append({"name": name, "state": state})
        return rows

    def invalidate(self) -> None:
        for c in (self.node1, self.node2, self.services, self.remote_git, self.guard, self.lifecycle):
            c.invalidate()


def gateway_text_metrics(path: Path, *, tail_bytes: int = 256_000) -> dict[str, Any]:
    """The newest gateway hook record per alias (privacy-safe metrics JSONL).

    The LiteLLM budget hook writes timing and token counts only, never prompt
    text (kept in the V4.1 gateway; ARCHITECTURE-V41.md section 5).
    Returns {"by_alias": {alias: record}, "recent_failures": {alias: n}}.
    """
    out: dict[str, Any] = {"by_alias": {}, "recent_failures": {}, "path": str(path)}
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - tail_bytes))
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return out
    now = time.time()
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        alias = rec.get("alias") if isinstance(rec, dict) else None
        if not isinstance(alias, str):
            continue
        out["by_alias"][alias] = rec
        if rec.get("outcome") not in ("ok", None) and now - float(rec.get("ts") or 0) < 900:
            out["recent_failures"][alias] = out["recent_failures"].get(alias, 0) + 1
    return out
