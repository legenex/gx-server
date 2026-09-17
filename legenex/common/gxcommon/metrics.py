"""Structured, privacy-safe metric lines (Build V3, Part 18).

One JSON object per line::

    {"ts": "...", "kind": "metric", "service": "gx-call", "node": "gx10-02",
     "event": "model.load", "outcome": "ok", "duration_ms": 81234, ...}

The event vocabulary and field names are published in
``coordination/build-v3/plt.md`` section 4. The helper never lets content or
credentials through: fields whose NAME suggests content or a secret are
dropped, strings are truncated and credential-shaped values are redacted.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Any

EVENTS = frozenset({
    "model.load", "model.unload", "queue.wait", "admission.wait", "generation",
    "realtime.session", "realtime.latency", "tunnel.open", "tunnel.close",
    "memory.sample", "failure", "flow.run", "flow.node", "call.disposition",
    "voice.synthesis", "music.render",
})
OUTCOMES = frozenset({"ok", "failed", "refused", "cancelled", "waiting"})
MAX_STR = 200
MAX_LIST = 20
MAX_FIELDS = 40

_EVENT_RE = re.compile(r"^[a-z][a-z0-9_]{1,30}(\.[a-z][a-z0-9_]{1,30})?$")
_FIELD_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
#: Field names that may carry content or credentials: never logged.
_DENY_EXACT = frozenset({
    "text", "prompt", "prompts", "negative_prompt", "transcript", "transcripts", "content", "audio", "image",
    "images", "video", "messages", "message_text", "body", "payload", "lyrics", "authorization", "cookie",
    "cookies", "key", "token", "ticket", "secret", "password", "passwd", "credential", "credentials",
    "headers", "caption", "utterance", "system_prompt",
})
_DENY_PARTS = ("api_key", "apikey", "secret", "password", "authorization", "cookie", "credential")
#: Counts such as ``completion_tokens`` are fine; anything else named *token* is not.
_TOKEN_COUNT = re.compile(r"(^|_)tokens$|_token_count$")
_SECRET_RES = (
    re.compile(r"sk-[A-Za-z0-9_\-]{6,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=\-]{8,}"),
    re.compile(r"hf_[A-Za-z0-9]{10,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{10,}"),
    re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}"),
)


def redact_text(value: str) -> str:
    """Remove credential-shaped substrings and cut to MAX_STR characters."""
    out = value
    for rx in _SECRET_RES:
        out = rx.sub("[redacted]", out)
    out = out.replace("\r", " ").replace("\n", " ")
    return out[:MAX_STR]


def allowed_field(name: str) -> bool:
    if not _FIELD_RE.match(name) or name in _DENY_EXACT:
        return False
    if "token" in name and not _TOKEN_COUNT.search(name):
        return False
    return not any(part in name for part in _DENY_PARTS)


def _scalar(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return round(value, 3) if value == value and value not in (float("inf"), float("-inf")) else None
    if isinstance(value, str):
        return redact_text(value)
    return _SKIP


class _Skip:
    pass


_SKIP = _Skip()


def clean_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    """The subset of `fields` that is safe to log (pure; unit-tested)."""
    out: dict[str, Any] = {}
    for name, value in fields.items():
        if len(out) >= MAX_FIELDS:
            break
        if not isinstance(name, str) or not allowed_field(name):
            continue
        if isinstance(value, (list, tuple)):
            items = [_scalar(v) for v in list(value)[:MAX_LIST]]
            if all(not isinstance(v, _Skip) for v in items):
                out[name] = items
            continue
        if isinstance(value, Mapping):
            flat = {}
            for k, v in list(value.items())[:MAX_LIST]:
                sv = _scalar(v)
                if isinstance(k, str) and len(k) <= 64 and not isinstance(sv, _Skip) \
                        and (k.startswith("gx-") or allowed_field(k)):
                    flat[k] = sv
            out[name] = flat
            continue
        sv = _scalar(value)
        if not isinstance(sv, _Skip):
            out[name] = sv
    return out


def _iso(ts: float) -> str:
    base = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))
    tz = time.strftime("%z", time.localtime(ts))
    return f"{base}.{int((ts % 1) * 1000):03d}{tz[:3]}:{tz[3:]}" if tz else f"{base}.{int((ts % 1) * 1000):03d}"


class Metrics:
    """Emits metric lines to a stream (stdout by default) and optionally a JSONL file.

    Thread-safe. A write failure never raises into the caller: metrics must not
    break the service they observe.
    """

    def __init__(self, service: str, *, node: str | None = None, file: str | os.PathLike[str] | None = None,
                 stream: IO[str] | None = None, enabled: bool = True) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9\-]{1,40}", service):
            raise ValueError("service must be a short lowercase name")
        self.service = service
        self.node = node or os.uname().nodename
        self.file = Path(file) if file else None
        self.stream = stream
        self.enabled = enabled
        self._lock = threading.Lock()

    def build(self, event: str, fields: Mapping[str, Any], *, ts: float | None = None) -> dict[str, Any]:
        if event not in EVENTS and not _EVENT_RE.match(event):
            raise ValueError(f"invalid metric event name: {event!r}")
        body = clean_fields(fields)
        outcome = body.get("outcome")
        if outcome is not None and outcome not in OUTCOMES:
            body["outcome"] = "failed" if outcome in ("error", "fail") else str(outcome)[:16]
        line = {"ts": _iso(ts if ts is not None else time.time()), "kind": "metric",
                "service": self.service, "node": self.node, "event": event}
        for k, v in body.items():
            if k not in line:
                line[k] = v
        return line

    def emit(self, event: str, /, **fields: Any) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        line = self.build(event, fields)
        text = json.dumps(line, separators=(",", ":"), ensure_ascii=False)
        with self._lock:
            try:
                out = self.stream if self.stream is not None else sys.stdout
                out.write(text + "\n")
                out.flush()
            except (OSError, ValueError):
                pass
            if self.file is not None:
                try:
                    self.file.parent.mkdir(parents=True, exist_ok=True)
                    fd = os.open(self.file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o640)
                    with os.fdopen(fd, "a", encoding="utf-8") as fh:
                        fh.write(text + "\n")
                except OSError:
                    pass
        return line

    @contextmanager
    def timer(self, event: str, /, **fields: Any) -> Iterator[_Timer]:
        """Emit `event` with duration_ms and outcome when the block ends.

        An exception marks the line ``outcome=failed`` with ``error_code`` set
        to the exception's ``code`` attribute (or its class name) and re-raises.
        """
        t = _Timer(fields)
        start = time.monotonic()
        try:
            yield t
        except BaseException as exc:
            t.fields.setdefault("outcome", "cancelled" if isinstance(exc, KeyboardInterrupt) else "failed")
            t.fields.setdefault("error_code", str(getattr(exc, "code", "") or type(exc).__name__)[:64])
            raise
        finally:
            t.fields.setdefault("outcome", "ok")
            self.emit(event, duration_ms=int((time.monotonic() - start) * 1000), **t.fields)


class _Timer:
    def __init__(self, fields: Mapping[str, Any]) -> None:
        self.fields = dict(fields)

    def set(self, **fields: Any) -> None:
        self.fields.update(fields)


def read_metrics(paths: list[Path], *, user: str | None = None, since: float = 0.0,
                 limit: int = 500, max_bytes: int = 4 * 2**20) -> list[dict[str, Any]]:
    """Newest-last metric lines from JSONL files (tail-bounded), optionally for one user."""
    out: list[dict[str, Any]] = []
    for path in paths:
        try:
            size = path.stat().st_size
            with path.open("rb") as fh:
                if size > max_bytes:
                    fh.seek(size - max_bytes)
                    fh.readline()
                data = fh.read()
        except OSError:
            continue
        for raw in data.splitlines():
            try:
                item = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(item, dict) or item.get("kind") != "metric":
                continue
            if user is not None and item.get("user") != user:
                continue
            if since and _parse_ts(item.get("ts")) < since:
                continue
            out.append(item)
    out.sort(key=lambda i: _parse_ts(i.get("ts")))
    return out[-limit:]


def _parse_ts(value: Any) -> float:
    if not isinstance(value, str):
        return 0.0
    from datetime import datetime
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return 0.0
