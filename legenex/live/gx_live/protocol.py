"""gx-live.v1 wire rules shared by the supervisor's validation and its tests.

See ``legenex/live/PROTOCOL.md``. Binary frames: 8-byte big-endian header
``kind(1) version(1) response(2) seq(4)`` then the payload.
"""

from __future__ import annotations

import json
import math
import re
import struct
from typing import Any

from .errors import ValidationError

VERSION = 1
HEADER = struct.Struct("!BBHI")
KIND_MIC = 0x01
KIND_CAMERA = 0x02
KIND_ASSISTANT_AUDIO = 0x11
AUDIO_IN_RATE = 16000
AUDIO_OUT_RATE = 24000
MIN_MIC_BYTES = 2 * AUDIO_IN_RATE * 20 // 1000      # 20 ms
MAX_MIC_BYTES = 2 * AUDIO_IN_RATE * 500 // 1000     # 500 ms
MAX_JPEG_BYTES = 512 * 1024
CAMERA_FPS = 2
MAX_EVENT_BYTES = 64 * 1024
MAX_TEXT = 4000
SESSION_ID = re.compile(r"^live_[0-9a-f]{32}$")
OWNER = re.compile(r"^[0-9a-f]{16}$")

CLIENT_EVENTS = frozenset({"session.update", "input.text", "input.audio.commit", "response.cancel",
                           "playback.state", "ping", "session.stop"})

LIMITS = {"audio_in_rate": AUDIO_IN_RATE, "audio_out_rate": AUDIO_OUT_RATE, "min_frame_ms": 20,
          "max_frame_ms": 500, "max_jpeg_bytes": MAX_JPEG_BYTES, "frames_per_s": CAMERA_FPS,
          "max_event_bytes": MAX_EVENT_BYTES, "max_text_chars": MAX_TEXT}


class FrameError(ValidationError):
    """A client frame that breaks the protocol; ``close_code`` says how to close."""

    def __init__(self, message: str, close_code: int) -> None:
        super().__init__(message, code="protocol_error")
        self.close_code = close_code


def pack(kind: int, payload: bytes, *, response: int = 0, seq: int = 0) -> bytes:
    return HEADER.pack(kind, VERSION, response & 0xFFFF, seq & 0xFFFFFFFF) + payload


def unpack(frame: bytes) -> tuple[int, int, int, bytes]:
    if len(frame) < HEADER.size:
        raise FrameError("binary frame shorter than its header", 1003)
    kind, version, response, seq = HEADER.unpack_from(frame)
    if version != VERSION:
        raise FrameError(f"unsupported binary frame version {version}", 1003)
    return kind, response, seq, frame[HEADER.size:]


def check_client_binary(frame: bytes) -> tuple[int, bytes]:
    kind, _response, _seq, payload = unpack(frame)
    if kind == KIND_MIC:
        if len(payload) % 2 or not MIN_MIC_BYTES <= len(payload) <= MAX_MIC_BYTES:
            raise FrameError("microphone frames must be 20-500 ms of 16-bit PCM at 16 kHz", 1003)
        return kind, payload
    if kind == KIND_CAMERA:
        if len(payload) > MAX_JPEG_BYTES:
            raise FrameError("camera frames are limited to 512 KiB", 1009)
        if payload[:3] != b"\xff\xd8\xff":
            raise FrameError("camera frames must be JPEG images", 1003)
        return kind, payload
    raise FrameError(f"unsupported binary frame kind 0x{kind:02x}", 1003)


def _bool(ev: dict, key: str) -> bool | None:
    if key not in ev:
        return None
    if not isinstance(ev[key], bool):
        raise ValidationError(f"'{key}' must be true or false", code="invalid_event")
    return ev[key]


