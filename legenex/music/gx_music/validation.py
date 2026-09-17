"""Request validation: public JSON -> a normalized, engine-ready parameter set.

Only controls that the installed checkpoint really honours are accepted.
ACE-Step XL *turbo* is a distilled 8-step model: it has no CFG, ignores
``shift``/``use_adg``/CFG intervals, and supports text2music, cover (remix) and
repaint (edit / extend). Extract, lego and complete need the XL *base*
checkpoint, which is not installed; they are rejected, not faked.

Everything here is pure and unit-tested; nothing touches the network or disk.
"""

from __future__ import annotations

import dataclasses
import re
import secrets
from typing import Any

from .errors import ValidationError

# Mirrors acestep/constants.py at the pinned runtime ref.
VALID_LANGUAGES = (
    "ar", "az", "bg", "bn", "ca", "cs", "da", "de", "el", "en",
    "es", "fa", "fi", "fr", "he", "hi", "hr", "ht", "hu", "id",
    "is", "it", "ja", "ko", "la", "lt", "ms", "ne", "nl", "no",
    "pa", "pl", "pt", "ro", "ru", "sa", "sk", "sr", "sv", "sw",
    "ta", "te", "th", "tl", "tr", "uk", "ur", "vi", "yue", "zh",
    "unknown",
)
VALID_TIME_SIGNATURES = ("2", "3", "4", "6")
BPM_MIN, BPM_MAX = 30, 300
DURATION_MIN = 10
CAPTION_MAX = 512
LYRICS_MAX = 4096
TAG_MAX_LEN = 48
TAGS_MAX = 24
TITLE_MAX = 120
SEED_MAX = 2**31 - 1
INSTRUMENTAL_LYRICS = "[Instrumental]"

# Structural lyric tags ACE-Step is trained on (docs/en/ace_step_musicians_guide.md).
LYRIC_SECTIONS = (
    "Intro", "Verse", "Pre-Chorus", "Chorus", "Post-Chorus", "Bridge",
    "Hook", "Breakdown", "Drop", "Build", "Interlude", "Instrumental",
    "Solo", "Guitar Solo", "Outro", "Fade Out",
)

_NOTES = "ABCDEFG"
_ACCIDENTALS = {"": "", "#": "#", "b": "b", "♯": "#", "♭": "b"}
_KEY_RE = re.compile(r"^\s*([A-Ga-g])\s*([#b♯♭]?)\s*(maj(?:or)?|min(?:or)?|m)?\s*$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{7,63}$")


