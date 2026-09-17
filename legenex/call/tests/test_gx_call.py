"""Hermetic tests for the gx-call supervisor. Standard library only.

No GPU, no Docker daemon, no model: ``engine_stub.EngineStub`` stands in for
the engine container (real WebSocket, real tracker events) and a fake Docker
plus a fake admission guard stand in for the lifecycle.

Run:  python3 -m unittest discover -s tests -t . -v     (from legenex/call)
"""

from __future__ import annotations

import base64
import dataclasses
import http.client
import json
import os
import secrets
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE.parents[1] / "engine"))
sys.path.insert(0, str(HERE.parent))

from engine_stub import EngineStub, silence, speech_like  # noqa: E402
from gx_call import config as config_mod  # noqa: E402
from gx_call import validation as v  # noqa: E402
from gx_call import ws as wsmod  # noqa: E402
from gx_call.engine import LOADING, READY, UNLOADED, EngineController  # noqa: E402
from gx_call.errors import ConflictError, ResourceWait, UnavailableError, ValidationError  # noqa: E402
from gx_call.server import build_servers  # noqa: E402
from gx_call.service import CallService  # noqa: E402
from gx_call_tracker import Tracker  # noqa: E402

KEY = "k" * 40


def agent(tools=("update_intake_fields",)) -> dict:
    return {"agent_id": "agt_" + "a" * 24, "version": 3, "name": "Test agent",
            "system_prompt": "You are a test agent.",
            "tools": [{"name": t, "description": "d", "parameters": {"type": "object", "properties": {}},
                       "on_hold": ["One moment."]} for t in tools]}


def session_body(sid=None, **kw) -> dict:
    return {"session_id": sid or "call_" + secrets.token_hex(16), "owner": "u-admin", "agent": agent(), **kw}


# ------------------------------------------------------------- validation --
class ValidationTests(unittest.TestCase):
    def test_valid_spec(self):
        spec = v.session_spec(session_body(record=True, max_duration_s=120), max_session_s=1800)
        self.assertTrue(spec.record)
        self.assertEqual(spec.engine_config(10)["on_hold"], {"update_intake_fields": ["One moment."]})
        self.assertEqual(spec.engine_config(10)["tools"][0]["name"], "update_intake_fields")

    def test_rejects_bad_input(self):
        bad = [
            {"session_id": "call_nothex"},
            {"agent": {**agent(), "agent_id": "x"}},
            {"agent": {**agent(), "version": 0}},
            {"agent": {**agent(), "tools": [{"name": "Bad Name"}]}},
            {"agent": {**agent(), "tools": [{"name": f"t{i}x"} for i in range(9)]}},
            {"agent": {**agent(), "tools": [{"name": "abc", "parameters": {"type": "string"}}]}},
            {"agent": {**agent(), "system_prompt": "x" * 20000}},
            {"max_duration_s": 5},
            {"owner": "bad owner!"},
            {"mode": "prod"},
        ]
        for patch in bad:
            with self.subTest(patch=patch), self.assertRaises(ValidationError):
                v.session_spec({**session_body(), **patch}, max_session_s=1800)

    def test_injected_events_are_restricted(self):
        self.assertEqual(v.injected_event({"type": "state.updated", "x": 1})["x"], 1)
        with self.assertRaises(ValidationError):
            v.injected_event({"type": "session.ended"})

    def test_tool_result_validation(self):
        call_id, out, ok, extra = v.tool_result({"call_id": "tc_" + "0" * 16, "output": {"a": 1}})
        self.assertEqual(json.loads(out), {"a": 1})
        self.assertTrue(ok)
        with self.assertRaises(ValidationError):
            v.tool_result({"call_id": "nope", "output": "x"})
        with self.assertRaises(ValidationError):
            v.tool_result({"call_id": "tc_" + "0" * 16, "output": "x" * 5000})


