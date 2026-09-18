"""Node executors: what each catalogue node really does.

Every executor receives a ``NodeContext`` (validated config with defaults,
the input values of this iteration, cancellation, logging) and returns a
``NodeResult``. Executors only call existing cluster services through
``ctx.services``; nothing here generates media by itself except the
deterministic FFmpeg compositions.

Port values (JSON, persisted in run state and the cache)::

    {"type": "text",  "text": "..."}
    {"type": "json",  "data": ...}
    {"type": "image" | "video" | "audio", "asset_id": "a_..."}
    {"type": "voice", "voice_id": "...", "revision": ...}
    {"type": "lora",  "preset_id": "...", "revision": ..., "scale": 1.0}
"""

from __future__ import annotations

import base64
import json
import re
import shutil
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..media_jobs import JobError
from ..media_library import LibraryError, NewAsset
from ..music import MusicError
from ..redact import redact
from . import catalog as cat
from . import ffmpeg as ff
from .services import Cancelled, NodeFailure, Services, parse_json_answer

VAR_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
MAX_TEXT = 20000
#: inline image ceiling for a vision call (the gateway rejects more)
MAX_IMAGE_BYTES = 12_000_000
PREVIEW = 4000


@dataclass
class NodeResult:
    outputs: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    model: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)
    final_assets: list[str] = field(default_factory=list)


@dataclass
class NodeContext:
    services: Services
    run_id: str
    flow_id: str
    flow_name: str
    node_id: str
    node: dict[str, Any]
    nt: cat.NodeType
    config: dict[str, Any]
    inputs: dict[str, list[dict[str, Any]]]
    connected: set[str]
    variables: dict[str, str]
    user: str
    owner: str
    cancel: threading.Event
    force: bool = False
    iteration: int = 0
    iterations: int = 1
    on_log: Callable[[str], None] = field(default=lambda msg: None)
    on_status: Callable[..., None] = field(default=lambda *a, **k: None)
    on_job: Callable[[str, str], None] = field(default=lambda kind, job_id: None)

    # ------------------------------------------------------------ plumbing
    def check(self) -> None:
        if self.cancel.is_set():
            raise Cancelled()

    def log(self, message: str) -> None:
        self.on_log(redact(message)[:600])

    def status(self, state: str, detail: str = "", *, resource: dict | None = None,
               progress: float | None = None) -> None:
        self.on_status(state, detail, resource=resource, progress=progress)

    def sleep(self, seconds: float) -> None:
        if self.cancel.wait(seconds):
            raise Cancelled()

    @property
    def label(self) -> str:
        return self.node.get("label") or self.nt.label

    def title(self) -> str:
        explicit = str(self.config.get("title") or "").strip()
        if explicit:
            return explicit[:200]
        suffix = f" ({self.iteration + 1})" if self.iterations > 1 else ""
        return f"{self.flow_name} · {self.label}{suffix}"[:200]

    def render(self, text: str, extra: dict[str, str] | None = None) -> str:
        values = {**self.variables, **(extra or {})}
        missing: list[str] = []

        def sub(m: re.Match[str]) -> str:
            name = m.group(1)
            if name in values:
                return values[name]
            missing.append(name)
            return m.group(0)

        out = VAR_RE.sub(sub, text)
        if missing:
            self.log(f"unknown variable(s) left as written: {', '.join(sorted(set(missing))[:6])}")
        return out

    def texts(self, port: str) -> list[str]:
        return [str(v.get("text", "")) for v in self.inputs.get(port, []) if v.get("type") == "text"]

    def text(self, port: str | None, field_id: str | None, *, required: bool = True, sep: str = "\n\n",
             limit: int = MAX_TEXT) -> str:
        """Connected text (joined) or the field that fills the port, rendered."""
        value = ""
        if port is not None and port in self.connected:
            value = sep.join(t for t in self.texts(port) if t.strip())
        elif field_id is not None:
            value = self.render(str(self.config.get(field_id) or ""))
        value = value.strip()
        if required and not value:
            in_port = self.nt.input(port) if port else None
            what = in_port.label if in_port is not None else field_id
            raise NodeFailure(f"{self.label}: '{what}' is empty", code="missing_input")
        if len(value) > limit:
            raise NodeFailure(f"{self.label}: the text is longer than {limit} characters", code="too_long")
        return value

    def values(self, port: str, kind: str | None = None) -> list[dict[str, Any]]:
        vals = self.inputs.get(port, [])
        if kind is not None:
            bad = [v for v in vals if v.get("type") != kind]
            if bad:
                raise NodeFailure(f"{self.label}: '{port}' received {bad[0].get('type')}, expected {kind}",
                                  code="type_mismatch")
        return vals

    def one(self, port: str, kind: str | None = None, *, required: bool = True) -> dict[str, Any] | None:
        vals = self.values(port, kind)
        if not vals:
            if required:
                raise NodeFailure(f"{self.label}: nothing arrived at '{port}'", code="missing_input")
            return None
        return vals[0]

    # --------------------------------------------------------------- assets
    def asset_row(self, value: dict[str, Any], kind: str | None = None) -> dict[str, Any]:
        aid = str(value.get("asset_id") or "")
        try:
            row: dict[str, Any] = self.services.library.get(aid)
        except LibraryError as exc:
            raise NodeFailure(f"Library item {aid} is not available: {exc}", code="asset_missing") from None
        if kind is not None and row["type"] != kind:
            raise NodeFailure(f"Library item {aid} is a {row['type']}, not {kind}", code="type_mismatch")
        return row

    def media(self, value: dict[str, Any], kind: str | None = None) -> ff.Media:
        row = self.asset_row(value, kind)
        lib = self.services.library
        path = lib.file_path(row)
        if row["type"] == "audio" and row["ext"] != "wav" and "wav" in (row.get("variants") or {}):
            path = lib.file_path(row, "wav")
        if not path.is_file():
            raise NodeFailure(f"the file of {row['id']} is missing from the Library store", code="asset_missing")
        ext = path.suffix.lstrip(".")
        probed = self.services.ffmpeg.probe(path, row["type"], ext, row["id"])
        return ff.Media(path=path, ext=ext, kind=row["type"], asset_id=row["id"],
                        duration=probed.duration or row.get("duration"),
                        width=probed.width or row.get("width"), height=probed.height or row.get("height"),
                        has_audio=probed.has_audio if row["type"] != "image" else False,
                        fps=probed.fps or row.get("fps"))

    def tag(self, asset_id: str) -> None:
        self.services.store.tag_asset(asset_id, flow_id=self.flow_id, run_id=self.run_id, node_id=self.node_id)

    def provenance(self) -> dict[str, str]:
        return {"flow_id": self.flow_id, "flow_run_id": self.run_id, "flow_node_id": self.node_id}

    def save_file(self, path: Path, kind: str, *, parent: str | None, prompt: str | None = None,
                  settings: dict | None = None, variants: dict[str, Path] | None = None,
                  model: str | None = None, workflow: str | None = None) -> dict[str, Any]:
        lib = self.services.library
        ext = path.suffix.lstrip(".")
        tmp = lib.tmp_file("." + ext)
        shutil.move(str(path), tmp)
        moved: dict[str, Path] = {}
        for fmt, vpath in (variants or {}).items():
            vt = lib.tmp_file("." + fmt)
            shutil.move(str(vpath), vt)
            moved[fmt] = vt
        try:
            asset: dict[str, Any] = lib.add(NewAsset(
                type=kind, ext=ext, operation="composite", data_path=tmp, title=self.title(),
                model_alias=model, workflow=workflow, prompt=prompt, parent_id=parent,
                settings={"flow": {"node_type": self.nt.type, "node_label": self.label, **(settings or {})}},
                variant_paths=moved or None, source_kind="flow_node", source_ref=f"{self.run_id}#{self.node_id}",
                flow_id=self.flow_id, flow_run_id=self.run_id, flow_node_id=self.node_id))
        except LibraryError as exc:
            tmp.unlink(missing_ok=True)
            for p in moved.values():
                p.unlink(missing_ok=True)
            raise NodeFailure(f"saving the result failed: {exc}", code="library") from None
        return asset


def asset_value(asset: dict[str, Any]) -> dict[str, Any]:
    return {"type": asset["type"], "asset_id": asset["id"]}


