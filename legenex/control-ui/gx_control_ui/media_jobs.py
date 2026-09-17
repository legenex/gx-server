"""Create-page jobs: generation and editing through the node-2 media router (D-034).

The browser never talks to the router or ComfyUI. It submits a validated job
here; one worker thread runs jobs in order (ComfyUI runs one generation at a
time anyway), calls the router over the RoCE fabric with the media key,
downloads the result and registers it in the media library with its full
metadata and lineage.

Job phases shown to the user (D-037):
    queued -> waiting (resource) -> generating -> saving -> ready
                                                           | failed | cancelled

Before a job is sent, the Resource Controller's gate is asked whether
gx10-02 can take it now (maintenance, gx-max, memory). A job that cannot
start waits with a human-readable reason instead of failing, and the gate
may free idle tenants when the active profile allows it. A job the router
still refuses for memory goes back to waiting (bounded).

"ready" is only set after the output file was downloaded, validated
(PNG/MP4 magic, non-trivial size) and committed to the library.
"""

from __future__ import annotations

import base64
import collections
import json
import logging
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .media_library import LibraryError, MediaLibrary, NewAsset
from .redact import redact
from .util import HTTPError, bearer, http

log = logging.getLogger("gx.ui.media")

KINDS = ("t2i", "edit", "variation", "t2v", "i2v", "v2v")
ALIAS = {"t2i": "gx-image", "edit": "gx-image", "variation": "gx-image",
         "t2v": "gx-video", "i2v": "gx-video", "v2v": "gx-video"}
TERMINAL = ("ready", "failed", "cancelled")
WAIT_LIMIT_S = 3600
WAIT_POLL_S = 10
KIND_LABEL = {"t2i": "Generate image", "edit": "Edit image", "variation": "Image variation",
              "t2v": "Generate video", "i2v": "Image to video", "v2v": "Edit video"}
IMAGE_SIZES = ("1328x1328", "1024x1024", "1328x800", "800x1328", "1664x928", "928x1664", "768x768", "512x512")
VIDEO_SIZES = ("640x640", "832x480", "480x832", "704x704", "512x512")
MAX_PROMPT = 4000
MAX_SEED = 2**63 - 1


class JobError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class MediaJob:
    id: str
    kind: str
    params: dict
    user: str
    created: float = field(default_factory=time.time)
    phase: str = "queued"
    detail: str = ""
    started: float | None = None
    ended: float | None = None
    router_job: str | None = None
    assets: list[str] = field(default_factory=list)
    error: str | None = None
    elapsed_generation: float | None = None
    waiting: dict | None = None
    cancel_requested: bool = False
    cold: bool | None = None

    @property
    def alias(self) -> str:
        return ALIAS[self.kind]

    @property
    def variant(self) -> str | None:
        if self.kind == "v2v" and float(self.params.get("strength") or 0.85) >= 0.5:
            return "keyframe_edit"
        return None

    def public(self) -> dict:
        return {"id": self.id, "kind": self.kind, "label": KIND_LABEL[self.kind], "phase": self.phase,
                "alias": self.alias, "waiting": self.waiting, "cancel_requested": self.cancel_requested,
                "cold_start": self.cold,
                "detail": self.detail, "created": self.created, "started": self.started, "ended": self.ended,
                "elapsed_seconds": round((self.ended or time.time()) - (self.started or self.created), 1),
                "router_job": self.router_job, "assets": list(self.assets), "error": self.error,
                "prompt": (self.params.get("prompt") or "")[:300], "source_id": self.params.get("source_id"),
                "params": {k: v for k, v in self.params.items() if k != "prompt"}}


def _text(body: dict, key: str, required: bool = False, limit: int = MAX_PROMPT) -> str | None:
    value = body.get(key)
    if value is None or value == "":
        if required:
            raise JobError(f"{key} is required")
        return None
    if not isinstance(value, str) or "\x00" in value:
        raise JobError(f"{key} must be text")
    value = value.strip()
    if required and not value:
        raise JobError(f"{key} is required")
    if len(value) > limit:
        raise JobError(f"{key} is longer than {limit} characters")
    return value


