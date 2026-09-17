"""Authoritative per-model context budgeting (D-039).

The gateway must never knowingly forward a request whose

    effective input tokens + requested output tokens + safety margin

exceeds the served context window of the model it is sent to. Agentic
clients (Kilo Code, Claude Code, Cline) ask for their whole advertised output
window (`max_tokens: 32000`) on every turn and attach 15-20 k tokens of tool
schema. Forwarding that unchanged is how a 33.5 k-token prompt hit
gx-reason's 65 536 window and failed with ContextWindowExceededError
(2026-09-17, see coordination/DECISIONS.md D-039).

Two estimates are kept, on purpose:

* ``total_tokens`` is PESSIMISTIC (about 3.2 characters per token). It sizes
  the output allowance, so a clamped request always fits.
* ``lower_bound_tokens`` is OPTIMISTIC (6 characters per token). It is used
  only to decide that an input CERTAINLY cannot fit and may be refused
  without asking the backend.

Between the two the backend's own tokenizer is authoritative: the request is
sent with a small output allowance, and if the engine refuses it with an exact
token count, :func:`parse_context_error` lets the caller correct the output
budget ONCE and retry immediately (never the same payload twice).

Pure standard library and no I/O, so the LiteLLM pre-call hook can import it
as well (legenex/gateway/litellm/gx_budget_hook.py).
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping

#: Pessimistic characters per token (over-estimates the token count). The
#: 2026-09-17 failure: estimate 46 163, Qwen tokenizer 33 537 (ratio 1.38).
CHARS_PER_TOKEN_UPPER = 3.2
#: Optimistic characters per token (under-estimates). Only used to prove
#: that an input cannot fit whatever the real tokenizer says.
CHARS_PER_TOKEN_LOWER = 6.0

#: Chat-template tokens per message (role markers, separators).
PER_MESSAGE_OVERHEAD = 8
#: Template tokens per tool definition beyond its JSON body.
PER_TOOL_OVERHEAD = 16
#: Fixed envelope (BOS, generation prompt, tool-section header).
REQUEST_OVERHEAD = 64
#: Vision tokens per image. Qwen3.x caps an image at a few thousand tokens;
#: the upper figure is deliberately generous.
IMAGE_TOKENS_UPPER = 2_048
IMAGE_TOKENS_LOWER = 64

#: Safety margin between the estimate and the window: max(floor, fraction).
SAFETY_MARGIN_MIN = 512
SAFETY_MARGIN_FRACTION = 0.02
#: Below this many output tokens a clamped request is not useful.
MIN_USEFUL_OUTPUT = 1_024
#: Margin used when correcting from the engine's EXACT input count.
EXACT_RETRY_MARGIN = 32
#: A correction that leaves fewer output tokens than this is not attempted.
MIN_RETRY_OUTPUT = 256

STATUS_OK = "ok"                # requested output fits unchanged
STATUS_CLAMPED = "clamped"      # output reduced so the request fits
STATUS_TIGHT = "tight"          # estimate says it may not fit; engine decides
STATUS_OVERFLOW = "overflow"    # the input alone certainly cannot fit

ERROR_CODE = "context_length_exceeded"

_OUTPUT_KEYS = ("max_tokens", "max_completion_tokens")
_IMAGE_TYPES = {"image_url", "input_image", "image"}
#: Keys whose values are binary payloads, never prompt text.
_BINARY_KEYS = {"image_url", "url", "data", "b64_json", "input_audio"}


# ---------------------------------------------------------------------------
# Estimation
# ---------------------------------------------------------------------------


def _json_len(value: Any) -> int:
    try:
        return len(json.dumps(value, separators=(",", ":"), default=str))
    except (TypeError, ValueError):
        return 0


def _part_chars(part: Any) -> tuple[int, int]:
    """(text characters, images) of one content part."""
    if isinstance(part, str):
        return len(part), 0
    if not isinstance(part, Mapping):
        return 0, 0
    kind = part.get("type")
    if kind in _IMAGE_TYPES:
        return 0, 1
    if kind == "text":
        text = part.get("text")
        return (len(text) if isinstance(text, str) else 0), 0
    # Unknown part (tool_result blocks, documents, ...): count its text-like
    # values, never embedded binary payloads.
    chars = images = 0
    for key, value in part.items():
        if key in _BINARY_KEYS or key == "type":
            continue
        c, i = _content_chars(value)
        chars += c
        images += i
    return chars, images


def _content_chars(content: Any) -> tuple[int, int]:
    if content is None:
        return 0, 0
    if isinstance(content, str):
        return len(content), 0
    if isinstance(content, list):
        chars = images = 0
        for part in content:
            c, i = _part_chars(part)
            chars += c
            images += i
        return chars, images
    if isinstance(content, Mapping):
        return _part_chars(content)
    return len(str(content)), 0


@dataclass(frozen=True)
class InputEstimate:
    """The complete effective input of one chat request."""

    total_tokens: int
    lower_bound_tokens: int
    system_tokens: int
    history_tokens: int
    tool_schema_tokens: int
    tool_count: int
    message_count: int
    image_count: int
    overhead_tokens: int

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


def _tokens(chars: int, ratio: float) -> int:
    return int(chars / ratio) + (1 if chars else 0)


def _tool_items(payload: Mapping[str, Any]) -> list[Any]:
    items: list[Any] = []
    for key in ("tools", "functions"):
        value = payload.get(key)
        if isinstance(value, list):
            items.extend(value)
    return items


def estimate_input(payload: Mapping[str, Any]) -> InputEstimate:
    """Estimate everything the engine will tokenize for `payload`.

    Counts system, user, assistant and tool messages, assistant tool calls,
    replayed reasoning, tool/function schemas and template overhead. Image
    bytes are never counted as text.
    """
    if not isinstance(payload, Mapping):
        payload = {}
    raw = payload.get("messages")
    messages = [m for m in raw if isinstance(m, Mapping)] if isinstance(raw, list) else []

    system_chars = history_chars = reasoning_chars = 0
    images = 0
    for msg in messages:
        chars, imgs = _content_chars(msg.get("content"))
        images += imgs
        for key in ("tool_calls", "function_call"):
            if msg.get(key):
                chars += _json_len(msg.get(key))
        name = msg.get("name")
        if isinstance(name, str):
            chars += len(name)
        for key in ("reasoning_content", "reasoning"):
            value = msg.get(key)
            if isinstance(value, str):
                reasoning_chars += len(value)
        if msg.get("role") in ("system", "developer"):
            system_chars += chars
        else:
            history_chars += chars

    tools = _tool_items(payload)
    tool_chars = _json_len(tools) if tools else 0
    tool_upper = _tokens(tool_chars, CHARS_PER_TOKEN_UPPER)
    tool_lower = _tokens(tool_chars, CHARS_PER_TOKEN_LOWER)

    overhead = REQUEST_OVERHEAD + PER_MESSAGE_OVERHEAD * len(messages) + PER_TOOL_OVERHEAD * len(tools)
    system_upper = _tokens(system_chars, CHARS_PER_TOKEN_UPPER)
    history_upper = _tokens(history_chars + reasoning_chars, CHARS_PER_TOKEN_UPPER)
    total = system_upper + history_upper + tool_upper + overhead + IMAGE_TOKENS_UPPER * images
    lower = (
        _tokens(system_chars + history_chars, CHARS_PER_TOKEN_LOWER)
        + tool_lower
        + PER_MESSAGE_OVERHEAD * len(messages)
        + IMAGE_TOKENS_LOWER * images
    )
    return InputEstimate(
        total_tokens=total,
        lower_bound_tokens=lower,
        system_tokens=system_upper,
        history_tokens=history_upper,
        tool_schema_tokens=tool_upper,
        tool_count=len(tools),
        message_count=len(messages),
        image_count=images,
        overhead_tokens=overhead,
    )


def requested_output(payload: Mapping[str, Any]) -> int | None:
    """The caller's output budget, or None if it set none (or set junk)."""
    if not isinstance(payload, Mapping):
        return None
    for key in _OUTPUT_KEYS:
        value = payload.get(key)
        if isinstance(value, bool) or value is None:
            continue
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            return number
    return None