def text_value(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


Executor = Callable[[NodeContext], NodeResult]
EXECUTORS: dict[str, Executor] = {}


def executor(*types: str) -> Callable[[Executor], Executor]:
    def deco(fn: Executor) -> Executor:
        for t in types:
            EXECUTORS[t] = fn
        return fn
    return deco


def _unescape(sep: str) -> str:
    return sep.replace("\\n", "\n").replace("\\t", "\t")


# ================================================================== local
@executor("text.input")
def _text_input(ctx: NodeContext) -> NodeResult:
    text = str(ctx.config.get("text") or "")
    if not text.strip():
        raise NodeFailure(f"{ctx.label}: the text is empty", code="missing_field")
    return NodeResult({"text": [text_value(text)]}, payload={"chars": len(text)})


@executor("text.prompt")
def _prompt(ctx: NodeContext) -> NodeResult:
    context = "\n\n".join(t for t in ctx.texts("context") if t.strip())
    template = str(ctx.config.get("template") or "")
    if "{{input}}" not in template.replace(" ", "") and context:
        template = f"{template}\n\n{{{{input}}}}" if template.strip() else "{{input}}"
    text = ctx.render(template, {"input": context}).strip()
    if not text:
        raise NodeFailure(f"{ctx.label}: the prompt is empty", code="missing_field")
    return NodeResult({"text": [text_value(text[:MAX_TEXT])]})


@executor("text.variables")
def _variables(ctx: NodeContext) -> NodeResult:
    data = dict(ctx.variables)
    for item in ctx.config.get("values") or []:
        data[item["key"]] = ctx.render(item["value"])
    return NodeResult({"json": [{"type": "json", "data": data}]})


@executor("text.combine")
def _combine(ctx: NodeContext) -> NodeResult:
    sep = _unescape(str(ctx.config.get("separator", "\\n\\n")))
    parts = [t for t in ctx.texts("parts") if t.strip()]
    if not parts:
        raise NodeFailure(f"{ctx.label}: nothing to combine", code="missing_input")
    return NodeResult({"text": [text_value(sep.join(parts)[:MAX_TEXT])]})


@executor("image.upload", "image.reference", "video.input", "sound.upload", "util.file_input")
def _library_input(ctx: NodeContext) -> NodeResult:
    kind = {"image.upload": "image", "image.reference": "image", "video.input": "video",
            "sound.upload": "audio"}.get(ctx.nt.type) or str(ctx.config.get("asset_type") or "")
    aid = ctx.config.get("asset_id")
    if not aid:
        raise NodeFailure(f"{ctx.label}: choose a Library item", code="missing_field")
    row = ctx.asset_row({"asset_id": aid}, kind)
    port = ctx.nt.outputs[0].id
    return NodeResult({port: [asset_value(row)]}, model=None,
                      meta={"asset": {"id": row["id"], "title": row.get("title"), "sha256": row.get("sha256")}})


@executor("image.output", "video.output", "voice.output", "music.output", "sound.output", "util.file_output")
def _output(ctx: NodeContext) -> NodeResult:
    port = ctx.nt.inputs[0].id
    ids = []
    for value in ctx.values(port):
        if value.get("type") not in cat.MEDIA_TYPES:
            raise NodeFailure(f"{ctx.label}: only media can be an output", code="type_mismatch")
        row = ctx.asset_row(value)
        lib = ctx.services.library
        title = str(ctx.config.get("title") or "").strip()
        if title or ctx.config.get("favourite"):
            lib.update(row["id"], title=title or None, favourite=True if ctx.config.get("favourite") else None)
        ctx.tag(row["id"])
        ids.append(row["id"])
    return NodeResult({}, final_assets=ids, meta={"final_assets": ids})


@executor("video.lora")
def _lora(ctx: NodeContext) -> NodeResult:
    pid = str(ctx.config.get("preset_id") or "")
    if not pid:
        raise NodeFailure(f"{ctx.label}: choose a LoRA preset", code="missing_field")
    wan = ctx.services.wan
    if wan is None:
        raise NodeFailure("Wan LoRA presets are not available on this Control Center", code="unavailable")
    try:
        preset = wan.preset(pid)
    except Exception as exc:  # noqa: BLE001 - WanError: invalid or deleted preset
        raise NodeFailure(f"LoRA preset {pid}: {exc}", code="missing_preset") from None
    value = {"type": "lora", "preset_id": pid, "revision": preset.get("updated_at") or preset.get("version"),
             "scale": float(ctx.config.get("strength_scale", 1.0)), "name": preset.get("name")}
    return NodeResult({"lora": [value]}, model=f"Wan LoRA preset {preset.get('name') or pid}")


@executor("voice.saved")
def _saved_voice(ctx: NodeContext) -> NodeResult:
    studio = _voice(ctx)
    vid = str(ctx.config.get("voice_id") or "")
    try:
        voice = studio.get_voice(vid)
    except Exception as exc:  # noqa: BLE001 - VoiceError carries a user-safe message
        raise NodeFailure(f"voice {vid}: {exc}", code="voice") from None
    return NodeResult({"voice": [{"type": "voice", "voice_id": voice.get("id", vid),
                                  "revision": voice.get("version"), "name": voice.get("name")}]},
                      model=f"gx-voice {voice.get('kind', '')}".strip())


@executor("util.delay")
def _delay(ctx: NodeContext) -> NodeResult:
    seconds = float(ctx.config.get("seconds", 0))
    ctx.status("running", f"waiting {seconds:g} s")
    ctx.sleep(seconds)
    return NodeResult({"value": list(ctx.values("value"))})


def _as_text(value: dict[str, Any]) -> str:
    if value.get("type") == "text":
        return str(value.get("text", ""))
    if value.get("type") == "json":
        data = value.get("data")
        return data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return str(value.get("asset_id") or value.get("voice_id") or value.get("preset_id") or "")


@executor("util.conditional")
def _conditional(ctx: NodeContext) -> NodeResult:
    values = ctx.values("value")
    test = ctx.config.get("test", "not_empty")
    arg = str(ctx.config.get("argument") or "")
    case = bool(ctx.config.get("case_sensitive"))
    joined = "\n".join(_as_text(v) for v in values)
    hay, needle = (joined, arg) if case else (joined.lower(), arg.lower())
    if test == "not_empty":
        result = bool(joined.strip())
    elif test == "contains":
        result = bool(needle) and needle in hay
    elif test == "equals":
        result = hay.strip() == needle.strip()
    elif test == "longer":
        try:
            result = len(joined) > int(arg or "0")
        except ValueError:
            raise NodeFailure("'Value' must be a whole number for this condition", code="bad_config") from None
    elif test == "count_at_least":
        try:
            result = len(values) >= int(arg or "1")
        except ValueError:
            raise NodeFailure("'Value' must be a whole number for this condition", code="bad_config") from None
    else:
        raise NodeFailure("unknown condition", code="bad_config")
    port = "yes" if result else "no"
    ctx.log(f"condition {test} -> {port}")
    return NodeResult({port: list(values)}, meta={"branch": port})


@executor("util.router")
def _router(ctx: NodeContext) -> NodeResult:
    values = ctx.values("value")
    if ctx.config.get("match_on") == "variable":
        name = str(ctx.config.get("variable") or "")
        subject = ctx.variables.get(name, "")
    else:
        subject = "\n".join(_as_text(v) for v in values)
    subject = subject.lower()
    chosen = "fallback"
    for route in ("route1", "route2", "route3"):
        kw = str(ctx.config.get(route) or "").strip().lower()
        if kw and kw in subject:
            chosen = route
            break
    ctx.log(f"routed to {chosen}")
    return NodeResult({chosen: list(values)}, meta={"branch": chosen})


_PATH_TOKEN = re.compile(r"([A-Za-z0-9_\-]+)|\[([0-9]{1,3})\]")


def select_path(data: Any, path: str) -> Any:
    pos = 0
    current = data
    for m in _PATH_TOKEN.finditer(path):
        gap = path[pos:m.start()]
        if gap not in ("", "."):
            raise ValueError(f"invalid path near {gap!r}")
        pos = m.end()
        key, index = m.group(1), m.group(2)
        if key is not None:
            if not isinstance(current, dict) or key not in current:
                raise KeyError(key)
            current = current[key]
        else:
            i = int(index)
            if not isinstance(current, list) or i >= len(current):
                raise KeyError(f"[{i}]")
            current = current[i]
    if pos != len(path):
        raise ValueError("invalid path")
    return current


@executor("util.select")
def _select(ctx: NodeContext) -> NodeResult:
    path = str(ctx.config.get("path") or "").strip()
    out: list[dict[str, Any]] = []
    for value in ctx.values("json", "json"):
        try:
            picked = select_path(value.get("data"), path) if path else value.get("data")
        except (KeyError, ValueError) as exc:
            raise NodeFailure(f"{ctx.label}: path '{path}' not found ({exc})", code="path_not_found") from None
        items = picked if (ctx.config.get("each") and isinstance(picked, list)) else [picked]
        for item in items[:16]:
            text = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
            out.append(text_value(text[:MAX_TEXT]))
    return NodeResult({"text": out})


@executor("util.batch")
def _batch(ctx: NodeContext) -> NodeResult:
    text = ctx.text("text", None)
    count = int(ctx.config.get("count", 3))
    suffix = str(ctx.config.get("suffix") or "")
    items = [text_value(text + (suffix.replace("{{n}}", str(i + 1)) if suffix else "")) for i in range(count)]
    return NodeResult({"text": items})


@executor("util.iterator")
def _iterator(ctx: NodeContext) -> NodeResult:
    mode = ctx.config.get("split", "lines")
    limit = int(ctx.config.get("limit", 8))
    items: list[str] = []
    for value in ctx.values("value"):
        if mode == "json":
            data = value.get("data") if value.get("type") == "json" else None
            if data is None and value.get("type") == "text":
                try:
                    data = json.loads(value.get("text", ""))
                except ValueError:
                    raise NodeFailure("the text is not a JSON list", code="bad_input") from None
            if not isinstance(data, list):
                raise NodeFailure("'JSON list items' needs a JSON list", code="bad_input")
            items += [d if isinstance(d, str) else json.dumps(d, ensure_ascii=False) for d in data]
        else:
            text = _as_text(value)
            parts = re.split(r"\n\s*\n" if mode == "paragraphs" else r"\n", text)
            items += [p.strip(" \t-*•") for p in parts if p.strip(" \t-*•")]
    if not items:
        raise NodeFailure(f"{ctx.label}: no items found", code="empty")
    if len(items) > limit:
        ctx.log(f"{len(items)} items found; only the first {limit} are used")
    return NodeResult({"text": [text_value(i[:MAX_TEXT]) for i in items[:limit]]})


# ==================================================================== LLM
def _schema_problem(value: Any, schema: dict[str, Any], where: str = "$") -> str | None:
    kind = schema.get("type")
    checks = {"object": dict, "array": list, "string": str, "boolean": bool}
    if kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"{where} must be a number"
    elif kind in checks and not isinstance(value, checks[kind]):
        return f"{where} must be a {kind}"
    if kind == "object":
        for req in schema.get("required", []):
            if req not in value:
                return f"{where}.{req} is missing"
        for k, sub in (schema.get("properties") or {}).items():
            if k in value:
                problem = _schema_problem(value[k], sub, f"{where}.{k}")
                if problem:
                    return problem
    if kind == "array":
        if "minItems" in schema and len(value) < schema["minItems"]:
            return f"{where} needs at least {schema['minItems']} items"
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            return f"{where} allows at most {schema['maxItems']} items"
        for i, item in enumerate(value):
            problem = _schema_problem(item, schema.get("items") or {}, f"{where}[{i}]")
            if problem:
                return problem
    if kind == "string" and not str(value).strip() and schema.get("minLength", 0) > 0:
        return f"{where} is empty"
    return None


def llm_json(ctx: NodeContext, messages: list[dict[str, Any]], schema: dict[str, Any], *,
             temperature: float = 0.6, max_tokens: int = 2048, repairs: int = 2) -> tuple[Any, dict]:
    """Structured output with bounded repair retries."""
    model = str(ctx.config.get("model") or "gx-auto")
    convo = list(messages)
    last = ""
    for attempt in range(repairs + 1):
        ctx.check()
        text, meta = _llm_call(ctx, model, convo, temperature=temperature, max_tokens=max_tokens, schema=schema)
        try:
            data = parse_json_answer(text)
            problem = _schema_problem(data, schema)
        except ValueError as exc:
            data, problem = None, str(exc)
        if problem is None:
            meta["repairs"] = attempt
            return data, meta
        last = problem
        ctx.log(f"answer did not match the schema ({problem}); asking for a correction")
        convo = list(messages) + [{"role": "assistant", "content": text[:6000]},
                                  {"role": "user", "content": f"That was not valid: {problem}. Reply again with "
                                   "ONLY the corrected JSON object that matches the schema."}]
    raise NodeFailure(f"{model} did not return valid structured output after {repairs + 1} attempts: {last}",
                      code="invalid_structured_output", retryable=True)


def _llm_call(ctx: NodeContext, model: str, messages: list[dict[str, Any]], **kw: Any) -> tuple[str, dict]:
    """Runs the gateway call in a helper thread so Cancel returns at once."""
    box: dict[str, Any] = {}
    done = threading.Event()

    def work() -> None:
        try:
            box["result"] = ctx.services.llm.chat(model, messages, **kw)
        except BaseException as exc:  # noqa: BLE001 - re-raised in the node thread
            box["error"] = exc
        finally:
            done.set()

    ctx.status("running", f"asking {model}")
    threading.Thread(target=work, name="flow-llm", daemon=True).start()
    while not done.wait(0.5):
        if ctx.cancel.is_set():
            ctx.log("cancelled; the gateway request is abandoned")
            raise Cancelled()
    if "error" in box:
        raise box["error"]
    text, meta = box["result"]
    ctx.log(f"{model} answered via {meta.get('routed_to') or meta.get('model_used') or model} "
            f"in {meta.get('latency_ms')} ms")
    return text, meta


def _llm_model_label(meta: dict[str, Any]) -> str:
    requested = meta.get("model_requested")
    used = meta.get("routed_to") or meta.get("model_used")
    return f"{requested} -> {used}" if used and used != requested else str(requested)


@executor("ai.llm")
def _ai_llm(ctx: NodeContext) -> NodeResult:
    instruction = ctx.render(str(ctx.config.get("instruction") or "")).strip()
    extra = "\n\n".join(t for t in ctx.texts("input") if t.strip())
    if not instruction and not extra:
        raise NodeFailure(f"{ctx.label}: write an instruction or connect text", code="missing_field")
    user = instruction + (f"\n\n{extra}" if extra else "")
    messages: list[dict[str, Any]] = []
    system = ctx.render(str(ctx.config.get("system") or "")).strip()
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": user[:30000]})
    model = str(ctx.config.get("model") or "gx-auto")
    text, meta = _llm_call(ctx, model, messages, temperature=float(ctx.config.get("temperature", 0.7)),
                           max_tokens=int(ctx.config.get("max_tokens", 1024)))
    return NodeResult({"text": [text_value(text[:MAX_TEXT])]}, model=_llm_model_label(meta), meta=meta,
                      payload={"model": model, "messages": _short_messages(messages)})


@executor("ai.vision")
def _ai_vision(ctx: NodeContext) -> NodeResult:
    """An image on a text input: read the picture, hand the answer on as text.

    This is the one conversion the catalogue could not do before, so an image
    could never reach an LLM or a prompt. The image is sent inline to a
    vision-capable alias through the same gateway every other text node uses.
    """
    row = ctx.asset_row(ctx.one("image", "image") or {}, "image")
    path = ctx.services.library.file_path(row)
    data = Path(path).read_bytes()
    if len(data) > MAX_IMAGE_BYTES:
        raise NodeFailure(f"{ctx.label}: the image is {len(data) // 1_000_000} MB; "
                          f"the gateway accepts up to {MAX_IMAGE_BYTES // 1_000_000} MB",
                          code="payload_too_large")
    mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}.get(
        str(row.get("ext") or "").lower(), "image/png")
    instruction = ctx.render(str(ctx.config.get("instruction") or "")).strip() \
        or "Describe this image in detail: the subject, the setting, the lighting and the mood."
    extra = "\n\n".join(t for t in ctx.texts("context") if t.strip())
    if extra:
        instruction = f"{instruction}\n\n{extra}"
    messages = [{"role": "user", "content": [
        {"type": "text", "text": instruction[:8000]},
        {"type": "image_url",
         "image_url": {"url": "data:" + mime + ";base64," + base64.b64encode(data).decode()}},
    ]}]
    # gx-fast is the default because it is resident and vision-capable; gx-auto
    # cannot be used blind here, since it may route to a text-only tier.
    model = str(ctx.config.get("model") or "gx-fast")
    text, meta = _llm_call(ctx, model, messages,
                           temperature=float(ctx.config.get("temperature", 0.7)),
                           max_tokens=int(ctx.config.get("max_tokens", 512)))
    return NodeResult({"text": [text_value(text[:MAX_TEXT])]}, model=_llm_model_label(meta), meta=meta,
                      payload={"model": model, "image_asset": row["id"],
                               "instruction": instruction[:PREVIEW]})


