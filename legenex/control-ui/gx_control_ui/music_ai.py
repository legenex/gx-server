"""Build with AI / Improve My Prompt / lyric writing for gx-music (MUS, build V3).

gx-auto is called server-side through the cluster's own LiteLLM gateway with
the gateway credential the Control Center already holds (never the browser).
Every answer must match a strict JSON schema (``response_format`` =
``json_schema``, ``strict``) and is then validated again here: types, ranges,
key names, lyric structure, the 512-character caption budget and the vocal
rules. A bad answer is retried (at most twice, with exponential back-off) with
the validation problems fed back; nothing is parsed out of free text.

Merge rules (``merge_improvement``), in order:

1. a locked field is never changed;
2. an empty field is filled from the proposal;
3. style tags are a union: the user's tags stay (in their order), new tags are
   appended, tags the proposal dropped are only *suggested* for removal;
4. description and style prompt are refined (replaced) - that is what Improve
   is for - and the change lists before/after so the user can undo it;
5. lyrics are only rewritten when the user asked for it;
6. BPM, key, time signature, duration, vocal language and planner settings the
   user set are kept; a different proposal is listed as a suggestion;
7. Instrumental and an explicit vocal type are never flipped; the seed is
   never changed.

``apply_build`` (Build with AI) replaces every unlocked field, keeps the seed
unless the proposal gives one, and keeps planner settings the proposal leaves
empty.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable
from typing import Any

from .util import HTTPError, http

log = logging.getLogger("gx.ui.music_ai")

MODEL = "gx-auto"
CAPTION_MAX = 512
DESCRIPTION_MAX = 512
LYRICS_MAX = 4096
TITLE_MAX = 120
TAG_MAX = 48
TAGS_MAX = 24
PROMPT_MAX = 1200
ATTEMPTS = 3
VOCAL_INTENTS = ("auto", "female", "male", "duet", "choir", "rap", "spoken")
VOCAL_WORDS = {"female": "female vocals", "male": "male vocals", "duet": "male and female vocal duet",
               "choir": "choir vocals", "rap": "rap vocals", "spoken": "spoken word vocals"}
LANGUAGES = (
    "ar", "az", "bg", "bn", "ca", "cs", "da", "de", "el", "en", "es", "fa", "fi", "fr", "he", "hi", "hr", "ht",
    "hu", "id", "is", "it", "ja", "ko", "la", "lt", "ms", "ne", "nl", "no", "pa", "pl", "pt", "ro", "ru", "sa",
    "sk", "sr", "sv", "sw", "ta", "te", "th", "tl", "tr", "uk", "ur", "vi", "yue", "zh",
)
TIME_SIGNATURES = ("2/4", "3/4", "4/4", "6/8")
SECTION_RE = re.compile(r"^\s*\[[^\]\n]{1,60}\]\s*$")
KEY_RE = re.compile(r"^\s*([A-Ga-g])\s*([#b♯♭]?)\s*(maj(?:or)?|min(?:or)?|m)?\s*$", re.IGNORECASE)
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

#: every field the Music form (and a flow node) carries, in form order
FORM_FIELDS = ("title", "description", "style_tags", "style_prompt", "instrumental", "vocal_intent",
               "vocal_language", "lyrics", "bpm", "key", "time_signature", "duration", "seed", "thinking",
               "inference_steps", "infer_method", "lm_temperature")
TEXT_FIELDS = ("description", "style_prompt")
META_FIELDS = ("bpm", "key", "time_signature", "duration", "vocal_language")
PLANNER_FIELDS = ("thinking", "inference_steps", "infer_method", "lm_temperature")


class MusicAIError(Exception):
    def __init__(self, message: str, status: int = 502, code: str = "ai_failed") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


# ------------------------------------------------------------------ schema
def _nullable(kind: str) -> dict:
    return {"type": [kind, "null"]}


SETTINGS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "description", "style_tags", "style_prompt", "instrumental", "vocal_intent",
                 "vocal_language", "lyrics", "bpm", "key", "time_signature", "duration", "seed", "thinking",
                 "inference_steps", "infer_method", "lm_temperature", "notes"],
    "properties": {
        "title": {"type": "string"},
        "description": {"type": "string"},
        "style_tags": {"type": "array", "items": {"type": "string"}},
        "style_prompt": {"type": "string"},
        "instrumental": {"type": "boolean"},
        "vocal_intent": {"type": "string", "enum": list(VOCAL_INTENTS)},
        "vocal_language": {"type": "string", "enum": ["", *LANGUAGES]},
        "lyrics": {"type": "string"},
        "bpm": _nullable("integer"),
        "key": _nullable("string"),
        "time_signature": {"type": ["string", "null"], "enum": [*TIME_SIGNATURES, None]},
        "duration": _nullable("number"),
        "seed": _nullable("integer"),
        "thinking": _nullable("boolean"),
        "inference_steps": _nullable("integer"),
        "infer_method": {"type": ["string", "null"], "enum": ["ode", "sde", None]},
        "lm_temperature": _nullable("number"),
        "notes": {"type": "string"},
    },
}

LYRICS_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False, "required": ["lyrics"],
    "properties": {"lyrics": {"type": "string"}},
}

GUIDE = """You are the music director for ACE-Step 1.5 (XL turbo DiT + 5 Hz planner LM).
ACE-Step is conditioned ONLY on: a caption (style prompt + style tags, max 512 characters in total),
a lyric block, and metadata (BPM 30-300, key such as "F# minor", time signature 2/4 3/4 4/4 6/8,
duration 10-600 s, vocal language). Fill the fields like this:
- description: WHAT the song is (theme, story, purpose, emotion), one or two sentences.
- style_tags: 4-10 short descriptors, each 1-4 words: genre, sub-genre, mood, key instruments,
  vocal type (e.g. "female vocals", "breathy vocal"), era, production. Lower case. No artist names.
