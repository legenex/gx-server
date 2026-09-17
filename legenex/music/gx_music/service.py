"""Job orchestration: one worker, one generation at a time, honest states."""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from . import store as st
from . import validation as v
from .audio import analyze_wav, sha256_file, sniff
from .config import ENGINE_TMP, Config
from .engine import READY, EngineController, meminfo
from .errors import (EngineError, MusicError, NotFoundError, ConflictError, ResourceWait,
                     TooLargeError, UnavailableError, ValidationError)

log = logging.getLogger("gx_music.service")

FORMATS = {"wav": ("track-{i}.wav", "audio/wav"),
           "flac": ("track-{i}.flac", "audio/flac"),
           "mp3": ("track-{i}.mp3", "audio/mpeg")}
UPLOAD_EXT = {"wav": ".wav", "flac": ".flac", "mp3": ".mp3", "ogg": ".ogg", "m4a": ".m4a"}


class MusicService:
    def __init__(self, cfg: Config, store: st.Store, engine: EngineController) -> None:
        self.cfg = cfg
        self.store = store
        self.engine = engine
        self.caps = v.Capabilities.for_model(cfg.model.dit_name)
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._current: str | None = None
        self._mem_low: dict[str, float] = {}
        self._requeues: dict[str, int] = {}
        self._disk_cache: dict | None = None
        for d in (cfg.jobs_dir, cfg.uploads_dir, cfg.data_root / "api_audio"):
            d.mkdir(parents=True, exist_ok=True)

    # --------------------------------------------------------- lifecycle --
    def start(self) -> None:
        n = self.store.recover_interrupted()
        if n:
            log.warning("marked %d interrupted job(s) as failed", n)
        self.engine.reconcile()
        self._threads = [threading.Thread(target=self._worker, name="gx-music-worker", daemon=True),
                         threading.Thread(target=self._reaper, name="gx-music-reaper", daemon=True)]
        for t in self._threads:
            t.start()

    def stop(self, timeout: float = 10) -> None:
        self._stop.set()
        self._wake.set()
        for t in getattr(self, "_threads", []):
            t.join(timeout)

    # ------------------------------------------------------------ submit --
    def submit(self, operation: str, body: Any) -> dict:
        builders = {"generate": v.generation, "remix": v.remix, "edit": v.edit, "extend": v.extend}
        if not isinstance(body, dict):
            raise ValidationError("request body must be a JSON object")
        req = builders[operation](body, self.caps, max_duration=self.cfg.max_duration_s)
        # Resolve sources, inherited text and ranges now so bad input is a
        # 400/404 immediately and the stored request shows what really runs.
        # (Idempotent: the worker resolves again against the same files.)
        self._prepare_request(req)
        if self.store.count_active() >= self.cfg.max_queue:
            raise UnavailableError("the music queue is full; try again shortly", code="queue_full")
        job_id = self.store.create_job(
            operation=req.operation, title=req.title or _auto_title(req), request=_serialize(req),
            model=self.model_identity(), parent_job_id=req.parent_job_id, parent_index=req.parent_index,
            source=req.source.as_dict() if req.source else None)
        self.store.event("job_submitted", job_id=job_id, operation=operation)
        self._wake.set()
        return self.job_view(job_id)

    def cancel(self, job_id: str) -> dict:
        job = self._job(job_id)
        if job["status"] in st.TERMINAL:
            raise ConflictError(f"the job is already {job['status']}")
        if job["status"] in (st.QUEUED, st.WAITING):
            self.store.update_job(job_id, cancel_requested=1)
            self.store.transition(job_id, st.CANCELLED, "cancelled before it started")
        else:
            # Upstream has no task-cancel; the render finishes and is discarded.
            self.store.update_job(job_id, cancel_requested=1, detail="cancelling — the current step will finish first")
        self.store.event("job_cancel", job_id=job_id)
        return self.job_view(job_id)

    def delete(self, job_id: str) -> None:
        job = self._job(job_id)
        if job["status"] not in st.TERMINAL:
            raise ConflictError("cancel the job before deleting it")
        shutil.rmtree(self.cfg.jobs_dir / job_id, ignore_errors=True)
        self.store.delete_job(job_id)
        self.store.event("job_deleted", job_id=job_id)

    # ------------------------------------------------------------ uploads --
    def add_upload(self, data: bytes, filename: str) -> dict:
        if not data:
            raise ValidationError("the upload is empty")
        if len(data) > self.cfg.max_upload_bytes:
            raise TooLargeError(f"audio uploads are limited to {self.cfg.max_upload_bytes // 1048576} MB")
        kind = sniff(data[:16])
        if kind is None:
            raise ValidationError("unsupported audio file; use WAV, FLAC, MP3, OGG or M4A", code="invalid_source")
        upload_id = st.new_id("upl")
        stored = upload_id + UPLOAD_EXT[kind]
        path = self.cfg.uploads_dir / stored
        tmp = path.with_suffix(".part")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        try:
            info = self.engine.probe(f"{ENGINE_TMP}/uploads/{stored}")
        except EngineError as exc:
            path.unlink(missing_ok=True)
            raise ValidationError(exc.message, code="invalid_source") from exc
        if not 1.0 <= info["duration_s"] <= self.cfg.max_duration_s:
            path.unlink(missing_ok=True)
            raise ValidationError(f"reference audio must be between 1 s and {self.cfg.max_duration_s} s long",
                                  code="invalid_source")
        safe_name = "".join(ch for ch in os.path.basename(filename or "") if ch.isprintable())[:120] or stored
        row = dict(id=upload_id, created_at=time.time(), filename=safe_name, container=kind,
                   size_bytes=len(data), sha256=sha256_file(path), duration_s=info["duration_s"],
                   sample_rate=info["sample_rate"], channels=info["channels"], stored_name=stored)
        self.store.add_upload(**row)
        self.store.event("upload", upload_id=upload_id, size=len(data), container=kind)
        return _upload_view(row)

    def get_upload(self, upload_id: str) -> dict:
        row = self.store.get_upload(upload_id)
        if not row:
            raise NotFoundError("no such upload")
        return _upload_view(row)

    # ------------------------------------------------------------- views --
    def _job(self, job_id: str) -> dict:
        job = self.store.get_job(job_id)
        if not job:
            raise NotFoundError("no such music job")
        return job

    def job_view(self, job_id: str) -> dict:
        return public_job(self._job(job_id))

    def list_jobs(self, status: str | None, limit: int) -> list[dict]:
        return [public_job(j) for j in self.store.list_jobs(status=status, limit=limit)]

    def lineage(self, job_id: str) -> dict:
        job = self._job(job_id)
        ancestors = []
        cur, seen = job, {job_id}
        while cur and cur.get("parent_job_id") and cur["parent_job_id"] not in seen:
            seen.add(cur["parent_job_id"])
            cur = self.store.get_job(cur["parent_job_id"])
            if cur:
                ancestors.append(_lineage_node(cur))

        def tree(jid: str, depth: int) -> list[dict]:
            if depth > 16:
                return []
            return [{**_lineage_node(c), "children": tree(c["id"], depth + 1)} for c in self.store.children(jid)]

        return {"job": _lineage_node(job), "ancestors": ancestors, "descendants": tree(job_id, 0)}

    def content_path(self, job_id: str, index: int, fmt: str) -> tuple[Path, str]:
        job = self._job(job_id)
        if job["status"] != st.COMPLETED:
            raise ConflictError("the track is not ready yet")
        if fmt not in FORMATS:
            raise ValidationError("format must be wav, flac or mp3")
        tracks = (job.get("result") or {}).get("tracks", [])
        if not 0 <= index < len(tracks):
            raise NotFoundError("no such track in this job")
        name, ctype = FORMATS[fmt]
        path = self.cfg.jobs_dir / job_id / name.format(i=index)
        if not path.is_file():
            raise NotFoundError(f"{fmt} is not available for this track")
        return path, ctype

    def model_identity(self) -> dict:
        return self.cfg.model.as_dict()

    def model_info(self) -> dict:
        return {
            "alias": "gx-music",
            "task": "music-generation",
            "node": "gx10-02",
            "identity": self.model_identity(),
            "runtime": {"name": "ACE-Step 1.5 REST server (acestep.api_server)",
                        "lm_backend": self.cfg.engine_lm_backend, "image": self.cfg.model.image},
            "capabilities": self.caps.as_dict(self.cfg.max_duration_s),
            "disk": self.disk_usage(),
            "engine": self.engine.snapshot(),
            "memory": {**meminfo(), "min_mem_available_gib_while_loaded": self._mem_low.get("MemAvailable")},
            "jobs": self.store.stats(),
            "queue": {"active": self.store.count_active(), "current_job": self._current,
                      "max": self.cfg.max_queue},
            "policy": {
                "workload_class": "medium", "estimated_gib": self.cfg.engine_estimate_gib,
                "reserve_gib": self.cfg.reserve_gib, "idle_unload_s": self.cfg.idle_unload_s,
                "resource_wait_s": self.cfg.resource_wait_s,
                "evict_idle_comfy_weights": self.cfg.evict_comfy, "evict_gx_reason": self.cfg.evict_reason,
                "gx_max": "never loads while gx-max-rank1 runs or a gx-max hold is fresh; "
                          "unloads immediately when either appears",
                "maintenance": "no new loads while node2.maintenance-hold exists; an idle engine is unloaded",
                "pinned": self.engine.pinned(),
                "blocked_by": (self.engine.policy_block_reason() or (None, None))[1],
            },
        }

    def disk_usage(self) -> dict:
        if self._disk_cache is None:
            parts = {}
            for name in (self.cfg.model.dit_name, self.cfg.model.lm_name, "vae", "Qwen3-Embedding-0.6B"):
                p = self.cfg.checkpoints_dir / name
                parts[name] = sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.exists() else 0
            self._disk_cache = {"checkpoints_bytes": parts, "total_bytes": sum(parts.values())}
        return self._disk_cache

    def health(self) -> dict:
        """Open liveness plus what the node-2 media router needs to keep the
        30 GiB reserve (D-038). No job content, no secrets."""
        return {"status": "ok", "service": "gx-music", "engine": self.engine.state,
                "active_jobs": self.store.count_active(), "busy": self._current is not None,
                "pinned": self.engine.pinned(),
                "idle_seconds": round(time.time() - self.engine.last_activity, 1),
                "memory": self.engine.memory_view()}

    # ------------------------------------------------------------ worker --
    def _worker(self) -> None:
        while not self._stop.is_set():
            job = None
            try:
                job = self.store.next_queued()
            except Exception:  # noqa: BLE001
                log.exception("queue read failed")
            if job is None or self._stop.is_set():
                self._wake.wait(5)
                self._wake.clear()
                continue
            self._current = job["id"]
            try:
                self._run(job)
            except EngineError as exc:
                if not self._requeue_if_reclaimed(job["id"], exc):
                    self._fail(job["id"], exc)
            except Exception as exc:  # noqa: BLE001 - never let the worker die
                log.exception("job %s crashed", job["id"])
                msg = exc.message if isinstance(exc, MusicError) else "the track could not be generated"
                code = exc.code if isinstance(exc, MusicError) else "internal_error"
                self.store.update_job(job["id"], error_code=code, error_message=msg,
                                      retryable=int(getattr(exc, "retryable", True)))
                self.store.transition(job["id"], st.FAILED)
                self.store.event("job_failed", job_id=job["id"], code=code)
            finally:
                self._current = None

    #: how often one job may be put back in the queue after gx-max or
    #: Maintenance reclaimed the node mid-render
    MAX_REQUEUES = 3

    def _requeue_if_reclaimed(self, job_id: str, exc: EngineError) -> bool:
        """A render stopped because gx-max (or Maintenance) reclaimed node 2 is
        not the user's failure: put the job back in the queue, honestly
        labelled, and run it again once the node is free (D-036)."""
        if exc.code != "engine_interrupted":
            return False
        block = self.engine.policy_block_reason()
        if not block:
            return False
        count = self._requeues.get(job_id, 0) + 1
        if count > self.MAX_REQUEUES:
            return False
        self._requeues[job_id] = count
        self.store.update_job(job_id, engine_task_id=None, progress=None)
        self.store.transition(job_id, st.WAITING, f"{block[1]} (the interrupted render restarts then)")
        self.store.event("job_requeued", job_id=job_id, reason=block[0], attempt=count)
        log.warning("job %s re-queued after the node was reclaimed (%s)", job_id, block[0])
        return True

    def _fail(self, job_id: str, exc: Exception) -> None:
        log.error("job %s failed: %s", job_id, exc)
        msg = exc.message if isinstance(exc, MusicError) else "the track could not be generated"
        code = exc.code if isinstance(exc, MusicError) else "internal_error"
        self.store.update_job(job_id, error_code=code, error_message=msg,
                              retryable=int(getattr(exc, "retryable", True)))
        self.store.transition(job_id, st.FAILED)
        self.store.event("job_failed", job_id=job_id, code=code)

    def _cancelled(self, job_id: str) -> bool:
        job = self.store.get_job(job_id)
        return bool(job and job["cancel_requested"])

    def _run(self, job: dict) -> None:
        job_id = job["id"]
        req = _deserialize(job["request"])
        timings: dict[str, float] = {"queued_s": round(time.time() - job["created_at"], 2)}

        # 1. resolve sources and inherited text before touching the engine
        src_container = self._prepare_request(req)

        # 2. wait for the model (admission-guarded)
        waited_since = None
        while True:
            if self._stop.is_set():
                return  # left queued/waiting; recovered on the next start
            if self._cancelled(job_id):
                self.store.transition(job_id, st.CANCELLED, "cancelled while waiting")
                return
            try:
                if self.engine.state != READY and not self.engine.policy_block_reason():
                    self.store.transition(job_id, st.LOADING, "loading ACE-Step 1.5 XL")
                    self.store.update_job(job_id, started_at=time.time())
                load_s = self.engine.ensure_loaded()
                if load_s is not None:
                    timings["model_load_s"] = load_s
                break
            except ResourceWait as wait:
                waited_since = waited_since or time.time()
                self.store.transition(job_id, st.WAITING, wait.reason)
                if time.time() - waited_since > self.cfg.resource_wait_s:
                    self.store.update_job(job_id, error_code=wait.code, retryable=1,
                                          error_message=f"Gave up after {self.cfg.resource_wait_s // 60} min: {wait.reason}.")
                    self.store.transition(job_id, st.FAILED)
                    self.store.event("job_failed", job_id=job_id, code=wait.code)
                    return
                self._wake.wait(self.cfg.resource_retry_s)
                self._wake.clear()
        if waited_since:
            timings["resource_wait_s"] = round(time.time() - waited_since, 1)

        # 3. submit and follow
        self.store.transition(job_id, st.PREPARING, "sending the job to the model")
        if not job.get("started_at"):
            self.store.update_job(job_id, started_at=time.time())
        params = dict(req.engine)
        if src_container.get("source"):
            params["src_audio_path"] = src_container["source"]
        if src_container.get("reference"):
            params["reference_audio_path"] = src_container["reference"]
        t_gen = time.time()
        task_id = self.engine.submit(params)
        self.store.update_job(job_id, engine_task_id=task_id)
        results = self._follow(job_id, task_id)
        timings["generate_s"] = round(time.time() - t_gen, 2)

        if self._cancelled(job_id):
            self._discard(results)
            self.store.transition(job_id, st.CANCELLED, "cancelled; the render was discarded")
            return

        # 4. collect, verify, transcode
        self.store.transition(job_id, st.PROCESSING, "checking the audio")
        t_post = time.time()
        tracks = self._collect(job_id, req, results)
        timings["postprocess_s"] = round(time.time() - t_post, 2)
        total_audio = sum(t["duration_s"] for t in tracks)
        timings["audio_seconds"] = round(total_audio, 2)
        if total_audio:
            timings["realtime_factor"] = round(total_audio / max(timings["generate_s"], 0.01), 2)
        timings["total_s"] = round(time.time() - job["created_at"], 2)
        mem = meminfo()
        self.store.update_job(job_id, result={"tracks": tracks, "memory_after": mem}, timings=timings)
        self.store.transition(job_id, st.COMPLETED, "", 1.0)
        self.store.event("job_completed", job_id=job_id, generate_s=timings["generate_s"],
                         audio_s=timings["audio_seconds"])
        self.engine.touch()

    def _prepare_request(self, req: v.MusicRequest) -> dict:
        out: dict[str, str] = {}
        if req.source:
            host, container, duration, parent = self._source_path(req.source)
            out["source"] = container
            if parent:
                preq = parent["request"]
                if not req.engine.get("prompt"):
                    req.engine["prompt"] = preq.get("_engine", {}).get("prompt") or v.build_caption(
                        preq.get("prompt", ""), preq.get("style_tags", []))
                    req.prompt = req.prompt or preq.get("prompt", "")
                    req.style_tags = req.style_tags or list(preq.get("style_tags", []))
                if req.engine.get("lyrics") in ("", None):
                    req.engine["lyrics"] = preq.get("_engine", {}).get("lyrics", preq.get("lyrics", ""))
                    req.lyrics = req.engine["lyrics"]
                if req.engine.get("vocal_language") in ("en", None) and preq.get("parameters", {}).get("vocal_language"):
                    req.engine["vocal_language"] = preq["parameters"]["vocal_language"]
            if not req.engine.get("prompt"):
                raise ValidationError("describe the music (prompt or style tags) for this edit")
            if req.operation == "extend":
                v.resolve_extend(req, duration, self.cfg.max_duration_s)
            elif req.operation == "edit":
                v.resolve_edit_range(req, duration)
        if req.reference:
            _, container, _, _ = self._source_path(req.reference)
            out["reference"] = container
        return out

    def _source_path(self, ref: v.SourceRef) -> tuple[Path, str, float, dict | None]:
        if ref.upload_id:
            row = self.store.get_upload(ref.upload_id)
            if not row:
                raise NotFoundError("the referenced upload does not exist")
            host = self.cfg.uploads_dir / row["stored_name"]
            if not host.is_file():
                raise NotFoundError("the referenced upload is no longer available")
            return host, f"{ENGINE_TMP}/uploads/{row['stored_name']}", float(row["duration_s"] or 0), None
        job = self.store.get_job(ref.job_id or "")
        if not job:
            raise NotFoundError("the referenced track does not exist")
        if job["status"] != st.COMPLETED:
            raise ValidationError("the referenced track is not finished")
        tracks = (job.get("result") or {}).get("tracks", [])
        if not 0 <= ref.index < len(tracks):
            raise NotFoundError("the referenced track index does not exist")
        name = f"track-{ref.index}.wav"
        host = self.cfg.jobs_dir / job["id"] / name
        if not host.is_file():
            raise NotFoundError("the referenced track file is missing")
        return host, f"{ENGINE_TMP}/jobs/{job['id']}/{name}", float(tracks[ref.index]["duration_s"]), job

    def _follow(self, job_id: str, task_id: str) -> list[dict]:
        deadline = time.time() + self.cfg.generation_timeout_s
        last_stage = ""
        while time.time() < deadline:
            self._sample_memory()
            try:
                r = self.engine.poll(task_id)
            except EngineError:
                if self.engine.gxmax_block_reason() or not self.engine.docker.running(self.cfg.engine_container):
                    raise EngineError("the music engine stopped during generation (the node was reclaimed)",
                                      code="engine_interrupted", retryable=True)
                time.sleep(3)
                continue
            status = r["status"]
            first = r["results"][0] if r["results"] else {}
            if status == 1:
                return r["results"]
            if status == 2:
                log.error("engine task %s failed: %s", task_id, json.dumps(first)[:3000])
                raise EngineError(_friendly_engine_error(first.get("error") or r["progress_text"]))
            progress = first.get("progress")
            stage = str(first.get("stage") or "")
            detail = _stage_label(stage) or "generating"
            prog = float(progress) if isinstance(progress, (int, float)) and progress > 0 else None
            if detail != last_stage or prog is not None:
                self.store.transition(job_id, st.GENERATING, detail, prog)
                last_stage = detail
            time.sleep(2)
        raise EngineError("generation timed out", code="timeout", retryable=True)

    def _host_path(self, url_or_path: str) -> Path:
        from urllib.parse import parse_qs, urlparse
        raw = url_or_path
        if raw.startswith("/v1/audio"):
            raw = parse_qs(urlparse(raw).query).get("path", [""])[0]
        if not raw.startswith(ENGINE_TMP + "/"):
            raise EngineError("the music engine returned an unexpected file location")
        rel = Path(raw[len(ENGINE_TMP) + 1:])
        host = (self.cfg.data_root / rel).resolve()
        if self.cfg.data_root.resolve() not in host.parents:
            raise EngineError("the music engine returned an unexpected file location")
        return host

    def _discard(self, results: list[dict]) -> None:
        for item in results:
            try:
                self._host_path(item.get("file", "")).unlink(missing_ok=True)
            except EngineError:
                pass

    def _collect(self, job_id: str, req: v.MusicRequest, results: list[dict]) -> list[dict]:
        out_dir = self.cfg.jobs_dir / job_id
        out_dir.mkdir(parents=True, exist_ok=True)
        files = [r for r in results if r.get("file")]
        if not files:
            raise EngineError("the model finished without producing audio")
        tracks = []
        for i, item in enumerate(files):
            src = self._host_path(item["file"])
            if not src.is_file():
                raise EngineError("the generated audio file is missing")
            wav = out_dir / f"track-{i}.wav"
            shutil.move(src, wav)
            for side in src.parent.glob(src.stem + ".*"):
                side.unlink(missing_ok=True)  # repaint-cache sidecars etc.
            info = analyze_wav(wav)
            if info.silent:
                raise EngineError("the model produced silence; try again with a different seed or prompt")
            if info.duration_s < 1.0:
                raise EngineError("the model produced an unusably short clip")
            self.store.transition(job_id, st.SAVING, f"encoding track {i + 1} of {len(files)}")
            files_meta = {"wav": self._file_meta(wav)}
            for fmt, args in (("flac", ["-c:a", "flac", "-sample_fmt", "s32", "-bits_per_raw_sample", "24"]),
                              ("mp3", ["-c:a", "libmp3lame", "-b:a", "320k"])):
                target = out_dir / f"track-{i}.{fmt}"
                r = self.engine.media_tool("ffmpeg", [
                    "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                    "-i", f"{ENGINE_TMP}/jobs/{job_id}/{wav.name}", *args,
                    "-metadata", f"title={req.title or 'gx-music'}", "-metadata", "encoded_by=gx-music",
                    f"{ENGINE_TMP}/jobs/{job_id}/{target.name}"])
                if r.returncode == 0 and target.is_file() and target.stat().st_size > 0:
                    files_meta[fmt] = self._file_meta(target)
                else:
                    log.error("transcode %s failed for %s: %s", fmt, job_id, r.stderr[-1500:])
            metas = {k: (None if val in ("N/A", "", None) else val)
                     for k, val in (item.get("metas") or {}).items()}
            seed = req.seeds[i] if i < len(req.seeds) else None
            tracks.append({
                "index": i, "seed": seed,
                "duration_s": info.duration_s, "sample_rate": info.sample_rate, "channels": info.channels,
                "bit_depth": info.bits, "encoding": info.encoding,
                "peak": info.peak, "rms_dbfs": info.rms_dbfs, "waveform": info.waveform,
                "files": files_meta,
                "caption": item.get("prompt") or req.engine.get("prompt", ""),
                "lyrics": item.get("lyrics") or req.engine.get("lyrics", ""),
                "bpm": metas.get("bpm"), "key": metas.get("keyscale") or None,
                "time_signature": metas.get("timesignature") or None,
                "genres": metas.get("genres") or None,
                "engine_seed": item.get("seed_value"),
                "dit_model": item.get("dit_model") or self.cfg.model.dit_name,
                "lm_model": item.get("lm_model") or (self.cfg.model.lm_name if req.engine.get("thinking") else None),
            })
        return tracks

    @staticmethod
    def _file_meta(path: Path) -> dict:
        return {"bytes": path.stat().st_size, "sha256": sha256_file(path)}

    def _sample_memory(self) -> None:
        m = meminfo()
        for k in ("MemAvailable",):
            if k in m and (k not in self._mem_low or m[k] < self._mem_low[k]):
                self._mem_low[k] = m[k]
        if "SwapUsed" in m and m["SwapUsed"] > self._mem_low.get("SwapUsedMax", -1):
            self._mem_low["SwapUsedMax"] = m["SwapUsed"]

    # ------------------------------------------------------------ reaper --
    def _reaper(self) -> None:
        while not self._stop.wait(15):
            try:
                self.reap_once()
            except Exception:  # noqa: BLE001
                log.exception("reaper iteration failed")

    def reap_once(self) -> str | None:
        """One lifecycle pass. Returns what it did (for tests and the log)."""
        if self.engine.state != READY and not self.engine.gxmax_block_reason():
            return None
        self._sample_memory()
        block = self.engine.gxmax_block_reason()
        if block:
            if self.engine.state != READY and not self.engine.docker.exists(self.cfg.engine_container):
                return "held"  # nothing loaded: nothing to tear down (no log noise every tick)
            log.warning("gx-max claims node 2; unloading gx-music now")
            self.engine.unload("gx-max drain")
            self.store.event("engine_unloaded", reason="gx-max drain")
            return "gx-max"
        if self.engine.maintenance_reason() and self._current is None:
            # Maintenance: let the running track finish, then hand the memory back.
            info = self.engine.unload("maintenance mode")
            self.store.event("engine_unloaded", **info)
            return "maintenance"
        if not self.engine.docker.running(self.cfg.engine_container):
            self.engine.reconcile()
            self.store.event("engine_lost")
            return "lost"
        idle = time.time() - self.engine.last_activity
        if (self.cfg.idle_unload_s and idle > self.cfg.idle_unload_s
                and self._current is None and self.store.count_active() == 0):
            if self.engine.pin_honoured():
                return "pinned"
            info = self.engine.unload(f"idle for {int(idle)} s")
            self.store.event("engine_unloaded", **info)
            return "idle"
        return None

    def load(self) -> dict:
        block = self.engine.policy_block_reason()
        if block:
            raise UnavailableError(block[1], code=block[0])
        if self._current:
            return self.engine.snapshot()
        try:
            self.engine.ensure_loaded()
        except ResourceWait as wait:
            raise UnavailableError(wait.reason, code=wait.code) from wait
        self.store.event("engine_loaded", seconds=self.engine.last_load_seconds)
        return self.engine.snapshot()

    def unload(self, *, if_idle: bool = False) -> dict:
        """Unload the engine. ``if_idle`` (the media router making room, D-038)
        additionally refuses while music jobs are queued or a pin is honoured,
        so a scheduler never takes the engine away from waiting work."""
        if self._current:
            raise ConflictError("a track is being generated; cancel it or wait before unloading")
        if if_idle:
            if self.store.count_active():
                raise ConflictError("music jobs are queued; the engine stays loaded for them")
            if self.engine.pinned() and self.engine.pin_honoured():
                raise ConflictError("gx-music is pinned")
            if self.engine.state != READY:
                return {"reason": f"engine is {self.engine.state}", "container_gone":
                        not self.engine.docker.exists(self.cfg.engine_container), "noop": True}
        info = self.engine.unload("unloaded while idle to make room on gx10-02 (30 GiB reserve)" if if_idle
                                  else "requested")
        self.store.event("engine_unloaded", **info)
        return info


