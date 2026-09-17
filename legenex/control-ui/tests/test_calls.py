"""Build V3 CAL: Call Agents, intake state and gx-call sessions on the Control Center.

The node-2 side is the REAL gx-call supervisor (legenex/call) with a stub
engine (legenex/call/tests/engine_stub.py) that speaks the engine protocol
and emits the real tracker events, so these are protocol-level integration
tests: every request crosses the real gx-call HTTP and WebSocket APIs.
Webhooks go to an injected fetch (no network); LiteLLM is a stub.
"""

from __future__ import annotations

import base64
import copy
import dataclasses
import datetime as dt
import hashlib
import hmac
import http.client
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from support import REPO, StubUpstream, TempEnv, fake_key

from gx_control_ui import auth
from gx_control_ui import call_agents as ca
from gx_control_ui import call_intake as ci
from gx_control_ui import server as srv
from gx_control_ui.calls import CallClient, CallError, CallManager, IntegrationSecrets
from gx_control_ui.media_library import MediaLibrary, MediaTools
from gx_control_ui.realtime import RealtimeRegistry, Target

CALL_DIR = REPO / "legenex" / "call"
sys.path.insert(0, str(CALL_DIR))
sys.path.insert(0, str(CALL_DIR / "tests"))
from engine_stub import EngineStub, silence, speech_like  # noqa: E402
from gx_call import ws as wsmod  # noqa: E402
from gx_call.engine import EngineController  # noqa: E402
from gx_call.server import build_servers  # noqa: E402
from gx_call.service import CallService  # noqa: E402
from test_gx_call import FakeDocker, FakeGuard, FakePeers, make_cfg  # noqa: E402

PASSWORD = "Test-Password-For-Calls-50"


class Audit(list):
    def __call__(self, **kw):
        self.append(kw)


class FakeFetch:
    def __init__(self, status=200):
        self.status = status
        self.calls = []

    def __call__(self, url, **kw):
        self.calls.append({"url": url, **kw})
        return SimpleNamespace(status=self.status, headers={}, body=b"ok")


# ---------------------------------------------------------------- intake --
class IntakeTests(unittest.TestCase):
    today = dt.date(2026, 9, 17)

    def test_coercion_and_normalisation(self):
        props = ci.MVA_SCHEMA["properties"]
        self.assertEqual(ci.coerce("phone", props["phone"], "(512) 555-0100"), "+15125550100")
        self.assertEqual(ci.coerce("accident_state", props["accident_state"], "texas"), "TX")
        self.assertEqual(ci.coerce("accident_date", props["accident_date"], "09/01/2026", today=self.today),
                         "2026-09-01")
        self.assertEqual(ci.coerce("email", props["email"], "jane at example dot com"), "jane@example.com")
        self.assertEqual(ci.coerce("fault", props["fault"], "Other Driver"), "other_driver")
        self.assertEqual(ci.coerce("passengers", props["passengers"], "two"), 2)
        for name, value in (("phone", "12"), ("accident_state", "Atlantis"), ("fault", "maybe"),
                            ("passengers", 99), ("email", "nope")):
            with self.subTest(name=name), self.assertRaises(ci.StateError):
                ci.coerce(name, props[name], value)
        with self.assertRaises(ci.StateError):
            ci.coerce("accident_date", props["accident_date"], "2027-01-01", today=self.today)

    def test_apply_fields_is_partial_and_tracks_completion(self):
        state = ci.empty_state(ci.MVA_SCHEMA)
        self.assertEqual(state["disposition"], "in_progress")
        new, changed, rejected = ci.apply_fields(ci.MVA_SCHEMA, state, {
            "caller_name": "Jane Doe", "phone": "bad", "unknown": 1, "accident_state": "TX"})
        self.assertEqual(sorted(changed), ["accident_state", "caller_name"])
        self.assertEqual(sorted(rejected), ["phone", "unknown"])
        comp = ci.completion(new, ci.MVA_REQUIRED_DEFAULT)
        self.assertIn("phone", comp["missing"])
        self.assertEqual(comp["filled"], 2)
        text = ci.speakable(new, changed, rejected, comp["missing"])
        self.assertTrue(text.isascii())
        self.assertIn("Still needed", text)
        with self.assertRaises(ci.StateError):
            ci.apply_fields(ci.MVA_SCHEMA, state, ["not", "a", "dict"])
        _, _, rej = ci.apply_fields(ci.MVA_SCHEMA, state, {"transfer_status": "connected"}, allow_system=False)
        self.assertIn("transfer_status", rej)

    def test_state_rules_and_deadline(self):
        info = ci.state_rules("florida", "2025-06-01", today=self.today)
        self.assertEqual(info["state"], "FL")
        self.assertEqual(info["statute_of_limitations_years"], 2)
        self.assertEqual(info["estimated_deadline"], "2027-06-01")
        self.assertIn("not legal advice", ci.state_rules_sentence(info))
        self.assertEqual(len(ci.STATE_RULES), 51)
        with self.assertRaises(ci.StateError):
            ci.state_rules("Narnia")

    def test_business_hours(self):
        hours = ci.check_hours({"timezone": "America/New_York", "days": {"thu": [["09:00", "17:00"]]},
                                "closed_dates": ["2026-12-25"]})
        open_now = ci.hours_status(hours, dt.datetime(2026, 9, 17, 15, 0, tzinfo=dt.timezone.utc))  # 11:00 EDT
        self.assertTrue(open_now["open"])
        closed = ci.hours_status(hours, dt.datetime(2026, 9, 17, 23, 0, tzinfo=dt.timezone.utc))
        self.assertFalse(closed["open"])
        self.assertTrue(closed["next_open"].startswith("2026-09-24T09:00"))
        for bad in ({"timezone": "Mars/Base"}, {"days": {"fun": []}}, {"days": {"mon": [["17:00", "09:00"]]}}):
            with self.subTest(bad=bad), self.assertRaises(ci.StateError):
                ci.check_hours(bad)

    def test_custom_schema_checks(self):
        ci.check_schema({"type": "object", "properties": {"x": {"type": "string", "pattern": "[a-z]+"}}})
        for bad in ({"type": "object", "properties": {}}, {"type": "object", "properties": {"Bad": {}}},
                    {"type": "object", "properties": {"x": {"type": "string", "pattern": "("}}},
                    {"type": "object", "properties": {"x": {"type": "object", "properties": {
                        "y": {"type": "object", "properties": {"z": {}}}}}}},
                    {"type": "object", "properties": {"x": {"type": "string", "$ref": "#"}}}):
            with self.subTest(bad=bad), self.assertRaises(ci.StateError):
                ci.check_schema(bad)