- style_prompt: HOW it sounds, a studio brief of 15-45 words: instrumentation, vocal timbre and
  delivery, arrangement and build, groove, mix/production. No artist or song names.
- ACE-Step has no negative prompt. Turn "no X" into positive wording (e.g. "no cheesy EDM drop"
  -> "restrained build, organic groove, no big drop" is wrong; write "restrained build, organic groove").
- Keep caption and lyrics consistent; avoid conflicting styles.
- instrumental=true means no vocals: lyrics "", vocal_intent "auto", vocal_language "".
- A vocal song needs lyrics to be sung: vocal_intent one of female, male, duet, choir, rap,
  spoken (or auto), vocal_language set (e.g. "en").
- lyrics use section tags on their own lines: [Intro] [Verse] [Pre-Chorus] [Chorus] [Bridge]
  [Outro] (optionally "[Chorus - anthemic]"); 6-10 syllables per line; about 4 lines per
  verse/chorus; fit the duration (roughly 30-40 sung lines for 3 minutes). Original words only.
- bpm/key/time_signature/duration: concrete values when the request implies them, otherwise
  sensible ones for the genre. seed: null unless the user asked for a specific seed.
- thinking (planner) true unless asked otherwise; inference_steps null or 8; infer_method null,
  "ode" (steady) or "sde" (more variety); lm_temperature null or 0.6-1.1.