def _num(body: dict, key: str, lo: float, hi: float, *, integer: bool = False) -> float | int | None:
    value = body.get(key)
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise JobError(f"{key} must be a number")
    try:
        number = int(value) if integer else float(value)
    except (TypeError, ValueError):
        raise JobError(f"{key} must be a number") from None
    if integer and isinstance(value, float) and not value.is_integer():
        raise JobError(f"{key} must be a whole number")
    if not lo <= number <= hi:
        raise JobError(f"{key} must be between {lo:g} and {hi:g}")
    return number


def _choice(body: dict, key: str, allowed: tuple[str, ...], default: str) -> str:
    value = body.get(key) or default
    if value not in allowed:
        raise JobError(f"{key} must be one of {', '.join(allowed)}")
    return str(value)


def validate(kind: str, body: dict) -> dict:
    """Server-side validation of a Create request. Returns clean params."""
    if kind not in KINDS:
        raise JobError(f"unknown job kind {kind!r}")
    p: dict[str, Any] = {"kind": kind}
    needs_prompt = kind not in ("variation",)
    p["prompt"] = _text(body, "prompt", required=needs_prompt)
    p["negative_prompt"] = _text(body, "negative_prompt")
    p["seed"] = _num(body, "seed", 0, MAX_SEED, integer=True)
    p["title"] = _text(body, "title", limit=200)
    uncensored = body.get("uncensored", True)
    if not isinstance(uncensored, bool):
        raise JobError("uncensored must be true or false")
    p["uncensored"] = uncensored
    p["adapter_strength"] = _num(body, "adapter_strength", 0.0, 1.5)
    if kind == "t2i":
        p["size"] = _choice(body, "size", IMAGE_SIZES, "1328x1328")
        p["n"] = _num(body, "n", 1, 4, integer=True) or 1
        p["quality"] = _choice(body, "quality", ("standard", "fast", "hd"), "standard")
        p["steps"] = _num(body, "steps", 1, 100, integer=True)
        p["guidance"] = _num(body, "guidance", 0.0, 20.0)
    if kind in ("edit", "variation"):
        p["strength"] = _num(body, "strength", 0.05, 1.0)
        p["steps"] = _num(body, "steps", 1, 50, integer=True)
    if kind in ("t2v", "i2v", "v2v"):
        p["size"] = _choice(body, "size", VIDEO_SIZES, "640x640")
        p["seconds"] = _num(body, "seconds", 0.5, 10.0) or 3.0
    if kind in ("t2v", "i2v"):
        p["fps"] = _num(body, "fps", 8, 24, integer=True) or 16
    if kind == "v2v":
        p["strength"] = _num(body, "strength", 0.05, 1.0) or 0.85
    if kind in ("edit", "variation", "i2v", "v2v"):
        source = body.get("source_id")
        if not isinstance(source, str):
            raise JobError("choose a source asset from the library")
        p["source_id"] = source
    return {k: v for k, v in p.items() if v is not None}


def _multipart(fields: dict[str, Any], files: list[tuple[str, str, str, Path]]) -> tuple[str, bytes]:
    boundary = uuid.uuid4().hex
    chunks: list[bytes] = []
    for k, v in fields.items():
        if v is None:
            continue
        value = json.dumps(v) if isinstance(v, bool) else str(v)
        chunks.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{value}\r\n".encode())
    for field_name, filename, ctype, path in files:
        chunks.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field_name}\"; "
                      f"filename=\"{filename}\"\r\nContent-Type: {ctype}\r\n\r\n".encode()
                      + path.read_bytes() + b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return f"multipart/form-data; boundary={boundary}", b"".join(chunks)


