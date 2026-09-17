"""A hermetic stand-in for the node-2 gx-music supervisor (tests and E2E).

Implements the subset of the gx-music API that gx10-01 uses, with the same
job vocabulary and payload shapes (legenex/music/gx_music/service.py
`public_job`). Jobs move queued -> generating -> completed over a few polls
and produce a short, real, playable 48 kHz stereo WAV (a sine chord), so the
browser can play it. Only WAV is produced (no encoder in a stdlib stub).
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import re
import struct
import sys
import threading
import time
import uuid
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# The supervisor's request validation is stdlib-only: the stub uses the real
# thing, so previews and vocal-rule refusals match gx10-02 exactly.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "music"))
from gx_music import validation as music_validation  # noqa: E402
from gx_music.errors import MusicError as SupervisorError  # noqa: E402

KEY_MIN = 32
TURBO = music_validation.Capabilities.for_model("acestep-v15-xl-turbo")
MEASURED = {
    "method": "gx-music DSP v1: numpy/scipy, STFT 2048/512 at 22.05 kHz, no ML model", "duration_s": 2.0,
    "tempo": {"bpm": 122.0, "confidence": 0.62, "stability": 0.9,
              "candidates": [{"bpm": 122.0, "score": 4.1}, {"bpm": 61.0, "score": 3.2}]},
    "beats": {"count": 4, "first_s": 0.1, "median_interval_s": 0.49},
    "time_signature": {"value": "4/4", "confidence": 0.4, "method": "beat accent periodicity (2/3/4 beats)"},
    "key": {"value": "A minor", "confidence": 0.35, "correlation": 0.8,
            "alternatives": [{"key": "C major", "correlation": 0.7}],
            "method": "Aarden-Essen profile correlation on log harmonic chroma"},
    "loudness": {"rms_dbfs": -14.2, "peak_dbfs": -1.0, "crest_db": 13.2, "dynamic_range_db": 8.1},
    "energy": {"level": "high", "trend": "builds", "curve": [0.2, 0.5, 0.9, 1.0]},
    "spectrum": {"centroid_hz": 1900, "rolloff_hz": 4200, "bass_ratio": 0.2, "air_ratio": 0.02,
                 "flatness": 0.1, "brightness": "balanced", "bass_weight": "moderate"},
    "texture": {"percussive_ratio": 0.52, "character": "percussive"},
    "stereo": {"width": 0.3, "label": "moderate"},
    "structure": {"segments": [{"start": 0.0, "end": 1.0, "label": "A", "energy_db": -6.0, "energy": "medium"},
                               {"start": 1.0, "end": 2.0, "label": "B", "energy_db": 0.0, "energy": "high"}],
                  "count": 2, "method": "Foote novelty"},
    "descriptors": ["fast tempo", "driving beat"],
}
UNDERSTOOD = {"method": "ACE-Step 1.5 audio understanding (audio -> 5Hz codes -> 5Hz LM); model inference, "
                        "not measurement",
              "caption": "An energetic Afro house groove with a soulful female vocal.", "genres": "afro house",
              "lyrics": "[Verse]\nhold the light", "vocals_detected": True, "language": "en", "bpm": 122,
              "key": "A minor", "key_raw": "A minor", "time_signature": "4", "duration_s": 2.0}


def sine_wav(seconds: float = 2.0, freq: float = 440.0, rate: int = 48000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        for i in range(int(seconds * rate)):
            t = i / rate
            v = 0.3 * math.sin(2 * math.pi * freq * t) + 0.2 * math.sin(2 * math.pi * freq * 1.25 * t)
            s = int(max(-1.0, min(1.0, v)) * 32000)
            frames += struct.pack("<hh", s, s)
        w.writeframes(bytes(frames))
    return buf.getvalue()


# The real capability table of the installed checkpoint (validation.Capabilities).
CAPABILITIES = {**TURBO.as_dict(600), "max_duration_s": 600}


class MusicStub:
    def __init__(self, key: str, polls_to_finish: int = 3) -> None:
        self.key = key
        self.polls = polls_to_finish
        self.jobs: dict[str, dict] = {}
        self.uploads: dict[str, dict] = {}
        self.wav = sine_wav()
        self.engine = "unloaded"
        self.calls: list[tuple[str, str]] = []
        self._lock = threading.Lock()
        stub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # noqa: A003
                pass

            def _send(self, status, payload, ctype="application/json", extra=None):
                body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def _handle(self):
                path, _, query = self.path.partition("?")
                stub.calls.append((self.command, path))
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                if path == "/health":
                    return self._send(200, {"status": "ok", "service": "gx-music", "engine": stub.engine,
                                            "active_jobs": stub.active()})
                if self.headers.get("Authorization") != f"Bearer {stub.key}":
                    return self._send(401, {"error": {"code": "unauthorized", "message": "missing or invalid API key",
                                                      "retryable": False}})
                status, payload, *rest = stub.route(self.command, path, query, raw, self.headers)
                return self._send(status, payload, *rest)

            do_GET = do_POST = do_DELETE = _handle  # noqa: N815

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()

    def active(self) -> int:
        return sum(1 for j in self.jobs.values() if j["status"] not in ("completed", "failed", "cancelled"))

    def model(self) -> dict:
        return {"alias": "gx-music", "task": "music-generation", "node": "gx10-02",
                "identity": {"dit_name": "acestep-v15-xl-turbo", "dit_repo": "ACE-Step/acestep-v15-xl-turbo",
                             "dit_revision": "d4a0b288b83ebb7e25a8c0b32c573c22e134e8ee",
                             "lm_name": "acestep-5Hz-lm-4B", "lm_repo": "ACE-Step/acestep-5Hz-lm-4B",
                             "lm_revision": "0a3ec94b557aea7d508da38b31cfe7341f6ff737",
                             "shared_repo": "ACE-Step/Ace-Step1.5",
                             "shared_revision": "19671f406d603126926c1b7e2adc169acbcade22",
                             "runtime_repo": "https://github.com/ace-step/ACE-Step-1.5",
                             "runtime_ref": "ca1e85fe9430179831e6bc6be790c332190a3866",
                             "image": "gx-music-engine:acestep15-ca1e85f-t214"},
                "runtime": {"name": "ACE-Step 1.5 REST server", "lm_backend": "vllm",
                            "image": "gx-music-engine:acestep15-ca1e85f-t214"},
                "capabilities": CAPABILITIES,
                "disk": {"checkpoints_bytes": {"acestep-v15-xl-turbo": 19950000000}, "total_bytes": 29900000000},
                "engine": {"state": self.engine, "detail": "", "container": "gx-music", "idle_seconds": 3.0,
                           "idle_unload_after_s": 600, "last_load_seconds": 85.5, "pinned": False,
                           "blocked_by": None},
                "memory": {"MemAvailable": 112.0}, "jobs": {"completed": 1},
                "queue": {"active": self.active(), "current_job": None, "max": 32},
                "policy": {"workload_class": "medium", "estimated_gib": 32.0, "reserve_gib": 30.0,
                           "evict_idle_comfy_weights": False, "pinned": False, "blocked_by": None}}

    def view(self, job: dict) -> dict:
        tracks = []
        if job["status"] == "completed":
            for i in range(job["batch"]):
                tracks.append({
                    "index": i, "seed": (job["request"].get("seed") or 7) + i, "duration_s": 2.0,
                    "sample_rate": 48000, "channels": 2, "bit_depth": 16, "encoding": "pcm_s16le",
                    "peak": 0.49, "rms_dbfs": -14.0,
                    "waveform": [[-0.4, 0.4]] * 64,
                    "files": {"wav": {"bytes": len(self.wav), "sha256": hashlib.sha256(self.wav).hexdigest(),
                                      "url": f"/v1/music/{job['id']}/content?index={i}&format=wav"}},
                    "caption": job["request"].get("prompt") or "e2e caption",
                    "lyrics": job["request"].get("lyrics") or "[Instrumental]",
                    "bpm": job["request"].get("bpm") or 96, "key": job["request"].get("key") or "C major",
                    "time_signature": job["request"].get("time_signature") or "4",
                    "genres": None, "engine_seed": "7", "dit_model": "acestep-v15-xl-turbo",
                    "lm_model": "acestep-5Hz-lm-4B"})
        req = dict(job["request"])
        return {"id": job["id"], "object": "music.job", "operation": job["operation"], "status": job["status"],
                "detail": job["detail"], "progress": job["progress"], "created_at": job["created_at"],
                "started_at": job["created_at"], "finished_at": job.get("finished_at"),
                "elapsed_s": round(time.time() - job["created_at"], 1),
                "title": req.get("title") or (req.get("prompt") or "Untitled")[:60], "request": {
                    "operation": job["operation"], "prompt": req.get("prompt", ""),
                    "style_tags": req.get("style_tags", []), "lyrics": req.get("lyrics", ""),
                    "description": req.get("description", ""), "vocal_intent": req.get("vocal_intent", "auto"),
                    "lyrics_source": req.get("lyrics_source", "user"), "instrumental": req.get("instrumental"),
                    "vocal_mode": (job.get("conditioning") or {}).get("vocal_mode"),
                    "conditioning": job.get("conditioning"),
                    "parameters": {k: req.get(k) for k in ("duration", "bpm", "key", "time_signature", "seed",
                                                           "inference_steps", "strength") if k in req}},
                "tracks": tracks, "timings": {"generate_s": 1.2, "audio_seconds": 2.0 * len(tracks)},
                "analysis": job.get("analysis"),
                "model": self.model()["identity"], "parent_job_id": (req.get("source") or {}).get("job_id"),
                "parent_index": (req.get("source") or {}).get("index"), "source": req.get("source"),
                "cancel_requested": 0, "error": job.get("error"),
                "links": {"self": f"/v1/music/{job['id']}"}}

    def advance(self, job: dict) -> None:
        if job["status"] in ("completed", "failed", "cancelled"):
            return
        job["polls"] += 1
        if job["operation"] == "analyze":
            understand = bool(job["request"].get("understand"))
            if job["polls"] == 1:
                job.update(status="preparing", detail="measuring tempo, key, energy and structure")
            elif job["polls"] == 2 and understand:
                job.update(status="generating", detail="listening (ACE-Step audio understanding)",
                           analysis={"measured": MEASURED, "understanding": None})
            else:
                job.update(status="completed", detail="", progress=1.0, finished_at=time.time(),
                           analysis={"measured": MEASURED, "understanding": UNDERSTOOD if understand else None})
            return
        if job["polls"] == 1:
            job.update(status="loading_model", detail="loading ACE-Step 1.5 XL")
            self.engine = "loading"
        elif job["polls"] < self.polls:
            job.update(status="generating", detail="generating music", progress=0.5)
            self.engine = "ready"
        else:
            job.update(status="completed", detail="", progress=1.0, finished_at=time.time())

    def route(self, method: str, path: str, query: str, raw: bytes, headers) -> tuple:
        ops = {"/v1/music/generations": "generate", "/v1/music/remix": "remix", "/v1/music/edits": "edit",
               "/v1/music/extend": "extend"}
        with self._lock:
            if method == "GET" and path == "/v1/music/model":
                return 200, self.model()
            if method == "GET" and path == "/v1/music/tags":
                q = dict(p.split("=", 1) for p in query.split("&") if "=" in p).get("q", "")
                words = ["synthwave", "synth-pop", "soul", "jazz", "lo-fi", "piano"]
                return 200, {"groups": {"genre": ["pop", "rock", "jazz"], "mood": ["happy", "dark"]},
                             "suggestions": [w for w in words if w.startswith(q.lower())] if q else []}
            if method == "POST" and path in ("/v1/music/load", "/v1/music/unload"):
                self.engine = "ready" if path.endswith("load") and "un" not in path else "unloaded"
                return 200, {"state": self.engine}
            if method == "POST" and path == "/v1/music/preview":
                try:
                    req = music_validation.generation(json.loads(raw or b"{}"), TURBO, max_duration=600)
                except SupervisorError as exc:
                    return 400, exc.payload()
                return 200, {"object": "music.preview", "conditioning": req.conditioning(),
                             "vocal_mode": req.vocal_mode, "vocal_intent": req.vocal_intent,
                             "lyrics_source": req.lyrics_source, "caption_length": len(req.engine["prompt"]),
                             "caption_max": 512}
            if method == "POST" and path == "/v1/music/analyses":
                body = json.loads(raw or b"{}")
                src = body.get("source") or {}
                if not (src.get("upload_id") in self.uploads or src.get("job_id") in self.jobs):
                    return 404, {"error": {"code": "not_found", "message": "the referenced upload does not exist",
                                           "retryable": False}}
                jid = "mus-" + uuid.uuid4().hex
                self.jobs[jid] = {"id": jid, "operation": "analyze", "status": "preparing",
                                  "detail": "measuring", "progress": None, "created_at": time.time(),
                                  "request": {"source": src, "understand": bool(body.get("understand"))},
                                  "polls": 0, "batch": 0}
                return 202, self.view(self.jobs[jid])
            if method == "POST" and path in ops:
                body = json.loads(raw or b"{}")
                conditioning = None
                if ops[path] == "generate":
                    try:
                        conditioning = music_validation.generation(body, TURBO, max_duration=600).conditioning()
                    except SupervisorError as exc:
                        return 400, exc.payload()
                if ops[path] != "generate" and not body.get("source"):
                    return 400, {"error": {"code": "invalid_request", "message": "source is required",
                                           "retryable": False}}
                src = body.get("source") or {}
                if src.get("job_id") and src["job_id"] not in self.jobs:
                    return 404, {"error": {"code": "not_found", "message": "the referenced track does not exist",
                                           "retryable": False}}
                jid = "mus-" + uuid.uuid4().hex
                self.jobs[jid] = {"id": jid, "operation": ops[path], "status": "queued", "detail": "queued",
                                  "progress": None, "created_at": time.time(), "request": body, "polls": 0,
                                  "batch": int(body.get("batch_size") or 1), "conditioning": conditioning}
                return 202, self.view(self.jobs[jid])
            if method == "POST" and path == "/v1/music/uploads":
                uid = "upl-" + uuid.uuid4().hex
                self.uploads[uid] = {"id": uid, "object": "music.upload", "filename": "upload.wav",
                                     "container": "wav", "size_bytes": len(raw),
                                     "sha256": hashlib.sha256(raw).hexdigest(), "duration_s": 2.0,
                                     "sample_rate": 48000, "channels": 2, "created_at": time.time()}
                return 201, self.uploads[uid]
            m = re.fullmatch(r"/v1/music/uploads/(upl-[0-9a-f]{32})", path)
            if m:
                up = self.uploads.get(m.group(1))
                return (200, up) if up else (404, {"error": {"code": "not_found", "message": "no such upload"}})
            if method == "GET" and path == "/v1/music/jobs":
                for j in self.jobs.values():
                    self.advance(j)
                op = dict(p.split("=", 1) for p in query.split("&") if "=" in p).get("operation")
                jobs = [j for j in self.jobs.values()
                        if op is None or (op == "creative" and j["operation"] != "analyze") or j["operation"] == op]
                return 200, {"data": [self.view(j) for j in sorted(jobs, key=lambda j: -j["created_at"])]}
            m = re.fullmatch(r"/v1/music/(mus-[0-9a-f]{32})(/lineage|/content|/cancel)?", path)
            if not m or m.group(1) not in self.jobs:
                return 404, {"error": {"code": "not_found", "message": "no such music job", "retryable": False}}
            job = self.jobs[m.group(1)]
            sub = m.group(2)
            if sub is None and method == "GET":
                self.advance(job)
                return 200, self.view(job)
            if sub == "/cancel":
                job.update(status="cancelled", detail="cancelled before it started")
                return 200, self.view(job)
            if sub == "/lineage":
                return 200, {"job": {"id": job["id"]}, "ancestors": [], "descendants": []}
            if sub == "/content":
                return 200, self.wav, "audio/wav"
        return 404, {"error": {"code": "not_found", "message": "no route"}}