def _short_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"role": m["role"], "content": str(m["content"])[:PREVIEW]} for m in messages]


FORMATS = {"video_ad": "short video advertisement", "voiceover": "voice-over", "social": "social media video post",
           "music_video": "music video treatment"}
SYSTEM_WRITER = ("You are an award-winning direct-response copywriter and storyboard artist. You write clear, "
                 "honest, persuasive copy in plain language and precise visual prompts for image and video "
                 "generation models. Reply with JSON only.")


@executor("ai.script_writer")
def _script_writer(ctx: NodeContext) -> NodeResult:
    brief = ctx.text("brief", "brief", limit=8000)
    scenes = int(ctx.config.get("scenes", 1))
    duration = int(ctx.config.get("duration", 30))
    words = max(8, int(duration * 2.4))
    cta = str(ctx.config.get("cta") or "").strip()
    user = (
        f"Write a {FORMATS.get(str(ctx.config.get('format')), 'short video')} script.\n"
        f"Brief: {brief}\n"
        f"Length: {duration} seconds of narration, about {words} words in total.\n"
        f"Tone: {ctx.config.get('tone') or 'natural'}. Audience: {ctx.config.get('audience') or 'general'}.\n"
        f"Call to action: {cta or 'none'}.\n"
        f"Split the story into exactly {scenes} scene(s). For every scene give 'visual_prompt': a detailed "
        "prompt for an image model (subject, setting, action, camera angle, lighting, style; no written text, "
        "no logos, no subtitles in the picture) and 'narration': the words spoken during that scene.\n"
        "'narration' at the top level is the complete voice-over (all scenes, ending with the call to action), "
        "ready to be read aloud: no stage directions, no speaker names, no scene numbers."
    )
    schema = {"type": "object", "additionalProperties": False,
              "properties": {
                  "title": {"type": "string"},
                  "narration": {"type": "string", "minLength": 1},
                  "cta": {"type": "string"},
                  "scenes": {"type": "array", "minItems": scenes, "maxItems": scenes,
                             "items": {"type": "object", "additionalProperties": False,
                                       "properties": {"visual_prompt": {"type": "string", "minLength": 1},
                                                      "narration": {"type": "string"}},
                                       "required": ["visual_prompt", "narration"]}}},
              "required": ["title", "narration", "cta", "scenes"]}
    messages = [{"role": "system", "content": SYSTEM_WRITER}, {"role": "user", "content": user}]
    data, meta = llm_json(ctx, messages, schema, max_tokens=max(1024, words * 6 + scenes * 200))
    narration = str(data["narration"]).strip()
    visuals = [text_value(str(s["visual_prompt"]).strip()[:4000]) for s in data["scenes"]]
    return NodeResult({"narration": [text_value(narration[:10000])], "visuals": visuals,
                       "script": [{"type": "json", "data": data}]},
                      model=_llm_model_label(meta), meta=meta,
                      payload={"model": ctx.config.get("model"), "messages": _short_messages(messages)})


