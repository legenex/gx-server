"""Creative Flows integration: the real App, HTTP server, Library database
(with its migrations) and flow engine; the GPU/network services are stubs
that create real Library assets (tests/flows_support.py).

Covers end-to-end execution with provenance, caching, cancellation, failure
and Rerun failed, restart recovery, list mapping, branching, bypass, lock,
the session routes (auth, CSRF, version conflicts, locks) and the public
/v1/flows, /v1/flow-runs and /v1/assets API (key auth, ownership, per-alias
authorisation, PUT/DELETE).
"""

from __future__ import annotations

import http.client
import json
import threading
import time
import unittest

from flows_support import StubFFmpeg, StubLLM, StubMedia, StubMusic, StubVoice
from support import StubUpstream, TempEnv, fake_key

from gx_control_ui import auth
from gx_control_ui import server as srv
from gx_control_ui.flows import FlowError, FlowService
from gx_control_ui.flows.engine import FlowEngine
from gx_control_ui.media_library import NewAsset

PASSWORD = "Flows-Test-Password-9"


class FlowBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.k_all = fake_key()
        cls.k_media = fake_key()
        cls.k_other = fake_key()
        cls.k_chat = fake_key()
        infos = {
            cls.k_all: {"key_alias": "all", "models": []},
            cls.k_media: {"key_alias": "media", "models": ["gx-image", "gx-video", "gx-auto"]},
            cls.k_other: {"key_alias": "other", "models": []},
            cls.k_chat: {"key_alias": "chat", "models": ["gx-mini"]},
        }

        def key_info(handler, body):  # noqa: ANN001
            info = infos.get((handler.headers.get("Authorization") or "")[7:])
            if info is None:
                return 401, {"error": {"message": "Authentication Error"}}
            return 200, {"info": info}

        cls.litellm = StubUpstream({("GET", "/key/info"): key_info})

    @classmethod
    def tearDownClass(cls) -> None:
        cls.litellm.close()

    def setUp(self) -> None:
        self.env = TempEnv(litellm_base=self.litellm.url)
        auth.PasswordStore(self.env.cfg.password_file).set_password("admin", PASSWORD, n=2**10)
        self.app, servers = srv.build(self.env.cfg)
        self.httpd = servers[0]
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        lib = self.app.library
        self.media = StubMedia(lib, delay=0.02)
        self.music = StubMusic(lib, delay=0.02)
        self.voice = StubVoice(lib, delay=0.02)
        self.llm = StubLLM()
        self.ff = StubFFmpeg(lib.root / "tmp")
        s = self.app.flows.services
        s.media, s.music, s.llm, s.ffmpeg = self.media, self.music, self.llm, self.ff
        s.voice = lambda: self.voice
        s.poll_interval = 0.02
        self.flows: FlowService = self.app.flows
        self.flows._voice_available = lambda: None
        self.cookie = None
        self.csrf = None

    def tearDown(self) -> None:
        for rid in list(self.flows.engine._runs):  # noqa: SLF001
            self.flows.engine.cancel(rid)
            self.flows.engine.wait(rid, 10)
        self.httpd.shutdown()
        self.httpd.server_close()
        self.env.cleanup()

    # ------------------------------------------------------------ helpers
    def req(self, method, path, body=None, headers=None, cookie=True):  # noqa: ANN001
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        hdrs = {"Host": f"127.0.0.1:{self.port}"}
        if cookie and self.cookie:
            hdrs["Cookie"] = self.cookie
        raw = None
        if body is not None:
            raw = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
        hdrs.update(headers or {})
        conn.request(method, path, body=raw, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        parsed = json.loads(data) if data and "json" in (resp.getheader("Content-Type") or "") else data
        return resp.status, parsed

    def login(self) -> None:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/api/login", body=json.dumps({"username": "admin", "password": PASSWORD}),
                     headers={"Content-Type": "application/json", "Host": f"127.0.0.1:{self.port}"})
        resp = conn.getresponse()
        body = json.loads(resp.read())
        self.cookie = resp.getheader("Set-Cookie").split(";")[0]
        self.csrf = body["csrf"]
        conn.close()

    def post(self, path, body=None, csrf=True):  # noqa: ANN001
        hdrs = {"Origin": f"http://127.0.0.1:{self.port}"}
        if csrf:
            hdrs["X-CSRF-Token"] = self.csrf or ""
        return self.req("POST", path, body if body is not None else {}, hdrs)

    def api(self, method, path, key, body=None):  # noqa: ANN001
        return self.req(method, path, body, {"Authorization": f"Bearer {key}"}, cookie=False)

    def image_asset(self) -> str:
        from flows_support import png_bytes
        return self.app.library.add(NewAsset(type="image", ext="png", operation="upload", data=png_bytes()))["id"]

    def create(self, graph: dict, owner: str = "ui") -> dict:
        return self.flows.create({"graph": graph}, owner=owner, user="admin")

    def execute(self, flow: dict, mode: str = "full", timeout: float = 30, **extra) -> dict:
        run = self.flows.run(flow["id"], {"mode": mode, **extra}, owner=None, run_owner="ui", user="admin")
        self.assertTrue(self.flows.engine.wait(run["id"], timeout), "run did not finish")
        return self.flows.run_state(run["id"], owner=None)

    @staticmethod
    def statuses(run: dict) -> dict:
        return {n: s["status"] for n, s in run["nodes"].items()}