class RouterClient:
    def __init__(self, base: str, key_fn) -> None:
        self.base = base.rstrip("/")
        self._key = key_fn

    def _headers(self, extra: dict | None = None) -> dict:
        return {**bearer(self._key()), **(extra or {})}

    def post_json(self, path: str, body: dict, timeout: float) -> dict:
        res = http("POST", self.base + path, body=body, headers=self._headers(), timeout=timeout)
        return self._decode(res)

    def post_multipart(self, path: str, ctype: str, data: bytes, timeout: float) -> dict:
        res = http("POST", self.base + path, raw_body=data, headers=self._headers({"Content-Type": ctype}),
                   timeout=timeout)
        return self._decode(res)

    def get_json(self, path: str, timeout: float = 30) -> dict:
        return self._decode(http("GET", self.base + path, headers=self._headers(), timeout=timeout))

    def get_bytes(self, path: str, timeout: float = 600) -> tuple[bytes, str]:
        res = http("GET", self.base + path, headers=self._headers(), timeout=timeout)
        if res.status != 200:
            raise JobError(f"router returned HTTP {res.status} for content: {res.text(300)}", 502)
        return res.body, res.headers.get("Content-Type", "")

    @staticmethod
    def _decode(res) -> dict:
        try:
            data = res.json()
        except ValueError:
            data = {}
        if not 200 <= res.status < 300:
            err = data.get("error") if isinstance(data, dict) else None
            msg = err.get("message") if isinstance(err, dict) else (err if isinstance(err, str) else None)
            raise JobError(f"media router HTTP {res.status}: {msg or res.text(300)}", 502)
        if not isinstance(data, dict):
            raise JobError("media router returned an unexpected response", 502)
        return data