@executor("ai.scene_prompts")
def _scene_prompts(ctx: NodeContext) -> NodeResult:
    source = ctx.text("source", None, limit=12000)
    count = int(ctx.config.get("count", 4))
    style = str(ctx.config.get("style") or "").strip()
    user = (f"Create exactly {count} distinct visual scene prompts for an image or video generation model, based "
            f"on the source below. Visual style: {style or 'cinematic'}. Each prompt is one self-contained "
            "paragraph: subject, setting, action, camera, lighting. No written text or logos in the picture. "
            "Keep characters consistent between scenes.\n\nSource:\n" + source)
    schema = {"type": "object", "additionalProperties": False,
              "properties": {"prompts": {"type": "array", "minItems": count, "maxItems": count,
                                         "items": {"type": "string", "minLength": 1}}},
              "required": ["prompts"]}
    messages = [{"role": "system", "content": SYSTEM_WRITER}, {"role": "user", "content": user}]
    data, meta = llm_json(ctx, messages, schema, max_tokens=300 * count + 200)
    return NodeResult({"prompts": [text_value(str(p).strip()[:4000]) for p in data["prompts"]]},
                      model=_llm_model_label(meta), meta=meta,
                      payload={"model": ctx.config.get("model"), "messages": _short_messages(messages)})


ENHANCE = {
    "image": "a detailed prompt for a text-to-image model: subject, setting, composition, camera and lens, "
             "lighting, colour palette, style; comma-separated phrases; no written text in the image",
    "video": "a detailed prompt for a text-to-video model: subject, action and motion, camera movement, "
             "setting, lighting, pacing; one paragraph",
    "music": "a style prompt for a music model: genre, mood, instruments, tempo feel, production; one line",
    "voice": "a short delivery instruction for a text-to-speech voice: emotion, pace, energy, tone; one line",
}


@executor("ai.prompt_enhancer")
def _enhancer(ctx: NodeContext) -> NodeResult:
    idea = ctx.text("text", None, limit=4000)
    target = str(ctx.config.get("target") or "image")
    hints = str(ctx.config.get("style") or "").strip()
    messages = [
        {"role": "system", "content": "You rewrite short ideas into excellent generation prompts. Reply with the "
                                      "prompt only: no preamble, no quotes, no markdown."},
        {"role": "user", "content": f"Write {ENHANCE[target]}.\nIdea: {idea}"
                                    + (f"\nStyle hints: {hints}" if hints else "")},
    ]
    model = str(ctx.config.get("model") or "gx-auto")
    text, meta = _llm_call(ctx, model, messages, temperature=0.7, max_tokens=600)
    text = text.strip().strip('"').strip()
    return NodeResult({"text": [text_value(text[:4000])]}, model=_llm_model_label(meta), meta=meta,
                      payload={"model": model, "messages": _short_messages(messages)})


STRUCT_SCHEMA: dict[str, dict[str, Any]] = {
    "string": {"type": "string"}, "number": {"type": "number"}, "boolean": {"type": "boolean"},
    "list": {"type": "array", "items": {"type": "string"}},
}


@executor("ai.structured")
def _structured(ctx: NodeContext) -> NodeResult:
    text = ctx.text("text", None, limit=20000)
    fields: list[dict[str, str]] = list(ctx.config.get("schema") or [])
    if not fields:
        raise NodeFailure(f"{ctx.label}: define at least one field", code="missing_field")
    props: dict[str, dict[str, Any]] = {f["key"]: dict(STRUCT_SCHEMA[f["value"]]) for f in fields}
    schema = {"type": "object", "additionalProperties": False, "properties": props, "required": list(props)}
    instruction = ctx.render(str(ctx.config.get("instruction") or "Extract the fields."))
    messages = [
        {"role": "system", "content": "You extract structured data. Reply with a JSON object only."},
        {"role": "user", "content": f"{instruction}\nFields: {json.dumps(props)}\n\nText:\n{text}"},
    ]
    data, meta = llm_json(ctx, messages, schema, temperature=0.2, max_tokens=1500)
    return NodeResult({"json": [{"type": "json", "data": data}]}, model=_llm_model_label(meta), meta=meta,
                      payload={"model": ctx.config.get("model"), "messages": _short_messages(messages)})


