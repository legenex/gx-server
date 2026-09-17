#!/usr/bin/env python3
"""gx-call engine: NVIDIA NemotronLabs VoiceChat 11B behind a loopback WebSocket.

Runs inside the ``gx-call-engine`` container on gx10-02. Only the gx-call
supervisor talks to it (127.0.0.1, bearer key from GX_CALL_ENGINE_KEY).

    GET  /health                    {"state": "loading|ready|busy|failed", ...}
    WS   /v1/engine/stream          one live call at a time (batch size 1)

Supervisor -> engine
    text   {"type": "session.configure", "session_id", "system_prompt",
            "tools": [...], "on_hold": {tool: [phrases]}, "tool_timeout_s"}
    binary caller audio, PCM16 LE mono 16 kHz, any chunk size, real time
    text   {"type": "tool.result", "call_id", "output"}   (ASCII, <= 2000 chars)
    text   {"type": "session.end", "reason"}
Engine -> supervisor
    text   {"type": "session.ready", ...} then gx-call.v1 events (gx_call_tracker)
    text   {"type": "tool.call", "call_id", "name", "arguments", "raw"}
    binary agent audio, PCM16 LE mono 22.05 kHz
    text   {"type": "engine.stats", ...} every 2 s, {"type": "session.ended", "summary"}

Other modes (run inside the image, GPU required):
    --bench WAV [--prompt TEXT] [--out DIR]   stream a WAV at full speed and time every step
    --selftest-cache                           cached vs uncached backbone logits must agree
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import logging
import os
import queue
import re
import secrets
import struct
import sys
import threading
import time
import wave
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path

import gx_voicechat_patches as patches
from gx_call_tracker import OUTPUT_RATE, Tracker, rms_dbfs_float

log = logging.getLogger("gx_call.engine")

NEMO_DIR = Path(os.environ.get("GX_CALL_NEMO_DIR", "/opt/nemo"))
STREAM_YAML = NEMO_DIR / "examples/speechlm2/nemo_inference_pipelines/conf/s2s_streaming.yaml"
TEMPLATE = NEMO_DIR / "examples/speechlm2/function_calling/template.jinja"
MODEL_DIR = os.environ.get("GX_CALL_MODEL_DIR", "/models/voicechat")
SPEAKER = os.environ.get("GX_CALL_SPEAKER", "Aria")
CHUNK_S = float(os.environ.get("GX_CALL_CHUNK_S", "0.08"))
BUFFER_S = float(os.environ.get("GX_CALL_BUFFER_S", "2.0"))
MAX_LEN = int(os.environ.get("GX_CALL_MAX_LEN", "6144"))
PORT = int(os.environ.get("GX_CALL_ENGINE_PORT", "18841"))
HOST = os.environ.get("GX_CALL_ENGINE_HOST", "0.0.0.0")  # container side; published on host loopback only
MAX_SESSION_S = int(os.environ.get("GX_CALL_MAX_SESSION_S", "1800"))
INPUT_RATE = 16000
STREAM_ID = 7
PROTOCOL = "gx-call.v1"
ASCII_MAP = {"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": " - ",
             "…": "...", " ": " ", "°": " degrees", "•": "-", "é": "e"}


def to_ascii(text: str, limit: int) -> str:
    """The model requires ASCII prompts and tool responses (model card)."""
    out = "".join(ASCII_MAP.get(ch, ch) for ch in str(text))
    out = out.encode("ascii", "ignore").decode("ascii")
    out = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", out)
    return out[:limit]


def render_system_prompt(system_message: str, tools: list[dict]) -> str:
    from jinja2 import Environment  # noqa: PLC0415

    env = Environment(autoescape=False, keep_trailing_newline=False)
    tpl = env.from_string(TEMPLATE.read_text(encoding="utf-8"))
    return to_ascii(tpl.render(system_message=system_message, tools=tools or None), 24000)


# ------------------------------------------------------------------ engine --
@dataclass
class StepOut:
    audio: object  # np.ndarray float32 at OUTPUT_RATE
    text: str
    asr: str
    asr_reset: bool
    function_text: str
    step_ms: float


@dataclass
class ToolWait:
    event: threading.Event = field(default_factory=threading.Event)
    output: str | None = None


class VoiceChatEngine:
    """Owns the NeMo streaming pipeline. Every method runs on ONE thread."""

    def __init__(self) -> None:
        self.state = "loading"
        self.error: str | None = None
        self.load_seconds: float | None = None
        self.pipeline = None
        self.patches: dict = {}
        self._text_pos = self._asr_pos = self._fn_pos = 0
        self.tool_handler = None  # callable(call_text) -> str, set per session

    def load(self) -> None:
        t0 = time.time()
        self.patches = patches.apply_all()
        import torch  # noqa: PLC0415
        from omegaconf import OmegaConf  # noqa: PLC0415

        from nemo.collections.speechlm2.inference.factory.s2s_pipeline_builder import S2SPipelineBuilder  # noqa: PLC0415

        torch.set_float32_matmul_precision("medium")
        cfg = OmegaConf.load(STREAM_YAML)
        overrides = {
            "audio_file": "unused", "output_dir": "/tmp/gx-call-generated",
            "s2s.model_path": MODEL_DIR, "s2s.llm_checkpoint_path": MODEL_DIR,
            "s2s.speaker_reference": None, "s2s.speaker_name": SPEAKER,
            "s2s.engine_type": "native", "s2s.system_prompt": None,
            "s2s.enable_builtin_tools": False,
            "s2s.use_perception_cudagraph": os.environ.get("GX_CALL_PERCEPTION_CUDAGRAPH", "1") == "1",
            "s2s.fc_tool_timeout_sec": float(os.environ.get("GX_CALL_TOOL_TIMEOUT_S", "12")),
            "streaming.chunk_size_in_secs": CHUNK_S,
            "streaming.buffer_size_in_secs": BUFFER_S,
            "streaming.max_len": MAX_LEN,
        }
        for key, value in overrides.items():
            OmegaConf.update(cfg, key, value, force_add=True)
        self.pipeline = S2SPipelineBuilder.build_pipeline(cfg)
        self.pipeline.open_session()
        self.pipeline._execute_tool_call = self._execute_tool_call  # every model tool call comes here
        self.pipeline.warmup()
        self.chunk_samples = int(self.pipeline.chunk_size_in_secs * self.pipeline.input_sample_rate)
        self.load_seconds = round(time.time() - t0, 1)
        self.state = "ready"
        log.info("VoiceChat loaded in %.1f s (patches %s, chunk %d samples)", self.load_seconds,
                 self.patches, self.chunk_samples)

    # tools ------------------------------------------------------------
    def _execute_tool_call(self, call_text: str) -> str | None:
        handler = self.tool_handler
        if handler is None:
            return json.dumps({"error": "no tools are available in this call"})
        try:
            return handler(call_text)
        except Exception:  # noqa: BLE001 - a tool must never kill the model thread
            log.exception("tool handler failed")
            return json.dumps({"error": "the tool failed"})

    def set_on_hold(self, on_hold: dict[str, list[str]]) -> None:
        """Per-agent on-hold phrases, tokenised exactly like upstream's loader."""
        import math  # noqa: PLC0415

        pipe = self.pipeline
        if not on_hold:
            pipe._fc_on_hold_token_map = None
            return
        stt = pipe.s2s_model.model.stt_model
        bos, eos, pad = (getattr(stt, "text_bos_id", None), getattr(stt, "text_eos_id", None),
                         getattr(stt, "text_pad_id", None))
        token_map = {}
        for tool, phrases in on_hold.items():
            variants = []
            for phrase in phrases[:4]:
                phrase = to_ascii(phrase, 200).strip()
                if not phrase:
                    continue
                ids = list(pipe.s2s_model.tokenizer.text_to_ids(phrase))
                trailing = max(17, math.ceil(0.5 * len(phrase)))
                if bos is not None:
                    ids = [bos] + ids
                if pad is not None:
                    ids += [pad] * trailing
                if eos is not None:
                    ids += [eos]
                variants.append(ids)
            if variants:
                token_map[tool] = variants
        pipe._fc_on_hold_token_map = token_map or None

    # stream -----------------------------------------------------------
    def start(self, system_prompt: str) -> None:
        import torch  # noqa: PLC0415
        from nemo.collections.asr.inference.streaming.framing.request import Frame  # noqa: PLC0415
        from nemo.collections.speechlm2.inference.streaming.framing.s2s_request_options import (  # noqa: PLC0415
            S2SRequestOptions)

        self._text_pos = self._asr_pos = self._fn_pos = 0
        self.pipeline.generate_step([Frame(samples=torch.empty(0, dtype=torch.float32), stream_id=STREAM_ID,
                                           is_first=True, is_last=False,
                                           options=S2SRequestOptions(system_prompt=system_prompt))])
        self.state = "busy"

    def step(self, pcm16: bytes, last: bool = False) -> StepOut:
        import numpy as np  # noqa: PLC0415
        import torch  # noqa: PLC0415
        from nemo.collections.asr.inference.streaming.framing.request import Frame  # noqa: PLC0415

        t0 = time.perf_counter()
        samples = torch.from_numpy(np.frombuffer(pcm16, dtype="<i2").astype(np.float32) / 32768.0)
        self.pipeline.generate_step([Frame(samples=samples, stream_id=STREAM_ID, is_first=False, is_last=last)])
        st = self.pipeline.get_or_create_state(STREAM_ID)
        audio = st.audio_buffer.detach().float().reshape(-1).cpu().numpy() if st.audio_buffer.numel() else \
            np.zeros(0, dtype=np.float32)
        full_text = st.get_output_text()
        full_asr = st.get_output_asr_text()
        full_fn = st.get_output_function_text()
        text, self._text_pos = full_text[self._text_pos:], len(full_text)
        asr_reset = len(full_asr) < self._asr_pos
        asr = "" if asr_reset else full_asr[self._asr_pos:]
        if asr_reset:
            asr = full_asr
        self._asr_pos = len(full_asr)
        fn, self._fn_pos = full_fn[self._fn_pos:], len(full_fn)
        st.cleanup_after_response()
        if last:
            self.pipeline.delete_state(STREAM_ID)
            self.state = "ready"
        return StepOut(audio=audio, text=text, asr=asr, asr_reset=asr_reset, function_text=fn,
                       step_ms=(time.perf_counter() - t0) * 1000)

    def abort(self) -> None:
        try:
            self.pipeline.reset_session()
            self.pipeline.open_session()
        except Exception:  # noqa: BLE001
            log.exception("pipeline reset failed")
        self.state = "ready"


