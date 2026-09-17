"""Call sessions, the one-call-at-a-time engine queue, relay and lifecycle policy.

A call session goes through::

    created -> (joined) -> queued -> loading -> live -> ending -> ended
                                     \\-> waiting (memory / gx-max / maintenance)

* ``created``: the Control Center registered it (agent config snapshot, owner,
  a join token that only the Control Center knows and puts in the tunnel's
  upstream path).
* The caller connects through the Playground tunnel; gx-call validates the
  bearer key, ``X-GX-Session`` and the join token before the 101.
* The engine runs ONE call at a time (the model's streaming pipeline has
  batch size 1). Other joined calls wait in FIFO order and are told their
  position.
* Events (transcripts, tool calls, timings) are forwarded to the caller and
  kept in memory for the Control Center, which long-polls them, persists
  them and executes tools. Nothing with call content is written to disk on
  gx10-02 except an optional recording, which the Control Center imports
  into the Library and then deletes here.
"""

from __future__ import annotations

import collections
import hashlib
import hmac
import json
import logging
import os
import secrets
import shutil
import struct
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import validation as v
from .config import Config
from .engine import READY, EngineController, meminfo
from .errors import (ConflictError, EngineError, NotFoundError, ResourceWait, UnavailableError,
                     ValidationError)
from .ws import Message, WebSocket, WSClosed, WSProtocolError, connect

log = logging.getLogger("gx_call.service")

TERMINAL = frozenset({"ended"})
EVENT_LOG_MAX = 20000
INPUT_RATE, OUTPUT_RATE = 16000, 22050
CLIENT_TEXT_TYPES = frozenset({"session.end", "ping", "input.text", "client.info"})


def _wav_header(data_len: int, rate: int, channels: int = 1) -> bytes:
    return (b"RIFF" + struct.pack("<I", 36 + data_len) + b"WAVEfmt " +
            struct.pack("<IHHIIHH", 16, 1, channels, rate, rate * 2 * channels, 2 * channels, 16) +
            b"data" + struct.pack("<I", data_len))


@dataclass
class CallSession:
    spec: v.SessionSpec
    join_hash: str
    created_at: float
    state: str = "created"
    detail: str = ""
    seq: int = 0
    events: collections.deque = field(default_factory=lambda: collections.deque(maxlen=EVENT_LOG_MAX))
    cond: threading.Condition = field(default_factory=threading.Condition)
    client: WebSocket | None = None
    engine_ws: WebSocket | None = None
    joined_at: float | None = None
    live_at: float | None = None
    ended_at: float | None = None
    last_disconnect: float | None = None
    end_reason: str | None = None
    disposition: str | None = None
    summary: dict | None = None
    error: dict | None = None
    tool_calls: dict = field(default_factory=dict)
    tools_done: int = 0
    audio_in_bytes: int = 0
    audio_out_bytes: int = 0
    joins: int = 0
    end_requested: threading.Event = field(default_factory=threading.Event)
    end_asked_at: float | None = None
    client_attached: threading.Event = field(default_factory=threading.Event)
    recording: dict | None = None

    @property
    def id(self) -> str:
        return self.spec.session_id

    def view(self) -> dict:
        now = time.time()
        return {
            "object": "call.session", "session_id": self.id, "state": self.state, "detail": self.detail,
            **{k: val for k, val in self.spec.public().items() if k != "session_id"},
            "created_at": self.created_at, "joined_at": self.joined_at, "live_at": self.live_at,
            "ended_at": self.ended_at, "end_reason": self.end_reason, "disposition": self.disposition,
            "duration_s": round((self.ended_at or now) - self.live_at, 1) if self.live_at else 0.0,
            "audio_in_s": round(self.audio_in_bytes / 2 / INPUT_RATE, 1),
            "audio_out_s": round(self.audio_out_bytes / 2 / OUTPUT_RATE, 1),
            "tool_calls": self.tools_done, "events": self.seq, "summary": self.summary, "error": self.error,
            "connected": self.client is not None, "recording": self.recording,
        }