# ========================================================== media queue
def _media_job(ctx: NodeContext, body: dict[str, Any], alias: str,
               submit: Callable[[dict[str, Any], str], dict[str, Any]] | None = None
               ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    media = ctx.services.media
    if media is None:
        raise NodeFailure("the media queue is not available", code="unavailable")
    user = f"flow:{ctx.user}"
    deadline = time.time() + ctx.services.media_timeout
    job = None
    while job is None:
        ctx.check()
        try:
            job = submit(body, user) if submit is not None else media.submit(body, user=user)
        except JobError as exc:
            if exc.status == 429:
                ctx.status("waiting", "the media queue is full; waiting for a free slot",
                           resource={"code": "queue_full", "reason": str(exc)})
                ctx.sleep(max(5.0, ctx.services.poll_interval))
                if time.time() > deadline:
                    raise NodeFailure("the media queue stayed full", code="queue_full", retryable=True) from None
                continue
            raise NodeFailure(str(exc), code=exc.code or "media_rejected") from None
        except ValueError as exc:  # WanError from a preset submission
            raise NodeFailure(str(exc), code=str(getattr(exc, "code", "media_rejected"))) from None
    job_id = job["id"]
    ctx.on_job("media", job_id)
    ctx.log(f"media job {job_id} queued ({job.get('label')})")
    last_phase = None
    while True:
        if ctx.cancel.is_set():
            try:
                media.cancel(job_id, user=user)
                ctx.log(f"media job {job_id} cancelled before it started")
            except JobError as exc:
                ctx.log(f"media job {job_id}: {exc}; its result is not used by this run")
            raise Cancelled()
        job = media.get(job_id)
        phase = job.get("phase")
        if phase != last_phase:
            ctx.log(f"media job {job_id}: {phase} {job.get('detail') or ''}".strip())
            last_phase = phase
        if phase == "ready":
            break
        if phase == "failed":
            raise NodeFailure(f"{alias} failed: {job.get('error') or 'unknown error'}",
                              code=str(job.get("error_code") or "generation_failed"), retryable=True)
        if phase == "cancelled":
            raise Cancelled()
        if phase == "waiting":
            ctx.status("waiting", str((job.get("waiting") or {}).get("reason") or job.get("detail") or "waiting"),
                       resource=job.get("waiting"))
        elif phase == "queued":
            pos = job.get("queue_position")
            ctx.status("queued", f"in the media queue{f' (position {pos})' if pos else ''}")
        else:
            ctx.status("running", str(job.get("detail") or phase), resource=None)
        if time.time() > deadline:
            raise NodeFailure(f"{alias} did not finish in time", code="timeout", retryable=True)
        ctx.cancel.wait(ctx.services.poll_interval)  # the loop top cancels the job itself
    assets = []
    for aid in job.get("assets") or []:
        ctx.tag(aid)
        assets.append(ctx.services.library.get(aid))
    if not assets:
        raise NodeFailure(f"{alias} finished without a result", code="no_output", retryable=True)
    return assets, job


def _model_label(alias: str, asset: dict[str, Any]) -> str:
    parts = [alias]
    if asset.get("workflow"):
        parts.append(str(asset["workflow"]))
    if asset.get("model_repo"):
        parts.append(str(asset["model_repo"]).split(" (")[0])
    return " · ".join(parts)


def _seed(ctx: NodeContext) -> int | None:
    seed = ctx.config.get("seed")
    if seed is None:
        return None
    return int(seed) + ctx.iteration


def _prompt_for(ctx: NodeContext, *, required: bool = True) -> str:
    return ctx.text("prompt", "prompt", required=required, limit=4000)


def _image_model(ctx: NodeContext, operation: str) -> dict[str, Any] | None:
    """The chosen gx-image model from IMG's catalogue (None = the catalogue default)."""
    model_id = str(ctx.config.get("image_model") or "")
    catalog = ctx.services.image_catalog
    if catalog is None:
        if model_id:
            raise NodeFailure("choosing the image model is not supported on this Control Center",
                              code="unsupported")
        return None
    kind = {"generate": "t2i", "edit": "edit", "variation": "variation"}[operation]
    try:
        model: dict[str, Any] = catalog.model(kind, model_id or None)
    except ValueError as exc:
        raise NodeFailure(str(exc), code="unknown_model") from None
    return model


def _put(body: dict[str, Any], ctx: NodeContext, *keys: str) -> None:
    for k in keys:
        if ctx.config.get(k) not in (None, ""):
            body[k] = ctx.config[k]


@executor("image.generate")
def _image_generate(ctx: NodeContext) -> NodeResult:
    body: dict[str, Any] = {"kind": "t2i", "prompt": _prompt_for(ctx), "n": int(ctx.config.get("count", 1)),
                            "title": ctx.title()}
    model = _image_model(ctx, "generate")
    size = str(ctx.config.get("size") or "")
    quality = str(ctx.config.get("quality") or "")
    if model is not None:
        body["image_model"] = model["id"]
        if size and model.get("sizes") and size not in model["sizes"]:
            raise NodeFailure(f"{model['label']} does not render {size}; choose one of "
                              f"{', '.join(model['sizes'])}", code="bad_size")
        if quality and not model.get("qualities"):
            ctx.log(f"{model['label']} has no quality presets; 'Quality' is not used")
            quality = ""
    if size:
        body["size"] = size
    if quality:
        body["quality"] = quality
    if ctx.config.get("negative_prompt"):
        body["negative_prompt"] = ctx.render(str(ctx.config["negative_prompt"]))
    _put(body, ctx, "steps", "guidance")
    if _seed(ctx) is not None:
        body["seed"] = _seed(ctx)
    assets, job = _media_job(ctx, body, "gx-image")
    label = (model or {}).get("label")
    return NodeResult({"image": [asset_value(a) for a in assets]},
                      model=_model_label("gx-image", assets[0]) + (f" · {label}" if label else ""),
                      meta={"job": job["id"], "seconds": job.get("elapsed_seconds")},
                      payload={"media_job": dict(body)})


@executor("image.edit", "image.img2img", "image.character")
def _image_edit(ctx: NodeContext) -> NodeResult:
    port = "character" if ctx.nt.type == "image.character" else "image"
    source = ctx.asset_row(ctx.one(port, "image") or {}, "image")
    prompt = _prompt_for(ctx, required=ctx.nt.type == "image.edit")
    kind = "variation" if ctx.nt.type == "image.img2img" else "edit"
    body: dict[str, Any] = {"kind": kind, "source_id": source["id"], "title": ctx.title()}
    if ctx.nt.type == "image.character":
        scene = prompt or "a new natural pose in a new setting"
        prompt = ("Keep exactly the same person: identical face, hair, skin tone, body and clothing style. "
                  f"Show them in a new scene: {scene}")
        body["edit_mode"] = "change"
    model = _image_model(ctx, "variation" if kind == "variation" else "edit")
    if model is not None and ctx.config.get("image_model"):
        body["image_model"] = model["id"]
    if prompt:
        body["prompt"] = prompt
    _put(body, ctx, "strength", "steps", "edit_mode", "edit_quality")
    if ctx.config.get("negative_prompt"):
        body["negative_prompt"] = ctx.render(str(ctx.config["negative_prompt"]))
    if _seed(ctx) is not None:
        body["seed"] = _seed(ctx)
    assets, job = _media_job(ctx, body, "gx-image")
    return NodeResult({"image": [asset_value(a) for a in assets]}, model=_model_label("gx-image", assets[0]),
                      meta={"job": job["id"]}, payload={"media_job": body})


def _video_body(ctx: NodeContext, kind: str) -> dict[str, Any]:
    body: dict[str, Any] = {"kind": kind, "size": ctx.config.get("size", "832x480"),
                            "seconds": float(ctx.config.get("seconds", 3)), "fps": int(ctx.config.get("fps", 16)),
                            "title": ctx.title()}
    if ctx.config.get("negative_prompt"):
        body["negative_prompt"] = ctx.render(str(ctx.config["negative_prompt"]))
    if _seed(ctx) is not None:
        body["seed"] = _seed(ctx)
    return body


def _lora_choice(ctx: NodeContext) -> tuple[str | None, float | None]:
    lora = ctx.one("lora", "lora", required=False) if "lora" in ctx.connected else None
    if lora:
        return str(lora.get("preset_id")), lora.get("scale")
    preset = str(ctx.config.get("lora_preset") or "")
    return (preset or None), None


def _wan_job(ctx: NodeContext, preset_id: str, scale: float | None, body: dict[str, Any]) -> tuple[list, dict]:
    """Text to video through WAN's preset contract (resolve_preset + generate)."""
    wan = ctx.services.wan
    if wan is None:
        raise NodeFailure("Wan LoRA presets are not available on this Control Center", code="unavailable")
    overrides = {k: body[k] for k in ("prompt", "negative_prompt", "seed", "size", "seconds", "fps", "title")
                 if body.get(k) not in (None, "")}
    overrides.update(ctx.provenance())
    try:
        resolved = wan.resolve_preset(preset_id, overrides)
    except Exception as exc:  # noqa: BLE001 - WanError carries a user-safe message
        raise NodeFailure(f"LoRA preset {preset_id}: {exc}", code=str(getattr(exc, "code", "preset"))) from None
    if not resolved.get("valid"):
        raise NodeFailure(f"LoRA preset {resolved['preset']['name']}: {'; '.join(resolved.get('warnings') or [])}",
                          code="preset_invalid")
    wan_body = resolved["body"]
    if scale not in (None, 1.0):
        for item in wan_body.get("loras") or []:
            for key in ("strength_high", "strength_low"):
                if isinstance(item.get(key), (int, float)):
                    item[key] = round(float(item[key]) * float(scale or 1.0), 3)
    ctx.log(f"LoRA preset {resolved['preset']['name']} resolved "
            f"({len([x for x in wan_body.get('loras') or [] if x.get('enabled', True)])} LoRA(s))")
    return _media_job(ctx, wan_body, "gx-video", submit=lambda b, user: wan.generate(b, user=user))


@executor("video.generate", "video.t2v", "video.i2v")
def _video(ctx: NodeContext) -> NodeResult:
    image = None
    if ctx.nt.input("image") is not None and ("image" in ctx.connected or ctx.nt.type == "video.i2v"):
        image = ctx.one("image", "image", required=ctx.nt.type == "video.i2v")
    kind = "i2v" if image else "t2v"
    body = _video_body(ctx, kind)
    prompt = _prompt_for(ctx, required=kind == "t2v")
    body["prompt"] = prompt or "subtle natural motion, gentle camera movement"
    preset, scale = _lora_choice(ctx) if ctx.nt.input("lora") is not None else (None, None)
    if preset and kind != "t2v":
        raise NodeFailure("Wan LoRA presets apply to text to video only; disconnect the start image or the preset",
                          code="lora_t2v_only")
    if image:
        body["source_id"] = ctx.asset_row(image, "image")["id"]
    if preset:
        assets, job = _wan_job(ctx, preset, scale, body)
    else:
        assets, job = _media_job(ctx, body, "gx-video")
    return NodeResult({"video": [asset_value(a) for a in assets]},
                      model=_model_label("gx-video", assets[0]) + (" + LoRA preset" if preset else ""),
                      meta={"job": job["id"], "seconds": job.get("elapsed_seconds"), "lora_preset": preset},
                      payload={"media_job": body, "lora_preset": preset})


def _compose(ctx: NodeContext, job: ff.FFJob, *, parent: str | None, settings: dict[str, Any] | None = None,
             prompt: str | None = None) -> dict[str, Any]:
    ctx.status("running", job.describe)
    try:
        result = ctx.services.ffmpeg.run(job, cancel=ctx.cancel, timeout=ctx.services.ffmpeg_timeout, log=ctx.log)
    except ff.ComposeError as exc:
        if str(exc) == "cancelled":
            raise Cancelled() from None
        raise NodeFailure(str(exc), code="compose_failed") from None
    try:
        main = result.files["main"]
        variants = {role: p for role, p in result.files.items() if role != "main"}
        asset = ctx.save_file(main, job.kind, parent=parent, prompt=prompt, variants=variants or None,
                              settings={"ffmpeg": job.describe, "inputs": [m.asset_id for m in job.inputs],
                                        "seconds": result.seconds, **(settings or {})},
                              workflow=f"ffmpeg:{job.describe}"[:120])
    finally:
        shutil.rmtree(result.workdir, ignore_errors=True)
    ctx.log(f"saved {asset['id']} ({job.describe}, {result.seconds} s)")
    return asset


@executor("video.extend")
def _video_extend(ctx: NodeContext) -> NodeResult:
    video = ctx.media(ctx.one("video", "video") or {}, "video")
    frame = _compose(ctx, ff.last_frame(video), parent=video.asset_id, settings={"role": "extend: last frame"})
    body = _video_body(ctx, "i2v")
    if video.width and video.height:
        size = f"{video.width}x{video.height}"
        from ..media_jobs import VIDEO_SIZES
        if size in VIDEO_SIZES:
            body["size"] = size
    body["prompt"] = _prompt_for(ctx, required=False) or "the scene continues naturally"
    body["source_id"] = frame["id"]
    assets, job = _media_job(ctx, body, "gx-video")
    tail = ctx.media(asset_value(assets[0]), "video")
    joined = _compose(ctx, ff.concat([video, tail], "first", int(round(video.fps or 16))), parent=video.asset_id,
                      settings={"role": "extend: joined", "continuation": tail.asset_id})
    return NodeResult({"video": [asset_value(joined)]}, model=_model_label("gx-video", assets[0]) + " + ffmpeg",
                      meta={"job": job["id"], "frame": frame["id"], "continuation": tail.asset_id},
                      payload={"media_job": body})


# ================================================================ ffmpeg
@executor("image.upscale")
def _upscale(ctx: NodeContext) -> NodeResult:
    image = ctx.media(ctx.one("image", "image") or {}, "image")
    asset = _compose(ctx, ff.upscale_image(image, int(ctx.config.get("factor", "2"))), parent=image.asset_id)
    return NodeResult({"image": [asset_value(asset)]}, model="ffmpeg lanczos (not AI)")


def _medias(ctx: NodeContext, port: str, kind: str | None = None) -> list[ff.Media]:
    vals = ctx.values(port, kind)
    if not vals:
        raise NodeFailure(f"{ctx.label}: nothing arrived at '{port}'", code="missing_input")
    return [ctx.media(v, kind) for v in vals]


@executor("compose.merge_audio")
def _merge_audio(ctx: NodeContext) -> NodeResult:
    clips = _medias(ctx, "audio", "audio")
    asset = _compose(ctx, ff.merge_audio(clips, float(ctx.config.get("gap", 0))), parent=clips[0].asset_id)
    return NodeResult({"audio": [asset_value(asset)]}, model="ffmpeg")


@executor("compose.mix_audio")
def _mix_audio(ctx: NodeContext) -> NodeResult:
    tracks = _medias(ctx, "audio", "audio")
    job = ff.mix_audio(tracks, str(ctx.config.get("length", "longest")), float(ctx.config.get("volume_db", -8)))
    asset = _compose(ctx, job, parent=tracks[0].asset_id)
    return NodeResult({"audio": [asset_value(asset)]}, model="ffmpeg")


@executor("compose.add_voice", "compose.add_music", "compose.add_sfx")
def _add_audio(ctx: NodeContext) -> NodeResult:
    video = ctx.media(ctx.one("video", "video") or {}, "video")
    audio = ctx.media(ctx.one("audio", "audio") or {}, "audio")
    role = ctx.nt.type.rsplit("_", 1)[1]
    c = ctx.config
    job = ff.add_audio(video, audio, role=role, volume_db=float(c.get("volume_db", 0)),
                       offset=float(c.get("offset", 0)), keep_original=bool(c.get("keep_original", True)),
                       fit=str(c.get("fit", "video")) if role == "voice" else "video",
                       loop=bool(c.get("loop", False)) if role == "music" else False,
                       fade_out=float(c.get("fade_out", 0)) if role == "music" else 0.0)
    asset = _compose(ctx, job, parent=video.asset_id, settings={"audio": audio.asset_id})
    return NodeResult({"video": [asset_value(asset)]}, model="ffmpeg")


@executor("compose.trim", "compose.fade_in", "compose.fade_out", "compose.volume", "compose.normalize",
          "compose.resize", "compose.crop")
def _one_clip(ctx: NodeContext) -> NodeResult:
    value = ctx.one("media") or {}
    media = ctx.media(value)
    c = ctx.config
    t = ctx.nt.type
    if t == "compose.trim":
        job = ff.trim(media, float(c.get("start", 0)), c.get("end"))
    elif t in ("compose.fade_in", "compose.fade_out"):
        job = ff.fade(media, t.rsplit("_", 1)[1], float(c.get("duration", 1)))
    elif t == "compose.volume":
        job = ff.volume(media, float(c.get("gain_db", 0)))
    elif t == "compose.normalize":
        job = ff.normalize(media, float(c.get("target_lufs", -16)))
    elif t == "compose.resize":
        if media.kind == "audio":
            raise NodeFailure("Resize needs an image or a video", code="type_mismatch")
        job = ff.resize(media, int(c.get("width", 1280)), int(c.get("height", 720)), str(c.get("fit", "contain")))
    else:
        if media.kind == "audio":
            raise NodeFailure("Crop needs an image or a video", code="type_mismatch")
        job = ff.crop(media, str(c.get("aspect", "9:16")), str(c.get("anchor", "center")))
    try:
        asset = _compose(ctx, job, parent=media.asset_id)
    except ff.ComposeError as exc:
        raise NodeFailure(str(exc), code="compose_failed") from None
    return NodeResult({"media": [asset_value(asset)]}, model="ffmpeg")


@executor("compose.concat")
def _concat(ctx: NodeContext) -> NodeResult:
    clips = _medias(ctx, "video", "video")
    job = ff.concat(clips, str(ctx.config.get("size", "first")), int(ctx.config.get("fps", 16)))
    asset = _compose(ctx, job, parent=clips[0].asset_id)
    return NodeResult({"video": [asset_value(asset)]}, model="ffmpeg")


@executor("compose.overlay")
def _overlay(ctx: NodeContext) -> NodeResult:
    video = ctx.media(ctx.one("video", "video") or {}, "video")
    image = ctx.media(ctx.one("image", "image") or {}, "image")
    c = ctx.config
    job = ff.overlay(video, image, str(c.get("position", "top-right")), int(c.get("scale", 20)),
                     float(c.get("opacity", 1)), float(c.get("start", 0)), c.get("end"))
    asset = _compose(ctx, job, parent=video.asset_id, settings={"overlay": image.asset_id})
    return NodeResult({"video": [asset_value(asset)]}, model="ffmpeg")


@executor("compose.captions")
def _captions(ctx: NodeContext) -> NodeResult:
    video = ctx.media(ctx.one("video", "video") or {}, "video")
    text = ctx.text("text", "text", limit=300 if "text" not in ctx.connected else 2000)
    c = ctx.config
    start, end = float(c.get("start", 0)), c.get("end")
    if c.get("last_seconds") is not None:
        duration = video.duration or 0
        if duration <= 0:
            raise NodeFailure("the video duration is unknown", code="no_duration")
        start, end = max(0.0, duration - float(c["last_seconds"])), None
    job = ff.captions(video, text, str(c.get("position", "bottom")), float(c.get("font_size", 6)),
                      start, end, bool(c.get("box", True)))
    asset = _compose(ctx, job, parent=video.asset_id, prompt=text[:500])
    return NodeResult({"video": [asset_value(asset)]}, model="ffmpeg drawtext")


@executor("compose.subtitles")
def _subtitles(ctx: NodeContext) -> NodeResult:
    video = ctx.media(ctx.one("video", "video") or {}, "video")
    text = ctx.text("text", "text")
    job = ff.subtitles(video, text, int(ctx.config.get("font_size", 22)))
    asset = _compose(ctx, job, parent=video.asset_id, prompt=text[:500])
    return NodeResult({"video": [asset_value(asset)]}, model="ffmpeg subtitles (libass)")


@executor("compose.export")
def _export(ctx: NodeContext) -> NodeResult:
    video = ctx.media(ctx.one("video", "video") or {}, "video")
    audio_value = ctx.one("audio", "audio", required=False)
    audio = ctx.media(audio_value, "audio") if audio_value else None
    fps = ctx.config.get("fps")
    job = ff.export(video, audio, str(ctx.config.get("preset", "source")), str(ctx.config.get("quality", "standard")),
                    int(fps) if fps else None)
    asset = _compose(ctx, job, parent=video.asset_id, settings={"audio": audio.asset_id if audio else None})
    if ctx.config.get("favourite"):
        ctx.services.library.update(asset["id"], favourite=True)
    return NodeResult({"video": [asset_value(asset)]}, model="ffmpeg libx264", final_assets=[asset["id"]],
                      meta={"final_assets": [asset["id"]]})


# ================================================================= music
MUSIC_TERMINAL_OK = "completed"


def _music_job(ctx: NodeContext, body: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    music = ctx.services.music
    if music is None:
        raise NodeFailure("gx-music is not available", code="unavailable")
    user = f"flow:{ctx.user}"
    body = {k: v for k, v in body.items() if v not in (None, "", [])}
    try:
        job = music.submit("generate", body, user=user, via="flow")
    except MusicError as exc:
        raise NodeFailure(f"gx-music refused the request: {exc}", code=exc.code,
                          retryable=exc.status in (503,)) from None
    job_id = job["id"]
    ctx.on_job("music", job_id)
    ctx.log(f"music job {job_id} submitted")
    deadline = time.time() + ctx.services.media_timeout
    last = None
    while True:
        if ctx.cancel.is_set():
            try:
                music.cancel(job_id, user=user)
                ctx.log(f"music job {job_id} cancelled")
            except MusicError as exc:
                ctx.log(f"music job {job_id}: cancel refused ({exc})")
            raise Cancelled()
        try:
            job = music.get(job_id)
        except MusicError as exc:
            if exc.status == 503:
                ctx.status("waiting", "gx10-02 music service is not reachable; retrying",
                           resource={"code": "node_unavailable", "reason": str(exc)})
                ctx.cancel.wait(max(5.0, ctx.services.poll_interval))
                continue
            raise NodeFailure(str(exc), code=exc.code) from None
        status = job.get("status")
        phase = job.get("phase") or status
        if phase != last:
            ctx.log(f"music job {job_id}: {phase} {job.get('phase_detail') or ''}".strip())
            last = phase
        if status == "completed" and job.get("imported") and job.get("library_assets"):
            break
        if status == "completed" and job.get("import_error"):
            raise NodeFailure(f"saving the track failed: {job['import_error']}", code="import_failed",
                              retryable=True)
        if status == "failed":
            err = job.get("error") or {}
            raise NodeFailure(f"gx-music failed: {err.get('message') or 'unknown error'}",
                              code=str(err.get("code") or "generation_failed"), retryable=True)
        if status == "cancelled":
            raise Cancelled()
        if status == "waiting_for_resource":
            waiting = job.get("waiting") or {}
            ctx.status("waiting", str(waiting.get("reason") or job.get("detail") or "waiting for gx10-02 memory"),
                       resource=waiting)
        elif status == "queued":
            ctx.status("queued", str(job.get("detail") or "queued on gx10-02"))
        else:
            ctx.status("running", str(job.get("phase_detail") or job.get("detail") or phase),
                       progress=job.get("progress") if isinstance(job.get("progress"), (int, float)) else None)
        if time.time() > deadline:
            raise NodeFailure("gx-music did not finish in time", code="timeout", retryable=True)
        ctx.cancel.wait(ctx.services.poll_interval)  # the loop top cancels the job itself
    assets = []
    for aid in job.get("library_assets") or []:
        ctx.tag(aid)
        assets.append(ctx.services.library.get(aid))
    return assets, job


def _music_body(ctx: NodeContext, *, description: str, instrumental: bool, lyrics: str = "") -> dict[str, Any]:
    c = ctx.config
    body: dict[str, Any] = {"title": ctx.title()[:120], "description": description[:512],
                            "style_tags": list(c.get("style_tags") or [])[:24],
                            "duration": int(c.get("duration", 30)), "instrumental": instrumental,
                            "batch_size": 1}
    style_prompt = ctx.render(str(c.get("style_prompt") or "")).strip()
    if style_prompt:
        body["prompt"] = style_prompt[:512]
    if not instrumental:
        body["vocal_language"] = c.get("vocal_language", "en")
        if lyrics.strip():
            body["lyrics"] = lyrics[:4096]
        elif "lyrics_source" in ctx.services.music_fields():
            body["lyrics_source"] = "planner"
        else:
            raise NodeFailure("a song with vocals needs lyrics: connect or write lyrics, or switch on Instrumental",
                              code="lyrics_required")
    else:
        body["lyrics"] = "[Instrumental]"
    if c.get("bpm") is not None:
        body["bpm"] = int(c["bpm"])
    seed = _seed(ctx)
    if seed is not None:
        body["seed"] = seed % (2**31 - 1)
    if not body.get("description") and not body.get("prompt") and not body["style_tags"]:
        raise NodeFailure(f"{ctx.label}: describe the music (description, style prompt or tags)",
                          code="missing_field")
    return body


def _music_result(assets: list[dict[str, Any]], job: dict[str, Any], body: dict[str, Any]) -> NodeResult:
    model = job.get("model") or {}
    label = "gx-music · " + str(model.get("dit_name") or "ACE-Step 1.5 XL")
    return NodeResult({"audio": [asset_value(a) for a in assets]}, model=label,
                      meta={"job": job.get("id"), "timings": job.get("timings")}, payload={"music_job": body})


@executor("music.generate")
def _music_generate(ctx: NodeContext) -> NodeResult:
    description = ctx.text("description", "description", required=False, limit=2000)
    lyrics = ctx.text("lyrics", "lyrics", required=False, limit=6000)
    instrumental = bool(ctx.config.get("instrumental", True))
    body = _music_body(ctx, description=description, instrumental=instrumental, lyrics=lyrics)
    assets, job = _music_job(ctx, body)
    return _music_result(assets, job, body)


@executor("music.prompt")
def _music_prompt(ctx: NodeContext) -> NodeResult:
    prompt = ctx.text("prompt", None, limit=2000)
    body = _music_body(ctx, description=prompt, instrumental=bool(ctx.config.get("instrumental", True)))
    assets, job = _music_job(ctx, body)
    return _music_result(assets, job, body)


@executor("music.lyrics")
def _music_lyrics(ctx: NodeContext) -> NodeResult:
    lyrics = ctx.text("lyrics", None, limit=6000)
    description = ctx.render(str(ctx.config.get("description") or ""))
    body = _music_body(ctx, description=description, instrumental=False, lyrics=lyrics)
    assets, job = _music_job(ctx, body)
    return _music_result(assets, job, body)


@executor("music.instrumental")
def _music_instrumental(ctx: NodeContext) -> NodeResult:
    description = ctx.text("description", "description", required=False, limit=2000)
    body = _music_body(ctx, description=description, instrumental=True)
    assets, job = _music_job(ctx, body)
    return _music_result(assets, job, body)


@executor("sound.ambient")
def _ambient(ctx: NodeContext) -> NodeResult:
    description = ctx.render(str(ctx.config.get("description") or "")).strip()
    if not description:
        raise NodeFailure(f"{ctx.label}: describe the ambience", code="missing_field")
    body = _music_body(ctx, description=f"ambient soundscape: {description}", instrumental=True)
    body["style_tags"] = list(dict.fromkeys(["ambient", "atmospheric", *body.get("style_tags", [])]))[:24]
    assets, job = _music_job(ctx, body)
    result = _music_result(assets, job, body)
    result.model = (result.model or "gx-music") + " (instrumental ambience)"
    return result


# ================================================================= voice
def _voice(ctx: NodeContext) -> Any:
    studio = ctx.services.voice()
    if studio is None:
        raise NodeFailure("gx-voice is not installed on this Control Center yet", code="unavailable")
    return studio


def _voice_job(ctx: NodeContext, body: dict[str, Any]) -> dict[str, Any]:
    studio = _voice(ctx)
    user = f"flow:{ctx.user}"
    body = {**body, "flow": ctx.provenance(), "auto_save": True}
    try:
        job: dict[str, Any] = studio.submit(body, user=user, via="flow")
    except Exception as exc:  # noqa: BLE001 - VoiceError has a user-safe message and .status
        raise NodeFailure(f"gx-voice refused the request: {exc}", code=str(getattr(exc, "code", "voice")),
                          retryable=getattr(exc, "status", 400) == 503) from None
    job_id = job["id"]
    ctx.on_job("voice", job_id)
    ctx.log(f"voice job {job_id} submitted ({body.get('operation')})")
    deadline = time.time() + ctx.services.media_timeout
    last = None
    while True:
        if ctx.cancel.is_set():
            try:
                studio.cancel(job_id, user=user)
                ctx.log(f"voice job {job_id} cancelled")
            except Exception as exc:  # noqa: BLE001
                ctx.log(f"voice job {job_id}: cancel refused ({exc})")
            raise Cancelled()
        job = studio.get(job_id)
        status = job.get("status")
        if status != last:
            ctx.log(f"voice job {job_id}: {status} {job.get('detail') or ''}".strip())
            last = status
        if status == "completed":
            break
        if status == "failed":
            err = job.get("error") or {}
            raise NodeFailure(f"gx-voice failed: {err.get('message') or 'unknown error'}",
                              code=str(err.get("code") or "voice_failed"), retryable=bool(err.get("retryable")))
        if status == "cancelled":
            raise Cancelled()
        if status == "waiting_for_resource":
            waiting = job.get("waiting") or {}
            ctx.status("waiting", str(waiting.get("reason") or job.get("detail") or "waiting for gx10-02"),
                       resource=waiting)
        elif status == "queued":
            ctx.status("queued", str(job.get("detail") or "queued"))
        else:
            ctx.status("running", str(job.get("detail") or status),
                       progress=job.get("progress") if isinstance(job.get("progress"), (int, float)) else None)
        if time.time() > deadline:
            raise NodeFailure("gx-voice did not finish in time", code="timeout", retryable=True)
        ctx.cancel.wait(ctx.services.poll_interval)  # the loop top cancels the job itself
    for note in job.get("notes") or []:
        ctx.log(f"gx-voice note: {note}")
    return job


def _take_asset(ctx: NodeContext, job: dict[str, Any], take: int = 0) -> dict[str, Any]:
    takes = job.get("takes") or []
    if take >= len(takes):
        raise NodeFailure("gx-voice returned no audio", code="no_output", retryable=True)
    aid = takes[take].get("asset_id")
    if not aid:
        studio = _voice(ctx)
        asset = studio.save_take(job["id"], take, user=f"flow:{ctx.user}", title=ctx.title(),
                                 flow=ctx.provenance())
        aid = asset["id"]
    ctx.tag(aid)
    row: dict[str, Any] = ctx.services.library.get(aid)
    return row


def _voice_label(job: dict[str, Any]) -> str:
    variant = (job.get("model") or {}).get("variant") if isinstance(job.get("model"), dict) else None
    return "gx-voice · Qwen3-TTS 1.7B" + (f" {variant}" if variant else "")


@executor("voice.tts")
def _tts(ctx: NodeContext) -> NodeResult:
    text = ctx.text("text", "text", limit=10000)
    voice = ctx.one("voice", "voice", required=False) if "voice" in ctx.connected else None
    voice_id = (voice or {}).get("voice_id") or ctx.config.get("voice_id")
    if not voice_id:
        raise NodeFailure(f"{ctx.label}: choose a voice or connect one", code="missing_field")
    body: dict[str, Any] = {"operation": "tts", "text": text, "voice_id": voice_id, "title": ctx.title(),
                            "language": ctx.config.get("language", "auto")}
    style = ctx.render(str(ctx.config.get("style") or "")).strip()
    if style:
        body["instructions"] = style[:500]
    seed = _seed(ctx)
    if seed is not None:
        body["seed"] = seed % 2147483647
    job = _voice_job(ctx, body)
    asset = _take_asset(ctx, job)
    return NodeResult({"audio": [asset_value(asset)]}, model=_voice_label(job),
                      meta={"job": job["id"], "timings": job.get("timings"), "duration_s": asset.get("duration")},
                      payload={"voice_job": {**body, "text": text[:PREVIEW]}})


@executor("voice.design")
def _voice_design(ctx: NodeContext) -> NodeResult:
    studio = _voice(ctx)
    description = ctx.render(str(ctx.config.get("description") or "")).strip()
    if not description:
        raise NodeFailure(f"{ctx.label}: describe the voice", code="missing_field")
    sample = ctx.render(str(ctx.config.get("sample_text") or "Hello, this is how I sound.")).strip()
    body = {"operation": "voice_design", "text": sample[:1000], "description": description[:1000],
            "language": ctx.config.get("language", "auto"), "title": ctx.title()}
    job = _voice_job(ctx, body)
    preview = _take_asset(ctx, job)
    name = str(ctx.config.get("name") or "").strip() or f"{ctx.flow_name} voice"[:80]
    try:
        voice = studio.create_voice({"kind": "designed", "name": name[:80], "description": description[:1000],
                                     "job_id": job["id"], "take": 0}, user=f"flow:{ctx.user}")
    except Exception as exc:  # noqa: BLE001
        raise NodeFailure(f"saving the designed voice failed: {exc}", code="voice") from None
    ctx.log(f"designed voice saved as {voice.get('id')}")
    return NodeResult({"voice": [{"type": "voice", "voice_id": voice["id"], "revision": voice.get("version"),
                                  "name": voice.get("name")}],
                       "preview": [asset_value(preview)]},
                      model=_voice_label(job) + " VoiceDesign", meta={"job": job["id"], "voice": voice.get("id")},
                      payload={"voice_job": body})


@executor("voice.clone")
def _voice_clone(ctx: NodeContext) -> NodeResult:
    studio = _voice(ctx)
    if not ctx.config.get("consent"):
        raise NodeFailure("confirm that you have the speaker's consent before cloning a voice", code="consent")
    ref = ctx.asset_row(ctx.one("reference", "audio") or {}, "audio")
    name = str(ctx.config.get("name") or "").strip()
    body: dict[str, Any] = {"kind": "cloned", "name": name[:80], "reference_asset_id": ref["id"],
                            "consent": {"confirmed": True,
                                        "statement": f"Confirmed in Creative Flows by {ctx.user} "
                                                     f"(flow {ctx.flow_id}, node {ctx.node_id})."}}
    transcript = str(ctx.config.get("reference_text") or "").strip()
    if transcript:
        body["transcript"] = transcript[:2000]
    try:
        voice = studio.create_voice(body, user=f"flow:{ctx.user}")
    except Exception as exc:  # noqa: BLE001
        raise NodeFailure(f"voice cloning failed: {exc}", code=str(getattr(exc, "code", "voice"))) from None
    return NodeResult({"voice": [{"type": "voice", "voice_id": voice["id"], "revision": voice.get("version"),
                                  "name": voice.get("name")}]},
                      model="gx-voice · Qwen3-TTS 1.7B Base (clone)", meta={"voice": voice.get("id")},
                      payload={"voice": {k: v for k, v in body.items() if k != "consent"}})


_LINE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9 _\-]{0,40})\s*:\s*(.+?)\s*$")