# --------------------------------------------------------------------- ws --
class WebSocketFramingTests(unittest.TestCase):
    def pair(self):
        a, b = socket.socketpair()
        server = wsmod.WebSocket(a, a.makefile("rb"), mask_outgoing=False, require_masked=True)
        client = wsmod.WebSocket(b, b.makefile("rb"), mask_outgoing=True, require_masked=False)
        return server, client

    def test_roundtrip_text_binary_and_large(self):
        server, client = self.pair()
        client.send_text("héllo")
        client.send_binary(b"\x01\x02" * 40000)
        self.assertEqual(server.recv().text(), "héllo")
        self.assertEqual(server.recv().data, b"\x01\x02" * 40000)
        server.send_binary(b"x" * 70000)
        self.assertEqual(client.recv().data, b"x" * 70000)

    def test_ping_is_answered_and_close_propagates(self):
        server, client = self.pair()
        client.ping(b"hi")
        client.send_text("after")
        self.assertEqual(server.recv().text(), "after")
        server.close(1000, "bye")
        with self.assertRaises(wsmod.WSClosed) as ctx:
            client.recv()
        self.assertEqual(ctx.exception.code, 1000)

    def test_server_rejects_unmasked_frames(self):
        a, b = socket.socketpair()
        server = wsmod.WebSocket(a, a.makefile("rb"), mask_outgoing=False, require_masked=True)
        b.sendall(bytes([0x81, 0x02]) + b"hi")
        with self.assertRaises(wsmod.WSProtocolError):
            server.recv()

    def test_frame_size_limit(self):
        a, b = socket.socketpair()
        server = wsmod.WebSocket(a, a.makefile("rb"), mask_outgoing=False, require_masked=True, max_frame=10)
        client = wsmod.WebSocket(b, b.makefile("rb"), mask_outgoing=True, require_masked=False)
        client.send_binary(b"y" * 11)
        with self.assertRaises(wsmod.WSProtocolError):
            server.recv()

    def test_accept_key_matches_rfc_example(self):
        self.assertEqual(wsmod.accept_key("dGhlIHNhbXBsZSBub25jZQ=="), "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")
        self.assertTrue(wsmod.valid_client_key(base64.b64encode(os.urandom(16)).decode()))
        self.assertFalse(wsmod.valid_client_key("short"))


