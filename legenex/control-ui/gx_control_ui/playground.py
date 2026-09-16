"""API playground: validated requests to the real gateway, made server-side.

The browser sends a small, validated description of the request; this module
builds the upstream body, attaches the credential, and returns the result
together with the exact body that was sent (without any credential), latency,
the model that answered and token usage.
"""

from __future__ import annotations

import base64
import json
import re
import threading
import time
import urllib.parse
from collections.abc import Generator
from typing import Any

from .config import UIConfig
from .models import ResultLog
from .redact import redact, redact_obj
from .services import Cluster
from .util import HTTPError, http, http_json

CHAT_ALIASES = ("gx-mini", "gx-fast", "gx-reason", "gx-max", "gx-auto")
VISION_ALIASES = ("gx-mini", "gx-fast", "gx-reason", "gx-auto")
IMAGE_SIZES = ("512x512", "768x768", "1024x1024", "1328x1328", "1024x768", "768x1024",
               "1328x768", "768x1328")
VIDEO_SIZES = ("480x480", "640x640", "832x480", "480x832")
MAX_PROMPT = 16000
MAX_SYSTEM = 8000
MAX_IMAGE_BYTES = 8 * 1024 * 1024
_JOB_ID = re.compile(r"^[A-Za-z0-9\-]{1,64}$")

SAMPLE_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the current weather for a city.",
        "parameters": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "City name, e.g. Cape Town"},
                "unit": {"type": "string", "enum": ["celsius", "fahrenheit"]},
            },
            "required": ["city"],
        },
    },
}

_MAGIC = {
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"\xff\xd8\xff": "image/jpeg",
    b"RIFF": "image/webp",
}


class PlaygroundError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _num(body: dict, key: str, default: float, lo: float, hi: float, integer: bool = False):
    value = body.get(key, default)
    if value is None or value == "":
        value = default
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise PlaygroundError(f"{key} must be a number")
    try:
        value = int(value) if integer else float(value)
    except ValueError:
        raise PlaygroundError(f"{key} must be a number") from None
    if not lo <= value <= hi:
        raise PlaygroundError(f"{key} must be between {lo} and {hi}")
    return value


def _text(body: dict, key: str, limit: int, required: bool = False) -> str:
    value = body.get(key) or ""
    if not isinstance(value, str):
        raise PlaygroundError(f"{key} must be a string")
    value = value.strip()
    if required and not value:
        raise PlaygroundError(f"{key} is required")
    if len(value) > limit:
        raise PlaygroundError(f"{key} exceeds {limit} characters")
    if "\x00" in value:
        raise PlaygroundError(f"{key} contains a NUL byte")
    return value


def validate_image_data_url(data_url: str) -> str:
    m = re.match(r"^data:(image/(?:png|jpeg|webp));base64,([A-Za-z0-9+/=\s]+)$", data_url or "")
    if not m:
        raise PlaygroundError("image must be a base64 data URL of type png, jpeg or webp")
    try:
        raw = base64.b64decode(m.group(2), validate=False)
    except ValueError:
        raise PlaygroundError("image is not valid base64") from None
    if len(raw) > MAX_IMAGE_BYTES:
        raise PlaygroundError("image exceeds 8 MiB")
    detected = None
    for magic, mime in _MAGIC.items():
        if raw.startswith(magic):
            detected = mime
    if detected == "image/webp" and raw[8:12] != b"WEBP":
        detected = None
    if detected != m.group(1):
        raise PlaygroundError("image content does not match its declared type")
    return f"data:{detected};base64,{base64.b64encode(raw).decode('ascii')}"


