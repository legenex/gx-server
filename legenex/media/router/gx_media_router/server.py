"""HTTP ingress. The ONLY component that listens on a routable address.

Contract (OpenAI-shaped where an OpenAI shape exists; D-031):

    GET  /health                          liveness + upstream state (unauthenticated)
    GET  /v1/models                       the two aliases this router serves
    GET  /v1/workflows                    vetted templates and their parameters
    POST /v1/images/generations           text-to-image, synchronous (JSON)
    POST /v1/images/edits                 instruction edit of a source image, synchronous
                                          (multipart `image`, or JSON `image` as base64/data URL)
    POST /v1/images/variations            variation of a source image (edit with a fixed prompt)
    GET  /v1/images/{id}/content[/{i}]    fetch an image by job id
    POST /v1/videos                       text-to-video, or image-to-video when
                                          `input_reference` (multipart) / `image` (JSON) is given;
                                          asynchronous, returns an OpenAI video object
    POST /v1/videos/edits                 video-to-video: multipart `video` file, or JSON
                                          {"prompt", "video": {"id"} | base64}
    POST /v1/videos/{id}/remix            video-to-video on an earlier router job
    GET  /v1/videos                       recent video jobs
    GET  /v1/videos/{id}                  job status (OpenAI video object)
    GET  /v1/videos/{id}/content          the finished mp4 (`?variant=thumbnail` -> first frame)

Source media is validated by magic bytes and size in uploads.py and staged
under a server-generated name; nothing from a caller becomes a path.
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import re
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__, uploads, validation as v
from .config import Config
from .errors import AuthError, NotFoundError, RouterError, ValidationError
from .service import MediaService

log = logging.getLogger("gx-media.http")

_ID = r"[A-Za-z0-9\-]{1,64}"
_IMAGE_CONTENT = re.compile(rf"^/v1/images/(?P<id>{_ID})/content(?:/(?P<index>\d{{1,2}}))?$")
_VIDEO_STATUS = re.compile(rf"^/v1/videos/(?P<id>{_ID})$")
_VIDEO_CONTENT = re.compile(rf"^/v1/videos/(?P<id>{_ID})/content$")
_VIDEO_REMIX = re.compile(rf"^/v1/videos/(?P<id>{_ID})/remix$")

#: quality -> text-to-image template. "standard" is the uncensored default.
IMAGE_WORKFLOWS = {
    "standard": "qwen-image-2512-uncensored",
    "fast": "qwen-image-2512-lightning",
    "hd": "qwen-image-2512-quality",
}
EDIT_WORKFLOW = "qwen-image-edit-2511"
T2V_WORKFLOW = "wan22-t2v-a14b-uncensored"
I2V_WORKFLOW = "wan22-i2v-a14b-uncensored"
V2V_STRONG_WORKFLOW = "wan22-v2v-a14b-uncensored"
V2V_LIGHT_WORKFLOW = "wan22-v2v-a14b-light"
VARIATION_PROMPT = ("Create a variation of this image: keep the subject, composition and style, "
                    "change small details, lighting and background elements.")

#: Default strength of the image adapters when the caller does not choose.
T2I_ADAPTER_DEFAULT = 0.6
EDIT_ADAPTER_DEFAULT = 0.0


def edit_start_step(strength: float) -> tuple[str, int]:
    """Map a 0..1 edit strength onto a v2v template and its start step.

    The Wan 4-step schedule is split high-noise 0-2 / low-noise 2-4.
      strength >= 0.8  -> both experts from step 1   (strong change)
      0.45 - 0.8       -> low-noise expert from step 2 (medium)
      < 0.45           -> low-noise expert from step 3 (light)
    """
    if strength >= 0.8:
        return V2V_STRONG_WORKFLOW, 1
    if strength >= 0.45:
        return V2V_LIGHT_WORKFLOW, 2
    return V2V_LIGHT_WORKFLOW, 3


class Handler(BaseHTTPRequestHandler):
    server_version = f"gx-media-router/{__version__}"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    cfg: Config
    service: MediaService

    # -- plumbing ----------------------------------------------------------
    def log_message(self, fmt: str, *args: object) -> None:  # noqa: A003
        log.info("%s %s", self.address_string(), fmt % args)

    def _send(self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: dict, extra: dict[str, str] | None = None) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json", extra)

    def _error(self, exc: RouterError) -> None:
        extra = {"Retry-After": "30"} if exc.status == 503 else None
        self._json(exc.status, exc.payload(), extra)

    def _authenticate(self) -> None:
        if not self.cfg.api_key:
            return
        header = self.headers.get("Authorization", "")
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if not token:
            token = (self.headers.get("X-Api-Key") or "").strip()
        if not token or not hmac.compare_digest(token, self.cfg.api_key):
            raise AuthError("missing or invalid API key")

    def _length(self, limit: int) -> int:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ValidationError("invalid Content-Length") from None
        if length <= 0:
            raise ValidationError("a request body is required")
        if length > limit:
            self.close_connection = True
            raise ValidationError(f"request body exceeds {limit} bytes")
        return length

    def _body(self) -> dict:
        length = self._length(self.cfg.max_body_bytes)
        try:
            return v.require_object(json.loads(self.rfile.read(length)))
        except json.JSONDecodeError as exc:
            raise ValidationError(f"request body is not valid JSON: {exc.msg}") from None

    def _request(self) -> tuple[dict, dict[str, uploads.UploadedFile]]:
        """JSON or multipart body -> (params, files by field name).

        Upload-capable routes accept a large body; the JSON form may carry
        base64 media, so it gets the upload limit too.
        """
        ctype = self.headers.get("Content-Type", "")
        length = self._length(self.cfg.max_upload_body_bytes)
        raw = self.rfile.read(length)
        if ctype.lower().startswith("multipart/form-data"):
            fields, files = uploads.parse_multipart(ctype, raw)
            by_name: dict[str, uploads.UploadedFile] = {}
            for f in files:
                key = f.field[:-2] if f.field.endswith("[]") else f.field
                by_name.setdefault(key, f)
            return uploads.coerce_form(fields), by_name
        try:
            return v.require_object(json.loads(raw)), {}
        except json.JSONDecodeError as exc:
            raise ValidationError(f"request body is not valid JSON: {exc.msg}") from None

    def _source_bytes(self, body: dict, files: dict, names: tuple[str, ...], kind: str) -> bytes | None:
        for name in names:
            if name in files:
                return files[name].data
        for name in names:
            value = body.get(name)
            if isinstance(value, str):
                return uploads.decode_data_url(value, name)
            if isinstance(value, dict) and isinstance(value.get("b64_json"), str):
                return uploads.decode_data_url(value["b64_json"], name)
            if isinstance(value, dict) and isinstance(value.get("image_url"), str) and kind == "image":
                return uploads.decode_data_url(value["image_url"], name)
        return None

    def _validated(self, data: bytes, kind: str) -> uploads.MediaInfo:
        cfg = self.cfg
        return uploads.validate(
            data, expect=kind,
            max_bytes=cfg.max_image_upload_bytes if kind == "image" else cfg.max_video_upload_bytes,
            max_pixels=cfg.max_source_pixels, max_side=cfg.max_source_side)

    # -- routing -----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        try:
            parsed = urllib.parse.urlsplit(self.path)
            path = parsed.path
            query = urllib.parse.parse_qs(parsed.query, max_num_fields=10)
            if path in ("/health", "/healthz", "/v1/health"):
                status = self.service.health()
                self._json(200 if status["status"] == "ok" else 503, status)
                return
            self._authenticate()
            if path == "/v1/models":
                self._models()
                return
            if path == "/v1/workflows":
                self._json(200, {"object": "list", "data": [w.public() for w in self.service.workflows.all()]})
                return
            if path == "/v1/videos":
                jobs = [j.public() for j in reversed(self.service.jobs.snapshot()) if j.kind == "video"]
                self._json(200, {"object": "list", "data": jobs[:50], "has_more": len(jobs) > 50})
                return
            match = _IMAGE_CONTENT.match(path)
            if match:
                self._content(match.group("id"), int(match.group("index") or 0), None)
                return
            match = _VIDEO_CONTENT.match(path)
            if match:
                variant = (query.get("variant") or [None])[0]
                self._content(match.group("id"), 0, variant)
                return
            match = _VIDEO_STATUS.match(path)
            if match:
                self._json(200, self.service.jobs.get(match.group("id")).public())
                return
            raise NotFoundError(f"no route for GET {path}")
        except RouterError as exc:
            self._error(exc)
        except Exception:  # pragma: no cover - defensive
            log.exception("unhandled error on GET %s", self.path)
            self._json(500, {"error": {"message": "internal error", "type": "internal_error"}})

    def do_POST(self) -> None:  # noqa: N802
        try:
            path = self.path.split("?", 1)[0]
            self._authenticate()
            if path == "/v1/images/generations":
                self._images()
                return
            if path == "/v1/images/edits":
                self._image_edit(variation=False)
                return
            if path == "/v1/images/variations":
                self._image_edit(variation=True)
                return
            if path in ("/v1/videos", "/v1/videos/generations"):
                self._videos()
                return
            if path == "/v1/videos/edits":
                self._video_edit(None)
                return
            match = _VIDEO_REMIX.match(path)
            if match:
                self._video_edit(match.group("id"))
                return
            raise NotFoundError(f"no route for POST {path}")
        except RouterError as exc:
            self._error(exc)
        except Exception:  # pragma: no cover - defensive
            log.exception("unhandled error on POST %s", self.path)
            self._json(500, {"error": {"message": "internal error", "type": "internal_error"}})

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    # -- handlers ----------------------------------------------------------
    def _named_workflow(self, name: str, expected_kind: str, operations: set[str]) -> str:
        """Resolve a caller-supplied workflow name, restricted by kind and operation.

        The name only ever selects among vetted templates already loaded by the
        WorkflowRegistry (no filesystem path is built from it). The kind and
        operation checks stop a caller running, say, the video template on the
        synchronous image endpoint, or an edit template without a source.
        """
        workflow = self.service.workflows.get(name)  # raises ValidationError if unknown
        if workflow.kind != expected_kind:
            raise ValidationError(
                f"workflow {name!r} is a {workflow.kind!r} workflow, not {expected_kind!r}",
                param="workflow",
            )
        if workflow.operation not in operations:
            raise ValidationError(
                f"workflow {name!r} is a {workflow.operation!r} workflow; this endpoint runs "
                f"{'/'.join(sorted(operations))}", param="workflow")
        return workflow.name

    def _models(self) -> None:
        now = int(time.time())
        self._json(200, {
            "object": "list",
            "data": [
                {"id": "gx-image", "object": "model", "created": now, "owned_by": "gx-cluster"},
                {"id": "gx-video", "object": "model", "created": now, "owned_by": "gx-cluster"},
            ],
        })

    def _adapter(self, body: dict, default: float) -> float:
        strength = v.bounded_float(body, "adapter_strength", 0.0, 1.5)
        if strength is not None:
            return strength
        uncensored = body.get("uncensored")
        if uncensored is not None and not isinstance(uncensored, bool):
            raise ValidationError("uncensored must be true or false", param="uncensored")
        if uncensored is False:
            return 0.0
        if uncensored is True and default == 0.0:
            return 0.8
        return default

    def _image_response(self, job, fmt: str, extra: dict) -> None:
        data = []
        outputs = [a for a in job.artefacts if not a.thumbnail]
        for index in range(len(outputs)):
            if fmt == "b64_json":
                payload, _, _ = self.service.content(job, index)
                data.append({"b64_json": base64.b64encode(payload).decode("ascii")})
            else:
                data.append({"url": f"/v1/images/{job.id}/content/{index}"})
        self._json(200, {
            "created": int(time.time()),
            "data": data,
            "gx": {"id": job.id, "model": "gx-image", "workflow": job.workflow, "operation": job.operation,
                   "node": "gx10-02", **extra},
        })

    def _images(self) -> None:
        body = self._body()
        cfg = self.cfg
        quality = v.enum(body, "quality", {"standard", "hd", "auto", "fast", "low", "medium", "high"}, "standard")
        quality = {"auto": "standard", "low": "fast", "medium": "standard", "high": "hd"}.get(quality, quality)
        workflow = IMAGE_WORKFLOWS[quality]
        if isinstance(body.get("workflow"), str):
            workflow = self._named_workflow(body["workflow"], "image", {"generate"})

        width, height = v.dimensions(body, cfg, cfg.default_image_size)
        params = {
            "prompt": v.prompt(body, cfg),
            "negative_prompt": v.optional_text(body, "negative_prompt", cfg),
            "width": width,
            "height": height,
            "batch_size": v.count(body, cfg),
            "seed": v.seed(body),
            "steps": v.bounded_int(body, "steps", 1, 100),
            "cfg": v.bounded_float(body, "cfg", 0.0, 20.0),
            "sampler_name": v.sampler(body),
            "scheduler": v.scheduler(body),
            "adapter_strength": self._adapter(body, T2I_ADAPTER_DEFAULT),
        }
        fmt = v.response_format(body)
        started = time.monotonic()
        job = self.service.generate_image(workflow, params)
        self._image_response(job, fmt, {
            "size": f"{width}x{height}", "seed": params["seed"],
            "adapter_strength": params["adapter_strength"] if "adapter_strength" in
            self.service.workflows.get(workflow).bindings else None,
            "elapsed_seconds": round(time.monotonic() - started, 2),
        })

    def _image_edit(self, *, variation: bool) -> None:
        body, files = self._request()
        cfg = self.cfg
        data = self._source_bytes(body, files, ("image", "images", "input_image"), "image")
        if data is None:
            raise ValidationError("an `image` (file or base64) is required", param="image")
        info = self._validated(data, "image")
        workflow = EDIT_WORKFLOW
        if isinstance(body.get("workflow"), str):
            workflow = self._named_workflow(body["workflow"], "image", {"edit"})
        if variation:
            prompt = v.optional_text(body, "prompt", cfg) or VARIATION_PROMPT
            strength = v.bounded_float(body, "strength", 0.05, 1.0, 0.75)
        else:
            prompt = v.prompt(body, cfg)
            strength = v.bounded_float(body, "strength", 0.05, 1.0, 1.0)
        if body.get("size") not in (None, "", "auto"):
            width, height = v.dimensions(body, cfg, "1024x1024")
        else:
            width, height = uploads.fit_to_pixels(info.width or 1024, info.height or 1024,
                                                  cfg.edit_target_pixels, cfg.dimension_multiple,
                                                  cfg.max_dimension)
        staged = self.service.stage(data, info)
        params = {
            "prompt": prompt,
            "negative_prompt": v.optional_text(body, "negative_prompt", cfg),
            "input_image": staged,
            "width": width,
            "height": height,
            "seed": v.seed(body),
            "steps": v.bounded_int(body, "steps", 1, 50),
            "cfg": v.bounded_float(body, "cfg", 0.0, 20.0),
            "strength": strength,
            "adapter_strength": self._adapter(body, EDIT_ADAPTER_DEFAULT),
        }
        fmt = v.response_format(body)
        started = time.monotonic()
        job = self.service.generate_image(workflow, params, staged=(staged,),
                                          operation="variation" if variation else "edit")
        self._image_response(job, fmt, {
            "size": f"{width}x{height}", "seed": params["seed"], "strength": strength,
            "adapter_strength": params["adapter_strength"],
            "source": {"width": info.width, "height": info.height, "format": info.ext, "bytes": len(data)},
            "elapsed_seconds": round(time.monotonic() - started, 2),
        })

    def _video_common(self, body: dict, default_size: str) -> dict:
        cfg = self.cfg
        fps = v.bounded_float(body, "fps", 4.0, 30.0, 16.0)
        width, height = v.dimensions(body, cfg, default_size)
        seed = v.seed(body)
        seconds = body.get("seconds")
        if isinstance(seconds, str):
            try:
                body = {**body, "seconds": float(seconds)}
            except ValueError:
                raise ValidationError("seconds must be a number", param="seconds") from None
        return {
            "prompt": v.prompt(body, cfg),
            "negative_prompt": v.optional_text(body, "negative_prompt", cfg),
            "width": width,
            "height": height,
            "length": v.video_length(body, cfg, fps),
            "fps": fps,
            "seed": seed,
            "seed_low": seed,
        }

    def _videos(self) -> None:
        body, files = self._request()
        data = self._source_bytes(body, files, ("input_reference", "image", "input_image"), "image")
        params = self._video_common(body, "640x640")
        staged: tuple[str, ...] = ()
        if data is not None:
            info = self._validated(data, "image")
            workflow = I2V_WORKFLOW
            if isinstance(body.get("workflow"), str):
                workflow = self._named_workflow(body["workflow"], "video", {"i2v"})
            name = self.service.stage(data, info)
            params["input_image"] = name
            staged = (name,)
        else:
            workflow = T2V_WORKFLOW
            if isinstance(body.get("workflow"), str):
                workflow = self._named_workflow(body["workflow"], "video", {"generate"})
        job = self.service.submit_video(workflow, params, staged=staged)
        self._accepted(job)

    def _video_edit(self, source_id: str | None) -> None:
        cfg = self.cfg
        body, files = self._request()
        data = None if source_id else self._source_bytes(body, files, ("video", "input_video"), "video")
        ref = body.get("video")
        if source_id is None and data is None and isinstance(ref, dict) and isinstance(ref.get("id"), str):
            source_id = ref["id"]
            if not re.fullmatch(_ID, source_id):
                raise ValidationError("video.id is not a valid id", param="video")
        if data is None and source_id is None:
            raise ValidationError("a source `video` (file, base64, or {\"id\": ...}) is required", param="video")

        strength = v.bounded_float(body, "strength", 0.05, 1.0, 0.85)
        workflow, start_step = edit_start_step(strength)
        if isinstance(body.get("workflow"), str):
            workflow = self._named_workflow(body["workflow"], "video", {"v2v"})
            start_step = v.bounded_int(body, "start_step", 0, 3, start_step)
        max_seconds = v.bounded_float(body, "max_seconds", 0.5, cfg.max_edit_seconds, 6.0)
        body = {k: val for k, val in body.items() if k not in ("fps",)}
        params = self._video_common(body, "640x640")
        # The edit keeps the SOURCE frame rate (linked inside the template).
        params.pop("fps", None)
        if body.get("length") is None and body.get("seconds") is None:
            params["length"] = v.video_length({"length": 49}, cfg, 16.0)
        params.update({"start_step": start_step, "max_seconds": max_seconds, "strength": strength})

        source_job = None
        if data is not None:
            info = self._validated(data, "video")
            name = self.service.stage(data, info)
        else:
            name, source = self.service.stage_from_job(str(source_id))
            source_job = source.id
            if source.kind != "video":
                self.service.inputs.remove(name)
                raise ValidationError(f"job {source.id} is not a video", param="video")
        params["input_video"] = name
        job = self.service.submit_video(workflow, params, staged=(name,), source_job=source_job)
        self._accepted(job)

    def _accepted(self, job) -> None:
        payload = job.public()
        payload["poll_url"] = f"/v1/videos/{job.id}"
        payload["note"] = "video generation takes minutes; poll poll_url until status=completed"
        self._json(HTTPStatus.OK if self._openai_client() else HTTPStatus.ACCEPTED, payload,
                   {"Location": f"/v1/videos/{job.id}"})

    def _openai_client(self) -> bool:
        """LiteLLM's OpenAI video client expects 200 on create, like OpenAI."""
        return "multipart/form-data" in (self.headers.get("Content-Type") or "").lower() or \
            (self.headers.get("X-GX-Status-Style") or "").lower() == "openai"

    def _content(self, job_id: str, index: int, variant: str | None) -> None:
        job = self.service.jobs.get(job_id)
        payload, media_type, filename = self.service.content(job, index, variant)
        # `filename` comes from ComfyUI's own /history response (see comfy.py),
        # never from caller input; CR/LF/quotes are stripped as defence in depth.
        safe_filename = filename.replace("\r", "").replace("\n", "").replace('"', "")
        self._send(200, payload, media_type,
                   {"Content-Disposition": f'inline; filename="{safe_filename}"'})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64


def build_server(cfg: Config, service: MediaService) -> Server:
    handler = type("BoundHandler", (Handler,), {"cfg": cfg, "service": service})
    return Server((cfg.bind_host, cfg.bind_port), handler)