def parse_dialogue(script: str, speakers: dict[str, str]) -> list[dict[str, str]]:
    segments: list[dict[str, str]] = []
    lookup = {k.strip().lower(): v for k, v in speakers.items()}
    for raw in script.splitlines():
        if not raw.strip():
            continue
        m = _LINE.match(raw)
        if not m:
            raise NodeFailure(f"dialogue lines look like 'NAME: text' (got {raw.strip()[:40]!r})",
                              code="bad_script")
        name, text = m.group(1).strip(), m.group(2)
        voice = lookup.get(name.lower())
        if not voice:
            raise NodeFailure(f"no voice is assigned to speaker {name!r} (Speakers setting)", code="missing_voice")
        segments.append({"voice_id": voice, "text": text})
    if not segments:
        raise NodeFailure("the dialogue script is empty", code="missing_input")
    if len(segments) > 60:
        raise NodeFailure("a dialogue can have at most 60 lines", code="too_long")
    return segments


@executor("voice.dialogue")
def _dialogue(ctx: NodeContext) -> NodeResult:
    script = ctx.text("script", "script", limit=10000)
    speakers = {i["key"]: i["value"] for i in ctx.config.get("speakers") or []}
    segments = parse_dialogue(script, speakers)
    pause = int(ctx.config.get("pause_ms", 350))
    for s in segments:
        s["pause_ms"] = pause  # type: ignore[assignment]
    body = {"operation": "dialogue", "segments": segments, "pause_ms": pause, "title": ctx.title(),
            "language": ctx.config.get("language", "auto")}
    job = _voice_job(ctx, body)
    asset = _take_asset(ctx, job)
    return NodeResult({"audio": [asset_value(asset)]}, model=_voice_label(job) + " (dialogue)",
                      meta={"job": job["id"], "lines": len(segments)}, payload={"voice_job": body})