def n(nid: str, ntype: str, **config) -> dict:
    return {"id": nid, "type": ntype, "config": config, "position": {"x": 0, "y": 0}}


def e(s: str, sp: str, t: str, tp: str) -> dict:
    return {"id": f"{s}-{sp}-{t}-{tp}", "source": s, "source_port": sp, "target": t, "target_port": tp}


PIPELINE = {
    "name": "Pipeline",
    "nodes": [n("brief", "text.input", text="an ad about a rear-ended BMW"),
              n("script", "ai.script_writer", scenes=2, duration=10),
              n("imgs", "image.generate", size="928x1664"),
              n("clips", "video.i2v", seconds=4, size="480x832"),
              n("join", "compose.concat"),
              n("voice", "voice.tts", voice_id="preset:serena", style="calm"),
              n("music", "music.instrumental", description="soft piano"),
              n("mix", "compose.add_voice", fit="longest"),
              n("bed", "compose.add_music"),
              n("final", "compose.export", preset="1080x1920", favourite=True)],
    "edges": [e("brief", "text", "script", "brief"), e("script", "visuals", "imgs", "prompt"),
              e("imgs", "image", "clips", "image"), e("clips", "video", "join", "video"),
              e("script", "narration", "voice", "text"), e("join", "video", "mix", "video"),
              e("voice", "audio", "mix", "audio"), e("mix", "video", "bed", "video"),
              e("music", "audio", "bed", "audio"), e("bed", "video", "final", "video")],
}


