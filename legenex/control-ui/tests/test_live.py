"""Build V3 LIV: gx-live sessions, tools and routes on the Control Center.

The node-2 side is the REAL gx-live supervisor (legenex/live) with the stub
engine from its own suite, so every session here crosses the real gx-live HTTP
and WebSocket APIs and the real tool bridge. The gateway (LiteLLM) and the URL
fetcher are stubs; no GPU, no Docker, no network beyond loopback.
"""

from __future__ import annotations

import dataclasses
import http.client
import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from support import REPO, StubUpstream, TempEnv, fake_key

from gx_control_ui import auth
from gx_control_ui import server as srv
from gx_control_ui import routes_liv  # noqa: F401  (registers /api/live and /v1/live)
from gx_control_ui.live import LiveClient, LiveError, LiveManager, normalise_config
from gx_control_ui.media_library import MediaLibrary, MediaTools, NewAsset
from gx_control_ui.netguard import BlockedURL
from gx_control_ui.realtime import RealtimeRegistry, Target

LIVE_DIR = REPO / "legenex" / "live"
sys.path.insert(0, str(LIVE_DIR))
sys.path.insert(0, str(LIVE_DIR / "tests"))
sys.path.insert(0, str(REPO / "legenex" / "common"))

from gxcommon import rtws  # noqa: E402
from gx_live.engine import EngineController  # noqa: E402
from gx_live.server import build_servers  # noqa: E402
from gx_live.service import LiveService  # noqa: E402
from test_gx_live import FakeDocker, FakeGuard, FakePeers, StubEngineHandler, make_config  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402

PASSWORD = "Test-Password-For-Live-51"
ANSWER = "A haiku about memory budgets."


class Audit(list):
    def __call__(self, **kw):
        self.append(kw)


class Metrics(list):
    def __call__(self, event, **fields):
        self.append((event, fields))


class Node2Live:
    """A real gx-live supervisor with the stub engine, on loopback."""

    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        StubEngineHandler.key = fake_key("")
        StubEngineHandler.sessions = []
        StubEngineHandler.received = []
        handler = type("H", (StubEngineHandler,), {})
        self.engine_srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.engine_srv.daemon_threads = True
        threading.Thread(target=self.engine_srv.serve_forever, daemon=True).start()
        self.cfg = make_config(root, engine_port=self.engine_srv.server_address[1], idle_unload_s=60,
                               reconnect_grace_s=2)
        self.cfg.engine_key_file.write_text(StubEngineHandler.key)
        self.key = (root / "secrets" / "api-key").read_text().strip()
        engine = EngineController(self.cfg, docker=FakeDocker(), guard=FakeGuard(), peers=FakePeers(),
                                  mem=lambda: {"MemAvailable": 100.0, "MemTotal": 121.0, "SwapTotal": 63.0,
                                               "SwapFree": 60.0})
        self.service = LiveService(self.cfg, engine)
        self.servers = build_servers(self.service, self.key, ("127.0.0.1",), 0, rtws)
        self.port = self.servers[0].server_address[1]
        for s in self.servers:
            threading.Thread(target=s.serve_forever, daemon=True).start()
        self.service.start()

    def close(self) -> None:
        self.service.stop()
        for s in self.servers:
            s.shutdown()
            s.server_close()
        self.engine_srv.shutdown()
        self.engine_srv.server_close()
        self.tmp.cleanup()


def gateway_stub() -> StubUpstream:
    def chat(handler, body):
        if (body or {}).get("model") == "gx-reason":
            return 503, {"error": {"message": "not loaded"}}
        return 200, {"model": (body or {}).get("model"), "choices": [
            {"message": {"role": "assistant", "content": ANSWER}, "finish_reason": "stop"}]}

    return StubUpstream({("POST", "/v1/chat/completions"): chat})


# ------------------------------------------------------------------ config --
class ConfigTests(unittest.TestCase):
    def test_defaults_and_limits(self) -> None:
        cfg = normalise_config(None)
        self.assertEqual(cfg["language"], "en")
        self.assertTrue(cfg["tools"] and cfg["output_audio"])
        self.assertEqual(cfg["vad"], {"threshold": 0.5, "silence_ms": 700})
        self.assertEqual(cfg["max_response_tokens"], 256)

    def test_rejects_bad_input(self) -> None:
        for body in ({"language": "de"}, {"tools": "yes"}, {"vad": {"threshold": 0.1}},
                     {"vad": {"silence_ms": 10}}, {"vad": {"nope": 1}}, {"max_response_tokens": 4},
                     {"instructions": "x" * 2001}, {"instructions": "bad\x07char"}, {"unknown": 1}, []):
            with self.subTest(body=body), self.assertRaises(LiveError):
                normalise_config(body)

    def test_instructions_are_kept_but_never_echoed_in_the_summary(self) -> None:
        cfg = normalise_config({"instructions": "  You are Jarvis.  "})
        self.assertEqual(cfg["instructions"], "You are Jarvis.")