def pcm16_from_float(audio) -> bytes:  # noqa: ANN001
    import numpy as np  # noqa: PLC0415

    clipped = np.clip(audio, -1.0, 1.0)
    return (clipped * 32767.0).astype("<i2").tobytes()


# --------------------------------------------------------------- session --
class Session:
    """One live call: an inference thread plus the asyncio side of the socket."""

    def __init__(self, engine: VoiceChatEngine, loop: asyncio.AbstractEventLoop, send) -> None:  # noqa: ANN001
        self.engine = engine
        self.loop = loop
        self.send = send  # coroutine function(obj: dict | bytes)
        self.inbuf = bytearray()
        self.in_arrival: list[tuple[int, float]] = []  # (byte offset end, wall time)
        self.consumed = 0
        self.cond = threading.Condition()
        self.ending: str | None = None
        self.tools: dict[str, dict] = {}
        self.tool_waits: dict[str, ToolWait] = {}
        self.tool_timeout_s = 10.0
        self.tracker = Tracker(emit=self._emit_event)
        self.step_ms: list[float] = []
        self.steps = 0
        self.started = time.time()
        self.config: dict = {}
        self.thread: threading.Thread | None = None
        self.summary: dict | None = None
        self.failed: str | None = None

    # thread-safe outbound
    def _post(self, obj) -> None:  # noqa: ANN001
        asyncio.run_coroutine_threadsafe(self.send(obj), self.loop)

    def _emit_event(self, event: dict) -> None:
        self._post(event)

    # inbound (asyncio thread)
    def feed(self, data: bytes) -> None:
        with self.cond:
            self.inbuf.extend(data)
            self.in_arrival.append((self.consumed + len(self.inbuf), time.time()))
            if len(self.in_arrival) > 4096:
                self.in_arrival = self.in_arrival[-2048:]
            self.cond.notify()

    def end(self, reason: str) -> None:
        with self.cond:
            self.ending = self.ending or reason
            self.cond.notify()
        for wait in self.tool_waits.values():
            wait.event.set()

    def tool_result(self, call_id: str, output: object) -> None:
        wait = self.tool_waits.get(call_id)
        if wait is None:
            return
        wait.output = to_ascii(output if isinstance(output, str) else json.dumps(output), 2000)
        wait.event.set()

    # tool calls (FC background thread) ---------------------------------
    def handle_tool(self, call_text: str) -> str:
        raw = call_text.strip()
        body = raw.removeprefix("<TOOLCALL>").removesuffix("</TOOLCALL>").strip()
        name, args = None, {}
        try:
            parsed = json.loads(body)
            call = parsed[0] if isinstance(parsed, list) and parsed else parsed
            if isinstance(call, dict):
                name = str(call.get("name") or "").replace("-", "_")
                args = call.get("arguments", call.get("parameters", {}))
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        args = {"value": args}
        except ValueError:
            pass
        call_id = "tc_" + secrets.token_hex(8)
        if not name or name not in self.tools:
            self._post({"type": "tool.call", "call_id": call_id, "name": name or "", "arguments": args,
                        "raw": raw[:2000], "known": False})
            return json.dumps({"error": f"there is no tool called {name or 'that'}"})
        wait = ToolWait()
        self.tool_waits[call_id] = wait
        self._post({"type": "tool.call", "call_id": call_id, "name": name, "arguments": args,
                    "raw": raw[:2000], "known": True})
        got = wait.event.wait(self.tool_timeout_s)
        self.tool_waits.pop(call_id, None)
        if not got or wait.output is None:
            self._post({"type": "tool.timeout", "call_id": call_id, "name": name})
            return json.dumps({"error": "timeout", "message": "the system did not answer in time"})
        return wait.output

    # inference thread --------------------------------------------------
    def run(self) -> None:
        eng = self.engine
        chunk_bytes = eng.chunk_samples * 2
        last_stats = time.time()
        try:
            cfg = self.config
            self.tools = {t["name"]: t for t in cfg.get("tools", []) if isinstance(t, dict) and t.get("name")}
            self.tool_timeout_s = float(cfg.get("tool_timeout_s", 10))
            eng.set_on_hold(cfg.get("on_hold") or {})
            eng.tool_handler = self.handle_tool
            prompt = render_system_prompt(to_ascii(cfg.get("system_prompt", ""), 16000),
                                          [{k: v for k, v in t.items() if k in ("name", "description", "parameters")}
                                           for t in self.tools.values()])
            t0 = time.time()
            eng.start(prompt)
            self._post({"type": "session.ready", "protocol": PROTOCOL, "input_rate": INPUT_RATE,
                        "output_rate": OUTPUT_RATE, "chunk_ms": round(CHUNK_S * 1000),
                        "prefill_ms": round((time.time() - t0) * 1000), "prompt_chars": len(prompt),
                        "voice": SPEAKER, "tools": sorted(self.tools)})
            deadline = time.time() + MAX_SESSION_S
            while True:
                with self.cond:
                    while len(self.inbuf) < chunk_bytes and not self.ending:
                        self.cond.wait(0.5)
                    if self.ending and len(self.inbuf) < chunk_bytes:
                        break
                    chunk = bytes(self.inbuf[:chunk_bytes])
                    del self.inbuf[:chunk_bytes]
                    self.consumed += chunk_bytes
                    backlog_ms = len(self.inbuf) / 2 / INPUT_RATE * 1000
                    arrival = next((w for off, w in self.in_arrival if off >= self.consumed), time.time())
                    self.in_arrival = [(o, w) for o, w in self.in_arrival if o > self.consumed]
                out = eng.step(chunk)
                self.steps += 1
                self.step_ms.append(out.step_ms)
                if out.audio.size:
                    self._post(pcm16_from_float(out.audio))
                self.tracker.step(in_pcm16=chunk, in_wall=arrival,
                                  out_level_dbfs=rms_dbfs_float(out.audio) if out.audio.size else -120.0,
                                  out_ms=round(out.audio.size / OUTPUT_RATE * 1000),
                                  text_delta=out.text, asr_delta=out.asr, asr_reset=out.asr_reset)
                if out.function_text:
                    self._post({"type": "tool.channel", "text": out.function_text[:2000]})
                now = time.time()
                if now - last_stats >= 2.0:
                    last_stats = now
                    self._post(self.stats(backlog_ms))
                if now > deadline:
                    self.ending = self.ending or "max_duration"
                    break
            # close the stream cleanly (is_last frame of silence)
            eng.step(b"\x00\x00" * eng.chunk_samples, last=True)
            self.summary = {**self.tracker.finish(), **self.stats(0.0), "reason": self.ending}
        except Exception as exc:  # noqa: BLE001
            log.exception("session failed")
            self.failed = type(exc).__name__
            eng.abort()
            self.summary = {**self.tracker.summary(), "reason": "engine_error", "error": self.failed}
            self._post({"type": "error", "code": "engine_error",
                        "message": "the voice model failed during the call", "fatal": True})
        finally:
            eng.tool_handler = None
            eng.state = "ready" if eng.state != "failed" else eng.state
            self._post({"type": "session.ended", "summary": self.summary})

    def stats(self, backlog_ms: float) -> dict:
        recent = self.step_ms[-100:]
        chunk_ms = CHUNK_S * 1000
        mean = sum(recent) / len(recent) if recent else 0.0
        ordered = sorted(recent)
        p95 = ordered[int(len(ordered) * 0.95) - 1] if len(ordered) >= 20 else (ordered[-1] if ordered else 0.0)
        all_mean = sum(self.step_ms) / len(self.step_ms) if self.step_ms else 0.0
        return {"type": "engine.stats", "steps": self.steps, "step_ms_mean": round(mean, 1),
                "step_ms_p95": round(p95, 1), "rtf": round(mean / chunk_ms, 3) if chunk_ms else None,
                "rtf_session": round(all_mean / chunk_ms, 3) if chunk_ms else None,
                "backlog_ms": round(backlog_ms), "realtime": bool(recent) and p95 <= chunk_ms}