# --------------------------------------------------------------- helpers --
def _serialize(req: v.MusicRequest) -> dict:
    d = req.public()
    d["_engine"] = req.engine
    return d


def _deserialize(d: dict) -> v.MusicRequest:
    def ref(x: dict | None) -> v.SourceRef | None:
        return v.SourceRef(job_id=x.get("job_id"), index=x.get("index", 0), upload_id=x.get("upload_id")) if x else None
    return v.MusicRequest(
        operation=d["operation"], title=d["title"], prompt=d["prompt"], style_tags=d["style_tags"],
        lyrics=d["lyrics"], instrumental=d["instrumental"], output_format=d["output_format"],
        batch_size=d["batch_size"], seeds=d["seeds"], source=ref(d.get("source")),
        reference=ref(d.get("reference")), parent_job_id=d.get("parent_job_id"),
        parent_index=d.get("parent_index"), engine=dict(d["_engine"]), extend=d.get("extend"))


def _auto_title(req: v.MusicRequest) -> str:
    base = req.prompt or ", ".join(req.style_tags[:3]) or req.engine.get("sample_query", "") or "Untitled"
    base = base[:60].rstrip(" ,")
    return {"generate": base, "remix": f"Remix — {base}", "edit": f"Edit — {base}",
            "extend": f"Extended — {base}"}.get(req.operation, base)