# ---------------------------------------------------------------- tracker --
class TrackerTests(unittest.TestCase):
    FRAME = 2560

    def run_frames(self, frames):
        events = []
        now = [1000.0]
        tr = Tracker(emit=events.append, clock=lambda: now[0])
        for loud_in, loud_out, text in frames:
            now[0] += 0.08
            tr.step(in_pcm16=(speech_like(0.08) if loud_in else silence(0.08)), in_wall=now[0],
                    out_level_dbfs=-10.0 if loud_out else -120.0, out_ms=80, text_delta=text)
        return tr, events

    def test_turn_latency_and_first_audio(self):
        frames = [(True, False, "")] * 10 + [(False, False, "")] * 10 + [(False, True, "Hi")] * 8 + \
                 [(False, False, "")] * 15
        tr, events = self.run_frames(frames)
        started = [e for e in events if e["type"] == "agent.speech.started"]
        self.assertEqual(len(started), 1)
        # caller stopped after frame 10 (800 ms); agent started at frame 20 (1600 ms)
        self.assertEqual(started[0]["turn_latency_ms"], 800)
        self.assertIsNotNone(started[0]["first_audio_wall_ms"])
        self.assertIn("transcript.agent.final", [e["type"] for e in events])
        self.assertEqual(tr.summary()["turn_latency_ms"]["count"], 1)

    def test_interruption_is_detected_and_measured(self):
        frames = [(False, True, "long answer")] * 5 + [(True, True, "")] * 3 + [(True, False, "")] * 3 + \
                 [(False, False, "")] * 10
        tr, events = self.run_frames(frames)
        kinds = [e["type"] for e in events]
        self.assertIn("interruption.started", kinds)
        self.assertTrue(next(e for e in events if e["type"] == "interruption.started")["flush"])
        intr = next(e for e in events if e["type"] == "interruption")
        self.assertTrue(intr["yielded"])
        # onset at frame 5 (400 ms, 2-frame confirmation), agent audible until frame 7 -> stopped at 640 ms
        self.assertEqual(intr["latency_ms"], 240)
        stopped = next(e for e in events if e["type"] == "agent.speech.stopped")
        self.assertEqual(stopped["reason"], "interrupted")
        self.assertEqual(tr.summary()["interruptions"], 1)

    def test_agent_that_does_not_yield(self):
        frames = [(False, True, "")] * 3 + [(True, True, "")] * 60
        _, events = self.run_frames(frames)
        intr = [e for e in events if e["type"] == "interruption"]
        self.assertEqual(intr[-1]["yielded"], False)

    def test_user_transcript_finalised_on_reset(self):
        events = []
        tr = Tracker(emit=events.append)
        tr.step(in_pcm16=silence(0.08), in_wall=0, out_level_dbfs=-120, out_ms=80, asr_delta="hello ")
        tr.step(in_pcm16=silence(0.08), in_wall=0, out_level_dbfs=-120, out_ms=80, asr_delta="there")
        tr.step(in_pcm16=silence(0.08), in_wall=0, out_level_dbfs=-120, out_ms=80, asr_delta="next",
                asr_reset=True)
        finals = [e for e in events if e["type"] == "transcript.user.final"]
        self.assertEqual(finals[0]["text"], "hello there")
        tr.finish()
        self.assertEqual([e["text"] for e in events if e["type"] == "transcript.user.final"],
                         ["hello there", "next"])


# ----------------------------------------------------------- engine lifecycle --
class FakeDocker:
    def __init__(self):
        self.containers: dict[str, bool] = {"gx-llama-swap-node02": True}
        self.calls: list[list[str]] = []

    def run(self, args, timeout=120):
        self.calls.append(args)
        if args[0] == "run" and "-d" in args:
            self.containers[args[args.index("--name") + 1]] = True
        elif args[0] in ("stop",):
            self.containers[args[-1]] = False
        elif args[0] == "rm":
            self.containers.pop(args[-1], None)
        return subprocess.CompletedProcess(args, 0, "", "")

    def running(self, name):
        return self.containers.get(name, False)

    def exists(self, name):
        return name in self.containers


class FakeGuard:
    on_admitted = None

    def __init__(self, refuse=False):
        self.refuse = refuse
        self.launched = 0
        self.extra = []
        self.ledger = set()

    def launch(self, start, extra_gib=0.0):
        self.extra.append(extra_gib)
        if self.refuse:
            raise ResourceWait("waiting for memory on gx10-02")
        if self.on_admitted:
            self.on_admitted()
        start()
        self.launched += 1
        self.ledger.add("gx-call-engine")

    def register(self):
        self.ledger.add("gx-call-engine")

    def release(self):
        self.ledger.discard("gx-call-engine")

    def released(self):
        return "gx-call-engine" not in self.ledger


class FakePeers:
    def __init__(self, pending=0.0):
        self.pending = pending

    def pending_gib(self, fresh=True):
        return self.pending