class EngineTests(FlowBase):
    def test_full_run_with_mapping_provenance_and_history(self) -> None:
        flow = self.create(PIPELINE)
        run = self.execute(flow)
        self.assertEqual(run["status"], "succeeded", run)
        st = self.statuses(run)
        self.assertTrue(all(s == "succeeded" for s in st.values()), st)
        self.assertEqual(len(run["nodes"]["imgs"]["outputs"]["image"]), 2, "one image per scene")
        self.assertEqual(len(run["nodes"]["clips"]["outputs"]["video"]), 2)
        self.assertEqual(len(self.media.submitted), 4)
        self.assertEqual([b["kind"] for b in self.media.submitted[:2]], ["t2i", "t2i"])
        self.assertTrue(all("source_id" in b for b in self.media.submitted[2:]))
        final = run["summary"]["final_assets"]
        self.assertEqual(len(final), 1)
        asset = self.app.library.get(final[0])
        self.assertEqual((asset["type"], asset["operation"], asset["flow_id"], asset["flow_run_id"],
                          asset["flow_node_id"]), ("video", "composite", flow["id"], run["id"], "final"))
        self.assertTrue(asset["favourite"])
        self.assertEqual(asset["source_kind"], "flow_node")
        for aid in run["nodes"]["imgs"]["outputs"]["image"]:
            img = self.app.library.get(aid["asset_id"])
            self.assertEqual((img["flow_run_id"], img["flow_node_id"]), (run["id"], "imgs"))
        voice_asset = self.app.library.get(run["nodes"]["voice"]["outputs"]["audio"][0]["asset_id"])
        self.assertEqual(voice_asset["flow_node_id"], "voice")
        self.assertIn("gx-voice", run["nodes"]["voice"]["model"])
        self.assertIn("gx-image", run["nodes"]["imgs"]["model"])
        self.assertEqual(run["summary"]["executed"], 10)
        self.assertTrue(run["summary"]["models"])
        self.assertTrue(any(w["code"] == "insufficient_memory" for w in run["summary"]["resource_waits"]))
        detail = self.flows.node_detail(run["id"], "imgs", owner=None)
        self.assertTrue(any("media job" in line["msg"] for line in detail["logs"]))
        self.assertEqual(len(detail["payload"]["iterations"]), 2)
        self.assertEqual(self.voice.submitted[0]["flow"]["flow_node_id"], "voice")
        history = self.flows.runs(flow["id"], owner=None)
        self.assertEqual(history[0]["id"], run["id"])
        self.assertEqual(history[0]["node_counts"], {"succeeded": 10})

    def test_rerun_unchanged_uses_cache_and_changes_invalidate(self) -> None:
        flow = self.create(PIPELINE)
        first = self.execute(flow)
        submitted = len(self.media.submitted)
        second = self.execute(flow)
        self.assertEqual(set(self.statuses(second).values()), {"cached"})
        self.assertEqual(len(self.media.submitted), submitted)
        self.assertEqual(second["nodes"]["final"]["outputs"], first["nodes"]["final"]["outputs"])
        self.assertEqual(second["summary"]["cached"], 10)
        # change the music: only music and what depends on it run again
        graph = self.flows.get(flow["id"], owner=None)["graph"]
        graph["nodes"][6]["config"]["description"] = "soft strings"
        flow = self.flows.update(flow["id"], {"graph": graph, "version": flow["version"]}, owner=None,
                                 user="admin")
        third = self.execute(flow)
        st = self.statuses(third)
        self.assertEqual({k for k, v in st.items() if v == "succeeded"}, {"music", "bed", "final"})
        self.assertEqual(len(self.media.submitted), submitted)
        # a deleted cached output is not reused
        self.app.library.delete([third["nodes"]["final"]["outputs"]["video"][0]["asset_id"]])
        fourth = self.execute(flow)
        self.assertEqual(self.statuses(fourth)["final"], "succeeded")
        # regenerate forces one node
        fifth = self.execute(flow, "regenerate", node_id="imgs")
        st = self.statuses(fifth)
        self.assertEqual(st["imgs"], "succeeded")
        self.assertEqual(st["brief"], "cached")
        self.assertNotIn("final", st)

    def test_cancel_propagates_to_the_media_job(self) -> None:
        self.media.hold.clear()
        flow = self.create(PIPELINE)
        run = self.flows.run(flow["id"], {"mode": "full"}, owner=None, run_owner="ui", user="admin")
        deadline = time.time() + 10
        while time.time() < deadline and not any(j["phase"] == "waiting" for j in self.media.jobs.values()):
            time.sleep(0.02)
        state = self.flows.cancel(run["id"], owner=None, user="admin")
        self.assertTrue(self.flows.engine.wait(run["id"], 10))
        self.media.hold.set()
        state = self.flows.run_state(run["id"], owner=None)
        self.assertEqual(state["status"], "cancelled")
        self.assertIn("cancelled", self.statuses(state).values())
        self.assertTrue(any(j["phase"] == "cancelled" for j in self.media.jobs.values()))
        self.assertNotIn("succeeded", {self.statuses(state).get(k) for k in ("final", "mix")})
        with self.assertRaises(FlowError):
            self.flows.cancel(run["id"], owner=None, user="admin")

    def test_failure_blocks_downstream_and_rerun_failed_recovers(self) -> None:
        flow = self.create(PIPELINE)
        self.media.fail_next = "out of memory on gx10-02"
        run = self.execute(flow)
        st = self.statuses(run)
        self.assertEqual(run["status"], "failed")
        self.assertEqual(st["imgs"], "failed")
        self.assertIn("out of memory", run["nodes"]["imgs"]["error"])
        self.assertEqual(st["clips"], "blocked")
        self.assertEqual(st["final"], "blocked")
        self.assertEqual(st["voice"], "succeeded", "independent branches continue")
        self.assertEqual(st["music"], "succeeded")
        self.assertIn("out of memory", run["error"])
        again = self.execute(flow, "rerun_failed", run_id=run["id"])
        st2 = self.statuses(again)
        self.assertEqual(again["status"], "succeeded")
        self.assertEqual(again["parent_run"], run["id"])
        self.assertEqual(st2["voice"], "cached")
        self.assertEqual(st2["imgs"], "succeeded")
        self.assertEqual(st2["final"], "succeeded")

    def test_restart_marks_runs_interrupted_and_rerun_failed_resumes(self) -> None:
        flow = self.create(PIPELINE)
        self.media.hold.clear()
        run = self.flows.run(flow["id"], {"mode": "full"}, owner=None, run_owner="ui", user="admin")
        deadline = time.time() + 10
        while time.time() < deadline and self.flows.run_state(run["id"], owner=None)["nodes"]["music"]["status"] \
                != "succeeded":
            time.sleep(0.02)
        # simulate a Control Center restart: a fresh engine over the same database
        restarted = FlowEngine(self.flows.services)
        self.assertIn(run["id"], restarted.recovered)
        state = self.flows.run_state(run["id"], owner=None)
        self.assertEqual(state["status"], "interrupted")
        self.assertIn("interrupted", self.statuses(state).values())
        self.assertEqual(self.statuses(state)["music"], "succeeded")
        self.assertEqual(self.statuses(state)["voice"], "interrupted", "queued behind the held image job")
        self.flows.engine.cancel(run["id"])
        self.flows.engine.wait(run["id"], 10)
        self.media.hold.set()
        self.flows.engine = restarted
        again = self.execute(flow, "rerun_failed", run_id=run["id"])
        self.assertEqual(again["status"], "succeeded")
        self.assertEqual(self.statuses(again)["music"], "cached")
        self.assertEqual(self.statuses(again)["voice"], "succeeded")

    def test_branching_bypass_lock_and_ready_checks(self) -> None:
        graph = {"name": "Branches", "nodes": [
            n("t", "text.input", text="make a video"),
            n("c", "util.conditional", test="contains", argument="music"),
            n("yes", "image.generate"), n("no", "text.prompt", template="no: {{input}}"),
            n("skip", "ai.prompt_enhancer"), n("after", "image.generate")],
            "edges": [e("t", "text", "c", "value"), e("c", "yes", "yes", "prompt"), e("c", "no", "no", "context"),
                      e("t", "text", "skip", "text"), e("skip", "text", "after", "prompt")]}
        flow = self.create(graph)
        g = self.flows.get(flow["id"], owner=None)["graph"]
        g["nodes"][4]["disabled"] = True
        flow = self.flows.update(flow["id"], {"graph": g, "version": flow["version"]}, owner=None, user="admin")
        run = self.execute(flow)
        st = self.statuses(run)
        self.assertEqual(st["yes"], "skipped")
        self.assertEqual(st["no"], "succeeded")
        self.assertEqual(run["nodes"]["no"]["outputs"]["text"][0]["text"], "no: make a video")
        self.assertEqual(st["skip"], "bypassed")
        self.assertEqual(st["after"], "succeeded", "a bypassed node passes its text through")
        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(self.media.submitted[-1]["prompt"], "make a video")
        # lock: the node reuses its last result and cannot be edited or deleted while locked
        g = self.flows.get(flow["id"], owner=None)["graph"]
        g["nodes"][5]["locked"] = True
        flow = self.flows.update(flow["id"], {"graph": g, "version": flow["version"]}, owner=None, user="admin")
        g["nodes"][5]["config"]["quality"] = "hd"
        with self.assertRaises(Exception) as cm:
            self.flows.update(flow["id"], {"graph": g, "version": flow["version"]}, owner=None, user="admin")
        self.assertEqual(cm.exception.code, "locked")  # type: ignore[attr-defined]
        g["nodes"][5]["config"].pop("quality")
        g["nodes"][0]["config"]["text"] = "make a music video"
        flow = self.flows.update(flow["id"], {"graph": g, "version": flow["version"]}, owner=None, user="admin")
        run = self.execute(flow)
        st = self.statuses(run)
        self.assertEqual(st["after"], "reused")
        self.assertEqual(st["yes"], "succeeded")
        self.assertEqual(st["no"], "skipped")
        # an unfinished flow is refused before anything starts
        g["nodes"].append(n("empty", "video.i2v"))
        flow = self.flows.update(flow["id"], {"graph": g, "version": flow["version"]}, owner=None, user="admin")
        with self.assertRaises(Exception) as cm:
            self.flows.run(flow["id"], {"mode": "full"}, owner=None, run_owner="ui", user="admin")
        self.assertEqual(cm.exception.code, "not_ready")  # type: ignore[attr-defined]
        self.assertEqual(cm.exception.issues[0]["node_id"], "empty")  # type: ignore[attr-defined]
        run = self.execute(flow, "node", node_id="no")
        self.assertEqual(set(run["nodes"]), {"t", "c", "no"})

    def test_llm_failure_and_node_cancel(self) -> None:
        self.llm.fail = "gx-auto answered HTTP 503"
        flow = self.create({"name": "LLM", "nodes": [n("q", "ai.llm", instruction="hi")], "edges": []})
        run = self.execute(flow)
        self.assertEqual(run["status"], "failed")
        self.assertIn("503", run["nodes"]["q"]["error"])
        self.llm.fail = None
        self.llm.delay = 5
        run = self.flows.run(flow["id"], {"mode": "full"}, owner=None, run_owner="ui", user="admin")
        time.sleep(0.3)
        self.flows.cancel_node(run["id"], "q", owner=None, user="admin")
        self.assertTrue(self.flows.engine.wait(run["id"], 5), "cancel must not wait for the gateway")
        state = self.flows.run_state(run["id"], owner=None)
        self.assertEqual(self.statuses(state)["q"], "cancelled")

    def test_voice_design_music_and_compose_nodes(self) -> None:
        tpl = self.flows.create({"template_id": "builtin_voiceover"}, owner="ui", user="admin")
        run = self.execute(tpl)
        self.assertEqual(run["status"], "succeeded", run)
        self.assertTrue(run["nodes"]["design"]["outputs"]["voice"][0]["voice_id"].startswith("vc_"))
        self.assertEqual(self.voice.submitted[0]["operation"], "voice_design")
        self.assertEqual(self.voice.submitted[1]["voice_id"],
                         run["nodes"]["design"]["outputs"]["voice"][0]["voice_id"])
        final = self.app.library.get(run["summary"]["final_assets"][0])
        self.assertEqual(final["operation"], "composite")
        self.assertIn("mp3", final["variants"])
        self.assertIn("loudnorm", " ".join(self.ff.jobs[-1].args))
        mv = self.flows.create({"template_id": "builtin_music_video"}, owner="ui", user="admin")
        run = self.execute(mv)
        self.assertEqual(run["status"], "succeeded", run)
        self.assertEqual(len(run["nodes"]["images"]["outputs"]["image"]), 4)
        self.assertTrue(self.music.submitted[-1]["instrumental"])
        self.assertEqual(self.music.submitted[-1]["lyrics"], "[Instrumental]")

    def test_concurrency_guard_and_asset_start(self) -> None:
        aid = self.image_asset()
        flow = self.flows.create({"graph": {"name": "From asset", "nodes": [], "edges": []}, "asset_id": aid},
                                 owner="ui", user="admin")
        self.assertEqual(flow["graph"]["nodes"][0]["config"]["asset_id"], aid)
        self.media.hold.clear()
        flow = self.create(PIPELINE)
        run = self.flows.run(flow["id"], {"mode": "full"}, owner=None, run_owner="ui", user="admin")
        with self.assertRaises(Exception) as cm:
            self.flows.run(flow["id"], {"mode": "full"}, owner=None, run_owner="ui", user="admin")
        self.assertEqual(cm.exception.code, "already_running")  # type: ignore[attr-defined]
        with self.assertRaises(Exception) as cm:
            self.flows.delete(flow["id"], owner=None, user="admin")
        self.assertEqual(cm.exception.code, "busy")  # type: ignore[attr-defined]
        self.media.hold.set()
        self.assertTrue(self.flows.engine.wait(run["id"], 20))
        # one gx10-02 generation at a time per run: images never overlapped the voice job
        self.flows.delete(flow["id"], owner=None, user="admin")
        self.assertEqual(self.flows.list(owner=None), [self.flows.list(owner=None)[0]])


