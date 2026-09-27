"""Runtime configuration for the gx-cluster orchestrator (V4.1).

Everything is overridable by environment variable so the service can be moved
between nodes or ports without editing code. No secrets are stored here; the
upstream API key, if any, is read from the environment at call time.

V4.1 notes: the SGLang / dual-worker / llama-swap era is retired. There is
ONE model (DeepSeek V4.1 Flash EXL3, served by the Mia kit on the head node's
loopback :8888) and TWO public aliases (gx-max direct, gx-auto with automatic
profile/reasoning selection). Fabric and node addresses come from the
registry (ARCHITECTURE-V41 §2), not from code.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

_REPO_DEFAULT = Path(__file__).resolve().parents[3]


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    """Orchestrator configuration."""

    #: Comma-separated bind addresses. Defaults to host loopback PLUS the docker
    #: bridge gateway, so containers (LiteLLM) can reach the orchestrator while
    #: it stays unreachable from the LAN and the tailnet. Do NOT set this to
    #: 0.0.0.0: the orchestrator can start and stop cluster-wide jobs and has no
    #: authentication of its own.
    hosts: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            h.strip()
            for h in _env("GX_ORCH_HOSTS", "127.0.0.1,172.17.0.1").split(",")
            if h.strip()
        )
    )
    port: int = field(default_factory=lambda: _env_int("GX_ORCH_PORT", 18900))

    #: The LiteLLM gateway (accounting, logging, virtual keys stay there). Used
    #: only for status reporting; inference goes straight to gxmax_base.
    gateway_base: str = field(
        default_factory=lambda: _env("GX_GATEWAY_BASE", "http://127.0.0.1:4000/v1")
    )

    #: The ONE model's OpenAI-compatible API on the head node, loopback only.
    gxmax_base: str = field(
        default_factory=lambda: _env("GX_MAX_BASE", "http://127.0.0.1:8888/v1")
    )
    #: The model id the Mia kit actually advertises (verified against
    #: /v1/models before a request is ever relayed).
    gxmax_model_id: str = field(
        default_factory=lambda: _env("GX_MAX_MODEL_ID", "DeepSeek-v4.1-Flash-EXL3")
    )
    #: Per-request output ceiling applied when the caller asks for more than
    #: the window holds (budget.py clamps; the registry carries no max_output).
    gxmax_max_output: int = field(
        default_factory=lambda: _env_int("GX_MAX_OUTPUT_TOKENS", 32_768)
    )

    #: Registry v2 (profiles / reasoning / nodes / runtimes / models / aliases).
    registry_path: Path = field(
        default_factory=lambda: Path(
            _env("GX_REGISTRY", str(_REPO_DEFAULT / "legenex" / "models" / "registry.json"))
        )
    )
    #: The Mia kit submodule directory (start.sh / stop.sh live there).
    runtime_dir: Path = field(
        default_factory=lambda: Path(_env("GX_RUNTIME_DIR", str(_REPO_DEFAULT / "mia-dsv41")))
    )

    lifecycle_dir: Path = field(
        default_factory=lambda: Path(_env("GX_LIFECYCLE_DIR", str(_REPO_DEFAULT / "legenex" / "lifecycle")))
    )
    log_dir: Path = field(default_factory=lambda: Path(_env("GX_LOG_DIR", "/srv/logs")))
    #: Mutable runtime state root, outside the Git checkout (D-026).
    state_dir: Path = field(
        default_factory=lambda: Path(_env("GX_STATE_ROOT", "/srv/projects/gx-cluster/state"))
    )

    #: Scheduler state (queue + history ring buffer), under state/scheduler/.
    scheduler_dir: Path = field(
        default_factory=lambda: Path(
            _env("GX_SCHEDULER_DIR", str(Path(_env("GX_STATE_ROOT", "/srv/projects/gx-cluster/state")) / "scheduler"))
        )
    )

    #: Idle seconds before the model releases both nodes. 0 disables it.
    gxmax_idle_ttl: int = field(default_factory=lambda: _env_int("GX_MAX_IDLE_TTL", 1800))
    #: How long a caller waits for the model to come up before giving up.
    #: The Mia kit's cold boot is ~25 minutes; 1800s covers it.
    gxmax_acquire_timeout: int = field(
        default_factory=lambda: _env_int("GX_MAX_ACQUIRE_TIMEOUT", 1800)
    )
    #: Upstream request timeout for proxied inference.
    upstream_timeout: int = field(default_factory=lambda: _env_int("GX_UPSTREAM_TIMEOUT", 900))

    # --- node 2 (worker rank) ----------------------------------------------
    #: Management SSH target for node 2 (management plane only; model and
    #: NCCL traffic runs on the ConnectX fabric, L-3).
    node2_ssh: str = field(
        default_factory=lambda: _env("GX_NODE2_SSH", "legenex-02@10.60.21.41")
    )
    #: Node 2's LAN address (fabric-adjacent checks, weight sync).
    node2_lan: str = field(default_factory=lambda: _env("GX_NODE2_LAN", "10.60.21.41"))
    #: Probe deadlines. Node 2 can be "alive but userspace-starved"
    #: (BLOCKERS.md B-012): short deadlines, and never retried here -- the
    #: health cache refreshes on its own TTL.
    node2_probe_timeout: float = field(
        default_factory=lambda: _env_float("GX_NODE2_PROBE_TIMEOUT", 2.0)
    )
    node1_probe_timeout: float = field(
        default_factory=lambda: _env_float("GX_NODE1_PROBE_TIMEOUT", 4.0)
    )

    # --- Mia container names (defaults from mia-dsv41/start.sh) -------------
    #: The head (rank 0) container on this node; `dsv41-exl3-head` is the
    #: upstream default (mia-dsv41/start.sh: CONTAINER_HEAD).
    head_container: str = field(
        default_factory=lambda: _env("GX_HEAD_CONTAINER", "dsv41-exl3-head")
    )
    #: The worker (rank 1) container on node 2; `dsv41-exl3-worker` is the
    #: upstream default (mia-dsv41/start.sh: CONTAINER_WORKER).
    worker_container: str = field(
        default_factory=lambda: _env("GX_WORKER_CONTAINER", "dsv41-exl3-worker")
    )

    # --- scheduler admission limits (ARCHITECTURE-V41 §4) -------------------
    #: Per-project ACTIVE cap. One project may not monopolise the engine.
    per_project_active_cap: int = field(
        default_factory=lambda: _env_int("GX_SCHED_PROJECT_ACTIVE", 2)
    )
    #: Per-project QUEUED cap.
    per_project_queued_cap: int = field(
        default_factory=lambda: _env_int("GX_SCHED_PROJECT_QUEUED", 8)
    )
    #: Global QUEUED cap; beyond this a submit is refused with 429.
    global_queued_cap: int = field(
        default_factory=lambda: _env_int("GX_SCHED_GLOBAL_QUEUED", 32)
    )
    #: Default per-request soft timeout, seconds (profile/record may override).
    request_timeout: int = field(
        default_factory=lambda: _env_int("GX_SCHED_REQUEST_TIMEOUT", 600)
    )
    #: Records kept in the history ring buffer (memory + history.jsonl cap).
    history_cap: int = field(default_factory=lambda: _env_int("GX_SCHED_HISTORY_CAP", 5000))

    def gateway_key(self) -> str | None:
        """API key for the LiteLLM gateway, from the environment only."""
        return os.environ.get("GX_GATEWAY_KEY") or os.environ.get("LITELLM_MASTER_KEY")

    def orchestrator_key(self) -> str | None:
        """Bearer key this service requires on every non-health route (D-044).

        Environment only. Placeholder values (not-required, CHANGEME) count as
        unset, and an unset key makes the service refuse everything except /health.
        """
        key = (os.environ.get("GX_ORCHESTRATOR_API_KEY") or "").strip()
        return None if key.casefold() in ("", "not-required", "changeme") else key


CONFIG = Config()