def make_cfg(root: Path, engine_port: int, **overrides) -> config_mod.Config:
    (root / "secrets").mkdir(exist_ok=True)
    (root / "secrets" / "api-key").write_text(KEY)
    env = {
        "GX_CALL_SECRETS_DIR": str(root / "secrets"), "GX_CALL_BINDS": "127.0.0.1", "GX_CALL_PORT": "18840",
        "GX_CALL_STATE_DIR": str(root / "state"), "GX_CALL_DATA_DIR": str(root / "data"),
        "GX_GUARD_STATE_DIR": str(root / "guard"), "GX_CALL_ENGINE_PORT": str(engine_port),
        "GX_CALL_GXMAX_HOLD": str(root / "guard" / "node2.gxmax-hold"),
        "GX_CALL_MAINTENANCE_HOLD": str(root / "guard" / "node2.maintenance-hold"),
        "GX_CALL_PINS_FILE": str(root / "guard" / "pins.json"),
        "GX_CALL_GXMAX_DEADMAN_PID": str(root / "deadman.pid"), "GX_CALL_REJOIN_WINDOW_S": "0",
    }
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        cfg = config_mod.load()
    finally:
        for k, val in old.items():
            if val is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = val
    (root / "guard").mkdir(exist_ok=True)
    return dataclasses.replace(cfg, **overrides) if overrides else cfg


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.stub = EngineStub(None or "e" * 43)
        self.cfg = make_cfg(self.root, self.stub.port)
        (self.cfg.engine_key_file).write_text(self.stub.key + "\n")
        self.docker = FakeDocker()
        self.guard = FakeGuard()
        self.mem = {"MemAvailable": 100.0}
        self.engine = EngineController(self.cfg, docker=self.docker, guard=self.guard, peers=FakePeers(4.0),
                                       mem=lambda: dict(self.mem))
        self.service = CallService(self.cfg, self.engine)

    def tearDown(self):
        self.service.stop()
        self.stub.close()
        self.tmp.cleanup()


class EngineControllerTests(Base):
    def test_load_counts_peer_pending_and_measures_resident(self):
        def fake_ready():
            self.mem["MemAvailable"] = 70.0
            return {"state": "ready"}
        self.engine.engine_health = lambda timeout=3.0: fake_ready()
        self.assertIsNotNone(self.engine.ensure_loaded())
        self.assertEqual(self.guard.extra, [4.0])
        self.assertEqual(self.engine.state, READY)
        self.assertEqual(self.engine.resident_gib, 30.0)
        self.assertIn("--device", self.docker.calls[-1])
        self.assertIn("nvidia.com/gpu=all", self.docker.calls[-1])
        self.assertIn(f"127.0.0.1:{self.cfg.engine_port}:{self.cfg.engine_port}", self.docker.calls[-1])
        self.assertEqual(self.engine.memory_view()["pending_gib"], 0.0)
        info = self.engine.unload("test", kind="manual")
        self.assertTrue(info["container_gone"])
        self.assertTrue(info["ledger_released"])
        self.assertEqual(self.engine.state, UNLOADED)

    def test_pending_memory_while_loading(self):
        self.engine.state = LOADING
        self.engine.admit_avail_gib = 100.0
        self.mem["MemAvailable"] = 90.0
        self.assertEqual(self.engine.pending_gib(), self.cfg.engine_estimate_gib - 10.0)

    def test_refusal_is_a_numeric_wait(self):
        self.guard.refuse = True
        with self.assertRaises(ResourceWait) as ctx:
            self.engine.ensure_loaded()
        self.assertIn("30 GiB reserve", ctx.exception.reason)
        self.assertIn("4 GiB other tenants", ctx.exception.reason)

    def test_gxmax_and_maintenance_block(self):
        self.cfg.gxmax_hold_file.write_text("x")
        with self.assertRaises(ResourceWait) as ctx:
            self.engine.ensure_loaded()
        self.assertEqual(ctx.exception.code, "gx_max_active")
        self.cfg.gxmax_hold_file.unlink()
        self.cfg.maintenance_hold_file.write_text("x")
        self.assertEqual(self.engine.policy_block_reason()[0], "maintenance")
        self.cfg.maintenance_hold_file.unlink()
        self.docker.containers["gx-llama-swap-node02"] = False
        self.assertEqual(self.engine.policy_block_reason()[0], "gx_max_active")

    def test_pin_is_bounded_by_reserve(self):
        self.cfg.pins_file.write_text(json.dumps({"gx-call": {"by": "admin"}}))
        self.assertTrue(self.engine.pin_honoured())
        self.mem["MemAvailable"] = 20.0
        self.assertFalse(self.engine.pin_honoured())

    def test_engine_key_file_is_private(self):
        self.assertEqual(os.stat(self.root / "state" / "engine.env").st_mode & 0o777, 0o600)


