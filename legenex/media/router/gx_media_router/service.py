"""Application layer: turns a validated request into a ComfyUI generation.

This is where the single-generation-at-a-time rule is enforced and where the
async video worker lives. It knows nothing about HTTP.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import uuid

from .comfy import Artefact, ComfyClient
from .config import Config
from .errors import RouterError, UpstreamError
from .jobs import GenerationSlot, Job, JobStore
from .workflows import WorkflowRegistry

log = logging.getLogger("gx-media.service")


class MediaService:
    def __init__(self, cfg: Config, comfy: ComfyClient, workflows: WorkflowRegistry) -> None:
        self.cfg = cfg
        self.comfy = comfy
        self.workflows = workflows
        self.slot = GenerationSlot()
        self.jobs = JobStore(cfg.max_jobs_retained)
        self._client_id = f"gx-media-router-{uuid.uuid4().hex[:8]}"
        self._video_queue: "queue.Queue[str]" = queue.Queue()
        self._worker = threading.Thread(target=self._video_worker, name="video-worker", daemon=True)
        self._worker.start()

    # -- images (synchronous) ---------------------------------------------
    def generate_image(self, workflow_name: str, params: dict) -> Job:
        workflow = self.workflows.get(workflow_name)
        job = self.jobs.create("image", workflow.name, str(params.get("prompt", "")), params)
        graph = workflow.build(params)
        with self.slot.hold(job.id, self.cfg.queue_wait_seconds):
            self._run(job, graph, self.cfg.image_timeout_seconds)
        return job

    # -- videos (asynchronous) --------------------------------------------
    def submit_video(self, workflow_name: str, params: dict) -> Job:
        workflow = self.workflows.get(workflow_name)
        job = self.jobs.create("video", workflow.name, str(params.get("prompt", "")), params)
        self._video_queue.put(job.id)
        return job

    def _video_worker(self) -> None:
        while True:
            job_id = self._video_queue.get()
            try:
                job = self.jobs.get(job_id)
                workflow = self.workflows.get(job.workflow)
                graph = workflow.build(job.params)
                with self.slot.hold(job.id, self.cfg.queue_wait_seconds):
                    self._run(job, graph, self.cfg.video_timeout_seconds)
            except RouterError as exc:
                log.warning("video job %s failed: %s", job_id, exc.message)
            except Exception:  # pragma: no cover - worker must never die
                log.exception("video job %s crashed", job_id)
            finally:
                self._video_queue.task_done()

    # -- shared execution --------------------------------------------------
    def _run(self, job: Job, graph: dict, timeout: float) -> None:
        job.status = "running"
        job.started_at = time.time()
        try:
            job.prompt_id = self.comfy.submit(graph, self._client_id)
            log.info("job %s -> comfy prompt %s (%s)", job.id, job.prompt_id, job.workflow)
            result = self.comfy.wait(job.prompt_id, timeout=timeout)
            job.artefacts = result.artefacts
            job.status = "completed"
            job.finished_at = time.time()
            log.info(
                "job %s completed in %.1fs -> %s",
                job.id, result.elapsed_seconds, [a.filename for a in result.artefacts],
            )
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

    # -- retrieval ---------------------------------------------------------
    def content(self, job: Job, index: int) -> tuple[bytes, str, str]:
        if job.status != "completed":
            raise UpstreamError(f"job {job.id} is {job.status}, no content available")
        try:
            artefact: Artefact = job.artefacts[index]
        except IndexError:
            raise UpstreamError(
                f"job {job.id} has {len(job.artefacts)} outputs; index {index} is out of range"
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