class CallService:
    def __init__(self, cfg: Config, engine: EngineController, metrics=None,  # noqa: ANN001
                 engine_connect: Callable[..., WebSocket] = connect) -> None:
        self.cfg = cfg
        self.engine = engine
        self.metrics = metrics
        self.engine_connect = engine_connect
        self._lock = threading.RLock()
        self.sessions: dict[str, CallSession] = {}
        self.queue: collections.deque[str] = collections.deque()
        self.live: str | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._loader: threading.Thread | None = None
        self.load_wait: dict | None = None
        cfg.recordings_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    # --------------------------------------------------------- lifecycle --
    def start(self) -> None:
        self.engine.reconcile()
        self._purge_orphan_recordings()
        self._threads = [threading.Thread(target=self._dispatcher, name="gx-call-dispatch", daemon=True),
                         threading.Thread(target=self._reaper, name="gx-call-reaper", daemon=True)]
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        for sid in list(self.sessions):
            self._request_end(sid, "service_stopping")

    # ------------------------------------------------------------ events --
    def emit(self, s: CallSession, event: dict, *, to_client: bool = True) -> dict:
        with s.cond:
            s.seq += 1
            ev = {"seq": s.seq, "session_id": s.id, **event}
            ev.setdefault("t", round(time.time() * 1000))
            s.events.append(ev)
            s.cond.notify_all()
        if to_client and s.client is not None:
            try:
                s.client.send_text(json.dumps(ev, separators=(",", ":")))
            except (OSError, WSClosed):
                pass
        return ev

    def events_after(self, sid: str, after: int, wait_s: float, limit: int = 500) -> dict:
        s = self._session(sid)
        deadline = time.time() + max(0.0, min(wait_s, 30.0))
        with s.cond:
            while True:
                items = [e for e in s.events if e["seq"] > after][:limit]
                if items or s.state in TERMINAL or time.time() >= deadline:
                    break
                s.cond.wait(max(0.05, deadline - time.time()))
        return {"events": items, "next": items[-1]["seq"] if items else after, "state": s.state,
                "dropped": bool(s.events) and s.events[0]["seq"] > after + 1}

    def _set_state(self, s: CallSession, state: str, detail: str = "", **extra: Any) -> None:
        s.state, s.detail = state, detail
        self.emit(s, {"type": "session.status", "state": state, "detail": detail, **extra})

    # ---------------------------------------------------------- sessions --
    def _session(self, sid: str) -> CallSession:
        if not v.SESSION_RE.match(sid or ""):
            raise NotFoundError("no such call session")
        s = self.sessions.get(sid)
        if s is None:
            raise NotFoundError("no such call session")
        return s

    def create(self, body: Any) -> dict:
        spec = v.session_spec(body, max_session_s=self.cfg.max_session_s)
        with self._lock:
            if spec.session_id in self.sessions:
                raise ConflictError("a session with this id already exists")
            open_sessions = sum(1 for s in self.sessions.values() if s.state not in TERMINAL)
            if open_sessions >= self.cfg.max_sessions_pending:
                raise UnavailableError("too many open call sessions; try again shortly", code="too_many_sessions")
            block = self.engine.policy_block_reason()
            if block:
                raise UnavailableError(block[1], code=block[0])
            token = secrets.token_urlsafe(32)
            s = CallSession(spec=spec, join_hash=hashlib.sha256(token.encode()).hexdigest(),
                            created_at=time.time())
            self.sessions[spec.session_id] = s
        self.emit(s, {"type": "session.created", "agent_id": spec.agent_id, "agent_version": spec.agent_version,
                      "tools": [t.name for t in spec.tools], "record": spec.record})
        self._ensure_loading()
        return {**s.view(), "join_token": token,
                "upstream_path": f"/v1/call/sessions/{spec.session_id}/ws?join={token}",
                "engine": self.engine.state}

    def get(self, sid: str) -> dict:
        return self._session(sid).view()

    def list(self) -> list[dict]:
        return [s.view() for s in sorted(self.sessions.values(), key=lambda x: x.created_at, reverse=True)]

    def active_sessions(self) -> int:
        return sum(1 for s in self.sessions.values() if s.state in ("queued", "loading", "live", "ending"))

    def end(self, sid: str, reason: str) -> dict:
        s = self._session(sid)
        if s.state in TERMINAL:
            return s.view()
        self._request_end(sid, reason)
        # wait briefly so the caller gets a final state in the response
        deadline = time.time() + 20
        while s.state not in TERMINAL and time.time() < deadline:
            time.sleep(0.1)
        return s.view()

    def _request_end(self, sid: str, reason: str) -> None:
        s = self.sessions.get(sid)
        if s is None or s.state in TERMINAL:
            return
        s.end_reason = s.end_reason or reason
        if not s.end_requested.is_set():
            s.end_asked_at = time.time()
        s.end_requested.set()
        s.client_attached.set()
        if s.engine_ws is not None:
            try:
                s.engine_ws.send_text(json.dumps({"type": "session.end", "reason": reason}))
            except (OSError, WSClosed):
                pass
        if s.state in ("created", "queued", "waiting") and self.live != sid:
            self._finish(s, reason)
        self._wake.set()

    def inject(self, sid: str, body: Any) -> dict:
        s = self._session(sid)
        if s.state in TERMINAL:
            raise ConflictError("the call has ended")
        event = v.injected_event(body)
        return self.emit(s, event)

    def tool_result(self, sid: str, body: Any) -> dict:
        s = self._session(sid)
        call_id, output, ok, extra = v.tool_result(body)
        pending = s.tool_calls.get(call_id)
        if pending is None:
            raise NotFoundError("no such tool call in this session")
        if pending.get("done"):
            raise ConflictError("this tool call already has a result")
        pending["done"] = True
        latency = round((time.time() - pending["at"]) * 1000)
        s.tools_done += 1
        if s.engine_ws is not None:
            try:
                s.engine_ws.send_text(json.dumps({"type": "tool.result", "call_id": call_id, "output": output}))
            except (OSError, WSClosed):
                pass
        ev = self.emit(s, {"type": "tool.result", "call_id": call_id, "name": pending["name"], "ok": ok,
                           "latency_ms": latency, **{k: val for k, val in extra.items() if k not in (
                               "type", "seq", "session_id", "call_id")}})
        if self.metrics:
            self.metrics.emit("realtime.latency", service="gx-call", session_id=sid, stage="tool", ms=latency,
                              outcome="ok" if ok else "failed")
        return {"accepted": True, "latency_ms": latency, "seq": ev["seq"]}

    # ---------------------------------------------------------- recording --
    def recording_path(self, sid: str, track: str) -> Path:
        s = self._session(sid)
        if track not in ("caller", "agent"):
            raise ValidationError("track must be caller or agent")
        if s.state not in TERMINAL or not s.recording or not s.recording.get("ready"):
            raise ConflictError("no finished recording for this call")
        path = self.cfg.recordings_dir / sid / f"{track}.wav"
        if not path.is_file():
            raise NotFoundError("recording not available")
        return path

    def delete_recording(self, sid: str) -> dict:
        s = self._session(sid)
        shutil.rmtree(self.cfg.recordings_dir / sid, ignore_errors=True)
        if s.recording:
            s.recording = {**s.recording, "ready": False, "deleted": True}
        return {"deleted": True}

    def _purge_orphan_recordings(self) -> None:
        cutoff = time.time() - 24 * 3600
        for d in self.cfg.recordings_dir.glob("call_*"):
            try:
                if d.stat().st_mtime < cutoff or d.name not in self.sessions:
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                pass

    # ------------------------------------------------------------- join --
    def check_join(self, sid: str, header_session: str | None, token: str) -> CallSession:
        s = self._session(sid)
        if header_session is not None and header_session != sid:
            raise ValidationError("X-GX-Session does not match the path")
        digest = hashlib.sha256(token.encode()).hexdigest()
        if not token or not hmac.compare_digest(digest, s.join_hash):
            raise ValidationError("invalid join token", code="invalid_join")
        now = time.time()
        if s.state in TERMINAL or s.end_requested.is_set():
            raise ConflictError("the call has ended", code="session_ended")
        if s.client is not None:
            raise ConflictError("the call is already connected", code="session_busy")
        if s.joins == 0 and now - s.created_at > self.cfg.join_window_s:
            raise ConflictError("the call was not joined in time", code="join_expired")
        if s.joins > 0 and (s.last_disconnect is None or now - s.last_disconnect > self.cfg.rejoin_window_s):
            raise ConflictError("the call can no longer be resumed", code="rejoin_expired")
        return s

    def attach(self, s: CallSession, ws: WebSocket) -> None:
        """Runs in the HTTP handler thread for as long as the caller is connected."""
        rejoin = s.joins > 0
        s.joins += 1
        s.client = ws
        s.joined_at = s.joined_at or time.time()
        if self.metrics:
            self.metrics.emit("realtime.session", service="gx-call", session_id=s.id, outcome="ok",
                              disposition="joined" if not rejoin else "rejoined")
        self.emit(s, {"type": "session.hello", "protocol": "gx-call.v1", "rejoined": rejoin,
                      "input": {"encoding": "pcm_s16le", "sample_rate": INPUT_RATE, "channels": 1},
                      "output": {"encoding": "pcm_s16le", "sample_rate": OUTPUT_RATE, "channels": 1},
                      "voice": self.cfg.model.voice, "text_input": False,
                      "agent": {"agent_id": s.spec.agent_id, "version": s.spec.agent_version,
                                "name": s.spec.agent_name}})
        # replay what happened before this connection (status, earlier transcript on rejoin)
        if not rejoin:
            with self._lock:
                if s.state == "created":
                    s.state = "queued"
                if s.id not in self.queue and s.state in ("queued", "waiting", "loading"):
                    self.queue.append(s.id)
            self._announce_queue()
            if self.load_wait and s.state == "waiting":
                self.emit(s, {"type": "session.status", "state": "waiting",
                              "detail": self.load_wait.get("reason"), "code": self.load_wait.get("code")})
        else:
            self.emit(s, {"type": "session.status", "state": s.state, "detail": "reconnected"})
        s.client_attached.set()
        self._wake.set()
        threading.Thread(target=self._pinger, args=(ws,), name=f"ping-{s.id[-6:]}", daemon=True).start()
        try:
            while True:
                self._client_message(s, ws.recv())
        except (WSClosed, WSProtocolError, OSError, UnicodeDecodeError) as exc:
            log.info("caller socket for %s closed: %s", s.id, exc)
        finally:
            s.client = None
            s.last_disconnect = time.time()
            s.client_attached.clear()
            if s.state not in TERMINAL and not s.end_requested.is_set():
                self.emit(s, {"type": "session.status", "state": s.state, "detail": "caller disconnected"},
                          to_client=False)
                if self.cfg.rejoin_window_s <= 0:
                    self._request_end(s.id, "caller_disconnected")
            try:
                ws.close(1000, "call ended" if s.end_requested.is_set() else "")
            except OSError:
                pass

    @staticmethod
    def _pinger(ws: WebSocket, every: float = 20.0) -> None:
        """Keep the tunnel (idle timeout 120 s) and NATs alive."""
        while not ws.closed:
            for _ in range(int(every)):
                if ws.closed:
                    return
                time.sleep(1)
            try:
                ws.ping()
            except (OSError, WSClosed):
                return

    def _client_message(self, s: CallSession, msg: Message) -> None:
        if not msg.is_text:
            if s.state != "live" or s.engine_ws is None:
                return  # audio before the model is ready is dropped (the client is told the state)
            s.audio_in_bytes += len(msg.data)
            if s.recording is not None and s.recording.get("caller_fh"):
                s.recording["caller_fh"].write(msg.data)
            try:
                s.engine_ws.send_binary(msg.data)
            except (OSError, WSClosed):
                pass
            return
        try:
            body = json.loads(msg.text())
        except ValueError:
            return
        kind = body.get("type") if isinstance(body, dict) else None
        if kind not in CLIENT_TEXT_TYPES:
            self.emit(s, {"type": "error", "code": "unknown_message", "message": f"unknown message type {kind!r}",
                          "fatal": False})
            return
        if kind == "session.end":
            self._request_end(s.id, "caller_ended")
        elif kind == "ping":
            try:
                s.client.send_text(json.dumps({"type": "pong", "t": round(time.time() * 1000)}))
            except (OSError, WSClosed, AttributeError):
                pass
        elif kind == "input.text":
            self.emit(s, {"type": "error", "code": "text_input_unsupported", "fatal": False,
                          "message": "NemotronLabs VoiceChat only takes caller audio; typed turns are "
                                     "converted to speech by the Control Center when a voice service is available"})
        elif kind == "client.info":
            info = {k: body.get(k) for k in ("user_agent", "sample_rate", "platform") if isinstance(body.get(k), str)}
            self.emit(s, {"type": "client.info", **{k: val[:120] for k, val in info.items()}}, to_client=False)

    def _announce_queue(self) -> None:
        with self._lock:
            order = list(self.queue)
        for pos, sid in enumerate(order, start=1):
            s = self.sessions.get(sid)
            if s is None:
                continue
            if self.live is None and pos == 1:
                continue
            ahead = pos - 1 + (1 if self.live else 0)
            self._set_state(s, "queued", f"another call is using the voice model ({ahead} ahead)", position=ahead)

    # ------------------------------------------------------- dispatcher --
    def _ensure_loading(self) -> None:
        """Start loading the model in the background (a call is coming)."""
        with self._lock:
            if self.engine.state == READY or (self._loader and self._loader.is_alive()):
                return
            if self.engine.policy_block_reason():
                return
            self._loader = threading.Thread(target=self._load_worker, name="gx-call-load", daemon=True)
            self._loader.start()

    def _load_worker(self) -> None:
        waited_since = None
        while not self._stop.is_set():
            try:
                self.engine.ensure_loaded(on_status=self._loading_status)
                self.load_wait = None
                self._wake.set()
                return
            except ResourceWait as wait:
                waited_since = waited_since or time.time()
                self.load_wait = {"code": wait.code, "reason": wait.reason, "since": waited_since}
                for s in self._waiting_sessions():
                    self._set_state(s, "waiting", wait.reason, code=wait.code)
                if time.time() - waited_since > self.cfg.resource_wait_s or not self._waiting_sessions():
                    for s in self._waiting_sessions():
                        self._fail_session(s, wait.code, f"The voice model could not start: {wait.reason}")
                    return
                self._stop.wait(self.cfg.resource_retry_s)
            except EngineError as exc:
                self.load_wait = {"code": exc.code, "reason": exc.message, "since": time.time()}
                for s in self._waiting_sessions():
                    self._fail_session(s, exc.code, exc.message)
                return

    def _waiting_sessions(self) -> list[CallSession]:
        return [s for s in self.sessions.values() if s.state in ("created", "queued", "waiting", "loading")]

    def _loading_status(self, detail: str) -> None:
        for s in self._waiting_sessions():
            if s.client is not None and (s.state != "loading" or int(time.time()) % 5 == 0):
                self._set_state(s, "loading", detail)

    def _fail_session(self, s: CallSession, code: str, message: str) -> None:
        s.error = {"code": code, "message": message}
        self.emit(s, {"type": "error", "code": code, "message": message, "fatal": True})
        self._finish(s, code)

    def _dispatcher(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(1.0)
            self._wake.clear()
            try:
                self._dispatch_once()
            except Exception:  # noqa: BLE001 - the dispatcher must survive
                log.exception("dispatcher error")

    def _dispatch_once(self) -> None:
        with self._lock:
            # drop queued sessions that ended or whose caller never came back
            for sid in list(self.queue):
                s = self.sessions.get(sid)
                if s is None or s.state in TERMINAL:
                    self.queue.remove(sid)
            if self.live is not None or not self.queue:
                return
            sid = self.queue[0]
            s = self.sessions[sid]
        if s.client is None:
            return  # wait for the caller (rejoin window handled by the reaper)
        block = self.engine.policy_block_reason()
        if block:
            self._set_state(s, "waiting", block[1], code=block[0])
            return
        if self.engine.state != READY:
            self._ensure_loading()
            if self.engine.state in ("loading",):
                self._set_state(s, "loading", self.engine.detail)
            return
        with self._lock:
            if self.live is not None or not self.queue or self.queue[0] != sid:
                return
            self.queue.popleft()
            self.live = sid
        threading.Thread(target=self._run_call, args=(s,), name=f"call-{sid[-6:]}", daemon=True).start()
        self._announce_queue()

    # --------------------------------------------------------------- call --
    def _run_call(self, s: CallSession) -> None:
        eng = None
        try:
            self._set_state(s, "starting", "preparing the agent")
            eng = self.engine_connect("127.0.0.1", self.cfg.engine_port, "/v1/engine/stream",
                                      {"Authorization": f"Bearer {self.engine.key}"}, timeout=15)
            s.engine_ws = eng
            self.engine.session_live = True
            eng.send_text(json.dumps(s.spec.engine_config(self.cfg.tool_timeout_s)))
            if s.spec.record:
                self._open_recording(s)
            while True:
                msg = eng.recv()
                if not msg.is_text:
                    s.audio_out_bytes += len(msg.data)
                    if s.recording is not None and s.recording.get("agent_fh"):
                        s.recording["agent_fh"].write(msg.data)
                    if s.client is not None:
                        try:
                            s.client.send_binary(msg.data)
                        except (OSError, WSClosed):
                            pass
                    continue
                event = json.loads(msg.text())
                kind = event.get("type")
                if kind == "session.ready":
                    s.live_at = time.time()
                    self.engine.touch()
                    self._set_state(s, "live", "the agent is listening", prefill_ms=event.get("prefill_ms"),
                                    voice=event.get("voice"), tools=event.get("tools"))
                    self.emit(s, {"type": "session.ready", **{k: val for k, val in event.items() if k != "type"}})
                    continue
                if kind == "tool.call":
                    s.tool_calls[event["call_id"]] = {"name": event.get("name"), "at": time.time(), "done": False}
                    if not event.get("known", True):
                        s.tool_calls[event["call_id"]]["done"] = True
                if kind == "session.ended":
                    s.summary = event.get("summary")
                    break
                self.emit(s, event)
                if kind == "error" and event.get("fatal"):
                    s.error = {"code": event.get("code"), "message": event.get("message")}
        except ConnectionRefusedError as exc:
            log.error("engine refused the call: %s", exc)
            s.error = {"code": "engine_unavailable", "message": "the voice model is not accepting calls right now"}
            self.emit(s, {"type": "error", **s.error, "fatal": True})
        except (WSClosed, WSProtocolError, OSError, ValueError) as exc:
            log.warning("engine stream for %s failed: %s", s.id, exc)
            if not s.end_requested.is_set():
                s.error = {"code": "engine_disconnected", "message": "the voice model connection was lost"}
                self.emit(s, {"type": "error", **s.error, "fatal": True})
        finally:
            if eng is not None:
                try:
                    eng.close(1000)
                except OSError:
                    pass
            s.engine_ws = None
            self.engine.session_live = False
            self.engine.touch()
            with self._lock:
                self.live = None
            self._finish(s, s.end_reason or ("engine_error" if s.error else "completed"))
            self._wake.set()

    def _open_recording(self, s: CallSession) -> None:
        d = self.cfg.recordings_dir / s.id
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        s.recording = {"caller_fh": open(d / "caller.pcm", "wb"), "agent_fh": open(d / "agent.pcm", "wb"),  # noqa: SIM115
                       "ready": False}
        self.emit(s, {"type": "recording.started", "tracks": ["caller", "agent"]})

    def _close_recording(self, s: CallSession) -> None:
        rec = s.recording
        if not rec or "caller_fh" not in rec:
            return
        d = self.cfg.recordings_dir / s.id
        out = {"ready": False}
        for track, rate in (("caller", INPUT_RATE), ("agent", OUTPUT_RATE)):
            fh = rec.pop(f"{track}_fh")
            fh.close()
            raw = d / f"{track}.pcm"
            size = raw.stat().st_size
            with open(d / f"{track}.wav", "wb") as wav, open(raw, "rb") as src:
                wav.write(_wav_header(size, rate))
                shutil.copyfileobj(src, wav, 1 << 20)
            os.chmod(d / f"{track}.wav", 0o600)
            raw.unlink()
            out[f"{track}_seconds"] = round(size / 2 / rate, 2)
        out["ready"] = True
        s.recording = out
        self.emit(s, {"type": "recording.ready", **out})

    def _finish(self, s: CallSession, reason: str) -> None:
        if s.state in TERMINAL:
            return
        s.end_requested.set()
        try:
            self._close_recording(s)
        except OSError:
            log.exception("could not finalise the recording of %s", s.id)
        s.ended_at = time.time()
        s.end_reason = s.end_reason or reason
        s.disposition = _disposition(s)
        with self._lock:
            if s.id in self.queue:
                self.queue.remove(s.id)
        s.state = "ended"
        self.emit(s, {"type": "session.ended", "reason": s.end_reason, "disposition": s.disposition,
                      "summary": s.summary, "error": s.error, "duration_s": s.view()["duration_s"]})
        if s.client is not None:
            try:
                s.client.close(1000, "call ended")
            except OSError:
                pass
        if self.metrics:
            summary = s.summary or {}
            self.metrics.emit("realtime.session", service="gx-call", session_id=s.id, disposition=s.disposition,
                              outcome="failed" if s.error else "ok", bytes_in=s.audio_in_bytes,
                              bytes_out=s.audio_out_bytes, duration_ms=round(s.view()["duration_s"] * 1000))
            if summary.get("first_audio_wall_ms") is not None:
                self.metrics.emit("realtime.latency", service="gx-call", session_id=s.id, stage="first_audio",
                                  ms=summary["first_audio_wall_ms"])
            turn = summary.get("turn_latency_wall_ms") or {}
            if turn.get("mean") is not None:
                self.metrics.emit("realtime.latency", service="gx-call", session_id=s.id, stage="turn",
                                  ms=turn["mean"])
        self._wake.set()

    # ------------------------------------------------------------ policy --
    def _reaper(self) -> None:
        while not self._stop.wait(5):
            try:
                self.reap_once()
            except Exception:  # noqa: BLE001
                log.exception("reaper error")

    def reap_once(self) -> str | None:
        now = time.time()
        # forget ended sessions after 30 minutes (the Control Center has persisted them)
        for sid, s in list(self.sessions.items()):
            if s.state in TERMINAL and s.ended_at and now - s.ended_at > 1800:
                shutil.rmtree(self.cfg.recordings_dir / sid, ignore_errors=True)
                self.sessions.pop(sid, None)
            elif s.state == "created" and now - s.created_at > self.cfg.join_window_s:
                self._finish(s, "join_expired")
            elif (s.state in ("queued", "waiting", "loading") and s.client is None and s.last_disconnect
                  and now - s.last_disconnect > self.cfg.rejoin_window_s):
                self._finish(s, "caller_disconnected")
            elif s.state not in TERMINAL and now - s.created_at > self.cfg.session_ttl_s:
                self._request_end(sid, "session_ttl")
            elif s.state == "live":
                if s.live_at and now > s.live_at + s.spec.max_duration_s:
                    self._request_end(sid, "max_duration")
                if s.client is None and s.last_disconnect and now - s.last_disconnect > self.cfg.rejoin_window_s:
                    self._request_end(sid, "caller_disconnected")
            if (s.state not in TERMINAL and s.end_asked_at and now - s.end_asked_at > 30
                    and s.engine_ws is not None):
                log.warning("engine did not close call %s within 30 s; closing the stream", sid)
                s.engine_ws.close(1001, "timeout")
        block = self.engine.policy_block_reason()
        if block and block[0] == "gx_max_active":
            for sid in [sid for sid, s in self.sessions.items() if s.state not in TERMINAL]:
                s = self.sessions[sid]
                self.emit(s, {"type": "error", "code": "gx_max_active", "message": block[1], "fatal": True})
                s.error = {"code": "gx_max_active", "message": block[1]}
                self._request_end(sid, "gx_max")
            if self.engine.state != "unloaded" or self.engine.docker.exists(self.cfg.engine_container):
                self._wait_idle(30)
                self.engine.unload("gx-max drain", kind="gxmax")
                return "gxmax"
            return None
        if self.engine.state != READY:
            if self.engine.state == "failed" and self._waiting_sessions():
                self._ensure_loading()
            return None
        if not self.engine.docker.running(self.cfg.engine_container):
            self.engine.reconcile()
            return "lost"
        busy = self.live is not None or bool(self.queue)
        if block and block[0] == "maintenance" and not busy:
            self.engine.unload("maintenance mode", kind="maintenance")
            return "maintenance"
        idle = now - self.engine.last_activity
        if self.cfg.idle_unload_s and idle > self.cfg.idle_unload_s and not busy:
            if self.engine.pin_honoured():
                return "pinned"
            self.engine.unload(f"idle for {int(idle)} s", kind="idle")
            return "idle"
        return None

    def _wait_idle(self, seconds: float) -> None:
        deadline = time.time() + seconds
        while self.live is not None and time.time() < deadline:
            time.sleep(0.5)

    def load(self) -> dict:
        block = self.engine.policy_block_reason()
        if block:
            raise UnavailableError(block[1], code=block[0])
        try:
            self.engine.ensure_loaded()
        except ResourceWait as wait:
            raise UnavailableError(wait.reason, code=wait.code) from wait
        return self.engine.snapshot()

    def unload(self, *, if_idle: bool, reason: str = "manual") -> dict:
        """``if_idle`` (other tenants making room) refuses while calls are live or
        queued, or a pin is honoured. Otherwise live calls are ended first."""
        busy = self.live is not None or bool(self.queue)
        if if_idle:
            if busy:
                raise ConflictError("a call is live or waiting; gx-call stays loaded")
            if self.engine.pin_honoured():
                raise ConflictError("gx-call is pinned")
            if self.engine.state != READY and not self.engine.docker.exists(self.cfg.engine_container):
                return {"state": "unloaded", "noop": True, "reason": f"engine is {self.engine.state}"}
        else:
            for sid, s in list(self.sessions.items()):
                if s.state not in TERMINAL:
                    self._request_end(sid, reason if reason in ("gxmax", "maintenance") else "unloaded")
            self._wait_idle(30)
        kind = reason if reason in ("gxmax", "maintenance", "evicted", "manual") else "manual"
        if if_idle:
            kind = "evicted"
        info = self.engine.unload("unloaded while idle to make room on gx10-02" if if_idle else f"requested ({reason})",
                                  kind=kind)
        return {"state": "unloaded", **info}

    # ------------------------------------------------------------- views --
    def health(self) -> dict:
        eng = self.engine
        state = eng.state
        if state == READY and self.live is not None:
            state = "busy"
        block = eng.policy_block_reason()
        waiting = None
        if self.load_wait:
            waiting = {"code": self.load_wait.get("code"), "reason": self.load_wait.get("reason")}
        return {"status": "ok", "service": "gx-call", "state": state, "busy": self.live is not None,
                "pinned": eng.pinned(), "active_sessions": self.active_sessions(),
                "queue": len(self.queue), "idle_seconds": round(time.time() - eng.last_activity, 1),
                "idle_unload_after_s": self.cfg.idle_unload_s,
                "blocked_by": block[1] if block else None, "waiting": waiting,
                "memory": eng.memory_view()}

    def model_info(self) -> dict:
        return {
            "alias": "gx-call", "task": "realtime-voice-agent", "node": "gx10-02",
            "identity": self.cfg.model.as_dict(),
            "runtime": {"name": "NeMo StreamingS2SPipeline (native PyTorch engine) with GB10 patches",
                        "image": self.cfg.model.image, "chunk_ms": round(self.cfg.engine_chunk_s * 1000),
                        "concurrency": 1},
            "capabilities": {
                "duplex_audio": True, "input": {"encoding": "pcm_s16le", "sample_rate": INPUT_RATE},
                "output": {"encoding": "pcm_s16le", "sample_rate": OUTPUT_RATE},
                "voices": [self.cfg.model.voice], "languages": ["en"], "tool_calling": True,
                "max_tools_recommended": 5, "barge_in": True, "caller_transcription": True,
                "agent_transcript": True, "text_input": False, "recording": True,
                "max_call_seconds": self.cfg.max_session_s,
            },
            "engine": self.engine.snapshot(),
            "memory": meminfo(),
            "queue": {"live": self.live, "queued": list(self.queue)},
            "load_wait": self.load_wait,
        }


def _disposition(s: CallSession) -> str:
    reason = s.end_reason or ""
    if s.error and s.error.get("code") == "gx_max_active":
        return "preempted"
    if s.error:
        return "failed"
    if reason in ("max_duration", "session_ttl"):
        return "timeout"
    if reason in ("join_expired",) or (s.live_at is None and reason in ("caller_disconnected", "caller_ended")):
        return "abandoned"
    if reason == "transferred":
        return "transferred"
    if reason in ("caller_disconnected",):
        return "dropped"
    return "completed"