def safety_margin(context_limit: int) -> int:
    return max(SAFETY_MARGIN_MIN, int(context_limit * SAFETY_MARGIN_FRACTION))


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContextBudget:
    """How a request fits one model. Every field is safe to log and show."""

    model: str
    context_limit: int
    max_output_limit: int
    estimated_input_tokens: int
    input_lower_bound: int
    tool_schema_tokens: int
    tool_count: int
    requested_output_tokens: int | None
    safety_margin: int
    safe_output_tokens: int
    output_tokens: int | None
    clamped: bool
    remaining_context: int
    status: str
    error_code: str | None = None

    @property
    def fits(self) -> bool:
        return self.status != STATUS_OVERFLOW

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["fits"] = self.fits
        return out


def compute_budget(
    payload: Mapping[str, Any],
    *,
    model: str,
    context_limit: int,
    max_output_limit: int,
    estimate: InputEstimate | None = None,
    min_output: int = MIN_USEFUL_OUTPUT,
) -> ContextBudget:
    """Decide the output allowance for `payload` on one model.

    * A request whose output already fits is forwarded unchanged (apart from
      the engine's own output ceiling).
    * Otherwise the output is clamped to what fits, if that is still useful.
    * If even a useful output does not fit by the pessimistic estimate but the
      input might still fit, the request is `tight`: it is sent with a small
      allowance and the engine's exact count decides.
    * If the optimistic estimate already overflows, the status is `overflow`
      and the request must not be sent.
    """
    est = estimate or estimate_input(payload)
    margin = safety_margin(context_limit)
    remaining = context_limit - est.total_tokens - margin
    safe_output = max(0, min(max_output_limit, remaining))
    requested = requested_output(payload)

    if est.lower_bound_tokens + 1 > context_limit - 16:
        return ContextBudget(
            model=model, context_limit=context_limit, max_output_limit=max_output_limit,
            estimated_input_tokens=est.total_tokens, input_lower_bound=est.lower_bound_tokens,
            tool_schema_tokens=est.tool_schema_tokens, tool_count=est.tool_count,
            requested_output_tokens=requested, safety_margin=margin,
            safe_output_tokens=0, output_tokens=None, clamped=False,
            remaining_context=remaining, status=STATUS_OVERFLOW, error_code=ERROR_CODE,
        )

    wanted = requested if requested is not None else None
    if wanted is None:
        # No budget given: the engine uses the rest of the window. Only set
        # one if the window is so tight that a default would overflow.
        if remaining >= min_output:
            output, status = None, STATUS_OK
        else:
            output, status = min_output, STATUS_TIGHT
        clamped = False
    elif wanted <= safe_output:
        output, status, clamped = wanted, STATUS_OK, False
    elif safe_output >= min(min_output, wanted):
        output, status, clamped = safe_output, STATUS_CLAMPED, True
    else:
        output = min(wanted, min_output)
        status = STATUS_TIGHT
        clamped = output < wanted
    return ContextBudget(
        model=model, context_limit=context_limit, max_output_limit=max_output_limit,
        estimated_input_tokens=est.total_tokens, input_lower_bound=est.lower_bound_tokens,
        tool_schema_tokens=est.tool_schema_tokens, tool_count=est.tool_count,
        requested_output_tokens=requested, safety_margin=margin,
        safe_output_tokens=safe_output, output_tokens=output, clamped=clamped,
        remaining_context=remaining, status=status,
        error_code=None,
    )