def parse_client_event(raw: str) -> dict:
    """Validate one client JSON event and return its normalised form."""
    if len(raw.encode("utf-8")) > MAX_EVENT_BYTES:
        raise FrameError("event larger than 64 KiB", 1009)
    try:
        ev = json.loads(raw)
    except ValueError as exc:
        raise FrameError("event is not valid JSON", 1007) from exc
    if not isinstance(ev, dict) or not isinstance(ev.get("type"), str):
        raise ValidationError("event must be an object with a string 'type'", code="invalid_event")
    kind = ev["type"]
    if kind not in CLIENT_EVENTS:
        raise ValidationError(f"unknown event type {kind[:40]!r}", code="unknown_event")
    if kind == "session.update":
        out: dict[str, Any] = {"type": kind}
        for key in ("camera", "output_audio", "muted"):
            value = _bool(ev, key)
            if value is not None:
                out[key] = value
        if len(out) == 1:
            raise ValidationError("session.update needs camera, output_audio or muted", code="invalid_event")
        return out
    if kind == "input.text":
        text = ev.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
            raise ValidationError(f"'text' must be 1-{MAX_TEXT} characters", code="invalid_event")
        if any(ord(c) < 32 and c not in "\n\t" for c in text):
            raise ValidationError("'text' contains control characters", code="invalid_event")
        return {"type": kind, "text": text.strip()}
    if kind == "playback.state":
        playing = _bool(ev, "playing")
        buffered = ev.get("buffered_ms", 0)
        if playing is None or not isinstance(buffered, (int, float)) or isinstance(buffered, bool) \
                or not 0 <= buffered <= 600000:
            raise ValidationError("playback.state needs playing (bool) and buffered_ms (0-600000)",
                                  code="invalid_event")
        return {"type": kind, "playing": playing, "buffered_ms": int(buffered)}
    if kind == "ping":
        t = ev.get("t", 0)
        if not isinstance(t, (int, float)) or isinstance(t, bool) or not math.isfinite(t):
            raise ValidationError("'t' must be a number", code="invalid_event")
        return {"type": kind, "t": t}
    return {"type": kind}


def normalise_config(body: Any) -> dict:
    """Session options accepted at creation (PROTOCOL.md section 1)."""
    if body is None:
        body = {}
    if not isinstance(body, dict):
        raise ValidationError("config must be an object")
    allowed = {"instructions", "language", "tools", "output_audio", "vad", "max_response_tokens"}
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise ValidationError(f"unsupported option(s): {', '.join(unknown[:6])}")
    instructions = body.get("instructions", "")
    if not isinstance(instructions, str) or len(instructions) > 2000:
        raise ValidationError("instructions must be at most 2000 characters")
    if any(ord(c) < 32 and c not in "\n\t" for c in instructions):
        raise ValidationError("instructions contain control characters")
    language = body.get("language", "en")
    if language not in ("en", "zh"):
        raise ValidationError("language must be en or zh")
    for key in ("tools", "output_audio"):
        if key in body and not isinstance(body[key], bool):
            raise ValidationError(f"{key} must be true or false")
    vad = body.get("vad") or {}
    if not isinstance(vad, dict) or set(vad) - {"threshold", "silence_ms"}:
        raise ValidationError("vad accepts threshold and silence_ms")
    threshold = vad.get("threshold", 0.5)
    silence = vad.get("silence_ms", 700)
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool) or not 0.3 <= threshold <= 0.9:
        raise ValidationError("vad.threshold must be between 0.3 and 0.9")
    if not isinstance(silence, int) or isinstance(silence, bool) or not 300 <= silence <= 2000:
        raise ValidationError("vad.silence_ms must be an integer between 300 and 2000")
    tokens = body.get("max_response_tokens", 256)
    if not isinstance(tokens, int) or isinstance(tokens, bool) or not 32 <= tokens <= 1024:
        raise ValidationError("max_response_tokens must be an integer between 32 and 1024")
    return {"instructions": instructions.strip(), "language": language, "tools": body.get("tools", True),
            "output_audio": body.get("output_audio", True),
            "vad": {"threshold": float(threshold), "silence_ms": silence}, "max_response_tokens": tokens}
