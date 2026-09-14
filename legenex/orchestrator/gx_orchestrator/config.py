"""Runtime configuration for the gx-cluster orchestrator.

Everything is overridable by environment variable so the service can be moved
between nodes or ports without editing code. No secrets are stored here; the
upstream API key, if any, is read from the environment at call time.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .tiers import Tier

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

    #: Where gx-auto forwards once it has chosen a tier. This is the LiteLLM
    #: gateway, so accounting and logging stay in one place.
    gateway_base: str = field(
        default_factory=lambda: _env("GX_GATEWAY_BASE", "http://127.0.0.1:4000/v1")
    )
    #: The SGLang two-node engine, proxied directly for gx-max.
    gxmax_base: str = field(
        default_factory=lambda: _env("GX_MAX_BASE", "http://127.0.0.1:30000/v1")
    )
    #: The model id the SGLang server actually advertises.
    gxmax_model_id: str = field(default_factory=lambda: _env("GX_MAX_MODEL_ID", "/model"))

    lifecycle_dir: Path = field(
        default_factory=lambda: Path(_env("GX_LIFECYCLE_DIR", str(_REPO_DEFAULT / "legenex" / "lifecycle")))
    )
    log_dir: Path = field(default_factory=lambda: Path(_env("GX_LOG_DIR", "/srv/logs")))

    #: Idle seconds before gx-max releases both nodes. 0 disables auto-release.
    gxmax_idle_ttl: int = field(default_factory=lambda: _env_int("GX_MAX_IDLE_TTL", 1800))
    #: How long a caller waits for gx-max to come up before giving up.
    gxmax_acquire_timeout: int = field(
        default_factory=lambda: _env_int("GX_MAX_ACQUIRE_TIMEOUT", 1800)
    )
    #: Upstream request timeout for proxied inference.
    upstream_timeout: int = field(default_factory=lambda: _env_int("GX_UPSTREAM_TIMEOUT", 900))

    # --- per-node llama-swap health (see gx_orchestrator.health) --------------
    # TierHealth probes each node's OWN llama-swap instance for a tier's real
    # state, instead of trusting the LiteLLM gateway's aggregate `/models`
    # (which answers 200 from its static config regardless of whether the
    # upstream it points at is alive -- see CURRENT_STATE.md / the gx-reason
    # incident this fixes).
    #: Node 1 llama-swap control API (gx-mini, gx-fast). Loopback: the
    #: orchestrator and node 1's llama-swap are on the same host.
    node1_swap_base: str = field(
        default_factory=lambda: _env("GX_NODE1_SWAP_BASE", "http://127.0.0.1:28080")
    )
    #: Node 2 llama-swap control API (gx-reason). MUST be the fabric address,
    #: never Tailscale -- see legenex/gateway/README.md port map.
    node2_swap_base: str = field(
        default_factory=lambda: _env("GX_NODE2_SWAP_BASE", "http://192.168.100.11:28080")
    )
    #: Node 1 is local: a generous timeout costs nothing when it is healthy and
    #: still fails fast if it is not.
    node1_probe_timeout: float = field(
        default_factory=lambda: _env_float("GX_NODE1_PROBE_TIMEOUT", 4.0)
    )
    #: Node 2 can be "alive but userspace-starved" (BLOCKERS.md B-012): the
    #: kernel answers ICMP while llama-swap never answers TCP at all. This
    #: timeout MUST stay short, and probing MUST NOT retry -- TierHealth
    #: refreshes on its own TTL, so a retry loop here would stack additional
    #: multi-second stalls onto every cache refresh while node 2 is down.
    node2_probe_timeout: float = field(
        default_factory=lambda: _env_float("GX_NODE2_PROBE_TIMEOUT", 2.0)
    )

    def gateway_key(self) -> str | None:
        """API key for the LiteLLM gateway, from the environment only."""
        return os.environ.get("GX_GATEWAY_KEY") or os.environ.get("LITELLM_MASTER_KEY")

    def swap_key(self) -> str | None:
        """Bearer token llama-swap requires, from the environment only.

        Same value on both nodes (see legenex/gateway/.env.sample).
        """
        return os.environ.get("GX_SWAP_API_KEY")


CONFIG = Config()
