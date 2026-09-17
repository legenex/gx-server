"""Hermetic stand-ins for the services Creative Flows calls (tests and the
offline E2E fixture only). They create REAL Library assets so provenance,
caching and ownership are exercised end to end; only the GPU/network work is
replaced."""

from __future__ import annotations

import base64
import json
import secrets
import shutil
import struct
import threading
import time
import zlib
from pathlib import Path
from typing import Any

import support  # noqa: F401  (puts gx_control_ui on sys.path)

from gx_control_ui.flows.ffmpeg import FFJob, FFResult, Media
from gx_control_ui.media_jobs import JobError
from gx_control_ui.media_library import MediaLibrary, NewAsset


def png_bytes(w: int = 64, h: int = 64, seed: int = 0) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + b"".join(bytes(((x + seed) * 4 % 256, y * 4 % 256, 128)) for x in range(w))
                   for y in range(h))
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64
WAV = b"RIFF" + b"\x00" * 60 + b"WAVEfmt "


class StubMedia:
    """MediaJobs look-alike: jobs finish after ``delay`` seconds with real assets."""

    def __init__(self, library: MediaLibrary, delay: float = 0.05) -> None:
        self.library = library
        self.delay = delay
        self.jobs: dict[str, dict[str, Any]] = {}
        self.submitted: list[dict[str, Any]] = []
        self.fail_next: str | None = None
        self.hold = threading.Event()
        self.hold.set()
        self._lock = threading.Lock()

    def submit(self, body: dict, *, user: str, ip: str = "", wan: dict | None = None) -> dict:
        kind = body.get("kind")
        if kind not in ("t2i", "edit", "variation", "t2v", "i2v", "v2v"):
            raise JobError(f"unknown job kind {kind!r}")
        if kind in ("t2i", "t2v") and not body.get("prompt"):
            raise JobError("prompt is required")
        jid = secrets.token_hex(8)
        job = {"id": jid, "kind": kind, "phase": "queued", "detail": "", "assets": [], "error": None,
               "label": kind, "created": time.time(), "body": dict(body), "user": user,
               "fail": self.fail_next}
        self.fail_next = None
        with self._lock:
            self.jobs[jid] = job
            self.submitted.append(dict(body))
        threading.Thread(target=self._run, args=(jid,), daemon=True).start()
        return dict(job)

    def _run(self, jid: str) -> None:
        job = self.jobs[jid]
        time.sleep(self.delay)
        job["phase"] = "waiting"
        job["waiting"] = {"code": "insufficient_memory", "reason": "Waiting for enough gx10-02 memory"}
        time.sleep(self.delay)
        self.hold.wait(30)
        if job["phase"] == "cancelled":
            return
        job["phase"] = "generating"
        job["waiting"] = None
        time.sleep(self.delay)
        if job["fail"]:
            job["phase"], job["error"], job["error_code"] = "failed", job["fail"], "stub_failure"
            return
        body = job["body"]
        count = int(body.get("n", 1)) if job["kind"] == "t2i" else 1
        for i in range(count):
            if job["kind"] in ("t2i", "edit", "variation"):
                asset = self.library.add(NewAsset(
                    type="image", ext="png", operation={"t2i": "generate"}.get(job["kind"], job["kind"]),
                    data=png_bytes(seed=len(self.jobs) + i), prompt=body.get("prompt"), model_alias="gx-image",
                    workflow="stub-image", parent_id=body.get("source_id"), job_id=jid, title=body.get("title")))
            else:
                asset = self.library.add(NewAsset(
                    type="video", ext="mp4", operation="generate" if job["kind"] == "t2v" else job["kind"],
                    data=MP4, prompt=body.get("prompt"), model_alias="gx-video", workflow="stub-video",
                    parent_id=body.get("source_id"), job_id=jid, duration=float(body.get("seconds", 3)),
                    width=832, height=480, fps=16, title=body.get("title")))
            job["assets"].append(asset["id"])
        job["phase"] = "ready"

    def get(self, job_id: str) -> dict:
        job = self.jobs.get(job_id)
        if job is None:
            raise JobError("no such media job", 404)
        return {k: v for k, v in job.items() if k != "body"}

    def cancel(self, job_id: str, *, user: str) -> dict:
        job = self.jobs[job_id]
        if job["phase"] in ("queued", "waiting"):
            job["phase"] = "cancelled"
            return dict(job)
        raise JobError("the job is already running on gx10-02 and finishes on its own", 409)


