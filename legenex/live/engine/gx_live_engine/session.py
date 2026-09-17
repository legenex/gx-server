"""One live conversation inside the engine: VAD, barge-in, camera, turns, tools.

Threads:
* the WebSocket reader (engine server) calls ``on_binary`` / ``on_event``;
  VAD runs there (CPU, ~0.3 ms per 32 ms window) so speech onset is seen
  immediately even while the GPU is busy answering;
* one worker thread owns every model call, in order.

Audio and images live only in memory for the turn that uses them.
"""

from __future__ import annotations

import logging
import queue
import secrets
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from collections.abc import Callable

import numpy as np

from .vad import StreamingVad

log = logging.getLogger("gx_live_engine.session")

HEADER = struct.Struct("!BBHI")
KIND_MIC, KIND_CAMERA, KIND_AUDIO_OUT = 0x01, 0x02, 0x11
FRAME_FRESH_S = 10.0
OUT_RATE = 24000


@dataclass
class Job:
    kind: str                       # speech | text | tool | stop
    created: float
    audio: np.ndarray | None = None
    text: str = ""
    frame: bytes | None = None
    call_id: str = ""
    name: str = ""
    ok: bool = True
    ended_at: float = 0.0
    extra: dict = field(default_factory=dict)


class EngineSession:
    def __init__(self, runtime: Any, sid: str, config: dict, tools: list[dict], *,
                 send_json: Callable[[dict], None], send_binary: Callable[[bytes], None],
                 vad: StreamingVad, clock: Callable[[], float] = time.monotonic,
                 asr: bool = True) -> None:
        self.rt = runtime
        self.sid = sid
        self.config = config
        self.tools = tools
        self.send_json = send_json
        self.send_binary = send_binary
        self.vad = vad
        self.clock = clock
        self.asr = asr
        self.t0 = clock()
        self.jobs: queue.Queue[Job] = queue.Queue()
        self.cancel = threading.Event()
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.output_audio = bool(config.get("output_audio", True))
        self.camera_on = True
        self.muted = False
        self.client_attached = True
        self.client_playing = False
        self.playback_until = 0.0
        self.frame: tuple[float, bytes] | None = None
        self.response = 0
        self.turn = 0
        self.generating = False
        self.cancel_reason = ""
        self.cancel_at: float | None = None
        self.audio_seq = 0
        self.ready = False
        self.worker = threading.Thread(target=self._run, name=f"session-{sid[-6:]}", daemon=True)

    # ------------------------------------------------------------ helpers --
    def ms(self, t: float | None = None) -> int:
        return int(((self.clock() if t is None else t) - self.t0) * 1000)

    def start(self) -> None:
        self.worker.start()

    def stop(self) -> None:
        self.stopped.set()
        self._cancel("session_end")
        self.jobs.put(Job("stop", self.clock()))

    def playing(self) -> bool:
        return self.client_playing or self.clock() < self.playback_until

    def _cancel(self, reason: str) -> None:
        with self.lock:
            if self.generating and not self.cancel.is_set():
                self.cancel_reason = reason
                self.cancel_at = self.clock()
                self.cancel.set()

    # ------------------------------------------------------------- inputs --
    def on_binary(self, data: bytes) -> None:
        if len(data) < HEADER.size:
            return
        kind, _version, _resp, _seq = HEADER.unpack_from(data)
        payload = data[HEADER.size:]
        if kind == KIND_CAMERA:
            if self.camera_on:
                self.frame = (self.clock(), payload)
            return
        if kind != KIND_MIC or self.muted or not self.ready or not self.client_attached:
            return
        pcm = np.frombuffer(payload, dtype="<i2").astype(np.float32) / 32768.0
        for ev in self.vad.feed(pcm, playing=self.playing()):
            now = self.clock()
            if ev.kind == "start":
                during = self.generating or self.playing()
                self.send_json({"type": "input.speech.started", "at_ms": self.ms(now), "during_response": during})
                if self.generating:
                    self._cancel("barge_in")
                elif self.playing():
                    self.playback_until = 0.0
                    self.send_json({"type": "response.interrupted", "response": self.response,
                                    "reason": "barge_in", "latency_ms": 0})
            elif ev.kind == "stop" and ev.audio is not None:
                self.send_json({"type": "input.speech.stopped", "at_ms": self.ms(now),
                                "duration_ms": int(len(ev.audio) * 1000 / 16000)})
                self.jobs.put(Job("speech", now, audio=ev.audio, frame=self._fresh_frame(now), ended_at=now))

    def _fresh_frame(self, now: float) -> bytes | None:
        if not self.camera_on or self.frame is None:
            return None
        at, data = self.frame
        return data if now - at <= FRAME_FRESH_S else None

    def on_event(self, ev: dict) -> None:
        kind = ev.get("type")
        now = self.clock()
        if kind == "session.update":
            if "camera" in ev:
                self.camera_on = bool(ev["camera"])
                if not self.camera_on:
                    self.frame = None
            if "output_audio" in ev:
                self.output_audio = bool(ev["output_audio"])
            if "muted" in ev:
                self.muted = bool(ev["muted"])
                self.vad.reset()
        elif kind == "input.text":
            if self.generating:
                self._cancel("new_input")
            self.jobs.put(Job("text", now, text=str(ev.get("text", ""))[:4000], frame=self._fresh_frame(now),
                              ended_at=now))
        elif kind == "input.audio.commit":
            vev = self.vad.commit()
            if vev is not None and vev.audio is not None:
                self.send_json({"type": "input.speech.stopped", "at_ms": self.ms(now),
                                "duration_ms": int(len(vev.audio) * 1000 / 16000)})
                self.jobs.put(Job("speech", now, audio=vev.audio, frame=self._fresh_frame(now), ended_at=now))
        elif kind == "response.cancel":
            if self.generating:
                self._cancel("client_cancel")
            else:
                self.playback_until = 0.0
                self.send_json({"type": "response.interrupted", "response": self.response,
                                "reason": "client_cancel", "latency_ms": 0})
        elif kind == "playback.state":
            self.client_playing = bool(ev.get("playing"))
            if not self.client_playing:
                self.playback_until = 0.0
        elif kind == "client.detached":
            self.client_attached = False
            self._cancel("client_detached")
            self.vad.reset()
        elif kind == "client.attached":
            self.client_attached = True
            self.vad.reset()
        elif kind == "tool.result":
            self.jobs.put(Job("tool", now, call_id=str(ev.get("call_id", ""))[:40], name=str(ev.get("name", ""))[:40],
                              ok=bool(ev.get("ok")), text=str(ev.get("content", ""))[:8000]))
        elif kind == "engine.session.end":
            self.stop()

    # -------------------------------------------------------------- worker --
    def _run(self) -> None:
        try:
            t = self.rt.start_session(self.sid, self.config, self.tools)
            self.ready = True
            self.send_json({"type": "session.ready", "system_prefill_ms": int(t * 1000)})
        except Exception:  # noqa: BLE001
            log.exception("session start failed")
            self.send_json({"type": "error", "code": "session_start_failed", "fatal": True,
                            "message": "the live model could not start the conversation"})
            return
        while not self.stopped.is_set():
            job = self.jobs.get()
            if job.kind == "stop" or self.stopped.is_set():
                break
            try:
                self._handle(job)
            except Exception:  # noqa: BLE001
                log.exception("turn failed")
                self.send_json({"type": "error", "code": "turn_failed", "fatal": False,
                                "message": "that turn could not be answered; please try again"})
        try:
            self.rt.end_session()
        except Exception:  # noqa: BLE001
            log.exception("end_session failed")

    def _handle(self, job: Job) -> None:
        rt = self.rt
        frame = None
        if job.frame is not None:
            try:
                frame = rt.decode_jpeg(job.frame)
            except Exception:  # noqa: BLE001 - a corrupt camera frame must not kill the turn
                self.send_json({"type": "error", "code": "bad_camera_frame", "fatal": False,
                                "message": "the camera frame could not be decoded; answering without it"})
        trigger = {"speech": "speech", "text": "text", "tool": "tool"}[job.kind]
        if job.kind in ("speech", "text"):
            self.turn += 1
        if job.kind == "text":
            self.send_json({"type": "transcript.user", "turn": self.turn, "text": job.text, "source": "text"})
        t_start = self.clock()
        if job.kind == "speech":
            prefill = rt.prefill_audio(self.sid, job.audio, frame)
        elif job.kind == "text":
            prefill = rt.prefill_text(self.sid, job.text, frame)
        else:
            content = job.text if job.ok else f"error: {job.text}"
            prefill = rt.prefill_tool_response(self.sid, job.name, content)
        self._respond(trigger, job, prefill, used_frame=frame is not None)
        if job.kind == "speech" and self.asr and job.audio is not None:
            try:
                t = self.clock()
                text = rt.transcribe(job.audio, self.config.get("language", "en"))
                self.send_json({"type": "transcript.user", "turn": self.turn, "text": text, "source": "speech",
                                "asr_ms": int((self.clock() - t) * 1000)})
            except Exception:  # noqa: BLE001
                log.exception("transcription failed")
        del t_start

    def _respond(self, trigger: str, job: Job, prefill_s: float, *, used_frame: bool) -> None:
        rt = self.rt
        self.response += 1
        response = self.response
        self.audio_seq = 0
        with self.lock:
            self.cancel.clear()
            self.cancel_reason = ""
            self.cancel_at = None
            self.generating = True
        self.send_json({"type": "response.started", "response": response, "turn": self.turn, "trigger": trigger,
                        "camera_frame": used_frame})
        t_gen = self.clock()
        first_audio: list[float] = []

        def on_audio(wave: np.ndarray, caption: str) -> None:
            if self.cancel.is_set():
                return
            now = self.clock()
            if not first_audio:
                first_audio.append(now)
            pcm = (np.clip(wave, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
            if caption:
                self.send_json({"type": "transcript.assistant.delta", "response": response, "text": caption})
            self.send_binary(HEADER.pack(KIND_AUDIO_OUT, 1, response & 0xFFFF, self.audio_seq) + pcm)
            self.audio_seq += 1
            self.playback_until = max(self.playback_until, now) + len(wave) / OUT_RATE

        def on_text(delta: str) -> None:
            if delta and not self.cancel.is_set():
                self.send_json({"type": "transcript.assistant.delta", "response": response, "text": delta})

        res = rt.speak(self.sid, audio_out=self.output_audio, max_tokens=int(self.config.get("max_response_tokens",
                                                                                               256)),
                       cancel=self.cancel, on_audio=on_audio, on_text=on_text)
        done = self.clock()
        with self.lock:
            self.generating = False
            reason = self.cancel_reason
            cancel_at = self.cancel_at
        status = res.status
        if status == "interrupted":
            self.playback_until = 0.0
            self.send_json({"type": "response.interrupted", "response": response, "reason": reason or "barge_in",
                            "latency_ms": int((done - cancel_at) * 1000) if cancel_at else None})
        metrics = {
            "prefill_ms": int(prefill_s * 1000),
            "first_text_ms": int((t_gen + res.first_text_s - job.ended_at) * 1000) if res.first_text_s and job.ended_at
            else None,
            "first_audio_ms": int((first_audio[0] - job.ended_at) * 1000) if first_audio and job.ended_at else None,
            "turn_ms": int((done - job.ended_at) * 1000) if job.ended_at else None,
            "queue_ms": int((t_gen - job.created) * 1000 - prefill_s * 1000),
            "audio_ms": int(res.audio_samples * 1000 / OUT_RATE), "tokens": res.tokens,
        }
        if status == "tool":
            call = res.tool_call
            if call is not None:
                call_id = "call_" + secrets.token_hex(8)
                self.send_json({"type": "tool.request", "call_id": call_id, "name": call["name"],
                                "arguments": call["arguments"], "response": response})
            else:
                self.jobs.put(Job("tool", self.clock(), name="unknown", ok=False,
                                  text=res.tool_error or "invalid tool call"))
        self.send_json({"type": "response.done", "response": response,
                        "status": "completed" if status == "tool" else status,
                        "tool_call": status == "tool", "text": res.text, "metrics": metrics})
        if status == "failed":
            self.send_json({"type": "error", "code": "generation_failed", "fatal": False,
                            "message": "the answer could not be generated"})