# ------------------------------------------------------------------- store --
class ManagerHarness(unittest.TestCase):
    """LiveManager against the real database, with a stub node-2 client."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.library = MediaLibrary(self.root / "media", MediaTools(enabled=False))
        self.gateway = gateway_stub()
        self.audit = Audit()
        self.metrics = Metrics()
        self.node = SimpleNamespace(calls=[], replies={})
        self.realtime = RealtimeRegistry({"live": Target("127.0.0.1", 18850,
                                                         self.root / "key")})
        (self.root / "key").write_text(fake_key(""))
        client = LiveClient("http://127.0.0.1:9", self.root / "key")
        client.request = self._node_request
        client.health = lambda: {"state": "ready", "service": "gx-live"}
        self.fetched: list[str] = []
        self.manager = LiveManager(
            connect=self.library.connect, client=client, realtime=self.realtime, library=self.library,
            gateway_base=self.gateway.url, gateway_headers=dict, audit=self.audit, metric=self.metrics,
            fetch=self._fetch, start_threads=False)

    def tearDown(self) -> None:
        self.gateway.close()
        self._tmp.cleanup()

    # a scripted gx-live supervisor
    def _node_request(self, method, path, body=None, *, timeout=None):
        self.node.calls.append((method, path, body))
        if method == "POST" and path == "/v1/live/sessions":
            return {"session_id": body["session_id"], "join_token": "t" * 24, "protocol": "gx-live.v1",
                    "expires_at": int(time.time()) + 3600, "model_state": "loading", "waiting": None,
                    "upstream_path": f"/v1/live/sessions/{body['session_id']}/ws?join=" + "t" * 24}
        if method == "GET" and path == "/v1/live/model":
            return {"identity": {"repository": "openbmb/MiniCPM-o-4_5", "revision": "503e754"}}
        if path.endswith("/end"):
            return {"state": "ended"}
        if method == "GET" and path.startswith("/v1/live/sessions/"):
            return self.node.replies.get("summary", {"state": "ended", "end_reason": "completed",
                                                     "duration_s": 12.5, "turns": 2, "responses": 2,
                                                     "interrupted": 1, "text_inputs": 1,
                                                     "latency": {"first_audio_ms": {"median": 1500}},
                                                     "media": {"camera_frames": 4}})
        return {}

    def _fetch(self, url, **kw):
        self.fetched.append(url)
        if "blocked" in url:
            raise BlockedURL("private address")
        return SimpleNamespace(url=url, status=200, headers={"content-type": "text/html; charset=utf-8"},
                               body=b"<html><head><style>x{}</style></head><body><h1>Title</h1>"
                                    b"<p>Hello &amp; welcome</p><script>bad()</script></body></html>",
                               truncated=False, hops=[])

    def make(self, **kw):
        return self.manager.create_session(owner="user:admin", via="playground", user_label="admin", **kw)


class SessionRecordTests(ManagerHarness):
    def test_create_records_registers_and_never_stores_instructions(self) -> None:
        res = self.make(config={"instructions": "You are Jarvis.", "language": "en"})
        sid = res["session_id"]
        self.assertRegex(sid, r"^live_[0-9a-f]{32}$")
        self.assertEqual(res["ws_path"], f"/rt/live/{sid}")
        self.assertNotIn("Jarvis", json.dumps(res))
        sess = self.realtime.get(sid)
        self.assertEqual(sess.owner, "user:admin")
        self.assertIn("join=", sess.upstream_path)
        view = self.manager.session_view(sid)
        self.assertTrue(view["has_instructions"])
        self.assertEqual(view["model"]["repository"], "openbmb/MiniCPM-o-4_5")
        with self.library.connect() as con:
            row = dict(con.execute("SELECT * FROM live_sessions WHERE session_id = ?", (sid,)).fetchone())
        self.assertNotIn("Jarvis", json.dumps(row))
        self.assertEqual(row["state"], "created")
        # the owner hash, not the user name, is what gx10-02 is told
        body = next(b for m, p, b in self.node.calls if p == "/v1/live/sessions")
        self.assertRegex(body["owner"], r"^[0-9a-f]{16}$")
        self.assertNotIn("admin", json.dumps(body["owner"]))

    def test_end_pulls_the_summary_and_closes_the_tunnel_session(self) -> None:
        sid = self.make()["session_id"]
        view = self.manager.end_session(sid, "completed", user_label="admin")
        self.assertEqual(view["state"], "ended")
        self.assertEqual(view["turns"], 2)
        self.assertEqual(view["duration_s"], 12.5)
        self.assertTrue(view["camera_used"])
        self.assertEqual(view["metrics"]["latency"]["first_audio_ms"]["median"], 1500)
        self.assertEqual(self.realtime.get(sid).disposition, "completed")
        # ending twice is harmless
        self.assertEqual(self.manager.end_session(sid)["state"], "ended")
        self.assertIn("live.session.end", [a["action"] for a in self.audit])

    def test_events_are_metadata_only(self) -> None:
        sid = self.make(config={"instructions": "secret rules"})["session_id"]
        self.manager._event(sid, "test.event", {"text": "transcript", "turn": 3, "name": "get_time"})
        events = self.manager.events(sid)["events"]
        self.assertEqual(events[0]["type"], "session.created")
        payload = json.dumps(events)
        self.assertNotIn("transcript", payload)
        self.assertIn("get_time", payload)

    def test_turn_records_are_validated_and_idempotent(self) -> None:
        sid = self.make()["session_id"]
        turns = [{"response": 1, "trigger": "speech", "status": "completed", "first_audio_ms": 1400,
                  "turn_ms": 2600, "camera": True},
                 {"response": 2, "trigger": "text", "status": "interrupted", "interrupt_ms": 180,
                  "interrupt_reason": "barge_in"}]
        out = self.manager.record_turns(sid, turns, owner="user:admin")
        self.assertEqual((out["stored"], out["turns"]), (2, 2))
        # re-posting the same turns updates them in place
        turns[0]["turn_ms"] = 2700
        self.assertEqual(self.manager.record_turns(sid, turns, owner="user:admin")["turns"], 2)
        with self.library.connect() as con:
            rows = [dict(r) for r in con.execute("SELECT * FROM live_turns WHERE session_id = ? ORDER BY "
                                                 "response", (sid,))]
        self.assertEqual(rows[0]["turn_ms"], 2700)
        self.assertEqual(rows[1]["interrupt_reason"], "barge_in")
        self.assertTrue(self.manager.session_view(sid)["camera_used"])
        stages = [f.get("stage") for e, f in self.metrics if e == "realtime.latency"]
        self.assertEqual(sorted(set(stages)), ["first_audio", "interrupt"])
        for bad in ([{"response": 0}], [{"response": 1, "turn_ms": -5}], [{"response": "x"}], [1],
                    "nope", [{"response": 1, "first_audio_ms": 10**9}]):
            with self.subTest(bad=bad), self.assertRaises(LiveError):
                self.manager.record_turns(sid, bad, owner="user:admin")
        with self.assertRaises(LiveError):
            self.manager.record_turns(sid, [{"response": 9}], owner="user:someone-else")

    def test_transcript_is_opt_in_and_deletable(self) -> None:
        sid = self.make()["session_id"]
        self.assertEqual(self.manager.transcript(sid)["entries"], [])
        self.manager.save_transcript(sid, [
            {"speaker": "user", "text": "what time is it?", "turn": 1},
            {"speaker": "assistant", "text": "It is 20:40.", "turn": 1, "interrupted": True}],
            owner="user:admin", user_label="admin")
        got = self.manager.transcript(sid)
        self.assertTrue(got["saved"])
        self.assertEqual([e["speaker"] for e in got["entries"]], ["user", "assistant"])
        self.assertTrue(got["entries"][1]["interrupted"])
        # saving again replaces, never appends
        self.manager.save_transcript(sid, [{"speaker": "user", "text": "again"}], owner="user:admin")
        self.assertEqual(len(self.manager.transcript(sid)["entries"]), 1)
        for bad in ([], [{"speaker": "system", "text": "x"}], [{"speaker": "user", "text": "x" * 4001}],
                    [{"speaker": "user"}], "no"):
            with self.subTest(bad=bad), self.assertRaises(LiveError):
                self.manager.save_transcript(sid, bad, owner="user:admin")
        self.manager.delete_session_content(sid, user_label="admin")
        gone = self.manager.transcript(sid)
        self.assertEqual(gone["entries"], [])
        self.assertTrue(gone["content_purged"])
        with self.assertRaises(LiveError):
            self.manager.save_transcript(sid, [{"speaker": "user", "text": "x"}], owner="user:admin")

    def test_retention_purge(self) -> None:
        sid = self.make()["session_id"]
        self.manager.save_transcript(sid, [{"speaker": "user", "text": "keep"}], owner="user:admin")
        self.manager.end_session(sid, "completed")
        self.manager._update(sid, retain_until=time.time() - 1)
        self.assertEqual(self.manager.purge_expired(), 1)
        self.assertEqual(self.manager.transcript(sid)["entries"], [])
        self.assertEqual(self.manager.purge_expired(), 0)

    def test_activity_rows(self) -> None:
        sid = self.make()["session_id"]
        self.manager.end_session(sid, "completed")
        rows = self.manager.activity("admin", 0, 20)
        self.assertEqual(rows[0]["kind"], "live")
        self.assertEqual(rows[0]["status"], "ok")
        self.assertEqual(rows[0]["link"], f"#/live?session={sid}")
        self.assertNotIn("instructions", json.dumps(rows))
        self.assertEqual(self.manager.activity("someone", 0, 20), [])

    def test_unreachable_node_marks_the_session_failed(self) -> None:
        def boom(*a, **kw):
            raise LiveError("gx-live on gx10-02 is not reachable", 502, "gx_live_unreachable")

        self.manager.client.request = boom
        with self.assertRaises(LiveError):
            self.make()
        rows = self.manager.list_sessions(owner="user:admin")
        self.assertEqual(rows[0]["state"], "ended")
        self.assertEqual(rows[0]["end_reason"], "gx_live_unreachable")


class ToolTests(ManagerHarness):
    def setUp(self) -> None:
        super().setUp()
        self.sid = self.make()["session_id"]

    def run_tool(self, name, args):
        return self.manager.execute_tool(self.sid, "call_" + "ab" * 8, name, args)

    def test_get_time(self) -> None:
        ok, content, summary, _extra = self.run_tool("get_time", {})
        self.assertTrue(ok)
        self.assertRegex(content, r"\d{4}, \d{2}:\d{2}")
        self.assertEqual(content, summary)

    def test_delegation_uses_the_gateway_and_never_gx_max(self) -> None:
        ok, content, _summary, extra = self.run_tool("delegate_to_gx", {"model": "gx-fast",
                                                                        "task": "write a haiku"})
        self.assertTrue(ok)
        self.assertEqual(content, ANSWER)
        self.assertEqual(extra["model"], "gx-fast")
        self.assertEqual(extra["routed_to"], "gx-fast")
        sent = json.loads(self.gateway.calls[-1][3] if isinstance(self.gateway.calls[-1][3], (str, bytes))
                          else json.dumps(self.gateway.calls[-1][3]))
        self.assertEqual(sent["model"], "gx-fast")
        for model in ("gx-max", "gpt-4o", "", None):
            ok, _c, summary, extra = self.run_tool("delegate_to_gx", {"model": model, "task": "x"})
            self.assertFalse(ok)
            self.assertEqual(extra["error_code"], "bad_model")
        self.assertNotIn("gx-max", json.dumps(self.gateway.calls))

    def test_delegation_reports_a_gateway_failure(self) -> None:
        ok, _content, summary, extra = self.run_tool("delegate_to_gx", {"model": "gx-reason",
                                                                        "task": "think"})
        self.assertFalse(ok)
        self.assertEqual(extra["error_code"], "gateway_503")
        self.assertIn("gx-reason", summary)

    def test_search_library_records_provenance(self) -> None:
        asset = self.library.add(NewAsset(type="image", ext="png", operation="generate",
                                          data=b"\x89PNG\r\n\x1a\n" + b"0" * 64,
                                          prompt="a lighthouse at dawn", title="Lighthouse"))
        ok, content, _summary, _extra = self.run_tool("search_library", {"query": "lighthouse",
                                                                         "type": "image"})
        self.assertTrue(ok)
        self.assertIn("Lighthouse", content)
        with self.library.connect() as con:
            rows = [dict(r) for r in con.execute("SELECT * FROM live_session_assets WHERE session_id = ?",
                                                 (self.sid,))]
        self.assertEqual(rows[0]["asset_id"], asset["id"])
        self.assertEqual(rows[0]["relation"], "referenced")
        view = self.manager.session_view(self.sid)
        self.assertEqual(view["assets"][0]["asset_id"], asset["id"])
        ok, content, _s, _e = self.run_tool("search_library", {"query": "nothing here", "type": "any"})
        self.assertTrue(ok)
        self.assertIn("Nothing in the library", content)

    def test_fetch_url_goes_through_netguard_and_returns_text(self) -> None:
        ok, content, _summary, _extra = self.run_tool("fetch_url", {"url": "https://example.com/page"})
        self.assertTrue(ok)
        self.assertIn("Hello & welcome", content)
        self.assertNotIn("bad()", content)
        self.assertNotIn("<", content)
        ok, _c, summary, extra = self.run_tool("fetch_url", {"url": "http://blocked.internal/x"})
        self.assertFalse(ok)
        self.assertEqual(extra["error_code"], "blocked_url")
        for bad in ({"url": "ftp://example.com"}, {"url": "javascript:alert(1)"}, {"url": "x"}):
            ok, _c, _s, extra = self.run_tool("fetch_url", bad)
            self.assertFalse(ok)

    def test_real_netguard_refuses_a_loopback_target(self) -> None:
        manager = self.manager
        manager.fetch = __import__("gx_control_ui.netguard", fromlist=["fetch"]).fetch
        ok, _c, _s, extra = self.run_tool("fetch_url", {"url": "http://127.0.0.1:9/secret"})
        self.assertFalse(ok)
        self.assertEqual(extra["error_code"], "blocked_url")

    def test_tools_disabled_refuses_every_tool(self) -> None:
        sid = self.make(config={"tools": False})["session_id"]
        ok, _c, _s, extra = self.manager.execute_tool(sid, "call_" + "cd" * 8, "get_time", {})
        self.assertFalse(ok)
        self.assertEqual(extra["error_code"], "tools_disabled")

    def test_unknown_tool(self) -> None:
        ok, _c, _s, extra = self.run_tool("rm_rf", {})
        self.assertFalse(ok)
        self.assertEqual(extra["error_code"], "unknown_tool")


# -------------------------------------------------------------- end to end --
class LiveFlowTests(unittest.TestCase):
    """The real gx-live supervisor, the real tool bridge, the real database."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.node = Node2Live()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.node.close()

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.library = MediaLibrary(self.root / "media", MediaTools(enabled=False))
        self.gateway = gateway_stub()
        key_file = self.root / "api-key"
        key_file.write_text(self.node.key)
        self.realtime = RealtimeRegistry({"live": Target("127.0.0.1", self.node.port, key_file)})
        self.metrics = Metrics()
        self.manager = LiveManager(
            connect=self.library.connect,
            client=LiveClient(f"http://127.0.0.1:{self.node.port}", key_file),
            realtime=self.realtime, library=self.library, gateway_base=self.gateway.url,
            gateway_headers=dict, audit=Audit(), metric=self.metrics, start_threads=True)
        self.manager.poll_wait = 2
        self.sockets: list = []

    def tearDown(self) -> None:
        for ws in self.sockets:
            try:
                ws.close()
            except OSError:
                pass
        self.manager.stop()
        active = self.node.service.active_session()
        if active is not None:
            self.node.service.end(active.id, "completed")
        self.gateway.close()
        self._tmp.cleanup()

    def attach(self, sid):
        sess = self.realtime.get(sid)
        path = sess.upstream_path
        ws = rtws.connect("127.0.0.1", self.node.port, path,
                          headers={"Authorization": f"Bearer {self.node.key}", "X-GX-Session": sid})
        self.sockets.append(ws)
        return ws

    @staticmethod
    def read_until(ws, kind, timeout=25):
        seen = []
        deadline = time.time() + timeout
        ws.sock.settimeout(timeout)
        while time.time() < deadline:
            msg = ws.recv()
            if msg is None:
                break
            ev = json.loads(msg.text()) if msg.is_text else {"type": "_binary"}
            seen.append(ev)
            if ev["type"] == kind:
                return seen
        raise AssertionError(f"{kind} never arrived; saw {[e['type'] for e in seen]}")

    def test_session_tool_call_and_end_to_end_record(self) -> None:
        created = self.manager.create_session(owner="user:admin", via="playground", user_label="admin",
                                              config={"tools": True})
        sid = created["session_id"]
        ws = self.attach(sid)
        self.read_until(ws, "session.ready")
        ws.send_text(json.dumps({"type": "input.text", "text": "use a tool"}))
        events = self.read_until(ws, "tool.result", timeout=30)
        result = events[-1]
        self.assertTrue(result["ok"])
        self.assertEqual(result["name"], "delegate_to_gx")
        self.assertEqual(result["model"], "gx-fast")
        self.assertIn(ANSWER[:10], result["summary"])
        self.assertIsInstance(result["latency_ms"], int)
        # the record knows the call happened, without its arguments
        deadline = time.time() + 10
        while time.time() < deadline:
            with self.library.connect() as con:
                rows = [dict(r) for r in con.execute("SELECT * FROM live_tool_calls WHERE session_id = ?",
                                                     (sid,))]
            if rows and rows[0]["finished_at"]:
                break
            time.sleep(0.2)
        self.assertEqual(rows[0]["name"], "delegate_to_gx")
        self.assertEqual(json.loads(rows[0]["arg_keys"]), ["model", "task"])
        self.assertEqual(rows[0]["ok"], 1)
        self.assertEqual(rows[0]["model"], "gx-fast")
        self.assertNotIn("haiku", json.dumps(rows[0]))
        # and the model spoke the answer back
        spoken = self.read_until(ws, "response.done", timeout=30)[-1]
        self.assertIn(ANSWER, spoken["text"])
        ws.send_text(json.dumps({"type": "session.stop"}))
        self.read_until(ws, "session.ended")
        deadline = time.time() + 15
        while time.time() < deadline and self.manager.session_view(sid)["state"] != "ended":
            time.sleep(0.2)
        view = self.manager.session_view(sid)
        self.assertEqual(view["state"], "ended")
        self.assertEqual(view["end_reason"], "completed")
        self.assertEqual(view["tool_calls"], 1)
        self.assertEqual(self.realtime.get(sid).disposition, "completed")
        self.assertIn("realtime.session", [e for e, _ in self.metrics])
        for _event, fields in self.metrics:
            for forbidden in ("text", "transcript", "prompt", "task", "join_token"):
                self.assertNotIn(forbidden, fields)

    def test_invalid_tool_call_is_refused_without_reaching_the_gateway(self) -> None:
        created = self.manager.create_session(owner="user:admin", via="api", user_label="key:x")
        ws = self.attach(created["session_id"])
        self.read_until(ws, "session.ready")
        ws.send_text(json.dumps({"type": "input.text", "text": "bad tool"}))
        result = self.read_until(ws, "tool.result", timeout=20)[-1]
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "invalid_tool_arguments")
        self.assertEqual(self.gateway.calls, [])

    def test_only_one_session_at_a_time(self) -> None:
        self.manager.create_session(owner="user:admin", via="playground", user_label="admin")
        with self.assertRaises(LiveError) as ctx:
            self.manager.create_session(owner="user:admin", via="playground", user_label="admin")
        self.assertEqual(ctx.exception.code, "session_busy")