# ------------------------------------------------------------------ server --
class Server:
    def __init__(self, engine: VoiceChatEngine, key: str) -> None:
        self.engine = engine
        self.key = key
        self.session: Session | None = None
        self.sessions_total = 0

    def health(self) -> dict:
        import torch  # noqa: PLC0415

        mem = {}
        try:
            free, total = torch.cuda.mem_get_info()
            mem = {"cuda_allocated_gib": round(torch.cuda.memory_allocated() / 2**30, 2),
                   "cuda_reserved_gib": round(torch.cuda.memory_reserved() / 2**30, 2),
                   "device_free_gib": round(free / 2**30, 2)}
        except Exception:  # noqa: BLE001
            pass
        return {"service": "gx-call-engine", "state": self.engine.state, "error": self.engine.error,
                "load_seconds": self.engine.load_seconds, "patches": self.engine.patches,
                "busy": self.session is not None, "sessions_total": self.sessions_total,
                "voice": SPEAKER, "chunk_ms": round(CHUNK_S * 1000), "memory": mem, "protocol": PROTOCOL}

    def process_request(self, connection, request):  # noqa: ANN001
        from websockets.http11 import Response  # noqa: PLC0415
        from websockets.datastructures import Headers  # noqa: PLC0415

        def respond(status: int, body: dict):
            data = json.dumps(body).encode()
            return Response(status, HTTPStatus(status).phrase,
                            Headers({"Content-Type": "application/json", "Content-Length": str(len(data)),
                                     "Cache-Control": "no-store"}), data)

        path = request.path.split("?", 1)[0]
        if path == "/health":
            return respond(200, self.health())
        auth = request.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        if not token or not hmac.compare_digest(token.encode(), self.key.encode()):
            return respond(401, {"error": {"code": "unauthorized", "message": "invalid engine key"}})
        if path != "/v1/engine/stream":
            return respond(404, {"error": {"code": "not_found", "message": "no such path"}})
        if self.engine.state != "ready" or self.session is not None:
            return respond(409, {"error": {"code": "engine_busy", "message": f"engine is {self.engine.state}"}})
        return None

    async def handler(self, ws) -> None:  # noqa: ANN001
        loop = asyncio.get_running_loop()
        lock = asyncio.Lock()

        async def send(obj) -> None:  # noqa: ANN001
            try:
                async with lock:
                    await ws.send(obj if isinstance(obj, (bytes, bytearray)) else json.dumps(obj))
            except Exception:  # noqa: BLE001 - the socket may be gone; the session ends on its own
                pass

        if self.session is not None:
            await ws.close(1013, "busy")
            return
        session = Session(self.engine, loop, send)
        self.session = session
        self.sessions_total += 1
        ended = asyncio.Event()
        try:
            first = await asyncio.wait_for(ws.recv(), timeout=30)
            cfg = json.loads(first) if isinstance(first, str) else {}
            if cfg.get("type") != "session.configure":
                await ws.close(1008, "expected session.configure")
                return
            session.config = cfg
            session.thread = threading.Thread(target=self._run_session, args=(session, loop, ended),
                                              name="gx-call-session", daemon=True)
            session.thread.start()
            async for message in ws:
                if isinstance(message, (bytes, bytearray)):
                    session.feed(bytes(message))
                    continue
                try:
                    msg = json.loads(message)
                except ValueError:
                    continue
                kind = msg.get("type")
                if kind == "tool.result":
                    session.tool_result(str(msg.get("call_id", "")), msg.get("output", ""))
                elif kind == "session.end":
                    session.end(str(msg.get("reason") or "ended")[:40])
                    break
        except (asyncio.TimeoutError, ValueError):
            pass
        except Exception:  # noqa: BLE001
            log.exception("socket error")
        finally:
            session.end("disconnected")
            if session.thread is not None:
                await ended.wait()
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
            self.session = None

    def _run_session(self, session: Session, loop, ended: asyncio.Event) -> None:  # noqa: ANN001
        try:
            session.run()
        finally:
            loop.call_soon_threadsafe(ended.set)