class RouteTests(FlowBase):
    def test_session_boundaries(self) -> None:
        for path in ("/api/flows", "/api/flows/catalog", "/api/flows/templates", "/api/flow-runs"):
            self.assertEqual(self.req("GET", path)[0], 401, path)
        self.login()
        status, _ = self.post("/api/flows", {"graph": {"name": "x", "nodes": [], "edges": []}}, csrf=False)
        self.assertEqual(status, 403)
        status, body = self.post("/api/flows", {"graph": {"name": "x", "nodes": [], "edges": []}})
        self.assertEqual(status, 201)
        self.assertEqual(self.req("PUT", f"/api/flows/{body['id']}", {})[0], 405)

    def test_crud_versions_templates_and_runs_over_http(self) -> None:
        self.login()
        status, cat = self.req("GET", "/api/flows/catalog")
        self.assertEqual(status, 200)
        self.assertGreater(len(cat["nodes"]), 60)
        status, opts = self.req("GET", "/api/flows/options")
        self.assertIn("visionmaster-pro-v3", {m["id"] for m in opts["image_models"]})
        status, tpls = self.req("GET", "/api/flows/templates")
        self.assertEqual(len([t for t in tpls["templates"] if t["builtin"]]), 5)
        status, flow = self.post("/api/flows", {"template_id": "builtin_talking_character", "name": "Mine"})
        self.assertEqual((status, flow["graph"]["name"]), (201, "Mine"))
        bad = json.loads(json.dumps(flow["graph"]))
        bad["edges"].append({"id": "zz", "source": "voice", "source_port": "audio", "target": "animate",
                             "target_port": "image"})
        status, err = self.post(f"/api/flows/{flow['id']}", {"graph": bad, "version": flow["version"]})
        self.assertEqual((status, err["error"]["code"]), (422, "invalid_flow"))
        self.assertEqual(err["error"]["issues"][0]["edge_id"], "zz")
        good = json.loads(json.dumps(flow["graph"]))
        good["name"] = "Renamed"
        status, updated = self.post(f"/api/flows/{flow['id']}", {"graph": good, "version": flow["version"]})
        self.assertEqual((status, updated["version"]), (200, 2))
        status, err = self.post(f"/api/flows/{flow['id']}", {"graph": good, "version": 1})
        self.assertEqual((status, err["error"]["code"]), (409, "version_conflict"))
        status, versions = self.req("GET", f"/api/flows/{flow['id']}/versions")
        self.assertEqual([v["version"] for v in versions["versions"]], [2, 1])
        status, restored = self.post(f"/api/flows/{flow['id']}/versions/1/restore")
        self.assertEqual((restored["version"], restored["name"]), (3, "Mine"))
        status, tpl = self.post("/api/flows/templates", {"name": "My template", "flow_id": flow["id"]})
        self.assertEqual(status, 201)
        status, dup = self.post(f"/api/flows/templates/{tpl['id']}/duplicate", {})
        self.assertEqual(dup["name"], "My template (copy)")
        status, err = self.post("/api/flows/templates/builtin_voiceover/delete", {"confirm": True})
        self.assertEqual((status, err["error"]["code"]), (409, "builtin"))
        status, _ = self.post(f"/api/flows/templates/{dup['id']}/delete", {"confirm": True})
        self.assertEqual(status, 200)
        status, run = self.post(f"/api/flows/{flow['id']}/run", {"mode": "full", "version": 3})
        self.assertEqual(status, 202)
        self.assertTrue(self.flows.engine.wait(run["id"], 30))
        status, state = self.req("GET", f"/api/flow-runs/{run['id']}")
        self.assertEqual(state["status"], "succeeded")
        status, node = self.req("GET", f"/api/flow-runs/{run['id']}/nodes/voice")
        self.assertIn("logs", node)
        status, err = self.post(f"/api/flows/{flow['id']}/run", {"mode": "node"})
        self.assertEqual((status, err["error"]["code"]), (400, "flow_error"))
        status, dupflow = self.post(f"/api/flows/{flow['id']}/duplicate")
        self.assertEqual(dupflow["name"], "Mine (copy)")
        status, err = self.post(f"/api/flows/{flow['id']}/delete", {})
        self.assertEqual(status, 400)
        status, _ = self.post(f"/api/flows/{flow['id']}/delete", {"confirm": True})
        self.assertEqual(self.req("GET", f"/api/flows/{flow['id']}")[0], 404)
        self.assertEqual(self.req("GET", "/api/flows/flow_nothex")[0], 404)

    def test_ai_generate_and_secrets(self) -> None:
        self.login()
        status, out = self.post("/api/flows/ai/generate", {
            "prompt": "Create a 30-second MVA Meta ad showing a woman whose BMW was rear-ended. Use a trustworthy "
                      "female voice, cinematic visuals, subtle background music and finish with a CTA."})
        self.assertEqual(status, 200, out)
        self.assertEqual(out["readiness"], [])
        status, created = self.post("/api/flows", {"graph": out["graph"]})
        self.assertEqual(status, 201)
        status, err = self.post("/api/flows/ai/generate", {"prompt": ""})
        self.assertEqual(status, 400)
        status, body = self.post("/api/flows/secrets", {"name": "crm_token", "value": "abc-123-secret"})
        self.assertEqual(body["secrets"][0]["name"], "crm_token")
        self.assertNotIn("abc-123-secret", json.dumps(body))
        status, body = self.req("GET", "/api/flows/secrets")
        self.assertNotIn("abc-123-secret", json.dumps(body))
        status, _ = self.post("/api/flows/secrets/crm_token/delete")
        self.assertEqual(status, 200)
        self.assertEqual(self.post("/api/flows/secrets/crm_token/delete")[0], 404)