def _stage_label(stage: str) -> str:
    s = stage.lower()
    if not s or s in {"queued", "running"}:
        return "generating"
    if "lm" in s or "code" in s or "think" in s or "cot" in s:
        return "planning the song (language model)"
    if "decode" in s or "vae" in s:
        return "rendering audio"
    if "diffusion" in s or "dit" in s or "step" in s:
        return "generating music"
    return "generating"


def _friendly_engine_error(raw: Any) -> str:
    text = str(raw or "").lower()
    if "out of memory" in text or "oom" in text:
        return "the media node ran out of memory while generating; try a shorter track"
    if "audio" in text and ("load" in text or "decode" in text):
        return "the source audio could not be read"
    return "the music model could not generate this track"


def _lineage_node(job: dict) -> dict:
    return {"id": job["id"], "operation": job["operation"], "status": job["status"],
            "title": job["title"], "created_at": job["created_at"],
            "parent_job_id": job.get("parent_job_id"), "parent_index": job.get("parent_index")}


def _upload_view(row: dict) -> dict:
    return {"id": row["id"], "object": "music.upload", "filename": row["filename"],
            "container": row["container"], "size_bytes": row["size_bytes"], "sha256": row["sha256"],
            "duration_s": row["duration_s"], "sample_rate": row["sample_rate"],
            "channels": row["channels"], "created_at": row["created_at"]}


