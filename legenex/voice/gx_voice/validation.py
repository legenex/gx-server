"""Request validation, the variant router and script chunking.

Everything that reaches the worker has passed through ``job_request``: the
stored request is exactly what runs. Voices arrive as one of four specs:

    {"kind": "preset",    "speaker": "ryan"}                       -> custom variant
    {"kind": "design",    "description": "..."}                    -> design variant
    {"kind": "reference", "reference_id": "ref-…", "transcript": "…"|None,
                          "x_vector_only": false}                   -> base variant
    {"kind": "saved",     "voice_id": "vc_…"}                       -> resolved from the voice replica
"""

from __future__ import annotations

import dataclasses
import re
import secrets
import unicodedata
from typing import Any

from .errors import ValidationError

LANGUAGES = ("auto", "english", "chinese", "german", "french", "spanish", "italian", "portuguese",
             "russian", "japanese", "korean")
#: The nine CustomVoice speakers (model card, Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice).
SPEAKERS = {
    "vivian": ("Vivian", "Bright, slightly edgy young female voice.", "chinese"),
    "serena": ("Serena", "Warm, gentle young female voice.", "chinese"),
    "uncle_fu": ("Uncle Fu", "Seasoned male voice with a low, mellow timbre.", "chinese"),
    "dylan": ("Dylan", "Youthful Beijing male voice with a clear, natural timbre.", "chinese (Beijing dialect)"),
    "eric": ("Eric", "Lively Chengdu male voice with a slightly husky brightness.", "chinese (Sichuan dialect)"),
    "ryan": ("Ryan", "Dynamic male voice with strong rhythmic drive.", "english"),
    "aiden": ("Aiden", "Sunny American male voice with a clear midrange.", "english"),
    "ono_anna": ("Ono Anna", "Playful Japanese female voice with a light, nimble timbre.", "japanese"),
    "sohee": ("Sohee", "Warm Korean female voice with rich emotion.", "korean"),
}
OPERATIONS = ("tts", "voice_design", "voice_clone", "dialogue")
FORMATS = ("wav", "mp3", "flac", "opus", "aac", "pcm")
VOICE_ID = re.compile(r"^vc_[0-9a-f]{24}$")
REF_ID = re.compile(r"^ref-[0-9a-f]{32}$")
JOB_ID = re.compile(r"^vox-[0-9a-f]{32}$")
MAX_SEGMENTS = 60
MAX_TAKES = 4
MAX_TEXT = 10000
CHUNK_CHARS = 300
SEED_MAX = 2_147_483_646
SAMPLING = {"temperature": (0.1, 2.0, float), "top_p": (0.05, 1.0, float), "top_k": (1, 200, int),
            "repetition_penalty": (1.0, 2.0, float)}
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_text(value: Any, field: str, *, max_len: int, required: bool = True, multiline: bool = True) -> str:
    if value is None or value == "":
        if required:
            raise ValidationError(f"'{field}' is required")
        return ""
    if not isinstance(value, str):
        raise ValidationError(f"'{field}' must be a string")
    text = unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n")
    text = _CTRL.sub("", text)
    if not multiline:
        text = " ".join(text.split())
    text = text.strip()
    if required and not text:
        raise ValidationError(f"'{field}' is empty")
    if len(text) > max_len:
        raise ValidationError(f"'{field}' is longer than {max_len} characters")
    return text


def _int(body: dict, key: str, default: int | None, lo: int, hi: int) -> int | None:
    v = body.get(key, default)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValidationError(f"'{key}' must be an integer")
    if not lo <= v <= hi:
        raise ValidationError(f"'{key}' must be between {lo} and {hi}")
    return v


def _num(body: dict, key: str, default: float | None, lo: float, hi: float) -> float | None:
    v = body.get(key, default)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
        raise ValidationError(f"'{key}' must be a number")
    if not lo <= float(v) <= hi:
        raise ValidationError(f"'{key}' must be between {lo} and {hi}")
    return float(v)


def language(value: Any) -> str:
    if value in (None, ""):
        return "auto"
    if not isinstance(value, str) or value.strip().lower() not in LANGUAGES:
        raise ValidationError(f"'language' must be one of: {', '.join(LANGUAGES)}")
    return value.strip().lower()


def speaker(value: Any) -> str:
    key = str(value or "").strip().lower().replace(" ", "_")
    if key not in SPEAKERS:
        raise ValidationError(f"unknown preset voice {value!r}; choose one of: {', '.join(SPEAKERS)}")
    return key


