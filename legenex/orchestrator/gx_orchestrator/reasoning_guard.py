"""Keep raw reasoning out of `content`, streamed and non-streamed (D-053).

Observed live on V4.1 (2026-10-16): with a longer coding prompt the engine's
reasoning parser did not engage, so the stream carried ``<think>We need
answer: ...`` at content offset 0, never closed within 1024 tokens, while
``reasoning`` stayed empty. Every client goes through the orchestrator, so the
guarantee lives here, for every deployment:

* text between ``<think>`` and ``</think>`` is moved to the reasoning field,
  an unclosed block included (it ends as reasoning, never as answer);
* when thinking was requested and the engine has sent NO reasoning yet, the
  first content is held (bounded) until a bare ``</think>`` proves it was
  reasoning, or the bound proves it was not;
* stray ``<think>`` / ``</think>`` tags never reach content;
* tags split across SSE chunks are reassembled (partial-tag tails are held);
* the first real answer token is never dropped: text after ``</think>`` in the
  same delta is content (the D-052 `DeltaSplitter` later separates such a
  mixed delta for LiteLLM).

Engines that already split reasoning correctly are untouched: once the engine
emits a reasoning delta, content is the answer and passes straight through
(only literal tags are removed).
"""
from __future__ import annotations

import json
from typing import Any

OPEN = "<think>"
CLOSE = "</think>"
_TAGS = (OPEN, CLOSE)
#: Keys vLLM uses for reasoning text (>= 0.11 "reasoning"; older "reasoning_content").
REASONING_KEYS = ("reasoning", "reasoning_content")
#: Upper bound on content held back while waiting for a bare </think>.
DEFAULT_HOLDBACK_CHARS = 2048
_SEP = b"\n\n"


def thinking_requested(chat_template_kwargs: "dict[str, Any] | None") -> bool:
    """True unless the injected kwargs switch thinking off (V4: thinking=false,
    V4.1: enable_thinking=false)."""
    kw = chat_template_kwargs or {}
    if kw.get("thinking") is False or kw.get("enable_thinking") is False:
        return False
    if "thinking" in kw:
        return bool(kw["thinking"])
    return True


def _partial_tag_suffix(text: str) -> int:
    """Length of the longest suffix of `text` that is a proper prefix of a tag."""
    best = 0
    for tag in _TAGS:
        for n in range(min(len(tag) - 1, len(text)), 0, -1):
            if text.endswith(tag[:n]):
                best = max(best, n)
                break
    return best


class ChoiceFilter:
    """Per-choice state machine: start -> think | content."""

    def __init__(self, *, thinking: bool, holdback_chars: int = DEFAULT_HOLDBACK_CHARS) -> None:
        self.state = "start"
        self.pending = ""
        self.thinking = thinking
        self.holdback = holdback_chars
        self.engine_reasoning_seen = False
        self.changed = False

    def note_engine_reasoning(self) -> None:
        self.engine_reasoning_seen = True

    def feed(self, text: str) -> tuple[str, str]:
        """(content, reasoning) to emit now for this piece of upstream content."""
        content: list[str] = []
        reasoning: list[str] = []
        buf = self.pending + (text or "")
        self.pending = ""
        while buf:
            if self.state == "start":
                stripped = buf.lstrip()
                if not stripped:
                    self.pending = buf
                    break
                if len(stripped) < len(OPEN) and OPEN.startswith(stripped):
                    self.pending = buf  # maybe the start of "<think>"
                    break
                if stripped.startswith(OPEN):
                    self.changed = True
                    self.state = "think"
                    buf = stripped[len(OPEN):]
                    continue
                if self.thinking and not self.engine_reasoning_seen:
                    idx = buf.find(CLOSE)
                    if idx >= 0:
                        # Reasoning whose opening tag was in the prompt.
                        self.changed = True
                        reasoning.append(buf[:idx])
                        buf = buf[idx + len(CLOSE):]
                        self.state = "content"
                        continue
                    if len(buf) < self.holdback:
                        self.pending = buf
                        break
                self.state = "content"
                continue
            if self.state == "think":
                idx = buf.find(CLOSE)
                if idx >= 0:
                    reasoning.append(buf[:idx])
                    buf = buf[idx + len(CLOSE):]
                    self.state = "content"
                    continue
                keep = _partial_tag_suffix(buf)
                reasoning.append(buf[:len(buf) - keep])
                self.pending = buf[len(buf) - keep:]
                break
            # content
            positions = [(buf.find(t), t) for t in _TAGS if t in buf]
            if positions:
                idx, tag = min(positions)
                content.append(buf[:idx])
                buf = buf[idx + len(tag):]
                self.changed = True
                if tag == OPEN:
                    self.state = "think"
                continue
            keep = _partial_tag_suffix(buf)
            content.append(buf[:len(buf) - keep])
            self.pending = buf[len(buf) - keep:]
            break
        return "".join(content), "".join(reasoning)

    def finish(self) -> tuple[str, str]:
        """Flush at end of choice: an unclosed think block stays reasoning."""
        rest, self.pending = self.pending, ""
        if not rest:
            return "", ""
        if self.state == "think":
            return "", rest
        if self.state == "start" and rest.strip() and len(rest.lstrip()) < len(OPEN) \
                and OPEN.startswith(rest.lstrip()):
            return rest, ""  # a short answer that merely looked like "<thi"
        return rest, ""


