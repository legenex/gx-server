"""Boundary validation. Every field a caller can influence is checked here.

Nothing downstream re-checks: by the time a value leaves this module it is the
right type and inside the bounds the hardware can actually serve.
"""

from __future__ import annotations

import re
import secrets

from .config import Config
from .errors import ValidationError

_SIZE_RE = re.compile(r"^(\d{2,5})\s*[xX]\s*(\d{2,5})$")
_MAX_SEED = 2**63 - 1

_SAMPLERS = {"euler", "euler_ancestral", "dpmpp_2m", "dpmpp_2m_sde", "ddim", "uni_pc", "lcm", "res_multistep"}
_SCHEDULERS = {"simple", "normal", "karras", "exponential", "sgm_uniform", "beta", "ddim_uniform"}


def require_object(body: object) -> dict:
    if not isinstance(body, dict):
        raise ValidationError("request body must be a JSON object")
    return body


def prompt(body: dict, cfg: Config, *, field: str = "prompt") -> str:
    value = body.get(field)
    if value is None or not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} is required and must be a non-empty string", param=field)
    text = value.strip()
    if len(text) > cfg.max_prompt_chars:
        raise ValidationError(
            f"{field} is {len(text)} characters; the maximum is {cfg.max_prompt_chars}", param=field
        )
    if "\x00" in text:
        raise ValidationError(f"{field} must not contain NUL bytes", param=field)
    return text


def optional_text(body: dict, field: str, cfg: Config, default: str | None = None) -> str | None:
    value = body.get(field)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a string", param=field)
    if len(value) > cfg.max_prompt_chars:
        raise ValidationError(f"{field} exceeds {cfg.max_prompt_chars} characters", param=field)
    return value


def dimensions(body: dict, cfg: Config, default: str) -> tuple[int, int]:
    raw = body.get("size")
    if raw is None or raw in ("auto", ""):
        raw = default
    if not isinstance(raw, str):
        raise ValidationError("size must be a string like '1024x1024' or 'auto'", param="size")
    match = _SIZE_RE.match(raw.strip())
    if not match:
        raise ValidationError(f"size {raw!r} is not of the form WIDTHxHEIGHT", param="size")
    width, height = int(match.group(1)), int(match.group(2))
    for name, value in (("width", width), ("height", height)):
        if not cfg.min_dimension <= value <= cfg.max_dimension:
            raise ValidationError(
                f"{name} {value} is outside [{cfg.min_dimension}, {cfg.max_dimension}]", param="size"
            )
        if value % cfg.dimension_multiple:
            raise ValidationError(
                f"{name} {value} must be a multiple of {cfg.dimension_multiple}", param="size"
            )
    return width, height


def count(body: dict, cfg: Config) -> int:
    value = body.get("n", 1)
    if value is None:
        return 1
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError("n must be an integer", param="n")
    if not 1 <= value <= cfg.max_images_per_request:
        raise ValidationError(f"n must be between 1 and {cfg.max_images_per_request}", param="n")
    return value


def seed(body: dict, field: str = "seed") -> int:
    value = body.get(field)
    if value is None:
        return secrets.randbelow(_MAX_SEED)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{field} must be an integer", param=field)
    if not 0 <= value <= _MAX_SEED:
        raise ValidationError(f"{field} must be between 0 and {_MAX_SEED}", param=field)
    return value


def response_format(body: dict) -> str:
    value = body.get("response_format") or "b64_json"
    if value not in {"b64_json", "url"}:
        raise ValidationError("response_format must be 'b64_json' or 'url'", param="response_format")
    return value


def bounded_int(body: dict, field: str, lo: int, hi: int, default: int | None = None) -> int | None:
    value = body.get(field)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{field} must be an integer", param=field)
    if not lo <= value <= hi:
        raise ValidationError(f"{field} must be between {lo} and {hi}", param=field)
    return value


def bounded_float(body: dict, field: str, lo: float, hi: float, default: float | None = None) -> float | None:
    value = body.get(field)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{field} must be a number", param=field)
    if not lo <= float(value) <= hi:
        raise ValidationError(f"{field} must be between {lo} and {hi}", param=field)
    return float(value)


def enum(body: dict, field: str, allowed: set[str], default: str | None = None) -> str | None:
    value = body.get(field)
    if value is None:
        return default
    if value not in allowed:
        raise ValidationError(
            f"{field} must be one of: {', '.join(sorted(allowed))}", param=field
        )
    return str(value)


def sampler(body: dict) -> str | None:
    return enum(body, "sampler_name", _SAMPLERS)


def scheduler(body: dict) -> str | None:
    return enum(body, "scheduler", _SCHEDULERS)


def video_length(body: dict, cfg: Config, fps: float) -> int:
    """Frame count. Wan's latent packing requires (length - 1) % 4 == 0."""
    frames = body.get("length")
    if frames is None:
        seconds = bounded_float(body, "seconds", 0.5, 20.0, 3.0)
        frames = int(round(float(seconds) * fps))
    if isinstance(frames, bool) or not isinstance(frames, int):
        raise ValidationError("length must be an integer frame count", param="length")
    frames = max(5, min(int(frames), cfg.max_video_frames))
    # Snap to the NEAREST valid 4k+1 so a requested duration is honoured rather
    # than always truncated; fall back downward if rounding up exceeds the cap.
    remainder = (frames - 1) % 4
    lower = frames - remainder
    upper = lower + 4
    frames = lower if remainder <= 2 else upper
    if frames > cfg.max_video_frames:
        frames = lower
    return max(5, frames)
