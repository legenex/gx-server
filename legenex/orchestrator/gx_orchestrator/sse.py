"""SSE normalisation for the engine's chat-completion stream (D-052).

Speculative decoding (dspark) can emit several tokens in one step, so a single
streamed delta sometimes carries BOTH the end of the reasoning and the first
answer token, e.g. ``{"reasoning": ".", "content": "1"}``. LiteLLM's
``merge_reasoning_content_in_choices`` then emits ``</think>`` and DROPS that
delta's content: clients saw ``</think>, 2, 3`` instead of ``</think>1, 2, 3``.

`DeltaSplitter` re-frames such an event as two: the reasoning part first, then
the content part. Every other event passes through byte-for-byte; JSON is only
parsed for events that mention both a reasoning key and content.
"""
from __future__ import annotations

import json

#: Delta keys that carry reasoning text (vLLM >= 0.11 "reasoning"; older
#: "reasoning_content").
_REASONING_KEYS = ("reasoning", "reasoning_content")
_SEP = b"\n\n"


def _is_mixed(choice: object) -> bool:
    if not isinstance(choice, dict):
        return False
    delta = choice.get("delta")
    if not isinstance(delta, dict) or not delta.get("content"):
        return False
    return any(delta.get(k) for k in _REASONING_KEYS)


def split_event(event: bytes) -> bytes:
    """One SSE event (without its trailing blank line) -> one or two events.

    Returns the bytes to write, including the trailing separator(s).
    """
    if (b'"content"' not in event or b'"reasoning' not in event
            or not event.startswith(b"data:")):
        return event + _SEP
    try:
        doc = json.loads(event[5:])
    except (ValueError, UnicodeDecodeError):
        return event + _SEP
    choices = doc.get("choices") if isinstance(doc, dict) else None
    if not isinstance(choices, list) or not any(_is_mixed(c) for c in choices):
        return event + _SEP

    first = dict(doc, choices=[])
    second = dict(doc, choices=[])
    # Usage (if any) belongs on the last event only.
    first.pop("usage", None)
    for choice in choices:
        if not _is_mixed(choice):
            first["choices"].append(choice)
            continue
        delta = choice["delta"]
        reasoning_delta = {k: v for k, v in delta.items() if k != "content"}
        content_delta = {k: v for k, v in delta.items()
                         if k not in _REASONING_KEYS and k != "role"}
        first["choices"].append(dict(choice, delta=reasoning_delta, finish_reason=None))
        second["choices"].append(dict(choice, delta=content_delta))
    return (b"data: " + json.dumps(first, separators=(",", ":")).encode() + _SEP
            + b"data: " + json.dumps(second, separators=(",", ":")).encode() + _SEP)


class DeltaSplitter:
    """Incremental `split_event` over arbitrary byte chunks of an SSE stream."""

    def __init__(self) -> None:
        self._buf = b""

    def feed(self, chunk: bytes) -> bytes:
        """Bytes ready to forward now (complete events only)."""
        self._buf += chunk
        if _SEP not in self._buf:
            return b""
        complete, _, self._buf = self._buf.rpartition(_SEP)
        return b"".join(split_event(ev) for ev in complete.split(_SEP))

    def flush(self) -> bytes:
        """Whatever is left at end of stream, forwarded unchanged."""
        rest, self._buf = self._buf, b""
        return rest