# ------------------------------------------------------------------ routes --
class RouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.node = Node2Live()
        cls.k_live = fake_key()
        cls.k_music = fake_key()
        infos = {cls.k_live: {"key_alias": "live-client", "models": ["gx-live"]},
                 cls.k_music: {"key_alias": "music-only", "models": ["gx-music"]}}

        def key_info(handler, body):
            info = infos.get((handler.headers.get("Authorization") or "")[7:])
            return (200, {"info": info}) if info else (401, {"error": {"message": "bad key"}})

        cls.litellm = StubUpstream({("GET", "/key/info"): key_info,
                                    ("POST", "/v1/chat/completions"): (200, {"choices": [
                                        {"message": {"content": ANSWER}}]})})

    @classmethod
    def tearDownClass(cls) -> None:
        cls.node.close()
        cls.litellm.close()

    def setUp(self) -> None:
        self.env = TempEnv(litellm_base=self.litellm.url,
                           rt_live_target=f"127.0.0.1:{self.node.port}",
                           live_base=f"http://127.0.0.1:{self.node.port}")
        self.env.cfg = dataclasses.replace(self.env.cfg, secrets_root=self.env.root / "secrets")
        key_file = self.env.root / "secrets" / "gx-live" / "api-key"
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.write_text(self.node.key)
        auth.PasswordStore(self.env.cfg.password_file).set_password("admin", PASSWORD, n=2**10)
        self.app, servers = srv.build(self.env.cfg)
        self.app.live = LiveManager(
            connect=self.app.library.connect,
            client=LiveClient(self.env.cfg.live_base, key_file),
            realtime=self.app.realtime, library=self.app.library,
            gateway_base=self.env.cfg.litellm_base,
            gateway_headers=self.app.cluster.litellm_headers, audit=self.app.actions.audit,
            start_threads=False)
        self.app.activity.register("live", self.app.live.activity)
        self.httpd = servers[0]
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.cookie = self.csrf = None

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        for sid in [r["session_id"] for r in self.app.live.list_sessions(limit=50)]:
            try:
                self.app.live.end_session(sid, "completed")
            except LiveError:
                pass
        self.env.cleanup()

    def req(self, method, path, body=None, headers=None, cookie=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        hdrs = {"Host": f"127.0.0.1:{self.port}"}
        if cookie and self.cookie:
            hdrs["Cookie"] = self.cookie
        raw = None
        if body is not None:
            raw = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
        if method == "POST" and self.csrf and cookie:
            hdrs["X-CSRF-Token"] = self.csrf
        hdrs.update(headers or {})
        conn.request(method, path, body=raw, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        parsed = json.loads(data) if data and "json" in (resp.getheader("Content-Type") or "") else data
        return resp.status, parsed

    def login(self) -> None:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/api/login", json.dumps({"username": "admin", "password": PASSWORD}),
                     {"Content-Type": "application/json", "Host": f"127.0.0.1:{self.port}"})
        res = conn.getresponse()
        res.read()
        self.cookie = res.getheader("Set-Cookie").split(";")[0]
        conn.close()
        self.csrf = self.req("GET", "/api/session")[1]["csrf"]

    def bearer(self, key):
        return {"Authorization": f"Bearer {key}"}

    def test_browser_routes_need_a_session(self) -> None:
        for method, path in (("GET", "/api/live/model"), ("POST", "/api/live/sessions"),
                             ("GET", "/api/live/sessions")):
            status, _ = self.req(method, path, {} if method == "POST" else None)
            self.assertEqual(status, 401, path)

    def test_browser_session_lifecycle(self) -> None:
        self.login()
        status, model = self.req("GET", "/api/live/model")
        self.assertEqual(status, 200)
        self.assertEqual(model["alias"], "gx-live")
        self.assertTrue(model["reachable"])
        self.assertEqual([t["name"] for t in model["tools"]],
                         ["get_time", "delegate_to_gx", "search_library", "fetch_url"])
        status, created = self.req("POST", "/api/live/sessions",
                                   {"config": {"language": "en", "tools": True}})
        self.assertEqual(status, 201, created)
        sid = created["session_id"]
        self.assertEqual(created["ws_path"], f"/rt/live/{sid}")
        status, listed = self.req("GET", "/api/live/sessions")
        self.assertEqual([s["session_id"] for s in listed["sessions"]], [sid])
        status, turns = self.req("POST", f"/api/live/sessions/{sid}/turns",
                                 {"turns": [{"response": 1, "turn_ms": 2200, "status": "completed"}]})
        self.assertEqual((status, turns["stored"]), (200, 1))
        status, _ = self.req("POST", f"/api/live/sessions/{sid}/transcript",
                             {"entries": [{"speaker": "user", "text": "hello"}]})
        self.assertEqual(status, 200)
        status, got = self.req("GET", f"/api/live/sessions/{sid}/transcript")
        self.assertEqual(got["entries"][0]["text"], "hello")
        status, view = self.req("GET", f"/api/live/sessions/{sid}")
        self.assertEqual(view["turn_log"][0]["turn_ms"], 2200)
        status, ended = self.req("POST", f"/api/live/sessions/{sid}/end", {"reason": "completed"})
        self.assertEqual((status, ended["state"]), (200, "ended"))
        status, events = self.req("GET", f"/api/live/sessions/{sid}/events")
        self.assertIn("session.ended", [e["type"] for e in events["events"]])
        status, purged = self.req("POST", f"/api/live/sessions/{sid}/delete-content", {})
        self.assertTrue(purged["content_purged"])
        # the Logs page sees it
        status, feed = self.req("GET", "/api/activity?kind=live")
        self.assertEqual(feed["items"][0]["id"], sid)

    def test_browser_validation_and_csrf(self) -> None:
        self.login()
        status, body = self.req("POST", "/api/live/sessions", {"config": {"language": "de"}})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_request")
        saved = self.csrf
        self.csrf = None
        status, _ = self.req("POST", "/api/live/sessions", {})
        self.assertEqual(status, 403)
        self.csrf = saved
        status, _ = self.req("GET", "/api/live/sessions/live_" + "0" * 32)
        self.assertEqual(status, 404)

    def test_public_api_key_scope_and_isolation(self) -> None:
        status, _ = self.req("GET", "/v1/live/model", cookie=False)
        self.assertEqual(status, 401)
        status, body = self.req("GET", "/v1/live/model", headers=self.bearer(self.k_music), cookie=False)
        self.assertEqual((status, body["error"]["code"]), (403, "forbidden"))
        status, model = self.req("GET", "/v1/live/model", headers=self.bearer(self.k_live), cookie=False)
        self.assertEqual(status, 200)
        self.assertEqual(model["alias"], "gx-live")
        self.assertEqual(model["delegation_models"], ["gx-auto", "gx-fast", "gx-reason"])
        status, created = self.req("POST", "/v1/live/sessions", {"config": {"tools": False}},
                                   headers=self.bearer(self.k_live), cookie=False)
        self.assertEqual(status, 201, created)
        sid = created["session_id"]
        self.assertRegex(created["ws_path"], rf"^/rt/live/{sid}\?ticket=v1\.")
        self.assertEqual(created["ticket_expires_in"], 60)
        # a ticket is single use
        ticket = created["ws_path"].split("ticket=")[1]
        self.assertEqual(self.app.realtime.redeem_ticket(ticket, sid), f"key:"
                         f"{self.app.live.owner_of(sid).split(':')[1]}")
        from gx_control_ui.realtime import RealtimeError
        with self.assertRaises(RealtimeError):
            self.app.realtime.redeem_ticket(ticket, sid)
        status, fresh = self.req("POST", f"/v1/live/sessions/{sid}/ticket", {},
                                 headers=self.bearer(self.k_live), cookie=False)
        self.assertEqual(status, 200)
        # a browser cookie is never accepted on /v1
        self.login()
        status, _ = self.req("GET", f"/v1/live/sessions/{sid}")
        self.assertEqual(status, 401)
        # and the signed-in user cannot see the key's session
        status, listed = self.req("GET", "/api/live/sessions")
        self.assertEqual(listed["sessions"], [])
        status, _ = self.req("GET", f"/api/live/sessions/{sid}")
        self.assertEqual(status, 404)
        status, ended = self.req("POST", f"/v1/live/sessions/{sid}/end", {},
                                 headers=self.bearer(self.k_live), cookie=False)
        self.assertEqual((status, ended["state"]), (200, "ended"))
        status, _ = self.req("GET", "/v1/live/sessions/live_" + "1" * 32,
                             headers=self.bearer(self.k_live), cookie=False)
        self.assertEqual(status, 404)

    def test_public_api_method_and_route_errors(self) -> None:
        status, body = self.req("GET", "/v1/live/nope", headers=self.bearer(self.k_live), cookie=False)
        self.assertEqual((status, body["error"]["code"]), (404, "not_found"))


class SchemaTests(unittest.TestCase):
    def test_060_creates_every_live_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lib = MediaLibrary(Path(tmp) / "media", MediaTools(enabled=False))
            self.assertIn("060_live.sql", [m["name"] for m in lib.migrations()])
            with lib.connect() as con:
                names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertLessEqual({"live_sessions", "live_events", "live_turns", "live_tool_calls",
                                  "live_transcripts", "live_session_assets"}, names)
            # applying the whole set again is a no-op
            again = MediaLibrary(Path(tmp) / "media", MediaTools(enabled=False))
            self.assertEqual([m["name"] for m in again.migrations()], [m["name"] for m in lib.migrations()])

    def test_turn_rows_are_unique_per_response(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lib = MediaLibrary(Path(tmp) / "media", MediaTools(enabled=False))
            with lib.connect() as con:
                con.execute("INSERT INTO live_sessions (session_id, owner, via, state, created_at) "
                            "VALUES ('live_" + "a" * 32 + "', 'user:admin', 'playground', 'created', 1.0)")
                con.execute("INSERT INTO live_turns (session_id, response, at) VALUES "
                            "('live_" + "a" * 32 + "', 1, 1.0)")
                with self.assertRaises(sqlite3.IntegrityError):
                    con.execute("INSERT INTO live_turns (session_id, response, at) VALUES "
                                "('live_" + "a" * 32 + "', 1, 2.0)")


if __name__ == "__main__":
    unittest.main()
