"""Live sessions: admission, the realtime relay, the tool bridge and lifecycle policy.

One session may be active at a time (one model instance with one streaming
context on gx10-02). A session is created by the Control Center, attached by
the tunnelled client WebSocket (join token in the fixed upstream path), and
relayed to the engine over a loopback WebSocket that lives as long as the
session, so a client can reconnect without losing the conversation.

Nothing in this module writes audio, images or transcripts anywhere.
"""

from __future__ import annotations

import hmac
import json
import logging
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any
from collections.abc import Callable

from . import PROTOCOL, __version__
from . import protocol as proto
from . import tools as toolspec
from .config import Config
from .engine import LOADING, READY, UNLOADED, UNLOADING, WAITING, EngineController, meminfo
from .errors import (ConflictError, EngineError, ForbiddenError, GoneError, NotFoundError, ResourceWait,
                     UnavailableError, ValidationError)

log = logging.getLogger("gx_live.service")

ENDED = "ended"
ENGINE_EVENT_PASSTHROUGH = frozenset({
    "session.ready", "input.speech.started", "input.speech.stopped", "transcript.user", "response.started",
    "transcript.assistant.delta", "response.done", "response.interrupted", "metrics", "error", "pong",
    "notice",
})


def _now_ms(start: float) -> int:
    return int((time.time() - start) * 1000)


@dataclass
class ToolCall:
    call_id: str
    name: str
    arguments: dict
    created_at: float
    deadline: float
    state: str = "queued"          # queued -> claimed -> done
    claimed_at: float | None = None
    result: dict | None = None
    progress: list[dict] = field(default_factory=list)

    def public(self) -> dict:
        return {"call_id": self.call_id, "name": self.name, "arguments": self.arguments,
                "created_at": self.created_at, "deadline": self.deadline, "state": self.state}


