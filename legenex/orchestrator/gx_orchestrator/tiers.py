"""Tier definitions for the gx-cluster gateway.

A *tier* is a user-facing alias. This module is pure data plus small pure
helpers: it has no I/O and no dependencies outside the standard library, so it
can be imported by the router, the health checker and the tests alike.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Tier(str, Enum):
    """The user-facing model aliases exposed by the gateway."""

    MINI = "gx-mini"
    FAST = "gx-fast"
    REASON = "gx-reason"
    MAX = "gx-max"
    IMAGE = "gx-image"
    VIDEO = "gx-video"
    AUTO = "gx-auto"


@dataclass(frozen=True)
class TierSpec:
    """Static capabilities of a tier, used for routing decisions.

    Attributes:
        alias: The user-facing name.
        node: Which physical node normally serves it.
        max_context: Usable context window in tokens.
        vision: Whether the underlying model accepts image input.
        tools: Whether the underlying model supports tool/function calling.
        exclusive_cluster: True if serving it requires BOTH nodes (gx-max only).
        cost_rank: Relative expense/latency. Lower is cheaper and faster.
    """

    alias: Tier
    node: str
    max_context: int
    vision: bool
    tools: bool
    exclusive_cluster: bool = False
    cost_rank: int = 0
    notes: str = ""


# Ordered cheapest -> most expensive. `cost_rank` drives escalation and the
# "route elsewhere when gx-max is busy" fallback behaviour.
TIERS: dict[Tier, TierSpec] = {
    Tier.MINI: TierSpec(
        alias=Tier.MINI,
        node="gx10-01",
        max_context=65_536,
        vision=True,
        tools=True,
        cost_rank=1,
        notes="Qwen3.5-4B Q4_K_M + BF16 mmproj on llama.cpp. Multimodal, always-hot.",
    ),
    Tier.FAST: TierSpec(
        alias=Tier.FAST,
        node="gx10-01",
        max_context=262_144,
        vision=True,
        tools=True,
        cost_rank=2,
        notes="nvidia/Qwen3.6-35B-A3B-NVFP4 on vLLM. Vision + native tool calling.",
    ),
    Tier.REASON: TierSpec(
        alias=Tier.REASON,
        node="gx10-02",
        # Served context is deliberately below the model's 262k native window:
        # gx-reason already occupies ~86GiB of node 2's 121GiB.
        max_context=131_072,
        vision=True,
        tools=True,
        cost_rank=3,
        notes="et0dev/Qwen3.5-122B-A10B-NVFP4-FP8Dense-GB10 on stock vLLM. Owns node 2 exclusively.",
    ),
    Tier.MAX: TierSpec(
        alias=Tier.MAX,
        node="gx10-01+gx10-02",
        max_context=327_680,
        vision=False,
        tools=True,
        exclusive_cluster=True,
        cost_rank=4,
        notes="DeepSeek V4 Flash NVFP4 on SGLang TP=2 across both nodes.",
    ),
}

#: Tiers gx-auto is allowed to select, cheapest first.
ROUTABLE: tuple[Tier, ...] = (Tier.MINI, Tier.FAST, Tier.REASON, Tier.MAX)


def cheaper_alternatives(tier: Tier) -> list[Tier]:
    """Return routable tiers strictly cheaper than `tier`, most capable first."""
    target = TIERS[tier].cost_rank
    return sorted(
        (t for t in ROUTABLE if TIERS[t].cost_rank < target),
        key=lambda t: TIERS[t].cost_rank,
        reverse=True,
    )


#: The largest context any SINGLE-NODE tier can serve. A request above this can
#: only be served by gx-max. Derived from the table above so the router's
#: escalation threshold can never drift out of sync with the tier definitions.
MAX_SINGLE_NODE_CONTEXT: int = max(
    spec.max_context for spec in TIERS.values() if not spec.exclusive_cluster
)
