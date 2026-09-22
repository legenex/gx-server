"""Deterministic task classifier for the `gx-auto` alias.

Design intent (see ARCHITECTURE.md §6): gx-auto is deterministic. No
embeddings, no ML, no network calls. Every decision is explainable from the
request alone, and every decision is logged with the reasons that produced it.

The classifier is a pure function of the request plus a snapshot of cluster
availability, which makes it fully unit-testable with no running cluster.

FOUR SEPARATE CONCEPTS (D-030, reworked by D-039)
-------------------------------------------------
Agentic coding clients (Kilo Code, Claude Code, Cline, Roo) send every turn
with a large system prompt, a large `tools` array, a large `max_tokens`, and
a user turn wrapped in envelopes (`<task>`, `<environment_details>`,
`<system-reminder>` carrying CLAUDE.md, ...). Those materials are full of
words such as "prove", "derive", "architecture", "agent" and "implement". On
2026-09-17 a Claude-Code continuation whose work order mentioned them scored
complexity 15 and was sent to gx-reason, where it did not fit. So:

* TASK CONTENT: only the current human instruction (envelopes stripped,
  tool results skipped) is read for meaning.
* REASONING INDICATORS: specific evidence that the task itself needs a
  reasoning model (a proof, a derivation, a concurrency bug, an explicit
  "think step by step"). Generic engineering vocabulary is not evidence.
  Evidence is diluted by the length of the instruction: a 2 000-token work
  order that mentions "prove" once is not a proof request.
* CONTEXT BURDEN and TOOL BURDEN: system prompt, history and tool schemas.
  They decide which tier can HOLD the request (budget.py). They never raise
  the semantic tier.
* EXPLICIT REQUEST and LATENCY PREFERENCE: `gx_tier` / `metadata.gx_tier`
  and `metadata.latency` from the caller.

gx-fast is the default workhorse for coding, agentic and ordinary work;
gx-mini takes greetings and short simple questions; gx-reason takes focused
tasks with real reasoning evidence; gx-max is used only when it is already
running and the task is explicitly extreme (the server never acquires it for
gx-auto).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from . import budget as B
from .tiers import MAX_SINGLE_NODE_CONTEXT, TIERS, ROUTABLE, Tier, cheaper_alternatives

# --------------------------------------------------------------------------
# Token estimation
# --------------------------------------------------------------------------

#: Kept for callers that estimate a single string. Request-level estimates
#: live in budget.estimate_input().
_CHARS_PER_TOKEN = B.CHARS_PER_TOKEN_UPPER


def estimate_tokens(text: str) -> int:
    """Estimate the token count of `text`. Intentionally pessimistic."""
    if not text:
        return 0
    return int(len(text) / _CHARS_PER_TOKEN) + 1


def _iter_text_parts(content: Any) -> Iterable[str]:
    """Yield the text fragments of an OpenAI `content` field."""
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
# Task-text extraction (agent envelopes)
# --------------------------------------------------------------------------

#: Blocks that agent clients attach to a user turn. They are context, not the
#: task.
_ENVELOPE_BLOCKS = re.compile(
    r"<(environment_details|system-reminder|system_reminder|file_content|folder_content|"
    r"custom_instructions|explicit_instructions|slash_command|workspace_configuration|"
    r"user_instructions|context|command-message|command-name|command-args|"
    r"local-command-stdout|local-command-stderr|ide_selection|ide_opened_file|"
    r"ide_diagnostics|git_status|claudeMd|project_instructions|instructions|"
    r"attached_files?|tool_use_error)\b[^>]*>.*?</\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
#: An unterminated envelope (clients sometimes truncate) swallows the rest.
_ENVELOPE_OPEN_TAIL = re.compile(
    r"<(environment_details|file_content|folder_content|system-reminder)\b[^>]*>.*\Z",
    re.IGNORECASE | re.DOTALL,
)
_TASK_TAGS = re.compile(
    r"<(task|user_message|feedback|answer)>\s*(.*?)\s*</\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
#: Cline/Roo/Kilo XML-protocol tool results come back as user turns.
_TOOL_RESULT_TEXT = re.compile(
    r"^\s*(\[[^\]\n]{1,160}\]\s*Result:|<tool_result\b|<function_results\b|Tool result:|"
    r"\[ERROR\] You did not use a tool)",
    re.IGNORECASE,
)


def _strip_envelopes(text: str) -> str:
    text = _ENVELOPE_BLOCKS.sub(" ", text)
    return _ENVELOPE_OPEN_TAIL.sub(" ", text)


def _task_from_text(text: str) -> str:
    """The instruction inside one user turn, with envelopes removed."""
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
    """Return (task_text, continuation).

    `continuation` is True when the newest turn is a tool result (or an
    envelope-only turn after assistant tool calls) inside an agent loop. The
    task is then the most recent HUMAN instruction in the conversation, which
    is what the model is currently working on.
    """
    valid = [m for m in messages if isinstance(m, Mapping)]
    if not valid:
        return "", False

    last = valid[-1]
    role = str(last.get("role") or "")
    continuation = role in {"tool", "function"}

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
            # Nothing but an envelope. If the assistant was mid-loop (it made
            # tool calls), this is the loop continuing; otherwise it is empty.
            prev = valid[last_user_idx - 1] if last_user_idx > 0 else {}
            if isinstance(prev, Mapping) and prev.get("tool_calls"):
                continuation = True
        else:
            return task, False

    if not continuation:
        return "", False

    # Agent loop: the newest human instruction that is not a tool result.
    for msg in reversed(valid):
        if msg.get("role") != "user" or _is_tool_result_turn(msg):
            continue
        task = _task_from_text("\n".join(_iter_text_parts(msg.get("content"))))
        if task:
            return task, True
    return "", True


# --------------------------------------------------------------------------
# Reasoning and extreme indicators (TASK CONTENT ONLY)
# --------------------------------------------------------------------------

# Each indicator is word-boundary anchored and case-insensitive and counts
# once per request. They are specific on purpose: "prove" in "prove it via the
# routing journal" is not a proof request, "architecture" is ordinary
# engineering work for gx-fast, "debug" and "refactor" are everyday coding.
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
    ("concurrency", r"\brace condition\b|\bdeadlock\b|\blivelock\b|\bheisenbug\b|\bdata race\b|"
                    r"\bmemory leak\b|\bconcurren(cy|t)\b.{0,40}\b(bug|issue|problem|failure)\b", 2),
    ("puzzle", r"\b(puzzle|riddle|brain ?teaser|logic problem|counter-?example)\b", 2),
    ("root_cause", r"\broot[- ]cause\b|\bwhy does\b|\bexplain why\b", 1),
    ("invariant", r"\binvariant\b|\bcorrectness\b|\bsoundness\b", 1),
    ("intermittent", r"\bintermittent\w*\b|\bflaky\b|\bnon-?deterministic\b", 1),
)

#: Explicitly extreme requests. Only meaningful when gx-max is already
#: running (the server never acquires it for gx-auto). Each alone reaches the
#: threshold only with a qualifier; see TestHardCategoryFalsePositives.
_HARD_INDICATORS: tuple[tuple[str, str, int], ...] = (
    ("research", r"\bresearch\b.*\b(paper|survey|literature)\b", 3),
    ("whole_repo", r"\bwhole (codebase|repository|repo)\b|\bentire (codebase|repo)\b", 2),
    ("exhaustive", r"\b(exhaustiv\w*|comprehensive)\b.*\b(analysis|review|audit)\b", 3),
    ("formal", r"\bformal (verification|proof|spec)", 4),
    ("novel", r"\bnovel\b.*\b(algorithm|approach|method)\b", 3),
)

_COMPILED_REASONING = tuple((n, re.compile(p, re.IGNORECASE | re.DOTALL), w) for n, p, w in _REASONING_INDICATORS)
_COMPILED_HARD = tuple((n, re.compile(p, re.IGNORECASE | re.DOTALL), w) for n, p, w in _HARD_INDICATORS)

# --------------------------------------------------------------------------
# Intent
# --------------------------------------------------------------------------

#: A whole message that is only a greeting / presence check / acknowledgement.
_CONVERSATIONAL_WHOLE = re.compile(
    r"^\W*(hi|hello|hey|hiya|yo|ping|test(ing)?|thanks?( you)?|thank you|ty|ok(ay)?|cool|"
    r"great|nice|good (morning|afternoon|evening|night)|you there|anyone (there|home)|"
    r"still there|are you (there|here|alive|awake|online|up|working|ready)|"
    r"r u there|u there|hello there|hey there)\W*$",
    re.IGNORECASE,
)
#: Presence, identity and capability questions, anywhere in a short message.
_CONVERSATIONAL_ANY = re.compile(
    r"\b(are|r) (you|u) (there|here|alive|awake|online|up|working|ready)\b|"
    r"\bwhat can you (do|help)\b|\bhow can you help\b|\bwhat (can|could) (i|we) (do|ask)\b|"
    r"\bwho are you\b|\bwhat are you\b|\bwhich (model|llm) are you\b|\bwhat (model|llm) are you\b|"
    r"\bhow are you\b|\bintroduce yourself\b|\bwhat (are|is) your (capabilities|skills)\b|"
    r"^\W*thanks?\b.{0,30}$",
    re.IGNORECASE,
)
#: Imperative coding / repository work.
_ACTION_VERB = re.compile(
    r"\b(fix|implement|add|create|write|build|make|edit|modify|update|change|refactor|"
    r"rename|remove|delete|replace|migrate|convert|port|install|configure|set ?up|"
    r"deploy|run|execute|test|debug|patch|bump|upgrade|generate|scaffold|document|"
    r"lint|format|review|optimi[sz]e|rewrite|extend|wire|hook up|integrate|commit|"
    r"investigate|diagnose|troubleshoot|resolve|read|open|inspect|analy[sz]e|explain|design)\b",
    re.IGNORECASE,
)
_CODE_OBJECT = re.compile(
    r"\b(file|files|function|functions|method|class|classes|module|component|tests?|"
    r"unit tests?|bug|bugs|error|errors|exception|issue|endpoint|api|script|config|"
    r"configuration|repo|repository|code|codebase|project|package|dependenc(y|ies)|"
    r"feature|page|route|schema|migration|query|dockerfile|compose|readme|docs?|"
    r"ui|frontend|backend|css|html|workflow|pipeline|build|lint|linter|type ?errors?|"
    r"server|service|database|table|handler|controller|hook|struct|interface|crate|"
    r"library|cli|commit|branch|pr|pull request|diff|log|logs|architecture|stack trace|traceback)\b",
    re.IGNORECASE,
)
_CODE_ARTIFACT = re.compile(
    r"```|\b[\w./-]+\.(py|js|mjs|cjs|ts|tsx|jsx|go|rs|java|kt|c|cc|cpp|h|hpp|cs|rb|php|"
    r"swift|md|json|ya?ml|toml|ini|sh|bash|sql|html|css|scss|vue|svelte|lua|tf|proto)\b|"
    r"\b(def|class|function|const|import|return)\s+\w",
    re.IGNORECASE,
)
#: Tool names that mark an attached schema as a coding agent's toolbox.
_CODING_TOOL_NAME = re.compile(
    r"(write|edit|apply|patch|diff|replace|insert|create_file|delete_file|"
    r"execute|command|shell|bash|terminal|run_|read_file|list_files|search_files|codebase|"
    r"^read$|^glob$|^grep$|^task$|notebook)",
    re.IGNORECASE,
)

INTENT_CONVERSATIONAL = "conversational"
INTENT_SIMPLE = "simple"
INTENT_ACTION = "action"
INTENT_CONTINUATION = "continuation"

#: Short-message ceiling for the "anywhere in the message" conversational rule.
_CONVERSATIONAL_MAX_TOKENS = 40


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


def _classify_intent(task: str, continuation: bool) -> str:
    if continuation:
        return INTENT_CONTINUATION
    if not task.strip():
        return INTENT_SIMPLE
    if _CONVERSATIONAL_WHOLE.match(task):
        return INTENT_CONVERSATIONAL
    if estimate_tokens(task) <= _CONVERSATIONAL_MAX_TOKENS and _CONVERSATIONAL_ANY.search(task):
        # "what can you help me with in this repo?" is still a capability
        # question: it asks about the assistant, not for work on the repo.
        return INTENT_CONVERSATIONAL
    if _ACTION_VERB.search(task) and (_CODE_OBJECT.search(task) or _CODE_ARTIFACT.search(task)):
        return INTENT_ACTION
    if _CODE_ARTIFACT.search(task):
        return INTENT_ACTION
    return INTENT_SIMPLE


# --------------------------------------------------------------------------
# Policy constants
# --------------------------------------------------------------------------

#: Above this many tokens of context, only gx-max can serve the request.
#: Derived from the tier table, never hardcoded.
LARGE_CONTEXT_THRESHOLD = MAX_SINGLE_NODE_CONTEXT

#: Effective reasoning evidence needed for gx-reason.
REASONING_REASON = 4
#: Effective reasoning evidence that lifts a simple question off gx-mini.
REASONING_FAST = 1
#: Effective `hard` evidence for gx-max (used only if gx-max is running).
HARD_SCORE_MAX = 3
#: Evidence is diluted beyond this many instruction tokens: a long work
#: order mentions everything once.
DENSITY_REFERENCE_TOKENS = 400
#: An agentic instruction longer than this is a work order; its vocabulary
#: never escalates it past gx-fast (a caller can still ask for gx-reason).
AGENTIC_FOCUS_TOKENS = 600
#: A simple (non-coding) question longer than this is long-form work.
SIMPLE_MINI_MAX_TOKENS = 1_500
#: A tool-less request asking for more output than this is long-form work.
LONG_OUTPUT_TOKENS = 8_000

#: Compatibility aliases (older tests and docs).
COMPLEXITY_FAST = REASONING_FAST
COMPLEXITY_REASON = REASONING_REASON

_EXPLICIT_TIERS = {
    "mini": Tier.MINI, "gx-mini": Tier.MINI,
    "fast": Tier.CODE, "gx-fast": Tier.CODE,
    "code": Tier.CODE, "gx-code": Tier.CODE,
    "reason": Tier.CODE, "gx-reason": Tier.CODE,
    "max": Tier.MAX, "gx-max": Tier.MAX,
}


def _explicit_tier(payload: Mapping[str, Any]) -> Tier | None:
    meta = payload.get("metadata") if isinstance(payload.get("metadata"), Mapping) else {}
    for value in (payload.get("gx_tier"), meta.get("gx_tier")):
        if isinstance(value, str) and value.strip().lower() in _EXPLICIT_TIERS:
            return _EXPLICIT_TIERS[value.strip().lower()]
    return None


# --------------------------------------------------------------------------
# Result types
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RequestFeatures:
    """Everything the router extracted from the incoming request."""

    # CONTEXT BURDEN (budget.estimate_input)
    prompt_tokens: int
    requested_max_tokens: int
    total_context_needed: int
    has_images: bool
    has_tools: bool
    # TASK CONTENT
    complexity_score: int
    reasoning_score: int
    hard_score: int
    latency_preference: str  # "low" | "balanced" | "quality"
    signals: tuple[str, ...] = ()
    intent: str = INTENT_SIMPLE
    task_tokens: int = 0
    # TOOL BURDEN
    tool_schema_tokens: int = 0
    tool_count: int = 0
    coding_toolset: bool = False
    # D-039 additions
    reasoning_raw: int = 0
    hard_raw: int = 0
    density: float = 1.0
    indicators: tuple[str, ...] = ()
    explicit_tier: str | None = None
    input_lower_bound: int = 0
    system_tokens: int = 0
    history_tokens: int = 0
    requested_output: int | None = None
    estimate: B.InputEstimate | None = None


@dataclass(frozen=True)
class RoutingDecision:
    """The outcome of routing, including why — this is what gets logged."""

    tier: Tier
    features: RequestFeatures
    reasons: tuple[str, ...]
    downgraded_from: Tier | None = None
    #: The tier may hold the request only if the engine's exact count agrees.
    tight_fit: bool = False
    #: No routable tier can hold the input at all.
    no_fit: bool = False

    def as_log_dict(self) -> dict[str, Any]:
        f = self.features
        return {
            "tier": self.tier.value,
            "downgraded_from": self.downgraded_from.value if self.downgraded_from else None,
            "intent": f.intent,
            "explicit_tier": f.explicit_tier,
            "prompt_tokens": f.prompt_tokens,
            "input_lower_bound": f.input_lower_bound,
            "system_tokens": f.system_tokens,
            "history_tokens": f.history_tokens,
            "task_tokens": f.task_tokens,
            "tool_schema_tokens": f.tool_schema_tokens,
            "tool_count": f.tool_count,
            "coding_toolset": f.coding_toolset,
            "max_tokens": f.requested_output,
            "context_needed": f.total_context_needed,
            "has_images": f.has_images,
            "has_tools": f.has_tools,
            "complexity": f.complexity_score,
            "reasoning_score": f.reasoning_score,
            "reasoning_raw": f.reasoning_raw,
            "hard_score": f.hard_score,
            "density": f.density,
            "indicators": list(f.indicators),
            "latency_preference": f.latency_preference,
            "tight_fit": self.tight_fit,
            "no_fit": self.no_fit,
            "signals": list(f.signals),
            "reasons": list(self.reasons),
            "summary": self.summary(),
        }

    def summary(self) -> str:
        """One line a human can read in the Control Center."""
        f = self.features
        why = next((r for r in self.reasons if r.startswith("tier:")), "")
        why = why[len("tier:"):].strip() if why else (self.reasons[-1] if self.reasons else "")
        return (
            f"{self.tier.value}: {why} (intent {f.intent}, task ~{f.task_tokens} tok, "
            f"input ~{f.prompt_tokens} tok incl. {f.tool_schema_tokens} tool-schema)"
        )


# --------------------------------------------------------------------------
# Feature extraction
# --------------------------------------------------------------------------


def _score(task: str, compiled, density: float) -> tuple[int, int, list[str]]:
    raw = 0
    hits: list[str] = []
    for name, rx, weight in compiled:
        if rx.search(task):
            raw += weight
            hits.append(name)
    return raw, int(raw * density + 1e-9), hits


def extract_features(payload: Mapping[str, Any]) -> RequestFeatures:
    """Derive routing features from an OpenAI chat-completions payload."""
    if not isinstance(payload, Mapping):
        payload = {}
    raw_messages = payload.get("messages")
    messages: Sequence[Mapping[str, Any]] = [
        m for m in (raw_messages if isinstance(raw_messages, list) else []) if isinstance(m, Mapping)
    ]
    has_images = any(_has_image(m.get("content")) for m in messages)
    est = B.estimate_input(payload)

    task, continuation = _extract_task(messages)
    intent = _classify_intent(task, continuation)
    names = _tool_names(payload)
    coding_toolset = any(_CODING_TOOL_NAME.search(n) for n in names)
    task_tokens = estimate_tokens(task)
    density = min(1.0, DENSITY_REFERENCE_TOKENS / task_tokens) if task_tokens else 1.0

    reasoning_raw, reasoning, r_hits = _score(task, _COMPILED_REASONING, density)
    hard_raw, hard, h_hits = _score(task, _COMPILED_HARD, density)

    signals: list[str] = [f"intent:{intent}", f"task_tokens:{task_tokens}", f"density:{density:.2f}"]
    signals += [f"reasoning:{h}" for h in r_hits] + [f"hard:{h}" for h in h_hits]
    if intent == INTENT_CONVERSATIONAL and (reasoning or hard):
        # A presence check or capability question is never "complex".
        signals.append("intent:conversational:cap->0")
        reasoning = hard = 0

    requested = B.requested_output(payload)
    has_tools = bool(payload.get("tools") or payload.get("functions"))
    if has_tools:
        signals.append(f"tools:{len(names)}:schema_tokens={est.tool_schema_tokens}:context_only")

    meta = payload.get("metadata") if isinstance(payload.get("metadata"), Mapping) else {}
    latency = str(meta.get("latency") or payload.get("gx_latency") or "balanced").lower()
    if latency not in {"low", "balanced", "quality"}:
        latency = "balanced"
    explicit = _explicit_tier(payload)

    planned = min(requested, B.MIN_USEFUL_OUTPUT * 4) if requested else B.MIN_USEFUL_OUTPUT * 4
    return RequestFeatures(
        prompt_tokens=est.total_tokens,
        requested_max_tokens=requested or 0,
        total_context_needed=est.total_tokens + planned,
        has_images=has_images,
        has_tools=has_tools,
        complexity_score=reasoning,
        reasoning_score=reasoning,
        hard_score=hard,
        latency_preference=latency,
        signals=tuple(signals),
        intent=intent,
        task_tokens=task_tokens,
        tool_schema_tokens=est.tool_schema_tokens,
        tool_count=est.tool_count,
        coding_toolset=coding_toolset,
        reasoning_raw=reasoning_raw,
        hard_raw=hard_raw,
        density=round(density, 3),
        indicators=tuple(r_hits + [f"hard:{h}" for h in h_hits]),
        explicit_tier=explicit.value if explicit else None,
        input_lower_bound=est.lower_bound_tokens,
        system_tokens=est.system_tokens,
        history_tokens=est.history_tokens,
        requested_output=requested,
        estimate=est,
    )


# --------------------------------------------------------------------------
# Routing
# --------------------------------------------------------------------------


def _fit(f: RequestFeatures, tier: Tier) -> tuple[bool, bool]:
    spec = TIERS[tier]
    assert f.estimate is not None
    return B.planned_fit(
        f.estimate,
        context_limit=spec.max_context,
        planning_output=spec.planning_output,
        requested=f.requested_output,
    )


def _semantic_tier(f: RequestFeatures) -> tuple[Tier, list[str]]:
    """The tier the TASK needs, ignoring size and availability."""
    reasons: list[str] = [f"intent={f.intent}"]
    agentic = f.intent in (INTENT_ACTION, INTENT_CONTINUATION) or f.coding_toolset

    if f.explicit_tier:
        tier = Tier(f.explicit_tier)
        reasons.append(f"tier: explicit request for {tier.value}")
        return tier, reasons
    if f.intent == INTENT_CONVERSATIONAL:
        reasons.append("tier: greeting / presence / capability question")
        reasons.extend(_tool_note(f))
        return Tier.MINI, reasons
    if f.hard_score >= HARD_SCORE_MAX:
        reasons.append(f"tier: explicitly extreme task (hard evidence {f.hard_score} >= {HARD_SCORE_MAX})")
        return Tier.MAX, reasons
    if f.reasoning_score >= REASONING_REASON:
        if agentic and f.task_tokens > AGENTIC_FOCUS_TOKENS:
            reasons.append(
                f"reasoning mentions ({','.join(f.indicators)}) sit in a {f.task_tokens}-token agentic work "
                f"order (> {AGENTIC_FOCUS_TOKENS}); not evidence of a reasoning task"
            )
        else:
            reasons.append(
                f"tier: task needs deeper reasoning (evidence {f.reasoning_score} >= {REASONING_REASON}: "
                f"{','.join(f.indicators)})"
            )
            return Tier.CODE, reasons
    if f.intent in (INTENT_ACTION, INTENT_CONTINUATION):
        reasons.append(f"tier: coding/agentic work ({f.intent}) -> gx-code")
        return Tier.CODE, reasons
    if f.reasoning_score >= REASONING_FAST:
        reasons.append(f"tier: some reasoning evidence ({','.join(f.indicators)}) -> gx-code")
        return Tier.CODE, reasons
    if f.task_tokens > SIMPLE_MINI_MAX_TOKENS:
        reasons.append(f"tier: long instruction ({f.task_tokens} tokens) -> gx-code")
        return Tier.CODE, reasons
    if f.requested_output and f.requested_output > LONG_OUTPUT_TOKENS and not f.has_tools:
        reasons.append(f"tier: long-form answer requested ({f.requested_output} tokens) -> gx-code")
        return Tier.CODE, reasons
    if f.coding_toolset and f.intent == INTENT_SIMPLE and f.task_tokens > _CONVERSATIONAL_MAX_TOKENS:
        reasons.append("tier: non-trivial question inside a coding agent -> gx-code")
        return Tier.CODE, reasons
    reasons.append("tier: short simple task")
    reasons.extend(_tool_note(f))
    return Tier.MINI, reasons


def _tool_note(f: RequestFeatures) -> list[str]:
    if not f.has_tools:
        return []
    return [
        f"tools attached ({f.tool_count}, ~{f.tool_schema_tokens} tokens) but intent is "
        f"{f.intent}: schema size does not raise the tier"
    ]


def _fitting_tier(f: RequestFeatures, tier: Tier, reasons: list[str]) -> tuple[Tier, bool, bool]:
    """Move to the nearest tier that can HOLD the request. Returns (tier, tight, no_fit)."""
    fits, _might = _fit(f, tier)
    if fits:
        return tier, False, False
    base_rank = TIERS[tier].cost_rank
    fitting = [t for t in ROUTABLE if _fit(f, t)[0] and (t is not Tier.MINI or tier is Tier.MINI)]
    if fitting:
        chosen = min(
            fitting,
            key=lambda t: (TIERS[t].exclusive_cluster, abs(TIERS[t].cost_rank - base_rank), -TIERS[t].cost_rank),
        )
        reasons.append(
            f"context: ~{f.prompt_tokens} input + {TIERS[tier].planning_output} output does not fit "
            f"{tier.value} ({TIERS[tier].max_context}); {chosen.value} ({TIERS[chosen].max_context}) holds it"
        )
        return chosen, False, False
    might = [t for t in ROUTABLE if _fit(f, t)[1]]
    if might:
        chosen = max(might, key=lambda t: TIERS[t].max_context)
        reasons.append(
            f"context: ~{f.prompt_tokens} input (at least {f.input_lower_bound}) is tight everywhere; "
            f"{chosen.value} has the largest window, the engine's exact count decides"
        )
        return chosen, True, False
    reasons.append(
        f"context: input of at least {f.input_lower_bound} tokens exceeds every tier "
        f"(largest {max(s.max_context for s in TIERS.values())})"
    )
    return Tier.MAX, False, True


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
        busy: Tier -> whether it is currently saturated or must not be used
            (the server passes gx-max as busy unless it is already READY).

    Returns:
        A RoutingDecision carrying the chosen tier and the reasons for it.
    """
    available = dict(available or {})
    busy = dict(busy or {})
    if not isinstance(payload, Mapping):
        payload = {}

    f = extract_features(payload)
    tier, reasons = _semantic_tier(f)
    tier, tight, no_fit = _fitting_tier(f, tier, reasons)

    # Latency preference shifts one step, never past the context constraint.
    if not f.explicit_tier and not no_fit:
        if f.latency_preference == "low" and tier is not Tier.MINI:
            cheaper = cheaper_alternatives(tier)
            if cheaper and _fit(f, cheaper[0])[0]:
                reasons.append(f"latency=low: stepping down {tier.value} -> {cheaper[0].value}")
                tier = cheaper[0]
        elif f.latency_preference == "quality" and tier is Tier.MINI:
            up = Tier.CODE
            if _fit(f, up)[0]:
                reasons.append(f"latency=quality: stepping up {tier.value} -> {up.value}")
                tier = up

    # Vision is a capability, not a tier: land on a tier whose model accepts
    # images, but NEVER at the cost of the context constraint.
    if f.has_images and not TIERS[tier].vision:
        vision_tiers = [t for t in ROUTABLE if TIERS[t].vision and _fit(f, t)[0]]
        if vision_tiers:
            at_or_below = [t for t in vision_tiers if TIERS[t].cost_rank <= TIERS[tier].cost_rank]
            chosen = max(at_or_below or vision_tiers, key=lambda t: TIERS[t].cost_rank)
            reasons.append(f"image input present and {tier.value} has no vision: -> {chosen.value}")
            tier = chosen
        else:
            reasons.append(
                f"image input present but no vision-capable tier can hold context "
                f"{f.total_context_needed}: staying on {tier.value} (context fit wins)"
            )

    original = tier

    def _usable(t: Tier) -> bool:
        return available.get(t, True) and not busy.get(t, False)

    if not _usable(tier):
        coding = f.intent in (INTENT_ACTION, INTENT_CONTINUATION) or f.coding_toolset or tier in (Tier.CODE, Tier.MAX)
        if coding and tier in (Tier.CODE, Tier.FAST, Tier.REASON, Tier.MAX):
            reasons.append(
                f"{tier.value} busy/unavailable: queue on gx-code; do not downgrade substantial coding to gx-mini"
            )
            if tier is not Tier.MAX:
                tier = Tier.CODE
        else:
            candidates = [
                c for c in cheaper_alternatives(tier)
                if _usable(c) and not (f.has_images and not TIERS[c].vision)
            ]
            full = [c for c in candidates if _fit(f, c)[0]]
            partial = [c for c in candidates if _fit(f, c)[1]]
            if full:
                chosen, tight = full[0], False
            elif partial:
                chosen, tight = max(partial, key=lambda t: TIERS[t].max_context), True
            else:
                chosen = None
            if chosen is not None:
                reasons.append(
                    f"{tier.value} unavailable/busy: falling back to {chosen.value}"
                    + (" (tight: the engine's exact count decides)" if tight else "")
                )
                tier, no_fit = chosen, False
            else:
                reasons.append(f"{tier.value} unavailable/busy and no cheaper tier fits; staying on {tier.value}")

    return RoutingDecision(
        tier=tier,
        features=f,
        reasons=tuple(reasons),
        downgraded_from=original if tier is not original else None,
        tight_fit=tight,
        no_fit=no_fit,
    )