@dataclasses.dataclass(frozen=True)
class Capabilities:
    """What the loaded DiT supports. Built from the checkpoint name."""

    dit_name: str
    task_types: tuple[str, ...]
    cfg: bool
    steps_min: int
    steps_max: int
    steps_default: int

    @classmethod
    def for_model(cls, dit_name: str) -> "Capabilities":
        if "turbo" in dit_name:
            return cls(dit_name, ("text2music", "cover", "repaint"), False, 1, 20, 8)
        if "sft" in dit_name:
            return cls(dit_name, ("text2music", "cover", "repaint"), True, 1, 200, 50)
        return cls(dit_name, ("text2music", "cover", "repaint", "extract", "lego", "complete"), True, 1, 200, 50)

    def as_dict(self, max_duration: int) -> dict:
        controls: dict[str, Any] = {
            "prompt": {"type": "string", "max_length": CAPTION_MAX},
            "style_tags": {"type": "array", "max_items": TAGS_MAX, "item_max_length": TAG_MAX_LEN},
            "lyrics": {"type": "string", "max_length": LYRICS_MAX, "sections": list(LYRIC_SECTIONS)},
            "instrumental": {"type": "boolean"},
            "description": {"type": "string", "max_length": CAPTION_MAX,
                            "note": "natural-language request; the 5Hz LM writes caption, lyrics, BPM, key, "
                                    "time signature and duration, so those controls are rejected with it"},
            "vocal_language": {"type": "enum", "values": list(VALID_LANGUAGES)},
            "duration": {"type": "number", "min": DURATION_MIN, "max": max_duration, "unit": "s"},
            "bpm": {"type": "integer", "min": BPM_MIN, "max": BPM_MAX, "nullable": True},
            "key": {"type": "string", "example": "F# minor", "nullable": True},
            "time_signature": {"type": "enum", "values": list(VALID_TIME_SIGNATURES), "nullable": True,
                               "labels": {"2": "2/4", "3": "3/4", "4": "4/4", "6": "6/8"}},
            "seed": {"type": "integer", "min": 0, "max": SEED_MAX, "nullable": True},
            "batch_size": {"type": "integer", "min": 1, "max": 4},
            "inference_steps": {"type": "integer", "min": self.steps_min, "max": self.steps_max,
                                "default": self.steps_default},
            "infer_method": {"type": "enum", "values": ["ode", "sde"]},
            "thinking": {"type": "boolean", "default": True,
                         "note": "5Hz LM plans the song (audio codes) before the DiT renders it"},
            "enhance_prompt": {"type": "boolean", "default": False},
            "lm_temperature": {"type": "number", "min": 0.0, "max": 2.0},
            "lm_cfg_scale": {"type": "number", "min": 1.0, "max": 5.0},
            "lm_top_p": {"type": "number", "min": 0.0, "max": 1.0},
            "output_format": {"type": "enum", "values": ["wav", "flac", "mp3"]},
            "reference": {"type": "source", "note": "optional style/timbre reference audio"},
        }
        if self.cfg:
            controls["guidance_scale"] = {"type": "number", "min": 1.0, "max": 15.0}
        return {
            "task_types": list(self.task_types),
            "operations": {
                "generate": "text2music",
                "remix": "cover" if "cover" in self.task_types else None,
                "edit": "repaint" if "repaint" in self.task_types else None,
                "extend": "repaint" if "repaint" in self.task_types else None,
                "extract": "extract" if "extract" in self.task_types else None,
                "lego": "lego" if "lego" in self.task_types else None,
                "complete": "complete" if "complete" in self.task_types else None,
            },
            "controls": controls,
            "remix_controls": {
                "strength": {"type": "number", "min": 0.0, "max": 1.0, "default": 0.5,
                             "note": "how much of the source structure is kept (audio_cover_strength)"},
                "noise_strength": {"type": "number", "min": 0.0, "max": 1.0, "default": 0.0,
                                   "note": "0 = fresh noise, 1 = start closest to the source"},
            },
            "edit_controls": {
                "start": {"type": "number", "min": 0, "unit": "s"},
                "end": {"type": "number", "unit": "s"},
                "mode": {"type": "enum", "values": ["conservative", "balanced", "aggressive"]},
                "strength": {"type": "number", "min": 0.0, "max": 1.0, "note": "balanced mode only"},
                "crossfade": {"type": "number", "min": 0.0, "max": 5.0, "unit": "s"},
            },
            "extend_controls": {
                "seconds": {"type": "number", "min": 5, "max": 240, "unit": "s"},
                "direction": {"type": "enum", "values": ["end", "start"]},
            },
            "cfg_supported": self.cfg,
            "output": {"sample_rate": 48000, "channels": 2,
                       "formats": {"wav": "32-bit float master", "flac": "24-bit lossless", "mp3": "320 kbps preview"}},
        }


@dataclasses.dataclass(frozen=True)
class SourceRef:
    job_id: str | None = None
    index: int = 0
    upload_id: str | None = None

    def as_dict(self) -> dict:
        if self.upload_id:
            return {"upload_id": self.upload_id}
        return {"job_id": self.job_id, "index": self.index}


