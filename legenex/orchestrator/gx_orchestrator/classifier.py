"""Deterministic task classifier for the `gx-auto` alias.

Design intent (see ARCHITECTURE.md §6): gx-auto is deterministic. No
embeddings, no ML, no network calls. Every decision is explainable from the
request alone, and every decision is logged with the reasons that produced it.

The classifier is a pure function of the request plus a snapshot of cluster
availability, which makes it fully unit-testable with no running cluster.

TASK INTENT vs. REQUEST SIZE (D-030)
------------------------------------
Agentic coding clients (Kilo Code, Cline, Roo) send every turn with a large
system prompt, a large `tools` array, a large `max_tokens`, and a user turn
wrapped in an envelope::

    <task>are you there?</task>
    <environment_details> ...hundreds of lines of file listings... </environment_details>

The first gx-auto version scored that whole envelope, counted the tool schema
and the output budget as "complexity", and applied a tool floor to anything
carrying tools. A two-word presence check was sent to gx-fast (or gx-reason)
and waited minutes for a cold start. The rules now are:

* Complexity is scored on the TASK TEXT only: envelopes are stripped and
  `<task>` / `<user_message>` / `<feedback>` / `<answer>` are extracted.
* Tool schemas, the system prompt and the output budget count towards
  CONTEXT FIT only. They never raise complexity.
* The tool floor (gx-mini -> gx-fast) applies only to ACTIONABLE intent:
  real coding/repo work, or an agent loop that is already in progress.
  Conversational turns and capability questions stay on gx-mini whatever the
  size of the attached schema.
* A tool-result turn in an agent loop is routed by the conversation's
  ORIGINAL task, so one Kilo task stays on one tier instead of flapping.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
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


def _json_len(value: Any) -> int:
    try:
        return len(json.dumps(value, separators=(",", ":"), default=str))
    except (TypeError, ValueError):
        return 0


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
#: task, and they are full of words ("api", "test", "debug.log") that used to
#: be scored as complexity.
_ENVELOPE_BLOCKS = re.compile(
    r"<(environment_details|system-reminder|file_content|folder_content|"
    r"custom_instructions|explicit_instructions|slash_command|"
    r"workspace_configuration|user_instructions|context)\b[^>]*>.*?</\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
#: An unterminated envelope (clients sometimes truncate) swallows the rest.
_ENVELOPE_OPEN_TAIL = re.compile(
    r"<(environment_details|file_content|folder_content)\b[^>]*>.*\Z",
    re.IGNORECASE | re.DOTALL,
)
_TASK_TAGS = re.compile(
    r"<(task|user_message|feedback|answer)>\s*(.*?)\s*</\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
#: Cline/Roo/Kilo XML-protocol tool results come back as user turns.
_TOOL_RESULT_TEXT = re.compile(
    r"^\s*(\[[^\]\n]{1,160}\]\s*Result:|<tool_result\b|Tool result:|"
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


def _extract_task(messages: Sequence[Mapping[str, Any]]) -> tuple[str, bool]:
    """Return (task_text, continuation).

    `continuation` is True when the newest turn is a tool result inside an
    agent loop; the task is then the conversation's first real instruction.
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

    last_task = ""
    if last_user_idx is not None and not continuation:
        content = valid[last_user_idx].get("content")
        raw = "\n".join(_iter_text_parts(content))
        tagged = bool(_TASK_TAGS.search(raw))
        last_task = _task_from_text(raw)
        if _has_tool_result_parts(content) or (not tagged and _TOOL_RESULT_TEXT.search(raw)):
            continuation = True
        elif not last_task:
            # Nothing but an envelope. If the assistant was mid-loop (it made
            # tool calls), this is the loop continuing; otherwise it is empty.
            prev = valid[last_user_idx - 1] if last_user_idx > 0 else {}
            if isinstance(prev, Mapping) and prev.get("tool_calls"):
                continuation = True

    if not continuation:
        return last_task, False

    # Agent loop: route by the original task, the first user instruction.
    for msg in valid:
        if msg.get("role") != "user":
            continue
        raw = "\n".join(_iter_text_parts(msg.get("content")))
        if _TOOL_RESULT_TEXT.search(raw) and not _TASK_TAGS.search(raw):
            continue
        task = _task_from_text(raw)
        if task:
            return task, True
    return "", True


