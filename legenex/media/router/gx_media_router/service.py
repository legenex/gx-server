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

from . import __version__
from .comfy import Artefact, ComfyClient
from .config import Config
from .errors import (ExceedsNodeError, InsufficientMemoryError, NotFoundError, PolicyBlockedError, RouterError,
                     UpstreamError, ValidationError)
from .jobs import GenerationSlot, Job, JobStore
from .policy import Policy
from .tenants import MUSIC_LOADED_FLOOR_GIB, MusicState, MusicTenant
from .uploads import InputStore, MediaInfo
from .workflows import WorkflowRegistry

log = logging.getLogger("gx-media.service")


class MediaService:
    def __init__(self, cfg: Config, comfy: ComfyClient, workflows: WorkflowRegistry,
                 inputs: InputStore | None = None, music: MusicTenant | None = None) -> None:
        self.cfg = cfg
        self.comfy = comfy
        self.workflows = workflows
        self.inputs = inputs or InputStore(cfg.input_dir, cfg.input_ttl_seconds)
        self.slot = GenerationSlot()
        self.jobs = JobStore(cfg.max_jobs_retained)
        self._client_id = f"gx-media-router-{uuid.uuid4().hex[:8]}"
        #: model files of the last generation (see _switch_models)
        self._resident_models: frozenset[str] = frozenset()
        #: which public alias the resident weights serve (gx-image / gx-video)
        self._resident_kind: str | None = None
        self._last_activity = time.monotonic()
        self.policy = Policy(cfg.guard_dir)
        self.music = music if music is not None else MusicTenant(cfg.music_url, cfg.music_key_file)
        self.last_refusal: dict | None = None
        self.last_eviction: dict | None = None
        #: GiB the resident weights hold between jobs (measured after a cold job;
        #: None = unknown, so a warm job is judged by its full footprint)
        self._held_gib: float | None = None
        #: the admitted job whose growth may not be in MemAvailable yet (D-038)
        self._inflight: dict | None = None
        self._admissions = 0
        self._mem_lock = threading.Lock()
        self._video_queue: "queue.Queue[str]" = queue.Queue()
        self._worker = threading.Thread(target=self._video_worker, name="video-worker", daemon=True)
        self._worker.start()
        self._janitor = threading.Thread(target=self._purge_loop, name="input-janitor", daemon=True)
        self._janitor.start()

    # -- cluster policy ----------------------------------------------------
    def check_policy(self) -> None:
        """Refuse a NEW job while gx-max holds node 2 or Maintenance is on."""
        block = self.policy.block()
        if block is not None:
            raise PolicyBlockedError(block.message, block.code)

    def _set_resident(self, models: frozenset[str], kind: str | None) -> None:
        if models != self._resident_models:
            self._held_gib = None
        self._resident_models = models
        self._resident_kind = kind if models else None

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
        if not self._resident_models or not self._video_queue.empty():
            return False
        maintenance = self.policy.maintenance()
        if not maintenance:
            if not idle or time.monotonic() - self._last_activity < idle:
                return False
            if self.pin_honoured():
                return False
        if not self.slot.acquire("idle-free", 0.0):
            return False
        try:
            why = "maintenance mode" if maintenance else f"idle for {idle}s"
            log.info("%s: freeing ComfyUI models %s", why, sorted(self._resident_models))
            self.comfy.free(unload_models=True, free_memory=True)
            self._set_resident(frozenset(), None)
            self._freed_at = time.monotonic()
            return True
        finally:
            self.slot.release()

    def pin_honoured(self) -> bool:
        """A pin keeps the resident set past the idle timer, never past the reserve."""
        kind = self._resident_kind
        if not kind or kind not in self.policy.pinned() or self.policy.block() is not None:
            return False
        avail = self._mem_available_gib()
        return avail is None or avail >= self.cfg.reserve_gib

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
            self._set_resident(frozenset(), None)
            self._freed_at = time.monotonic()
            return {"freed": True, "models": models}
        finally:
            self.slot.release()

    # -- images (synchronous) ---------------------------------------------
    def generate_image(self, workflow_name: str, params: dict, *, staged: tuple[str, ...] = (),
                       operation: str | None = None) -> Job:
        workflow = self.workflows.get(workflow_name)
        try:
            self.check_policy()
            self._check_ever_fits(workflow_name)
        except RouterError:
            for name in staged:
                self.inputs.remove(name)
            raise
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
        try:
            self.check_policy()
            self._check_ever_fits(workflow_name)
        except RouterError:
            for name in staged:
                self.inputs.remove(name)
            raise
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
                self._run_when_admitted(job, graph, workflow.thumbnail_node)
            except RouterError as exc:
                if job is not None and job.status != "failed":
                    job.status, job.error, job.finished_at = "failed", exc.message, time.time()
                if job is not None:
                    job.error_code = exc.code
                    job.waiting = None
                log.warning("video job %s failed: %s", job_id, exc.message)
            except Exception as exc:  # pragma: no cover - worker must never die
                if job is not None:
                    job.status, job.error, job.finished_at = "failed", f"{type(exc).__name__}: {exc}", time.time()
                    job.waiting = None
                log.exception("video job %s crashed", job_id)
            finally:
                if job is not None:
                    self._cleanup(job)
                self._video_queue.task_done()

    def _run_when_admitted(self, job: Job, graph: dict, thumbnail_node: str | None) -> None:
        """Run a video once gx10-02 can hold it above the reserve; until then it
        WAITS (status queued, phase "waiting") with the reason, and never starts
        into a gx-max hold or Maintenance."""
        cfg = self.cfg
        deadline = time.monotonic() + cfg.resource_wait_seconds
        while True:
            block = self.policy.block()
            if block is not None:
                reason = {"code": block.code, "reason": block.message, "blocker": "gx-max" if
                          block.code == "gx_max_active" else "maintenance",
                          "next": "starts automatically when it is lifted"}
            else:
                try:
                    with self.slot.hold(job.id, cfg.queue_wait_seconds):
                        self._run(job, graph, cfg.video_timeout_seconds, thumbnail_node, wait_on_memory=True)
                    job.waiting = None
                    return
                except InsufficientMemoryError as exc:
                    if not exc.retryable:
                        raise
                    reason = {"code": exc.code, "reason": exc.message, **exc.details}
            since = (job.waiting or {}).get("since") or time.time()
            job.waiting = {**reason, "since": since}
            if time.monotonic() >= deadline:
                raise InsufficientMemoryError(
                    f"Gave up after {cfg.resource_wait_seconds // 60} minutes of waiting: {reason['reason']}",
                    details={k: v for k, v in reason.items() if k not in ("reason",)}, retryable=False)
            time.sleep(cfg.resource_retry_seconds)

    # -- shared execution --------------------------------------------------
    def _switch_models(self, workflow_name: str) -> bool:
        cold = self._switch_model_set(workflow_name)
        self._resident_kind = ("gx-video" if self.workflows.get(workflow_name).kind == "video" else "gx-image") \
            if self._resident_models else None
        return cold

    def _switch_model_set(self, workflow_name: str) -> bool:
        """Free ComfyUI's cached models before a job that needs different weights.

        ComfyUI keeps every model it has loaded. On this 121 GiB unified-memory
        node, the image, edit, text-to-video and image-to-video weights together
        are ~110 GiB, so letting them stack would starve the host (B-012/B-018).
        Called with the generation slot held, so nothing is running.
        """
        models = frozenset(self.workflows.get(workflow_name).models)
        cold = not models <= self._resident_models
        if not self.cfg.free_on_model_switch or not models:
            if cold:
                self._held_gib = None
            self._resident_models = self._resident_models | models
            return cold
        if self._resident_models and not models <= self._resident_models:
            log.info("model set changes (%s -> %s): freeing ComfyUI models first",
                     sorted(self._resident_models), sorted(models))
            self.comfy.free(unload_models=True, free_memory=True)
            self._resident_models = frozenset()
            self._held_gib = None
            self._freed_at = time.monotonic()
        # A subset of what is already loaded reuses it; the loaded set is unchanged.
        self._resident_models = self._resident_models | models
        return cold

    def _mem_available_gib(self) -> float | None:
        path = self.cfg.meminfo_path
        if not path:
            return None
        try:
            with open(path, encoding="ascii") as fh:
                for line in fh:
                    if line.startswith("MemAvailable:"):
                        return int(line.split()[1]) / (1024 * 1024)
        except (OSError, ValueError, IndexError):
            now = time.monotonic()
            if now - self._meminfo_warned > 60:
                self._meminfo_warned = now
                log.warning("cannot read %s", path)
        return None

    _meminfo_warned = -1e9

    # -- memory admission (D-038) ------------------------------------------
    def _footprint_gib(self, workflow_name: str) -> float:
        cfg = self.cfg
        if "keyframe" in workflow_name:
            return cfg.footprint_keyframe_gib
        return cfg.footprint_video_gib if self.workflows.get(workflow_name).kind == "video" \
            else cfg.footprint_image_gib

    def _growth_gib(self, job: Job) -> tuple[float, str]:
        """How much MemAvailable this job takes away, and on what basis."""
        footprint = self._footprint_gib(job.workflow)
        if job.cold_start:
            return footprint, "cold load (measured footprint)"
        held = self._held_gib
        if held is None:
            return footprint, "warm, resident size unknown (full footprint assumed)"
        return max(self.cfg.warm_growth_floor_gib, footprint - held), \
            f"warm (footprint {footprint:.0f} GiB minus {held:.0f} GiB already held)"

    def _check_ever_fits(self, workflow_name: str) -> None:
        """Refuse at submit time what gx10-02 can never hold above the reserve."""
        cfg = self.cfg
        if not cfg.meminfo_path:
            return
        footprint = self._footprint_gib(workflow_name)
        if footprint + cfg.reserve_gib <= cfg.node_capacity_gib:
            return
        what = "keyframe video edit" if "keyframe" in workflow_name else "job"
        hint = (" Use a strength below 0.5 (the restyle edit) until B-028 is decided."
                if "keyframe" in workflow_name else "")
        raise ExceedsNodeError(
            f"This {what} needs about {footprint:.0f} GiB on gx10-02, and the node must keep its "
            f"{cfg.reserve_gib:.0f} GiB reserve ({footprint + cfg.reserve_gib:.0f} GiB in total). gx10-02 never has "
            f"more than about {cfg.node_capacity_gib:.0f} GiB available, so it cannot run.{hint}",
            details={"required_gib": round(footprint + cfg.reserve_gib, 1), "growth_gib": footprint,
                     "reserve_gib": cfg.reserve_gib, "node_capacity_gib": cfg.node_capacity_gib,
                     "blocker": "node capacity", "next": "not retried"})

    def _decision(self, job: Job, avail: float, growth: float, basis: str, music: MusicState | None) -> dict:
        """Pure arithmetic plus a human explanation. ok == projected >= reserve."""
        cfg = self.cfg
        reserve = cfg.reserve_gib
        music_known = music is not None and music.reachable
        pending = music.pending_gib if music_known else 0.0
        projected = avail - pending - growth
        ok = projected >= reserve
        required = growth + pending + reserve
        what = f"this {job.kind} job ({job.workflow})"
        numbers = (f"{what} needs {growth:.0f} GiB plus the {reserve:.0f} GiB reserve"
                   + (f" plus {pending:.0f} GiB that gx-music has not taken yet" if pending >= 0.5 else "")
                   + f", so {required:.0f} GiB must be available; {avail:.0f} GiB is")
        music_holds = music_known and music.holds_memory
        freed_by_music = (music.loaded_gib or MUSIC_LOADED_FLOOR_GIB) if music_holds else 0.0
        music_is_enough = music_holds and avail + freed_by_music - growth >= reserve
        if ok:
            blocker, reason, nxt = None, f"admitted: {numbers}", "starting"
        elif music_holds and music_is_enough:
            blocker = "gx-music"
            reason = f"Waiting for gx-music to release enough gx10-02 memory: {numbers}"
            if music.engine == "loading":
                nxt = "gx-music is loading; the check repeats when it is ready"
            elif music.busy or music.active_jobs:
                nxt = "gx-music is working; this starts after it finishes and unloads"
            elif music.pinned or self.policy.music_pinned():
                nxt = "gx-music is pinned; unpin or unload it in Resource Control"
            else:
                nxt = "idle gx-music is unloaded first (or unloads after its idle timer)"
        else:
            blocker = "another gx10-02 tenant (gx-reason)" + (" and gx-music" if music_holds else "")
            reason = f"Waiting for enough gx10-02 memory (held by {blocker}): {numbers}"
            nxt = ("retries automatically; gx-reason unloads after 15 minutes idle, or unload it in Resource "
                   "Control")
        return {"ok": ok, "reason": reason, "details": {
            "required_gib": round(required, 1), "available_gib": round(avail, 1), "reserve_gib": reserve,
            "growth_gib": round(growth, 1), "growth_basis": basis, "pending_gib": round(pending, 1),
            "projected_gib": round(projected, 1), "blocker": blocker, "next": nxt,
            "music": music.public() if music is not None else None}}

    def _settled_available(self, avail: float, want: float, since: float) -> float:
        """Right after a free, memory is still on its way back: wait (bounded)
        while it rises and is still short of ``want``."""
        deadline = since + self._settle_seconds
        best, rose_at = avail, time.monotonic()
        while (avail < want and time.monotonic() < deadline
               and time.monotonic() - rose_at < self._settle_flat_seconds):
            time.sleep(self._settle_poll)
            sample = self._mem_available_gib()
            avail = avail if sample is None else sample
            if avail > best + 0.25:
                best, rose_at = avail, time.monotonic()
        return avail

    def _music_evictable(self, music: MusicState | None) -> str | None:
        """None when the idle gx-music engine may be unloaded for this job, else why not."""
        if music is None or not music.reachable:
            return "gx-music supervisor not reachable"
        if not self.cfg.evict_idle_music:
            return "eviction of idle gx-music is switched off"
        if not self.music.can_unload:
            return "the router has no gx-music key"
        if music.engine != "ready":
            return f"gx-music is {music.engine}"
        if music.busy or music.active_jobs:
            return "gx-music is working"
        if music.pinned or self.policy.music_pinned():
            return "gx-music is pinned"
        if self.policy.profile() in ("music", "maintenance", "max"):
            return f"the {self.policy.profile()} profile keeps gx-music"
        if self.policy.block() is not None:
            return "cluster policy hold"
        return None

    def _evict_music(self, job: Job, music: MusicState) -> bool:
        """Unload the idle engine through its supervisor and VERIFY the release:
        engine unloaded, container gone, ledger clean, memory back."""
        before = self._mem_available_gib()
        t0 = time.monotonic()
        ok, body = self.music.unload_if_idle()
        record: dict = {"at": time.time(), "job": job.id, "requested": ok, "mem_available_before_gib":
                        None if before is None else round(before, 1)}
        if not ok:
            record["refused"] = str(body.get("reason", ""))[:200]
            self.last_eviction = record
            log.info("gx-music did not unload for job %s: %s", job.id, record["refused"])
            return False
        deadline = t0 + self.cfg.eviction_settle_seconds
        state = None
        while time.monotonic() < deadline:
            state = self.music.state()
            if state is not None and state.reachable and state.engine == "unloaded" \
                    and not self.policy.ledger_has("gx-music"):
                break
            time.sleep(self._settle_poll)
        engine_gone = bool(state and state.reachable and state.engine == "unloaded")
        ledger_clean = not self.policy.ledger_has("gx-music")
        container_gone = bool(body.get("container_gone"))
        expect = music.loaded_gib or MUSIC_LOADED_FLOOR_GIB
        after = self._mem_available_gib()
        if before is not None and after is not None:
            after = self._settled_available(after, before + 0.8 * expect, time.monotonic())
        record.update(engine_unloaded=engine_gone, container_gone=container_gone, ledger_clean=ledger_clean,
                      mem_available_after_gib=None if after is None else round(after, 1),
                      released_gib=None if after is None or before is None else round(after - before, 1),
                      seconds=round(time.monotonic() - t0, 1))
        self.last_eviction = record
        verified = engine_gone and container_gone and ledger_clean
        log.info("gx-music eviction for job %s: %s", job.id, record)
        return verified

    def _admit(self, job: Job, held_before: frozenset) -> None:
        """Start a job only if gx10-02 keeps the 30 GiB reserve afterwards (D-038).

        Runs after _switch_models, which already dropped any other cached
        weights. Order: settle after a free -> decide -> (warm job of unknown
        size: free our own weights, judge cold) -> (idle gx-music: unload it
        through its supervisor, verify, decide again) -> refuse with the reason.
        """
        cfg = self.cfg
        if not cfg.meminfo_path:
            return
        avail = self._mem_available_gib()
        if avail is None:
            self._forget_unloaded(job, held_before)
            raise InsufficientMemoryError(
                "gx10-02's memory state cannot be read, so the job waits instead of guessing.",
                details={"blocker": "memory state unreadable", "reserve_gib": cfg.reserve_gib,
                         "next": "retries automatically"})
        music = self.music.state()
        growth, basis = self._growth_gib(job)
        pending = music.pending_gib if music is not None and music.reachable else 0.0
        avail = self._settled_available(avail, growth + pending + cfg.reserve_gib, self._freed_at)
        decision = self._decision(job, avail, growth, basis, music)
        if not decision["ok"] and not job.cold_start and self._held_gib is None and self._resident_models:
            # A warm job whose resident size is unknown is judged by its full footprint;
            # freeing our own weights and loading them again costs seconds, not safety.
            log.info("job %s: warm admission short with unknown resident size; freeing ComfyUI and "
                     "judging it cold", job.id)
            models = self._resident_models
            kind = self._resident_kind
            self.comfy.free(unload_models=True, free_memory=True)
            self._freed_at = time.monotonic()
            self._set_resident(frozenset(), None)
            self._set_resident(models, kind)
            held_before = frozenset()
            job.cold_start = True
            growth, basis = self._growth_gib(job)
            avail = self._settled_available(self._mem_available_gib() or avail,
                                            growth + pending + cfg.reserve_gib, self._freed_at)
            decision = self._decision(job, avail, growth, basis, music)
        if not decision["ok"] and decision["details"]["blocker"] == "gx-music":
            why_not = self._music_evictable(music)
            if why_not is None and music is not None:
                log.info("job %s: %s -> unloading idle gx-music first", job.id, decision["reason"])
                verified = self._evict_music(job, music)
                music = self.music.state()
                pending = music.pending_gib if music is not None and music.reachable else 0.0
                avail = self._mem_available_gib() or avail
                decision = self._decision(job, avail, growth, basis, music)
                if not verified and decision["ok"]:
                    decision["ok"] = False
                    decision["reason"] = ("gx-music reported an unload that could not be verified (engine, "
                                          "container, ledger); waiting instead of guessing")
                    decision["details"]["next"] = "retries automatically"
            else:
                decision["details"]["eviction"] = why_not
        if decision["ok"]:
            with self._mem_lock:
                self._admissions += 1
                self._inflight = {"job": job.id, "growth": growth, "baseline": avail, "cold": job.cold_start,
                                  "music": music.signature if music is not None else None,
                                  "epoch": self._admissions}
            log.info("job %s %s", job.id, decision["reason"])
            return
        self._forget_unloaded(job, held_before)
        previous = self.last_refusal or {}
        if previous.get("job") != job.id or previous.get("reason") != decision["reason"]:
            log.warning("job %s refused: %s", job.id, decision["reason"])
        details = decision["details"]
        self.last_refusal = {"at": time.time(), "job": job.id, "kind": job.kind, "workflow": job.workflow,
                             "cold": job.cold_start, "need_gib": details["required_gib"],
                             "available_gib": details["available_gib"], "blocker": details["blocker"],
                             "reason": decision["reason"]}
        raise InsufficientMemoryError(decision["reason"], details=details)

    def _forget_unloaded(self, job: Job, held_before: frozenset) -> None:
        if job.cold_start:
            # The job's weights were never loaded; do not remember them as resident.
            kind_before = self._resident_kind
            self._set_resident(held_before if not self.cfg.free_on_model_switch else frozenset(), kind_before)

    def pending_gib(self) -> float:
        """Growth of the running job that MemAvailable does not show yet (for gx-music)."""
        inflight = self._inflight
        if not inflight:
            return 0.0
        avail = self._mem_available_gib()
        if avail is None:
            return round(inflight["growth"], 1)
        consumed = max(0.0, inflight["baseline"] - avail)
        return round(max(0.0, inflight["growth"] - consumed), 1)

    def _finish_inflight(self, job: Job, completed: bool) -> None:
        with self._mem_lock:
            inflight, self._inflight = self._inflight, None
        if not (completed and inflight and inflight.get("job") == job.id and inflight.get("cold")
                and self._held_measure_seconds > 0 and self.cfg.meminfo_path):
            return
        models = self._resident_models
        threading.Thread(target=self._measure_held, args=(inflight, models), name="held-measure",
                         daemon=True).start()

    def _measure_held(self, inflight: dict, models: frozenset) -> None:
        """After a cold job: once activations are released, how much do the
        resident weights still hold? Discarded if anything else changed."""
        time.sleep(self._held_settle_delay)
        deadline = time.monotonic() + self._held_measure_seconds
        best = self._mem_available_gib()
        rose_at = time.monotonic()
        while best is not None and time.monotonic() < deadline and time.monotonic() - rose_at < 4.0:
            time.sleep(self._settle_poll)
            sample = self._mem_available_gib()
            if sample is not None and sample > best + 0.25:
                best, rose_at = sample, time.monotonic()
        music = self.music.state()
        with self._mem_lock:
            if (best is None or self._resident_models != models or self._inflight is not None
                    or self._admissions != inflight["epoch"]
                    or (music.signature if music is not None else None) != inflight["music"]):
                return
            self._held_gib = round(max(0.0, min(inflight["growth"], inflight["baseline"] - best)), 1)
        log.info("resident weights %s hold about %.1f GiB between jobs", sorted(models), self._held_gib)

    #: how long after a free the admission waits for memory to come back
    _settle_seconds = 30.0
    _settle_poll = 1.0
    #: give up early once MemAvailable has stopped rising for this long
    _settle_flat_seconds = 6.0
    _freed_at = -1e9
    #: held-memory measurement after a cold job (0 disables)
    _held_settle_delay = 3.0
    _held_measure_seconds = 20.0

    def _run(self, job: Job, graph: dict, timeout: float, thumbnail_node: str | None, *,
             wait_on_memory: bool = False) -> None:
        self._last_activity = time.monotonic()
        held_before = self._resident_models
        job.cold_start = self._switch_models(job.workflow)
        job.status = "running"
        job.started_at = time.time()
        completed = False
        try:
            try:
                self._admit(job, held_before)
            except InsufficientMemoryError as exc:
                if wait_on_memory and exc.retryable:
                    job.status, job.started_at = "queued", None
                    raise
                job.error_code = exc.code
                raise
            job.waiting = None
            job.prompt_id = self.comfy.submit(graph, self._client_id)
            log.info("job %s -> comfy prompt %s (%s)", job.id, job.prompt_id, job.workflow)
            result = self.comfy.wait(job.prompt_id, timeout=timeout, thumbnail_node=thumbnail_node)
            job.artefacts = result.artefacts
            if job.primary() is None:
                raise UpstreamError(f"ComfyUI prompt {job.prompt_id} produced no {job.kind} output")
            job.status = "completed"
            job.finished_at = time.time()
            completed = True
            log.info("job %s completed in %.1fs -> %s", job.id, result.elapsed_seconds,
                     [a.filename for a in result.artefacts])
        except RouterError as exc:
            if job.status != "queued":
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
            self._finish_inflight(job, completed)
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

    def _memory_view(self) -> dict:
        cfg = self.cfg
        foot = {"image": cfg.footprint_image_gib, "video": cfg.footprint_video_gib,
                "keyframe_edit": cfg.footprint_keyframe_gib}
        warm = None
        if self._resident_models and self._held_gib is not None:
            kind_foot = cfg.footprint_video_gib if self._resident_kind == "gx-video" else cfg.footprint_image_gib
            warm = round(max(cfg.warm_growth_floor_gib, kind_foot - self._held_gib), 1)
        return {"available_gib": _round(self._mem_available_gib()),
                "reserve_gib": cfg.reserve_gib,
                "footprint_gib": foot,
                # MemAvailable a job needs before it starts on an otherwise idle router
                "need_gib": {**{k: round(v + cfg.reserve_gib, 1) for k, v in foot.items()},
                             "warm": round((warm if warm is not None else cfg.warm_growth_floor_gib)
                                           + cfg.reserve_gib, 1)},
                "warm_growth_gib": warm,
                "resident_held_gib": self._held_gib,
                "pending_gib": self.pending_gib(),
                "node_capacity_gib": cfg.node_capacity_gib,
                "admission": "projected MemAvailable after the job must stay >= reserve (D-038)"}

    def health(self) -> dict:
        holder, since = self.slot.held_by()
        music = self.music.state(fresh=False)
        status: dict = {
            "status": "ok",
            "service": "gx-media-router",
            "version": __version__,
            "busy": holder is not None,
            "held_by": holder,
            "held_for_seconds": round(time.time() - since, 1) if since else 0.0,
            "video_queue_depth": self._video_queue.qsize(),
            "workflows": self.workflows.names(),
            "uploads_enabled": self.inputs.available,
            "resident_models": sorted(self._resident_models),
            "resident_alias": self._resident_kind,
            "idle_seconds": round(time.monotonic() - self._last_activity, 1),
            "idle_free_seconds": self.cfg.idle_free_seconds,
            "memory": self._memory_view(),
            "tenants": {"gx-music": music.public() if music is not None else None},
            "waiting": [{"id": j.public()["id"], "gx_id": j.id, "since": (j.waiting or {}).get("since"),
                         "reason": (j.waiting or {}).get("reason"), "blocker": (j.waiting or {}).get("blocker")}
                        for j in self.jobs.snapshot() if j.waiting and j.status == "queued"],
            "policy": {**self.policy.state(), "pin_honoured": self.pin_honoured()},
            "last_refusal": self.last_refusal,
            "last_eviction": self.last_eviction,
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


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 1)
