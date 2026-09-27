"""Deterministic intent -> (profile, reasoning) routing for `gx-auto`.

The old classifier chose a MODEL tier. There is one model now: routing means
choosing how to SHAPE it -- a serving profile (max_num_seqs / speculation /
window) and a reasoning level. Like the classifier it replaces, this module
is deterministic: no embeddings, no ML, no network. Every decision is
explainable from the request alone and is journaled with its reasons.

Inputs, in precedence order (ARCHITECTURE-V41 §3):

1. USER OVERRIDES -- the X-GX-Profile / X-GX-Reasoning headers win outright.
2. LONG CONTEXT -- an approximate context above 96k tokens goes to the
   `long` profile no matter the intent: prefill cost dominates and a single
   stream is the right shape for it.
3. INTENT -- the X-GX-Intent header if present (interactive, implementation,
   architecture, debugging, validation, burst, long-context), otherwise
   inferred from content features (agent envelopes, reasoning evidence,
   tool burden) extracted exactly as the old classifier did.

Intent map (registry profiles; reasoning per intent):

    interactive    -> fast / medium       (one stream, interactive latency)
    implementation -> balanced / medium   (AgentOS default, 2 generations)
    architecture   -> deep / high         (1-2 streams, heavy reasoning)
    validation     -> deep / max           (review/audit work)
    debugging      -> deep / medium, max when the task shows hard-debugging
                      evidence (race conditions, deadlocks, flakiness...)
    burst          -> swarm / low          (many logical agents, throughput)
    long-context   -> long / high          (large prefill, TTFT warning)

Feature extraction (envelope stripping, task-text isolation, reasoning
indicators, tool-name scanning) is carried over verbatim in spirit from the
old classifier: agent clients (Kilo Code, Claude Code, Cline, Roo) wrap
every user turn in envelopes full of words like "prove" and "architecture",
so only the current HUMAN instruction is read for meaning and evidence is
diluted by instruction length.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from . import budget as B

# --------------------------------------------------------------------------
# Token estimation (request-level estimates live in budget.estimate_input)
# --------------------------------------------------------------------------

_CHARS_PER_TOKEN = B.CHARS_PER_TOKEN_UPPER


def estimate_tokens(text: str) -> int:
    """Estimate the token count of `text`. Intentionally pessimistic."""
    if not text:
        return 0
    return int(len(text) / _CHARS_PER_TOKEN) + 1


def _iter_text_parts(content: Any) -> Iterable[str]:
    if isinstance(content, str):
        yield content
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, str):
                yield part
            elif isinstance(part, Mapping):
                if part.get("type") == "text" and isinstance(part.get("text"), str):
                    yield part["text"]


def request_fingerprint(payload: Mapping[str, Any]) -> str:
    """A stable short hash of the conversation, for log/request matching.

    Only `messages` is hashed: a client can recompute it from what it sent,
    and it identifies one exact request in the routing log without the log
    ever containing prompt text.
    """
    messages = payload.get("messages") if isinstance(payload, Mapping) else None
    blob = json.dumps(messages, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
# Task-text extraction (agent envelopes) -- unchanged from the classifier
# --------------------------------------------------------------------------

_ENVELOPE_BLOCKS = re.compile(
    r"<(environment_details|system-reminder|system_reminder|file_content|folder_content|"
    r"custom_instructions|explicit_instructions|slash_command|workspace_configuration|"
    r"user_instructions|context|command-message|command-name|command-args|"
    r"local-command-stdout|local-command-stderr|ide_selection|ide_opened_file|"
    r"ide_diagnostics|git_status|claudeMd|project_instructions|instructions|"
    r"attached_files?|tool_use_error)\b[^>]*>.*?</\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
_ENVELOPE_OPEN_TAIL = re.compile(
    r"<(environment_details|file_content|folder_content|system-reminder)\b[^>]*>.*\Z",
    re.IGNORECASE | re.DOTALL,
)
_TASK_TAGS = re.compile(
    r"<(task|user_message|feedback|answer)>\s*(.*?)\s*</\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
_TOOL_RESULT_TEXT = re.compile(
    r"^\s*(\[[^\]\n]{1,160}\]\s*Result:|<tool_result\b|<function_results\b|Tool result:|"
    r"\[ERROR\] You did not use a tool)",
    re.IGNORECASE,
)


def _strip_envelopes(text: str) -> str:
    text = _ENVELOPE_BLOCKS.sub(" ", text)
    return _ENVELOPE_OPEN_TAIL.sub(" ", text)


def _task_from_text(text: str) -> str:
    tagged = [m.group(2) for m in _TASK_TAGS.finditer(text)]
    if tagged:
        return "\n".join(_strip_envelopes(t) for t in tagged).strip()
    return _strip_envelopes(text).strip()


def _has_tool_result_parts(content: Any) -> bool:
    if isinstance(content, list):
        for part in content:
            if isinstance(part, Mapping) and part.get("type") in {"tool_result", "function_result"}:
                return True
    return False


def _is_tool_result_turn(msg: Mapping[str, Any]) -> bool:
    content = msg.get("content")
    if _has_tool_result_parts(content):
        return True
    raw = "\n".join(_iter_text_parts(content))
    return bool(_TOOL_RESULT_TEXT.search(raw)) and not _TASK_TAGS.search(raw)


def _extract_task(messages: Sequence[Mapping[str, Any]]) -> tuple[str, bool]:
    """Return (task_text, continuation). Same contract as the classifier:
    the newest turn inside an agent loop defers to the most recent HUMAN
    instruction."""
    valid = [m for m in messages if isinstance(m, Mapping)]
    if not valid:
        return "", False

    last = valid[-1]
    continuation = str(last.get("role") or "") in {"tool", "function"}

    last_user_idx = None
    for idx in range(len(valid) - 1, -1, -1):
        if valid[idx].get("role") == "user":
            last_user_idx = idx
            break

    if last_user_idx is not None and not continuation:
        msg = valid[last_user_idx]
        raw = "\n".join(_iter_text_parts(msg.get("content")))
        task = _task_from_text(raw)
        if _is_tool_result_turn(msg):
            continuation = True
        elif not task:
            prev = valid[last_user_idx - 1] if last_user_idx > 0 else {}
            if isinstance(prev, Mapping) and prev.get("tool_calls"):
                continuation = True
        else:
            return task, False

    if not continuation:
        return "", False

    for msg in reversed(valid):
        if msg.get("role") != "user" or _is_tool_result_turn(msg):
            continue
        task = _task_from_text("\n".join(_iter_text_parts(msg.get("content"))))
        if task:
            return task, True
    return "", True


# --------------------------------------------------------------------------
# Evidence indicators (TASK CONTENT ONLY -- envelopes stripped first)
# --------------------------------------------------------------------------

#: Specific evidence that the task itself needs deep reasoning. Generic
#: engineering vocabulary is deliberately absent ("refactor", "architecture"
#: in a work order is ordinary work, not evidence).
_REASONING_INDICATORS: tuple[tuple[str, str, int], ...] = (
    ("proof", r"\bprove (that|the|this|it|why|there)\b|\bproof (of|that|for)\b|\btheorem\b|\blemma\b|\bcorollary\b", 4),
    ("derivation", r"\bderiv(e|ation)\b.{0,60}\b(formula|equation|closed[- ]form|bound|expression|"
                   r"probability|gradient|complexity|recurrence|identity)\b|\bclosed[- ]form\b", 4),
    ("formal", r"\bformal(ly)? (verify|verification|proof|spec|semantics)\b|\binduction (on|over)\b", 4),
    ("explicit", r"\bthink (hard|deeply|carefully|it through)\b|\bstep[- ]by[- ]step\b|"
                 r"\breason (carefully|rigorously|it out)\b|\bdeep reasoning\b|\bchain of thought\b", 2),
    ("math", r"\b(integral|derivative|eigen\w*|matri(x|ces) (rank|inverse)|probability|combinatori\w*|"
             r"expected value|modular arithmetic|diophantine|polynomial|inequality)\b", 2),
    ("algorithmic", r"\btime complexity\b|\bbig-?o\b|\basymptotic\b|\bnp-?(hard|complete)\b|"
                    r"\bdynamic programming\b|\bamortized\b", 2),
    ("root_cause", r"\broot[- ]cause\b|\bwhy does\b|\bexplain why\b", 1),
    ("invariant", r"\binvariant\b|\bcorrectness\b|\bsoundness\b", 1),
)
#: Hard-debugging evidence: concurrency and intermittent-failure shapes that
#: need maximal reasoning, not just deep.
_HARD_DEBUG_INDICATORS: tuple[tuple[str, str, int], ...] = (
    ("concurrency", r"\brace condition\b|\bdeadlock\b|\blivelock\b|\bheisenbug\b|\bdata race\b|"
                    r"\bmemory leak\b|\bconcurren(cy|t)\b.{0,40}\b(bug|issue|problem|failure)\b", 3),
    ("intermittent", r"\bintermittent\w*\b|\bflaky\b|\bnon-?deterministic\b", 3),
)
#: Review/audit shapes -> the validation intent (deep, maximal reasoning).
_VALIDATION_INDICATORS: tuple[tuple[str, str, int], ...] = (
    ("exhaustive", r"\b(exhaustiv\w*|comprehensive)\b.*\b(analysis|review|audit)\b", 3),
    ("formal", r"\bformal (verification|proof|spec)", 4),
    ("review", r"\b(critical|independent|security) (review|audit)\b|\bcode review\b.*\b(verify|thorough)\b", 2),
)
#: Many-agent / fan-out shapes -> the burst intent (swarm).
_BURST_INDICATORS: tuple[tuple[str, str], ...] = (
    (r"\b(in parallel|fan ?out|swarm)\b", "parallel"),
    (r"\b(spawn|launch|run|start)\b.{0,40}\b(\d{2,}|multiple|many|several|all)\b.{0,30}\b(agents?|workers?|subagents?)\b", "many_agents"),
    (r"\b(multiple|many|several)\b.{0,30}\b(agents?|subagents?|workers?)\b", "multiple_agents"),
)

_COMPILED_REASONING = tuple((n, re.compile(p, re.IGNORECASE | re.DOTALL), w) for n, p, w in _REASONING_INDICATORS)
_COMPILED_HARD_DEBUG = tuple((n, re.compile(p, re.IGNORECASE | re.DOTALL), w) for n, p, w in _HARD_DEBUG_INDICATORS)
_COMPILED_VALIDATION = tuple((n, re.compile(p, re.IGNORECASE | re.DOTALL), w) for n, p, w in _VALIDATION_INDICATORS)
_COMPILED_BURST = tuple((re.compile(p, re.IGNORECASE), name) for p, name in _BURST_INDICATORS)


# --------------------------------------------------------------------------
# Intent vocabulary
# --------------------------------------------------------------------------

INTENT_INTERACTIVE = "interactive"
INTENT_IMPLEMENTATION = "implementation"
INTENT_ARCHITECTURE = "architecture"
INTENT_DEBUGGING = "debugging"
INTENT_VALIDATION = "validation"
INTENT_BURST = "burst"
INTENT_LONG_CONTEXT = "long-context"

#: Values the X-GX-Intent header may carry (contract with the gateway).
KNOWN_INTENTS: tuple[str, ...] = (
    INTENT_INTERACTIVE, INTENT_IMPLEMENTATION, INTENT_ARCHITECTURE,
    INTENT_DEBUGGING, INTENT_VALIDATION, INTENT_BURST, INTENT_LONG_CONTEXT,
)

#: Intent -> (profile, default reasoning). The registry's profile table is
#: authoritative for what each profile MEANS; this table only picks among
#: registry profiles, so a registry edit moves the shapes, not the intents.
INTENT_PROFILE: dict[str, tuple[str, str]] = {
    INTENT_INTERACTIVE: ("fast", "medium"),
    INTENT_IMPLEMENTATION: ("balanced", "medium"),
    INTENT_ARCHITECTURE: ("deep", "high"),
    INTENT_DEBUGGING: ("deep", "medium"),
    INTENT_VALIDATION: ("deep", "max"),
    INTENT_BURST: ("swarm", "low"),
    INTENT_LONG_CONTEXT: ("long", "high"),
}

#: Above this approximate context (tokens) the `long` profile applies
#: regardless of intent: prefill dominates and one stream is the right shape.
LONG_CONTEXT_TOKENS = 96_000

#: Evidence thresholds (effective score, after density dilution).
REASONING_DEBUG_THRESHOLD = 2
DEBUG_HARD_THRESHOLD = 3
VALIDATION_THRESHOLD = 3
#: Evidence is diluted beyond this many instruction tokens: a long work
#: order mentions everything once.
DENSITY_REFERENCE_TOKENS = 400

#: A whole message that is only a greeting / presence check / ack.
_CONVERSATIONAL_WHOLE = re.compile(
    r"^\W*(hi|hello|hey|hiya|yo|ping|test(ing)?|thanks?( you)?|thank you|ty|ok(ay)?|cool|"
    r"great|nice|good (morning|afternoon|evening|night)|you there|anyone (there|home)|"
    r"still there|are you (there|here|alive|awake|online|up|working|ready)|"
    r"r u there|u there|hello there|hey there)\W*$",
    re.IGNORECASE,
)
_CONVERSATIONAL_MAX_TOKENS = 40

#: The intent-to-profile map is data; content-based inference below feeds it.
_CODING_TOOL_NAME = re.compile(
    r"(write|edit|apply|patch|diff|replace|insert|create_file|delete_file|"
    r"execute|command|shell|bash|terminal|run_|read_file|list_files|search_files|codebase|"
    r"^read$|^glob$|^grep$|^task$|notebook)",
    re.IGNORECASE,
)
_ACTION_VERB = re.compile(
    r"\b(fix|implement|add|create|write|build|make|edit|modify|update|change|refactor|"
    r"remove|delete|replace|migrate|convert|port|install|configure|deploy|run|test|debug|"
    r"patch|generate|scaffold|document|lint|review|optimi[sz]e|rewrite|extend|"
    r"investigate|diagnose|resolve|read|open|inspect|analy[sz]e|explain|design)\b",
    re.IGNORECASE,
)


def _tool_names(payload: Mapping[str, Any]) -> list[str]:
    names: list[str] = []
    for key in ("tools", "functions"):
        items = payload.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, Mapping):
                continue
            fn = item.get("function") if isinstance(item.get("function"), Mapping) else item
            name = fn.get("name") if isinstance(fn, Mapping) else None
            if isinstance(name, str):
                names.append(name)
    return names


def _score(task: str, compiled, density: float) -> tuple[int, list[str]]:
    raw = 0
    hits: list[str] = []
    for name, rx, weight in compiled:
        if rx.search(task):
            raw += weight
            hits.append(name)
    return int(raw * density + 1e-9), hits


def _header(headers: Mapping[str, str] | None, name: str) -> str:
    """First non-empty value of `name` in `headers` (case-insensitive)."""
    if not headers:
        return ""
    for key, value in headers.items():
        if key.lower() == name.lower() and str(value).strip():
            return str(value).strip()
    return ""


# --------------------------------------------------------------------------
# Features + decision
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RequestFeatures:
    """Everything the autorouter extracted from the incoming request."""

    intent: str
    inferred: bool
    approx_context_tokens: int
    task_tokens: int
    reasoning_score: int
    debug_hard_score: int
    validation_score: int
    burst_hit: str = ""
    coding_toolset: bool = False
    continuation: bool = False
    indicators: tuple[str, ...] = ()
    signals: tuple[str, ...] = ()


@dataclass(frozen=True)
class RouteDecision:
    """The outcome of one gx-auto routing decision: profile + reasoning + why."""

    profile: str
    reasoning: str
    intent: str
    features: RequestFeatures
    reason: str
    profile_override: bool = False
    reasoning_override: bool = False

    def as_log_dict(self) -> dict[str, Any]:
        f = self.features
        return {
            "profile": self.profile,
            "reasoning": self.reasoning,
            "intent": f.intent,
            "inferred_intent": f.inferred,
            "reason": self.reason,
            "profile_override": self.profile_override,
            "reasoning_override": self.reasoning_override,
            "approx_context_tokens": f.approx_context_tokens,
            "task_tokens": f.task_tokens,
            "reasoning_score": f.reasoning_score,
            "debug_hard_score": f.debug_hard_score,
            "validation_score": f.validation_score,
            "burst": f.burst_hit,
            "signals": list(f.signals),
        }

    def summary(self) -> str:
        """One line a human can read in the Control Center."""
        return f"{self.profile}/{self.reasoning}: {self.reason} (intent {self.features.intent})"


def extract_features(
    payload: Mapping[str, Any],
    *,
    intent_header: str = "",
    approx_context_tokens: "int | None" = None,
) -> RequestFeatures:
    """Derive routing features from an OpenAI chat-completions payload.

    `intent_header` is the X-GX-Intent value (already read by the caller).
    `approx_context_tokens` may be supplied directly (the /v1/completions
    path); otherwise it is estimated from the payload.
    """
    if not isinstance(payload, Mapping):
        payload = {}
    raw_messages = payload.get("messages")
    messages: Sequence[Mapping[str, Any]] = [
        m for m in (raw_messages if isinstance(raw_messages, list) else []) if isinstance(m, Mapping)
    ]
    est = B.estimate_input(payload)
    ctx = int(approx_context_tokens) if approx_context_tokens is not None else est.total_tokens

    task, continuation = _extract_task(messages)
    names = _tool_names(payload)
    coding_toolset = any(_CODING_TOOL_NAME.search(n) for n in names)
    task_tokens = estimate_tokens(task)
    density = min(1.0, DENSITY_REFERENCE_TOKENS / task_tokens) if task_tokens else 1.0

    reasoning, r_hits = _score(task, _COMPILED_REASONING, density)
    hard_debug, d_hits = _score(task, _COMPILED_HARD_DEBUG, density)
    validation, v_hits = _score(task, _COMPILED_VALIDATION, density)
    burst_hit = next((name for rx, name in _COMPILED_BURST if task and rx.search(task)), "")

    # Intent: header wins; otherwise infer from content.
    header_intent = intent_header.strip().lower()
    inferred = False
    if header_intent in KNOWN_INTENTS:
        intent = header_intent
    else:
        intent, inferred = _infer_intent(
            task, continuation, coding_toolset, reasoning, hard_debug, validation, burst_hit
        )

    signals = [f"intent:{intent}", f"ctx:{ctx}", f"task_tokens:{task_tokens}"]
    signals += [f"reasoning:{h}" for h in r_hits]
    signals += [f"hard_debug:{h}" for h in d_hits]
    signals += [f"validation:{h}" for h in v_hits]
    if burst_hit:
        signals.append(f"burst:{burst_hit}")

    return RequestFeatures(
        intent=intent,
        inferred=inferred,
        approx_context_tokens=ctx,
        task_tokens=task_tokens,
        reasoning_score=reasoning,
        debug_hard_score=hard_debug,
        validation_score=validation,
        burst_hit=burst_hit,
        coding_toolset=coding_toolset,
        continuation=continuation,
        indicators=tuple(r_hits + d_hits + v_hits),
        signals=tuple(signals),
    )


def _infer_intent(
    task: str,
    continuation: bool,
    coding_toolset: bool,
    reasoning: int,
    hard_debug: int,
    validation: int,
    burst_hit: str,
) -> tuple[str, bool]:
    """Content-based intent inference. Deterministic, order matters."""
    if not task.strip() and not continuation:
        # Nothing to read: a bare capability probe rides the interactive shape.
        return INTENT_INTERACTIVE, True
    if _CONVERSATIONAL_WHOLE.match(task) or (
        estimate_tokens(task) <= _CONVERSATIONAL_MAX_TOKENS and not coding_toolset and not continuation
        and re.search(r"\b(are|r) (you|u) (there|here|alive|awake|online|up|working|ready)\b|"
                      r"\bwhat can you (do|help)\b|\bwho are you\b|\bthanks?\b", task, re.IGNORECASE)
    ):
        return INTENT_INTERACTIVE, True
    if validation >= VALIDATION_THRESHOLD:
        return INTENT_VALIDATION, True
    if burst_hit:
        return INTENT_BURST, True
    if hard_debug >= DEBUG_HARD_THRESHOLD or (
        reasoning >= REASONING_DEBUG_THRESHOLD and _ACTION_VERB.search(task)
        and re.search(r"\b(debug|diagnose|troubleshoot|investigate|root[- ]cause|why)\b", task, re.IGNORECASE)
    ):
        return INTENT_DEBUGGING, True
    if reasoning >= REASONING_DEBUG_THRESHOLD and re.search(
        r"\b(design|architect|architecture|trade[- ]offs?|structure|system design)\b", task, re.IGNORECASE
    ):
        return INTENT_ARCHITECTURE, True
    # Default work shape: agentic/coding implementation, plain questions
    # included -- balanced is the AgentOS default profile for a reason.
    return INTENT_IMPLEMENTATION, True


def decide(
    payload: Mapping[str, Any],
    *,
    headers: Mapping[str, str] | None = None,
    approx_context_tokens: "int | None" = None,
) -> RouteDecision:
    """Choose (profile, reasoning) for one gx-auto request. Pure function.

    Precedence: X-GX-Profile / X-GX-Reasoning overrides, then the long-context
    rule, then the intent (header or inferred). Overrides are validated
    against nothing here -- the caller (server) validates the profile name
    against the registry before applying it, so an unknown header value is a
    400, not a silent default.
    """
    headers = headers or {}
    features = extract_features(
        payload,
        intent_header=_header(headers, "X-GX-Intent"),
        approx_context_tokens=approx_context_tokens,
    )

    profile_override = _header(headers, "X-GX-Profile")
    reasoning_override = _header(headers, "X-GX-Reasoning")
    default_profile, default_reasoning = INTENT_PROFILE[features.intent]

    # Rule 2: long context. Prefill dominates; the `long` profile is the
    # single-stream shape built for it. The intent's reasoning level is kept:
    # what changes is the serving shape, not the thinking depth.
    profile, reasoning = default_profile, default_reasoning
    if features.approx_context_tokens > LONG_CONTEXT_TOKENS and profile != "long":
        profile = "long"
        reason = (
            f"context ~{features.approx_context_tokens} tokens > {LONG_CONTEXT_TOKENS}; "
            f"long profile (intent {features.intent} keeps reasoning {default_reasoning})"
        )
    else:
        reason = f"intent {features.intent}"
        if features.intent is INTENT_DEBUGGING and features.debug_hard_score >= DEBUG_HARD_THRESHOLD:
            reasoning = "max"
            reason += f" with hard-debugging evidence ({features.indicators})"

    if profile_override:
        profile = profile_override
        reason += f"; profile overridden by X-GX-Profile"
    if reasoning_override:
        reasoning = reasoning_override
        reason += "; reasoning overridden by X-GX-Reasoning"

    return RouteDecision(
        profile=profile,
        reasoning=reasoning,
        intent=features.intent,
        features=features,
        reason=reason.lstrip(),
        profile_override=bool(profile_override),
        reasoning_override=bool(reasoning_override),
    )
