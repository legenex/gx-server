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
import threading
import time
import uuid
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KEY_MIN = 32


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


CAPABILITIES = {
    "task_types": ["text2music", "cover", "repaint"],
    "operations": {"generate": "text2music", "remix": "cover", "edit": "repaint", "extend": "repaint",
                   "extract": None, "lego": None, "complete": None},
    "controls": {
        "prompt": {"type": "string", "max_length": 512},
        "style_tags": {"type": "array", "max_items": 24, "item_max_length": 48},
        "lyrics": {"type": "string", "max_length": 4096,
                   "sections": ["Intro", "Verse", "Pre-Chorus", "Chorus", "Post-Chorus", "Bridge", "Hook",
                                "Breakdown", "Drop", "Build", "Interlude", "Instrumental", "Solo",
                                "Guitar Solo", "Outro", "Fade Out"]},
        "instrumental": {"type": "boolean"},
        "description": {"type": "string", "max_length": 512},
        "vocal_language": {"type": "enum", "values": ["en", "de", "es", "fr", "ja", "ko", "zh", "unknown"]},
        "duration": {"type": "number", "min": 10, "max": 600, "unit": "s"},
        "bpm": {"type": "integer", "min": 30, "max": 300, "nullable": True},
        "key": {"type": "string", "example": "F# minor", "nullable": True},
        "time_signature": {"type": "enum", "values": ["2", "3", "4", "6"], "nullable": True,
                           "labels": {"2": "2/4", "3": "3/4", "4": "4/4", "6": "6/8"}},
        "seed": {"type": "integer", "min": 0, "max": 2147483647, "nullable": True},
        "batch_size": {"type": "integer", "min": 1, "max": 4},
        "inference_steps": {"type": "integer", "min": 1, "max": 20, "default": 8},
        "infer_method": {"type": "enum", "values": ["ode", "sde"]},
        "thinking": {"type": "boolean", "default": True},
        "enhance_prompt": {"type": "boolean", "default": False},
        "lm_temperature": {"type": "number", "min": 0.0, "max": 2.0},
        "lm_cfg_scale": {"type": "number", "min": 1.0, "max": 5.0},
        "lm_top_p": {"type": "number", "min": 0.0, "max": 1.0},
        "output_format": {"type": "enum", "values": ["wav", "flac", "mp3"]},
        "reference": {"type": "source"},
    },
    "remix_controls": {"strength": {"type": "number", "min": 0.0, "max": 1.0, "default": 0.5},
                       "noise_strength": {"type": "number", "min": 0.0, "max": 1.0, "default": 0.0}},
    "edit_controls": {"start": {"type": "number", "min": 0}, "end": {"type": "number", "min": 0},
                      "mode": {"type": "enum", "values": ["conservative", "balanced", "aggressive"]},
                      "strength": {"type": "number", "min": 0.0, "max": 1.0}},
    "extend_controls": {"seconds": {"type": "number", "min": 5, "max": 240},
                        "direction": {"type": "enum", "values": ["end", "start"]}},
    "max_duration_s": 600,
}


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
                    "parameters": {k: req.get(k) for k in ("duration", "bpm", "key", "time_signature", "seed",
                                                           "inference_steps", "strength") if k in req}},
                "tracks": tracks, "timings": {"generate_s": 1.2, "audio_seconds": 2.0 * len(tracks)},
                "model": self.model()["identity"], "parent_job_id": (req.get("source") or {}).get("job_id"),
                "parent_index": (req.get("source") or {}).get("index"), "source": req.get("source"),
                "cancel_requested": 0, "error": job.get("error"),
                "links": {"self": f"/v1/music/{job['id']}"}}

    def advance(self, job: dict) -> None:
        if job["status"] in ("completed", "failed", "cancelled"):
            return
        job["polls"] += 1
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
            if method == "POST" and path in ops:
                body = json.loads(raw or b"{}")
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
                                  "batch": int(body.get("batch_size") or 1)}
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
                return 200, {"data": [self.view(j) for j in sorted(self.jobs.values(),
                                                                   key=lambda j: -j["created_at"])]}
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
