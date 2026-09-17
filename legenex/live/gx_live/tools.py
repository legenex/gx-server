"""Application tools offered to MiniCPM-o 4.5 (strict schemas).

The model sees these definitions in its chat template's native ``<tools>``
block and answers with ``<tool_call>{"name", "arguments"}</tool_call>``. Every
call is validated here before anything runs; execution happens on gx10-01
(the Control Center), never on gx10-02 and never in the browser.

``delegate_to_gx`` can never reach gx-max: the enum is closed and the
Control Center checks the alias again before it calls the gateway.
"""

from __future__ import annotations

import re
from typing import Any

from .errors import ValidationError

DELEGATE_MODELS = ("gx-auto", "gx-fast", "gx-reason")
LIBRARY_TYPES = ("image", "video", "audio", "any")

DEFINITIONS: list[dict[str, Any]] = [
    {"type": "function", "function": {
        "name": "get_time",
        "description": "Get the current local date and time. Use it whenever the user asks about the time or date.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
    {"type": "function", "function": {
        "name": "delegate_to_gx",
        "description": ("Hand a difficult task (long writing, coding, maths, careful reasoning, research-style "
                        "answers) to a larger GX text model and speak its answer. gx-fast answers quickly, "
                        "gx-reason thinks carefully but may need minutes to load, gx-auto picks one."),
        "parameters": {"type": "object", "properties": {
            "model": {"type": "string", "enum": list(DELEGATE_MODELS)},
            "task": {"type": "string", "description": "The complete task, self-contained, in the user's words."}},
            "required": ["model", "task"], "additionalProperties": False}}},
    {"type": "function", "function": {
        "name": "search_library",
        "description": "Search the user's GX media library (generated images, videos and music) by words.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"},
            "type": {"type": "string", "enum": list(LIBRARY_TYPES)}},
            "required": ["query"], "additionalProperties": False}}},
    {"type": "function", "function": {
        "name": "fetch_url",
        "description": "Read the text of a public web page the user named (http or https).",
        "parameters": {"type": "object", "properties": {"url": {"type": "string"}},
                       "required": ["url"], "additionalProperties": False}}},
]
NAMES = tuple(d["function"]["name"] for d in DEFINITIONS)
_URL = re.compile(r"^https?://[^\s]{3,2000}$")


def _text(args: dict, key: str, lo: int, hi: int) -> str:
    value = args.get(key)
    if not isinstance(value, str):
        raise ValidationError(f"'{key}' must be a string", code="invalid_tool_arguments")
    value = value.strip()
    if not lo <= len(value) <= hi:
        raise ValidationError(f"'{key}' must be {lo}-{hi} characters", code="invalid_tool_arguments")
    if any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise ValidationError(f"'{key}' contains control characters", code="invalid_tool_arguments")
    return value


def validate(name: Any, arguments: Any) -> dict:
    """Return the normalised arguments or raise ValidationError(code=invalid_tool_*)."""
    if not isinstance(name, str) or name not in NAMES:
        raise ValidationError(f"unknown tool {str(name)[:40]!r}", code="unknown_tool")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise ValidationError("tool arguments must be an object", code="invalid_tool_arguments")
    spec = next(d["function"]["parameters"] for d in DEFINITIONS if d["function"]["name"] == name)
    extra = sorted(set(arguments) - set(spec["properties"]))
    if extra:
        raise ValidationError(f"unexpected argument(s): {', '.join(extra[:5])}", code="invalid_tool_arguments")
    missing = [k for k in spec.get("required", []) if k not in arguments]
    if missing:
        raise ValidationError(f"missing argument(s): {', '.join(missing)}", code="invalid_tool_arguments")
    if name == "get_time":
        return {}
    if name == "delegate_to_gx":
        model = arguments.get("model")
        if model not in DELEGATE_MODELS:
            raise ValidationError("model must be gx-auto, gx-fast or gx-reason", code="invalid_tool_arguments")
        return {"model": model, "task": _text(arguments, "task", 1, 4000)}
    if name == "search_library":
        kind = arguments.get("type", "any")
        if kind not in LIBRARY_TYPES:
            raise ValidationError("type must be image, video, audio or any", code="invalid_tool_arguments")
        return {"query": _text(arguments, "query", 1, 200), "type": kind}
    url = _text(arguments, "url", 8, 2000)
    if not _URL.match(url):
        raise ValidationError("url must be an http or https URL", code="invalid_tool_arguments")
    return {"url": url}


def public_arguments(name: str, arguments: dict) -> dict:
    """What the browser is shown for a call (full arguments; the owner asked for them)."""
    out = dict(arguments)
    if "task" in out and len(out["task"]) > 600:
        out["task"] = out["task"][:600] + "…"
    return out