class MediaJobs:
    def __init__(self, library: MediaLibrary, router: RouterClient, *, results=None,
                 model_identity=None, audit=None, poll_interval: float = 3.0, gate=None,
                 wait_poll: float = WAIT_POLL_S, wait_limit: float = WAIT_LIMIT_S) -> None:
        self.library = library
        self.router = router
        self.results = results
        self.model_identity: Any = model_identity or (lambda workflow: {})
        self.audit = audit or (lambda **kw: None)
        self.poll_interval = poll_interval
        #: gate(alias, variant) -> None (go) | wait-reason dict (Resource Control)
        self.gate = gate
        self.wait_poll = wait_poll
        self.wait_limit = wait_limit
        self._jobs: collections.OrderedDict[str, MediaJob] = collections.OrderedDict()
        self._queue: collections.deque[str] = collections.deque()
        self._cv = threading.Condition()
        self._worker = threading.Thread(target=self._loop, name="media-jobs", daemon=True)
        self._worker.start()

    # ------------------------------------------------------------- public
    def submit(self, body: dict, *, user: str, ip: str = "") -> dict:
        kind = body.get("kind")
        params = validate(str(kind), body)
        source_id = params.get("source_id")
        if source_id:
            source = self.library.get(source_id)
            want = "video" if params["kind"] == "v2v" else "image"
            if source["type"] != want:
                raise JobError(f"{KIND_LABEL[params['kind']]} needs an {want} source; "
                               f"{source_id} is a {source['type']}")
        job = MediaJob(id=secrets.token_hex(8), kind=params["kind"], params=params, user=user)
        with self._cv:
            if sum(1 for j in self._jobs.values() if j.phase not in TERMINAL) >= 20:
                raise JobError("20 media jobs are already queued; wait for some to finish", 429)
            self._jobs[job.id] = job
            while len(self._jobs) > 200:
                oldest = next(iter(self._jobs))
                if self._jobs[oldest].phase not in TERMINAL:
                    break
                self._jobs.pop(oldest)
            self._queue.append(job.id)
            self._cv.notify()
        self.audit(user=user, ip=ip, action=f"media.{job.kind}", outcome="queued", job=job.id)
        return job.public()

    def get(self, job_id: str) -> dict:
        with self._cv:
            job = self._jobs.get(job_id)
            if job is None:
                raise JobError("no such media job", 404)
            out = job.public()
            if job.phase == "queued":
                try:
                    out["queue_position"] = list(self._queue).index(job_id) + 1
                except ValueError:
                    out["queue_position"] = None
            return out

    def list(self) -> list[dict]:
        with self._cv:
            return [j.public() for j in reversed(self._jobs.values())]

    def busy(self) -> bool:
        with self._cv:
            return any(j.phase not in TERMINAL for j in self._jobs.values())

    def cancel(self, job_id: str, *, user: str) -> dict:
        """Cancel a job that has not been sent to gx10-02 yet. A running
        generation cannot be interrupted (ComfyUI has one slot)."""
        with self._cv:
            job = self._jobs.get(job_id)
            if job is None:
                raise JobError("no such media job", 404)
            if job.phase in TERMINAL:
                raise JobError(f"the job is already {job.phase}", 409)
            if job.phase not in ("queued", "waiting"):
                raise JobError("the job is already running on gx10-02 and finishes on its own", 409)
            job.cancel_requested = True
            if job_id in self._queue:
                self._queue.remove(job_id)
                job.phase, job.detail, job.ended = "cancelled", "cancelled before it started", time.time()
        self.audit(user=user, ip="", action=f"media.{job.kind}", outcome="cancel", job=job_id)
        return self.get(job_id)

    def snapshot(self) -> dict:
        with self._cv:
            jobs: list[dict[str, Any]] = [{"id": j.id, "alias": j.alias, "kind": j.kind, "phase": j.phase, "waiting": j.waiting,
                     "done": j.phase in TERMINAL} for j in self._jobs.values()]
        counts: dict[str, int] = {}
        for j in jobs:
            if not j["done"]:
                counts[j["phase"]] = counts.get(j["phase"], 0) + 1
        return {"jobs": jobs, "counts": counts}

    def _wait_for_resources(self, job: MediaJob) -> bool:
        """True when the job may go; False when it was cancelled or gave up."""
        if self.gate is None:
            return True
        started = time.time()
        while True:
            if job.cancel_requested:
                job.phase, job.detail = "cancelled", "cancelled while waiting"
                return False
            try:
                reason = self.gate(job.alias, job.variant)
            except Exception as exc:  # noqa: BLE001 - never lose a job to a probe error
                log.warning("resource gate failed: %s", exc)
                reason = None
            if reason is None:
                job.waiting = None
                return True
            job.phase = "waiting"
            job.waiting = reason
            job.detail = reason.get("reason", "waiting for resources")
            if time.time() - started > self.wait_limit:
                job.phase = "failed"
                job.error = f"Gave up after {int(self.wait_limit // 60)} minutes: {job.detail}. Retry later."
                return False
            time.sleep(self.wait_poll)

    # ------------------------------------------------------------- worker
    def _loop(self) -> None:
        while True:
            with self._cv:
                while not self._queue:
                    self._cv.wait()
                job = self._jobs[self._queue.popleft()]
            if not self._wait_for_resources(job):
                job.ended = time.time()
                self.audit(user=job.user, ip="", action=f"media.{job.kind}", outcome=job.phase, job=job.id)
                continue
            job.started = time.time()
            try:
                for attempt in range(3):
                    try:
                        self._run(job)
                        break
                    except JobError as exc:
                        # The router re-checks memory itself; a refusal sends the job back to waiting.
                        if ("insufficient_memory" in str(exc) or "HTTP 503" in str(exc)) and attempt < 2 \
                                and self.gate is not None and not job.assets:
                            job.phase, job.detail = "waiting", f"gx10-02 refused: {exc}"
                            if not self._wait_for_resources(job):
                                raise
                            continue
                        raise
                job.phase = "ready"
                job.detail = f"{len(job.assets)} asset(s) saved"
                if self.results:
                    alias = "gx-video" if job.kind in ("t2v", "i2v", "v2v") else "gx-image"
                    self.results.record(alias, "inference", True, f"{KIND_LABEL[job.kind]} ok",
                                        seconds=round(time.time() - job.started, 1))
            except Exception as exc:  # noqa: BLE001 - reported to the user
                if job.phase == "cancelled":
                    job.ended = time.time()
                    continue
                job.phase = "failed"
                if isinstance(exc, (JobError, LibraryError)):
                    job.error = redact(str(exc))[:1000]
                elif isinstance(exc, HTTPError):
                    job.error = "gx10-02 could not be reached; try again shortly"
                else:
                    # never show internal exception text to the user; the log has it
                    job.error = "the job failed unexpectedly; an administrator can see the details in the logs"
                log.warning("media job %s (%s) failed: %s: %s", job.id, job.kind, type(exc).__name__,
                            redact(str(exc))[:500])
                if self.results:
                    alias = "gx-video" if job.kind in ("t2v", "i2v", "v2v") else "gx-image"
                    self.results.record(alias, "inference", False, job.error[:200])
            finally:
                job.ended = time.time()
                self.audit(user=job.user, ip="", action=f"media.{job.kind}", outcome=job.phase, job=job.id,
                           assets=job.assets, elapsed=round(job.ended - job.started, 1))

    def _source(self, job: MediaJob) -> tuple[dict, Path]:
        source = self.library.get(job.params["source_id"])
        return source, self.library.file_path(source)

    def _common_fields(self, p: dict) -> dict:
        fields: dict[str, Any] = {"prompt": p.get("prompt"), "negative_prompt": p.get("negative_prompt"),
                                  "seed": p.get("seed")}
        if p.get("adapter_strength") is not None:
            fields["adapter_strength"] = p["adapter_strength"]
        else:
            fields["uncensored"] = p.get("uncensored", True)
        return fields

    def _run(self, job: MediaJob) -> None:
        p = job.params
        kind = job.kind
        job.phase = "generating"
        job.detail = "submitted to gx10-02"
        try:
            health = self.router.get_json("/health", timeout=5)
            job.cold = health.get("resident_alias") != job.alias
            if job.cold:
                job.detail = "loading the model on gx10-02, then generating"
        except JobError:
            job.cold = None
        if kind == "t2i":
            body = {**self._common_fields(p), "size": p["size"], "n": p.get("n", 1), "quality": p["quality"],
                    "steps": p.get("steps"), "cfg": p.get("guidance"), "response_format": "b64_json",
                    "model": "gx-image"}
            body = {k: v for k, v in body.items() if v is not None}
            t0 = time.time()
            result = self.router.post_json("/v1/images/generations", body, timeout=2400)
            job.elapsed_generation = round(time.time() - t0, 1)
            self._store_images(job, result, operation="generate", parent=None)
            return
        if kind in ("edit", "variation"):
            source, path = self._source(job)
            fields = {**self._common_fields(p), "strength": p.get("strength"), "steps": p.get("steps"),
                      "response_format": "b64_json", "model": "gx-image"}
            if kind == "variation" and not p.get("prompt"):
                fields.pop("prompt")
            ctype, data = _multipart(fields, [("image", f"source.{source['ext']}", source["media_type"], path)])
            t0 = time.time()
            route = "/v1/images/edits" if kind == "edit" else "/v1/images/variations"
            result = self.router.post_multipart(route, ctype, data, timeout=2400)
            job.elapsed_generation = round(time.time() - t0, 1)
            self._store_images(job, result, operation=kind, parent=source["id"])
            return
        # ------------------------------------------------------------- video
        fields = {**self._common_fields(p), "size": p["size"], "seconds": p["seconds"], "model": "gx-video"}
        files: list[tuple[str, str, str, Path]] = []
        parent = None
        route = "/v1/videos"
        if kind == "t2v":
            fields["fps"] = p["fps"]
        elif kind == "i2v":
            source, path = self._source(job)
            parent = source["id"]
            fields["fps"] = p["fps"]
            files.append(("input_reference", f"start.{source['ext']}", source["media_type"], path))
        else:
            source, path = self._source(job)
            parent = source["id"]
            fields["strength"] = p["strength"]
            files.append(("video", f"source.{source['ext']}", source["media_type"], path))
            route = "/v1/videos/edits"
        ctype, data = _multipart({k: v for k, v in fields.items() if v is not None}, files)
        created = self.router.post_multipart(route, ctype, data, timeout=600)
        job.router_job = created.get("id")
        t0 = time.time()
        deadline = t0 + 3 * 3600
        status = created
        while True:
            phase = status.get("phase") or {"queued": "queued"}.get(status.get("status", ""), "generating")
            job.phase = {"ready": "generating"}.get(phase, phase)
            job.detail = f"router job {job.router_job}: {status.get('status')}"
            if status.get("status") in ("completed", "failed"):
                break
            if time.time() > deadline:
                raise JobError("video generation did not finish within 3 hours", 504)
            time.sleep(self.poll_interval)
            status = self.router.get_json(f"/v1/videos/{job.router_job}")
        if status.get("status") != "completed":
            err = status.get("error") or {}
            raise JobError(f"generation failed: {err.get('message') if isinstance(err, dict) else err}", 502)
        job.elapsed_generation = status.get("elapsed_seconds") or round(time.time() - t0, 1)
        job.phase = "saving"
        job.detail = "downloading the video from gx10-02"
        content, _ = self.router.get_bytes(f"/v1/videos/{job.router_job}/content")
        ext = _video_ext(content)
        tmp = self.library.tmp_file("." + ext)
        tmp.write_bytes(content)
        identity = self.model_identity(status.get("workflow") or "")
        width, _, height = str(status.get("size") or "x").partition("x")
        asset = self.library.add(NewAsset(
            type="video", ext=ext, operation="generate" if kind == "t2v" else kind, data_path=tmp,
            title=p.get("title"),
            model_alias="gx-video", model_repo=identity.get("repository"),
            model_revision=identity.get("revision"), workflow=status.get("workflow"),
            prompt=p.get("prompt"), negative_prompt=p.get("negative_prompt"), seed=status.get("seed"),
            strength=p.get("strength"), width=int(width) if width.isdigit() else None,
            height=int(height) if height.isdigit() else None,
            parent_id=parent, job_id=job.id, router_job_id=job.router_job,
            settings={"requested": p, "router": {k: status.get(k) for k in (
                "seconds", "frames", "fps", "elapsed_seconds", "cold_start", "operation", "workflow")},
                "components": identity.get("components")},
        ))
        job.assets.append(asset["id"])
        frames = asset.get("frame_count") or 0
        distinct = asset.get("distinct_frames")
        if distinct is not None and frames and distinct < max(2, frames // 3):
            raise JobError(f"the video has {frames} frames but only {distinct} distinct ones: no real motion "
                           f"(asset {asset['id']} kept for inspection)", 502)

    def _store_images(self, job: MediaJob, result: dict, *, operation: str, parent: str | None) -> None:
        job.phase = "saving"
        job.detail = "storing images"
        gx = result.get("gx") or {}
        identity = self.model_identity(gx.get("workflow") or "")
        width, _, height = str(gx.get("size") or "x").partition("x")
        data = result.get("data") or []
        if not data:
            raise JobError("the router returned no images", 502)
        for item in data:
            raw = base64.b64decode(item.get("b64_json") or "", validate=True)
            if not raw.startswith(b"\x89PNG\r\n\x1a\n") or len(raw) < 1000:
                raise JobError("the router returned something that is not a valid PNG", 502)
            asset = self.library.add(NewAsset(
                type="image", ext="png", operation=operation, data=raw, title=job.params.get("title"),
                model_alias="gx-image", model_repo=identity.get("repository"),
                model_revision=identity.get("revision"), workflow=gx.get("workflow"),
                prompt=job.params.get("prompt"), negative_prompt=job.params.get("negative_prompt"),
                seed=gx.get("seed"), steps=job.params.get("steps"), guidance=job.params.get("guidance"),
                strength=gx.get("strength") if gx.get("strength") is not None else job.params.get("strength"),
                width=int(width) if width.isdigit() else None,
                height=int(height) if height.isdigit() else None,
                parent_id=parent, job_id=job.id, router_job_id=gx.get("id"),
                settings={"requested": job.params, "router": gx, "components": identity.get("components")},
            ))
            job.assets.append(asset["id"])


def _video_ext(content: bytes) -> str:
    if len(content) >= 12 and content[4:8] == b"ftyp":
        return "mp4"
    if content.startswith(b"\x1a\x45\xdf\xa3"):
        return "webm"
    raise JobError("the router returned something that is not a video", 502)


def import_upload(library: MediaLibrary, tmp: Path, content_type: str, title: str | None) -> dict:
    """Register a user upload (an edit source) as an `upload` asset."""
    from .media_library import image_bytes_info, video_ext

    head = tmp.read_bytes()[:64] if tmp.stat().st_size < 64 else _head(tmp)
    if content_type.startswith("image/"):
        data = tmp.read_bytes()
        ext, w, h = image_bytes_info(data)
        return library.add(NewAsset(type="image", ext=ext, operation="upload", data_path=tmp, title=title,
                                    width=w, height=h))
    if content_type.startswith("video/"):
        ext = video_ext(head)
        return library.add(NewAsset(type="video", ext=ext, operation="upload", data_path=tmp, title=title))
    raise LibraryError("upload an image (PNG/JPEG/WebP) or a video (MP4/MOV/WebM)")


def _head(path: Path) -> bytes:
    with path.open("rb") as fh:
        return fh.read(64)
