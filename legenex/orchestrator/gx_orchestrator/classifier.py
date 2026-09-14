"""Deterministic task classifier for the `gx-auto` alias.

Design intent (see ARCHITECTURE.md): gx-auto is a *deterministic* first version.
No embeddings, no ML, no network calls. Every decision is explainable from the
request alone, and every decision is logged with the reasons that produced it.

The classifier is a pure function of the request plus a snapshot of cluster
availability. That makes it fully unit-testable with no running cluster.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from .tiers import MAX_SINGLE_NODE_CONTEXT, TIERS, ROUTABLE, Tier, cheaper_alternatives

# --------------------------------------------------------------------------
# Token estimation
# --------------------------------------------------------------------------

#: Conservative characters-per-token ratio for English + code. Real tokenisers
#: land around 3.5-4.0; we deliberately UNDER-estimate characters per token
#: (i.e. over-estimate token count) so we never route a prompt to a tier whose
#: context cannot hold it.
_CHARS_PER_TOKEN = 3.2


def estimate_tokens(text: str) -> int:
    """Estimate the token count of `text`. Intentionally pessimistic."""
    if not text:
        return 0
    return int(len(text) / _CHARS_PER_TOKEN) + 1


def _iter_text_parts(content: Any) -> Iterable[str]:
    """Yield the text fragments of an OpenAI `content` field.

    Handles both the plain-string form and the multipart list form used for
    vision requests.
    """
    if isinstance(content, str):
        yield content
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, str):
                yield part
            elif isinstance(part, Mapping):
                if part.get("type") == "text" and isinstance(part.get("text"), str):
                    yield part["text"]


def _has_image(content: Any) -> bool:
    """True if an OpenAI `content` field carries image input."""
    if isinstance(content, list):
        for part in content:
            if isinstance(part, Mapping) and part.get("type") in {
                "image_url",
                "input_image",
                "image",
            }:
                return True
    return False


# --------------------------------------------------------------------------
# Complexity signals
# --------------------------------------------------------------------------

# Each pattern contributes to a complexity score. Patterns are word-boundary
# anchored and case-insensitive. Weights are deliberately coarse: the point is
# a stable, explainable ordering, not a calibrated probability.
_REASONING_PATTERNS: tuple[tuple[str, int], ...] = (
    (r"\bprove\b|\bproof\b|\btheorem\b", 3),
    (r"\bderive\b|\bderivation\b", 3),
    (r"\boptimi[sz]e\b.*\b(algorithm|complexity|performance)\b", 3),
    (r"\btime complexity\b|\bbig-?o\b|\basymptotic\b", 3),
    (r"\bdesign\b.*\b(architecture|system|schema|protocol)\b", 2),
    (r"\brefactor\b|\bredesign\b", 2),
    (r"\bdebug\b|\broot cause\b|\bstack trace\b|\btraceback\b", 2),
    (r"\bstep by step\b|\bthink through\b|\breason(ing)? about\b", 2),
    (r"\bwhy does\b|\bexplain why\b", 1),
    (r"\bimplement\b|\bwrite a (program|function|class|script)\b", 1),
    (r"\bmathematical\b|\bequation\b|\bintegral\b", 2),
)

_TOOL_PATTERNS: tuple[tuple[str, int], ...] = (
    (r"\btool[_ ]?call\b|\bfunction[_ ]?call\b", 2),
    (r"\bapi\b|\bendpoint\b|\bcurl\b|\bhttp\b", 1),
    (r"\bsearch (the )?(web|internet)\b|\bfetch\b|\bscrape\b", 1),
    (r"\bagent\b|\bworkflow\b|\borchestrat", 1),
)

_TRIVIAL_PATTERNS: tuple[tuple[str, int], ...] = (
    (r"^\s*(hi|hello|hey|thanks|thank you|ok|okay|yes|no)\b", -3),
    (r"\btranslate\b|\bsummari[sz]e\b(?!.*\bpaper\b)", -1),
    (r"\bclassify\b|\bextract\b|\broute\b|\bwhich (tool|model)\b", -2),
    (r"\bwhat time\b|\bwhat is the (date|weather)\b", -3),
)

_HARD_PATTERNS: tuple[tuple[str, int], ...] = (
    (r"\bresearch\b.*\b(paper|survey|literature)\b", 3),
    (r"\bwhole (codebase|repository|repo)\b|\bentire (codebase|repo)\b", 4),
    (r"\bexhaustiv|\bcomprehensive\b.*\b(analysis|review|audit)\b", 3),
    (r"\bformal (verification|proof|spec)", 4),
    (r"\bnovel\b.*\b(algorithm|approach|method)\b", 3),
)

_ALL_PATTERNS: tuple[tuple[str, int, str], ...] = tuple(
    [(p, w, "reasoning") for p, w in _REASONING_PATTERNS]
    + [(p, w, "tool") for p, w in _TOOL_PATTERNS]
    + [(p, w, "trivial") for p, w in _TRIVIAL_PATTERNS]
    + [(p, w, "hard") for p, w in _HARD_PATTERNS]
)

_COMPILED = tuple((re.compile(p, re.IGNORECASE), w, kind) for p, w, kind in _ALL_PATTERNS)


# --------------------------------------------------------------------------
# Result types
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RequestFeatures:
    """Everything the router extracted from the incoming request."""

    prompt_tokens: int
    requested_max_tokens: int
    total_context_needed: int
    has_images: bool
    has_tools: bool
    complexity_score: int
    reasoning_score: int
    hard_score: int
    latency_preference: str  # "low" | "balanced" | "quality"
    signals: tuple[str, ...] = ()


@dataclass(frozen=True)
class RoutingDecision:
    """The outcome of routing, including why — this is what gets logged."""

    tier: Tier
    features: RequestFeatures
    reasons: tuple[str, ...]
    downgraded_from: Tier | None = None

    def as_log_dict(self) -> dict[str, Any]:
        f = self.features
        return {
            "tier": self.tier.value,
            "downgraded_from": self.downgraded_from.value if self.downgraded_from else None,
            "prompt_tokens": f.prompt_tokens,
            "max_tokens": f.requested_max_tokens,
            "context_needed": f.total_context_needed,
            "has_images": f.has_images,
            "has_tools": f.has_tools,
            "complexity": f.complexity_score,
            "reasoning_score": f.reasoning_score,
            "hard_score": f.hard_score,
            "latency_preference": f.latency_preference,
            "signals": list(f.signals),
            "reasons": list(self.reasons),
        }


# --------------------------------------------------------------------------
# Feature extraction
# --------------------------------------------------------------------------

#: Above this many tokens of context, only gx-max can serve the request.
#: Derived from the tier table, never hardcoded -- if a single-node tier's
#: served context changes, this moves with it.
LARGE_CONTEXT_THRESHOLD = MAX_SINGLE_NODE_CONTEXT
#: Above this, we prefer a big-context tier even if the task looks simple.
MEDIUM_CONTEXT_THRESHOLD = 60_000

#: Complexity score cut-points. Tuned to be conservative: a request must look
#: genuinely hard before it is escalated to an expensive tier.
COMPLEXITY_FAST = 1     # >= this -> at least gx-fast
COMPLEXITY_REASON = 4   # >= this -> at least gx-reason

#: gx-max is NOT reachable by accumulating ordinary reasoning keywords. It takes
#: over BOTH nodes and evicts every other model, so gx-auto only escalates to it
#: on an explicit "extreme" marker (the `hard` pattern category) or on a context
#: that genuinely does not fit anywhere else. A hard debugging or refactoring
#: request is a gx-reason task, not a gx-max task.
HARD_SCORE_MAX = 3      # >= this in the `hard` category -> gx-max


def extract_features(payload: Mapping[str, Any]) -> RequestFeatures:
    """Derive routing features from an OpenAI chat-completions payload."""
    messages: Sequence[Mapping[str, Any]] = payload.get("messages") or []

    texts: list[str] = []
    has_images = False
    for msg in messages:
        content = msg.get("content")
        texts.extend(_iter_text_parts(content))
        has_images = has_images or _has_image(content)

    joined = "\n".join(texts)
    prompt_tokens = estimate_tokens(joined)

    # Only the most recent user turn drives complexity scoring: earlier turns
    # are context, not the task being asked for now.
    last_user = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            last_user = "\n".join(_iter_text_parts(msg.get("content")))
            break

    by_kind: dict[str, int] = {"reasoning": 0, "tool": 0, "trivial": 0, "hard": 0}
    # `trivial` is tracked separately so an explicitly trivial request can still
    # opt out of the tool floor below.
    signals: list[str] = []
    for rx, weight, kind in _COMPILED:
        if rx.search(last_user):
            by_kind[kind] += weight
            signals.append(f"{kind}:{rx.pattern[:40]}:{weight:+d}")
    score = sum(by_kind.values())

    # Very long single instructions tend to be genuinely harder.
    last_user_tokens = estimate_tokens(last_user)
    if last_user_tokens > 2_000:
        score += 2
        signals.append("length:long_instruction:+2")
    elif last_user_tokens < 30:
        score -= 1
        signals.append("length:very_short:-1")

    requested_max = int(payload.get("max_tokens") or payload.get("max_completion_tokens") or 1024)
    if requested_max > 8_000:
        score += 1
        signals.append("output:large_max_tokens:+1")

    has_tools = bool(payload.get("tools") or payload.get("functions"))
    if has_tools:
        score += 1
        signals.append("tools:present:+1")

    latency = str(
        (payload.get("metadata") or {}).get("latency")
        or payload.get("gx_latency")
        or "balanced"
    ).lower()
    if latency not in {"low", "balanced", "quality"}:
        latency = "balanced"

    return RequestFeatures(
        reasoning_score=by_kind["reasoning"],
        hard_score=by_kind["hard"],
        prompt_tokens=prompt_tokens,
        requested_max_tokens=requested_max,
        total_context_needed=prompt_tokens + requested_max,
        has_images=has_images,
        has_tools=has_tools,
        complexity_score=score,
        latency_preference=latency,
        signals=tuple(signals),
    )


# --------------------------------------------------------------------------
# Routing
# --------------------------------------------------------------------------


def _base_tier(f: RequestFeatures) -> tuple[Tier, list[str]]:
    """Pick a tier from the request features alone, ignoring availability."""
    reasons: list[str] = []

    # Hard constraint first: context must fit.
    if f.total_context_needed > LARGE_CONTEXT_THRESHOLD:
        reasons.append(
            f"context {f.total_context_needed} > {LARGE_CONTEXT_THRESHOLD} "
            "(largest single-node window): only gx-max fits"
        )
        return Tier.MAX, reasons

    if f.hard_score >= HARD_SCORE_MAX:
        reasons.append(
            f"hard-category score {f.hard_score} >= {HARD_SCORE_MAX}: explicitly extreme task"
        )
        tier = Tier.MAX
    elif f.complexity_score >= COMPLEXITY_REASON:
        reasons.append(f"complexity {f.complexity_score} >= {COMPLEXITY_REASON}: hard reasoning/coding")
        tier = Tier.REASON
    elif f.complexity_score >= COMPLEXITY_FAST:
        reasons.append(f"complexity {f.complexity_score} >= {COMPLEXITY_FAST}: normal agentic work")
        tier = Tier.FAST
    else:
        reasons.append(f"complexity {f.complexity_score} < {COMPLEXITY_FAST}: simple/dispatch task")
        tier = Tier.MINI

    # Medium context nudges up one tier if we picked the smallest.
    if f.total_context_needed >= MEDIUM_CONTEXT_THRESHOLD and TIERS[tier].max_context < f.total_context_needed:
        reasons.append(
            f"context {f.total_context_needed} exceeds {tier.value} window; escalating"
        )
        for candidate in ROUTABLE:
            if TIERS[candidate].max_context >= f.total_context_needed:
                tier = candidate
                break

    # Latency preference shifts one step, never past a hard constraint.
    if f.latency_preference == "low" and tier not in (Tier.MINI,):
        cheaper = cheaper_alternatives(tier)
        if cheaper and TIERS[cheaper[0]].max_context >= f.total_context_needed:
            reasons.append(f"latency=low: stepping down {tier.value} -> {cheaper[0].value}")
            tier = cheaper[0]
    elif f.latency_preference == "quality" and tier is not Tier.MAX:
        order = [t for t in ROUTABLE if TIERS[t].cost_rank > TIERS[tier].cost_rank]
        if order:
            reasons.append(f"latency=quality: stepping up {tier.value} -> {order[0].value}")
            tier = order[0]

    # Floor: a request that actually carries tool definitions is agentic work by
    # definition, and gx-mini is not the right tier for it -- unless the task is
    # explicitly trivial (a pure dispatch/classification call).
    if f.has_tools and tier is Tier.MINI and f.complexity_score > -2:
        reasons.append("tool definitions present: raising gx-mini -> gx-fast (tool floor)")
        tier = Tier.FAST

    return tier, reasons


def route(
    payload: Mapping[str, Any],
    *,
    available: Mapping[Tier, bool] | None = None,
    busy: Mapping[Tier, bool] | None = None,
) -> RoutingDecision:
    """Choose a tier for a gx-auto request.

    Args:
        payload: The OpenAI chat-completions request body.
        available: Tier -> whether it can currently serve at all. A tier absent
            from the mapping is assumed available.
        busy: Tier -> whether it is currently saturated. gx-auto is permitted to
            route around a busy gx-max; a DIRECT gx-max request never is (that
            rule is enforced in the server, not here).

    Returns:
        A RoutingDecision carrying the chosen tier and the reasons for it.
    """
    available = dict(available or {})
    busy = dict(busy or {})

    f = extract_features(payload)
    tier, reasons = _base_tier(f)

    # Vision is a capability, not a tier: if the request carries images we must
    # land on a tier whose model actually accepts them.
    if f.has_images and not TIERS[tier].vision:
        vision_tiers = [t for t in ROUTABLE if TIERS[t].vision]
        if vision_tiers:
            # Prefer the most capable vision tier at or below the chosen cost.
            at_or_below = [t for t in vision_tiers if TIERS[t].cost_rank <= TIERS[tier].cost_rank]
            chosen = max(at_or_below or vision_tiers, key=lambda t: TIERS[t].cost_rank)
            reasons.append(f"image input present and {tier.value} has no vision: -> {chosen.value}")
            tier = chosen

    original = tier

    # Availability / saturation fallback. gx-auto degrades gracefully rather
    # than failing; it never escalates past what the task needed.
    def _usable(t: Tier) -> bool:
        return available.get(t, True) and not busy.get(t, False)

    if not _usable(tier):
        for candidate in cheaper_alternatives(tier):
            if _usable(candidate) and TIERS[candidate].max_context >= f.total_context_needed:
                if f.has_images and not TIERS[candidate].vision:
                    continue
                reasons.append(
                    f"{tier.value} unavailable/busy: falling back to {candidate.value}"
                )
                tier = candidate
                break
        else:
            reasons.append(
                f"{tier.value} unavailable/busy and no cheaper tier fits; staying on {tier.value}"
            )

    return RoutingDecision(
        tier=tier,
        features=f,
        reasons=tuple(reasons),
        downgraded_from=original if tier is not original else None,
    )