class StubMusic:
    def __init__(self, library: MediaLibrary, delay: float = 0.05) -> None:
        self.library = library
        self.delay = delay
        self.jobs: dict[str, dict[str, Any]] = {}
        self.submitted: list[dict[str, Any]] = []

    def submit(self, operation: str, body: Any, *, user: str, via: str = "ui", ip: str = "") -> dict:
        jid = "mus-" + secrets.token_hex(16)
        self.submitted.append(dict(body))
        self.jobs[jid] = {"id": jid, "status": "queued", "phase": "queued", "imported": False,
                          "library_assets": [], "created": time.time(), "body": dict(body)}
        return dict(self.jobs[jid])

    def get(self, job_id: str) -> dict:
        job = self.jobs[job_id]
        age = time.time() - job["created"]
        if job["status"] == "queued" and age > self.delay:
            job["status"] = job["phase"] = "generating"
        if job["status"] == "generating" and age > 2 * self.delay:
            asset = self.library.add(NewAsset(
                type="audio", ext="wav", operation="generate", data=WAV, model_alias="gx-music",
                prompt=job["body"].get("description"), duration=float(job["body"].get("duration", 30)),
                job_id=job_id, title=job["body"].get("title")))
            job.update(status="completed", phase="completed", imported=True, library_assets=[asset["id"]],
                       model={"dit_name": "stub-ace-step"})
        return {k: v for k, v in job.items() if k != "body"}

    def cancel(self, job_id: str, *, user: str) -> dict:
        self.jobs[job_id]["status"] = "cancelled"
        return self.jobs[job_id]


class StubVoice:
    def __init__(self, library: MediaLibrary, delay: float = 0.05) -> None:
        self.library = library
        self.delay = delay
        self.jobs: dict[str, dict[str, Any]] = {}
        self.voices: dict[str, dict[str, Any]] = {
            "preset:serena": {"id": "preset:serena", "name": "Serena", "kind": "preset", "version": 1,
                              "description": "Warm, gentle young female voice."},
            "preset:aiden": {"id": "preset:aiden", "name": "Aiden", "kind": "preset", "version": 1,
                             "description": "Sunny American male voice."},
        }
        self.submitted: list[dict[str, Any]] = []

    def list_voices(self, *, include_presets: bool = True) -> list[dict]:
        return list(self.voices.values())

    def get_voice(self, voice_id: str) -> dict:
        if voice_id not in self.voices:
            raise ValueError("no such voice")
        return self.voices[voice_id]

    def create_voice(self, body: dict, *, user: str, ip: str = "") -> dict:
        vid = "vc_" + secrets.token_hex(12)
        self.voices[vid] = {"id": vid, "name": body.get("name"), "kind": body.get("kind"), "version": 1}
        return self.voices[vid]

    def submit(self, body: dict, *, user: str, via: str = "ui", ip: str = "") -> dict:
        if body.get("operation") == "tts" and body.get("voice_id") not in self.voices:
            raise ValueError("unknown voice")
        jid = "vox-" + secrets.token_hex(16)
        self.submitted.append(dict(body))
        self.jobs[jid] = {"id": jid, "status": "queued", "created": time.time(), "body": dict(body), "takes": []}
        return {k: v for k, v in self.jobs[jid].items() if k != "body"}

    def get(self, job_id: str) -> dict:
        job = self.jobs[job_id]
        if job["status"] == "queued" and time.time() - job["created"] > self.delay:
            flow = job["body"].get("flow") or {}
            asset = self.library.add(NewAsset(
                type="audio", ext="wav", operation="tts", data=WAV, model_alias="gx-voice", duration=4.2,
                prompt=job["body"].get("text"), source_kind="voice_take", source_ref=f"{job_id}#0", **flow))
            job.update(status="completed", takes=[{"index": 0, "asset_id": asset["id"], "duration_s": 4.2}])
        return {k: v for k, v in job.items() if k != "body"}

    def cancel(self, job_id: str, *, user: str) -> dict:
        self.jobs[job_id]["status"] = "cancelled"
        return self.jobs[job_id]

    def save_take(self, job_id: str, take: int, *, user: str, title: str | None = None,
                  flow: dict | None = None) -> dict:
        return self.library.get(self.jobs[job_id]["takes"][take]["asset_id"])