def planned_fit(
    estimate: InputEstimate,
    *,
    context_limit: int,
    planning_output: int,
    requested: int | None,
) -> tuple[bool, bool]:
    """(fits, might_fit) for tier SELECTION.

    `fits`: pessimistic input + margin + a useful output all fit.
    `might_fit`: the optimistic input leaves room for a minimal output.
    """
    want = min(requested, planning_output) if requested else planning_output
    fits = estimate.total_tokens + safety_margin(context_limit) + want <= context_limit
    might = estimate.lower_bound_tokens + MIN_RETRY_OUTPUT <= context_limit
    return fits, might


def apply_budget(payload: Mapping[str, Any], budget: ContextBudget) -> dict[str, Any]:
    """A copy of `payload` carrying the budget's output allowance."""
    out = dict(payload)
    if budget.output_tokens is None:
        # Still honour the engine ceiling on a caller-supplied value.
        for key in _OUTPUT_KEYS:
            value = out.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value > budget.max_output_limit:
                out[key] = budget.max_output_limit
        return out
    present = [k for k in _OUTPUT_KEYS if k in out and out.get(k) is not None]
    if not present:
        out["max_tokens"] = budget.output_tokens
    for key in present:
        out[key] = budget.output_tokens
    return out


# ---------------------------------------------------------------------------
# Engine feedback
# ---------------------------------------------------------------------------