@dataclass
class Session:
    id: str
    owner: str
    config: dict
    created_at: float
    expires_at: float
    join_token: str
    state: str = "created"         # created | connected | detached | ended
    client: Any = None
    client_gen: int = 0
    engine: Any = None
    engine_ready: bool = False
    detached_at: float | None = None
    first_attach_at: float | None = None
    ended_at: float | None = None
    end_reason: str | None = None
    load_ms: int | None = None
    waited_ms: int = 0
    stats: dict = field(default_factory=lambda: {
        "turns": 0, "responses": 0, "interrupted": 0, "failed_responses": 0, "text_inputs": 0,
        "speech_segments": 0, "first_audio_ms": [], "first_text_ms": [], "turn_ms": [], "interrupt_ms": [],
        "tools": [], "errors": [], "bytes_in": 0, "bytes_out": 0, "mic_ms": 0, "camera_frames": 0,
        "camera_dropped": 0, "audio_out_ms": 0, "connects": 0})
    tool_calls: dict[str, ToolCall] = field(default_factory=dict)
    cond: threading.Condition = field(default_factory=threading.Condition)
    lock: threading.RLock = field(default_factory=threading.RLock)
    camera_times: deque = field(default_factory=lambda: deque(maxlen=8))

    @property
    def active(self) -> bool:
        return self.state != ENDED

    def summary(self) -> dict:
        s = self.stats

        def stat(values: list) -> dict | None:
            if not values:
                return None
            ordered = sorted(values)
            return {"count": len(values), "min": ordered[0], "median": ordered[len(values) // 2],
                    "max": ordered[-1]}

        end = self.ended_at or time.time()
        return {
            "session_id": self.id, "owner": self.owner, "state": self.state, "created_at": self.created_at,
            "first_attach_at": self.first_attach_at, "ended_at": self.ended_at, "end_reason": self.end_reason,
            "duration_s": round(end - (self.first_attach_at or self.created_at), 1),
            "expires_at": self.expires_at, "config": {k: v for k, v in self.config.items() if k != "instructions"},
            "has_instructions": bool(self.config.get("instructions")),
            "model_load_ms": self.load_ms, "model_wait_ms": self.waited_ms,
            "turns": s["turns"], "responses": s["responses"], "interrupted": s["interrupted"],
            "failed_responses": s["failed_responses"], "text_inputs": s["text_inputs"],
            "speech_segments": s["speech_segments"],
            "latency": {"first_audio_ms": stat(s["first_audio_ms"]), "first_text_ms": stat(s["first_text_ms"]),
                        "turn_ms": stat(s["turn_ms"]), "interrupt_ms": stat(s["interrupt_ms"])},
            "tools": list(s["tools"]), "errors": list(s["errors"][-20:]),
            "media": {"mic_seconds": round(s["mic_ms"] / 1000, 1), "camera_frames": s["camera_frames"],
                      "camera_frames_dropped": s["camera_dropped"],
                      "assistant_audio_seconds": round(s["audio_out_ms"] / 1000, 1)},
            "transport": {"bytes_in": s["bytes_in"], "bytes_out": s["bytes_out"], "connects": s["connects"]},
            "pending_tool_calls": sum(1 for c in self.tool_calls.values() if c.state != "done"),
        }


class LiveService:
    def __init__(self, cfg: Config, engine: EngineController, *, metrics: Any = None,
                 connect: Callable[..., Any] | None = None, clock: Callable[[], float] = time.time) -> None:
        self.cfg = cfg
        self.engine = engine
        self.metrics = metrics
        self.clock = clock
        if connect is None:
            import sys
            if str(cfg.common_dir) not in sys.path:
                sys.path.insert(0, str(cfg.common_dir))
            from gxcommon import rtws  # noqa: PLC0415
            connect = rtws.connect
        self._connect = connect
        self._lock = threading.RLock()
        self.sessions: dict[str, Session] = {}
        self._stop = threading.Event()
        self._loader: threading.Thread | None = None
        self._last_reconcile = 0.0
        self._last_ping = 0.0
        self.engine.subscribe(self._on_engine_state)

    # ================================================================ lifecycle
    def start(self) -> None:
        self.engine.reconcile()
        self._monitor = threading.Thread(target=self._monitor_loop, name="gx-live-monitor", daemon=True)
        self._monitor.start()

    def stop(self) -> None:
        self._stop.set()
        for sess in list(self.sessions.values()):
            if sess.active:
                self.end(sess.id, "shutdown", close_code=1001)

    def _metric(self, event: str, **fields: Any) -> None:
        if self.metrics is not None:
            try:
                self.metrics.emit(event, **fields)
            except Exception:  # noqa: BLE001
                log.exception("metric emit failed")

    # ================================================================== views
    def active_session(self) -> Session | None:
        with self._lock:
            return next((s for s in self.sessions.values() if s.active), None)

    def public_state(self) -> str:
        st = self.engine.state
        sess = self.active_session()
        if st == READY and sess is not None and sess.engine_ready:
            return "busy"
        return st

    def health(self) -> dict:
        sess = self.active_session()
        waiting = self.engine.waiting if self.engine.state == WAITING else None
        return {"status": "ok", "service": "gx-live", "version": __version__, "protocol": PROTOCOL,
                "state": self.public_state(), "engine": self.engine.state,
                "busy": sess is not None, "pinned": self.engine.pinned(),
                "active_sessions": 1 if sess else 0, "active_jobs": 0, "queue": 0,
                "waiting": waiting, "idle_seconds": round(self.clock() - self.engine.last_activity, 1),
                "memory": self.engine.memory_view()}

    def model_info(self) -> dict:
        sess = self.active_session()
        return {
            "alias": "gx-live", "task": "realtime-omni", "node": "gx10-02", "protocol": PROTOCOL,
            "identity": self.cfg.model.as_dict(),
            "capabilities": {
                "audio_in": True, "audio_out": True, "full_duplex_transport": True, "interruption": True,
                "vision": True, "camera_frames_per_s": proto.CAMERA_FPS, "text_chat": True,
                "tools": list(toolspec.NAMES), "delegation_models": list(toolspec.DELEGATE_MODELS),
                "languages": ["en", "zh"], "concurrent_sessions": 1},
            "limits": proto.LIMITS,
            "engine": self.engine.snapshot(),
            "memory": {**meminfo(), "peers": self.engine.peers.snapshot()},
            "session": {"active": sess is not None, "state": sess.state if sess else None,
                        "since": sess.created_at if sess else None},
            "policy": {
                "workload": self.cfg.workload, "workload_class": "medium",
                "estimated_gib": self.cfg.engine_estimate_gib, "reserve_gib": self.cfg.reserve_gib,
                "idle_unload_s": self.cfg.idle_unload_s, "resource_wait_s": self.cfg.resource_wait_s,
                "pinned": self.engine.pinned(), "blocked_by": (self.engine.policy_block() or (None, None))[1],
                "gx_max": "never loads while gx-max holds gx10-02; live sessions end and the engine unloads "
                          "(verified) as soon as a gx-max hold appears",
                "maintenance": "no new sessions or loads; an idle engine is unloaded"},
        }

    # =============================================================== sessions
    def create(self, body: Any) -> dict:
        if not isinstance(body, dict):
            raise ValidationError("request body must be a JSON object")
        sid = body.get("session_id")
        owner = body.get("owner")
        if not isinstance(sid, str) or not proto.SESSION_ID.match(sid):
            raise ValidationError("session_id must look like live_<32 hex>")
        if not isinstance(owner, str) or not proto.OWNER.match(owner):
            raise ValidationError("owner must be a 16-hex owner hash")
        config = proto.normalise_config(body.get("config"))
        ttl = body.get("ttl_s", self.cfg.session_max_s)
        if not isinstance(ttl, int) or isinstance(ttl, bool) or not 60 <= ttl <= self.cfg.session_max_s:
            raise ValidationError(f"ttl_s must be 60-{self.cfg.session_max_s}")
        block = self.engine.policy_block()
        if block:
            raise UnavailableError(block[1], code=block[0], extra={"waiting": {"code": block[0], "reason": block[1]}})
        now = self.clock()
        with self._lock:
            if sid in self.sessions:
                raise ConflictError("a session with this id already exists", code="duplicate_session")
            current = self.active_session()
            if current is not None:
                raise ConflictError("gx-live is already in a live session; only one session can run at a time",
                                    code="session_busy", retryable=True)
            self._prune(now)
            sess = Session(id=sid, owner=owner, config=config, created_at=now, expires_at=now + ttl,
                           join_token=secrets.token_urlsafe(24))
            self.sessions[sid] = sess
        log.info("session %s created (owner %s)", sid, owner)
        self._ensure_engine_async()
        state = self.engine.public_state()
        return {"session_id": sid, "join_token": sess.join_token, "protocol": PROTOCOL,
                "expires_at": int(sess.expires_at), "model_state": state["state"],
                "waiting": state["waiting"],
                "upstream_path": f"/v1/live/sessions/{sid}/ws?join={sess.join_token}"}

    def _prune(self, now: float) -> None:
        for sid, s in list(self.sessions.items()):
            if s.state == ENDED and s.ended_at and now - s.ended_at > 3600:
                del self.sessions[sid]

    def get(self, sid: str) -> Session:
        if not proto.SESSION_ID.match(sid or ""):
            raise NotFoundError("no such live session")
        sess = self.sessions.get(sid)
        if sess is None:
            raise NotFoundError("no such live session")
        return sess

    def summary(self, sid: str) -> dict:
        return self.get(sid).summary()

    def end(self, sid: str, reason: str = "completed", *, close_code: int = 1000) -> dict:
        sess = self.get(sid)
        with sess.lock:
            if sess.state == ENDED:
                return sess.summary()
            sess.state = ENDED
            sess.ended_at = self.clock()
            sess.end_reason = reason
            client, engine = sess.client, sess.engine
            sess.client = None
            sess.engine = None
            sess.engine_ready = False
        with sess.cond:
            for call in sess.tool_calls.values():
                if call.state != "done":
                    call.state = "done"
                    call.result = {"ok": False, "error": {"code": "session_ended",
                                                          "message": "the session ended"}}
            sess.cond.notify_all()
        if client is not None and not getattr(client, "_gx_counted", False):
            sess.stats["bytes_in"] += getattr(client, "bytes_in", 0)
            sess.stats["bytes_out"] += getattr(client, "bytes_out", 0)
            client._gx_counted = True
        summary = sess.summary()
        self.engine.touch()
        log.info("session %s ended (%s): %s", sid, reason, json.dumps(
            {k: summary[k] for k in ("duration_s", "turns", "responses", "interrupted")}))
        self._metric("realtime.session", service="gx-live", session_id=sid, disposition=reason,
                     outcome="ok" if reason in ("completed", "abandoned", "timeout") else "failed",
                     duration_ms=int(summary["duration_s"] * 1000), owner=sess.owner,
                     bytes_in=summary["transport"]["bytes_in"], bytes_out=summary["transport"]["bytes_out"],
                     turns=summary["turns"])
        if client is not None:
            self._send_json(sess, client, {"type": "session.ended", "reason": reason,
                                           "duration_s": summary["duration_s"], "turns": summary["turns"]})
            _close(client, close_code, reason)
        if engine is not None:
            try:
                engine.send_text(json.dumps({"type": "engine.session.end", "reason": reason}))
            except Exception:  # noqa: BLE001
                log.debug("engine link already closed")
            _close(engine, 1000, "session ended")
        return summary

    # ------------------------------------------------------------- attach --
    def check_join(self, sid: str, token: str) -> Session:
        sess = self.get(sid)
        if not token or not hmac.compare_digest(token.encode(), sess.join_token.encode()):
            raise ForbiddenError("invalid join token for this session") from None
        if sess.state == ENDED:
            raise GoneError("this live session has ended")
        if self.clock() > sess.expires_at:
            self.end(sid, "timeout")
            raise GoneError("this live session has expired")
        return sess

    def serve_client(self, sess: Session, ws: Any) -> None:
        """Run the client side of a session on the handler thread until it closes."""
        with sess.lock:
            old = sess.client
            sess.client = ws
            sess.client_gen += 1
            gen = sess.client_gen
            sess.state = "connected"
            sess.detached_at = None
            sess.first_attach_at = sess.first_attach_at or self.clock()
            sess.stats["connects"] += 1
        if old is not None:
            _close(old, 4409, "another connection took over this session")
        state = self.engine.public_state()
        self._send_json(sess, ws, {
            "type": "session.created", "protocol": PROTOCOL, "session_id": sess.id,
            "model": {"alias": "gx-live", "repository": self.cfg.model.repository,
                      "revision": self.cfg.model.revision},
            "config": {k: v for k, v in sess.config.items() if k != "instructions"},
            "tools": list(toolspec.NAMES) if sess.config.get("tools") else [],
            "limits": proto.LIMITS, "resumed": gen > 1})
        self._send_json(sess, ws, {"type": "model.state", **state})
        if sess.engine is not None:
            # The engine gates microphone audio on client_attached, and the
            # engine.session.start payload was built when the link opened. On a
            # warm model that happens BEFORE the browser's WebSocket arrives
            # (create -> link -> connect), so the engine was told "no client"
            # and would drop every microphone frame for the whole session
            # unless it is told now. Found by the 2026-09-17 GPU acceptance.
            self._engine_send(sess, {"type": "client.attached"})
        if sess.engine_ready:
            self._send_json(sess, ws, {"type": "session.ready", "load_ms": sess.load_ms, "resumed": True})
        self._ensure_engine_async()
        try:
            self._client_loop(sess, ws)
        finally:
            with sess.lock:
                current = sess.client is ws
                if current:
                    sess.client = None
                    if sess.state != ENDED:
                        sess.state = "detached"
                        sess.detached_at = self.clock()
                if not getattr(ws, "_gx_counted", False):
                    sess.stats["bytes_in"] += getattr(ws, "bytes_in", 0)
                    sess.stats["bytes_out"] += getattr(ws, "bytes_out", 0)
                    ws._gx_counted = True
            if current and sess.state != ENDED:
                self._engine_send(sess, {"type": "client.detached"})
                log.info("session %s: client detached (grace %ss)", sess.id, self.cfg.reconnect_grace_s)

    def _client_loop(self, sess: Session, ws: Any) -> None:
        mic_budget_t = time.monotonic()
        mic_frames = 0
        while not self._stop.is_set():
            msg = ws.recv()
            if msg is None:
                return
            if sess.state == ENDED or sess.client is not ws:
                return
            try:
                if msg.is_text:
                    ev = proto.parse_client_event(msg.text())
                    if ev["type"] == "ping":
                        self._send_json(sess, ws, {"type": "pong", "t": ev["t"], "server_ms": int(time.time() * 1000)})
                        continue
                    if ev["type"] == "session.stop":
                        self.end(sess.id, "completed")
                        return
                    if ev["type"] == "input.text":
                        sess.stats["text_inputs"] += 1
                    if not sess.engine_ready:
                        if ev["type"] in ("input.text", "input.audio.commit"):
                            self._send_json(sess, ws, {"type": "error", "code": "model_not_ready", "fatal": False,
                                                       "message": "gx-live is not ready yet: "
                                                       + (self.engine.state_detail or self.engine.state)})
                        continue
                    self._engine_send(sess, ev)
                    continue
                kind, _payload = proto.check_client_binary(msg.data)
                if kind == proto.KIND_MIC:
                    now = time.monotonic()
                    if now - mic_budget_t >= 1.0:
                        mic_budget_t, mic_frames = now, 0
                    mic_frames += 1
                    if mic_frames > 100:
                        continue  # more than 100 frames/s is not a microphone
                    sess.stats["mic_ms"] += (len(msg.data) - proto.HEADER.size) // 32
                else:
                    now = time.monotonic()
                    recent = [t for t in sess.camera_times if now - t < 1.0]
                    if len(recent) >= proto.CAMERA_FPS:
                        sess.stats["camera_dropped"] += 1
                        continue
                    sess.camera_times.append(now)
                    sess.stats["camera_frames"] += 1
                if sess.engine_ready and sess.engine is not None:
                    try:
                        sess.engine.send_binary(msg.data)
                    except Exception:  # noqa: BLE001
                        log.debug("engine link send failed (media frame dropped)")
            except proto.FrameError as exc:
                self._send_json(sess, ws, {"type": "error", "code": exc.code, "message": exc.message, "fatal": True})
                _close(ws, exc.close_code, exc.message[:100])
                return
            except ValidationError as exc:
                self._send_json(sess, ws, {"type": "error", "code": exc.code, "message": exc.message,
                                           "fatal": False})

    # ------------------------------------------------------------- engine --
    def _ensure_engine_async(self) -> None:
        with self._lock:
            if self._loader is not None and self._loader.is_alive():
                return
            sess = self.active_session()
            if sess is None or sess.engine_ready:
                return
            self._loader = threading.Thread(target=self._engine_worker, args=(sess,), name="gx-live-loader",
                                            daemon=True)
            self._loader.start()

    def _engine_worker(self, sess: Session) -> None:
        waited_since: float | None = None
        load_s: float | None = None
        while sess.active and not self._stop.is_set():
            try:
                load_s = self.engine.ensure_loaded()
                break
            except ResourceWait as wait:
                waited_since = waited_since or self.clock()
                sess.waited_ms = int((self.clock() - waited_since) * 1000)
                if wait.code in ("gx_max_active", "maintenance"):
                    self.end(sess.id, "gx_max" if wait.code == "gx_max_active" else "maintenance",
                             close_code=1001)
                    return
                if self.clock() - waited_since > self.cfg.resource_wait_s:
                    self._fail(sess, wait.code, f"Gave up after {self.cfg.resource_wait_s // 60} min: {wait.reason}")
                    return
                self._stop.wait(self.cfg.resource_retry_s)
            except EngineError as exc:
                self._fail(sess, exc.code, exc.message)
                return
            except Exception:  # noqa: BLE001
                log.exception("engine load crashed")
                self._fail(sess, "internal_error", "the live model could not be started")
                return
        if not sess.active:
            return
        sess.load_ms = int(load_s * 1000) if load_s is not None else None
        try:
            link = self._connect("127.0.0.1", self.cfg.engine_port, "/v1/session",
                                 headers={"Authorization": f"Bearer {self.engine.key}", "X-GX-Session": sess.id},
                                 timeout=10)
        except Exception as exc:  # noqa: BLE001
            log.error("engine link failed: %s", exc)
            self._fail(sess, "engine_unreachable", "the live engine did not accept the session")
            return
        start = {"type": "engine.session.start", "session_id": sess.id, "config": sess.config,
                 "tools": toolspec.DEFINITIONS if sess.config.get("tools") else [],
                 "client_attached": sess.client is not None}
        try:
            link.send_text(json.dumps(start))
        except Exception:  # noqa: BLE001
            self._fail(sess, "engine_unreachable", "the live engine did not accept the session")
            return
        with sess.lock:
            if not sess.active:
                _close(link, 1000, "session ended")
                return
            sess.engine = link
        self._engine_loop(sess, link)

    def _fail(self, sess: Session, code: str, message: str) -> None:
        sess.stats["errors"].append({"code": code, "at": self.clock()})
        client = sess.client
        if client is not None:
            self._send_json(sess, client, {"type": "error", "code": code, "message": message, "fatal": True})
        self._metric("failure", component="gx-live", error_code=code, message=message)
        if sess.active:
            self.end(sess.id, "failed", close_code=1011)

    def _engine_send(self, sess: Session, event: dict) -> None:
        link = sess.engine
        if link is None:
            return
        try:
            link.send_text(json.dumps(event))
        except Exception:  # noqa: BLE001
            log.warning("session %s: engine link send failed", sess.id)

    def _engine_loop(self, sess: Session, link: Any) -> None:
        while not self._stop.is_set():
            msg = link.recv()
            if msg is None:
                break
            self.engine.touch()
            if not msg.is_text:
                client = sess.client
                sess.stats["audio_out_ms"] += (len(msg.data) - proto.HEADER.size) * 1000 // (2 * proto.AUDIO_OUT_RATE)
                if client is not None:
                    try:
                        client.send_binary(msg.data)
                    except Exception:  # noqa: BLE001
                        log.debug("client send failed (audio frame dropped)")
                continue
            try:
                ev = json.loads(msg.text())
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue
            self._on_engine_event(sess, ev)
        if sess.active and sess.engine is link:
            log.error("session %s: engine link closed unexpectedly (code %s)", sess.id,
                      getattr(link, "close_code", None))
            self.engine.reconcile()
            self._fail(sess, "engine_failed", "the live model stopped unexpectedly")

    def _on_engine_event(self, sess: Session, ev: dict) -> None:
        kind = ev.get("type")
        s = sess.stats
        if kind == "tool.request":
            self._tool_request(sess, ev)
            return
        if kind == "session.ready":
            sess.engine_ready = True
            ev = {**ev, "load_ms": sess.load_ms, "wait_ms": sess.waited_ms or None}
            self._send_client(sess, {"type": "model.state", **self.engine.public_state(),
                                     "state": "ready", "load_ms": sess.load_ms})
        elif kind == "input.speech.stopped":
            s["speech_segments"] += 1
        elif kind == "response.started":
            s["responses"] += 1
            if ev.get("trigger") in ("speech", "text"):
                s["turns"] += 1
            self._send_client(sess, {"type": "model.state", "state": "busy", "reason": "answering",
                                     "waiting": None})
        elif kind == "response.done":
            m = ev.get("metrics") or {}
            if ev.get("status") == "interrupted":
                s["interrupted"] += 1
            elif ev.get("status") == "failed":
                s["failed_responses"] += 1
            for key in ("first_audio_ms", "first_text_ms", "turn_ms"):
                if isinstance(m.get(key), (int, float)):
                    s[key].append(int(m[key]))
            if isinstance(m.get("first_audio_ms"), (int, float)):
                self._metric("realtime.latency", service="gx-live", session_id=sess.id, stage="first_audio",
                             ms=int(m["first_audio_ms"]))
            if isinstance(m.get("turn_ms"), (int, float)):
                self._metric("realtime.latency", service="gx-live", session_id=sess.id, stage="turn",
                             ms=int(m["turn_ms"]))
            self._send_client(sess, {"type": "model.state", "state": "ready", "reason": "", "waiting": None})
        elif kind == "response.interrupted":
            if isinstance(ev.get("latency_ms"), (int, float)):
                s["interrupt_ms"].append(int(ev["latency_ms"]))
                self._metric("realtime.latency", service="gx-live", session_id=sess.id, stage="interrupt",
                             ms=int(ev["latency_ms"]))
        elif kind == "error":
            s["errors"].append({"code": str(ev.get("code"))[:40], "at": self.clock()})
            if ev.get("fatal"):
                self._send_client(sess, ev)
                self.end(sess.id, "failed", close_code=1011)
                return
        elif kind not in ENGINE_EVENT_PASSTHROUGH:
            return
        self._send_client(sess, ev)

    def _send_client(self, sess: Session, ev: dict) -> None:
        client = sess.client
        if client is not None:
            self._send_json(sess, client, ev)

    @staticmethod
    def _send_json(sess: Session, ws: Any, ev: dict) -> None:
        try:
            ws.send_text(json.dumps(ev, separators=(",", ":")))
        except Exception:  # noqa: BLE001
            log.debug("session %s: event %s not delivered", sess.id, ev.get("type"))

    def _on_engine_state(self, view: dict) -> None:
        sess = self.active_session()
        if sess is not None and sess.client is not None and not sess.engine_ready:
            self._send_json(sess, sess.client, {"type": "model.state", **view})

    # ---------------------------------------------------------------- tools --
    def _tool_request(self, sess: Session, ev: dict) -> None:
        call_id = str(ev.get("call_id") or "")[:40]
        if not call_id:
            return
        name = ev.get("name")
        started = self.clock()
        try:
            if not sess.config.get("tools"):
                raise ValidationError("tools are switched off for this session", code="tools_disabled")
            args = toolspec.validate(name, ev.get("arguments"))
            pending = sum(1 for c in sess.tool_calls.values() if c.state != "done")
            if pending >= self.cfg.max_pending_tools:
                raise ValidationError("too many tool calls are already running", code="tool_busy")
        except ValidationError as exc:
            result = {"ok": False, "error": {"code": exc.code, "message": exc.message}}
            self._engine_send(sess, {"type": "tool.result", "call_id": call_id, "ok": False,
                                     "content": f"Tool call refused: {exc.message}"})
            self._send_client(sess, {"type": "tool.result", "call_id": call_id, "name": str(name)[:40],
                                     "ok": False, "summary": "", "latency_ms": 0, "error": result["error"]})
            sess.stats["tools"].append({"name": str(name)[:40], "ok": False, "latency_ms": 0,
                                        "error_code": exc.code, "at": started})
            return
        timeout = self.cfg.delegate_timeout_s if name == "delegate_to_gx" else self.cfg.tool_timeout_s
        call = ToolCall(call_id=call_id, name=str(name), arguments=args, created_at=started,
                        deadline=started + timeout)
        with sess.cond:
            sess.tool_calls[call_id] = call
            sess.cond.notify_all()
        self._send_client(sess, {"type": "tool.call", "call_id": call_id, "name": call.name,
                                 "arguments": toolspec.public_arguments(call.name, args), "status": "running"})

    def claim_tool_calls(self, sid: str, wait_s: float) -> dict:
        """Long poll for the Control Center's tool executor."""
        sess = self.get(sid)
        deadline = self.clock() + max(0.0, min(wait_s, 30.0))
        with sess.cond:
            while True:
                queued = [c for c in sess.tool_calls.values() if c.state == "queued"]
                if queued or not sess.active or self.clock() >= deadline:
                    break
                sess.cond.wait(timeout=max(0.05, min(1.0, deadline - self.clock())))
            for c in queued:
                c.state = "claimed"
                c.claimed_at = self.clock()
        return {"session_id": sid, "session_state": sess.state, "end_reason": sess.end_reason,
                "calls": [c.public() for c in queued]}

    def tool_update(self, sid: str, call_id: str, body: Any) -> dict:
        sess = self.get(sid)
        if not isinstance(body, dict):
            raise ValidationError("body must be an object")
        call = sess.tool_calls.get(call_id)
        if call is None:
            raise NotFoundError("no such tool call")
        if call.state == "done":
            raise ConflictError("the tool call already finished", code="tool_done")
        kind = body.get("kind")
        if kind == "progress":
            state = body.get("state")
            if state not in ("waiting", "loading", "running"):
                raise ValidationError("progress state must be waiting, loading or running")
            detail = str(body.get("detail") or "")[:300]
            model = body.get("model") if body.get("model") in toolspec.DELEGATE_MODELS else None
            call.progress.append({"state": state, "detail": detail, "at": self.clock()})
            self._send_client(sess, {"type": "tool.progress", "call_id": call_id, "state": state,
                                     "detail": detail, "model": model})
            return {"ok": True}
        if kind != "result":
            raise ValidationError("kind must be progress or result")
        ok = body.get("ok") is True
        content = str(body.get("content") or "")[:8000]
        summary = str(body.get("summary") or content)[:600]
        model = body.get("model") if body.get("model") in toolspec.DELEGATE_MODELS else None
        err = body.get("error") if isinstance(body.get("error"), dict) else None
        if err is not None:
            err = {"code": str(err.get("code") or "tool_failed")[:40], "message": str(err.get("message") or "")[:300]}
        if not ok and err is None:
            err = {"code": "tool_failed", "message": content[:300] or "the tool failed"}
        self._finish_tool(sess, call, ok=ok, content=content, summary=summary, model=model, error=err,
                          routed=str(body.get("routed_to") or "")[:40] or None)
        return {"ok": True}

    def _finish_tool(self, sess: Session, call: ToolCall, *, ok: bool, content: str, summary: str,
                     model: str | None, error: dict | None, routed: str | None = None) -> None:
        with sess.cond:
            if call.state == "done":
                return
            call.state = "done"
            call.result = {"ok": ok, "error": error}
            sess.cond.notify_all()
        latency = int((self.clock() - call.created_at) * 1000)
        for_model = content if ok else f"The tool failed: {(error or {}).get('message', 'unknown error')}"
        self._engine_send(sess, {"type": "tool.result", "call_id": call.call_id, "name": call.name, "ok": ok,
                                 "content": for_model})
        self._send_client(sess, {"type": "tool.result", "call_id": call.call_id, "name": call.name, "ok": ok,
                                 "summary": summary if ok else "", "model": model, "routed_to": routed,
                                 "latency_ms": latency, "error": error})
        self._send_client(sess, {"type": "metrics", "tool_ms": latency,
                                 **({"delegation_ms": latency} if call.name == "delegate_to_gx" else {})})
        sess.stats["tools"].append({"name": call.name, "ok": ok, "latency_ms": latency, "at": call.created_at,
                                    "model": model, "error_code": (error or {}).get("code")})
        self._metric("realtime.latency", service="gx-live", session_id=sess.id,
                     stage="delegation" if call.name == "delegate_to_gx" else "tool", ms=latency,
                     outcome="ok" if ok else "failed")

    # --------------------------------------------------------- load/unload --
    def request_load(self) -> dict:
        block = self.engine.policy_block()
        if block:
            raise UnavailableError(block[1], code=block[0])
        if self.engine.state in (READY, LOADING):
            return self.health()

        def run() -> None:
            try:
                self.engine.ensure_loaded()
            except (ResourceWait, EngineError) as exc:
                log.warning("explicit load did not complete: %s", exc)

        threading.Thread(target=run, name="gx-live-explicit-load", daemon=True).start()
        return self.health()

    def request_unload(self, body: Any) -> dict:
        if body is None:
            body = {}
        if not isinstance(body, dict):
            raise ValidationError("body must be an object")
        if_idle = body.get("if_idle") is True
        reason = body.get("reason") if body.get("reason") in ("gxmax", "maintenance", "manual", "evicted") \
            else ("evicted" if if_idle else "manual")
        sess = self.active_session()
        if if_idle:
            if sess is not None:
                raise ConflictError("a live session is running", code="busy")
            if self.engine.pin_honoured():
                raise ConflictError("gx-live is pinned in Resource Control", code="pinned")
            if self.engine.state == LOADING:
                raise ConflictError("gx-live is loading", code="busy")
        if sess is not None:
            self.end(sess.id, "gx_max" if reason == "gxmax" else ("maintenance" if reason == "maintenance"
                                                                   else "completed"), close_code=1001)
        if self.engine.state == UNLOADED and not self.engine.docker.exists(self.cfg.engine_container):
            result = {"state": "unloaded", "already": True, "verified": not self.engine.guard.listed()}
            if self.engine.guard.listed():
                self.engine.guard.release()
                result["verified"] = True
            return result
        out = self.engine.unload(reason)
        return {"state": self.engine.state, **out}

    # -------------------------------------------------------------- monitor --
    def _monitor_loop(self) -> None:
        while not self._stop.wait(1.0):
            try:
                self._tick()
            except Exception:  # noqa: BLE001
                log.exception("monitor tick failed")

    def _tick(self) -> None:
        now = self.clock()
        eng = self.engine
        if eng.state == LOADING:
            eng.sample_memory()
        sess = self.active_session()
        # --- session expiry
        if sess is not None:
            if now > sess.expires_at:
                self.end(sess.id, "timeout", close_code=1001)
            elif self._abandoned(sess, now):
                self.end(sess.id, "abandoned")
            else:
                self._expire_tools(sess, now)
                if now - self._last_ping >= 20:
                    self._last_ping = now
                    for ws in (sess.client, sess.engine):
                        if ws is not None:
                            try:
                                ws.ping(b"gx")
                            except Exception:  # noqa: BLE001
                                log.debug("ping failed")
        # --- gx-max always wins, Maintenance stops new work
        block = eng.policy_block()
        sess = self.active_session()
        if block is not None:
            code, _reason = block
            if code == "gx_max_active":
                if sess is not None:
                    self.end(sess.id, "gx_max", close_code=1001)
                holding = eng.state in (READY, LOADING, WAITING) or eng.docker.exists(self.cfg.engine_container)
                if holding and eng.state != UNLOADING:
                    eng.unload("gxmax: gx-max claimed gx10-02")
                return
            if sess is None and eng.state == READY:
                eng.unload("maintenance: Maintenance mode")
                return
        # --- idle unload
        if sess is None and eng.state == READY and self.cfg.idle_unload_s > 0 \
                and now - eng.last_activity > self.cfg.idle_unload_s and not eng.pin_honoured():
            eng.unload(f"idle for {int(now - eng.last_activity)} s")
            return
        if sess is None and eng.state == WAITING:
            eng._set(UNLOADED, "")  # noqa: SLF001 - nobody is waiting any more
        # --- reconcile
        if now - self._last_reconcile > 15 and eng.state in (READY, UNLOADED):
            self._last_reconcile = now
            eng.reconcile()

    def _abandoned(self, sess: Session, now: float) -> bool:
        if sess.state == "created":
            return now - sess.created_at > self.cfg.attach_timeout_s
        if sess.state == "detached" and sess.detached_at:
            return now - sess.detached_at > self.cfg.reconnect_grace_s
        return False

    def _expire_tools(self, sess: Session, now: float) -> None:
        for call in list(sess.tool_calls.values()):
            if call.state != "done" and now > call.deadline:
                self._finish_tool(sess, call, ok=False, content="", summary="", model=None,
                                  error={"code": "tool_timeout",
                                         "message": f"no result within {int(call.deadline - call.created_at)} s"})


def _close(ws: Any, code: int, reason: str) -> None:
    try:
        ws.close(code, reason[:100])
    except Exception:  # noqa: BLE001
        log.debug("close failed")
