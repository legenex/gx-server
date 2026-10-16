"""The five public aliases as logical modes of ONE resident model (D-053).

    gx-fast    thinking off,  admission <= 32K,  top interactive priority
    gx-medium  thinking low,  admission <= 128K
    gx-high    thinking high, admission <= 128K
    gx-max     thinking max,  the largest tested safe window, capped concurrency
    gx-auto    task-aware: autoroute's intent picks one of the four

Everything here is a pure function of (payload, registry facts): no I/O. The
server owns the reasoning switch completely: whatever the client sent as
``chat_template_kwargs``, top-level ``reasoning_effort`` / ``reasoning`` /
``thinking``, it is removed and the mode's kwargs are injected, so a caller
can never exceed its mode. Output is clamped to the mode's ceiling and the
context admission is the mode's window (budget.py does the arithmetic).

Dialects (how a reasoning level is spelled for the engine):

* ``deepseek_v4`` (V4 Flash, vLLM deepseek_v4 parser): ``{"thinking": false}``
  or ``{"thinking": true, "reasoning_effort": "low"|"high"|"max"}``. A
  top-level reasoning_effort is ignored by the engine, hence the kwargs.
* ``deepseek_v41`` (Mia V4.1 rollback): the registry ``reasoning.mapping``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from . import autoroute as AR
from . import budget as B
from .profiles import ModeSpec, Registry, RegistryError

#: alias -> logical mode (gx-auto is resolved per request).
ALIAS_MODE: dict[str, str] = {
    "gx-fast": "fast",
    "gx-medium": "medium",
    "gx-high": "high",
    "gx-max": "max",
}
ALIAS_AUTO = "gx-auto"
MODE_ALIASES: tuple[str, ...] = (ALIAS_AUTO, *ALIAS_MODE)
#: Escalation order when a request does not fit a mode's window.
MODE_ORDER: tuple[str, ...] = ("fast", "medium", "high", "max")

#: autoroute intent -> mode. Hard debugging escalates to max (see decide()).
INTENT_MODE: dict[str, str] = {
    AR.INTENT_INTERACTIVE: "fast",
    AR.INTENT_IMPLEMENTATION: "medium",
    AR.INTENT_BURST: "medium",
    AR.INTENT_ARCHITECTURE: "high",
    AR.INTENT_DEBUGGING: "high",
    AR.INTENT_LONG_CONTEXT: "high",
    AR.INTENT_VALIDATION: "max",
}

_V4_EFFORT = {"low": "low", "medium": "high", "high": "high", "xhigh": "max", "max": "max"}
#: Client fields that would let a caller pick its own reasoning depth.
_CLIENT_REASONING_FIELDS = ("reasoning_effort", "reasoning", "thinking", "enable_thinking",
                            "chat_template_kwargs", "include_reasoning")


def reasoning_kwargs(dialect: str, level: str, registry: Registry) -> dict[str, Any]:
    """chat_template_kwargs for `level` in the deployment's dialect."""
    if dialect == "deepseek_v4":
        if level in ("none", "minimal"):
            return {"thinking": False}
        effort = _V4_EFFORT.get(level)
        if effort is None:
            raise RegistryError(f"reasoning level {level!r} has no deepseek_v4 spelling")
        return {"thinking": True, "reasoning_effort": effort}
    if dialect == "deepseek_v41":
        return registry.reasoning_kwargs(level)
    raise RegistryError(f"unknown reasoning dialect {dialect!r}")


@dataclass(frozen=True)
class ModeDecision:
    alias: str
    mode: str
    reason: str
    route: "AR.RouteDecision | None" = None

    def as_log_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"alias": self.alias, "mode": self.mode, "mode_reason": self.reason}
        if self.route is not None:
            out.update(self.route.as_log_dict())
        return out


def choose_mode(alias: str, payload: Mapping[str, Any], registry: Registry,
                headers: "Mapping[str, str] | None" = None) -> ModeDecision:
    """The mode for one request. Fixed for gx-fast/medium/high/max; for gx-auto
    the autoroute intent, escalated to the smallest mode whose window fits."""
    if alias in ALIAS_MODE:
        return ModeDecision(alias, ALIAS_MODE[alias], f"alias {alias}")
    if alias != ALIAS_AUTO:
        raise RegistryError(f"{alias!r} is not a mode alias")
    # Only the intent hint is honoured from headers; profile/reasoning overrides
    # are a privilege handled by the caller (identity policy), not here.
    hint = {k: v for k, v in (headers or {}).items() if k.lower() == "x-gx-intent"}
    route = AR.decide(payload, headers=hint)
    mode = INTENT_MODE.get(route.intent, "medium")
    reason = f"intent {route.intent}"
    if route.intent == AR.INTENT_DEBUGGING and route.features.debug_hard_score >= AR.DEBUG_HARD_THRESHOLD:
        mode, reason = "max", reason + " with hard-debugging evidence"
    need = route.features.approx_context_tokens
    start = MODE_ORDER.index(mode)
    for candidate in MODE_ORDER[start:]:
        if need <= registry.mode(candidate).max_context_tokens:
            if candidate != mode:
                reason += f"; context ~{need} tokens > {mode} window, escalated to {candidate}"
            mode = candidate
            break
    else:
        mode = "max"
        reason += f"; context ~{need} tokens exceeds every window (max refuses with the exact numbers)"
    return ModeDecision(alias, mode, reason, route)


def shape_request(payload: Mapping[str, Any], spec: ModeSpec, kwargs: Mapping[str, Any]
                  ) -> tuple[dict[str, Any], list[str]]:
    """Server-owned reasoning + output ceiling. Returns (body, what-was-overridden)."""
    body = dict(payload)
    stripped = [k for k in _CLIENT_REASONING_FIELDS if k in body]
    for key in stripped:
        body.pop(key, None)
    body["chat_template_kwargs"] = dict(kwargs)
    for key in ("max_tokens", "max_completion_tokens"):
        value = body.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > spec.max_output_tokens:
            body[key] = spec.max_output_tokens
            stripped.append(f"{key}>{spec.max_output_tokens}")
    return body, stripped


def mode_budget(payload: Mapping[str, Any], spec: ModeSpec, alias: str,
                estimate: "B.InputEstimate | None" = None) -> B.ContextBudget:
    """Admission for the mode's window (input + output within max_context_tokens)."""
    return B.compute_budget(
        payload,
        model=f"{alias}:{spec.name}",
        context_limit=spec.max_context_tokens,
        max_output_limit=spec.max_output_tokens,
        estimate=estimate,
    )
