"""Service adapters used by flow node executors (dependency injection).

The engine never imports the media, music, voice or gateway modules
directly; it receives a ``Services`` object. Production wires the real
``App`` objects (see ``gx_control_ui/flows/__init__.py``); tests wire stubs.
"""

from __future__ import annotations

import json
import os
import re
import stat
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..redact import redact
from ..util import HTTPError, http
from .ffmpeg import FFmpegRunner
from .store import FlowError, FlowStore

_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)
SECRET_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_\-]{0,63}$")


class Cancelled(Exception):
    """The node (or its run) was cancelled."""


class NodeFailure(Exception):
    """A user-facing node error (the message is shown in the Inspector)."""

    def __init__(self, message: str, *, code: str = "node_failed", retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


# ------------------------------------------------------------------- LLM
class LLMClient(Protocol):
    def chat(self, model: str, messages: list[dict], *, temperature: float = 0.7, max_tokens: int = 1024,
             schema: dict | None = None, timeout: float = 900) -> tuple[str, dict]: ...


class GatewayLLM:
    """Chat completions through the cluster's LiteLLM gateway (server-side key)."""

    def __init__(self, base: str, headers: Callable[[], dict[str, str]],
                 gxmax_state: Callable[[], str]) -> None:
        self.base = base.rstrip("/")
        self._headers = headers
        self._gxmax_state = gxmax_state

    def chat(self, model: str, messages: list[dict], *, temperature: float = 0.7, max_tokens: int = 1024,
             schema: dict | None = None, timeout: float = 900) -> tuple[str, dict]:
        if model == "gx-max" and self._gxmax_state() != "ready":
            raise NodeFailure("gx-max is not running. Flows never start it (it takes over both nodes); "
                              "choose gx-auto, gx-fast or gx-reason, or start gx-max in the Control Center first.",
                              code="gxmax_not_ready")
        body: dict[str, Any] = {"model": model, "messages": messages, "temperature": temperature,
                                "max_tokens": max_tokens, "stream": False}
        if schema is not None:
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": "flow_output", "schema": schema,
                                                       "strict": schema.get("additionalProperties") is False}}
        t0 = time.time()
        try:
            res = http("POST", f"{self.base}/v1/chat/completions", body=body, headers=self._headers(),
                       timeout=3700 if model in ("gx-reason", "gx-max") else timeout)
        except HTTPError as exc:
            raise NodeFailure(f"the gateway is not reachable: {exc.message}", code="gateway_unreachable",
                              retryable=True) from None
        try:
            data = res.json()
        except ValueError:
            data = None
        if not 200 <= res.status < 300 or not isinstance(data, dict):
            err = (data or {}).get("error") if isinstance(data, dict) else None
            msg = err.get("message") if isinstance(err, dict) else res.text(300)
            raise NodeFailure(f"{model} answered HTTP {res.status}: {redact(str(msg))[:300]}",
                              code="gateway_error", retryable=res.status in (429, 502, 503, 504))
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        text = _THINK.sub("", str(message.get("content") or "")).strip()
        if not text:
            raise NodeFailure(f"{model} returned an empty answer", code="empty_answer", retryable=True)
        meta = {"model_requested": model, "model_used": data.get("model"),
                "routed_to": res.headers.get("x-gx-routed-to") or res.headers.get("X-GX-Routed-To"),
                "usage": data.get("usage"), "latency_ms": round((time.time() - t0) * 1000),
                "finish_reason": choice.get("finish_reason")}
        return text, meta


def parse_json_answer(text: str) -> Any:
    """JSON from a model answer (tolerates ```json fences and prose around it)."""
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.S)
    if fence:
        cleaned = fence.group(1).strip()
    try:
        return json.loads(cleaned)
    except ValueError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = cleaned.find(opener), cleaned.rfind(closer)
        if 0 <= start < end:
            try:
                return json.loads(cleaned[start:end + 1])
            except ValueError:
                continue
    raise ValueError("the answer is not valid JSON")


# --------------------------------------------------------------- secrets
class SecretStore:
    """Named HTTP header secrets for Webhook / API Request nodes.

    Stored in one 0600 JSON file outside Git (``secrets/flows/http-secrets.json``).
    Values are write-only through the API: listings return names only.
    """

    MAX = 64

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def _read(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write(self, data: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp, self.path)

    def names(self) -> list[dict[str, Any]]:
        with self._lock:
            return [{"name": k, "updated_at": v.get("updated_at"), "length": len(v.get("value", ""))}
                    for k, v in sorted(self._read().items())]

    def set(self, name: str, value: str) -> None:
        if not SECRET_NAME.fullmatch(name or ""):
            raise FlowError("secret names are 1-64 letters, digits, _ or - (starting with a letter)")
        if not isinstance(value, str) or not value or len(value) > 4096 or any(c in value for c in "\r\n\x00"):
            raise FlowError("the secret value must be 1-4096 characters on one line")
        with self._lock:
            data = self._read()
            if name not in data and len(data) >= self.MAX:
                raise FlowError(f"at most {self.MAX} secrets can be stored")
            data[name] = {"value": value, "updated_at": time.time()}
            self._write(data)

    def delete(self, name: str) -> bool:
        with self._lock:
            data = self._read()
            if name not in data:
                return False
            del data[name]
            self._write(data)
            return True

    def get(self, name: str) -> str | None:
        with self._lock:
            entry = self._read().get(name)
        return entry.get("value") if isinstance(entry, dict) else None

    def values(self) -> list[str]:
        with self._lock:
            return [v.get("value", "") for v in self._read().values() if len(v.get("value", "")) >= 4]


# -------------------------------------------------------------- container
@dataclass
class Services:
    library: Any                       # MediaLibrary
    store: FlowStore
    llm: LLMClient
    ffmpeg: FFmpegRunner
    secrets: SecretStore
    media: Any = None                  # MediaJobs (submit/get/cancel)
    music: Any = None                  # MusicJobs (submit/get/cancel)
    voice: Callable[[], Any] = field(default=lambda: None)  # VoiceStudio or None (VOI)
    fetch: Callable[..., Any] | None = None  # netguard.fetch
    explain: Callable[[str], dict | None] = field(default=lambda alias: None)
    image_catalog: Any = None          # ImageCatalog (IMG)
    wan: Any = None                    # WanVideo (WAN): presets, resolve_preset, generate
    model_identity: Callable[[str], str] = field(default=lambda alias: "")
    music_fields: Callable[[], set[str]] = field(default=lambda: set())
    metric: Callable[..., None] = field(default=lambda event, **kw: None)
    poll_interval: float = 2.0
    media_timeout: float = 4 * 3600
    ffmpeg_timeout: float = 1800