- notes: one or two sentences explaining the choices.
Reply with the JSON object only."""


# -------------------------------------------------------------- gateway
class GatewayChat:
    """gx-auto through the local LiteLLM gateway (trusted, fixed URL; not netguard)."""

    def __init__(self, base: str, headers: Callable[[], dict[str, str]], *, model: str = MODEL,
                 timeout: float = 240.0) -> None:
        self.base = base.rstrip("/")
        self.headers = headers
        self.model = model
        self.timeout = timeout

    def complete(self, messages: list[dict], *, schema_name: str, schema: dict, max_tokens: int,
                 temperature: float) -> dict:
        body = {"model": self.model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature,
                "response_format": {"type": "json_schema",
                                    "json_schema": {"name": schema_name, "strict": True, "schema": schema}}}
        try:
            res = http("POST", f"{self.base}/v1/chat/completions", body=body, headers=self.headers(),
                       timeout=self.timeout)
        except HTTPError as exc:
            raise MusicAIError("gx-auto is not reachable through the gateway", 503, "gateway_unavailable") \
                from exc
        try:
            data = res.json()
        except ValueError:
            data = None
        if res.status in (429, 500, 502, 503, 504):
            raise MusicAIError(f"the gateway answered HTTP {res.status}", 503, "gateway_busy")
        if res.status != 200 or not isinstance(data, dict):
            raise MusicAIError(f"the gateway refused the request (HTTP {res.status})", 502, "gateway_error")
        choice = (data.get("choices") or [{}])[0]
        content = ((choice.get("message") or {}).get("content") or "").strip()
        return {"content": content, "model": data.get("model"), "usage": data.get("usage") or {},
                "finish_reason": choice.get("finish_reason")}


# ----------------------------------------------------------- validation
def normalize_key(raw: Any) -> str | None:
    if raw in (None, ""):
        return None
    if not isinstance(raw, str):
        raise ValueError("key must be text such as 'F# minor'")
    m = KEY_RE.match(raw)
    if not m:
        raise ValueError(f"key '{raw[:20]}' is not like 'C major' or 'F# minor'")
    acc = {"": "", "#": "#", "b": "b", "♯": "#", "♭": "b"}[m.group(2)]
    scale = "minor" if (m.group(3) or "").lower() in ("m", "min", "minor") else "major"
    return f"{m.group(1).upper()}{acc} {scale}"


def lyric_has_words(text: str) -> bool:
    return any(line.strip() and not SECTION_RE.match(line) for line in (text or "").splitlines())


def lyric_has_sections(text: str) -> bool:
    return any(SECTION_RE.match(line) for line in (text or "").splitlines())


def mentions_vocals(text: str) -> bool:
    cleaned = re.sub(r"\b(no|without|non)[ -]+(\w+[ -]+)?(vocals?|voices?|singing|lyrics)\b", " ", text or "",
                     flags=re.IGNORECASE)
    return bool(re.search(r"\b(vocals?|singers?|singing|voices?|rap|choir|duet|spoken word)\b", cleaned,
                          re.IGNORECASE))


def clean_text(value: Any, limit: int, name: str, problems: list[str]) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        problems.append(f"{name} must be a string")
        return ""
    value = CONTROL_RE.sub("", value).replace("\r\n", "\n").strip()
    if len(value) > limit:
        problems.append(f"{name} is {len(value)} characters; the maximum is {limit}")
    return value


def clean_tags(value: Any, problems: list[str]) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        problems.append("style_tags must be a list of strings")
        return []
    out: list[str] = []
    seen: set[str] = set()
    for tag in value:
        if not isinstance(tag, str):
            problems.append("every style tag must be a string")
            continue
        tag = re.sub(r"\s+", " ", CONTROL_RE.sub("", tag).replace(",", " ")).strip()
        if not tag:
            continue
        if len(tag) > TAG_MAX:
            problems.append(f"style tag '{tag[:20]}…' is longer than {TAG_MAX} characters")
            continue
        if tag.lower() not in seen:
            seen.add(tag.lower())
            out.append(tag)
    if len(out) > TAGS_MAX:
        problems.append(f"at most {TAGS_MAX} style tags")
        out = out[:TAGS_MAX]
    return out


def _number(value: Any, name: str, lo: float, hi: float, problems: list[str], *, integer: bool) -> Any:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        problems.append(f"{name} must be a number")
        return None
    if integer and not float(value).is_integer():
        problems.append(f"{name} must be a whole number")
        return None
    if not lo <= value <= hi:
        problems.append(f"{name} must be between {lo:g} and {hi:g}")
        return None
    return int(value) if integer else round(float(value), 3)


def caption_length(style_prompt: str, tags: list[str]) -> int:
    parts = [style_prompt.rstrip(",;. ")] if style_prompt else []
    low = style_prompt.lower()
    parts += [t for t in tags if t.lower() not in low]
    return len(", ".join(parts))


def validate_settings(obj: Any, *, require_lyrics: bool = False, strict: bool = True) -> tuple[dict, list[str]]:
    """Proposal or form -> (normalised settings, problems).

    ``strict`` (answers from the model) reports everything; the form
    normaliser (strict=False) raises on type errors only.
    """
    problems: list[str] = []
    if not isinstance(obj, dict):
        return {}, ["the answer is not a JSON object"]
    s: dict[str, Any] = {}
    s["title"] = clean_text(obj.get("title"), TITLE_MAX, "title", problems)
    s["description"] = clean_text(obj.get("description"), DESCRIPTION_MAX, "description", problems)
    s["style_tags"] = clean_tags(obj.get("style_tags"), problems)
    s["style_prompt"] = clean_text(obj.get("style_prompt"), CAPTION_MAX, "style_prompt", problems)
    inst = obj.get("instrumental", False)
    if not isinstance(inst, bool):
        problems.append("instrumental must be true or false")
        inst = False
    s["instrumental"] = inst
    intent = obj.get("vocal_intent") or "auto"
    if intent not in VOCAL_INTENTS:
        problems.append(f"vocal_intent must be one of {', '.join(VOCAL_INTENTS)}")
        intent = "auto"
    lang = obj.get("vocal_language") or ""
    if lang not in ("", *LANGUAGES):
        problems.append("vocal_language must be a supported language code such as 'en'")
        lang = ""
    s["lyrics"] = clean_text(obj.get("lyrics"), LYRICS_MAX, "lyrics", problems)
    s["bpm"] = _number(obj.get("bpm"), "bpm", 30, 300, problems, integer=True)
    try:
        s["key"] = normalize_key(obj.get("key"))
    except ValueError as exc:
        problems.append(str(exc))
        s["key"] = None
    ts = obj.get("time_signature")
    ts = {"2": "2/4", "3": "3/4", "4": "4/4", "6": "6/8"}.get(str(ts), ts) if ts not in (None, "") else None
    if ts is not None and ts not in TIME_SIGNATURES:
        problems.append("time_signature must be 2/4, 3/4, 4/4 or 6/8")
        ts = None
    s["time_signature"] = ts
    s["duration"] = _number(obj.get("duration"), "duration", 10, 600, problems, integer=False)
    s["seed"] = _number(obj.get("seed"), "seed", 0, 2**31 - 1, problems, integer=True)
    thinking = obj.get("thinking")
    if thinking is not None and not isinstance(thinking, bool):
        problems.append("thinking must be true, false or null")
        thinking = None
    s["thinking"] = thinking
    s["inference_steps"] = _number(obj.get("inference_steps"), "inference_steps", 1, 20, problems, integer=True)
    method = obj.get("infer_method") or None
    if method not in (None, "ode", "sde"):
        problems.append("infer_method must be ode or sde")
        method = None
    s["infer_method"] = method
    s["lm_temperature"] = _number(obj.get("lm_temperature"), "lm_temperature", 0.0, 2.0, problems, integer=False)
    # vocal rules
    if inst:
        if intent != "auto":
            problems.append("an instrumental must have vocal_intent 'auto'")
        intent, lang = "auto", ""
        if strict and lyric_has_words(s["lyrics"]):
            problems.append("an instrumental must have empty lyrics")
        s["lyrics"] = ""
    else:
        if s["lyrics"]:
            if not lyric_has_words(s["lyrics"]):
                problems.append("lyrics contain only section tags; write lines to sing")
            elif strict and not lyric_has_sections(s["lyrics"]):
                problems.append("lyrics need section tags such as [Verse] and [Chorus] on their own lines")
        elif require_lyrics:
            problems.append("lyrics are required for this vocal song")
        if strict and s["lyrics"] and not lang:
            problems.append("a vocal song needs vocal_language (for example 'en')")
    s["vocal_intent"] = intent
    s["vocal_language"] = lang
    if caption_length(s["style_prompt"], s["style_tags"]) > CAPTION_MAX:
        problems.append(f"style_prompt plus style_tags must fit in {CAPTION_MAX} characters "
                        f"(it is {caption_length(s['style_prompt'], s['style_tags'])}); shorten them")
    if strict:
        if not s["style_prompt"] and not s["style_tags"]:
            problems.append("give a style_prompt and style_tags")
        s["notes"] = clean_text(obj.get("notes"), 600, "notes", problems)
    return s, problems


def normalize_form(current: Any) -> dict:
    """The browser's current form (or a flow node's fields) -> settings. Raises ValueError."""
    if current is None:
        current = {}
    if not isinstance(current, dict):
        raise ValueError("current settings must be an object")
    unknown = sorted(set(current) - set(FORM_FIELDS))
    if unknown:
        raise ValueError(f"unknown field(s): {', '.join(unknown[:6])}")
    settings, problems = validate_settings(current, strict=False)
    hard = [p for p in problems if "must be" in p or "unknown" in p or "maximum" in p or "at most" in p]
    if hard:
        raise ValueError(hard[0])
    return settings


def _repair(settings: dict, problems: list[str]) -> tuple[dict, list[str]]:
    """Last attempt: make a nearly valid answer usable, and say what was changed."""
    notes = []
    for field, limit in (("title", TITLE_MAX), ("description", DESCRIPTION_MAX), ("style_prompt", CAPTION_MAX)):
        if len(settings.get(field) or "") > limit:
            settings[field] = settings[field][:limit - 1].rsplit(" ", 1)[0] + "…"
            notes.append(f"{field} was shortened")
    while settings.get("style_tags") and caption_length(settings["style_prompt"], settings["style_tags"]) > CAPTION_MAX:
        dropped = settings["style_tags"].pop()
        notes.append(f"style tag '{dropped}' was dropped to fit the caption")
    left = [p for p in validate_settings(settings, strict=False)[1]
            if "maximum" in p or "fit in" in p or "section tags" in p]
    return settings, notes + [f"not repaired: {p}" for p in left]


# ------------------------------------------------------------------ merge
def _is_empty(field: str, value: Any) -> bool:
    if field in ("instrumental", "thinking"):
        return value is None
    if field == "vocal_intent":
        return value in (None, "", "auto")
    return value in (None, "", [])


def _change(changes: list, field: str, action: str, before: Any, after: Any, why: str = "") -> None:
    changes.append({"field": field, "action": action, "before": before, "after": after, "why": why})


def apply_build(current: dict, proposal: dict, locked: set[str]) -> tuple[dict, list[dict]]:
    merged = dict(current)
    changes: list[dict] = []
    for field in FORM_FIELDS:
        before, after = current.get(field), proposal.get(field)
        if field in locked:
            if after != before and not _is_empty(field, after):
                _change(changes, field, "kept_locked", before, after, "locked")
            continue
        if field == "seed" or field in PLANNER_FIELDS:
            if after is None:
                continue
        if after != before:
            merged[field] = after
            _change(changes, field, "set", before, after)
    _vocal_consistency(merged, changes, locked)
    return merged, changes


def merge_improvement(current: dict, proposal: dict, locked: set[str], *, improve_lyrics: bool = False) \
        -> tuple[dict, list[dict]]:
    merged = dict(current)
    changes: list[dict] = []
    for field in FORM_FIELDS:
        before, after = current.get(field), proposal.get(field)
        if after == before or (field != "style_tags" and _is_empty(field, after) and field != "lyrics"):
            continue
        if field in locked:
            _change(changes, field, "kept_locked", before, after, "locked")
            continue
        if field == "seed":
            continue
        if field == "style_tags":
            cur = list(before or [])
            low = {t.lower() for t in cur}
            added = [t for t in (after or []) if t.lower() not in low]
            room = TAGS_MAX - len(cur)
            if added[:room]:
                merged["style_tags"] = cur + added[:room]
                _change(changes, field, "added", cur, merged["style_tags"], ", ".join(added[:room]))
            new_low = {t.lower() for t in (after or [])}
            dropped = [t for t in cur if t.lower() not in new_low]
            if dropped:
                _change(changes, field, "suggested", None, None, "consider removing: " + ", ".join(dropped))
            continue
        if field == "instrumental":
            _change(changes, field, "suggested", before, after, "Instrumental is your choice; not changed")
            continue
        if field == "vocal_intent":
            if _is_empty(field, before) and not merged.get("instrumental"):
                merged[field] = after
                _change(changes, field, "filled", before, after)
            else:
                _change(changes, field, "suggested", before, after, "your vocal type was kept")
            continue
        if field == "lyrics":
            if merged.get("instrumental"):
                continue
            if _is_empty(field, before) and not _is_empty(field, after):
                merged[field] = after
                _change(changes, field, "filled", before, after)
            elif improve_lyrics and not _is_empty(field, after):
                merged[field] = after
                _change(changes, field, "refined", before, after)
            elif not _is_empty(field, after):
                _change(changes, field, "suggested", None, None, "lyric edits were not applied (not requested)")
            continue
        if field in TEXT_FIELDS or field == "title":
            if _is_empty(field, before):
                merged[field] = after
                _change(changes, field, "filled", before, after)
            elif field in TEXT_FIELDS:
                merged[field] = after
                _change(changes, field, "refined", before, after)
            continue
        # metadata and planner settings
        if _is_empty(field, before):
            merged[field] = after
            _change(changes, field, "filled", before, after)
        else:
            _change(changes, field, "suggested", before, after, "your value was kept")
    _vocal_consistency(merged, changes, locked)
    return merged, changes


def _vocal_consistency(merged: dict, changes: list[dict], locked: set[str]) -> None:
    if merged.get("instrumental"):
        if merged.get("vocal_intent") not in (None, "auto") and "vocal_intent" not in locked:
            _change(changes, "vocal_intent", "set", merged["vocal_intent"], "auto", "instrumental: no vocals")
            merged["vocal_intent"] = "auto"


def summarize(changes: list[dict]) -> str:
    counts: dict[str, int] = {}
    for c in changes:
        counts[c["action"]] = counts.get(c["action"], 0) + 1
    words = {"set": "set", "filled": "filled", "refined": "refined", "added": "tag update",
             "kept_locked": "kept (locked)", "suggested": "suggestion"}
    if not counts:
        return "No changes: the settings already match."
    return ", ".join(f"{n} {words.get(k, k)}{'s' if n > 1 and k in ('suggested', 'added') else ''}"
                     for k, n in counts.items())


# --------------------------------------------------------------- service
class MusicAI:
    RATE_PER_MIN = 10

    def __init__(self, chat: GatewayChat, *, audit: Callable[..., None] | None = None,
                 sleep: Callable[[float], None] = time.sleep, attempts: int = ATTEMPTS) -> None:
        self.chat = chat
        self.audit = audit or (lambda **kw: None)
        self.sleep = sleep
        self.attempts = max(1, min(3, attempts))
        self._lock = threading.Lock()
        self._busy: set[str] = set()
        self._hits: dict[str, list[float]] = {}

    # ------------------------------------------------------------ plumbing
    def _admit(self, user: str) -> None:
        now = time.time()
        with self._lock:
            hits = [t for t in self._hits.get(user, []) if now - t < 60]
            if user in self._busy:
                raise MusicAIError("an AI request is already running for you; wait for it to finish", 429,
                                   "busy")
            if len(hits) >= self.RATE_PER_MIN:
                raise MusicAIError(f"at most {self.RATE_PER_MIN} AI requests per minute", 429, "rate_limited")
            hits.append(now)
            self._hits[user] = hits
            self._busy.add(user)

    def _release(self, user: str) -> None:
        with self._lock:
            self._busy.discard(user)

    def _ask(self, messages: list[dict], *, schema_name: str, schema: dict, validate: Callable[[Any], tuple],
             max_tokens: int, temperature: float) -> tuple[dict, dict]:
        """Bounded retries: transport errors back off; invalid answers are fed back."""
        feedback: list[dict] = []
        last_problem = "no answer"
        for attempt in range(1, self.attempts + 1):
            try:
                reply = self.chat.complete(messages + feedback, schema_name=schema_name, schema=schema,
                                           max_tokens=max_tokens, temperature=temperature)
            except MusicAIError as exc:
                last_problem = str(exc)
                if exc.status == 503 and attempt < self.attempts:
                    self.sleep(0.5 * 2 ** (attempt - 1))
                    continue
                raise
            content = reply["content"]
            try:
                obj = json.loads(content)
            except ValueError:
                obj = None
                problems = ["the answer was not valid JSON"]
            else:
                value, problems = validate(obj)
                if not problems:
                    return value, {"attempts": attempt, "model": reply.get("model"), "usage": reply.get("usage")}
                if attempt == self.attempts and isinstance(value, dict) and value:
                    repaired, notes = _repair(value, problems)
                    if not [n for n in notes if n.startswith("not repaired")]:
                        repaired.setdefault("repair_notes", notes)
                        return repaired, {"attempts": attempt, "model": reply.get("model"),
                                          "usage": reply.get("usage"), "repaired": notes}
            last_problem = "; ".join(problems[:4])
            log.info("gx-auto answer rejected (attempt %d): %s", attempt, last_problem)
            feedback = [{"role": "assistant", "content": content[:6000]},
                        {"role": "user", "content": "That JSON is not usable: " + last_problem
                         + ". Reply again with the corrected JSON object only."}]
            if attempt < self.attempts:
                self.sleep(0.5 * 2 ** (attempt - 1))
        raise MusicAIError(f"gx-auto did not return usable settings after {self.attempts} attempts "
                           f"({last_problem})", 502, "ai_invalid")

    # ---------------------------------------------------------------- API
    def build(self, prompt: Any, current: Any, locked: Any, *, write_lyrics: bool, user: str) -> dict:
        problems: list[str] = []
        text = clean_text(prompt, PROMPT_MAX, "prompt", problems)
        if problems:
            raise MusicAIError(problems[0], 400, "invalid_request")
        if len(text) < 3:
            raise MusicAIError("describe the track you want", 400, "invalid_request")
        cur = normalize_form(current)
        lock = _locked(locked)
        context = {k: cur[k] for k in lock if k in cur}
        instr = (f"Request: {text}\n\nWrite lyrics: {'yes' if write_lyrics else 'no (leave lyrics empty)'}.\n"
                 + (f"These fields are fixed by the user; stay consistent with them: {json.dumps(context)}\n"
                    if context else ""))
        messages = [{"role": "system", "content": GUIDE}, {"role": "user", "content": instr}]
        wants_vocals = mentions_vocals(text)

        def check(obj: Any) -> tuple[dict, list[str]]:
            s, p = validate_settings(obj, require_lyrics=write_lyrics and not (obj or {}).get("instrumental"))
            if not write_lyrics:
                s["lyrics"] = "" if s.get("instrumental") else cur["lyrics"]
            if wants_vocals and s.get("instrumental") and not re.search(r"instrumental", text, re.I):
                p.append("the request asks for vocals, so instrumental must be false")
            return s, p

        self._admit(user)
        t0 = time.time()
        try:
            proposal, meta = self._ask(messages, schema_name="gx_music_settings", schema=SETTINGS_SCHEMA,
                                       validate=check, max_tokens=2600, temperature=0.6)
        finally:
            self._release(user)
        merged, changes = apply_build(cur, proposal, lock)
        self.audit(user=user, ip="", action="music.ai.build", outcome="ok", attempts=meta["attempts"])
        return {"settings": merged, "proposal": proposal, "changes": changes, "summary": summarize(changes),
                "notes": proposal.get("notes", ""), "repair_notes": proposal.get("repair_notes", []),
                "model": MODEL, "attempts": meta["attempts"], "elapsed_s": round(time.time() - t0, 2)}

    def improve(self, current: Any, locked: Any, *, improve_lyrics: bool, instruction: Any, user: str) -> dict:
        cur = normalize_form(current)
        lock = _locked(locked)
        problems: list[str] = []
        note = clean_text(instruction, 600, "instruction", problems)
        if problems:
            raise MusicAIError(problems[0], 400, "invalid_request")
        if not any(cur.get(k) for k in ("description", "style_prompt", "style_tags", "lyrics")):
            raise MusicAIError("there is nothing to improve yet: add a description, tags or a style prompt, "
                               "or use Build with AI", 400, "nothing_to_improve")
        payload = {k: cur[k] for k in FORM_FIELDS if k != "seed"}
        instr = ("Improve these settings for ACE-Step. Keep the user's intent, genre and choices; make the "
                 "description and style prompt more specific and musical; suggest extra style tags; fill empty "
                 "metadata sensibly. Do not change fields marked locked.\n"
                 f"Current settings: {json.dumps(payload)}\nLocked fields: {sorted(lock)}\n"
                 f"Rewrite lyrics: {'yes' if improve_lyrics else 'no, return them unchanged'}\n"
                 + (f"User note: {note}\n" if note else ""))
        messages = [{"role": "system", "content": GUIDE}, {"role": "user", "content": instr}]

        def check(obj: Any) -> tuple[dict, list[str]]:
            s, p = validate_settings(obj)
            if isinstance(obj, dict) and cur["instrumental"] is True:
                p = [x for x in p if "instrumental must have" not in x]
            return s, p

        self._admit(user)
        t0 = time.time()
        try:
            proposal, meta = self._ask(messages, schema_name="gx_music_settings", schema=SETTINGS_SCHEMA,
                                       validate=check, max_tokens=2600, temperature=0.4)
        finally:
            self._release(user)
        merged, changes = merge_improvement(cur, proposal, lock, improve_lyrics=improve_lyrics)
        self.audit(user=user, ip="", action="music.ai.improve", outcome="ok", attempts=meta["attempts"])
        return {"settings": merged, "proposal": proposal, "changes": changes, "summary": summarize(changes),
                "notes": proposal.get("notes", ""), "repair_notes": proposal.get("repair_notes", []),
                "model": MODEL, "attempts": meta["attempts"], "elapsed_s": round(time.time() - t0, 2)}

    def write_lyrics(self, request: dict, *, user: str) -> str:
        """Lyrics for a vocal request that has none (``lyrics_source: assistant``)."""
        style = ", ".join(x for x in [str(request.get("prompt") or ""), *[str(t) for t in
                                                                          request.get("style_tags") or []]] if x)
        intent = VOCAL_WORDS.get(str(request.get("vocal_intent") or ""), "")
        duration = request.get("duration")
        lang = request.get("vocal_language") or "en"
        brief = {"description": str(request.get("description") or "")[:DESCRIPTION_MAX], "style": style[:CAPTION_MAX],
                 "vocals": intent or "lead vocal", "language": lang,
                 "duration_seconds": duration if isinstance(duration, (int, float)) else "about 120"}
        if not brief["description"] and not brief["style"]:
            raise MusicAIError("Write with AI needs a song description or a style to write lyrics for", 400,
                               "invalid_request")
        messages = [{"role": "system", "content": GUIDE},
                    {"role": "user", "content": "Write original song lyrics only (field 'lyrics'), with section "
                     "tags on their own lines, in the language given, sized for the duration. Brief: "
                     + json.dumps(brief)}]

        def check(obj: Any) -> tuple[str, list[str]]:
            problems: list[str] = []
            text = clean_text((obj or {}).get("lyrics") if isinstance(obj, dict) else None, LYRICS_MAX, "lyrics",
                              problems)
            if not lyric_has_words(text):
                problems.append("lyrics must contain lines to sing")
            if not lyric_has_sections(text):
                problems.append("lyrics need section tags such as [Verse] and [Chorus]")
            return text, problems

        self._admit(user)
        try:
            lyrics, meta = self._ask(messages, schema_name="gx_music_lyrics", schema=LYRICS_SCHEMA, validate=check,
                                     max_tokens=1800, temperature=0.8)
        finally:
            self._release(user)
        self.audit(user=user, ip="", action="music.ai.lyrics", outcome="ok", attempts=meta["attempts"])
        return lyrics

    def suggest(self, facts: dict, *, hint: str, user: str) -> dict:
        """Settings from a reference analysis. ``facts`` is already labelled."""
        messages = [{"role": "system", "content": GUIDE},
                    {"role": "user", "content":
                     "Suggest ACE-Step settings for a NEW, original track inspired by this reference analysis. "
                     "Describe the style generically; never name the artist or song, never copy its lyrics, "
                     "and leave lyrics empty. Use measured tempo/key/time signature when their confidence is "
                     "reasonable. Facts: " + json.dumps(facts)[:6000]
                     + (f"\nUser hint: {hint}" if hint else "")}]

        def check(obj: Any) -> tuple[dict, list[str]]:
            s, p = validate_settings(obj)
            s["lyrics"] = ""
            p = [x for x in p if "vocal_language" not in x and "lyrics" not in x]
            return s, p

        self._admit(user)
        try:
            proposal, meta = self._ask(messages, schema_name="gx_music_settings", schema=SETTINGS_SCHEMA,
                                       validate=check, max_tokens=1800, temperature=0.4)
        finally:
            self._release(user)
        proposal["seed"] = None
        return {"settings": proposal, "attempts": meta["attempts"], "model": MODEL}


def _locked(value: Any) -> set[str]:
    if value is None:
        return set()
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise ValueError("locked must be a list of field names")
    bad = sorted(set(value) - set(FORM_FIELDS))
    if bad:
        raise ValueError(f"unknown locked field(s): {', '.join(bad[:6])}")
    return set(value)


def form_to_request(settings: dict) -> dict:
    """Settings (form names) -> a gx-music generation body (API names)."""
    body: dict[str, Any] = {}
    for src, dst in (("title", "title"), ("description", "description"), ("style_prompt", "prompt"),
                     ("lyrics", "lyrics"), ("bpm", "bpm"), ("key", "key"), ("duration", "duration"),
                     ("seed", "seed"), ("thinking", "thinking"), ("inference_steps", "inference_steps"),
                     ("infer_method", "infer_method"), ("lm_temperature", "lm_temperature")):
        if settings.get(src) not in (None, ""):
            body[dst] = settings[src]
    if settings.get("style_tags"):
        body["style_tags"] = list(settings["style_tags"])
    body["instrumental"] = bool(settings.get("instrumental"))
    if settings.get("vocal_intent") not in (None, "", "auto") and not body["instrumental"]:
        body["vocal_intent"] = settings["vocal_intent"]
    if settings.get("vocal_language") and not body["instrumental"]:
        body["vocal_language"] = settings["vocal_language"]
    if settings.get("time_signature"):
        body["time_signature"] = settings["time_signature"]
    return body
