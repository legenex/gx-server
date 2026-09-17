"""Hermetic tests for the gx-live supervisor. Standard library only.

No GPU, no Docker daemon, no model: a stub engine (real WebSocket server on
loopback) stands in for the MiniCPM-o engine, and a fake Docker / guard stand
in for the container runtime and the node admission guard.

Run:  python3 -m unittest discover -s tests -t .     (from legenex/live)
"""

from __future__ import annotations

import json
import os
import secrets
import struct
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE.parents[2] / "common"))

from gxcommon import rtws  # noqa: E402
from gx_live import config as config_mod  # noqa: E402
from gx_live import protocol as proto  # noqa: E402
from gx_live import tools as toolspec  # noqa: E402
from gx_live.engine import READY, UNLOADED, WAITING, EngineController  # noqa: E402
from gx_live.errors import LiveError, ValidationError  # noqa: E402
from gx_live.server import build_servers  # noqa: E402
from gx_live.service import LiveService  # noqa: E402


def fake_key() -> str:
    return secrets.token_urlsafe(32)


def sid() -> str:
    return "live_" + secrets.token_hex(16)


def owner() -> str:
    return secrets.token_hex(8)


# ------------------------------------------------------------------ stubs --
class StubEngineHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    key = ""
    sessions: list = []
    received: list = []

    def log_message(self, *a):
        return

    def do_GET(self):  # noqa: N802
        if self.headers.get("Authorization") != f"Bearer {self.key}":
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/health":
            body = json.dumps({"ready": True, "loading": False, "gpu_allocated_gib": 21.0}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        ws = rtws.accept(self)
        start = json.loads(ws.recv().text())
        type(self).sessions.append(start)
        ws.send_text(json.dumps({"type": "session.ready", "system_prefill_ms": 5}))
        response = 0
        while True:
            msg = ws.recv()
            if msg is None:
                break
            if not msg.is_text:
                type(self).received.append(("binary", msg.data[:1]))
                continue
            ev = json.loads(msg.text())
            type(self).received.append(("json", ev.get("type")))
            if ev["type"] == "input.text" and ev["text"] == "use a tool":
                response += 1
                ws.send_text(json.dumps({"type": "response.started", "response": response, "turn": 1,
                                         "trigger": "text"}))
                ws.send_text(json.dumps({"type": "tool.request", "call_id": "call_" + "ab" * 8,
                                         "name": "delegate_to_gx",
                                         "arguments": {"model": "gx-fast", "task": "write a haiku"}}))
                ws.send_text(json.dumps({"type": "response.done", "response": response, "status": "completed",
                                         "tool_call": True, "text": "", "metrics": {"turn_ms": 400}}))
            elif ev["type"] == "input.text" and ev["text"] == "bad tool":
                ws.send_text(json.dumps({"type": "tool.request", "call_id": "call_" + "cd" * 8,
                                         "name": "delegate_to_gx",
                                         "arguments": {"model": "gx-max", "task": "x"}}))
            elif ev["type"] == "input.text":
                response += 1
                ws.send_text(json.dumps({"type": "response.started", "response": response, "turn": 1,
                                         "trigger": "text"}))
                ws.send_text(json.dumps({"type": "transcript.assistant.delta", "response": response,
                                         "text": "hello"}))
                ws.send_binary(proto.pack(proto.KIND_ASSISTANT_AUDIO, b"\x00\x00" * 2400, response=response))
                ws.send_text(json.dumps({"type": "response.done", "response": response, "status": "completed",
                                         "text": "hello", "metrics": {"first_audio_ms": 1500, "turn_ms": 2500}}))
            elif ev["type"] == "tool.result":
                response += 1
                ws.send_text(json.dumps({"type": "response.started", "response": response, "turn": 1,
                                         "trigger": "tool"}))
                ws.send_text(json.dumps({"type": "response.done", "response": response, "status": "completed",
                                         "text": "said: " + ev.get("content", ""), "metrics": {}}))
            elif ev["type"] == "engine.session.end":
                break
        ws.close()


class FakeDocker:
    def __init__(self):
        self.containers: dict[str, bool] = {}
        self.calls: list[list[str]] = []
        self.fail_run = False

    def run(self, args, timeout=120):
        self.calls.append(args)

        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        r = R()
        if args[0] == "run":
            if self.fail_run:
                r.returncode = 125
                return r
            name = args[args.index("--name") + 1]
            self.containers[name] = True
        elif args[0] in ("stop",):
            self.containers[args[-1]] = False
        elif args[0] == "rm":
            self.containers.pop(args[-1], None)
        return r

    def running(self, name):
        return bool(self.containers.get(name)) or name == "gx-llama-swap-node02"

    def exists(self, name):
        return name in self.containers or name == "gx-llama-swap-node02"

    def processes(self, name):
        return 0


class FakeGuard:
    def __init__(self):
        self.ledger: set[str] = set()
        self.refuse: str | None = None

    def launch(self, start, *, extra_gib, on_admitted):
        from gx_live.errors import ResourceWait
        if self.refuse:
            raise ResourceWait("waiting for memory on gx10-02 (other models are using it)",
                               code=self.refuse)
        on_admitted()
        start()
        self.ledger.add("gx-live-engine")

    def register(self):
        self.ledger.add("gx-live-engine")

    def release(self):
        self.ledger.discard("gx-live-engine")

    def listed(self):
        return "gx-live-engine" in self.ledger


class FakePeers:
    def pending_gib(self):
        return 0.0

    def snapshot(self):
        return {}


class FakeMetrics:
    def __init__(self):
        self.lines: list[tuple[str, dict]] = []

    def emit(self, event, **fields):
        self.lines.append((event, fields))


def make_config(tmp: Path, **over) -> config_mod.Config:
    secrets_dir = tmp / "secrets"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    (secrets_dir / "api-key").write_text(fake_key())
    env = {"GX_LIVE_SECRETS_DIR": str(secrets_dir), "GX_LIVE_BINDS": "127.0.0.1",
           "GX_LIVE_STATE_DIR": str(tmp / "state"), "GX_GUARD_STATE_DIR": str(tmp / "guard"),
           "GX_LIVE_GXMAX_HOLD": str(tmp / "guard" / "node2.gxmax-hold"),
           "GX_LIVE_MAINTENANCE_HOLD": str(tmp / "guard" / "node2.maintenance-hold"),
           "GX_LIVE_PINS_FILE": str(tmp / "guard" / "pins.json"),
           "GX_LIVE_GXMAX_DEADMAN_PID": str(tmp / "no-deadman.pid"),
           "GX_METRICS_FILE": "", "GX_LIVE_LOG_DIR": str(tmp / "logs")}
    (tmp / "guard").mkdir(parents=True, exist_ok=True)
    with mock.patch.dict(os.environ, env):
        cfg = config_mod.load()
    import dataclasses
    return dataclasses.replace(cfg, **over)


# ------------------------------------------------------------ protocol --
class ProtocolTests(unittest.TestCase):
    def test_binary_frames(self):
        mic = proto.pack(proto.KIND_MIC, b"\x00\x01" * 1600)  # 100 ms
        self.assertEqual(proto.check_client_binary(mic)[0], proto.KIND_MIC)
        jpeg = proto.pack(proto.KIND_CAMERA, b"\xff\xd8\xff\xe0" + b"x" * 100)
        self.assertEqual(proto.check_client_binary(jpeg)[0], proto.KIND_CAMERA)
        bad = [
            (proto.pack(proto.KIND_MIC, b"\x00" * 3201), 1003),         # odd length
            (proto.pack(proto.KIND_MIC, b"\x00\x00" * 100), 1003),       # < 20 ms
            (proto.pack(proto.KIND_MIC, b"\x00\x00" * 8001), 1003),      # > 500 ms
            (proto.pack(proto.KIND_CAMERA, b"GIF89a" + b"x" * 10), 1003),
            (proto.pack(proto.KIND_CAMERA, b"\xff\xd8\xff" + b"x" * proto.MAX_JPEG_BYTES), 1009),
            (proto.pack(proto.KIND_ASSISTANT_AUDIO, b"\x00\x00" * 800), 1003),  # server-only kind
            (b"\x01\x02\x00", 1003),                                       # short header
            (struct.pack("!BBHI", 1, 2, 0, 0) + b"\x00\x00" * 800, 1003),  # wrong version
        ]
        for frame, code in bad:
            with self.assertRaises(proto.FrameError) as ctx:
                proto.check_client_binary(frame)
            self.assertEqual(ctx.exception.close_code, code)

    def test_client_events(self):
        self.assertEqual(proto.parse_client_event('{"type":"input.text","text":"  hi "}'),
                         {"type": "input.text", "text": "hi"})
        self.assertEqual(proto.parse_client_event('{"type":"session.update","camera":false}'),
                         {"type": "session.update", "camera": False})
        self.assertEqual(proto.parse_client_event('{"type":"playback.state","playing":true,"buffered_ms":300}')
                         ["buffered_ms"], 300)
        for raw, code in (('{"type":"nope"}', "unknown_event"), ('{"type":"input.text","text":""}', "invalid_event"),
                          ('{"type":"input.text","text":"a\\u0000b"}', "invalid_event"),
                          ('{"type":"session.update"}', "invalid_event"),
                          ('{"type":"session.update","camera":"yes"}', "invalid_event"),
                          ('{"type":"ping","t":true}', "invalid_event"), ('[1]', "invalid_event")):
            with self.assertRaises(ValidationError) as ctx:
                proto.parse_client_event(raw)
            self.assertEqual(ctx.exception.code, code, raw)
        with self.assertRaises(proto.FrameError) as ctx:
            proto.parse_client_event("{not json")
        self.assertEqual(ctx.exception.close_code, 1007)
        with self.assertRaises(proto.FrameError) as ctx:
            proto.parse_client_event(json.dumps({"type": "input.text", "text": "x" * 70000}))
        self.assertEqual(ctx.exception.close_code, 1009)

    def test_config_normalisation(self):
        cfg = proto.normalise_config({})
        self.assertEqual(cfg["vad"], {"threshold": 0.5, "silence_ms": 700})
        self.assertTrue(cfg["tools"])
        for bad in ({"x": 1}, {"language": "fr"}, {"vad": {"threshold": 0.1}}, {"vad": {"silence_ms": 5000}},
                    {"max_response_tokens": 5}, {"instructions": "a" * 2001}, {"tools": "yes"},
                    {"vad": {"threshold": 0.5, "other": 1}}, {"instructions": "a\x00"}):
            with self.assertRaises(ValidationError, msg=str(bad)):
                proto.normalise_config(bad)


class ToolTests(unittest.TestCase):
    def test_valid_calls(self):
        self.assertEqual(toolspec.validate("get_time", None), {})
        self.assertEqual(toolspec.validate("delegate_to_gx", {"model": "gx-reason", "task": " prove it "}),
                         {"model": "gx-reason", "task": "prove it"})
        self.assertEqual(toolspec.validate("search_library", {"query": "sunset"})["type"], "any")
        self.assertEqual(toolspec.validate("fetch_url", {"url": "https://example.org/a"})["url"],
                         "https://example.org/a")

    def test_gx_max_can_never_be_delegated(self):
        for model in ("gx-max", "gx-mini", "GX-FAST", None):
            with self.assertRaises(ValidationError):
                toolspec.validate("delegate_to_gx", {"model": model, "task": "x"})

    def test_invalid_calls(self):
        cases = [("rm_rf", {}), ("get_time", {"x": 1}), ("delegate_to_gx", {"model": "gx-fast"}),
                 ("delegate_to_gx", {"model": "gx-fast", "task": ""}), ("delegate_to_gx", "string"),
                 ("search_library", {"query": "a", "type": "pdf"}), ("fetch_url", {"url": "file:///etc/passwd"}),
                 ("fetch_url", {"url": "https://x.org/" + "a" * 2100}), (None, {})]
        for name, args in cases:
            with self.assertRaises(ValidationError, msg=f"{name} {args}"):
                toolspec.validate(name, args)

    def test_definitions_are_closed_schemas(self):
        for d in toolspec.DEFINITIONS:
            self.assertFalse(d["function"]["parameters"]["additionalProperties"])


# ------------------------------------------------------------- service --
class ServiceHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        StubEngineHandler.key = fake_key()
        StubEngineHandler.sessions = []
        StubEngineHandler.received = []
        handler = type("H", (StubEngineHandler,), {})
        self.engine_srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.engine_srv.daemon_threads = True
        threading.Thread(target=self.engine_srv.serve_forever, daemon=True).start()
        port = self.engine_srv.server_address[1]
        self.cfg = make_config(self.tmp, engine_port=port, idle_unload_s=2, attach_timeout_s=30,
                               reconnect_grace_s=2, resource_retry_s=2)
        (self.cfg.engine_key_file).write_text(StubEngineHandler.key)
        self._sockets = []
        self.docker = FakeDocker()
        self.guard = FakeGuard()
        self.metrics = FakeMetrics()
        self.mem = {"MemAvailable": 90.0, "MemTotal": 121.0, "SwapTotal": 63.0, "SwapFree": 60.0}
        self.engine = EngineController(self.cfg, docker=self.docker, guard=self.guard, peers=FakePeers(),
                                       mem=lambda: dict(self.mem), metrics=self.metrics)
        self.engine._verify_gone.__func__  # noqa: B018 - exists
        self.service = LiveService(self.cfg, self.engine, metrics=self.metrics)
        self.servers = build_servers(self.service, self.cfg.api_key, ("127.0.0.1",), 0, rtws)
        self.port = self.servers[0].server_address[1]
        for s in self.servers:
            threading.Thread(target=s.serve_forever, daemon=True).start()
        self.service.start()

    def tearDown(self):
        for ws in self._sockets:
            ws.close()
        self.service.stop()
        for s in self.servers:
            s.shutdown()
            s.server_close()
        self.engine_srv.shutdown()
        self.engine_srv.server_close()

    # helpers
    def call(self, method, path, body=None, key=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method)
        if key is not False:
            req.add_header("Authorization", f"Bearer {key or self.cfg.api_key}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def create(self, **config):
        s = sid()
        status, body = self.call("POST", "/v1/live/sessions", {"session_id": s, "owner": owner(),
                                                                "config": config})
        self.assertEqual(status, 201, body)
        return s, body

    def attach(self, s, token, **kw):
        ws = self._connect(s, token, **kw)
        self._sockets.append(ws)
        return ws

    def _connect(self, s, token, **kw):
        return rtws.connect("127.0.0.1", self.port, f"/v1/live/sessions/{s}/ws?join={token}",
                            headers={"Authorization": f"Bearer {kw.get('key', self.cfg.api_key)}",
                                     "X-GX-Session": kw.get("header_sid", s)})

    @staticmethod
    def read_until(ws, kind, limit=60, timeout=20):
        out = []
        deadline = time.time() + timeout
        ws.sock.settimeout(timeout)
        while time.time() < deadline and len(out) < limit:
            msg = ws.recv()
            if msg is None:
                break
            ev = json.loads(msg.text()) if msg.is_text else {"type": "_binary", "data": msg.data}
            out.append(ev)
            if ev["type"] == kind:
                return out
        raise AssertionError(f"{kind} not received; got {[e['type'] for e in out]}")


class LifecycleTests(ServiceHarness):
    def test_health_is_open_and_follows_the_contract(self):
        status, body = self.call("GET", "/health", key=False)
        self.assertEqual(status, 200)
        for field in ("service", "state", "busy", "pinned", "active_sessions", "memory"):
            self.assertIn(field, body)
        self.assertEqual(body["service"], "gx-live")
        self.assertEqual(set(body["memory"]) >= {"pending_gib", "resident_gib", "estimate_gib"}, True)
        self.assertEqual(body["memory"]["pending_gib"], 0.0)

    def test_everything_else_needs_the_key(self):
        for method, path in (("GET", "/v1/live/model"), ("POST", "/v1/live/sessions"), ("POST", "/v1/live/unload"),
                             ("GET", f"/v1/live/sessions/{sid()}")):
            status, body = self.call(method, path, {} if method == "POST" else None, key=False)
            self.assertEqual(status, 401, path)
            status, _ = self.call(method, path, {} if method == "POST" else None, key=fake_key())
            self.assertEqual(status, 401, path)

    def test_create_loads_and_session_runs_end_to_end(self):
        s, body = self.create()
        self.assertIn("join_token", body)
        self.assertTrue(body["upstream_path"].startswith(f"/v1/live/sessions/{s}/ws?join="))
        ws = self.attach(s, body["join_token"])
        events = self.read_until(ws, "session.ready")
        kinds = [e["type"] for e in events]
        self.assertEqual(kinds[0], "session.created")
        self.assertIn("model.state", kinds)
        created = events[0]
        self.assertEqual(created["protocol"], "gx-live.v1")
        self.assertEqual(created["model"]["revision"], "503e754207c94da6bb26850b4469f367c9ea3582")
        self.assertEqual(self.engine.state, READY)
        self.assertEqual(StubEngineHandler.sessions[0]["tools"][0]["function"]["name"], "get_time")
        # a text turn: events + audio pass through; stats are recorded
        ws.send_text(json.dumps({"type": "input.text", "text": "hello"}))
        events = self.read_until(ws, "response.done")
        kinds = [e["type"] for e in events]
        self.assertIn("transcript.assistant.delta", kinds)
        audio = [e for e in events if e["type"] == "_binary"]
        self.assertEqual(proto.unpack(audio[0]["data"])[0], proto.KIND_ASSISTANT_AUDIO)
        # mic + camera frames are validated and forwarded
        ws.send_binary(proto.pack(proto.KIND_MIC, b"\x00\x00" * 1600))
        ws.send_binary(proto.pack(proto.KIND_CAMERA, b"\xff\xd8\xff\xe0" + b"j" * 64))
        ws.send_text(json.dumps({"type": "ping", "t": 5}))
        self.assertEqual(self.read_until(ws, "pong")[-1]["t"], 5)
        status, health = self.call("GET", "/health", key=False)
        self.assertEqual(health["state"], "busy")
        self.assertTrue(health["busy"])
        self.assertEqual(health["active_sessions"], 1)
        ws.send_text(json.dumps({"type": "session.stop"}))
        ended = self.read_until(ws, "session.ended")[-1]
        self.assertEqual(ended["reason"], "completed")
        status, summary = self.call("GET", f"/v1/live/sessions/{s}")
        self.assertEqual(summary["state"], "ended")
        self.assertEqual(summary["turns"], 1)
        self.assertEqual(summary["latency"]["first_audio_ms"]["median"], 1500)
        self.assertEqual(summary["media"]["camera_frames"], 1)
        self.assertNotIn("instructions", summary["config"])
        self.assertIn(("binary", b"\x01"), StubEngineHandler.received)
        self.assertIn(("binary", b"\x02"), StubEngineHandler.received)
        # metrics carry latencies but never content
        events = [e for e, _ in self.metrics.lines]
        self.assertIn("model.load", events)
        self.assertIn("realtime.session", events)
        for _, fields in self.metrics.lines:
            for bad in ("text", "transcript", "prompt", "join_token", "instructions"):
                self.assertNotIn(bad, fields)

    def test_one_session_at_a_time(self):
        self.create()
        status, body = self.call("POST", "/v1/live/sessions", {"session_id": sid(), "owner": owner()})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "session_busy")

    def test_create_validation(self):
        for body in ({"session_id": "call_" + "a" * 32, "owner": owner()}, {"session_id": sid(), "owner": "x"},
                     {"session_id": sid(), "owner": owner(), "config": {"language": "de"}},
                     {"session_id": sid(), "owner": owner(), "ttl_s": 10}, []):
            status, _ = self.call("POST", "/v1/live/sessions", body)
            self.assertEqual(status, 400, body)

    def test_join_token_and_session_header_are_enforced(self):
        s, body = self.create()
        for kwargs, token, status in (({}, "wrong-token-value", 403), ({"header_sid": sid()}, body["join_token"], 403),
                                      ({"key": fake_key()}, body["join_token"], 401)):
            with self.assertRaises(rtws.HandshakeError) as ctx:
                self.attach(s, token, **kwargs)
            self.assertEqual(ctx.exception.status, status)
        with self.assertRaises(rtws.HandshakeError) as ctx:
            self.attach(sid(), body["join_token"])
        self.assertEqual(ctx.exception.status, 404)
        self.call("POST", f"/v1/live/sessions/{s}/end")
        with self.assertRaises(rtws.HandshakeError) as ctx:
            self.attach(s, body["join_token"])
        self.assertEqual(ctx.exception.status, 410)

    def test_protocol_violation_closes_the_client(self):
        s, body = self.create()
        ws = self.attach(s, body["join_token"])
        self.read_until(ws, "session.ready")
        ws.send_binary(b"\x07\x01\x00\x00\x00\x00\x00\x00abc")
        events = self.read_until(ws, "error")
        self.assertTrue(events[-1]["fatal"])
        self.assertIsNone(ws.recv())
        self.assertEqual(ws.close_code, 1003)

    def test_reconnect_replaces_connection_and_grace_expiry_ends(self):
        s, body = self.create()
        ws1 = self.attach(s, body["join_token"])
        self.read_until(ws1, "session.ready")
        ws2 = self.attach(s, body["join_token"])
        created = self.read_until(ws2, "session.ready")
        self.assertTrue(created[0]["resumed"])
        self.assertIsNone(ws1.recv())
        self.assertEqual(ws1.close_code, 4409)
        ws2.close()
        deadline = time.time() + 10
        while time.time() < deadline and self.service.get(s).state != "ended":
            time.sleep(0.2)
        self.assertEqual(self.service.get(s).end_reason, "abandoned")


class ToolBridgeTests(ServiceHarness):
    def test_tool_call_roundtrip(self):
        s, body = self.create()
        ws = self.attach(s, body["join_token"])
        self.read_until(ws, "session.ready")
        ws.send_text(json.dumps({"type": "input.text", "text": "use a tool"}))
        call = self.read_until(ws, "tool.call")[-1]
        self.assertEqual(call["name"], "delegate_to_gx")
        self.assertEqual(call["arguments"], {"model": "gx-fast", "task": "write a haiku"})
        status, claimed = self.call("GET", f"/v1/live/sessions/{s}/tool-calls?wait=5")
        self.assertEqual(status, 200)
        self.assertEqual([c["call_id"] for c in claimed["calls"]], [call["call_id"]])
        # a claimed call is not handed out twice
        status, again = self.call("GET", f"/v1/live/sessions/{s}/tool-calls?wait=0")
        self.assertEqual(again["calls"], [])
        path = f"/v1/live/sessions/{s}/tool-calls/{call['call_id']}"
        status, _ = self.call("POST", path, {"kind": "progress", "state": "loading", "model": "gx-fast",
                                             "detail": "gx-fast is loading"})
        self.assertEqual(status, 200)
        prog = self.read_until(ws, "tool.progress")[-1]
        self.assertEqual(prog["state"], "loading")
        status, _ = self.call("POST", path, {"kind": "result", "ok": True, "content": "Silicon rivers hum",
                                             "model": "gx-fast"})
        self.assertEqual(status, 200)
        events = self.read_until(ws, "response.done")
        result = next(e for e in events if e["type"] == "tool.result")
        self.assertTrue(result["ok"])
        self.assertEqual(result["model"], "gx-fast")
        self.assertIsInstance(result["latency_ms"], int)
        self.assertEqual(events[-1]["text"], "said: Silicon rivers hum")
        status, dup = self.call("POST", path, {"kind": "result", "ok": True, "content": "x"})
        self.assertEqual(status, 409)
        status, summary = self.call("GET", f"/v1/live/sessions/{s}")
        self.assertEqual(summary["tools"][0]["name"], "delegate_to_gx")
        self.assertTrue(summary["tools"][0]["ok"])

    def test_invalid_tool_call_is_refused_without_execution(self):
        s, body = self.create()
        ws = self.attach(s, body["join_token"])
        self.read_until(ws, "session.ready")
        ws.send_text(json.dumps({"type": "input.text", "text": "bad tool"}))
        result = self.read_until(ws, "tool.result")[-1]
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "invalid_tool_arguments")
        status, claimed = self.call("GET", f"/v1/live/sessions/{s}/tool-calls?wait=0")
        self.assertEqual(claimed["calls"], [])
        # the engine was told the call failed
        deadline = time.time() + 5
        while time.time() < deadline and ("json", "tool.result") not in StubEngineHandler.received:
            time.sleep(0.1)
        self.assertIn(("json", "tool.result"), StubEngineHandler.received)

    def test_tools_disabled(self):
        s, body = self.create(tools=False)
        ws = self.attach(s, body["join_token"])
        self.read_until(ws, "session.ready")
        self.assertEqual(StubEngineHandler.sessions[-1]["tools"], [])
        ws.send_text(json.dumps({"type": "input.text", "text": "use a tool"}))
        result = self.read_until(ws, "tool.result")[-1]
        self.assertEqual(result["error"]["code"], "tools_disabled")

    def test_tool_timeout(self):
        s, body = self.create()
        ws = self.attach(s, body["join_token"])
        self.read_until(ws, "session.ready")
        ws.send_text(json.dumps({"type": "input.text", "text": "use a tool"}))
        call = self.read_until(ws, "tool.call")[-1]
        sess = self.service.get(s)
        sess.tool_calls[call["call_id"]].deadline = time.time() - 1
        result = self.read_until(ws, "tool.result", timeout=10)[-1]
        self.assertEqual(result["error"]["code"], "tool_timeout")

    def test_long_poll_returns_when_session_ends(self):
        s, _ = self.create()
        threading.Timer(0.5, lambda: self.call("POST", f"/v1/live/sessions/{s}/end")).start()
        t = time.time()
        status, body = self.call("GET", f"/v1/live/sessions/{s}/tool-calls?wait=20")
        self.assertLess(time.time() - t, 10)
        self.assertEqual(body["session_state"], "ended")