# --------------------------------------------------------------------------
# Complexity signals
# --------------------------------------------------------------------------

# Each pattern contributes to a complexity score. Patterns are word-boundary
# anchored and case-insensitive. Weights are deliberately coarse: the point is
# a stable, explainable ordering, not a calibrated probability.
_REASONING_PATTERNS: tuple[tuple[str, int], ...] = (
    (r"\bprove\b|\bproof\b|\btheorem\b|\blemma\b", 3),
    (r"\bderive\b|\bderivation\b", 3),
    (r"\boptimi[sz]e\b.*\b(algorithm|complexity|performance)\b", 3),
    (r"\btime complexity\b|\bbig-?o\b|\basymptotic\b", 3),
    (r"\b(design|architect)\w*\b.*\b(architecture|system|schema|protocol|service)\b", 2),
    (r"\barchitecture\b.*\b(review|decision|trade-?offs?|proposal)\b", 2),
    (r"\brefactor\b|\bredesign\b", 2),
    (r"\bdebug\w*\b|\broot[- ]cause\b|\bstack ?trace\b|\btraceback\b", 2),
    (r"\brace condition\b|\bdeadlock\b|\bmemory leak\b|\bheisenbug\b|\bconcurren(cy|t)\b.*\b(bug|issue|problem)\b", 2),
    (r"\bintermittent\w*\b|\bflaky\b|\bnon-?deterministic\b", 1),
    (r"\bstep by step\b|\bthink (it )?through\b|\breason(ing)? about\b", 2),
    (r"\bwhy does\b|\bexplain why\b", 1),
    (r"\bimplement\b|\bwrite a (program|function|class|script)\b", 1),
    (r"\bmathematical\b|\bequation\b|\bintegral\b|\bprobability\b|\bcombinatori\w*\b", 2),
    (r"\bcounter-?example\b|\binvariant\b|\bcorrectness\b", 1),
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
    # Both "exhaustive" and "comprehensive" must be paired with a qualifying
    # noun. Keep the qualifier grouped across BOTH alternatives (do not split
    # this into `\bexhaustiv|\bcomprehensive\b.*\b(...)\b`) -- that used to let
    # a bare "exhaustive" (e.g. "give me an exhaustive list of ...") reach
    # HARD_SCORE_MAX on its own, which is exactly the single-keyword
    # gx-max-by-accident that D-005 forbids. See tests/test_classifier.py
    # TestHardCategoryFalsePositives.
    (r"\b(exhaustiv\w*|comprehensive)\b.*\b(analysis|review|audit)\b", 3),
    (r"\bformal (verification|proof|spec)", 4),
    (r"\bnovel\b.*\b(algorithm|approach|method)\b", 3),
)

_ALL_PATTERNS: tuple[tuple[str, int, str], ...] = tuple(
    [(p, w, "reasoning") for p, w in _REASONING_PATTERNS]
    + [(p, w, "tool") for p, w in _TOOL_PATTERNS]
    + [(p, w, "trivial") for p, w in _TRIVIAL_PATTERNS]
    + [(p, w, "hard") for p, w in _HARD_PATTERNS]
)

