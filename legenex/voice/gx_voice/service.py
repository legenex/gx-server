"""Job orchestration: the variant router, one worker, honest states.

A job renders ``takes`` × segments × paragraphs × chunks utterances. The
worker groups them by model variant (starting with the variant that is
already resident) so a mixed dialogue switches models as few times as
possible, then assembles every take in script order with deterministic
pauses, applies the optional pitch-preserving tempo change and encodes MP3.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from . import __version__
from . import audio as au
from . import store as st
from . import validation as v
from .config import ENGINE_WORK, RUNTIME, TOKENIZER, VARIANTS, Config
from .engine import READY, EngineController, meminfo
from .errors import (ConflictError, EngineError, NotFoundError, ResourceWait, TooLargeError, UnavailableError,
                     ValidationError, VoiceError)

log = logging.getLogger("gx_voice.service")

UPLOAD_EXT = {"wav": ".wav", "flac": ".flac", "mp3": ".mp3", "ogg": ".ogg", "m4a": ".m4a", "webm": ".webm"}
REF_MIN_S, REF_MAX_S = 2.0, 60.0
BREATH_MS = 140          # between chunks of one paragraph
MP3_BITRATE = "192k"
FORMAT_ENCODE = {
    "mp3": ("mp3", ["-c:a", "libmp3lame", "-b:a", MP3_BITRATE]),
    "flac": ("flac", ["-c:a", "flac"]),
    "opus": ("ogg", ["-c:a", "libopus", "-b:a", "64k"]),
    "aac": ("aac", ["-c:a", "aac", "-b:a", "128k", "-f", "adts"]),
}
SPEECH_CLIENT_REF = "openai-speech"


class VoiceService:
    def __init__(self, cfg: Config, store: st.Store, engine: EngineController) -> None:
        self.cfg = cfg
        self.store = store
        self.engine = engine
        self._wake = threading.Event()
        self._done = threading.Condition()
        self._stop = threading.Event()
        self._current: str | None = None
        self._waiting: dict | None = None
        self._mem_low: dict[str, float] = {}
        self._last_retention = 0.0
        for d in (cfg.jobs_dir, cfg.references_dir, cfg.prompts_dir):
            d.mkdir(parents=True, exist_ok=True)

    # --------------------------------------------------------- lifecycle --
    def start(self) -> None:
        n = self.store.recover_interrupted()
        if n:
            log.warning("marked %d interrupted job(s) as failed", n)
        self.engine.reconcile()
        self._threads = [threading.Thread(target=self._worker, name="gx-voice-worker", daemon=True),
                         threading.Thread(target=self._reaper, name="gx-voice-reaper", daemon=True)]
        for t in self._threads:
            t.start()

    def stop(self, timeout: float = 10) -> None:
        self._stop.set()
        self._wake.set()
        for t in getattr(self, "_threads", []):
            t.join(timeout)

    # ------------------------------------------------------------ voices --
    def resolve_voice(self, spec: dict) -> tuple[dict, str, str]:
        """(render spec, default instructions, default language) for a validated spec."""
        if spec["kind"] != "saved":
            return spec, "", "auto"
        rec = self.store.get_voice(spec["voice_id"])
        if not rec:
            raise NotFoundError(f"saved voice {spec['voice_id']} is not known on gx10-02")
        return rec["spec"], rec["instructions"], rec["language"]

    def voice_by_name(self, name: str) -> dict:
        """OpenAI `voice`: a voice id, a preset speaker or a saved voice name."""
        if v.VOICE_ID.match(name):
            return {"kind": "saved", "voice_id": name}
        key = name.strip().lower().replace(" ", "_")
        if key.startswith("preset:"):
            key = key[7:]
        if key in v.SPEAKERS:
            return {"kind": "preset", "speaker": key}
        matches = self.store.voices_named(name)
        if len(matches) == 1:
            return {"kind": "saved", "voice_id": matches[0]["id"]}
        if len(matches) > 1:
            raise ValidationError(f"more than one saved voice is called {name!r}; use its voice id")
        raise ValidationError(f"unknown voice {name!r}: use a preset ({', '.join(v.SPEAKERS)}), "
                              "a saved voice name or a voice id")

    def put_voice(self, voice_id: str, body: Any) -> dict:
        rec = v.voice_record(voice_id, body)
        if rec["spec"]["kind"] == "reference" and not self.store.get_ref(rec["spec"]["reference_id"]):
            raise NotFoundError("the voice's reference clip is not on gx10-02; upload it first")
        out = self.store.put_voice(rec)
        self.store.event("voice_put", voice_id=voice_id, version=rec["version"])
        return out

    def delete_voice(self, voice_id: str) -> dict:
        if not v.VOICE_ID.match(voice_id):
            raise ValidationError("invalid voice id")
        gone = self.store.delete_voice(voice_id)
        self.store.event("voice_deleted", voice_id=voice_id)
        return {"id": voice_id, "deleted": gone}

    # -------------------------------------------------------- references --
    def add_reference(self, data: bytes, filename: str) -> dict:
        if not data:
            raise ValidationError("the upload is empty")
        if len(data) > self.cfg.max_upload_bytes:
            raise TooLargeError(f"reference clips are limited to {self.cfg.max_upload_bytes // 1048576} MB")
        kind = au.sniff(data[:16])
        if kind is None:
            raise ValidationError("unsupported audio; use WAV, FLAC, MP3, OGG, M4A or WebM", code="invalid_source")
        digest = hashlib.sha256(data).hexdigest()
        ref_id = f"ref-{digest[:32]}"
        existing = self.store.get_ref(ref_id)
        if existing and (self.cfg.references_dir / f"{ref_id}.wav").is_file():
            return _ref_view(existing)
        orig = self.cfg.references_dir / f"{ref_id}.orig{UPLOAD_EXT[kind]}"
        wav = self.cfg.references_dir / f"{ref_id}.wav"
        tmp = orig.with_name(orig.name + ".part")
        tmp.write_bytes(data)
        os.replace(tmp, orig)
        try:
            info = self.engine.probe(f"{ENGINE_WORK}/references/{orig.name}")
            if not REF_MIN_S <= info["duration_s"] <= REF_MAX_S:
                raise ValidationError(f"a reference clip must be {REF_MIN_S:.0f}-{REF_MAX_S:.0f} seconds long "
                                      f"(this one is {info['duration_s']:.1f} s)", code="invalid_source")
            # Normalise once: 24 kHz mono 16-bit, what the model's speaker encoder expects.
            r = self.engine.media_tool("ffmpeg", [
                "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i",
                f"{ENGINE_WORK}/references/{orig.name}", "-vn", "-ac", "1", "-ar", "24000",
                "-c:a", "pcm_s16le", f"{ENGINE_WORK}/references/{wav.name}"], timeout=120)
            if r.returncode != 0 or not wav.is_file():
                raise ValidationError("the reference clip could not be decoded", code="invalid_source")
            level = au.analyze_wav(wav)
            if level.silent:
                raise ValidationError("the reference clip is silent", code="invalid_source")
        except (VoiceError, OSError) as exc:
            orig.unlink(missing_ok=True)
            wav.unlink(missing_ok=True)
            if isinstance(exc, EngineError):
                raise ValidationError(exc.message, code="invalid_source") from exc
            raise
        safe = "".join(ch for ch in os.path.basename(filename or "") if ch.isprintable())[:120] or orig.name
        row = dict(id=ref_id, created_at=time.time(), filename=safe, container=kind, size_bytes=len(data),
                   sha256=digest, duration_s=level.duration_s)
        self.store.add_ref(**row)
        self.store.event("reference_added", ref_id=ref_id, seconds=level.duration_s)
        return _ref_view(self.store.get_ref(ref_id) or row)

    def get_reference(self, ref_id: str) -> dict:
        row = self.store.get_ref(ref_id) if v.REF_ID.match(ref_id) else None
        if not row:
            raise NotFoundError("no such reference clip")
        return _ref_view(row)

    def delete_reference(self, ref_id: str) -> dict:
        self.get_reference(ref_id)
        users = self.store.refs_in_use(ref_id)
        if users:
            raise ConflictError(f"the clip is used by saved voice(s) {', '.join(users[:5])}; delete them first")
        for f in self.cfg.references_dir.glob(f"{ref_id}.*"):
            f.unlink(missing_ok=True)
        shutil.rmtree(self.cfg.prompts_dir / ref_id, ignore_errors=True)
        self.store.delete_ref(ref_id)
        self.store.event("reference_deleted", ref_id=ref_id)
        return {"id": ref_id, "deleted": True}

    def _ref_ready(self, ref_id: str) -> None:
        if not self.store.get_ref(ref_id) or not (self.cfg.references_dir / f"{ref_id}.wav").is_file():
            raise NotFoundError("the reference clip is not on gx10-02; upload it again")

    # ------------------------------------------------------------ submit --
    def submit(self, body: Any, *, client_ref: str | None = None) -> dict:
        req = v.job_request(body, max_chars=self.cfg.max_job_chars)
        if client_ref:
            req["client_ref"] = client_ref
        plan = self.plan(req)  # resolve voices and references now: bad input fails with 400/404 immediately
        req["resolved_variants"] = sorted({seg["variant"] for seg in plan["segments"]})
        if self.store.count_active() >= self.cfg.max_queue:
            raise UnavailableError("the voice queue is full; try again shortly", code="queue_full")
        title = req["title"] or _auto_title(req)
        job_id = self.store.create_job(operation=req["operation"], title=title, request=req,
                                       client_ref=req["client_ref"])
        self.store.event("job_submitted", job_id=job_id, operation=req["operation"])
        self._wake.set()
        return self.job_view(job_id)

    def plan(self, req: dict) -> dict:
        """The render plan: every utterance with its variant, text, voice and seed."""
        segments = []
        notes: list[str] = []
        for si, seg in enumerate(req["segments"]):
            spec, default_instr, default_lang = self.resolve_voice(seg["voice"])
            variant = v.variant_for(spec)
            instructions = "; ".join(x for x in (default_instr, seg["instructions"]) if x)
            lang = req["language"] if req["language"] != "auto" else default_lang
            if spec["kind"] == "reference":
                self._ref_ready(spec["reference_id"])
                if instructions:
                    notes.append(f"line {si + 1}: style instructions are not supported by the voice-clone model "
                                 "and were not applied (the delivery follows the reference clip)")
            segments.append({"index": si, "spec": spec, "variant": variant, "language": lang,
                             "instructions": instructions if variant != "base" else "",
                             "paragraphs": v.chunk_text(seg["text"]),
                             "pause_ms": seg["pause_ms"] if seg["pause_ms"] is not None else req["pause_ms"]})
        units = []
        for take in range(req["takes"]):
            for seg in segments:
                n = 0
                for pi, para in enumerate(seg["paragraphs"]):
                    for ci, text in enumerate(para):
                        units.append({"take": take, "seg": seg["index"], "para": pi, "chunk": ci,
                                      "variant": seg["variant"], "text": text,
                                      "seed": v.chunk_seed(req["seed"] + take, n)})
                        n += 1
        return {"segments": segments, "units": units, "notes": notes}

    def cancel(self, job_id: str) -> dict:
        job = self._job(job_id)
        if job["status"] in st.TERMINAL:
            raise ConflictError(f"the job is already {job['status']}")
        self.store.update_job(job_id, cancel_requested=1)
        if job["status"] in (st.QUEUED, st.WAITING):
            self.store.transition(job_id, st.CANCELLED, "cancelled before it started")
        else:
            self.store.update_job(job_id, detail="cancelling after the current line")
        self.store.event("job_cancel", job_id=job_id)
        self._notify()
        return self.job_view(job_id)

    def delete(self, job_id: str) -> None:
        job = self._job(job_id)
        if job["status"] not in st.TERMINAL:
            raise ConflictError("cancel the job before deleting it")
        shutil.rmtree(self.cfg.jobs_dir / job_id, ignore_errors=True)
        self.store.delete_job(job_id)
        self.store.event("job_deleted", job_id=job_id)

    # ------------------------------------------------------------- views --
    def _job(self, job_id: str) -> dict:
        job = self.store.get_job(job_id) if v.JOB_ID.match(job_id) else None
        if not job:
            raise NotFoundError("no such voice job")
        return job

    def job_view(self, job_id: str) -> dict:
        view = public_job(self._job(job_id))
        if view["status"] == st.WAITING and self._waiting and self._waiting.get("job_id") == job_id:
            view["waiting"] = {k: self._waiting[k] for k in ("code", "reason", "since")}
        return view

    def list_jobs(self, status: str | None, limit: int) -> list[dict]:
        return [public_job(j) for j in self.store.list_jobs(status=status, limit=limit)]

    def content_path(self, job_id: str, take: int, fmt: str) -> tuple[Path, str]:
        job = self._job(job_id)
        if job["status"] != st.COMPLETED:
            raise ConflictError("the audio is not ready yet")
        takes = (job.get("result") or {}).get("takes", [])
        if not 0 <= take < len(takes):
            raise NotFoundError("no such take in this job")
        if fmt not in v.SPEECH_FORMATS:
            raise ValidationError(f"format must be one of: {', '.join(v.SPEECH_FORMATS)}")
        wav = self.cfg.jobs_dir / job_id / f"take-{take}.wav"
        if not wav.is_file():
            raise NotFoundError("the audio of this job has been removed")
        if fmt in ("wav", "pcm"):
            return wav, v.SPEECH_FORMATS[fmt]
        ext = FORMAT_ENCODE[fmt][0]
        target = wav.with_suffix(f".{ext}")
        if not target.is_file():
            self._encode(job_id, wav.name, target.name, FORMAT_ENCODE[fmt][1])
        return target, v.SPEECH_FORMATS[fmt]

    def _encode(self, job_id: str, src: str, dst: str, args: list[str], extra_in: list[str] | None = None) -> None:
        base = f"{ENGINE_WORK}/jobs/{job_id}"
        r = self.engine.media_tool("ffmpeg", ["-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                                              "-i", f"{base}/{src}", *(extra_in or []), *args,
                                              f"{base}/{dst}"], timeout=300)
        out = self.cfg.jobs_dir / job_id / dst
        if r.returncode != 0 or not out.is_file() or out.stat().st_size == 0:
            log.error("ffmpeg %s -> %s failed: %s", src, dst, str(r.stderr)[-1500:])
            out.unlink(missing_ok=True)
            raise EngineError("the audio could not be encoded", code="encode_failed")

    def model_info(self) -> dict:
        return {
            "alias": "gx-voice", "task": "text-to-speech", "node": "gx10-02", "version": __version__,
            "family": "Qwen3-TTS 12Hz 1.7B", "licence": "apache-2.0",
            "variants": {k: {**var.as_dict(), "installed": (self.cfg.models_dir / var.directory
                                                              / "config.json").is_file()}
                         for k, var in VARIANTS.items()},
            "tokenizer": TOKENIZER, "runtime": {**RUNTIME, "image": self.cfg.image},
            "router": {"preset": "custom", "design": "design", "reference": "base", "saved": "by the voice's kind"},
            "speakers": [{"id": k, "label": lab, "description": d, "native_language": n}
                         for k, (lab, d, n) in v.SPEAKERS.items()],
            "languages": list(v.LANGUAGES),
            "limits": {"segments": v.MAX_SEGMENTS, "takes": v.MAX_TAKES, "text_chars": v.MAX_TEXT,
                       "job_chars": self.cfg.max_job_chars, "speech_input_chars": 4096,
                       "reference_seconds": [REF_MIN_S, REF_MAX_S],
                       "reference_upload_mb": self.cfg.max_upload_bytes // 1048576,
                       "speed": [0.5, 2.0], "pause_ms": [0, 5000], "sampling": {
                           k: [lo, hi] for k, (lo, hi, _) in v.SAMPLING.items()}},
            "output": {"sample_rate": 24000, "channels": 1, "formats": list(v.SPEECH_FORMATS)},
            "engine": self.engine.snapshot(),
            "memory": {**meminfo(), "min_mem_available_gib_while_loaded": self._mem_low.get("MemAvailable")},
            "jobs": self.store.stats(),
            "queue": {"active": self.store.count_active(), "current_job": self._current, "max": self.cfg.max_queue},
            "policy": {
                "workload_class": "small", "estimated_gib": self.cfg.engine_estimate_gib,
                "max_resident_variants": self.cfg.engine_max_resident, "reserve_gib": self.cfg.reserve_gib,
                "idle_unload_s": self.cfg.idle_unload_s, "resource_wait_s": self.cfg.resource_wait_s,
                "evict_idle_peers": self.cfg.evict_idle_peers,
                "gx_max": "never loads while gx-max holds node 2; unloads within one reaper tick when it does",
                "maintenance": "no new loads; a running job finishes, then the engine is unloaded",
                "pinned": self.engine.pinned(),
            },
        }

    def health(self) -> dict:
        eng = self.engine.state
        busy = self._current is not None
        state = {"unloaded": "unloaded", "loading": "loading", "unloading": "unloading",
                 "failed": "error"}.get(eng, "busy" if busy else "ready")
        waiting = {k: self._waiting[k] for k in ("code", "reason", "since")} if self._waiting else None
        block = self.engine.policy_block_reason()
        return {"status": "ok", "service": "gx-voice", "version": __version__, "state": state, "engine": eng,
                "variants_loaded": list(self.engine.variants_loaded), "busy": busy,
                "active_jobs": self.store.count_active(), "pinned": self.engine.pinned(),
                "pin_honoured": self.engine.pin_honoured() if eng == READY else False,
                "idle_seconds": round(time.time() - self.engine.last_activity, 1),
                "idle_unload_after_s": self.cfg.idle_unload_s,
                "queue": {"active": self.store.count_active(), "waiting": self.store.count_status(st.WAITING)},
                "waiting": waiting, "blocked_by": block[1] if block else None,
                "memory": self.engine.memory_view()}

    # ------------------------------------------------------------ worker --
    def _notify(self) -> None:
        with self._done:
            self._done.notify_all()

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
            except Exception as exc:  # noqa: BLE001 - never let the worker die
                if not isinstance(exc, VoiceError):
                    log.exception("job %s crashed", job["id"])
                self._fail(job["id"], exc)
            finally:
                self._current = None
                self._waiting = None
                shutil.rmtree(self.cfg.jobs_dir / job["id"] / "units", ignore_errors=True)
                self._notify()

    def _fail(self, job_id: str, exc: Exception) -> None:
        msg = exc.message if isinstance(exc, VoiceError) else "the audio could not be generated"
        code = exc.code if isinstance(exc, VoiceError) else "internal_error"
        log.error("job %s failed: %s (%s)", job_id, msg, code)
        self.store.update_job(job_id, error_code=code, error_message=msg,
                              retryable=int(getattr(exc, "retryable", True)))
        self.store.transition(job_id, st.FAILED)
        self.store.event("job_failed", job_id=job_id, code=code)

    def _cancelled(self, job_id: str) -> bool:
        job = self.store.get_job(job_id)
        return bool(job and job["cancel_requested"])

    def _wait_for_engine(self, job_id: str, timings: dict) -> bool:
        waited_since = None
        while True:
            if self._stop.is_set():
                return False
            if self._cancelled(job_id):
                self.store.transition(job_id, st.CANCELLED, "cancelled while waiting")
                return False
            try:
                if self.engine.state != READY and not self.engine.policy_block_reason():
                    self.store.transition(job_id, st.LOADING, "starting the voice engine")
                load_s = self.engine.ensure_loaded()
                if load_s is not None:
                    timings["engine_start_s"] = load_s
                break
            except ResourceWait as wait:
                waited_since = waited_since or time.time()
                self._waiting = {"job_id": job_id, "code": wait.code, "reason": wait.reason, "since": waited_since}
                self.store.transition(job_id, st.WAITING, wait.reason)
                if time.time() - waited_since > self.cfg.resource_wait_s:
                    self.store.update_job(job_id, error_code=wait.code, retryable=1, error_message=(
                        f"Gave up after {self.cfg.resource_wait_s // 60} min: {wait.reason}."))
                    self.store.transition(job_id, st.FAILED)
                    self.store.event("job_failed", job_id=job_id, code=wait.code)
                    return False
                self._wake.wait(self.cfg.resource_retry_s)
                self._wake.clear()
        self._waiting = None
        if waited_since:
            timings["resource_wait_s"] = round(time.time() - waited_since, 1)
        return True

    def _run(self, job: dict) -> None:
        job_id = job["id"]
        req = job["request"]
        timings: dict[str, float] = {"queued_s": round(time.time() - job["created_at"], 2)}
        plan = self.plan(req)
        if not self._wait_for_engine(job_id, timings):
            return
        self.store.update_job(job_id, started_at=time.time(), notes=plan["notes"])
        units = plan["units"]
        order = _variant_order(units, self.engine.variants_loaded)
        unit_dir = self.cfg.jobs_dir / job_id / "units"
        unit_dir.mkdir(parents=True, exist_ok=True)
        t_gen = time.time()
        first_audio = None
        loads: dict[str, float] = {}
        rendered: dict[tuple, tuple[bytes, float]] = {}
        segs = plan["segments"]
        for n, unit in enumerate(order):
            if self._stop.is_set():
                raise EngineError("the voice service is stopping", code="interrupted", retryable=True)
            if self._cancelled(job_id):
                self.store.transition(job_id, st.CANCELLED, "cancelled; the partial audio was discarded")
                return
            block = self.engine.gxmax_block_reason()
            if block:
                raise EngineError(f"stopped: {block}", code="engine_interrupted", retryable=True)
            seg = segs[unit["seg"]]
            label = {"custom": "preset voice", "design": "designed voice", "base": "cloned voice"}[unit["variant"]]
            self.store.transition(job_id, st.GENERATING,
                                  f"speaking line {unit['seg'] + 1}, take {unit['take'] + 1} ({label})",
                                  round(n / len(order), 3))
            name = f"u{unit['take']}-{unit['seg']}-{unit['para']}-{unit['chunk']}.wav"
            params = {"variant": unit["variant"], "text": unit["text"], "language": seg["language"],
                      "seed": unit["seed"], "sampling": req["sampling"],
                      "max_new_tokens": v.max_new_tokens(unit["text"]),
                      "out": f"jobs/{job_id}/units/{name}"}
            spec = seg["spec"]
            if unit["variant"] == "custom":
                params["speaker"] = spec["speaker"]
                params["instruct"] = seg["instructions"]
            elif unit["variant"] == "design":
                params["instruct"] = ". ".join(x for x in (spec["description"], seg["instructions"]) if x)
            else:
                params["reference"] = self._reference_params(spec)
            self._sample_memory()
            result = self._synthesize(params)
            if result.get("variant_load_s"):
                loads[unit["variant"]] = round(loads.get(unit["variant"], 0.0) + result["variant_load_s"], 2)
            path = unit_dir / name
            self._check_unit(path, unit, params["max_new_tokens"])
            rate, pcm = au.read_pcm16(path)
            if rate != 24000:
                raise EngineError("the model returned an unexpected sample rate")
            rendered[(unit["take"], unit["seg"], unit["para"], unit["chunk"])] = (pcm, result.get("generate_s", 0.0))
            if first_audio is None and unit["take"] == 0:
                first_audio = round(time.time() - t_gen, 2)
        timings["generate_s"] = round(time.time() - t_gen, 2)
        if first_audio is not None:
            timings["first_audio_s"] = first_audio
        if loads:
            timings["variant_load_s"] = loads

        if self._cancelled(job_id):
            self.store.transition(job_id, st.CANCELLED, "cancelled; the audio was discarded")
            return
        self.store.transition(job_id, st.PROCESSING, "assembling the takes", 1.0)
        t_post = time.time()
        takes = []
        for take in range(req["takes"]):
            takes.append(self._assemble(job_id, req, segs, take, rendered))
        timings["postprocess_s"] = round(time.time() - t_post, 2)
        audio_s = sum(t["duration_s"] for t in takes)
        timings["audio_s"] = round(audio_s, 2)
        speech_s = sum(len(p) / 48000 for p, _ in rendered.values())
        model_s = sum(g for _, g in rendered.values())
        if speech_s:
            # model seconds per second of speech (< 1 is faster than real time)
            timings["rtf"] = round(model_s / speech_s, 3)
        timings["total_s"] = round(time.time() - job["created_at"], 2)
        self.store.update_job(job_id, result={"takes": takes, "sample_rate": 24000, "memory_after": meminfo()},
                              timings=timings)
        shutil.rmtree(unit_dir, ignore_errors=True)
        self.store.transition(job_id, st.COMPLETED, "", 1.0)
        self.store.event("job_completed", job_id=job_id, generate_s=timings["generate_s"], audio_s=audio_s)
        self.engine.touch()

    def _synthesize(self, params: dict) -> dict:
        try:
            return self.engine.synthesize(params)
        except EngineError as exc:
            if exc.code in ("engine_unreachable",) and (self.engine.gxmax_block_reason()
                                                        or not self.engine.docker.running(self.cfg.engine_container)):
                self.engine.reconcile()
                raise EngineError("the voice engine stopped during generation (the node was reclaimed)",
                                  code="engine_interrupted", retryable=True) from exc
            raise

    def _reference_params(self, spec: dict) -> dict:
        ref_id = spec["reference_id"]
        self.store.touch_ref(ref_id)
        xvec = bool(spec.get("x_vector_only")) or not spec.get("transcript")
        key = hashlib.sha256(f"{ref_id}|{spec.get('transcript') or ''}|{int(xvec)}|"
                             f"{VARIANTS['base'].revision}".encode()).hexdigest()
        return {"ref_id": ref_id, "path": f"references/{ref_id}.wav", "text": spec.get("transcript") or "",
                "x_vector_only": xvec, "cache_key": key}

    @staticmethod
    def _check_unit(path: Path, unit: dict, max_new: int) -> None:
        if not path.is_file():
            raise EngineError("the model finished without producing audio")
        info = au.analyze_wav(path)
        if info.silent:
            raise EngineError("the model produced silence for a line; try another seed", retryable=True)
        if info.duration_s < 0.2:
            raise EngineError("the model produced an unusably short line; try another seed", retryable=True)
        if info.duration_s >= max_new / 12.5 * 0.98:
            raise EngineError("the model did not finish a line (it ran to the length limit); try another seed",
                              code="runaway", retryable=True)

    def _assemble(self, job_id: str, req: dict, segs: list[dict], take: int, rendered: dict) -> dict:
        parts: list[tuple[bytes, int]] = []
        for seg in segs:
            for pi, para in enumerate(seg["paragraphs"]):
                for ci in range(len(para)):
                    pcm = rendered[(take, seg["index"], pi, ci)][0]
                    if not parts:
                        gap = 0
                    elif ci > 0:
                        gap = BREATH_MS
                    else:
                        gap = seg["pause_ms"]
                    parts.append((pcm, gap))
        out_dir = self.cfg.jobs_dir / job_id
        wav = out_dir / f"take-{take}.wav"
        au.write_pcm16(wav, 24000, au.concat(parts, 24000))
        if abs(req["speed"] - 1.0) > 1e-6:
            raw = out_dir / f"take-{take}.raw.wav"
            wav.replace(raw)
            try:
                self._encode(job_id, raw.name, wav.name,
                             ["-filter:a", f"atempo={req['speed']:.3f}", "-ar", "24000", "-ac", "1",
                              "-c:a", "pcm_s16le"])
            finally:
                raw.unlink(missing_ok=True)
        info = au.analyze_wav(wav)
        if info.silent:
            raise EngineError("the finished take is silent", retryable=True)
        files = {"wav": {"bytes": wav.stat().st_size, "sha256": au.sha256_file(wav)}}
        self._encode(job_id, wav.name, f"take-{take}.mp3", FORMAT_ENCODE["mp3"][1])
        mp3 = out_dir / f"take-{take}.mp3"
        files["mp3"] = {"bytes": mp3.stat().st_size, "sha256": au.sha256_file(mp3)}
        return {"index": take, "seed": req["seed"] + take, "duration_s": info.duration_s,
                "sample_rate": info.sample_rate, "channels": 1, "peak": info.peak, "rms_dbfs": info.rms_dbfs,
                "waveform": info.waveform, "files": files}

    def _sample_memory(self) -> None:
        m = meminfo()
        if "MemAvailable" in m and m["MemAvailable"] < self._mem_low.get("MemAvailable", 1e9):
            self._mem_low["MemAvailable"] = m["MemAvailable"]

    # ------------------------------------------------------------- speech --
    def speech(self, body: Any) -> tuple[Path, str, str, dict]:
        """OpenAI speech: render one take and return (file, content type, job id, timings)."""
        sreq = v.speech_request(body)
        spec = self.voice_by_name(sreq["voice"])
        job_body = {"operation": "tts", "language": sreq["language"], "takes": 1, "speed": sreq["speed"],
                    "segments": [{"text": sreq["text"], "voice": spec, "instructions": sreq["instructions"]}]}
        if sreq["seed"] is not None:
            job_body["seed"] = sreq["seed"]
        job = self.submit(job_body, client_ref=SPEECH_CLIENT_REF)
        final = self.wait(job["id"], self.cfg.speech_timeout_s)
        if final["status"] == st.COMPLETED:
            path, ctype = self.content_path(job["id"], 0, sreq["format"])
            return path, ctype, job["id"], final.get("timings") or {}
        if final["status"] in (st.QUEUED, st.WAITING, st.LOADING, st.GENERATING, st.PROCESSING):
            try:
                self.cancel(job["id"])
            except ConflictError:
                pass
            raise UnavailableError(f"gx-voice could not start in time: {final.get('detail') or 'busy'}",
                                   code="timeout")
        err = final.get("error") or {}
        exc = EngineError(err.get("message") or "the speech could not be generated",
                          code=err.get("code") or "generation_failed", retryable=bool(err.get("retryable")))
        if err.get("code") in ("gx_max_active", "maintenance", "insufficient_memory", "node_busy"):
            raise UnavailableError(exc.message, code=exc.code)
        raise exc

    def discard_audio(self, job_id: str) -> None:
        """Data minimisation for the OpenAI endpoint: the caller has the bytes."""
        shutil.rmtree(self.cfg.jobs_dir / job_id, ignore_errors=True)
        job = self.store.get_job(job_id)
        if job:
            result = dict(job.get("result") or {})
            result["audio_removed"] = True
            self.store.update_job(job_id, result=result)

    def wait(self, job_id: str, timeout: float) -> dict:
        deadline = time.time() + timeout
        while True:
            job = self.job_view(job_id)
            if job["status"] in st.TERMINAL or time.time() >= deadline:
                return job
            with self._done:
                self._done.wait(min(1.0, max(0.05, deadline - time.time())))

    # ------------------------------------------------------------ reaper --
    def _reaper(self) -> None:
        while not self._stop.wait(15):
            try:
                self.reap_once()
                if time.time() - self._last_retention > 3600:
                    self._last_retention = time.time()
                    self.retention()
            except Exception:  # noqa: BLE001
                log.exception("reaper iteration failed")

    def reap_once(self) -> str | None:
        """One lifecycle pass. Returns what it did (for tests and the log)."""
        block = self.engine.gxmax_block_reason()
        if self.engine.state != READY and not block:
            return None
        self._sample_memory()
        if block:
            if self.engine.state != READY and not self.engine.docker.exists(self.cfg.engine_container):
                return "held"
            log.warning("gx-max claims node 2; unloading gx-voice now")
            self.store.event("engine_unloaded", **self.engine.unload("gx-max drain"))
            return "gx-max"
        if self.engine.maintenance_reason() and self._current is None:
            self.store.event("engine_unloaded", **self.engine.unload("maintenance mode"))
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
            self.store.event("engine_unloaded", **self.engine.unload(f"idle for {int(idle)} s"))
            return "idle"
        return None

    def retention(self, days: float = 14.0) -> int:
        """Terminal jobs older than `days` lose their audio (gx10-01 keeps what it saved)."""
        cutoff = time.time() - days * 86400
        n = 0
        for job in self.store.list_jobs(limit=1000):
            if job["status"] in st.TERMINAL and (job.get("finished_at") or job["created_at"]) < cutoff:
                shutil.rmtree(self.cfg.jobs_dir / job["id"], ignore_errors=True)
                self.store.delete_job(job["id"])
                n += 1
        if n:
            self.store.event("retention", deleted=n)
        return n

    def load(self, variant: str | None = None) -> dict:
        block = self.engine.policy_block_reason()
        if block:
            raise UnavailableError(block[1], code=block[0])
        try:
            self.engine.ensure_loaded()
        except ResourceWait as wait:
            raise UnavailableError(wait.reason, code=wait.code) from wait
        if variant:
            if variant not in VARIANTS:
                raise ValidationError("variant must be custom, design or base")
            if self._current:
                raise ConflictError("a job is running; the variant switches when it needs to")
            self.engine.load_variant(variant)
        self.store.event("engine_loaded", seconds=self.engine.last_load_seconds, variant=variant)
        return self.engine.snapshot()

    def unload(self, *, if_idle: bool = False) -> dict:
        if self._current:
            raise ConflictError("a voice job is running; cancel it or wait before unloading")
        if if_idle:
            if self.store.count_active():
                raise ConflictError("voice jobs are queued; the engine stays loaded for them")
            if self.engine.pin_honoured():
                raise ConflictError("gx-voice is pinned")
        if self.engine.state != READY and not self.engine.docker.exists(self.cfg.engine_container):
            return {"reason": f"engine is {self.engine.state}", "container_gone": True, "noop": True}
        info = self.engine.unload("unloaded while idle to make room on gx10-02 (30 GiB reserve)"
                                  if if_idle else "requested")
        self.store.event("engine_unloaded", **info)
        return info


# --------------------------------------------------------------- helpers --
def _variant_order(units: list[dict], loaded: list[str]) -> list[dict]:
    """Stable grouping by variant; the resident variant goes first."""
    seen: list[str] = []
    for u in units:
        if u["variant"] not in seen:
            seen.append(u["variant"])
    current = loaded[-1] if loaded else None
    if current in seen:
        seen.remove(current)
        seen.insert(0, current)
    rank = {name: i for i, name in enumerate(seen)}
    return sorted(units, key=lambda u: rank[u["variant"]])


def _auto_title(req: dict) -> str:
    text = " ".join(req["segments"][0]["text"].split())
    base = text[:60].rstrip(" ,.") + ("…" if len(text) > 60 else "")
    prefix = {"voice_design": "Voice design", "voice_clone": "Voice clone", "dialogue": "Dialogue"}.get(
        req["operation"])
    return f"{prefix} — {base}" if prefix else base


def _ref_view(row: dict) -> dict:
    return {"id": row["id"], "object": "voice.reference", "filename": row["filename"],
            "container": row["container"], "size_bytes": row["size_bytes"], "sha256": row["sha256"],
            "duration_s": row["duration_s"], "created_at": row["created_at"]}


def public_job(job: dict) -> dict:
    req = dict(job["request"])
    result = job.get("result") or {}
    now = time.time()
    takes = []
    for t in result.get("takes", []):
        t = dict(t)
        if not result.get("audio_removed"):
            t["files"] = {fmt: {**meta, "url": f"/v1/voice/jobs/{job['id']}/content?take={t['index']}&format={fmt}"}
                          for fmt, meta in (t.get("files") or {}).items()}
        else:
            t["files"] = {}
        takes.append(t)
    return {
        "id": job["id"], "object": "voice.job", "operation": job["operation"], "status": job["status"],
        "detail": job["detail"], "progress": job["progress"], "title": job["title"],
        "created_at": job["created_at"], "started_at": job.get("started_at"), "finished_at": job.get("finished_at"),
        "elapsed_s": round((job.get("finished_at") or now) - job["created_at"], 1),
        "request": req, "takes": takes, "timings": job.get("timings") or {}, "notes": job.get("notes") or [],
        "client_ref": job.get("client_ref"), "cancel_requested": job["cancel_requested"],
        "audio_removed": bool(result.get("audio_removed")),
        "model": {"family": "Qwen3-TTS 12Hz 1.7B",
                  "variants": {name: {"repo": VARIANTS[name].repo, "revision": VARIANTS[name].revision}
                               for name in req.get("resolved_variants", []) if name in VARIANTS}},
        "error": ({"code": job["error_code"], "message": job["error_message"], "retryable": job["retryable"]}
                  if job.get("error_code") else None),
        "waiting": None,
        "links": {"self": f"/v1/voice/jobs/{job['id']}"},
    }