def voice_spec(value: Any, field: str = "voice") -> dict:
    if not isinstance(value, dict):
        raise ValidationError(f"'{field}' must be an object")
    kind = value.get("kind")
    if kind == "preset":
        return {"kind": "preset", "speaker": speaker(value.get("speaker"))}
    if kind == "design":
        return {"kind": "design",
                "description": clean_text(value.get("description"), f"{field}.description", max_len=1000,
                                          multiline=False)}
    if kind == "reference":
        rid = value.get("reference_id")
        if not isinstance(rid, str) or not REF_ID.match(rid):
            raise ValidationError(f"'{field}.reference_id' is invalid")
        transcript = clean_text(value.get("transcript"), f"{field}.transcript", max_len=2000, required=False)
        xvec = value.get("x_vector_only", False)
        if not isinstance(xvec, bool):
            raise ValidationError(f"'{field}.x_vector_only' must be true or false")
        return {"kind": "reference", "reference_id": rid, "transcript": transcript or None,
                "x_vector_only": xvec or not transcript}
    if kind == "saved":
        vid = value.get("voice_id")
        if not isinstance(vid, str) or not VOICE_ID.match(vid):
            raise ValidationError(f"'{field}.voice_id' is invalid")
        return {"kind": "saved", "voice_id": vid}
    raise ValidationError(f"'{field}.kind' must be preset, design, reference or saved")


def variant_for(spec: dict) -> str:
    """The router: which Qwen3-TTS checkpoint renders this voice."""
    return {"preset": "custom", "design": "design", "reference": "base"}[spec["kind"]]


@dataclasses.dataclass
class Segment:
    text: str
    voice: dict
    instructions: str
    pause_ms: int | None

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


def job_request(body: Any, *, max_chars: int) -> dict:
    """Validate a node-2 job. Returns the canonical request that is stored and run."""
    if not isinstance(body, dict):
        raise ValidationError("request body must be a JSON object")
    allowed = {"operation", "title", "language", "takes", "seed", "speed", "pause_ms", "sampling",
               "segments", "client_ref"}
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise ValidationError(f"unsupported field(s): {', '.join(unknown[:8])}")
    op = body.get("operation", "tts")
    if op not in OPERATIONS:
        raise ValidationError(f"'operation' must be one of: {', '.join(OPERATIONS)}")
    segs = body.get("segments")
    if not isinstance(segs, list) or not 1 <= len(segs) <= MAX_SEGMENTS:
        raise ValidationError(f"'segments' must be a list of 1-{MAX_SEGMENTS} items")
    if op != "dialogue" and len(segs) != 1:
        raise ValidationError("only a dialogue has more than one segment")
    segments: list[dict] = []
    total = 0
    for i, s in enumerate(segs):
        if not isinstance(s, dict):
            raise ValidationError(f"segments[{i}] must be an object")
        extra = sorted(set(s) - {"text", "voice", "instructions", "pause_ms"})
        if extra:
            raise ValidationError(f"segments[{i}]: unsupported field(s): {', '.join(extra)}")
        text = clean_text(s.get("text"), f"segments[{i}].text", max_len=MAX_TEXT)
        total += len(text)
        voice = voice_spec(s.get("voice"), f"segments[{i}].voice")
        if op == "voice_design" and voice["kind"] != "design":
            raise ValidationError("voice_design needs a design voice (a description)")
        if op == "voice_clone" and voice["kind"] != "reference":
            raise ValidationError("voice_clone needs a reference voice")
        instr = clean_text(s.get("instructions"), f"segments[{i}].instructions", max_len=500, required=False,
                           multiline=False)
        pause = _int(s, "pause_ms", None, 0, 5000)
        segments.append(Segment(text, voice, instr, pause).as_dict())
    if total > max_chars:
        raise ValidationError(f"the script is longer than {max_chars} characters")
    takes = _int(body, "takes", 1, 1, MAX_TAKES)
    seed = _int(body, "seed", None, 0, SEED_MAX)
    if seed is None:
        seed = secrets.randbelow(SEED_MAX - MAX_TAKES)
    if seed + takes - 1 > SEED_MAX:  # take i uses seed + i
        raise ValidationError(f"'seed' must be at most {SEED_MAX - takes + 1} for {takes} takes")
    sampling_in = body.get("sampling") or {}
    if not isinstance(sampling_in, dict):
        raise ValidationError("'sampling' must be an object")
    bad = sorted(set(sampling_in) - set(SAMPLING))
    if bad:
        raise ValidationError(f"unsupported sampling field(s): {', '.join(bad)}")
    sampling = {}
    for k, (lo, hi, typ) in SAMPLING.items():
        if k in sampling_in and sampling_in[k] is not None:
            sampling[k] = _int(sampling_in, k, None, int(lo), int(hi)) if typ is int else \
                _num(sampling_in, k, None, lo, hi)
    title = clean_text(body.get("title"), "title", max_len=200, required=False, multiline=False)
    client_ref = body.get("client_ref")
    if client_ref is not None and (not isinstance(client_ref, str) or not re.fullmatch(r"[A-Za-z0-9_\-#.:]{1,80}",
                                                                                      client_ref)):
        raise ValidationError("'client_ref' is invalid")
    return {
        "operation": op, "title": title, "language": language(body.get("language")),
        "takes": takes, "seed": seed,
        "speed": _num(body, "speed", 1.0, 0.5, 2.0),
        "pause_ms": _int(body, "pause_ms", 350, 0, 5000),
        "sampling": sampling, "segments": segments, "client_ref": client_ref,
    }


# ----------------------------------------------------------------- chunking
_SENT = re.compile(r"(?<=[.!?…。！？;；])\s+|(?<=[。！？；])")