# ------------------------------------------------------------ end to end --
class Client:
    """The Playground tunnel's view: HTTP upgrade to gx-call with the bearer key."""

    def __init__(self, port: int, sid: str, token: str, *, key=KEY, header_session=None):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        k = base64.b64encode(os.urandom(16)).decode()
        headers = [f"GET /v1/call/sessions/{sid}/ws?join={token} HTTP/1.1", f"Host: 127.0.0.1:{port}",
                   "Upgrade: websocket", "Connection: Upgrade", f"Sec-WebSocket-Key: {k}",
                   "Sec-WebSocket-Version: 13", f"Authorization: Bearer {key}",
                   f"X-GX-Session: {header_session or sid}"]
        self.sock.sendall(("\r\n".join(headers) + "\r\n\r\n").encode())
        self.rfile = self.sock.makefile("rb")
        self.status = int(self.rfile.readline().split()[1])
        self.headers = {}
        while True:
            line = self.rfile.readline().decode()
            if line in ("\r\n", ""):
                break
            name, _, value = line.partition(":")
            self.headers[name.strip().lower()] = value.strip()
        self.ws = wsmod.WebSocket(self.sock, self.rfile, mask_outgoing=True, require_masked=False) \
            if self.status == 101 else None
        self.events: list[dict] = []
        self.audio = bytearray()
        if self.ws:
            threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            while True:
                m = self.ws.recv()
                if m.is_text:
                    self.events.append(json.loads(m.text()))
                else:
                    self.audio.extend(m.data)
        except (wsmod.WSClosed, OSError):
            pass

    def wait_for(self, kind, timeout=15, **match):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for e in list(self.events):
                if e.get("type") == kind and all(e.get(k) == val for k, val in match.items()):
                    return e
            time.sleep(0.02)
        raise AssertionError(f"no {kind} {match}; got {[e.get('type') for e in self.events]}")

    def stream(self, pcm: bytes, realtime=False):
        for off in range(0, len(pcm), 640):
            self.ws.send_binary(pcm[off:off + 640])
            if realtime:
                time.sleep(0.02)


