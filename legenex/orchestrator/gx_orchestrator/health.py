"""Single-model health probing for the DeepSeek V4.1 Flash Mia runtime.

The old per-tier llama-swap probes are retired with the old models. There is
exactly ONE upstream now: the Mia kit's OpenAI API on the head node's
loopback :8888, fronted by the worker container on node 2. Health therefore
means three separate facts, all cached on a TTL:

* HEAD  -- `GET {GX_MAX_BASE}/health` answers 2xx AND `/v1/models` lists the
  served model id (a proxy answering without the right model is a config
  fault, not a healthy engine; see the old llama-swap incident in the module
  history for why the served id must be verified, never assumed).
* WORKER -- node 2 is reachable over management SSH and the rank-1 worker
  container is running (`docker ps`), plus a fabric ping per ConnectX rail IP
  from the registry. Fabric liveness and userspace liveness are two facts,
  not one (BLOCKERS.md B-012: the kernel answers ICMP while userspace is
  wedged; the inverse -- docker up, fabric dead -- kills distributed collectives).
* MEMORY -- MemAvailable on BOTH nodes (node 1 from /proc/meminfo, node 2
  over SSH). The engine pins ~105 GiB per node; a node that never got its
  memory back after a stop is not healthy no matter what the HTTP probe says.

gx-max lifecycle state is deliberately NOT handled here: DOWN is a normal
resting state for the engine, not a fault (lifecycle.py owns that vocabulary).
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Mapping

from .config import Config
from .profiles import Registry
from .upstream import get_json

log = logging.getLogger("gx.health")


class AliasState(str, Enum):
    """Human-facing state vocabulary, shared with `gx status` and the Control
    Center. A small, fixed set by design."""

    READY = "ready"
    STOPPED = "stopped"
    LOADING = "loading"
    QUEUED = "queued"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


@dataclass(frozen=True)
class TierStatus:
    """A model's real state, why, and whether it may be served.

    Kept under the old name so the Control Center / status rendering keep one
    vocabulary across the refactor; there is only one model now, but the
    state/reason/usable triple is still what humans read.
    """

    state: AliasState
    reason: str = ""
    usable: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"state": self.state.value, "usable": self.usable, "reason": self.reason}


# --------------------------------------------------------------------------
# Pluggable host operations -- fakes in the unit tests, real ones here.
# --------------------------------------------------------------------------

#: SSH command prefix for node 2. BatchMode: no interactive prompt ever.
_SSH_OPTS = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=10")


def _real_ssh(target: str, command: str, timeout: float) -> "str | None":
    """Run `command` on `target` over SSH; stripped stdout, or None on failure."""
    try:
        proc = subprocess.run(
            ["ssh", *_SSH_OPTS, target, command],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("ssh %s failed: %r", target, exc)
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _real_ping(host: str, timeout: float) -> bool:
    """One ICMP echo, short deadline, never retried."""
    deadline = max(1, int(timeout))
    try:
        proc = subprocess.run(
            ["ping", "-c", "1", "-W", str(deadline), host],
            capture_output=True, timeout=deadline + 1,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


_MEM_RE = re.compile(r"^MemAvailable:\s+(\d+)\s+kB", re.MULTILINE)


def _real_local_mem_gib() -> "float | None":
    """MemAvailable on THIS node, in GiB, from /proc/meminfo."""
    try:
        text = open("/proc/meminfo", encoding="ascii").read()
    except OSError:
        return None
    m = _MEM_RE.search(text)
    return int(m.group(1)) / (1024.0 * 1024.0) if m else None


def _ssh_mem_gib(ssh_target: str, ssh_run: Callable[..., "str | None"], timeout: float) -> "float | None":
    out = ssh_run(ssh_target, "awk '/MemAvailable/{print $2/1048576}' /proc/meminfo", timeout)
    try:
        return round(float(out), 1) if out not in (None, "") else None
    except ValueError:
        return None


class ClusterHealth:
    """Cached head/worker/memory facts for the single-model cluster."""

    def __init__(
        self,
        cfg: Config,
        registry: Registry,
        *,
        ttl: float = 15.0,
        ssh_run: Callable[..., "str | None"] | None = None,
        ping: Callable[[str, float], bool] | None = None,
        local_mem_gib: Callable[[], "float | None"] | None = None,
        http_get: Callable[..., Any] | None = None,
    ) -> None:
        self._cfg = cfg
        self._registry = registry
        self._ttl = ttl
        self._ssh_run = ssh_run or (lambda target, cmd, timeout: _real_ssh(target, cmd, timeout))
        self._ping = ping or _real_ping
        self._local_mem = local_mem_gib or _real_local_mem_gib
        self._http_get = http_get or get_json
        self._lock = threading.Lock()
        self._cache: dict[str, Any] | None = None
        self._checked = 0.0

    # ---------------------------------------------------------------- probes
    def _probe_head(self) -> dict[str, Any]:
        """/health + /v1/models on the head node; the served id is verified,
        never assumed (a vLLM proxy up with the wrong model is a fault)."""
        cfg = self._cfg
        base = cfg.gxmax_base.rstrip("/")
        health_url = base.removesuffix("/v1") + "/health"
        out: dict[str, Any] = {
            "healthy": False, "serves_model": False,
            "model_id": cfg.gxmax_model_id, "detail": "",
        }
        try:
            self._http_get(health_url, timeout=cfg.node1_probe_timeout)
            out["healthy"] = True
        except Exception as exc:  # noqa: BLE001 - any I/O failure means down
            out["detail"] = f"health probe failed: {exc!r}"
            return out
        try:
            resp = self._http_get(f"{base}/models", timeout=cfg.node1_probe_timeout)
            body = resp.json() if hasattr(resp, "json") else json.loads(resp.body)
            ids = [str(m.get("id")) for m in (body.get("data") or []) if isinstance(m, Mapping)]
            out["serves_model"] = cfg.gxmax_model_id in ids
            if not out["serves_model"]:
                out["detail"] = f"/v1/models lists {ids!r}, expected '{cfg.gxmax_model_id}'"
        except Exception as exc:  # noqa: BLE001
            out["detail"] = f"/v1/models probe failed: {exc!r}"
        return out

    def _probe_worker(self) -> dict[str, Any]:
        """Management-SSH reachability, the worker container, and one fabric
        ping per rail (registry IPs). No retries: the cache refreshes on TTL."""
        cfg = self._cfg
        worker = self._registry.worker_node()
        out: dict[str, Any] = {
            "ssh_reachable": False, "container_running": False,
            "container": cfg.worker_container, "fabric": {}, "detail": "",
        }
        ps = self._ssh_run(cfg.node2_ssh, "docker ps --format '{{.Names}}'", cfg.node2_probe_timeout + 8)
        if ps is None:
            out["detail"] = f"ssh {cfg.node2_ssh} unreachable"
        else:
            out["ssh_reachable"] = True
            names = {line.strip() for line in ps.splitlines() if line.strip()}
            out["container_running"] = cfg.worker_container in names
            if not out["container_running"]:
                out["detail"] = f"worker container '{cfg.worker_container}' not in docker ps"
        for rail, ip in worker.fabric.items():
            out["fabric"][ip] = self._ping(ip, cfg.node2_probe_timeout)
        return out

    def _probe_mem(self) -> dict[str, Any]:
        cfg = self._cfg
        return {
            "node1_gib": None if (m := self._local_mem()) is None else round(m, 1),
            "node2_gib": _ssh_mem_gib(cfg.node2_ssh, self._ssh_run, cfg.node2_probe_timeout + 8),
        }

    # --------------------------------------------------------------- snapshot
    def snapshot(self) -> dict[str, Any]:
        """The cached per-fact health snapshot (one dict, cheap to relay)."""
        with self._lock:
            if self._cache is not None and time.time() - self._checked < self._ttl:
                return dict(self._cache)
        snap = {
            "head": self._probe_head(),
            "worker": self._probe_worker(),
            "mem": self._probe_mem(),
        }
        snap["checked"] = time.time()
        with self._lock:
            self._cache = snap
            self._checked = snap["checked"]
        return dict(snap)

    def head_status(self) -> TierStatus:
        """The head's serving state in the shared state vocabulary."""
        snap = self.snapshot()
        head = snap["head"]
        if head.get("healthy") and head.get("serves_model"):
            return TierStatus(AliasState.READY, "health ok and model id verified", usable=True)
        if head.get("healthy"):
            return TierStatus(AliasState.FAILED, head.get("detail") or "wrong model id", usable=False)
        return TierStatus(AliasState.UNAVAILABLE, head.get("detail") or "unreachable", usable=False)

    def worker_ok(self) -> bool:
        """True when node 2 answers SSH, runs the worker container, and at
        least one fabric rail answers ping. A TP=2 engine cannot serve
        without its worker, so this is a serving precondition, not trivia."""
        w = self.snapshot()["worker"]
        return bool(
            w.get("ssh_reachable") and w.get("container_running") and any(w.get("fabric", {}).values())
        )