@dataclasses.dataclass
class MusicRequest:
    """A validated request. ``engine`` holds upstream release_task fields."""

    operation: str  # generate | remix | edit | extend
    title: str
    prompt: str
    style_tags: list[str]
    lyrics: str
    instrumental: bool
    output_format: str
    batch_size: int
    seeds: list[int]
    source: SourceRef | None
    reference: SourceRef | None
    parent_job_id: str | None
    parent_index: int | None
    engine: dict[str, Any]
    extend: dict[str, Any] | None = None

    def public(self) -> dict:
        return {
            "operation": self.operation,
            "title": self.title,
            "prompt": self.prompt,
            "style_tags": self.style_tags,
            "lyrics": self.lyrics,
            "instrumental": self.instrumental,
            "output_format": self.output_format,
            "batch_size": self.batch_size,
            "seeds": self.seeds,
            "source": self.source.as_dict() if self.source else None,
            "reference": self.reference.as_dict() if self.reference else None,
            "parent_job_id": self.parent_job_id,
            "parent_index": self.parent_index,
            "extend": self.extend,
            "parameters": {k: v for k, v in self.engine.items()
                           if k not in {"prompt", "lyrics", "src_audio_path", "reference_audio_path"}},
        }


# ------------------------------------------------------------------ helpers --
def _text(body: dict, key: str, limit: int, *, default: str = "") -> str:
    value = body.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValidationError(f"'{key}' must be a string")
    value = _CONTROL_RE.sub("", value).replace("\r\n", "\n").strip()
    if len(value) > limit:
        raise ValidationError(f"'{key}' is too long ({len(value)} characters, maximum {limit})")
    return value


def _bool(body: dict, key: str, default: bool) -> bool:
    value = body.get(key, default)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValidationError(f"'{key}' must be true or false")
    return value


def _number(body: dict, key: str, lo: float, hi: float, default: float | None) -> float | None:
    value = body.get(key, default)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"'{key}' must be a number")
    if value != value or not lo <= value <= hi:  # NaN check first
        raise ValidationError(f"'{key}' must be between {lo:g} and {hi:g}")
    return float(value)


def _integer(body: dict, key: str, lo: int, hi: int, default: int | None) -> int | None:
    value = body.get(key, default)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        else:
            raise ValidationError(f"'{key}' must be an integer")
    if not lo <= value <= hi:
        raise ValidationError(f"'{key}' must be between {lo} and {hi}")
    return value


def _enum(body: dict, key: str, values: tuple[str, ...] | list[str], default: str) -> str:
    value = body.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str) or value not in values:
        raise ValidationError(f"'{key}' must be one of: {', '.join(values)}")
    return value


def normalize_key(raw: Any) -> str:
    """'Am' -> 'A minor', 'f# maj' -> 'F# major', 'Bb minor' -> 'Bb minor'."""
    if raw is None or raw == "":
        return ""
    if not isinstance(raw, str):
        raise ValidationError("'key' must be a string such as 'C major' or 'F# minor'")
    m = _KEY_RE.match(raw)
    if not m:
        raise ValidationError("'key' must look like 'C major', 'F# minor' or 'Am'")
    note, acc, mode = m.group(1).upper(), _ACCIDENTALS[m.group(2)], (m.group(3) or "").lower()
    if note not in _NOTES:
        raise ValidationError("'key' note must be A-G")
    scale = "minor" if mode in {"m", "min", "minor"} else "major"
    return f"{note}{acc} {scale}"


def normalize_time_signature(raw: Any) -> str:
    if raw is None or raw == "":
        return ""
    value = str(raw).strip()
    mapping = {"2/4": "2", "3/4": "3", "4/4": "4", "6/8": "6"}
    value = mapping.get(value, value)
    if value not in VALID_TIME_SIGNATURES:
        raise ValidationError("'time_signature' must be one of 2/4, 3/4, 4/4, 6/8")
    return value


