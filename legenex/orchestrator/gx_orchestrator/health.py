"""Per-tier availability probing for gx-mini, gx-fast and gx-reason.

gx-max is intentionally NOT handled here: its availability is a lifecycle
question (see lifecycle.py), not a probe -- DOWN is a normal resting state for
it, not a fault. Combining a tier's health here with gx-max's lifecycle state
happens one layer up, in server.py.

The bug this module fixes
--------------------------
The previous `TierHealth.snapshot()` (formerly in server.py) probed only the
LiteLLM gateway's aggregate `/models` endpoint and used that single boolean
for EVERY tier. LiteLLM answers 200 from its own static config regardless of
whether the upstream a given alias actually points at is alive, so gx-reason
reported "available" even while node 2 was completely wedged (unreachable
over both Tailscale and the ConnectX fabric). That violates the rule that an
unavailable model must never report healthy.

This module probes each NODE's OWN llama-swap control API instead
(`GET /v1/models`), which reports each model's real, current state --
whether it is loaded, unloaded-but-startable, mid-load, or errored. That is
the actual upstream gx-mini/gx-fast/gx-reason depend on.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from .config import Config
from .tiers import Tier
from .upstream import UpstreamError, get_json

log = logging.getLogger("gx.health")


class AliasState(str, Enum):
    """Human-facing state vocabulary, shared with the `gx-status` command.

    Deliberately a small, fixed set (matches the task brief for `gx status`)
    rather than passing through llama-swap's raw status strings verbatim --
    those are preserved separately in `TierStatus.reason` for anyone who wants
    the exact upstream detail.
    """

    READY = "ready"
    STOPPED = "stopped"
    LOADING = "loading"
    QUEUED = "queued"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


@dataclass(frozen=True)
class TierStatus:
    """A tier's real state, why, and whether gx-auto may route to it.

    `usable` is set explicitly by whoever constructs a TierStatus rather than
    derived generically from `state`: "loading" is routable for a
    llama-swap-managed model that will finish starting on its own, but is NOT
    routable for gx-max mid-release (see server._max_tier_status). One state
    name does not imply one routing answer across every tier.
    """

    state: AliasState
    reason: str = ""
    usable: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"state": self.state.value, "usable": self.usable, "reason": self.reason}


# --------------------------------------------------------------------------
# llama-swap response parsing
# --------------------------------------------------------------------------


def _swap_model_status(models_json: Mapping[str, Any] | None, model_id: str) -> tuple[str, bool]:
    """Return (raw `status.value`, found) for `model_id` in a llama-swap
    `GET /v1/models` body. `found=False` means llama-swap does not know this
    model at all (a config problem, not a transient fault).
    """
    for entry in (models_json or {}).get("data", []) or []:
        if entry.get("id") == model_id or entry.get("name") == model_id:
            raw = str((entry.get("status") or {}).get("value", "")).lower()
            return raw, True
    return "", False


def _tier_status_from_swap(raw: str, found: bool) -> TierStatus:
    if not found:
        return TierStatus(AliasState.FAILED, "not_configured_in_llama_swap", usable=False)
    if raw in ("loaded", "ready"):
        return TierStatus(AliasState.READY, raw, usable=True)
    if raw in ("unloaded", "stopped"):
        # Not currently running, but llama-swap starts it on-demand on the
        # next request -- that IS available, just cold.
        return TierStatus(AliasState.STOPPED, raw, usable=True)
    if raw in ("loading", "starting"):
        return TierStatus(AliasState.LOADING, raw, usable=True)
    if raw in ("failed", "error", "stopping"):
        return TierStatus(AliasState.FAILED, raw or "engine_error", usable=False)
    # An unrecognised status is not a green light. Never report healthy for a
    # state this code cannot explain.
    return TierStatus(AliasState.UNAVAILABLE, f"unrecognised_status:{raw or 'empty'}", usable=False)


def _fetch_models(
    base_url: str,
    *,
    api_key: str | None,
    timeout: float,
    offline_reason: str,
) -> tuple[Mapping[str, Any] | None, TierStatus | None]:
    """`GET <base_url>/v1/models` once. Single attempt, no retries: the caller
    is refreshed on a TTL, so retrying here would stack extra latency onto
    every cache refresh while a node is down.

    Returns (parsed_body, None) on success, or (None, error_status) on any
    failure -- callers use the error status directly rather than guessing.
    """
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    url = f"{base_url.rstrip('/')}/v1/models"
    try:
        resp = get_json(url, headers=headers, timeout=timeout)
    except UpstreamError as exc:
        # llama-swap answered but rejected the request (bad/missing auth,
        # etc.) -- a config problem, not a dead node.
        return None, TierStatus(AliasState.FAILED, f"llama_swap_error_{exc.status}", usable=False)
    except Exception as exc:  # noqa: BLE001 - any I/O failure means unreachable
        log.warning("llama-swap at %s unreachable: %r", base_url, exc)
        return None, TierStatus(AliasState.UNAVAILABLE, offline_reason, usable=False)

    try:
        return resp.json(), None
    except Exception as exc:  # noqa: BLE001
        return None, TierStatus(AliasState.FAILED, f"malformed_response:{exc!r}", usable=False)


class TierHealth:
    """Cached, per-tier availability for the tiers gx-auto may route to
    through llama-swap: gx-mini, gx-fast (node 1) and gx-reason (node 2).

    gx-max is intentionally absent -- see module docstring.
    """

    def __init__(self, cfg: Config, ttl: float = 15.0) -> None:
        self._cfg = cfg
        self._ttl = ttl
        self._lock = threading.Lock()
        self._cache: dict[Tier, TierStatus] = {}
        self._checked = 0.0

    def snapshot(self) -> dict[Tier, TierStatus]:
        with self._lock:
            if time.time() - self._checked < self._ttl and self._cache:
                return dict(self._cache)

        cfg = self._cfg
        key = cfg.swap_key()

        node1_json, node1_err = _fetch_models(
            cfg.node1_swap_base,
            api_key=key,
            timeout=cfg.node1_probe_timeout,
            offline_reason="node1_llama_swap_unreachable",
        )
        mini = node1_err or _tier_status_from_swap(*_swap_model_status(node1_json, Tier.MINI.value))
        fast = node1_err or _tier_status_from_swap(*_swap_model_status(node1_json, Tier.FAST.value))

        node2_json, node2_err = _fetch_models(
            cfg.node2_swap_base,
            api_key=key,
            timeout=cfg.node2_probe_timeout,
            offline_reason="node2_offline",
        )
        reason = node2_err or _tier_status_from_swap(*_swap_model_status(node2_json, Tier.REASON.value))

        snap = {Tier.MINI: mini, Tier.FAST: fast, Tier.REASON: reason}
        with self._lock:
            self._cache = snap
            self._checked = time.time()
        return dict(snap)