_COMPILED = tuple((re.compile(p, re.IGNORECASE | re.DOTALL), w, kind) for p, w, kind in _ALL_PATTERNS)

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
    r"\bhow are you\b|\bintroduce yourself\b|\bwhat (are|is) your (capabilities|skills)\b",
    re.IGNORECASE,
)
#: Imperative coding / repository work.
_ACTION_VERB = re.compile(
    r"\b(fix|implement|add|create|write|build|make|edit|modify|update|change|refactor|"
    r"rename|remove|delete|replace|migrate|convert|port|install|configure|set ?up|"
    r"deploy|run|execute|test|debug|patch|bump|upgrade|generate|scaffold|document|"
    r"lint|format|review|optimi[sz]e|rewrite|extend|wire|hook up|integrate|commit|"
    r"investigate|diagnose|troubleshoot|resolve|read|open|inspect|analy[sz]e|explain)\b",
    re.IGNORECASE,
)
_CODE_OBJECT = re.compile(
    r"\b(file|files|function|functions|method|class|classes|module|component|tests?|"
    r"unit tests?|bug|bugs|error|errors|exception|issue|endpoint|api|script|config|"
    r"configuration|repo|repository|code|codebase|project|package|dependenc(y|ies)|"
    r"feature|page|route|schema|migration|query|dockerfile|compose|readme|docs?|"
    r"ui|frontend|backend|css|html|workflow|pipeline|build|lint|linter|type ?errors?|"
    r"server|service|database|table|handler|controller|hook|struct|interface|crate|"
    r"library|cli|commit|branch|pr|pull request|diff|log|logs)\b",
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
    r"execute|command|shell|bash|terminal|run_|read_file|list_files|search_files|codebase)",
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
    intent: str = INTENT_SIMPLE
    task_tokens: int = 0
    tool_schema_tokens: int = 0
    tool_count: int = 0
    coding_toolset: bool = False


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
            "intent": f.intent,
            "prompt_tokens": f.prompt_tokens,
            "task_tokens": f.task_tokens,
            "tool_schema_tokens": f.tool_schema_tokens,
            "tool_count": f.tool_count,
            "coding_toolset": f.coding_toolset,
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

#: Agent clients send `max_tokens` equal to the model's whole output window on
#: every turn. For CONTEXT PLANNING only, the requested output is capped here;
#: the server clamps the forwarded value to the chosen tier's real output
#: limit, so this cap can never cause a context overflow.
OUTPUT_PLANNING_CAP = 16_384

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
    raw_messages = payload.get("messages") if isinstance(payload, Mapping) else None
    messages: Sequence[Mapping[str, Any]] = [
        m for m in (raw_messages if isinstance(raw_messages, list) else []) if isinstance(m, Mapping)
    ]

    texts: list[str] = []
    has_images = False
    for msg in messages:
        content = msg.get("content")
        texts.extend(_iter_text_parts(content))
        has_images = has_images or _has_image(content)
        if msg.get("tool_calls"):
            texts.append(json.dumps(msg.get("tool_calls"), default=str))

    tools_len = _json_len(payload.get("tools")) if payload.get("tools") else 0
    tools_len += _json_len(payload.get("functions")) if payload.get("functions") else 0
    tool_schema_tokens = int(tools_len / _CHARS_PER_TOKEN) + (1 if tools_len else 0)
    prompt_tokens = estimate_tokens("\n".join(texts)) + tool_schema_tokens

    task, continuation = _extract_task(messages)
    intent = _classify_intent(task, continuation)
    names = _tool_names(payload)
    coding_toolset = any(_CODING_TOOL_NAME.search(n) for n in names)

    by_kind: dict[str, int] = {"reasoning": 0, "tool": 0, "trivial": 0, "hard": 0}
    signals: list[str] = [f"intent:{intent}"]
    for rx, weight, kind in _COMPILED:
        if rx.search(task):
            by_kind[kind] += weight
            signals.append(f"{kind}:{rx.pattern[:40]}:{weight:+d}")
    score = sum(by_kind.values())

    task_tokens = estimate_tokens(task)
    if task_tokens > 2_000:
        score += 2
        signals.append("length:long_instruction:+2")
    elif task_tokens < 30 and intent not in (INTENT_ACTION, INTENT_CONTINUATION):
        score -= 1
        signals.append("length:very_short:-1")

    if intent in (INTENT_ACTION, INTENT_CONTINUATION):
        # Real work: at least the primary coding tier.
        if score < COMPLEXITY_FAST:
            signals.append(f"intent:{intent}:floor->{COMPLEXITY_FAST}")
            score = COMPLEXITY_FAST
    elif intent == INTENT_CONVERSATIONAL:
        # A presence check or capability question is never "complex", however
        # many reasoning-looking words a capability question happens to use.
        if score > 0:
            signals.append("intent:conversational:cap->0")
            score = 0

    raw_max = payload.get("max_tokens") or payload.get("max_completion_tokens") or 1024
    try:
        requested_max = max(1, int(raw_max))
    except (TypeError, ValueError):
        requested_max = 1024
    has_tools = bool(payload.get("tools") or payload.get("functions"))
    if requested_max > 8_000 and not has_tools and intent != INTENT_CONVERSATIONAL:
        # A client with no tools asking for a very long answer is a weak hint of
        # a longer task. Agent clients always ask for their full window, so the
        # hint is ignored whenever tools are attached.
        score += 1
        signals.append("output:large_max_tokens:+1")
    if has_tools:
        signals.append(f"tools:{len(names)}:schema_tokens={tool_schema_tokens}:context_only")

    latency = str(
        ((payload.get("metadata") or {}) if isinstance(payload.get("metadata"), Mapping) else {}).get("latency")
        or payload.get("gx_latency")
        or "balanced"
    ).lower()
    if latency not in {"low", "balanced", "quality"}:
        latency = "balanced"

    planned_output = min(requested_max, OUTPUT_PLANNING_CAP)
    return RequestFeatures(
        reasoning_score=by_kind["reasoning"],
        hard_score=by_kind["hard"],
        prompt_tokens=prompt_tokens,
        requested_max_tokens=requested_max,
        total_context_needed=prompt_tokens + planned_output,
        has_images=has_images,
        has_tools=has_tools,
        complexity_score=score,
        latency_preference=latency,
        signals=tuple(signals),
        intent=intent,
        task_tokens=task_tokens,
        tool_schema_tokens=tool_schema_tokens,
        tool_count=len(names),
        coding_toolset=coding_toolset,
    )


# --------------------------------------------------------------------------
# Routing
# --------------------------------------------------------------------------


def _base_tier(f: RequestFeatures) -> tuple[Tier, list[str]]:
    """Pick a tier from the request features alone, ignoring availability."""
    reasons: list[str] = [f"intent={f.intent}"]

    # Hard constraint first: context must fit.
    if f.total_context_needed > LARGE_CONTEXT_THRESHOLD:
        reasons.append(
            f"context {f.total_context_needed} > {LARGE_CONTEXT_THRESHOLD} "
            "(largest single-node window): only gx-max fits"
        )
        return Tier.MAX, reasons

    if f.hard_score >= HARD_SCORE_MAX and f.intent != INTENT_CONVERSATIONAL:
        reasons.append(
            f"hard-category score {f.hard_score} >= {HARD_SCORE_MAX}: explicitly extreme task"
        )
        tier = Tier.MAX
    elif f.complexity_score >= COMPLEXITY_REASON:
        reasons.append(f"complexity {f.complexity_score} >= {COMPLEXITY_REASON}: hard reasoning/coding")
        tier = Tier.REASON
    elif f.complexity_score >= COMPLEXITY_FAST:
        reasons.append(f"complexity {f.complexity_score} >= {COMPLEXITY_FAST}: normal coding/agentic work")
        tier = Tier.FAST
    else:
        reasons.append(f"complexity {f.complexity_score} < {COMPLEXITY_FAST}: simple/conversational task")
        tier = Tier.MINI

    # Context nudges up to the cheapest tier that can hold the request.
    if TIERS[tier].max_context < f.total_context_needed:
        reasons.append(
            f"context {f.total_context_needed} exceeds {tier.value} window "
            f"{TIERS[tier].max_context}; escalating"
        )
        for candidate in ROUTABLE:
            if TIERS[candidate].cost_rank > TIERS[tier].cost_rank and TIERS[candidate].max_context >= f.total_context_needed:
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

    # Tool floor: only ACTIONABLE work that carries tools is lifted off
    # gx-mini. The size of the schema is irrelevant; a presence check with
    # Kilo's whole toolbox attached is still a presence check.
    if f.has_tools and tier is Tier.MINI:
        if f.intent in (INTENT_ACTION, INTENT_CONTINUATION):
            reasons.append(f"tools + {f.intent} intent: raising gx-mini -> gx-fast (tool floor)")
            tier = Tier.FAST
        else:
            reasons.append(
                f"tools attached ({f.tool_count}, ~{f.tool_schema_tokens} tokens) but intent is "
                f"{f.intent}: schema size does not raise the tier"
            )

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
    if not isinstance(payload, Mapping):
        payload = {}

    f = extract_features(payload)
    tier, reasons = _base_tier(f)

    # Vision is a capability, not a tier: if the request carries images we must
    # land on a tier whose model actually accepts them -- but NEVER at the cost
    # of the context constraint above. A vision tier that would truncate the
    # prompt is worse than a non-vision tier that holds all of it.
    if f.has_images and not TIERS[tier].vision:
        vision_tiers = [
            t
            for t in ROUTABLE
            if TIERS[t].vision and TIERS[t].max_context >= f.total_context_needed
        ]
        if vision_tiers:
            # Prefer the most capable vision tier at or below the chosen cost.
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