def normalize_tags(raw: Any) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValidationError("'style_tags' must be an array of strings")
    if len(raw) > TAGS_MAX:
        raise ValidationError(f"at most {TAGS_MAX} style tags are allowed")
    out: list[str] = []
    seen: set[str] = set()
    for tag in raw:
        if not isinstance(tag, str):
            raise ValidationError("every style tag must be a string")
        tag = _CONTROL_RE.sub("", tag).replace(",", " ").strip()
        tag = re.sub(r"\s+", " ", tag)
        if not tag:
            continue
        if len(tag) > TAG_MAX_LEN:
            raise ValidationError(f"style tag '{tag[:20]}…' is longer than {TAG_MAX_LEN} characters")
        if tag.lower() not in seen:
            seen.add(tag.lower())
            out.append(tag)
    return out


def build_caption(prompt: str, tags: list[str]) -> str:
    """ACE-Step captions are comma-separated descriptors; tags are appended."""
    parts = [prompt] if prompt else []
    lowered = prompt.lower()
    parts += [t for t in tags if t.lower() not in lowered]
    caption = ", ".join(parts)
    if len(caption) > CAPTION_MAX:
        raise ValidationError(f"prompt plus style tags is {len(caption)} characters; the maximum is {CAPTION_MAX}")
    return caption


def parse_source(raw: Any, field: str) -> SourceRef | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValidationError(f"'{field}' must be an object with job_id or upload_id")
    job_id, upload_id = raw.get("job_id"), raw.get("upload_id")
    if bool(job_id) == bool(upload_id):
        raise ValidationError(f"'{field}' needs exactly one of job_id or upload_id")
    ident = job_id or upload_id
    if not isinstance(ident, str) or not _ID_RE.match(ident):
        raise ValidationError(f"'{field}' id is malformed")
    index = _integer(raw, "index", 0, 7, 0) or 0
    return SourceRef(job_id=job_id, index=index, upload_id=upload_id)


def _seeds(body: dict, batch: int) -> list[int]:
    seed = _integer(body, "seed", 0, SEED_MAX, None)
    if seed is None:
        return [secrets.randbelow(SEED_MAX) for _ in range(batch)]
    # Deterministic: item i uses seed+i, so batch item 0 always equals a
    # single-item run with the same seed.
    return [(seed + i) % SEED_MAX for i in range(batch)]


