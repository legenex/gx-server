"""The single global generation slot and the asynchronous job registry.

ComfyUI has ONE queue and evicting a resident model set costs 270-430 s on this
hardware, so overlapping generations are actively harmful, not merely slow.
Every generation -- synchronous image or asynchronous video -- must hold
``GenerationSlot`` for its whole duration.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Literal

from .comfy import Artefact
from .errors import BusyError, NotFoundError

JobStatus = Literal["queued", "running", "completed", "failed"]


class GenerationSlot:
    """A one-holder mutex with a bounded wait and observable ownership."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = threading.Lock()
        self._holder: str | None = None
        self._since: float = 0.0

    def acquire(self, owner: str, timeout: float) -> bool:
        if not self._lock.acquire(timeout=timeout):
            return False
        with self._state:
            self._holder = owner
            self._since = time.time()
        return True

    def release(self) -> None:
        with self._state:
            self._holder = None
            self._since = 0.0
        self._lock.release()

    def held_by(self) -> tuple[str | None, float]:
        with self._state:
            return self._holder, self._since

    class _Guard:
        def __init__(self, slot: "GenerationSlot", owner: str, timeout: float) -> None:
            self._slot = slot
            self._owner = owner
            self._timeout = timeout

        def __enter__(self) -> "GenerationSlot._Guard":
            if not self._slot.acquire(self._owner, self._timeout):
                holder, since = self._slot.held_by()
                waited = time.time() - since if since else 0.0
                raise BusyError(
                    "the single generation slot is busy "
                    f"(held by {holder or 'another request'} for {waited:.0f}s); "
                    f"waited {self._timeout:.0f}s"
                )
            return self

        def __exit__(self, *exc: object) -> Literal[False]:
            self._slot.release()
            return False

    def hold(self, owner: str, timeout: float) -> "GenerationSlot._Guard":
        return GenerationSlot._Guard(self, owner, timeout)


@dataclass
class Job:
    """An image or video generation, tracked for later retrieval."""

    id: str
    kind: str
    workflow: str
    prompt: str
    params: dict
    status: JobStatus = "queued"
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    prompt_id: str | None = None
    artefacts: tuple[Artefact, ...] = ()
    error: str | None = None
    operation: str = "generate"
    #: staged ComfyUI input names, removed when the job ends
    staged: tuple[str, ...] = ()
    #: id of the router job this one edits (remix / edit-by-id)
    source_job: str | None = None

    #: OpenAI video-object status vocabulary (LiteLLM validates it).
    _OPENAI_STATUS = {"queued": "queued", "running": "in_progress", "completed": "completed", "failed": "failed"}

    def primary(self) -> Artefact | None:
        want = "video/" if self.kind == "video" else "image/"
        for artefact in self.artefacts:
            if artefact.media_type.startswith(want) and not artefact.thumbnail:
                return artefact
        return None

    def thumbnail(self) -> Artefact | None:
        for artefact in self.artefacts:
            if artefact.thumbnail:
                return artefact
        return None

    def public(self) -> dict:
        params = self.params or {}
        width, height = params.get("width"), params.get("height")
        fps = params.get("fps")
        length = params.get("length")
        body: dict = {
            "id": self.id,
            "object": "video" if self.kind == "video" else "image.generation",
            "status": self._OPENAI_STATUS[self.status] if self.kind == "video" else self.status,
            "model": f"gx-{self.kind}",
            "created_at": int(self.created_at),
            "progress": {"queued": 0, "running": 50, "completed": 100, "failed": 0}[self.status],
            "workflow": self.workflow,
            "operation": self.operation,
            "gx_status": self.status,
        }
        if width and height:
            body["size"] = f"{width}x{height}"
        if self.kind == "video" and isinstance(length, int) and isinstance(fps, (int, float)) and fps:
            body["seconds"] = f"{length / float(fps):.2f}".rstrip("0").rstrip(".")
            body["frames"] = length
            body["fps"] = fps
        if params.get("seed") is not None:
            body["seed"] = params["seed"]
        if self.source_job:
            body["remixed_from_video_id"] = self.source_job
        if self.started_at is not None:
            body["started_at"] = int(self.started_at)
        if self.finished_at is not None:
            body["completed_at"] = int(self.finished_at)
            body["finished_at"] = int(self.finished_at)
            body["elapsed_seconds"] = round(self.finished_at - (self.started_at or self.created_at), 2)
        if self.status == "completed":
            body["outputs"] = [a.filename for a in self.artefacts]
            body["content_url"] = f"/v1/{self.kind}s/{self.id}/content"
            if self.thumbnail() is not None:
                body["thumbnail_url"] = f"/v1/{self.kind}s/{self.id}/content?variant=thumbnail"
        body["error"] = {"code": "generation_failed", "message": self.error} if self.error else None
        return body


class JobStore:
    """Bounded, thread-safe, in-memory registry. Content stays in ComfyUI."""

    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._jobs: "OrderedDict[str, Job]" = OrderedDict()
        self._lock = threading.Lock()

    def create(self, kind: str, workflow: str, prompt: str, params: dict, *,
               operation: str = "generate", staged: tuple[str, ...] = (),
               source_job: str | None = None) -> Job:
        job = Job(id=f"{kind}-{uuid.uuid4().hex[:16]}", kind=kind, workflow=workflow,
                  prompt=prompt, params=params, operation=operation, staged=staged,
                  source_job=source_job)
        with self._lock:
            self._jobs[job.id] = job
            while len(self._jobs) > self._capacity:
                self._jobs.popitem(last=False)
        return job

    def get(self, job_id: str) -> Job:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise NotFoundError(f"no such job: {job_id}", param="id")
        return job

    def snapshot(self) -> list[Job]:
        with self._lock:
            return list(self._jobs.values())