# ---------------------------------------------------------------- agents --
class AgentTests(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv()
        self.library = MediaLibrary(self.env.cfg.media_dir, MediaTools(enabled=False))
        self.audit = Audit()
        self.store = ca.AgentStore(self.library.connect, audit=self.audit)

    def tearDown(self):
        self.env.cleanup()

    def test_default_template_is_valid_and_compiles(self):
        cfg = ca.validate_config(ca.default_config())
        prompt = ca.compile_prompt(cfg)
        self.assertTrue(prompt.isascii())
        self.assertIn("Start the call right away", prompt)
        self.assertIn("caller_name", prompt)
        tools = ca.compile_tools(cfg)
        self.assertEqual([t["name"] for t in tools], cfg["tool_permissions"])
        self.assertLessEqual(len(tools), ca.MAX_TOOLS_PER_AGENT)
        ca.validate_config(ca.default_config("general"))

    def test_versioning_clone_status_and_diff(self):
        agent = self.store.create(ca.default_config(), user="admin")
        self.assertRegex(agent["agent_id"], r"^agt_[0-9a-f]{24}$")
        self.assertEqual((agent["version"], agent["status"], agent["mode"]), (1, "draft", "test"))
        cfg = copy.deepcopy(agent["config"])
        same = self.store.save(agent["agent_id"], cfg, user="admin")
        self.assertTrue(same["unchanged"])
        cfg["personality"] = "Brisk."
        v2 = self.store.save(agent["agent_id"], cfg, user="admin", base_version=1, note="brisk")
        self.assertEqual(v2["version"], 2)
        with self.assertRaises(ca.AgentError) as ctx:
            self.store.save(agent["agent_id"], cfg | {"personality": "x"}, user="admin", base_version=1)
        self.assertEqual(ctx.exception.code, "version_conflict")
        self.assertEqual(self.store.get(agent["agent_id"], 1)["config"]["personality"],
                         ca.default_config()["personality"])
        self.assertEqual([v["version"] for v in self.store.versions(agent["agent_id"])], [2, 1])
        diff = ca.diff_configs(self.store.get(agent["agent_id"], 1)["config"], v2["config"])
        self.assertEqual([d["field"] for d in diff], ["personality"])
        clone = self.store.clone(agent["agent_id"], user="admin", version=1)
        self.assertNotEqual(clone["agent_id"], agent["agent_id"])
        self.assertTrue(clone["name"].endswith("(copy)"))
        self.assertEqual(clone["cloned_from"], f"{agent['agent_id']}@1")
        enabled = self.store.set_status(agent["agent_id"], user="admin", status="enabled", mode="production")
        self.assertEqual((enabled["status"], enabled["mode"]), ("enabled", "production"))
        self.store.set_status(agent["agent_id"], user="admin", status="archived")
        self.assertNotIn(agent["agent_id"], [a["agent_id"] for a in self.store.list()])
        self.assertIn(agent["agent_id"], [a["agent_id"] for a in self.store.list(include_archived=True)])
        with self.assertRaises(ca.AgentError):
            self.store.save(agent["agent_id"], cfg | {"personality": "y"}, user="admin")
        with self.assertRaises(ca.AgentError):
            self.store.get("agt_" + "0" * 24)
        self.assertTrue(any(a["action"] == "call.agent.save" for a in self.audit))

    def test_validation_errors(self):
        base = ca.default_config()
        cases = {
            "too many tools": {"tool_permissions": list(ca.TOOL_CATALOG)},
            "unknown tool": {"tool_permissions": ["rm_rf"]},
            "send_webhook without hook": {"tool_permissions": ["send_webhook"]},
            "private webhook": {"webhooks": [{"name": "crm", "url": "https://192.168.1.5/hook"}]},
            "http webhook": {"webhooks": [{"name": "crm", "url": "http://example.com/hook"}]},
            "credentials in url": {"webhooks": [{"name": "crm", "url": "https://u:p@example.com/h"}]},
            "recording without notice": {"recording": {"enabled": True, "notice": ""}},
            "unknown required": {"required_fields": ["shoe_size"]},
            "required and optional": {"optional_fields": ["caller_name"]},
            "bad voice": {"voice": "Brad"},
            "other model": {"model": "gx-fast"},
            "no instructions": {"system_instructions": ""},
            "long knowledge": {"knowledge": "x" * 7000},
            "bad hours": {"business_hours": {"timezone": "Nowhere/Zone"}},
            "bad post-call": {"post_call_actions": [{"type": "webhook", "target": "missing"}]},
            "bad tags": {"tags": ["Has Space"]},
            "call too long": {"max_call_minutes": 90},
            "bad phone destination": {"transfer_destination": {"type": "phone", "value": "12"}},
        }
        for label, patch in cases.items():
            with self.subTest(label), self.assertRaises(ca.AgentError):
                ca.validate_config({**base, **patch})

    def test_integrations_validate(self):
        cfg = ca.validate_config({**ca.default_config(), "webhooks": [
            {"name": "office", "url": "https://hooks.example.com/gx", "events": ["call.ended"],
             "secret_ref": "office-hook"}],
            "tool_permissions": ["update_intake_fields", "send_webhook"],
            "post_call_actions": [{"type": "webhook", "target": "office", "when": "always"}],
            "leaddistro": {"enabled": True, "campaign": "mva", "integration": {
                "name": "leaddistro", "url": "https://ld.example.com/leads"}}})
        self.assertEqual(cfg["webhooks"][0]["secret_ref"], "office-hook")
        self.assertTrue(cfg["leaddistro"]["enabled"])


# ------------------------------------------------------------- full call --
class Tunnel:
    """What the Playground does after authorisation: upgrade to gx-call with the upstream path."""

    def __init__(self, port: int, path: str, sid: str, key: str):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        k = base64.b64encode(os.urandom(16)).decode()
        req = [f"GET {path} HTTP/1.1", f"Host: 127.0.0.1:{port}", "Upgrade: websocket", "Connection: Upgrade",
               f"Sec-WebSocket-Key: {k}", "Sec-WebSocket-Version: 13", f"Authorization: Bearer {key}",
               f"X-GX-Session: {sid}"]
        self.sock.sendall(("\r\n".join(req) + "\r\n\r\n").encode())
        self.rfile = self.sock.makefile("rb")
        self.status = int(self.rfile.readline().split()[1])
        while self.rfile.readline() not in (b"\r\n", b""):
            pass
        self.ws = wsmod.WebSocket(self.sock, self.rfile, mask_outgoing=True, require_masked=False)
        self.events: list[dict] = []
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            while True:
                m = self.ws.recv()
                if m.is_text:
                    self.events.append(json.loads(m.text()))
        except (wsmod.WSClosed, OSError):
            pass

    def wait(self, kind, timeout=20, **match):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for e in list(self.events):
                if e.get("type") == kind and all(e.get(k) == v for k, v in match.items()):
                    return e
            time.sleep(0.02)
        raise AssertionError(f"no {kind}: {[e.get('type') for e in self.events]}")


class Node2:
    """A real gx-call supervisor with the engine stub, on loopback."""

    def __init__(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.stub = EngineStub("e" * 43)
        self.cfg = make_cfg(root, self.stub.port)
        self.cfg.engine_key_file.write_text(self.stub.key)
        self.key = (root / "secrets" / "api-key").read_text()
        self.docker = FakeDocker()
        engine = EngineController(self.cfg, docker=self.docker, guard=FakeGuard(), peers=FakePeers(),
                                  mem=lambda: {"MemAvailable": 100.0})
        engine.engine_health = lambda timeout=3.0: {"state": "ready"}
        self.service = CallService(self.cfg, engine)
        self.servers = build_servers(self.service, self.key, ("127.0.0.1",), 0)
        self.port = self.servers[0].server_address[1]
        for s in self.servers:
            threading.Thread(target=s.serve_forever, daemon=True).start()
        self.service.start()

    def close(self):
        self.service.stop()
        for s in self.servers:
            s.shutdown()
            s.server_close()
        self.stub.close()
        self.tmp.cleanup()


class CallFlowTests(unittest.TestCase):
    def setUp(self):
        self.node = Node2()
        self.env = TempEnv()
        key_file = self.env.root / "secrets" / "gx-call" / "api-key"
        key_file.parent.mkdir(parents=True)
        key_file.write_text(self.node.key)
        self.library = MediaLibrary(self.env.cfg.media_dir, MediaTools(enabled=False))
        self.audit = Audit()
        self.agents = ca.AgentStore(self.library.connect, audit=self.audit)
        self.realtime = RealtimeRegistry({"call": Target("127.0.0.1", self.node.port, key_file)})
        self.fetch = FakeFetch()
        self.secrets_store = IntegrationSecrets(self.env.root / "secrets" / "gx-call" / "integrations")
        self.metrics = []
        self.manager = CallManager(connect=self.library.connect, agents=self.agents,
                                   client=CallClient(f"http://127.0.0.1:{self.node.port}", key_file),
                                   realtime=self.realtime, library=self.library, secrets_store=self.secrets_store,
                                   audit=self.audit, metric=lambda *a, **k: self.metrics.append((a, k)),
                                   fetch=self.fetch, start_threads=True)

    def tearDown(self):
        self.node.close()
        self.env.cleanup()

    def agent(self, **patch):
        cfg = {**ca.default_config(), **patch}
        agent = self.agents.create(cfg, user="admin")
        return self.agents.set_status(agent["agent_id"], user="admin", status="enabled")

    def connect(self, sid, owner="user:admin"):
        auth_info = self.realtime.authorize(service="call", session_id=sid, ticket=None,
                                            cookie_user=owner.split(":", 1)[1],
                                            origin="http://127.0.0.1:8090", host="127.0.0.1:8090")
        self.assertEqual(auth_info["authorization"], f"Bearer {self.node.key}")
        tunnel = Tunnel(self.node.port, auth_info["path"], sid, self.node.key)
        self.assertEqual(tunnel.status, 101)
        return tunnel

    def wait_state(self, sid, state="ended", timeout=20):
        deadline = time.time() + timeout
        while time.time() < deadline:
            view = self.manager.session_view(sid)
            if view["state"] == state:
                return view
            time.sleep(0.05)
        self.fail(f"call {sid} never reached {state}: {self.manager.session_view(sid)['state']}")

    def test_call_with_real_tool_execution_and_result(self):
        self.secrets_store.set("office-hook", "s3cret-value-123")
        agent = self.agent(webhooks=[{"name": "office", "url": "https://hooks.example.com/gx",
                                      "events": ["call.ended", "intake.updated"], "secret_ref": "office-hook"}],
                           post_call_actions=[{"type": "webhook", "target": "office", "when": "always"}])
        res = self.manager.create_session(agent_id=agent["agent_id"], owner="user:admin", via="playground",
                                          user_label="admin", initial_state={"phone": "512-555-0100"})
        sid = res["session_id"]
        self.assertEqual(res["ws_path"], f"/rt/call/{sid}")
        self.assertEqual(res["state_snapshot"]["phone"], "+15125550100")
        tunnel = self.connect(sid)
        tunnel.wait("session.ready")
        tunnel.ws.send_binary(speech_like(0.8) + silence(1.5))
        result = tunnel.wait("tool.result")
        self.assertTrue(result["ok"])
        self.assertEqual(result["state"]["caller_name"], "Jane Doe")
        self.assertEqual(result["state"]["accident_state"], "TX")
        self.assertIn("Still needed", self.node.stub.tool_results[0]["output"])
        tunnel.wait("state.updated")
        tunnel.wait("agent.speech.started")
        # operator correction and a warm transfer requested by the integrator
        upd = self.manager.update_state(sid, {"injuries": "neck pain"}, source="operator")
        self.assertEqual(upd["changed"], ["injuries"])
        self.manager.transfer(sid, "requested", source="api", user_label="key:x", reason="asked for a person")
        tunnel.wait("transfer.updated", status="requested")
        with self.assertRaises(CallError):
            self.manager.transfer(sid, "bogus", source="api", user_label="key:x")
        self.manager.transfer(sid, "connected", source="api", user_label="key:x")
        view = self.wait_state(sid)
        self.assertEqual(view["disposition"], "transferred")
        self.assertEqual(view["transfer"]["status"], "connected")
        self.assertEqual(view["result"]["intake_disposition"], "transferred")
        self.assertEqual(view["intake"]["data"]["transfer_status"], "connected")
        self.assertEqual(view["tools"][0]["name"], "update_intake_fields")
        self.assertEqual(view["tools"][0]["ok"], 0)  # the model's call is saved, completion still missing fields
        self.assertIsNotNone(view["tools"][0]["latency_ms"])
        speakers = {t["speaker"] for t in view["transcript"]}
        self.assertEqual(speakers, {"caller", "agent"})
        self.assertIn("first_audio_wall_ms", view["metrics"])
        # webhooks: signed, SSRF-safe fetch, post-call action delivered
        deadline = time.time() + 10
        while not any(c["headers"]["X-GX-Event"] == "call.ended" for c in self.fetch.calls) and time.time() < deadline:
            time.sleep(0.05)
        ended = [c for c in self.fetch.calls if c["headers"]["X-GX-Event"] == "call.ended"]
        self.assertTrue(ended)
        call = ended[0]
        self.assertFalse(call["allow_http"])
        body = call["body"]
        ts = call["headers"]["X-GX-Timestamp"]
        expected = hmac.new(b"s3cret-value-123", ts.encode() + b"." + body, hashlib.sha256).hexdigest()
        self.assertEqual(call["headers"]["X-GX-Signature"], "sha256=" + expected)
        payload = json.loads(body)
        self.assertEqual(payload["intake"]["caller_name"], "Jane Doe")
        self.assertEqual(payload["session"]["session_id"], sid)
        deadline = time.time() + 10
        while time.time() < deadline and not self.manager.session_view(sid)["result"]["post_call"]:
            time.sleep(0.05)
        self.assertTrue(self.manager.session_view(sid)["result"]["post_call"][0]["ok"])
        # the persisted feed has metadata only (no transcript text in call_events)
        with self.library.connect() as con:
            rows = [r["data"] for r in con.execute("SELECT data FROM call_events WHERE session_id = ?", (sid,))]
        self.assertTrue(rows)
        self.assertFalse(any("Jane" in r for r in rows))
        feed = self.manager.events(sid)
        self.assertTrue(any(e["type"] == "transcript.caller" for e in feed["events"]))
        # version metrics and comparison
        metrics = self.manager.version_metrics(agent["agent_id"], agent["version"])
        self.assertEqual(metrics["calls"], 1)
        self.assertEqual(metrics["dispositions"], {"transferred": 1})
        cmp = self.manager.compare((agent["agent_id"], 1), (agent["agent_id"], 1))
        self.assertEqual(cmp["diff"], [])
        # retention purge removes content, keeps metadata
        self.manager.delete_session_content(sid, user_label="admin")
        purged = self.manager.session_view(sid)
        self.assertEqual(purged["content_purged"], 1)
        self.assertNotIn("transcript", purged)
        with self.assertRaises(CallError):
            self.manager.get_state(sid)

    def test_end_call_tool_and_recording_import(self):
        agent = self.agent(recording={"enabled": True, "notice": "This call is recorded."})
        self.node.stub.tool_args = {"outcome": "callback_requested"}
        # make the stub call end_call instead of update_intake_fields
        cfg = copy.deepcopy(agent["config"])
        cfg["tool_permissions"] = ["end_call"]
        agent = self.agents.save(agent["agent_id"], cfg, user="admin")
        self.node.stub.close()
        self.node.stub = EngineStub("e" * 43, port=self.node.stub.port, tool_args={"outcome": "callback_requested"})
        mixed = []

        def fake_mix(work):
            mixed.append(sorted(p.name for p in work.iterdir()))
            out = work / "call.wav"
            out.write_bytes((work / "agent.wav").read_bytes())
            return out

        self.manager.mix_recording = fake_mix
        res = self.manager.create_session(agent_id=agent["agent_id"], owner="user:admin", via="playground",
                                          user_label="admin")
        self.assertTrue(res["record"])
        self.assertEqual(res["recording_notice"], "This call is recorded.")
        sid = res["session_id"]
        tunnel = self.connect(sid)
        cfg_sent = None
        tunnel.wait("session.ready")
        cfg_sent = self.node.stub.configs[-1]
        self.assertTrue(cfg_sent["system_prompt"].startswith("You are the intake specialist"))
        self.assertIn("This call is recorded.", cfg_sent["system_prompt"])
        tunnel.ws.send_binary(speech_like(0.8) + silence(1.0))
        tunnel.wait("session.ended", timeout=30)  # the stub never calls end_call; end explicitly below
        self.wait_state(sid)
        deadline = time.time() + 20
        while time.time() < deadline and not self.manager.session_view(sid)["recording_asset_id"]:
            time.sleep(0.1)
        view = self.manager.session_view(sid)
        self.assertIsNotNone(view["recording_asset_id"])
        self.assertEqual(mixed[0], ["agent.wav", "caller.wav"])
        asset = self.library.get(view["recording_asset_id"])
        self.assertEqual((asset["operation"], asset["type"]), ("recording", "audio"))
        self.assertEqual(asset["source_ref"], sid)

    def test_refusals(self):
        agent = self.agents.create(ca.default_config(), user="admin")  # draft
        with self.assertRaises(CallError) as ctx:
            self.manager.create_session(agent_id=agent["agent_id"], owner="key:" + "0" * 16, via="api",
                                        user_label="key", require_enabled=True)
        self.assertEqual(ctx.exception.code, "agent_disabled")
        with self.assertRaises(CallError) as ctx:
            self.manager.create_session(agent_id=agent["agent_id"], owner="user:admin", via="playground",
                                        user_label="admin", record=True)
        self.assertEqual(ctx.exception.code, "recording_disabled")
        with self.assertRaises(CallError):
            self.manager.create_session(agent_id=agent["agent_id"], owner="user:admin", via="playground",
                                        user_label="admin", initial_state={"phone": "nope"})
        with self.assertRaises(CallError):
            self.manager.create_session(agent_id=agent["agent_id"], owner="user:admin", via="playground",
                                        user_label="admin", external_ref="bad ref!")
        # node 2 refuses (gx-max hold) -> the session is recorded as failed, the error is surfaced
        self.node.cfg.gxmax_hold_file.write_text("x")
        with self.assertRaises(CallError) as ctx:
            self.manager.create_session(agent_id=agent["agent_id"], owner="user:admin", via="playground",
                                        user_label="admin")
        self.assertEqual(ctx.exception.code, "gx_max_active")
        sessions = self.manager.list_sessions(owner="user:admin")
        self.assertEqual(sessions[0]["disposition"], "failed")

    def test_tools_execute_against_config(self):
        agent = self.agent()
        res = self.manager.create_session(agent_id=agent["agent_id"], owner="user:admin", via="playground",
                                          user_label="admin")
        sid = res["session_id"]
        ok, text, extra = self.manager.execute_tool(sid, "lookup_accident_state_rules", {"state": "NY"})
        self.assertTrue(ok)
        self.assertIn("New York", text)
        self.assertTrue(text.isascii())
        ok, text, _ = self.manager.execute_tool(sid, "lookup_accident_state_rules", {"state": "Mordor"})
        self.assertFalse(ok)
        ok, text, extra = self.manager.execute_tool(sid, "check_business_hours", {})
        self.assertTrue(ok)
        self.assertIn(extra["hours"]["open"], (True, False))
        ok, text, _ = self.manager.execute_tool(sid, "send_webhook", {"reason": "x"})
        self.assertFalse(ok)  # not permitted for this agent
        ok, text, extra = self.manager.execute_tool(sid, "update_intake_fields", {"caller_name": "Flat Args"})
        self.assertEqual(extra["changed"], ["caller_name"])
        ok, text, extra = self.manager.execute_tool(sid, "request_warm_transfer", {"reason": "qualified"})
        self.assertTrue(ok)
        self.assertEqual(self.manager.session_view(sid)["transfer"]["status"], "requested")
        self.manager.end_session(sid, "operator_ended", user_label="admin")


# ------------------------------------------------------------ HTTP routes --
class RouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = Node2()
        cls.k_call = fake_key()
        cls.k_music = fake_key()
        infos = {cls.k_call: {"key_alias": "intakepilot", "models": ["gx-call"]},
                 cls.k_music: {"key_alias": "music-only", "models": ["gx-music"]}}

        def key_info(handler, body):
            info = infos.get((handler.headers.get("Authorization") or "")[7:])
            return (200, {"info": info}) if info else (401, {"error": {"message": "bad key"}})

        cls.litellm = StubUpstream({("GET", "/key/info"): key_info})

    @classmethod
    def tearDownClass(cls):
        cls.node.close()
        cls.litellm.close()

    def setUp(self):
        self.env = TempEnv(litellm_base=self.litellm.url, rt_call_target=f"127.0.0.1:{self.node.port}",
                           secrets_root=None)
        self.env.cfg = dataclasses.replace(self.env.cfg, secrets_root=self.env.root / "secrets")
        key_file = self.env.root / "secrets" / "gx-call" / "api-key"
        key_file.parent.mkdir(parents=True)
        key_file.write_text(self.node.key)
        auth.PasswordStore(self.env.cfg.password_file).set_password("admin", PASSWORD, n=2**10)
        self.app, servers = srv.build(self.env.cfg)
        self.httpd = servers[0]
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.cookie = self.csrf = None

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
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

    def login(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/api/login", json.dumps({"username": "admin", "password": PASSWORD}),
                     {"Content-Type": "application/json", "Host": f"127.0.0.1:{self.port}"})
        res = conn.getresponse()
        res.read()
        self.cookie = res.getheader("Set-Cookie").split(";")[0]
        conn.close()
        self.csrf = self.req("GET", "/api/session")[1]["csrf"]

    def test_agent_routes(self):
        self.assertEqual(self.req("GET", "/api/call/agents")[0], 401)
        self.login()
        status, cat = self.req("GET", "/api/call/catalog")
        self.assertEqual(status, 200)
        self.assertIn("update_intake_fields", [t["name"] for t in cat["tools"]])
        status, agent = self.req("POST", "/api/call/agents", {"template": "intakepilot_mva"})
        self.assertEqual(status, 201, agent)
        aid = agent["agent_id"]
        cfg = agent["config"]
        cfg["tool_permissions"] = list(ca.TOOL_CATALOG)
        status, err = self.req("POST", f"/api/call/agents/{aid}", {"config": cfg})
        self.assertEqual(status, 400)
        self.assertIn("at most", err["error"]["message"])
        cfg["tool_permissions"] = ["end_call"]
        status, saved = self.req("POST", f"/api/call/agents/{aid}", {"config": cfg, "base_version": 1})
        self.assertEqual((status, saved["version"]), (200, 2))
        status, prev = self.req("POST", "/api/call/preview", {"config": cfg})
        self.assertEqual(status, 200)
        self.assertEqual([t["name"] for t in prev["compiled_tools"]], ["end_call"])
        status, got = self.req("GET", f"/api/call/agents/{aid}?version=1")
        self.assertEqual(got["version"], 1)
        self.assertIn("compiled_prompt", got)
        status, vers = self.req("GET", f"/api/call/agents/{aid}/versions")
        self.assertEqual([v["version"] for v in vers["versions"]], [2, 1])
        status, cmp = self.req("GET", f"/api/call/compare?a={aid}@1&b={aid}@2")
        self.assertEqual(status, 200)
        self.assertIn("tool_permissions", [d["field"] for d in cmp["diff"]])
        self.assertEqual(self.req("GET", f"/api/call/compare?a={aid}&b=x")[0], 400)
        status, st = self.req("POST", f"/api/call/agents/{aid}/status", {"status": "enabled"})
        self.assertEqual(st["status"], "enabled")
        status, clone = self.req("POST", f"/api/call/agents/{aid}/clone", {"name": "Copy"})
        self.assertEqual((status, clone["name"]), (201, "Copy"))
        # secrets are write-only
        status, res = self.req("POST", "/api/call/integrations/secrets", {"name": "crm-key", "value": "abcdefgh12"})
        self.assertEqual(status, 200)
        status, listing = self.req("GET", "/api/call/integrations/secrets")
        self.assertEqual([s["name"] for s in listing["secrets"]], ["crm-key"])
        self.assertNotIn("abcdefgh12", json.dumps(listing))
        path = self.env.root / "secrets" / "gx-call" / "integrations" / "crm-key"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.req("POST", "/api/call/integrations/secrets/crm-key/delete", {})[0], 200)
        self.assertFalse(path.exists())
        # a browser session is created for the signed-in user and registered for the tunnel
        status, sess = self.req("POST", "/api/call/sessions", {"agent_id": aid})
        self.assertEqual(status, 201, sess)
        self.assertEqual(sess["ws_path"], f"/rt/call/{sess['session_id']}")
        self.assertEqual(self.app.realtime.get(sess["session_id"]).owner, "user:admin")
        status, view = self.req("GET", f"/api/call/sessions/{sess['session_id']}")
        self.assertEqual(status, 200)
        status, listing = self.req("GET", "/api/call/sessions")
        self.assertEqual(listing["sessions"][0]["session_id"], sess["session_id"])
        status, ended = self.req("POST", f"/api/call/sessions/{sess['session_id']}/end", {})
        self.assertEqual(status, 200)

    def test_public_api(self):
        self.login()
        _, agent = self.req("POST", "/api/call/agents", {"template": "intakepilot_mva"})
        aid = agent["agent_id"]
        auth_call = {"Authorization": f"Bearer {self.k_call}"}
        self.assertEqual(self.req("GET", "/v1/call/agents", cookie=True)[0], 401)  # a cookie is not enough
        self.assertEqual(self.req("GET", "/v1/call/agents", headers={
            "Authorization": f"Bearer {self.k_music}"}, cookie=False)[0], 403)
        status, listing = self.req("GET", "/v1/call/agents", headers=auth_call, cookie=False)
        self.assertEqual((status, listing["data"]), (200, []))  # drafts are not offered to API clients
        status, err = self.req("POST", "/v1/call/sessions", {"agent_id": aid}, headers=auth_call, cookie=False)
        self.assertEqual((status, err["error"]["code"]), (409, "agent_disabled"))
        self.req("POST", f"/api/call/agents/{aid}/status", {"status": "enabled", "mode": "production"})
        status, listing = self.req("GET", "/v1/call/agents", headers=auth_call, cookie=False)
        self.assertEqual(listing["data"][0]["agent_id"], aid)
        status, sess = self.req("POST", "/v1/call/sessions",
                                {"agent_id": aid, "external_ref": "ip-lead-42", "initial_state": {"phone": "5125550100"}},
                                headers=auth_call, cookie=False)
        self.assertEqual(status, 201, sess)
        sid = sess["session_id"]
        self.assertRegex(sess["ws_path"], rf"^/rt/call/{sid}\?ticket=v1\.")
        ticket = sess["ws_path"].split("ticket=")[1]
        owner = self.app.realtime.redeem_ticket(ticket, sid)
        self.assertTrue(owner.startswith("key:"))
        with self.assertRaises(Exception):
            self.app.realtime.redeem_ticket(ticket, sid)  # single use
        status, t2 = self.req("POST", f"/v1/call/sessions/{sid}/ticket", {}, headers=auth_call, cookie=False)
        self.assertEqual(status, 200)
        status, state = self.req("GET", f"/v1/call/sessions/{sid}/state", headers=auth_call, cookie=False)
        self.assertEqual(state["data"]["phone"], "+15125550100")
        status, upd = self.req("POST", f"/v1/call/sessions/{sid}/state", {"fields": {"caller_name": "Api Caller"}},
                               headers=auth_call, cookie=False)
        self.assertEqual((status, upd["changed"]), (200, ["caller_name"]))
        self.assertEqual(self.req("GET", f"/v1/call/sessions/{sid}/result", headers=auth_call,
                                  cookie=False)[0], 409)
        status, ended = self.req("POST", f"/v1/call/sessions/{sid}/end", {"reason": "client_ended"},
                                 headers=auth_call, cookie=False)
        self.assertEqual(status, 200)
        # the browser user cannot see an API caller's session, and vice versa
        self.assertEqual(self.req("GET", f"/api/call/sessions/{sid}")[0], 404)
        status, result = self.req("GET", f"/v1/call/sessions/{sid}/result", headers=auth_call, cookie=False)
        self.assertEqual(status, 200, result)
        self.assertEqual(result["external_ref"], "ip-lead-42")
        self.assertEqual(result["result"]["structured"]["caller_name"], "Api Caller")
        self.assertEqual(self.req("GET", f"/v1/call/sessions/call_{'0' * 32}", headers=auth_call,
                                  cookie=False)[0], 404)
        self.assertEqual(self.req("DELETE", f"/v1/call/sessions/{sid}", headers=auth_call, cookie=False)[0], 405)


if __name__ == "__main__":
    unittest.main()