def chunk_text(text: str, limit: int = CHUNK_CHARS) -> list[list[str]]:
    """Paragraphs -> lists of chunks of at most `limit` characters.

    Split at sentence ends first, then at commas, then hard at spaces. The
    model generates each chunk separately; the take joins them with a short
    breath (inside a paragraph) or the paragraph pause.
    """
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    out: list[list[str]] = []
    for para in paragraphs:
        para = " ".join(para.split())
        sentences = [s for s in _SENT.split(para) if s.strip()]
        chunks: list[str] = []
        cur = ""
        for sent in sentences:
            for piece in _split_long(sent.strip(), limit):
                if cur and len(cur) + 1 + len(piece) > limit:
                    chunks.append(cur)
                    cur = piece
                else:
                    cur = f"{cur} {piece}".strip() if cur else piece
        if cur:
            chunks.append(cur)
        out.append(chunks)
    return out


def _split_long(sentence: str, limit: int) -> list[str]:
    if len(sentence) <= limit:
        return [sentence]
    parts: list[str] = []
    cur = ""
    for clause in re.split(r"(?<=[,，、:：])\s*", sentence):
        while len(clause) > limit:
            cut = clause.rfind(" ", 0, limit)
            cut = cut if cut > limit // 2 else limit
            parts.append(clause[:cut].strip())
            clause = clause[cut:].strip()
        if cur and len(cur) + 1 + len(clause) > limit:
            parts.append(cur)
            cur = clause
        else:
            cur = f"{cur} {clause}".strip() if cur else clause
    if cur:
        parts.append(cur)
    return [p for p in parts if p]


def max_new_tokens(chunk: str) -> int:
    """A cap that a normal reading never reaches (the 12 Hz codec makes about
    12.5 codes per second; slow CJK speech is ~3 codes per character)."""
    return max(192, min(8192, 160 + 6 * len(chunk)))


def chunk_seed(take_seed: int, index: int) -> int:
    return take_seed if index == 0 else (take_seed * 31 + index) % (SEED_MAX + 1)


# ------------------------------------------------------- OpenAI speech API
SPEECH_FORMATS = {"mp3": "audio/mpeg", "opus": "audio/ogg", "aac": "audio/aac", "flac": "audio/flac",
                  "wav": "audio/wav", "pcm": "audio/L16;rate=24000;channels=1"}


def speech_request(body: Any) -> dict:
    """OpenAI `POST /v1/audio/speech`: {model, input, voice, response_format?, speed?, instructions?}."""
    if not isinstance(body, dict):
        raise ValidationError("request body must be a JSON object")
    known = {"model", "input", "voice", "response_format", "speed", "instructions", "stream_format",
             "language", "seed"}
    unknown = sorted(set(body) - known)
    if unknown:
        raise ValidationError(f"unsupported field(s): {', '.join(unknown[:8])}")
    model = body.get("model", "gx-voice")
    if model not in ("gx-voice", "openai/gx-voice"):
        raise ValidationError("model must be gx-voice")
    text = clean_text(body.get("input"), "input", max_len=4096)
    voice = body.get("voice")
    if isinstance(voice, dict):
        voice = voice.get("id")
    if not isinstance(voice, str) or not voice.strip() or len(voice) > 120:
        raise ValidationError("'voice' is required: a preset voice name, a saved voice name or a voice id")
    fmt = body.get("response_format") or "mp3"
    if fmt not in SPEECH_FORMATS:
        raise ValidationError(f"'response_format' must be one of: {', '.join(SPEECH_FORMATS)}")
    if body.get("stream_format") not in (None, "audio"):
        raise ValidationError("only stream_format 'audio' is supported")
    speed = _num(body, "speed", 1.0, 0.25, 4.0)
    if not 0.5 <= speed <= 2.0:
        raise ValidationError("'speed' must be between 0.5 and 2.0 on gx-voice")
    return {"text": text, "voice": voice.strip(), "format": fmt, "speed": speed,
            "instructions": clean_text(body.get("instructions"), "instructions", max_len=500, required=False,
                                       multiline=False),
            "language": language(body.get("language")),
            "seed": _int(body, "seed", None, 0, SEED_MAX)}


def voice_record(voice_id: str, body: Any) -> dict:
    """The node-2 replica of a saved voice (the source of truth is gx10-01)."""
    if not VOICE_ID.match(voice_id):
        raise ValidationError("invalid voice id")
    if not isinstance(body, dict):
        raise ValidationError("request body must be a JSON object")
    name = clean_text(body.get("name"), "name", max_len=80, multiline=False)
    spec = voice_spec(body.get("voice"), "voice")
    if spec["kind"] not in ("preset", "reference"):
        raise ValidationError("a saved voice is a preset or a reference voice")
    return {"id": voice_id, "name": name, "spec": spec,
            "instructions": clean_text(body.get("instructions"), "instructions", max_len=500, required=False,
                                       multiline=False),
            "language": language(body.get("language")),
            "version": _int(body, "version", 1, 1, 1_000_000)}