class PolicyTests(ServiceHarness):
    def test_unload_if_idle_refuses_during_session_and_pin(self):
        s, body = self.create()
        ws = self.attach(s, body["join_token"])
        self.read_until(ws, "session.ready")
        status, err = self.call("POST", "/v1/live/unload", {"if_idle": True})
        self.assertEqual(status, 409)
        self.assertEqual(err["error"]["code"], "busy")
        ws.send_text(json.dumps({"type": "session.stop"}))
        self.read_until(ws, "session.ended")
        (self.tmp / "guard" / "pins.json").write_text(json.dumps({"gx-live": {"by": "admin"}}))
        status, err = self.call("POST", "/v1/live/unload", {"if_idle": True})
        self.assertEqual((status, err["error"]["code"]), (409, "pinned"))
        (self.tmp / "guard" / "pins.json").write_text("{}")
        status, out = self.call("POST", "/v1/live/unload", {"if_idle": True})
        self.assertEqual(status, 200)
        self.assertEqual(out["state"], UNLOADED)
        self.assertTrue(out["verified"])
        self.assertFalse(self.guard.listed())

    def test_gxmax_hold_ends_session_and_unloads(self):
        s, body = self.create()
        ws = self.attach(s, body["join_token"])
        self.read_until(ws, "session.ready")
        self.cfg.gxmax_hold_file.write_text("drain")
        ended = self.read_until(ws, "session.ended", timeout=15)[-1]
        self.assertEqual(ended["reason"], "gx_max")
        deadline = time.time() + 15
        while time.time() < deadline and (self.engine.state != UNLOADED or self.engine.last_unload is None):
            time.sleep(0.2)
        self.assertEqual(self.engine.state, UNLOADED)
        self.assertTrue(self.engine.last_unload["verified"])
        self.assertTrue(self.engine.last_unload["reason"].startswith("gxmax"))
        status, err = self.call("POST", "/v1/live/sessions", {"session_id": sid(), "owner": owner()})
        self.assertEqual((status, err["error"]["code"]), (503, "gx_max_active"))
        self.cfg.gxmax_hold_file.unlink()

    def test_drain_call_ends_session(self):
        s, body = self.create()
        ws = self.attach(s, body["join_token"])
        self.read_until(ws, "session.ready")
        status, out = self.call("POST", "/v1/live/unload", {"if_idle": False, "reason": "gxmax"})
        self.assertEqual(status, 200)
        self.assertTrue(out["verified"])
        self.assertEqual(self.service.get(s).end_reason, "gx_max")

    def test_maintenance_blocks_new_sessions_and_unloads_idle_engine(self):
        s, body = self.create()
        ws = self.attach(s, body["join_token"])
        self.read_until(ws, "session.ready")
        ws.send_text(json.dumps({"type": "session.stop"}))
        self.read_until(ws, "session.ended")
        self.cfg.maintenance_hold_file.write_text("x")
        try:
            status, err = self.call("POST", "/v1/live/sessions", {"session_id": sid(), "owner": owner()})
            self.assertEqual((status, err["error"]["code"]), (503, "maintenance"))
            deadline = time.time() + 10
            while time.time() < deadline and self.engine.state != UNLOADED:
                time.sleep(0.2)
            self.assertEqual(self.engine.state, UNLOADED)
        finally:
            self.cfg.maintenance_hold_file.unlink()

    def test_idle_unload(self):
        s, body = self.create()
        ws = self.attach(s, body["join_token"])
        self.read_until(ws, "session.ready")
        ws.send_text(json.dumps({"type": "session.stop"}))
        self.read_until(ws, "session.ended")
        deadline = time.time() + 15
        while time.time() < deadline and self.engine.state != UNLOADED:
            time.sleep(0.2)
        self.assertEqual(self.engine.state, UNLOADED)
        self.assertTrue(self.engine.last_unload["reason"].startswith("idle"))

    def test_memory_wait_is_reported_with_numbers(self):
        self.guard.refuse = "insufficient_memory"
        self.mem["MemAvailable"] = 40.0
        s, body = self.create()
        ws = self.attach(s, body["join_token"])
        deadline = time.time() + 10
        states = []
        ws.sock.settimeout(10)
        while time.time() < deadline:
            msg = ws.recv()
            ev = json.loads(msg.text())
            if ev["type"] == "model.state":
                states.append(ev)
                if ev["state"] == "waiting":
                    break
        waiting = states[-1]
        self.assertEqual(waiting["state"], "waiting")
        self.assertEqual(waiting["waiting"]["code"], "insufficient_memory")
        self.assertEqual(waiting["waiting"]["required_gib"], 64.0)
        self.assertEqual(waiting["waiting"]["available_gib"], 40.0)
        self.assertIn("30 GiB reserve", waiting["reason"])
        status, health = self.call("GET", "/health", key=False)
        self.assertEqual(health["state"], WAITING)
        self.assertEqual(health["waiting"]["code"], "insufficient_memory")
        self.assertEqual(health["memory"]["pending_gib"], 0.0)
        # memory frees up -> the session proceeds
        self.guard.refuse = None
        self.mem["MemAvailable"] = 90.0
        self.read_until(ws, "session.ready", timeout=15)

    def test_pending_memory_while_loading_and_ready(self):
        self.engine.state = "loading"
        self.engine.admit_avail_gib = 90.0
        self.mem["MemAvailable"] = 80.0
        self.assertEqual(self.engine.pending_gib(), 24.0)
        self.engine.state = READY
        self.engine.resident_gib = 31.0
        self.assertEqual(self.engine.pending_gib(), 3.0)
        self.engine.state = UNLOADED
        self.assertEqual(self.engine.pending_gib(), 0.0)

    def test_failed_container_start_fails_the_session(self):
        self.docker.fail_run = True
        s, body = self.create()
        deadline = time.time() + 15
        while time.time() < deadline and self.service.get(s).state != "ended":
            time.sleep(0.1)
        status, summary = self.call("GET", f"/v1/live/sessions/{s}")
        self.assertEqual(summary["end_reason"], "failed")
        self.assertEqual(summary["errors"][0]["code"], "engine_failed")
        self.assertEqual(self.engine.state, "failed")
        with self.assertRaises(rtws.HandshakeError) as ctx:
            self.attach(s, body["join_token"])
        self.assertEqual(ctx.exception.status, 410)
        # a later session retries the load
        self.docker.fail_run = False
        s2, body2 = self.create()
        ws = self.attach(s2, body2["join_token"])
        self.read_until(ws, "session.ready")