_VLLM_NEW = re.compile(
    r"maximum context length is (\d+) tokens.*?requested (\d+) output tokens.*?"
    r"prompt contains at least (\d+) input tokens",
    re.IGNORECASE | re.DOTALL,
)
_VLLM_OLD = re.compile(
    r"maximum context length is (\d+) tokens.*?requested (\d+) tokens \((\d+) in the messages",
    re.IGNORECASE | re.DOTALL,
)
_VLLM_INPUT_ONLY = re.compile(
    r"maximum context length is (\d+) tokens.*?(?:your (?:request|prompt|messages?) "
    r"(?:has|have|contains?)|resulted in) (\d+) (?:input )?tokens",
    re.IGNORECASE | re.DOTALL,
)
_LLAMACPP = re.compile(
    r"request \((\d+) tokens\) exceeds the available context size \((\d+) tokens\)",
    re.IGNORECASE,
)
_GENERIC = re.compile(
    r"context ?window ?exceeded|context_length_exceeded|maximum context length|"
    r"exceeds the available context size|prompt is too long",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class EngineContextError:
    context_limit: int | None
    input_tokens: int | None


def parse_context_error(body: str) -> EngineContextError | None:
    """Recognise an engine/gateway context-overflow error.

    Returns the engine's exact window and input count when the message
    carries them, an empty EngineContextError when it only says "too long",
    and None when the error is something else.
    """
    if not body:
        return None
    m = _VLLM_NEW.search(body)
    if m:
        return EngineContextError(int(m.group(1)), int(m.group(3)))
    m = _VLLM_OLD.search(body)
    if m:
        return EngineContextError(int(m.group(1)), int(m.group(3)))
    m = _LLAMACPP.search(body)
    if m:
        return EngineContextError(int(m.group(2)), int(m.group(1)))
    m = _VLLM_INPUT_ONLY.search(body)
    if m:
        return EngineContextError(int(m.group(1)), int(m.group(2)))
    if _GENERIC.search(body):
        return EngineContextError(None, None)
    return None


def corrected_output(err: EngineContextError, budget: ContextBudget) -> int | None:
    """The output allowance implied by the engine's exact count, or None.

    None means no valid payload can be built (unknown count, or the input
    leaves less than MIN_RETRY_OUTPUT): the failure is terminal.
    """
    if err.input_tokens is None:
        return None
    limit = err.context_limit or budget.context_limit
    room = min(budget.max_output_limit, limit - err.input_tokens - EXACT_RETRY_MARGIN)
    if budget.requested_output_tokens is not None:
        room = min(room, budget.requested_output_tokens)
    if room < MIN_RETRY_OUTPUT:
        return None
    if budget.output_tokens is not None and room >= budget.output_tokens:
        # The engine refused a budget no larger than this: retrying would
        # resend an equivalent payload.
        return None
    return room


def error_payload(
    budget: ContextBudget,
    *,
    message: str,
    engine: EngineContextError | None = None,
    attempts: int = 0,
    elapsed_ms: float | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """An OpenAI-shaped, non-retryable context-overflow error with the budget."""
    detail = budget.as_dict()
    if engine is not None:
        detail["engine_context_limit"] = engine.context_limit
        detail["engine_input_tokens"] = engine.input_tokens
    detail["attempts"] = attempts
    if elapsed_ms is not None:
        detail["elapsed_ms"] = round(elapsed_ms, 1)
    if extra:
        detail.update(extra)
    return {
        "error": {
            "message": message,
            "type": "invalid_request_error",
            "param": "messages",
            "code": ERROR_CODE,
            "retryable": False,
            "gx_budget": detail,
        }
    }


def overflow_message(budget: ContextBudget, hint: str = "") -> str:
    text = (
        f"{budget.model}: the request needs about {budget.estimated_input_tokens} input tokens "
        f"(at least {budget.input_lower_bound}, including {budget.tool_schema_tokens} tokens of "
        f"tool schema) but the model's context window is {budget.context_limit} tokens. "
        "Shorten or compact the conversation."
    )
    return f"{text} {hint}".strip()


def iter_budget_fields(budget: ContextBudget) -> Iterable[tuple[str, Any]]:
    """Compact journal fields."""
    yield "context_limit", budget.context_limit
    yield "estimated_input_tokens", budget.estimated_input_tokens
    yield "input_lower_bound", budget.input_lower_bound
    yield "tool_schema_tokens", budget.tool_schema_tokens
    yield "requested_output_tokens", budget.requested_output_tokens
    yield "safe_output_tokens", budget.safe_output_tokens
    yield "output_tokens", budget.output_tokens
    yield "clamped", budget.clamped
    yield "remaining_context", budget.remaining_context
    yield "budget_status", budget.status