def _common(body: dict, caps: Capabilities, *, max_duration: int, lyrics_required: bool) -> tuple[dict, dict]:
    if not isinstance(body, dict):
        raise ValidationError("request body must be a JSON object")
    title = _text(body, "title", TITLE_MAX)
    prompt = _text(body, "prompt", CAPTION_MAX)
    tags = normalize_tags(body.get("style_tags"))
    lyrics = _text(body, "lyrics", LYRICS_MAX)
    instrumental = _bool(body, "instrumental", False)
    description = _text(body, "description", CAPTION_MAX)
    if instrumental:
        lyrics = INSTRUMENTAL_LYRICS
    caption = build_caption(prompt, tags)
    if not caption and not description and lyrics_required:
        raise ValidationError("give a prompt, style tags or a description")
    batch = _integer(body, "batch_size", 1, 4, 1) or 1
    steps = _integer(body, "inference_steps", caps.steps_min, caps.steps_max, caps.steps_default)
    if not caps.cfg:
        for unsupported in ("guidance_scale", "shift", "use_adg", "cfg_interval_start", "cfg_interval_end"):
            if body.get(unsupported) is not None:
                raise ValidationError(f"'{unsupported}' is not supported by {caps.dit_name} (distilled, no CFG)")
    seeds = _seeds(body, batch)
    engine: dict[str, Any] = {
        "prompt": caption,
        "lyrics": lyrics,
        "vocal_language": _enum(body, "vocal_language", VALID_LANGUAGES, "unknown" if instrumental else "en"),
        "inference_steps": steps,
        "infer_method": _enum(body, "infer_method", ("ode", "sde"), "ode"),
        "thinking": _bool(body, "thinking", True),
        "use_format": _bool(body, "enhance_prompt", False),
        "lm_temperature": _number(body, "lm_temperature", 0.0, 2.0, 0.85),
        "lm_cfg_scale": _number(body, "lm_cfg_scale", 1.0, 5.0, 2.5),
        "lm_top_p": _number(body, "lm_top_p", 0.0, 1.0, 0.9),
        "batch_size": batch,
        "use_random_seed": False,
        "seed": ",".join(str(s) for s in seeds) if batch > 1 else seeds[0],
        "audio_format": "wav32",
        "model": caps.dit_name,
    }
    if not engine["thinking"]:
        # "No LM planning" must mean no LM sampling at all (it is not seeded).
        engine["use_cot_caption"] = False
        engine["use_cot_language"] = False
    if caps.cfg:
        gs = _number(body, "guidance_scale", 1.0, 15.0, None)
        if gs is not None:
            engine["guidance_scale"] = gs
    if description:
        # Upstream sample mode lets the LM write caption, lyrics, BPM, key,
        # time signature AND duration, overwriting anything supplied. Refuse
        # the combination instead of silently ignoring the user's controls.
        clash = [k for k in ("prompt", "lyrics", "duration", "bpm", "key", "time_signature")
                 if body.get(k) not in (None, "", [])]
        if clash or tags:
            raise ValidationError(
                "description mode lets the model choose everything; remove "
                + ", ".join(clash + (["style_tags"] if tags else [])) + " or leave description empty")
        engine["sample_mode"] = True
        engine["sample_query"] = description
    bpm = _integer(body, "bpm", BPM_MIN, BPM_MAX, None)
    if bpm is not None:
        engine["bpm"] = bpm
    key = normalize_key(body.get("key"))
    if key:
        engine["key_scale"] = key
    ts = normalize_time_signature(body.get("time_signature"))
    if ts:
        engine["time_signature"] = ts
    duration = _number(body, "duration", DURATION_MIN, max_duration, None)
    if duration is not None:
        engine["audio_duration"] = duration
    common = {
        "title": title, "prompt": prompt, "style_tags": tags, "lyrics": lyrics,
        "instrumental": instrumental, "batch_size": batch, "seeds": seeds,
        "output_format": _enum(body, "output_format", ("wav", "flac", "mp3"), "mp3"),
        "reference": parse_source(body.get("reference"), "reference"),
    }
    return common, engine


def _parent(body: dict, source: SourceRef | None) -> tuple[str | None, int | None]:
    parent = body.get("parent_job_id")
    if parent is not None and (not isinstance(parent, str) or not _ID_RE.match(parent)):
        raise ValidationError("'parent_job_id' is malformed")
    if parent is None and source and source.job_id:
        return source.job_id, source.index
    return parent, _integer(body, "parent_index", 0, 7, 0) if parent else None


def generation(body: dict, caps: Capabilities, *, max_duration: int) -> MusicRequest:
    common, engine = _common(body, caps, max_duration=max_duration, lyrics_required=True)
    engine["task_type"] = "text2music"
    if "audio_duration" not in engine and not engine["thinking"] and "sample_query" not in engine:
        # Without the LM nobody picks a length; upstream would fall back to 120 s.
        engine["audio_duration"] = 60.0
    parent, pidx = _parent(body, None)
    return MusicRequest(operation="generate", source=None, parent_job_id=parent, parent_index=pidx,
                        engine=engine, **common)


def _require_task(caps: Capabilities, task: str, operation: str) -> None:
    if task not in caps.task_types:
        raise ValidationError(f"{operation} is not supported by {caps.dit_name}", code="unsupported_operation")


def remix(body: dict, caps: Capabilities, *, max_duration: int) -> MusicRequest:
    _require_task(caps, "cover", "remix")
    source = parse_source(body.get("source"), "source")
    if source is None:
        raise ValidationError("remix needs a 'source' track")
    common, engine = _common(body, caps, max_duration=max_duration, lyrics_required=True)
    engine.update({
        "task_type": "cover",
        "audio_cover_strength": _number(body, "strength", 0.0, 1.0, 0.5),
        "cover_noise_strength": _number(body, "noise_strength", 0.0, 1.0, 0.0),
    })
    engine.pop("audio_duration", None)  # a cover follows the source length
    parent, pidx = _parent(body, source)
    return MusicRequest(operation="remix", source=source, parent_job_id=parent, parent_index=pidx,
                        engine=engine, **common)