# ================================================================== HTTP
def _headers(ctx: NodeContext) -> tuple[dict[str, str], list[str]]:
    headers: dict[str, str] = {}
    names = []
    for item in ctx.config.get("headers") or []:
        value = ctx.services.secrets.get(item["secret"])
        if value is None:
            raise NodeFailure(f"secret {item['secret']!r} is not stored (Creative Flows > Secrets)",
                              code="missing_secret")
        headers[item["header"]] = value
        names.append(f"{item['header']}: <secret {item['secret']}>")
    return headers, names


def _scrub(text: str, ctx: NodeContext) -> str:
    for secret in ctx.services.secrets.values():
        text = text.replace(secret, "[secret]")
    return redact(text)


def _http(ctx: NodeContext, method: str, url: str, body: bytes | None, content_type: str | None) -> dict[str, Any]:
    fetch = ctx.services.fetch
    if fetch is None:
        raise NodeFailure("outbound requests are disabled", code="unavailable")
    headers, shown = _headers(ctx)
    if content_type:
        headers["Content-Type"] = content_type
    headers.setdefault("Accept", "application/json, text/plain;q=0.8")
    ctx.status("running", f"{method} {url.split('?')[0][:80]}")
    from ..netguard import BlockedURL
    try:
        res = fetch(url, method=method, body=body, headers=headers, timeout=float(ctx.config.get("timeout", 15)),
                    max_bytes=256 * 1024)
    except BlockedURL as exc:
        raise NodeFailure(f"request refused: {exc}", code="blocked_url") from None
    except OSError as exc:
        raise NodeFailure(f"request failed: {type(exc).__name__}", code="network", retryable=True) from None
    text = _scrub(res.text(), ctx)
    try:
        parsed: Any = json.loads(text) if text.strip() else None
    except ValueError:
        parsed = None
    ctx.log(f"{method} {res.url.split('?')[0][:100]} -> HTTP {res.status}"
            + (" (truncated)" if res.truncated else ""))
    if not 200 <= res.status < 300:
        raise NodeFailure(f"{method} answered HTTP {res.status}: {text[:200]}", code="http_status",
                          retryable=res.status in (429, 502, 503, 504))
    return {"status": res.status, "url": res.url, "json": parsed,
            "text": None if parsed is not None else text[:20000], "shown_headers": shown}