async def serve(engine: VoiceChatEngine, key: str) -> None:
    from websockets.asyncio.server import serve as ws_serve  # noqa: PLC0415

    server = Server(engine, key)
    loader = threading.Thread(target=_load_engine, args=(engine,), name="gx-call-load", daemon=True)
    loader.start()
    async with ws_serve(server.handler, HOST, PORT, process_request=server.process_request,
                        max_size=4 * 1024 * 1024, ping_interval=20, ping_timeout=60,
                        compression=None) as srv:
        log.info("gx-call engine listening on %s:%s", HOST, PORT)
        await srv.serve_forever()


def _load_engine(engine: VoiceChatEngine) -> None:
    try:
        engine.load()
    except Exception as exc:  # noqa: BLE001
        log.exception("model load failed")
        engine.state = "failed"
        engine.error = f"{type(exc).__name__}: {str(exc)[:300]}"


# ------------------------------------------------------------ bench modes --
def read_wav_16k(path: str) -> bytes:
    import numpy as np  # noqa: PLC0415

    with wave.open(path, "rb") as w:
        rate, ch, width, n = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()
        raw = w.readframes(n)
    if width != 2:
        raise SystemExit("bench WAV must be 16-bit PCM")
    data = np.frombuffer(raw, dtype="<i2").reshape(-1, ch).mean(axis=1)
    if rate != INPUT_RATE:
        import torch  # noqa: PLC0415
        import torchaudio.functional as AF  # noqa: PLC0415
        data = AF.resample(torch.from_numpy(data.astype(np.float32)), rate, INPUT_RATE).numpy()
    return data.astype("<i2").tobytes()