def build_chat(body: dict) -> dict:
    model = body.get("model")
    if model not in CHAT_ALIASES:
        raise PlaygroundError(f"model must be one of {', '.join(CHAT_ALIASES)}")
    prompt = _text(body, "prompt", MAX_PROMPT, required=True)
    system = _text(body, "system", MAX_SYSTEM)
    temperature = _num(body, "temperature", 0.7, 0.0, 2.0)
    max_tokens = _num(body, "max_tokens", 512, 1, 16384, integer=True)
    stream = bool(body.get("stream"))
    use_tools = bool(body.get("tools"))
    image = body.get("image")

    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    if image:
        if model not in VISION_ALIASES:
            raise PlaygroundError(f"{model} does not accept image input")
        url = validate_image_data_url(image)
        messages.append({"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": url}},
        ]})
    else:
        messages.append({"role": "user", "content": prompt})

    req: dict[str, Any] = {"model": model, "messages": messages, "temperature": temperature,
                           "max_tokens": max_tokens, "stream": stream}
    if stream:
        req["stream_options"] = {"include_usage": True}
    if use_tools:
        req["tools"] = [SAMPLE_TOOL]
        req["tool_choice"] = "auto"
    return req


def summarise_request(req: dict) -> dict:
    """The request as shown to the operator: images shortened, no credentials."""
    shown = json.loads(json.dumps(req))
    for msg in shown.get("messages", []):
        if isinstance(msg.get("content"), list):
            for part in msg["content"]:
                url = (part.get("image_url") or {}).get("url", "")
                if url.startswith("data:"):
                    part["image_url"]["url"] = url[:40] + f"...<{len(url)} chars>"
    return shown