class StubLLM:
    """Answers by request shape; ``script`` overrides per call."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.graph_answers: list[str] = []
        self.fail: str | None = None
        self.delay = 0.0

    def chat(self, model: str, messages: list[dict], *, temperature: float = 0.7, max_tokens: int = 1024,
             schema: dict | None = None, timeout: float = 900) -> tuple[str, dict]:
        self.calls.append({"model": model, "messages": messages, "schema": schema})
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            from gx_control_ui.flows.services import NodeFailure
            raise NodeFailure(self.fail, code="gateway_error")
        meta = {"model_requested": model, "model_used": "stub-model", "routed_to": "gx-fast",
                "latency_ms": 5, "usage": {"total_tokens": 10}}
        props = (schema or {}).get("properties") or {}
        if "nodes" in props:
            answer = self.graph_answers.pop(0) if self.graph_answers else json.dumps(DEFAULT_AI_GRAPH)
            return answer, meta
        if "scenes" in props:
            n = props["scenes"]["minItems"]
            return json.dumps({"title": "Stub ad", "narration": "Your car was hit. We help. Call today.",
                               "cta": "Call today.",
                               "scenes": [{"visual_prompt": f"scene {i + 1}: woman beside a car",
                                           "narration": f"line {i + 1}"} for i in range(n)]}), meta
        if "prompts" in props:
            n = props["prompts"]["minItems"]
            return json.dumps({"prompts": [f"scene prompt {i + 1}" for i in range(n)]}), meta
        if schema is not None:
            return json.dumps({k: _sample(v) for k, v in props.items()}), meta
        return "stub answer: " + str(messages[-1]["content"])[:40], meta


def _sample(spec: dict) -> Any:
    return {"string": "value", "number": 1, "boolean": True, "array": ["a"]}.get(spec.get("type"), "value")


DEFAULT_AI_GRAPH = {
    "name": "MVA Meta ad",
    "description": "AI draft",
    "nodes": [
        {"id": "brief", "type": "text.input", "label": "Brief",
         "config": {"text": "30-second MVA Meta ad: a woman whose BMW was rear-ended."}},
        {"id": "script", "type": "ai.script_writer", "label": "Script", "config": {"duration": 30, "scenes": 2,
                                                                                    "cta": "Call today."}},
        {"id": "voice", "type": "voice.tts", "label": "Voice",
         "config": {"voice_id": "preset:serena", "style": "trustworthy", "bogus_setting": 1}},
        {"id": "image", "type": "image.generate", "label": "Images", "config": {"size": "928x1664"}},
        {"id": "clip", "type": "video.i2v", "label": "Clips", "config": {"size": "480x832", "seconds": 5}},
        {"id": "music", "type": "music.instrumental", "label": "Music",
         "config": {"description": "subtle background music"}},
        {"id": "join", "type": "compose.concat", "label": "Join", "config": {}},
        {"id": "mix", "type": "compose.add_voice", "label": "Add voice", "config": {}},
        {"id": "bed", "type": "compose.add_music", "label": "Add music", "config": {}},
        {"id": "final", "type": "compose.export", "label": "Final", "config": {"preset": "1080x1920"}},
    ],
    "edges": [
        {"source": "brief", "source_port": "text", "target": "script", "target_port": "brief"},
        {"source": "script", "source_port": "narration", "target": "voice", "target_port": "text"},
        {"source": "script", "source_port": "visuals", "target": "image", "target_port": "prompt"},
        {"source": "image", "source_port": "image", "target": "clip", "target_port": "image"},
        {"source": "clip", "source_port": "video", "target": "join", "target_port": "video"},
        {"source": "join", "source_port": "video", "target": "mix", "target_port": "video"},
        {"source": "voice", "source_port": "audio", "target": "mix", "target_port": "audio"},
        {"source": "mix", "source_port": "video", "target": "bed", "target_port": "video"},
        {"source": "music", "source_port": "audio", "target": "bed", "target_port": "audio"},
        {"source": "bed", "source_port": "video", "target": "final", "target_port": "video"},
    ],
}


class StubFFmpeg:
    """Records FFJobs and 'renders' by copying the first input (or a PNG for images)."""

    def __init__(self, tmp_root: Path) -> None:
        self.tmp_root = Path(tmp_root)
        self.jobs: list[FFJob] = []
        self.fail_next: str | None = None
        self.delay = 0.0

    def probe(self, path: Path, kind: str, ext: str, asset_id: str | None = None) -> Media:
        return Media(path=path, ext=ext, kind=kind, duration=4.0 if kind != "image" else None,
                     width=832 if kind != "audio" else None, height=480 if kind != "audio" else None,
                     has_audio=kind == "audio", fps=16 if kind == "video" else None, asset_id=asset_id)

    def run(self, job: FFJob, *, cancel: threading.Event | None = None, timeout: float = 1800,
            log: Any = None) -> FFResult:
        from gx_control_ui.flows.ffmpeg import ComposeError

        self.jobs.append(job)
        deadline = time.time() + self.delay
        while time.time() < deadline:
            if cancel is not None and cancel.is_set():
                raise ComposeError("cancelled")
            time.sleep(0.02)
        if self.fail_next:
            msg, self.fail_next = self.fail_next, None
            raise ComposeError(msg)
        work = self.tmp_root / f"stub-ff-{secrets.token_hex(4)}"
        (work / "out").mkdir(parents=True)
        files = {}
        for role, name in job.outputs.items():
            dest = work / "out" / name
            if name.endswith(".png"):
                dest.write_bytes(png_bytes(seed=len(self.jobs)))
            elif name.endswith((".wav", ".mp3")):
                dest.write_bytes(WAV if name.endswith(".wav") else b"ID3" + b"\x00" * 64)
            else:
                src = job.inputs[0].path
                shutil.copyfile(src, dest)
                if dest.stat().st_size < 64:
                    dest.write_bytes(MP4)
            files[role] = dest
        return FFResult(files, work, 0.01, ["ffmpeg", *job.args])


def b64png() -> str:
    return base64.b64encode(png_bytes()).decode()