class ConfigTests(unittest.TestCase):
    def test_refuses_wildcard_and_short_keys(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "api-key").write_text("short")
        with mock.patch.dict(os.environ, {"GX_LIVE_SECRETS_DIR": str(tmp)}), self.assertRaises(LiveError):
            config_mod.load()
        (tmp / "api-key").write_text(fake_key())
        with mock.patch.dict(os.environ, {"GX_LIVE_SECRETS_DIR": str(tmp), "GX_LIVE_BINDS": "0.0.0.0"}), \
                self.assertRaises(LiveError):
            config_mod.load()
        with mock.patch.dict(os.environ, {"GX_LIVE_SECRETS_DIR": str(tmp), "GX_GUARD_RESERVE_GIB": "20"}), \
                self.assertRaises(LiveError):
            config_mod.load()
        with mock.patch.dict(os.environ, {"GX_LIVE_SECRETS_DIR": str(tmp)}):
            cfg = config_mod.load()
        self.assertEqual(cfg.port, 18850)
        self.assertEqual(cfg.workload, "gx-live-engine")
        self.assertEqual(cfg.binds, ("127.0.0.1", "192.168.100.11"))
        self.assertGreaterEqual(cfg.reserve_gib, 30)


if __name__ == "__main__":
    unittest.main()