class Playground:
    MAX_CONCURRENT = 3

    def __init__(self, cfg: UIConfig, cluster: Cluster, results: ResultLog) -> None:
        self.cfg = cfg
        self.cluster = cluster
        self.results = results
        self._slots = threading.BoundedSemaphore(self.MAX_CONCURRENT)

    def _acquire(self) -> None:
        if not self._slots.acquire(blocking=False):
            raise PlaygroundError("too many playground requests in flight; try again shortly", 429)

    def _gxmax_guard(self, req: dict, body: dict) -> None:
        if req["model"] != "gx-max":
            return
        state = self.cluster.gxmax_state()
        if state != "ready" and body.get("confirm_takeover") is not True:
            raise PlaygroundError(
                f"gx-max is {state}. A direct gx-max request acquires BOTH nodes (~9 minutes, drains "
                "every other model). Confirm the takeover to continue.", 409)

    def _timeout(self, model: str) -> float:
        return 3700.0 if model in ("gx-max", "gx-reason") else 900.0

    # ---------------------------------------------------------------- chat
    def chat(self, body: dict) -> dict:
        req = build_chat({**body, "stream": False})
        self._gxmax_guard(req, body)
        self._acquire()
        try:
            t0 = time.time()
            try:
                res = http("POST", f"{self.cfg.litellm_base}/v1/chat/completions", body=req,
                           headers=self.cluster.litellm_headers(), timeout=self._timeout(req["model"]))
            except HTTPError as exc:
                self.results.record(req["model"], "inference", False, exc.message)
                raise PlaygroundError(exc.message, 502) from None
            latency = round((time.time() - t0) * 1000)
            try:
                data = res.json()
            except ValueError:
                data = {"raw": res.text(2000)}
            ok = 200 <= res.status < 300 and isinstance(data, dict) and bool(data.get("choices"))
            choice = (data.get("choices") or [{}])[0] if isinstance(data, dict) else {}
            message = choice.get("message") or {}
            content = message.get("content") or ""
            if ok and not (content.strip() or message.get("tool_calls") or message.get("reasoning_content")):
                ok = False
            detail = (content or json.dumps(message.get("tool_calls") or "")[:200])[:200]
            self.results.record(req["model"], "inference", ok, detail if ok else res.text(300),
                                latency_ms=latency, model_used=data.get("model") if isinstance(data, dict) else None,
                                usage=(data.get("usage") if isinstance(data, dict) else None))
            return {
                "ok": ok,
                "status": res.status,
                "latency_ms": latency,
                "model_requested": req["model"],
                "model_used": data.get("model") if isinstance(data, dict) else None,
                "routed_to": res.headers.get("x-gx-routed-to") or res.headers.get("X-GX-Routed-To"),
                "usage": data.get("usage") if isinstance(data, dict) else None,
                "request": summarise_request(req),
                "response": redact_obj(data),
            }
        finally:
            self._slots.release()

    def chat_stream(self, body: dict) -> Generator[bytes, None, None]:
        """Yield the upstream SSE stream unchanged (it carries no credential),
        followed by one gx-meta event with timing."""
        req = build_chat({**body, "stream": True})
        self._gxmax_guard(req, body)
        import urllib.request

        def gen() -> Generator[bytes, None, None]:
            # The slot is taken inside the generator so that a generator that
            # is never started cannot leak it.
            if not self._slots.acquire(blocking=False):
                msg = "too many playground requests in flight; try again shortly"
                yield f"event: gx-error\ndata: {json.dumps({'error': msg})}\n\n".encode()
                return
            t0 = time.time()
            first = None
            chunks = 0
            ok = False
            try:
                data = json.dumps(req).encode()
                request = urllib.request.Request(
                    f"{self.cfg.litellm_base}/v1/chat/completions", data=data, method="POST",
                    headers={"Content-Type": "application/json", **self.cluster.litellm_headers()})
                try:
                    resp = urllib.request.urlopen(request, timeout=self._timeout(req["model"]))
                except Exception as exc:  # noqa: BLE001
                    body_txt = ""
                    if hasattr(exc, "read"):
                        try:
                            body_txt = exc.read().decode("utf-8", "replace")[:1000]
                        except Exception:  # noqa: BLE001 - the error body is optional detail
                            body_txt = ""
                    msg = redact(f"{exc} {body_txt}".strip())
                    yield f"event: gx-error\ndata: {json.dumps({'error': msg})}\n\n".encode()
                    self.results.record(req["model"], "inference", False, msg)
                    return
                with resp:
                    while True:
                        line = resp.readline()
                        if not line:
                            break
                        if first is None and line.startswith(b"data:") and b"[DONE]" not in line:
                            first = time.time()
                        if line.startswith(b"data:"):
                            chunks += 1
                        yield line
                ok = chunks > 1
            finally:
                meta = {"latency_ms": round((time.time() - t0) * 1000),
                        "ttft_ms": round((first - t0) * 1000) if first else None,
                        "chunks": chunks, "request": summarise_request(req)}
                self.results.record(req["model"], "inference", ok, f"stream {chunks} chunks",
                                    latency_ms=meta["latency_ms"])
                self._slots.release()
                yield f"event: gx-meta\ndata: {json.dumps(meta)}\n\n".encode()

        return gen()

    # --------------------------------------------------------------- image
    def image(self, body: dict) -> dict:
        prompt = _text(body, "prompt", 4000, required=True)
        size = body.get("size") or "1024x1024"
        if size not in IMAGE_SIZES:
            raise PlaygroundError(f"size must be one of {', '.join(IMAGE_SIZES)}")
        quality = body.get("quality") or "standard"
        if quality not in ("standard", "hd"):
            raise PlaygroundError("quality must be standard or hd")
        req: dict[str, Any] = {"model": "gx-image", "prompt": prompt, "size": size, "n": 1,
                               "quality": quality, "response_format": "b64_json"}
        if body.get("seed") not in (None, ""):
            req["seed"] = _num(body, "seed", 0, 0, 2**63 - 1, integer=True)
        negative = _text(body, "negative_prompt", 2000)
        if negative:
            req["negative_prompt"] = negative
        if self.cluster.gxmax_state() in ("ready", "acquiring", "releasing"):
            raise PlaygroundError("gx-image is drained while gx-max owns the cluster", 409)
        self._acquire()
        try:
            t0 = time.time()
            try:
                res = http("POST", f"{self.cfg.litellm_base}/v1/images/generations", body=req,
                           headers=self.cluster.litellm_headers(), timeout=1800)
            except HTTPError as exc:
                self.results.record("gx-image", "inference", False, exc.message)
                raise PlaygroundError(exc.message, 502) from None
            latency = round((time.time() - t0) * 1000)
            try:
                data = res.json()
            except ValueError:
                data = {"raw": res.text(1000)}
            images = []
            for item in (data.get("data") or []) if isinstance(data, dict) else []:
                b64 = item.get("b64_json")
                if b64:
                    raw = base64.b64decode(b64)
                    mime = "image/png" if raw.startswith(b"\x89PNG") else "image/jpeg"
                    images.append({"data_url": f"data:{mime};base64,{b64}", "bytes": len(raw)})
            ok = 200 <= res.status < 300 and bool(images)
            self.results.record("gx-image", "inference", ok,
                                f"{len(images)} image(s), {images[0]['bytes']} bytes" if ok else res.text(200),
                                latency_ms=latency)
            meta = {k: v for k, v in (data.items() if isinstance(data, dict) else []) if k != "data"}
            return {"ok": ok, "status": res.status, "latency_ms": latency, "request": req,
                    "images": images, "response_meta": redact_obj(meta)}
        finally:
            self._slots.release()

    # --------------------------------------------------------------- video
    def video_submit(self, body: dict) -> dict:
        prompt = _text(body, "prompt", 4000, required=True)
        seconds = _num(body, "seconds", 2.0, 0.5, 10.0)
        size = body.get("size") or "640x640"
        if size not in VIDEO_SIZES:
            raise PlaygroundError(f"size must be one of {', '.join(VIDEO_SIZES)}")
        req: dict[str, Any] = {"model": "gx-video", "prompt": prompt, "seconds": seconds, "size": size}
        if body.get("seed") not in (None, ""):
            req["seed"] = _num(body, "seed", 0, 0, 2**63 - 1, integer=True)
        if self.cluster.gxmax_state() in ("ready", "acquiring", "releasing"):
            raise PlaygroundError("gx-video is drained while gx-max owns the cluster", 409)
        try:
            status, data = http_json("POST", f"{self.cfg.media_base}/v1/videos", body=req,
                                     headers=self.cluster.media_headers(), timeout=30)
        except HTTPError as exc:
            self.results.record("gx-video", "inference", False, exc.message)
            raise PlaygroundError(exc.message, 502) from None
        if status not in (200, 202):
            raise PlaygroundError(redact(json.dumps(data))[:500], 502)
        return {"ok": True, "status": status, "request": req, "job": redact_obj(data),
                "submitted_at": time.time()}

    def video_status(self, job_id: str) -> dict:
        if not _JOB_ID.match(job_id or ""):
            raise PlaygroundError("invalid job id")
        try:
            status, data = http_json("GET", f"{self.cfg.media_base}/v1/videos/{urllib.parse.quote(job_id)}",
                                     headers=self.cluster.media_headers(), timeout=15)
        except HTTPError as exc:
            raise PlaygroundError(exc.message, 502) from None
        if status == 404:
            raise PlaygroundError("unknown video job", 404)
        if isinstance(data, dict) and data.get("status") in ("completed", "failed"):
            ok = data.get("status") == "completed"
            raw_gx = data.get("gx")
            gx: dict = raw_gx if isinstance(raw_gx, dict) else {}
            self.results.record("gx-video", "inference", ok, f"job {job_id} {data.get('status')}",
                                seconds=gx.get("elapsed_seconds"))
        return {"status": status, "job": redact_obj(data)}

    def video_content(self, job_id: str) -> tuple[bytes, str]:
        if not _JOB_ID.match(job_id or ""):
            raise PlaygroundError("invalid job id")
        try:
            res = http("GET", f"{self.cfg.media_base}/v1/videos/{urllib.parse.quote(job_id)}/content",
                       headers=self.cluster.media_headers(), timeout=60)
        except HTTPError as exc:
            raise PlaygroundError(exc.message, 502) from None
        if res.status != 200:
            raise PlaygroundError(f"video not available (HTTP {res.status})", 404 if res.status == 404 else 502)
        ctype = res.headers.get("Content-Type", "video/mp4")
        if not ctype.startswith("video/"):
            raise PlaygroundError("upstream returned a non-video payload", 502)
        return res.body, ctype
