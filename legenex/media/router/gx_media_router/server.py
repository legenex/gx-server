"""HTTP ingress. The ONLY component that listens on a routable address.

Contract (OpenAI-shaped where an OpenAI shape exists):
    GET  /health                       liveness + upstream state (unauthenticated)
    GET  /v1/models                    the two aliases this router serves
    POST /v1/images/generations        synchronous; returns b64_json or a URL
    GET  /v1/images/{id}/content[/{i}] fetch an image by job id
    POST /v1/videos                    asynchronous; returns 202 + job id
    GET  /v1/videos/{id}               job status
    GET  /v1/videos/{id}/content       the finished mp4
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import re
import socketserver
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__, validation as v
from .config import Config
from .errors import AuthError, NotFoundError, RouterError, ValidationError
from .service import MediaService

log = logging.getLogger("gx-media.http")

_IMAGE_CONTENT = re.compile(r"^/v1/images/(?P<id>[A-Za-z0-9\-]{1,64})/content(?:/(?P<index>\d{1,2}))?$")
_VIDEO_STATUS = re.compile(r"^/v1/videos/(?P<id>[A-Za-z0-9\-]{1,64})$")
_VIDEO_CONTENT = re.compile(r"^/v1/videos/(?P<id>[A-Za-z0-9\-]{1,64})/content$")

IMAGE_WORKFLOWS = {"standard": "qwen-image-2512-lightning", "hd": "qwen-image-2512-quality"}
VIDEO_WORKFLOW = "wan22-t2v-a14b-lightning"


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

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ValidationError("invalid Content-Length") from None
        if length <= 0:
            raise ValidationError("a JSON request body is required")
        if length > self.cfg.max_body_bytes:
            raise ValidationError(f"request body exceeds {self.cfg.max_body_bytes} bytes")
        try:
            return v.require_object(json.loads(self.rfile.read(length)))
        except json.JSONDecodeError as exc:
            raise ValidationError(f"request body is not valid JSON: {exc.msg}") from None

    # -- routing -----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        try:
            path = self.path.split("?", 1)[0]
            if path in ("/health", "/healthz", "/v1/health"):
                status = self.service.health()
                self._json(200 if status["status"] == "ok" else 503, status)
                return
            self._authenticate()
            if path == "/v1/models":
                self._models()
                return
            match = _IMAGE_CONTENT.match(path)
            if match:
                self._content(match.group("id"), int(match.group("index") or 0))
                return
            match = _VIDEO_CONTENT.match(path)
            if match:
                self._content(match.group("id"), 0)
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
            if path in ("/v1/videos", "/v1/videos/generations"):
                self._videos()
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
    def _named_workflow(self, name: str, expected_kind: str) -> str:
        """Resolve a caller-supplied workflow name, restricted to ``expected_kind``.

        The name still only ever selects among vetted templates already loaded
        by the WorkflowRegistry (no filesystem path is ever built from it), but
        without this check a caller could ask the synchronous /v1/images
        endpoint to run the video template (or vice versa): the wrong params
        would apply, the wrong timeout budget would be used, and the response
        would be mislabelled. Reject the mismatch outright instead.
        """
        workflow = self.service.workflows.get(name)  # raises ValidationError if unknown
        if workflow.kind != expected_kind:
            raise ValidationError(
                f"workflow {name!r} is a {workflow.kind!r} workflow, not {expected_kind!r}",
                param="workflow",
            )
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

    def _images(self) -> None:
        body = self._body()
        cfg = self.cfg
        quality = v.enum(body, "quality", {"standard", "hd", "auto"}, "standard")
        workflow = IMAGE_WORKFLOWS[{"auto": "standard"}.get(quality, quality)]
        if isinstance(body.get("workflow"), str):
            workflow = self._named_workflow(body["workflow"], "image")

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
        }
        fmt = v.response_format(body)
        started = time.monotonic()
        job = self.service.generate_image(workflow, params)
        elapsed = time.monotonic() - started

        data = []
        for index in range(len(job.artefacts)):
            if fmt == "b64_json":
                payload, _, _ = self.service.content(job, index)
                data.append({"b64_json": base64.b64encode(payload).decode("ascii")})
            else:
                data.append({"url": f"/v1/images/{job.id}/content/{index}"})
        self._json(200, {
            "created": int(time.time()),
            "data": data,
            "gx": {
                "id": job.id,
                "model": "gx-image",
                "workflow": workflow,
                "size": f"{width}x{height}",
                "seed": params["seed"],
                "elapsed_seconds": round(elapsed, 2),
                "node": "gx10-02",
            },
        })

    def _videos(self) -> None:
        body = self._body()
        cfg = self.cfg
        workflow = VIDEO_WORKFLOW
        if isinstance(body.get("workflow"), str):
            workflow = self._named_workflow(body["workflow"], "video")
        fps = v.bounded_float(body, "fps", 4.0, 30.0, 16.0)
        width, height = v.dimensions(body, cfg, "640x640")
        seed = v.seed(body)
        params = {
            "prompt": v.prompt(body, cfg),
            "negative_prompt": v.optional_text(body, "negative_prompt", cfg),
            "width": width,
            "height": height,
            "length": v.video_length(body, cfg, fps),
            "fps": fps,
            "seed": seed,
            "seed_low": seed,
        }
        job = self.service.submit_video(workflow, params)
        payload = job.public()
        payload["poll_url"] = f"/v1/videos/{job.id}"
        payload["note"] = "video generation takes minutes; poll poll_url until status=completed"
        self._json(HTTPStatus.ACCEPTED, payload, {"Location": f"/v1/videos/{job.id}"})

    def _content(self, job_id: str, index: int) -> None:
        job = self.service.jobs.get(job_id)
        payload, media_type, filename = self.service.content(job, index)
        # `filename` comes from ComfyUI's own /history response (see comfy.py),
        # never from caller input, so this is defense-in-depth rather than a
        # live injection path: http.server.send_header does not itself strip
        # CR/LF from header values, so a header value must never carry them.
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