def bench(wav: str, prompt: str, out_dir: str, tail_s: float) -> None:
    engine = VoiceChatEngine()
    engine.load()
    pcm = read_wav_16k(wav) + b"\x00\x00" * int(tail_s * INPUT_RATE)
    events: list[dict] = []
    tracker = Tracker(emit=events.append)
    engine.start(render_system_prompt(prompt, []))
    chunk = engine.chunk_samples * 2
    audio_out = bytearray()
    times = []
    for off in range(0, len(pcm) - chunk + 1, chunk):
        part = pcm[off:off + chunk]
        out = engine.step(part)
        times.append(out.step_ms)
        if out.audio.size:
            audio_out += pcm16_from_float(out.audio)
        tracker.step(in_pcm16=part, in_wall=time.time(),
                     out_level_dbfs=rms_dbfs_float(out.audio) if out.audio.size else -120.0,
                     out_ms=round(out.audio.size / OUTPUT_RATE * 1000), text_delta=out.text,
                     asr_delta=out.asr, asr_reset=out.asr_reset)
    engine.step(b"\x00\x00" * engine.chunk_samples, last=True)
    summary = tracker.finish()
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    with wave.open(str(Path(out_dir) / "agent.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(OUTPUT_RATE)
        w.writeframes(bytes(audio_out))
    ordered = sorted(times[5:]) or sorted(times)
    result = {
        "chunk_ms": CHUNK_S * 1000, "steps": len(times), "load_seconds": engine.load_seconds,
        "patches": engine.patches, "step_ms_mean": round(sum(ordered) / len(ordered), 1),
        "step_ms_p50": round(ordered[len(ordered) // 2], 1), "step_ms_p95": round(ordered[int(len(ordered) * .95)], 1),
        "step_ms_max": round(ordered[-1], 1),
        "rtf": round(sum(ordered) / len(ordered) / (CHUNK_S * 1000), 3),
        "agent_audio_s": round(len(audio_out) / 2 / OUTPUT_RATE, 2), "summary": summary,
        "agent_text": " | ".join(e["text"] for e in events if e["type"] == "transcript.agent.final"),
        "user_text": " | ".join(e["text"] for e in events if e["type"] == "transcript.user.final"),
        "step_ms_series": [round(t, 1) for t in times],
    }
    (Path(out_dir) / "events.json").write_text(json.dumps(events, indent=1))
    (Path(out_dir) / "bench.json").write_text(json.dumps(result, indent=1))
    print(json.dumps({k: v for k, v in result.items() if k != "step_ms_series"}, indent=1))


def selftest_cache() -> None:
    """Cached (patched) and uncached backbone logits must agree on the same inputs."""
    import torch  # noqa: PLC0415

    engine = VoiceChatEngine()
    engine.load()
    wrapper = engine.pipeline.s2s_model
    stt = wrapper.model.stt_model
    torch.manual_seed(0)
    prompt = render_system_prompt("You are a helpful assistant on a phone call.", [])
    emb, n = wrapper._prepare_system_prompt_embeddings(prompt)
    steps = 24
    extra = stt.embed_tokens(torch.randint(100, 5000, (1, steps), device=emb.device)).to(emb.dtype)
    full = torch.cat([emb, extra], dim=1)
    with torch.inference_mode():
        ref = stt(full, cache=None)["text_logits"][:, -steps:].float()
        cache = patches.new_hybrid_cache(wrapper)
        stt(emb, cache=cache)
        outs = []
        for i in range(steps):
            outs.append(stt(extra[:, i:i + 1], cache=cache)["text_logits"][:, -1:].float())
        got = torch.cat(outs, dim=1)
    diff = (got - ref).abs().max().item()
    agree = (got.argmax(-1) == ref.argmax(-1)).float().mean().item()
    result = {"prompt_tokens": int(n), "steps": steps, "max_abs_logit_diff": round(diff, 4),
              "argmax_agreement": agree, "ok": agree == 1.0}
    print(json.dumps(result))
    if not result["ok"]:
        sys.exit(1)


def main() -> int:
    logging.basicConfig(level=os.environ.get("GX_CALL_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--bench")
    parser.add_argument("--prompt", default="You are a friendly phone agent. Keep answers short.")
    parser.add_argument("--out", default="/work/bench")
    parser.add_argument("--tail", type=float, default=8.0)
    parser.add_argument("--selftest-cache", action="store_true")
    args = parser.parse_args()
    if args.selftest_cache:
        selftest_cache()
        return 0
    if args.bench:
        bench(args.bench, args.prompt, args.out, args.tail)
        return 0
    key = os.environ.get("GX_CALL_ENGINE_KEY", "")
    if len(key) < 32:
        log.error("GX_CALL_ENGINE_KEY missing or too short")
        return 78
    asyncio.run(serve(VoiceChatEngine(), key))
    return 0


if __name__ == "__main__":
    sys.exit(main())