# ---------------------------------------------------------------------------
# Non-streamed completion bodies
# ---------------------------------------------------------------------------

def guard_message(message: dict[str, Any], *, thinking: bool) -> bool:
    """Fix one chat message in place; True when it changed."""
    content = message.get("content")
    if not isinstance(content, str) or not content:
        return False
    flt = ChoiceFilter(thinking=thinking, holdback_chars=1 << 62)
    key = next((k for k in REASONING_KEYS if message.get(k)), None)
    if key:
        flt.note_engine_reasoning()
    c1, r1 = flt.feed(content)
    c2, r2 = flt.finish()
    new_content, new_reasoning = c1 + c2, r1 + r2
    if new_content == content and not new_reasoning:
        return False
    message["content"] = new_content
    if new_reasoning:
        key = key or "reasoning"
        message[key] = (message.get(key) or "") + new_reasoning
    return True


def guard_completion_body(data: bytes, *, thinking: bool) -> bytes:
    """Return `data` unchanged unless a choice's content carried reasoning tags."""
    if b"think>" not in data and not thinking:
        return data
    try:
        doc = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        return data
    changed = False
    for choice in (doc.get("choices") or []) if isinstance(doc, dict) else []:
        msg = choice.get("message") if isinstance(choice, dict) else None
        if isinstance(msg, dict) and guard_message(msg, thinking=thinking):
            changed = True
    return json.dumps(doc, separators=(",", ":")).encode() if changed else data


# ---------------------------------------------------------------------------
# Streams
# ---------------------------------------------------------------------------

class StreamGuard:
    """Incremental SSE rewriter: feed(upstream bytes) -> bytes safe to forward.

    Works on complete SSE events; an event that needs no change is forwarded
    byte-for-byte. Pending text is flushed into the delta that carries the
    choice's finish_reason, or into a synthetic event before ``[DONE]``.
    """

    def __init__(self, *, thinking: bool, holdback_chars: int = DEFAULT_HOLDBACK_CHARS) -> None:
        self.thinking = thinking
        self.holdback = holdback_chars
        self._buf = b""
        self._filters: dict[int, ChoiceFilter] = {}
        self._key: dict[int, str] = {}
        self._template: dict[str, Any] = {}
        self._finished: set[int] = set()

    def _filter(self, idx: int) -> ChoiceFilter:
        if idx not in self._filters:
            self._filters[idx] = ChoiceFilter(thinking=self.thinking, holdback_chars=self.holdback)
        return self._filters[idx]

    def _process(self, event: bytes) -> bytes:
        if event.strip() == b"data: [DONE]":
            return self._flush_all() + event + _SEP
        if not event.startswith(b"data:"):
            return event + _SEP
        try:
            doc = json.loads(event[5:])
        except (ValueError, UnicodeDecodeError):
            return event + _SEP
        choices = doc.get("choices") if isinstance(doc, dict) else None
        if not isinstance(choices, list):
            return event + _SEP
        self._template = {k: v for k, v in doc.items() if k in ("id", "object", "created", "model")}
        modified = False
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            idx = int(choice.get("index") or 0)
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                continue
            flt = self._filter(idx)
            for k in REASONING_KEYS:
                if delta.get(k):
                    flt.note_engine_reasoning()
                    self._key[idx] = k
            text = delta.get("content")
            content, reasoning = flt.feed(text) if isinstance(text, str) and text else ("", "")
            if choice.get("finish_reason"):
                c2, r2 = flt.finish()
                content += c2
                reasoning += r2
                self._finished.add(idx)
            if (isinstance(text, str) and text != content) or reasoning:
                modified = True
                if isinstance(text, str) or content:
                    delta["content"] = content
                if reasoning:
                    key = self._key.get(idx, "reasoning")
                    delta[key] = (delta.get(key) or "") + reasoning
        if not modified:
            return event + _SEP
        return b"data: " + json.dumps(doc, separators=(",", ":")).encode() + _SEP

    def _flush_all(self) -> bytes:
        out = b""
        for idx, flt in self._filters.items():
            if idx in self._finished:
                continue
            content, reasoning = flt.finish()
            if not content and not reasoning:
                continue
            delta: dict[str, Any] = {}
            if reasoning:
                delta[self._key.get(idx, "reasoning")] = reasoning
            if content:
                delta["content"] = content
            doc = dict(self._template, choices=[{"index": idx, "delta": delta, "finish_reason": None}])
            out += b"data: " + json.dumps(doc, separators=(",", ":")).encode() + _SEP
            self._finished.add(idx)
        return out

    def feed(self, chunk: bytes) -> bytes:
        self._buf += chunk
        if _SEP not in self._buf:
            return b""
        complete, _, self._buf = self._buf.rpartition(_SEP)
        return b"".join(self._process(ev) for ev in complete.split(_SEP))

    def flush(self) -> bytes:
        """End of stream: a half event is dropped only when the caller says so
        (see `abort`); a clean end forwards the remainder and any held text."""
        rest, self._buf = self._buf, b""
        out = b""
        if rest.strip():
            out += self._process(rest.rstrip(b"\n"))
        return out + self._flush_all()

    def abort(self) -> None:
        """Upstream died mid-stream: discard the half-received event."""
        self._buf = b""
