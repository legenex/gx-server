"""Application layer: turns a validated request into a ComfyUI generation.

This is where the single-generation-at-a-time rule is enforced, where staged
source media is cleaned up, and where the async video worker lives. It knows
nothing about HTTP.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import uuid

from .comfy import Artefact, ComfyClient
from .config import Config
from .errors import NotFoundError, RouterError, UpstreamError, ValidationError
from .jobs import GenerationSlot, Job, JobStore
from .uploads import InputStore, MediaInfo
from .workflows import WorkflowRegistry

log = logging.getLogger("gx-media.service")


class MediaService:
    def __init__(self, cfg: Config, comfy: ComfyClient, workflows: WorkflowRegistry,
                 inputs: InputStore | None = None) -> None:
        self.cfg = cfg
        self.comfy = comfy
        self.workflows = workflows
        self.inputs = inputs or InputStore(cfg.input_dir, cfg.input_ttl_seconds)
        self.slot = GenerationSlot()
        self.jobs = JobStore(cfg.max_jobs_retained)
        self._client_id = f"gx-media-router-{uuid.uuid4().hex[:8]}"
        #: model files of the last generation (see _switch_models)
        self._resident_models: frozenset[str] = frozenset()
        self._last_activity = time.monotonic()
        self._video_queue: "queue.Queue[str]" = queue.Queue()
        self._worker = threading.Thread(target=self._video_worker, name="video-worker", daemon=True)
        self._worker.start()
        self._janitor = threading.Thread(target=self._purge_loop, name="input-janitor", daemon=True)
        self._janitor.start()

    # -- staging -----------------------------------------------------------
    def stage(self, data: bytes, info: MediaInfo) -> str:
        return self.inputs.put(data, info)

    def stage_from_job(self, job_id: str) -> tuple[str, Job]:
        """Stage the primary output of an earlier router job as an input."""
        source = self.jobs.get(job_id)
        if source.status != "completed":
            raise ValidationError(f"job {job_id} is {source.status}; only completed jobs can be edited",
                                  param="video")
        artefact = source.primary()
        if artefact is None:
            raise ValidationError(f"job {job_id} has no primary output", param="video")
        data = self.comfy.fetch(artefact)
        ext = artefact.filename.rsplit(".", 1)[-1].lower()
        kind = "video" if artefact.media_type.startswith("video/") else "image"
        return self.inputs.put(data, MediaInfo(kind, ext, artefact.media_type)), source

    def _cleanup(self, job: Job) -> None:
        for name in job.staged:
            self.inputs.remove(name)

    def _purge_loop(self) -> None:
        last_purge = 0.0
        while True:
            try:
                if time.monotonic() - last_purge > 3600:
                    removed = self.inputs.purge_stale()
                    last_purge = time.monotonic()
                    if removed:
                        log.info("purged %d stale staged input(s)", removed)
                self.free_if_idle()
            except Exception:  # pragma: no cover - janitor must never die
                log.exception("janitor failed")
            time.sleep(30)

    def free_if_idle(self) -> bool:
        """Hand the node back after media work: free ComfyUI's models when idle."""
        idle = self.cfg.idle_free_seconds
        if not idle or not self._resident_models:
            return False
        if time.monotonic() - self._last_activity < idle or not self._video_queue.empty():
            return False
        if not self.slot.acquire("idle-free", 0.0):
            return False
        try:
            log.info("idle for %ss: freeing ComfyUI models %s", idle, sorted(self._resident_models))
            self.comfy.free(unload_models=True, free_memory=True)
            self._resident_models = frozenset()
            return True
        finally:
            self.slot.release()

    def free_now(self, wait_seconds: float = 2.0) -> dict:
        """Hand node 2 to another tenant now (gx-reason's start calls this).

        Frees ComfyUI's models only when no generation holds the slot and no video
        is queued; a running job is never disturbed.
        """
        if not self._video_queue.empty():
            return {"freed": False, "reason": "video jobs queued"}
        if not self.slot.acquire("free-request", wait_seconds):
            holder, _ = self.slot.held_by()
            return {"freed": False, "reason": f"busy ({holder})"}
        try:
            models = sorted(self._resident_models)
            log.info("free requested: freeing ComfyUI models %s", models)
            self.comfy.free(unload_models=True, free_memory=True)
            self._resident_models = frozenset()
            return {"freed": True, "models": models}
        finally:
            self.slot.release()

    # -- images (synchronous) ---------------------------------------------
    def generate_image(self, workflow_name: str, params: dict, *, staged: tuple[str, ...] = (),
                       operation: str | None = None) -> Job:
        workflow = self.workflows.get(workflow_name)
        job = self.jobs.create("image", workflow.name, str(params.get("prompt", "")), params,
                               operation=operation or workflow.operation, staged=staged)
        try:
            graph = workflow.build({**params, "filename_prefix": f"gx-image/{job.id}"})
            with self.slot.hold(job.id, self.cfg.queue_wait_seconds):
                self._run(job, graph, self.cfg.image_timeout_seconds, workflow.thumbnail_node)
        finally:
            self._cleanup(job)
        return job

    # -- videos (asynchronous) --------------------------------------------
    def submit_video(self, workflow_name: str, params: dict, *, staged: tuple[str, ...] = (),
                     source_job: str | None = None) -> Job:
        workflow = self.workflows.get(workflow_name)
        job = self.jobs.create("video", workflow.name, str(params.get("prompt", "")), params,
                               operation=workflow.operation, staged=staged, source_job=source_job)
        self._video_queue.put(job.id)
        return job

    def _video_worker(self) -> None:
        while True:
            job_id = self._video_queue.get()
            job = None
            try:
                job = self.jobs.get(job_id)
                workflow = self.workflows.get(job.workflow)
                graph = workflow.build({**job.params, "filename_prefix": f"gx-video/{job.id}",
                                        "thumb_prefix": f"gx-video/{job.id}-thumb"})
                with self.slot.hold(job.id, self.cfg.queue_wait_seconds):
                    self._run(job, graph, self.cfg.video_timeout_seconds, workflow.thumbnail_node)
            except RouterError as exc:
                if job is not None and job.status != "failed":
                    job.status, job.error, job.finished_at = "failed", exc.message, time.time()
                log.warning("video job %s failed: %s", job_id, exc.message)
            except Exception as exc:  # pragma: no cover - worker must never die
                if job is not None:
                    job.status, job.error, job.finished_at = "failed", f"{type(exc).__name__}: {exc}", time.time()
                log.exception("video job %s crashed", job_id)
            finally:
                if job is not None:
                    self._cleanup(job)
                self._video_queue.task_done()

    # -- shared execution --------------------------------------------------
    def _switch_models(self, workflow_name: str) -> bool:
        """Free ComfyUI's cached models before a job that needs different weights.

        ComfyUI keeps every model it has loaded. On this 121 GiB unified-memory
        node, the image, edit, text-to-video and image-to-video weights together
        are ~110 GiB, so letting them stack would starve the host (B-012/B-018).
        Called with the generation slot held, so nothing is running.
        """
        models = frozenset(self.workflows.get(workflow_name).models)
        cold = not models <= self._resident_models
        if not self.cfg.free_on_model_switch or not models:
            self._resident_models = self._resident_models | models
            return cold
        if self._resident_models and not models <= self._resident_models:
            log.info("model set changes (%s -> %s): freeing ComfyUI models first",
                     sorted(self._resident_models), sorted(models))
            self.comfy.free(unload_models=True, free_memory=True)
            self._resident_models = frozenset()
        # A subset of what is already loaded reuses it; the loaded set is unchanged.
        self._resident_models = self._resident_models | models
        return cold

    def _run(self, job: Job, graph: dict, timeout: float, thumbnail_node: str | None) -> None:
        self._last_activity = time.monotonic()
        job.cold_start = self._switch_models(job.workflow)
        job.status = "running"
        job.started_at = time.time()
        try:
            job.prompt_id = self.comfy.submit(graph, self._client_id)
            log.info("job %s -> comfy prompt %s (%s)", job.id, job.prompt_id, job.workflow)
            result = self.comfy.wait(job.prompt_id, timeout=timeout, thumbnail_node=thumbnail_node)
            job.artefacts = result.artefacts
            if job.primary() is None:
                raise UpstreamError(f"ComfyUI prompt {job.prompt_id} produced no {job.kind} output")
            job.status = "completed"
            job.finished_at = time.time()
            log.info("job %s completed in %.1fs -> %s", job.id, result.elapsed_seconds,
                     [a.filename for a in result.artefacts])
        except RouterError as exc:
            job.status = "failed"
            job.error = exc.message
            job.finished_at = time.time()
            raise
        except Exception as exc:  # pragma: no cover - defensive
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
            job.finished_at = time.time()
            raise UpstreamError(job.error) from exc
        finally:
            self._last_activity = time.monotonic()

    # -- retrieval ---------------------------------------------------------
    def content(self, job: Job, index: int = 0, variant: str | None = None) -> tuple[bytes, str, str]:
        if job.status != "completed":
            raise UpstreamError(f"job {job.id} is {job.status}, no content available")
        artefact: Artefact | None
        if variant == "thumbnail":
            artefact = job.thumbnail()
            if artefact is None:
                raise NotFoundError(f"job {job.id} has no thumbnail", param="variant")
        elif variant not in (None, "", "video", "image"):
            raise ValidationError("variant must be 'thumbnail' or omitted", param="variant")
        else:
            primary = job.primary()
            outputs = [primary] if primary is not None else []
            outputs += [a for a in job.artefacts if not a.thumbnail and a is not primary]
            try:
                artefact = outputs[index]
            except IndexError:
                raise NotFoundError(
                    f"job {job.id} has {len(outputs)} outputs; index {index} is out of range"
                ) from None
        return self.comfy.fetch(artefact), artefact.media_type, artefact.filename

    def health(self) -> dict:
        holder, since = self.slot.held_by()
        status: dict = {
            "status": "ok",
            "service": "gx-media-router",
            "busy": holder is not None,
            "held_by": holder,
            "held_for_seconds": round(time.time() - since, 1) if since else 0.0,
            "video_queue_depth": self._video_queue.qsize(),
            "workflows": self.workflows.names(),
            "uploads_enabled": self.inputs.available,
        }
        try:
            stats = self.comfy.system_stats()
            system = stats.get("system", {})
            devices = stats.get("devices", [{}])
            status["comfyui"] = {
                "reachable": True,
                "comfyui_version": system.get("comfyui_version"),
                "torch_version": system.get("pytorch_version"),
                "device": devices[0].get("name") if devices else None,
                "vram_free_bytes": devices[0].get("vram_free") if devices else None,
                "queue_depth": self.comfy.queue_depth(),
            }
        except RouterError as exc:
            status["status"] = "degraded"
            status["comfyui"] = {"reachable": False, "error": exc.message}
        return status