def _summary(ctx: NodeContext, value: dict[str, Any]) -> dict[str, Any]:
    kind = value.get("type")
    if kind == "text":
        return {"type": "text", "text": str(value.get("text", ""))[:MAX_TEXT]}
    if kind == "json":
        return {"type": "json", "data": value.get("data")}
    if kind in cat.MEDIA_TYPES:
        row = ctx.asset_row(value)
        return {"type": kind, "asset_id": row["id"], "title": row.get("title"), "media_type": row["media_type"],
                "bytes": row["file_size"], "sha256": row.get("sha256"), "duration": row.get("duration"),
                "download": f"/v1/assets/{row['id']}/content"}
    return {k: v for k, v in value.items() if k in ("type", "voice_id", "preset_id", "name")}


@executor("util.webhook")
def _webhook(ctx: NodeContext) -> NodeResult:
    url = str(ctx.config.get("url") or "")
    payload = {"event": "gx.flow.node", "flow_id": ctx.flow_id, "flow_name": ctx.flow_name, "run_id": ctx.run_id,
               "node_id": ctx.node_id, "sent_at": time.time(),
               "items": [_summary(ctx, v) for v in ctx.values("value")]}
    raw = json.dumps(payload).encode()
    if len(raw) > 512 * 1024:
        raise NodeFailure("the webhook payload is larger than 512 KiB", code="too_large")
    res = _http(ctx, "POST", url, raw, "application/json")
    shown = res.pop("shown_headers")
    return NodeResult({"response": [{"type": "json", "data": res}]}, model="netguard HTTP",
                      payload={"method": "POST", "url": url, "headers": shown, "body_bytes": len(raw)})


@executor("util.api_request")
def _api_request(ctx: NodeContext) -> NodeResult:
    method = str(ctx.config.get("method") or "GET")
    url = ctx.render(str(ctx.config.get("url") or ""))
    body: bytes | None = None
    ctype = None
    value = ctx.one("body", required=False) if "body" in ctx.connected else None
    if value is not None and method != "GET":
        if value.get("type") == "json":
            body, ctype = json.dumps(value.get("data")).encode(), "application/json"
        else:
            body, ctype = str(value.get("text", "")).encode(), "text/plain; charset=utf-8"
        if len(body) > 512 * 1024:
            raise NodeFailure("the request body is larger than 512 KiB", code="too_large")
    res = _http(ctx, method, url, body, ctype)
    shown = res.pop("shown_headers")
    return NodeResult({"response": [{"type": "json", "data": res}]}, model="netguard HTTP",
                      payload={"method": method, "url": url, "headers": shown,
                               "body_bytes": len(body) if body else 0})


def check_registry() -> list[str]:
    """Available catalogue types without an executor (must be empty)."""
    return sorted(t for t, n in cat.NODES.items() if n.available and t not in EXECUTORS)


assert not check_registry(), check_registry()  # noqa: S101 - import-time invariant
