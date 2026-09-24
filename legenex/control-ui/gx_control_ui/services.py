"""Clients for the existing control plane, plus cached snapshots of it.

Every function here is a thin, read-mostly call to an API that already
exists. The only writes are the ones `actions.py` explicitly maps to:
orchestrator acquire/release, llama-swap per-model load/unload, and the
media router's generation endpoints.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any

from . import hostfacts
from .config import PLACEHOLDER_SECRETS, UIConfig
from .redact import redact
from .util import HTTPError, TTLCache, bearer, http, http_json, run, ssh_args, tcp_state

TEXT_ALIASES = ("gx-mini", "gx-code", "gx-max", "gx-auto")
MEDIA_ALIASES = ()
#: Operator-facing logical modes. Retired media aliases are not probed.
ALL_ALIASES = ("gx-mini", "gx-code", "gx-auto", "gx-max")
SWAP_MODELS = {"gx-mini": "node1", "gx-code": "node1"}


def _http_status(url: str, timeout: float = 4.0):
    res = http("GET", url, timeout=timeout)
    return res.status, {"status": res.status}


def _probe(fn, *a, **kw) -> dict:
    t0 = time.time()
    try:
        status, body = fn(*a, **kw)
        return {"ok": 200 <= status < 300, "status": status, "body": body,
                "ms": round((time.time() - t0) * 1000), "checked_at": time.time()}
    except HTTPError as exc:
        return {"ok": False, "status": 0, "error": exc.message,
                "ms": round((time.time() - t0) * 1000), "checked_at": time.time()}


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

    def swap_headers(self) -> dict[str, str]:
        return bearer(self.key("GX_SWAP_API_KEY"))

    def litellm_headers(self) -> dict[str, str]:
        return bearer(self.key("LITELLM_MASTER_KEY"))

    def media_headers(self) -> dict[str, str]:
        return bearer(self.key("GX_MEDIA_API_KEY"))

    def swap_base(self, node: str) -> str:
        return self.cfg.node1_swap_base if node == "node1" else self.cfg.node2_swap_base

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
        swap_h = self.swap_headers()
        out: dict[str, Any] = {
            "orchestrator": _probe(http_json, "GET", f"{c.orchestrator_base}/health/detailed", timeout=4),
            "litellm_live": _probe(http_json, "GET", f"{c.litellm_base}/health/liveliness", timeout=4),
            "litellm_ready": _probe(http_json, "GET", f"{c.litellm_base}/health/readiness", timeout=4),
            "swap_node1": _probe(http_json, "GET", f"{c.node1_swap_base}/v1/models", headers=swap_h, timeout=4),
            "swap_node1_running": _probe(http_json, "GET", f"{c.node1_swap_base}/running", headers=swap_h, timeout=4),
            "swap_node2": _probe(http_json, "GET", f"{c.node2_swap_base}/v1/models", headers=swap_h, timeout=3),
            "swap_node2_running": _probe(http_json, "GET", f"{c.node2_swap_base}/running", headers=swap_h, timeout=3),
            "openwebui": _probe(_http_status, "http://127.0.0.1:3000/", timeout=4),
            "agentos": _probe(_http_status, "http://127.0.0.1:4173/api/health", timeout=4),
            "sglang": _probe(http_json, "GET", f"{c.gxmax_base}/health", timeout=3),
            # D-039: per-alias budget, routing and last-request facts.
            "text_status": _probe(http_json, "GET", f"{c.orchestrator_base}/text/status", timeout=4),
            "gateway_text": gateway_text_metrics(c.srv_logs / "gx-text" / "gateway-text.jsonl"),
        }
        if out["sglang"]["ok"]:
            out["sglang_models"] = _probe(http_json, "GET", f"{c.gxmax_base}/v1/models", timeout=4)
            out["sglang_info"] = _probe(http_json, "GET", f"{c.gxmax_base}/get_server_info", timeout=4)
            info = out["sglang_info"].get("body")
            if isinstance(info, dict):
                keep = ("tp_size", "nnodes", "node_rank", "model_path", "served_model_name",
                        "context_length", "mem_fraction_static", "dist_init_addr", "version",
                        "speculative_algorithm", "max_running_requests", "chunked_prefill_size")
                out["sglang_info"]["body"] = {k: info.get(k) for k in keep if k in info}
        out["collected_at"] = time.time()
        return out

    def _lifecycle(self) -> dict:
        if self.cfg.offline:
            return {"status": {"state": "down"}, "events": {"events": [], "history": []}}
        base = self.cfg.orchestrator_base
        return {
            "status": _probe(http_json, "GET", f"{base}/lifecycle/gx-max/status", timeout=4),
            "events": _probe(http_json, "GET", f"{base}/lifecycle/gx-max/events?limit=400", timeout=4),
        }

    def gxmax_state(self) -> str:
        st = (self.lifecycle.get(max_age=1.5) or {}).get("status") or {}
        body = st.get("body") if isinstance(st, dict) else None
        return (body or {}).get("state", "unknown") if isinstance(body, dict) else "unknown"

    def _remote_head(self) -> dict:
        if self.cfg.offline:
            return {"ok": False, "offline": True}
        res = run(["git", "ls-remote", self.cfg.github_url, "refs/heads/main"], timeout=20)
        if res.ok and res.out.strip():
            return {"ok": True, "head": res.out.split()[0], "checked_at": time.time()}
        return {"ok": False, "error": redact(res.out.strip()[-200:]), "checked_at": time.time()}

    def _guard(self) -> dict:
        out = {}
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

    # ------------------------------------------------------------- helpers
    def admission_preview(self, alias: str) -> dict:
        """What the ONE admission formula (gx_orchestrator.resource_guard)
        says about loading `alias` right now. Read-only; takes no lock."""
        orch = self.cfg.repo_root / "legenex" / "orchestrator"
        if str(orch) not in sys.path:
            sys.path.insert(0, str(orch))
        from gx_orchestrator import resource_guard as rg  # noqa: PLC0415

        spec = rg.WORKLOAD_SIZING.get(alias)
        if spec is None:
            return {"allowed": True, "reason": "no sizing entry; loads on demand"}
        node = spec.node
        facts = self.node1.get() if node == "node1" else self.node2.get()
        avail = ((facts or {}).get("memory") or {}).get("MemAvailable")
        if not avail:
            return {"allowed": False, "reason": f"{node} memory facts unavailable"}
        ledger = self.guard.get().get(node, {}) or {}
        residency = 0.0
        for name, rec in ledger.items():
            if isinstance(rec, dict) and name != alias:
                residency += float(rec.get("estimated_gib", 0) or 0)
        res = rg.compute_admission(node, spec.estimated_gib, current_residency_gib=residency,
                                   mem_available_gib=avail / 2**30)
        return {"allowed": res.allowed, "reason": res.reason, "node": node, **res.numbers}

    def secret_hygiene(self) -> list[dict]:
        rows = []
        for name in ("LITELLM_MASTER_KEY", "GX_SWAP_API_KEY", "GX_ORCHESTRATOR_API_KEY"):
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

    def swap_load(self, alias: str, timeout: float) -> tuple[bool, str]:
        node = SWAP_MODELS[alias]
        url = f"{self.swap_base(node)}/upstream/{urllib.parse.quote(alias)}/health"
        try:
            res = http("GET", url, headers=self.swap_headers(), timeout=timeout)
        except HTTPError as exc:
            return False, exc.message
        return 200 <= res.status < 300, f"HTTP {res.status}: {res.text(300)}"

    def swap_unload(self, alias: str) -> tuple[bool, str]:
        node = SWAP_MODELS[alias]
        url = f"{self.swap_base(node)}/api/models/unload/{urllib.parse.quote(alias)}"
        try:
            res = http("POST", url, headers=self.swap_headers(), timeout=240)
        except HTTPError as exc:
            return False, exc.message
        return 200 <= res.status < 300, f"HTTP {res.status}: {res.text(300)}"

    def invalidate(self) -> None:
        for c in (self.node1, self.node2, self.services, self.remote_git, self.guard, self.lifecycle):
            c.invalidate()


def gateway_text_metrics(path: Path, *, tail_bytes: int = 256_000) -> dict[str, Any]:
    """The newest gateway record per text alias (D-039 metrics file).

    The LiteLLM hook writes timing and token counts only, never prompt text.
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