class PublicApiTests(FlowBase):
    GRAPH = {"name": "API flow", "nodes": [n("p", "text.input", text="a lighthouse"), n("g", "image.generate")],
             "edges": [e("p", "text", "g", "prompt")]}

    def test_key_auth_ownership_and_rest(self) -> None:
        self.assertEqual(self.api("GET", "/v1/flows", None)[0], 401)
        self.assertEqual(self.api("GET", "/v1/flows", "sk-" + "x" * 30)[0], 401)
        self.login()
        status, _ = self.req("GET", "/v1/flows", headers={})  # a session cookie is not an API key
        self.assertEqual(status, 401)
        self.assertEqual(self.api("GET", "/v1/flows", self.k_chat)[0], 200)
        status, flow = self.api("POST", "/v1/flows", self.k_media, {"graph": self.GRAPH})
        self.assertEqual(status, 201, flow)
        fid = flow["id"]
        self.assertEqual(self.api("GET", f"/v1/flows/{fid}", self.k_other)[0], 404)
        self.assertEqual(len(self.api("GET", "/v1/flows", self.k_media)[1]["data"]), 1)
        self.assertEqual(self.api("GET", "/v1/flows", self.k_other)[1]["data"], [])
        graph = dict(self.GRAPH, name="API flow 2")
        status, put = self.api("PUT", f"/v1/flows/{fid}", self.k_media, {"graph": graph, "version": 1})
        self.assertEqual((status, put["version"], put["name"]), (200, 2, "API flow 2"))
        status, run = self.api("POST", f"/v1/flows/{fid}/run", self.k_media, {})
        self.assertEqual(status, 202, run)
        self.assertNotIn("owner", run)
        self.assertTrue(self.flows.engine.wait(run["id"], 20))
        status, state = self.api("GET", f"/v1/flow-runs/{run['id']}", self.k_media)
        self.assertEqual(state["status"], "succeeded")
        self.assertEqual(self.api("GET", f"/v1/flow-runs/{run['id']}", self.k_other)[0], 404)
        aid = state["summary"]["assets"][0]
        status, meta = self.api("GET", f"/v1/assets/{aid}", self.k_media)
        self.assertEqual((status, meta["flow_id"]), (200, fid))
        status, data = self.api("GET", f"/v1/assets/{aid}/content", self.k_media)
        self.assertEqual(status, 200)
        self.assertTrue(data.startswith(b"\x89PNG"))
        self.assertEqual(self.api("GET", f"/v1/assets/{aid}", self.k_other)[0], 404)
        foreign = self.image_asset()
        self.assertEqual(self.api("GET", f"/v1/assets/{foreign}", self.k_media)[0], 404)
        leak = {"name": "leak", "nodes": [n("u", "image.upload", asset_id=foreign)], "edges": []}
        status, err = self.api("POST", "/v1/flows", self.k_media, {"graph": leak})
        self.assertEqual(status, 404, err)
        status, _ = self.api("DELETE", f"/v1/flows/{fid}", self.k_other)
        self.assertEqual(status, 404)
        status, _ = self.api("DELETE", f"/v1/flows/{fid}", self.k_media)
        self.assertEqual(status, 200)
        self.assertEqual(self.api("GET", f"/v1/flows/{fid}", self.k_media)[0], 404)
        self.assertEqual(self.api("PATCH", "/v1/flows", self.k_media)[0], 405)

    def test_alias_authorisation(self) -> None:
        graph = {"name": "Music", "nodes": [n("m", "music.instrumental", description="piano")], "edges": []}
        status, flow = self.api("POST", "/v1/flows", self.k_media, {"graph": graph})
        status, err = self.api("POST", f"/v1/flows/{flow['id']}/run", self.k_media, {})
        self.assertEqual((status, err["error"]["code"]), (403, "forbidden"))
        self.assertIn("gx-music", err["error"]["message"])
        status, flow = self.api("POST", "/v1/flows", self.k_all, {"graph": graph})
        status, run = self.api("POST", f"/v1/flows/{flow['id']}/run", self.k_all, {})
        self.assertEqual(status, 202)
        self.flows.engine.wait(run["id"], 20)
        status, err = self.api("POST", "/v1/flows", self.k_media, {"graph": {"name": "x", "nodes": [
            n("a", "nope")], "edges": []}})
        self.assertEqual((status, err["error"]["code"]), (422, "invalid_flow"))


if __name__ == "__main__":
    unittest.main()