class ServiceTests(Base):
    def setUp(self):
        super().setUp()
        self.engine.engine_health = lambda timeout=3.0: {"state": "ready"}
        self.servers = build_servers(self.service, KEY, ("127.0.0.1",), 0)
        self.port = self.servers[0].server_address[1]
        for s in self.servers:
            threading.Thread(target=s.serve_forever, daemon=True).start()
        self.service.start()

    def tearDown(self):
        for s in self.servers:
            s.shutdown()
            s.server_close()
        super().tearDown()

    def http(self, method, path, body=None, key=KEY):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=40)
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        conn.request(method, path, json.dumps(body) if body is not None else None, headers)
        res = conn.getresponse()
        data = res.read()
        conn.close()
        return res.status, (json.loads(data) if data and res.getheader("Content-Type") == "application/json"
                            else data)

    def create(self, **kw):
        status, body = self.http("POST", "/v1/call/sessions", session_body(**kw))
        self.assertEqual(status, 201, body)
        token = body["upstream_path"].split("join=")[1]
        return body["session_id"], token

    def test_health_is_open_and_follows_contract(self):
        status, body = self.http("GET", "/health", key=None)
        self.assertEqual(status, 200)
        for k in ("state", "busy", "pinned", "active_sessions", "memory"):
            self.assertIn(k, body)
        for k in ("pending_gib", "resident_gib", "estimate_gib"):
            self.assertIn(k, body["memory"])
        self.assertEqual(self.http("GET", "/v1/call/model", key="wrong" * 8)[0], 401)
        self.assertEqual(self.http("GET", "/v1/call/model", key=None)[0], 401)

    def test_full_call_with_tool_round_trip(self):
        sid, token = self.create(record=True)
        client = Client(self.port, sid, token)
        self.assertEqual(client.status, 101)
        client.wait_for("session.hello")
        client.wait_for("session.ready")
        client.stream(speech_like(0.8) + silence(1.2))
        call = client.wait_for("tool.call")
        self.assertEqual(call["name"], "update_intake_fields")
        # the Control Center answers via HTTP
        page = self.http("GET", f"/v1/call/sessions/{sid}/events?after=0&wait=1")[1]
        self.assertTrue(any(e["type"] == "tool.call" for e in page["events"]))
        status, res = self.http("POST", f"/v1/call/sessions/{sid}/tool-results",
                                {"call_id": call["call_id"], "output": "Saved caller name.",
                                 "event": {"state": {"caller_name": "Jane Doe"}}})
        self.assertEqual(status, 200, res)
        result = client.wait_for("tool.result")
        self.assertEqual(result["state"], {"caller_name": "Jane Doe"})
        deadline = time.time() + 5
        while not self.stub.tool_results and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.stub.tool_results[0]["output"], "Saved caller name.")
        self.assertEqual(self.http("POST", f"/v1/call/sessions/{sid}/tool-results",
                                   {"call_id": call["call_id"], "output": "again"})[0], 409)
        client.wait_for("agent.speech.started")
        self.assertGreater(len(client.audio), 0)
        # state push from the Control Center
        self.assertEqual(self.http("POST", f"/v1/call/sessions/{sid}/events",
                                   {"type": "transfer.updated", "status": "requested"})[0], 200)
        client.wait_for("transfer.updated", status="requested")
        # a second concurrent caller is queued behind the live call
        sid2, token2 = self.create()
        client2 = Client(self.port, sid2, token2)
        client2.wait_for("session.status", state="queued")
        # end the first call
        status, view = self.http("POST", f"/v1/call/sessions/{sid}/end", {"reason": "caller_ended"})
        self.assertEqual(view["state"], "ended")
        self.assertEqual(view["disposition"], "completed")
        self.assertTrue(view["recording"]["ready"])
        ended = client.wait_for("session.ended")
        self.assertIn("summary", ended)
        wav = self.http("GET", f"/v1/call/sessions/{sid}/recording?track=agent")[1]
        self.assertEqual(wav[:4], b"RIFF")
        self.assertEqual(self.http("GET", f"/v1/call/sessions/{sid}/recording?track=both")[0], 400)
        self.assertEqual(self.http("DELETE", f"/v1/call/sessions/{sid}/recording")[0], 200)
        self.assertEqual(self.http("GET", f"/v1/call/sessions/{sid}/recording?track=agent")[0], 409)
        # the queued caller now gets the engine
        client2.wait_for("session.ready", timeout=20)
        self.http("POST", f"/v1/call/sessions/{sid2}/end", {"reason": "done"})
        client2.wait_for("session.ended")
        # nothing about call content was written to disk except the (deleted) recording
        leftovers = [p for p in (self.root / "data").rglob("*") if p.is_file()]
        self.assertEqual(leftovers, [])

    def test_join_is_validated_before_upgrade(self):
        sid, token = self.create()
        self.assertEqual(Client(self.port, sid, "wrong-token").status, 400)
        self.assertEqual(Client(self.port, sid, token, key="x" * 40).status, 401)
        self.assertEqual(Client(self.port, sid, token, header_session="call_" + "0" * 32).status, 400)
        other = "call_" + secrets.token_hex(16)
        self.assertEqual(Client(self.port, other, token).status, 404)
        ok = Client(self.port, sid, token)
        self.assertEqual(ok.status, 101)
        ok.wait_for("session.ready")
        self.assertEqual(Client(self.port, sid, token).status, 409)  # already connected
        ok.ws.send_text(json.dumps({"type": "session.end"}))
        ok.wait_for("session.ended")
        self.assertEqual(Client(self.port, sid, token).status, 409)  # ended

    def test_unload_if_idle_refuses_during_call_and_gxmax_ends_it(self):
        sid, token = self.create()
        client = Client(self.port, sid, token)
        client.wait_for("session.ready")
        status, body = self.http("POST", "/v1/call/unload", {"if_idle": True})
        self.assertEqual(status, 409)
        self.cfg.gxmax_hold_file.write_text("draining")
        self.assertEqual(self.service.reap_once(), "gxmax")
        client.wait_for("session.ended")
        self.assertEqual(self.service.get(sid)["disposition"], "preempted")
        self.assertFalse(self.docker.exists(self.cfg.engine_container))
        status, body = self.http("POST", "/v1/call/sessions", session_body())
        self.assertEqual(status, 503)
        self.assertEqual(body["error"]["code"], "gx_max_active")

    def test_unknown_client_message_and_text_input_are_reported(self):
        sid, token = self.create()
        client = Client(self.port, sid, token)
        client.wait_for("session.ready")
        client.ws.send_text(json.dumps({"type": "hack"}))
        self.assertEqual(client.wait_for("error")["code"], "unknown_message")
        client.ws.send_text(json.dumps({"type": "input.text", "text": "hello"}))
        client.wait_for("error", code="text_input_unsupported")
        client.ws.send_text(json.dumps({"type": "ping"}))
        client.wait_for("pong")
        client.ws.close()
        deadline = time.time() + 10
        while self.service.get(sid)["state"] != "ended" and time.time() < deadline:
            time.sleep(0.1)
        self.assertEqual(self.service.get(sid)["state"], "ended")

    def test_idle_unload_respects_pin(self):
        self.engine.ensure_loaded()
        self.engine.last_activity = time.time() - self.cfg.idle_unload_s - 5
        self.cfg.pins_file.write_text(json.dumps({"gx-call": {}}))
        self.assertEqual(self.service.reap_once(), "pinned")
        self.cfg.pins_file.write_text("{}")
        self.assertEqual(self.service.reap_once(), "idle")
        self.assertEqual(self.engine.state, UNLOADED)

    def test_session_limits(self):
        cfg = dataclasses.replace(self.cfg, max_sessions_pending=1)
        self.service.cfg = cfg
        self.create()
        status, body = self.http("POST", "/v1/call/sessions", session_body())
        self.assertEqual(status, 503)
        self.assertEqual(body["error"]["code"], "too_many_sessions")
        status, body = self.http("POST", "/v1/call/sessions", {"session_id": "x"})
        self.assertEqual(status, 400)

    def test_load_refusal_marks_waiting_sessions(self):
        self.guard.refuse = True
        self.service.cfg = dataclasses.replace(self.cfg, resource_wait_s=3, resource_retry_s=2)
        sid, token = self.create()
        client = Client(self.port, sid, token)
        self.assertEqual(client.status, 101)
        waiting = client.wait_for("session.status", state="waiting")
        self.assertIn("30 GiB reserve", waiting["detail"])
        err = client.wait_for("error", timeout=20)
        self.assertEqual(err["code"], "insufficient_memory")
        client.wait_for("session.ended")


class ServiceUnitTests(unittest.TestCase):
    def test_errors_are_user_safe(self):
        e = UnavailableError("the queue is full", code="queue_full")
        self.assertEqual(e.payload()["error"], {"code": "queue_full", "message": "the queue is full",
                                                 "retryable": True})
        self.assertEqual(ConflictError("x").status, 409)


if __name__ == "__main__":
    unittest.main()