def edit(body: dict, caps: Capabilities, *, max_duration: int) -> MusicRequest:
    _require_task(caps, "repaint", "edit")
    source = parse_source(body.get("source"), "source")
    if source is None:
        raise ValidationError("edit needs a 'source' track")
    start = _number(body, "start", 0.0, float(max_duration), None)
    end = _number(body, "end", 0.0, float(max_duration), None)
    if start is None or end is None:
        raise ValidationError("edit needs 'start' and 'end' (seconds)")
    if end - start < 1.0:
        raise ValidationError("the edited section must be at least 1 second long")
    common, engine = _common(body, caps, max_duration=max_duration, lyrics_required=False)
    engine.update({
        "task_type": "repaint",
        "repainting_start": start,
        "repainting_end": end,
        "chunk_mask_mode": "explicit",
        "repaint_mode": _enum(body, "mode", ("conservative", "balanced", "aggressive"), "balanced"),
        "repaint_strength": _number(body, "strength", 0.0, 1.0, 0.5),
        "repaint_wav_crossfade_sec": _number(body, "crossfade", 0.0, 5.0, 0.0),
    })
    engine.pop("audio_duration", None)
    parent, pidx = _parent(body, source)
    return MusicRequest(operation="edit", source=source, parent_job_id=parent, parent_index=pidx,
                        engine=engine, **common)


def extend(body: dict, caps: Capabilities, *, max_duration: int) -> MusicRequest:
    """Outpainting: upstream repaint pads the source when the range leaves it.

    The start/end are resolved against the real source duration by the
    service (it knows the file); here we only validate intent.
    """
    _require_task(caps, "repaint", "extend")
    source = parse_source(body.get("source"), "source")
    if source is None:
        raise ValidationError("extend needs a 'source' track")
    seconds = _number(body, "seconds", 5.0, 240.0, 30.0)
    direction = _enum(body, "direction", ("end", "start"), "end")
    common, engine = _common(body, caps, max_duration=max_duration, lyrics_required=False)
    engine.update({
        "task_type": "repaint",
        "chunk_mask_mode": "explicit",
        "repaint_mode": _enum(body, "mode", ("conservative", "balanced", "aggressive"), "balanced"),
        "repaint_strength": _number(body, "strength", 0.0, 1.0, 0.5),
        "repaint_wav_crossfade_sec": _number(body, "crossfade", 0.0, 5.0, 0.0),
    })
    engine.pop("audio_duration", None)
    parent, pidx = _parent(body, source)
    return MusicRequest(operation="extend", source=source, parent_job_id=parent, parent_index=pidx,
                        engine=engine, extend={"seconds": seconds, "direction": direction}, **common)


def resolve_extend(req: MusicRequest, source_duration: float, max_duration: int) -> None:
    assert req.extend is not None
    seconds = float(req.extend["seconds"])
    if source_duration + seconds > max_duration:
        raise ValidationError(
            f"the extended track would be {source_duration + seconds:.0f} s; the maximum is {max_duration} s")
    if req.extend["direction"] == "end":
        req.engine["repainting_start"] = round(source_duration, 3)
        req.engine["repainting_end"] = round(source_duration + seconds, 3)
    else:
        req.engine["repainting_start"] = -round(seconds, 3)
        req.engine["repainting_end"] = 0.0


def resolve_edit_range(req: MusicRequest, source_duration: float) -> None:
    start, end = req.engine["repainting_start"], req.engine["repainting_end"]
    if start >= source_duration:
        raise ValidationError(f"'start' ({start:g} s) is past the end of the source ({source_duration:.1f} s)")
    if end > source_duration + 0.05:
        raise ValidationError(
            f"'end' ({end:g} s) is past the end of the source ({source_duration:.1f} s); use extend to add time")