def public_job(job: dict) -> dict:
    req = dict(job["request"])
    req.pop("_engine", None)
    now = time.time()
    started = job.get("started_at")
    end = job.get("finished_at") or now
    result = job.get("result") or {}
    tracks = []
    for t in result.get("tracks", []):
        t = dict(t)
        t["files"] = {fmt: {**meta, "url": f"/v1/music/{job['id']}/content?index={t['index']}&format={fmt}"}
                      for fmt, meta in t.get("files", {}).items()}
        tracks.append(t)
    view = {
        "id": job["id"], "object": "music.job", "operation": job["operation"],
        "status": job["status"], "detail": job["detail"], "progress": job["progress"],
        "created_at": job["created_at"], "started_at": started, "finished_at": job.get("finished_at"),
        "elapsed_s": round(end - job["created_at"], 1),
        "title": job["title"], "request": req, "tracks": tracks,
        "timings": job.get("timings") or {}, "model": job["model"],
        "parent_job_id": job.get("parent_job_id"), "parent_index": job.get("parent_index"),
        "source": job.get("source"), "cancel_requested": job["cancel_requested"],
        "error": ({"code": job["error_code"], "message": job["error_message"], "retryable": job["retryable"]}
                  if job.get("error_code") else None),
        "links": {"self": f"/v1/music/{job['id']}", "lineage": f"/v1/music/{job['id']}/lineage"},
    }
    return view
