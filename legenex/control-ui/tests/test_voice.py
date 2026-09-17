"""Build V3 VOI: Voice Studio on gx10-01 (voice.py, routes_voi.py, migration 040).

The node-2 side is the REAL gx-voice supervisor with a stub engine
(e2e/voice_stub.py), so these are protocol-level integration tests: every
request crosses the real gx-voice HTTP API. LiteLLM is a stub for /key/info.
"""

from __future__ import annotations

import http.client
import json
import secrets
import sys
import threading
import time
import unittest

from support import UI_DIR, StubUpstream, TempEnv, fake_key

from gx_control_ui import auth
from gx_control_ui import server as srv
from gx_control_ui.media_library import MediaLibrary, MediaTools
from gx_control_ui.voice import DEFAULT_CONSENT, PRESETS, VoiceClient, VoiceError, VoiceStudio

sys.path.insert(0, str(UI_DIR / "e2e"))
from voice_stub import VoiceStub, reference_wav  # noqa: E402

PASSWORD = "Test-Password-For-Voice-40"
CONSENT = {"confirmed": True, "statement": DEFAULT_CONSENT}


class Audit(list):
    def __call__(self, **kw):
        self.append(kw)


class Results:
    def __init__(self):
        self.records = []

    def record(self, alias, kind, ok, detail="", **kw):
        self.records.append((alias, kind, ok, detail))


class StudioBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = secrets.token_urlsafe(32)
        cls.node = VoiceStub(cls.key)

    @classmethod
    def tearDownClass(cls):
        cls.node.close()

    def setUp(self):
        self.env = TempEnv(voice_base=self.node.url)
        self.env.cfg.voice_key_file.write_text(self.key + "\n")
        self.library = MediaLibrary(self.env.cfg.media_dir, MediaTools(enabled=False))
        self.audit = Audit()
        self.results = Results()
        self.studio = VoiceStudio(VoiceClient(self.node.url, self.env.cfg.voice_key_file), self.library,
                                  self.env.cfg.media_dir / "voice", audit=self.audit, results=self.results,
                                  explain=lambda alias: {"alias": alias, "reason": "test"}, start_worker=False)

    def tearDown(self):
        self.env.cleanup()

    def finish(self, job_id, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.studio.sweep()
            job = self.studio.get(job_id)
            if job["status"] in ("completed", "failed", "cancelled"):
                return job
            time.sleep(0.05)
        self.fail(f"job {job_id} did not finish: {self.studio.get(job_id)}")

    def ref_asset(self, seconds=3.0):
        data = reference_wav(self.env.root / f"ref-{secrets.token_hex(4)}.wav", seconds)
        return self.studio.upload_reference(data, "speaker.wav", "audio/wav", user="admin", title="Speaker")


class ClientAndVoicesTests(StudioBase):
    def test_key_handling(self):
        self.env.cfg.voice_key_file.write_text("short")
        with self.assertRaises(VoiceError) as cm:
            self.studio.model()
        self.assertEqual((cm.exception.status, cm.exception.code), (503, "not_configured"))
        self.env.cfg.voice_key_file.write_text(secrets.token_urlsafe(32))
        with self.assertRaises(VoiceError) as cm:
            self.studio.client.call("GET", "/v1/voice/model")
        self.assertEqual(cm.exception.status, 502)

    def test_model_and_presets(self):
        info = self.studio.model()
        self.assertEqual(info["alias"], "gx-voice")
        self.assertIn(info["state"], ("unloaded", "ready", "busy", "loading"))
        voices = self.studio.list_voices()
        presets = [v for v in voices if v["builtin"]]
        self.assertEqual({v["speaker"] for v in presets}, set(PRESETS))
        self.assertEqual(self.studio.get_voice("preset:ryan")["name"], "Ryan")
        with self.assertRaises(VoiceError):
            self.studio.get_voice("preset:alloy")
        with self.assertRaises(VoiceError):
            self.studio.get_voice("../etc")

    def test_preset_voice_lifecycle_and_versions(self):
        v = self.studio.create_voice({"kind": "preset", "name": "Promo Host", "speaker": "aiden",
                                      "instructions": "energetic, upbeat", "language": "english",
                                      "style": {"speed": 1.1, "pause_ms": 250}}, user="admin")
        self.assertTrue(v["synced"])
        self.assertEqual(v["version"], 1)
        replica = self.node.rig.store.get_voice(v["id"])
        self.assertEqual(replica["spec"], {"kind": "preset", "speaker": "aiden"})
        with self.assertRaises(VoiceError) as cm:
            self.studio.create_voice({"kind": "preset", "name": "promo  HOST", "speaker": "ryan"}, user="admin")
        self.assertEqual(cm.exception.code, "name_taken")
        with self.assertRaises(VoiceError):
            self.studio.create_voice({"kind": "preset", "name": "Ryan", "speaker": "ryan"}, user="admin")
        up = self.studio.update_voice(v["id"], {"instructions": "calm", "name": "Promo Host 2"}, user="admin")
        self.assertEqual(up["version"], 2)
        self.assertEqual(self.node.rig.store.get_voice(v["id"])["version"], 2)
        versions = self.studio.voice_versions(v["id"])
        self.assertEqual([x["version"] for x in versions], [2, 1])
        self.assertEqual(versions[1]["snapshot"]["instructions"], "energetic, upbeat")
        self.studio.delete_voice(v["id"], user="admin")
        self.assertIsNone(self.node.rig.store.get_voice(v["id"]))
        with self.assertRaises(VoiceError):
            self.studio.get_voice(v["id"])
        actions = [a["action"] for a in self.audit]
        self.assertEqual(actions.count("voice.create"), 1)
        self.assertIn("voice.delete", actions)

    def test_invalid_voice_bodies(self):
        bad = [{"kind": "robot", "name": "x"}, {"kind": "preset", "name": "", "speaker": "ryan"},
               {"kind": "preset", "name": "x", "speaker": "alloy"},
               {"kind": "preset", "name": "x", "speaker": "ryan", "style": {"speed": 9}},
               {"kind": "preset", "name": "x", "speaker": "ryan", "language": "elvish"},
               {"kind": "preset", "name": "x", "speaker": "ryan", "evil": 1},
               {"kind": "designed", "name": "x", "job_id": "nope"},
               {"kind": "cloned", "name": "x", "reference_asset_id": "a_zz"}]
        for body in bad:
            with self.subTest(body=body), self.assertRaises(VoiceError):
                self.studio.create_voice(body, user="admin")

    def test_reconcile_repushes_lost_replicas(self):
        v = self.studio.create_voice({"kind": "preset", "name": "Lost", "speaker": "serena"}, user="admin")
        self.node.rig.store.delete_voice(v["id"])
        self.assertGreaterEqual(self.studio.reconcile_voices(), 1)
        self.assertIsNotNone(self.node.rig.store.get_voice(v["id"]))


class JobTests(StudioBase):
    def test_tts_takes_download_verify_and_save(self):
        job = self.studio.submit({"operation": "tts", "text": "Welcome to the launch event.",
                                  "voice_id": "preset:ryan", "takes": 2, "seed": 11, "title": "Launch VO"},
                                 user="admin", via="playground")
        self.assertTrue(job["id"].startswith("vj_"))
        self.assertIn(job["status"], ("queued", "loading_model", "generating", "processing", "saving"))
        done = self.finish(job["id"])
        self.assertEqual(done["status"], "completed", done)
        self.assertEqual([t["seed"] for t in done["takes"]], [11, 12])
        self.assertEqual(done["takes"][0]["formats"], ["mp3", "wav"])
        self.assertTrue(done["takes"][1]["waveform"])
        self.assertIn("generate_s", done["timings"])
        wav = self.studio.take_file(job["id"], 0, "wav")
        self.assertEqual(wav.read_bytes()[:4], b"RIFF")
        asset = self.studio.save_take(job["id"], 1, user="admin")
        self.assertEqual((asset["type"], asset["operation"], asset["model_alias"]), ("audio", "tts", "gx-voice"))
        self.assertEqual(asset["source_kind"], "voice_take")
        self.assertEqual(asset["source_ref"], f"{job['id']}#1")
        self.assertIn("mp3", asset["variants"])
        self.assertEqual(asset["model_revision"], "0c0e3051f131929182e2c023b9537f8b1c68adfe")
        self.assertEqual(self.studio.save_take(job["id"], 1, user="admin")["id"], asset["id"])  # idempotent
        self.assertEqual(self.studio.get(job["id"])["takes"][1]["asset_id"], asset["id"])
        self.assertTrue(any(r[2] for r in self.results.records))
        with self.assertRaises(VoiceError):
            self.studio.take_file(job["id"], 3, "wav")
        with self.assertRaises(VoiceError):
            self.studio.take_file(job["id"], 0, "exe")

    def test_design_then_saved_designed_voice_uses_base(self):
        job = self.studio.submit({"operation": "voice_design", "text": "Hello there, this is my new voice. " * 3,
                                  "description": "gravelly old sea captain, slow and warm"}, user="admin")
        done = self.finish(job["id"])
        self.assertEqual(done["status"], "completed", done)
        self.assertEqual(self.node.engine_calls[-1]["variant"], "design")
        voice = self.studio.create_voice({"kind": "designed", "name": "Captain", "job_id": job["id"], "take": 0},
                                         user="admin")
        self.assertEqual(voice["kind"], "designed")
        self.assertEqual(voice["description"], "gravelly old sea captain, slow and warm")
        self.assertTrue(voice["reference_asset_id"])
        ref_asset = self.library.get(voice["reference_asset_id"])
        self.assertEqual(ref_asset["operation"], "voice_design")
        use = self.studio.submit({"operation": "tts", "text": "Ahoy, the tide is turning.",
                                  "voice_id": voice["id"]}, user="admin")
        done = self.finish(use["id"])
        self.assertEqual(done["status"], "completed", done)
        call = self.node.engine_calls[-1]
        self.assertEqual(call["variant"], "base")
        self.assertEqual(call["reference"]["text"], ("Hello there, this is my new voice. " * 3).strip())
        saved = self.studio.save_take(use["id"], 0, user="admin")
        self.assertEqual(saved["parent_id"], voice["reference_asset_id"])
        self.assertEqual(saved["model_repo"], "Qwen/Qwen3-TTS-12Hz-1.7B-Base")
        with self.assertRaises(VoiceError):
            self.studio.create_voice({"kind": "designed", "name": "Other", "job_id": use["id"], "take": 0},
                                     user="admin")

    def test_clone_requires_and_records_consent(self):
        asset = self.ref_asset()
        body = {"operation": "voice_clone", "text": "A line in the cloned voice.",
                "reference": {"asset_id": asset["id"], "transcript": "Reference words."}}
        with self.assertRaises(VoiceError) as cm:
            self.studio.submit(body, user="admin")
        self.assertEqual((cm.exception.status, cm.exception.code), (403, "consent_required"))
        body["reference"]["consent"] = {"confirmed": "yes"}
        with self.assertRaises(VoiceError):
            self.studio.submit(body, user="admin")
        body["reference"]["consent"] = CONSENT
        job = self.studio.submit(body, user="admin", ip="127.0.0.1")
        self.assertNotIn("consent", json.dumps(job["request"]).replace("consent_id", ""))
        done = self.finish(job["id"])
        self.assertEqual(done["status"], "completed", done)
        self.assertEqual(self.node.engine_calls[-1]["variant"], "base")
        with self.library.connect() as con:
            row = con.execute("SELECT * FROM voice_consents WHERE job_id=?", (job["id"],)).fetchone()
        self.assertEqual(row["reference_asset_id"], asset["id"])
        self.assertEqual(row["reference_sha256"], asset["sha256"])
        self.assertEqual(row["confirmed_by"], "admin")
        self.assertEqual(row["statement"], DEFAULT_CONSENT)
        saved = self.studio.save_take(job["id"], 0, user="admin")
        self.assertEqual((saved["operation"], saved["parent_id"]), ("voice_clone", asset["id"]))

    def test_cloned_voice_and_transcript_versioning(self):
        asset = self.ref_asset()
        with self.assertRaises(VoiceError) as cm:
            self.studio.create_voice({"kind": "cloned", "name": "Me", "reference_asset_id": asset["id"]},
                                     user="admin")
        self.assertEqual(cm.exception.code, "consent_required")
        v = self.studio.create_voice({"kind": "cloned", "name": "Me", "reference_asset_id": asset["id"],
                                      "consent": CONSENT}, user="admin", ip="10.0.0.1")
        self.assertTrue(v["x_vector_only"])
        self.assertTrue(v["consent_id"].startswith("vcs_"))
        self.assertEqual(self.node.rig.store.get_voice(v["id"])["spec"]["kind"], "reference")
        v2 = self.studio.update_voice(v["id"], {"transcript": "What I said in the clip."}, user="admin")
        self.assertFalse(v2["x_vector_only"])
        self.assertEqual(self.node.rig.store.get_voice(v["id"])["spec"]["transcript"], "What I said in the clip.")
        # gx10-02 lost the clip: the next use uploads it again from the Library
        ref_id = self.node.rig.store.get_voice(v["id"])["spec"]["reference_id"]
        self.node.rig.store.delete_voice(v["id"])
        self.node.rig.service.delete_reference(ref_id)
        job = self.studio.submit({"operation": "tts", "text": "Still me.", "voice_id": v["id"]}, user="admin")
        self.assertEqual(self.finish(job["id"])["status"], "completed")
        self.assertIsNotNone(self.node.rig.store.get_voice(v["id"]))

    def test_dialogue_auto_save_and_flow_provenance(self):
        host = self.studio.create_voice({"kind": "preset", "name": "Host", "speaker": "ryan"}, user="admin")
        flow = {"flow_id": "flw_demo", "flow_run_id": "run_1", "flow_node_id": "node_7"}
        job = self.studio.submit({"operation": "dialogue", "auto_save": True, "flow": flow, "pause_ms": 300,
                                  "segments": [
                                      {"voice_id": host["id"], "text": "Welcome back to the show."},
                                      {"voice_id": "preset:serena", "text": "Thanks for having me.",
                                       "instructions": "cheerful"}]},
                                 user="flows", via="flow")
        done = self.finish(job["id"])
        self.assertEqual(done["status"], "completed", done)
        self.assertEqual(done["flow"], flow)
        asset_id = done["takes"][0]["asset_id"]
        self.assertTrue(asset_id)
        asset = self.library.get(asset_id)
        self.assertEqual((asset["operation"], asset["flow_id"], asset["flow_run_id"], asset["flow_node_id"]),
                         ("tts", "flw_demo", "run_1", "node_7"))
        self.assertEqual([j["id"] for j in self.studio.list_jobs(flow_run_id="run_1")], [job["id"]])
        wait = self.studio.wait(job["id"], timeout=1)
        self.assertEqual(wait["status"], "completed")

    def test_invalid_jobs(self):
        bad = [{"operation": "sing", "text": "x"}, {"operation": "tts", "text": "x"},
               {"operation": "tts", "text": "", "voice_id": "preset:ryan"},
               {"operation": "tts", "text": "x" * 10001, "voice_id": "preset:ryan"},
               {"operation": "tts", "text": "x", "voice_id": "preset:ryan", "takes": 9},
               {"operation": "tts", "text": "x", "voice_id": "preset:ryan", "engine_url": "http://evil"},
               {"operation": "voice_design", "text": "x"},
               {"operation": "voice_clone", "text": "x", "reference": {"asset_id": "a_" + "0" * 24}},
               {"operation": "dialogue", "segments": []},
               {"operation": "dialogue", "segments": [{"voice_id": "preset:ryan", "text": "x", "path": "/"}]},
               {"operation": "tts", "text": "x", "voice_id": "preset:ryan", "flow": {"flow_id": "../x"}},
               {"operation": "tts", "text": "x", "voice_id": "preset:ryan", "auto_save": "yes"}]
        for body in bad:
            with self.subTest(body=str(body)[:70]), self.assertRaises(Exception) as cm:
                self.studio.submit(body, user="admin")
            self.assertIn(type(cm.exception).__name__, ("VoiceError", "LibraryError"))
        with self.assertRaises(VoiceError):
            self.studio.submit({"operation": "tts", "text": "x", "voice_id": "preset:ryan"}, user="a", via="evil")

    def test_cancel_and_delete(self):
        self.node.rig.service.stop()  # nothing picks the job up: it stays queued
        try:
            job = self.studio.submit({"operation": "tts", "text": "Cancel me.", "voice_id": "preset:ryan"},
                                     user="admin")
            out = self.studio.cancel(job["id"], user="admin")
            self.assertEqual(out["status"], "cancelled")
            with self.assertRaises(VoiceError):
                self.studio.cancel(job["id"], user="admin")
            self.assertTrue(self.studio.delete_job(job["id"], user="admin")["deleted"])
            with self.assertRaises(VoiceError):
                self.studio.get(job["id"])
        finally:
            self.node.rig.service._stop.clear()  # noqa: SLF001
            self.node.rig.service.start()

    def test_upload_reference_validation(self):
        with self.assertRaises(VoiceError):
            self.studio.upload_reference(b"x", "a.txt", "text/plain", user="admin")
        with self.assertRaises(VoiceError):
            self.studio.upload_reference(b"", "a.wav", "audio/wav", user="admin")
        short = reference_wav(self.env.root / "short.wav", 0.5)
        with self.assertRaises(VoiceError) as cm:
            self.studio.upload_reference(short, "short.wav", "audio/wav", user="admin")
        self.assertIn("2-60 seconds", str(cm.exception))
        asset = self.ref_asset()
        self.assertEqual((asset["type"], asset["operation"], asset["source_kind"]),
                         ("audio", "upload", "voice_reference"))


# ------------------------------------------------------------------ HTTP
class RouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = secrets.token_urlsafe(32)
        cls.node = VoiceStub(cls.key)
        cls.k_voice = fake_key()
        cls.k_other = fake_key()
        cls.k_music = fake_key()
        infos = {cls.k_voice: {"key_alias": "voice-app", "models": ["gx-voice"]},
                 cls.k_other: {"key_alias": "voice-app-2", "models": []},
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
        self.env = TempEnv(voice_base=self.node.url, litellm_base=self.litellm.url)
        self.env.cfg.voice_key_file.write_text(self.key)
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

    def req(self, method, path, body=None, headers=None, raw=None, cookie=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        hdrs = {"Host": f"127.0.0.1:{self.port}"}
        if cookie and self.cookie:
            hdrs["Cookie"] = self.cookie
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
        return resp.status, dict(resp.getheaders()), parsed

    def login(self):
        status, hdrs, _ = self.req("POST", "/api/login", {"username": "admin", "password": PASSWORD})
        self.assertEqual(status, 200)
        self.cookie = hdrs["Set-Cookie"].split(";")[0]
        self.csrf = self.req("GET", "/api/session")[2]["csrf"]

    def finish(self, job_id, path_prefix="/api/voice/jobs", headers=None, cookie=True):
        for _ in range(400):
            self.app.voice.sweep()
            status, _, job = self.req("GET", f"{path_prefix}/{job_id}", headers=headers, cookie=cookie)
            if job["status"] in ("completed", "failed", "cancelled"):
                return job
            time.sleep(0.05)
        self.fail("job did not finish")

    def test_session_routes_require_login_and_csrf(self):
        self.assertEqual(self.req("GET", "/api/voice/voices")[0], 401)
        self.login()
        csrf, self.csrf = self.csrf, None
        status, _, _ = self.req("POST", "/api/voice/jobs", {"operation": "tts", "text": "x",
                                                            "voice_id": "preset:ryan"})
        self.assertEqual(status, 403)
        self.csrf = csrf
        status, _, body = self.req("GET", "/api/voice/voices")
        self.assertEqual(status, 200)
        self.assertEqual(len([v for v in body["voices"] if v["builtin"]]), 9)
        self.assertEqual(self.req("GET", "/api/voice/voices/vc_zz")[0], 404)

    def test_session_job_play_save_and_delete(self):
        self.login()
        status, _, job = self.req("POST", "/api/voice/jobs", {"operation": "tts", "text": "Studio take.",
                                                              "voice_id": "preset:aiden", "takes": 2})
        self.assertEqual(status, 202, job)
        done = self.finish(job["id"])
        self.assertEqual(done["status"], "completed")
        status, hdrs, audio = self.req("GET", done["takes"][0]["audio_url"])
        self.assertEqual((status, hdrs["Content-Type"]), (200, "audio/wav"))
        self.assertEqual(audio[:4], b"RIFF")
        status, hdrs, part = self.req("GET", done["takes"][0]["audio_url"], headers={"Range": "bytes=0-3"})
        self.assertEqual((status, part), (206, b"RIFF"))
        status, hdrs, _ = self.req("GET", done["takes"][1]["audio_url"] + "?format=mp3&download=1")
        self.assertEqual(status, 200)
        self.assertIn("attachment", hdrs["Content-Disposition"])
        status, _, asset = self.req("POST", f"/api/voice/jobs/{job['id']}/takes/1/save", {"title": "Keeper"})
        self.assertEqual((status, asset["title"], asset["operation"]), (200, "Keeper", "tts"))
        self.assertEqual(self.req("POST", f"/api/voice/jobs/{job['id']}/delete", {})[0], 400)
        self.assertEqual(self.req("POST", f"/api/voice/jobs/{job['id']}/delete", {"confirm": True})[0], 200)
        self.assertEqual(self.req("GET", f"/api/voice/jobs/{job['id']}")[0], 404)
        self.app.library.get(asset["id"])  # the Library copy stays

    def test_session_upload_and_clone_voice(self):
        self.login()
        data = reference_wav(self.env.root / "r.wav")
        status, _, asset = self.req("POST", "/api/voice/upload", raw=data,
                                    headers={"Content-Type": "audio/wav", "X-Filename": "me.wav"})
        self.assertEqual(status, 200, asset)
        status, _, err = self.req("POST", "/api/voice/voices", {"kind": "cloned", "name": "Clone",
                                                                "reference_asset_id": asset["id"]})
        self.assertEqual((status, err["error"]["code"]), (403, "consent_required"))
        status, _, voice = self.req("POST", "/api/voice/voices", {
            "kind": "cloned", "name": "Clone", "reference_asset_id": asset["id"], "consent": CONSENT})
        self.assertEqual(status, 201, voice)
        status, _, versions = self.req("GET", f"/api/voice/voices/{voice['id']}/versions")
        self.assertEqual(len(versions["versions"]), 1)
        status, _, up = self.req("POST", f"/api/voice/voices/{voice['id']}", {"description": "my own voice"})
        self.assertEqual((status, up["version"]), (200, 2))
        self.assertEqual(self.req("POST", f"/api/voice/voices/{voice['id']}/delete", {"confirm": True})[0], 200)
        status, _, _ = self.req("POST", "/api/voice/upload", raw=b"<svg/>",
                                headers={"Content-Type": "image/svg+xml"})
        self.assertEqual(status, 400)

    def test_playground_cannot_drive_lifecycle(self):
        self.login()
        status, _, _ = self.req("POST", "/api/voice/unload", {"if_idle": True},
                                headers={"X-GX-Proxy-Token": self.app.proxy_token})
        self.assertEqual(status, 403)

    def test_public_api_keys_and_ownership(self):
        auth_voice = {"Authorization": f"Bearer {self.k_voice}"}
        auth_other = {"Authorization": f"Bearer {self.k_other}"}
        self.assertEqual(self.req("GET", "/v1/voice/voices", cookie=False)[0], 401)
        status, _, body = self.req("GET", "/v1/voice/voices", headers={"Authorization": f"Bearer {self.k_music}"})
        self.assertEqual((status, body["error"]["code"]), (403, "forbidden"))
        self.login()  # a session cookie alone never works on /v1
        self.assertEqual(self.req("GET", "/v1/voice/voices")[0], 401)
        status, _, body = self.req("GET", "/v1/voice/model", headers=auth_voice, cookie=False)
        self.assertEqual((status, body["alias"]), (200, "gx-voice"))
        self.assertNotIn("health", body)
        status, hdrs, job = self.req("POST", "/v1/voice/speech", {"text": "API voice.", "voice_id": "preset:ryan"},
                                     headers=auth_voice, cookie=False)
        self.assertEqual(status, 202, job)
        self.assertEqual(hdrs["Location"], f"/v1/voice/jobs/{job['id']}")
        self.assertNotIn("user", job)
        done = self.finish(job["id"], "/v1/voice/jobs", headers=auth_voice, cookie=False)
        self.assertEqual(done["status"], "completed")
        url = done["takes"][0]["files"]["wav"]
        status, hdrs, audio = self.req("GET", url, headers=auth_voice, cookie=False)
        self.assertEqual((status, audio[:4]), (200, b"RIFF"))
        # another key cannot see or fetch it
        self.assertEqual(self.req("GET", f"/v1/voice/jobs/{job['id']}", headers=auth_other, cookie=False)[0], 404)
        self.assertEqual(self.req("GET", url, headers=auth_other, cookie=False)[0], 404)
        status, _, listing = self.req("GET", "/v1/voice/jobs", headers=auth_other, cookie=False)
        self.assertEqual(listing["data"], [])
        status, _, saved = self.req("POST", f"/v1/voice/jobs/{job['id']}/takes/0/save", {},
                                    headers=auth_voice, cookie=False)
        self.assertEqual(status, 200)
        self.assertTrue(saved["asset_id"].startswith("a_"))
        status, _, err = self.req("POST", "/v1/voice/design", {"operation": "tts", "text": "x"},
                                  headers=auth_voice, cookie=False)
        self.assertEqual(status, 400)
        self.assertEqual(self.req("POST", "/v1/voice/unload", {}, headers=auth_voice, cookie=False)[0], 403)
        self.assertEqual(self.req("GET", "/v1/voice/nothing", headers=auth_voice, cookie=False)[0], 404)

    def test_public_upload_and_clone(self):
        auth_voice = {"Authorization": f"Bearer {self.k_voice}"}
        data = reference_wav(self.env.root / "p.wav")
        status, _, up = self.req("POST", "/v1/voice/uploads", raw=data, cookie=False,
                                 headers={**auth_voice, "Content-Type": "audio/wav", "X-Filename": "p.wav"})
        self.assertEqual(status, 201, up)
        status, _, err = self.req("POST", "/v1/voice/clone", {"text": "Cloned via API.",
                                                              "reference": {"asset_id": up["asset_id"]}},
                                  headers=auth_voice, cookie=False)
        self.assertEqual((status, err["error"]["code"]), (403, "consent_required"))
        status, _, job = self.req("POST", "/v1/voice/clone", {
            "text": "Cloned via API.", "reference": {"asset_id": up["asset_id"], "consent": CONSENT}},
            headers=auth_voice, cookie=False)
        self.assertEqual(status, 202, job)
        self.assertEqual(self.finish(job["id"], "/v1/voice/jobs", headers=auth_voice, cookie=False)["status"],
                         "completed")


if __name__ == "__main__":
    unittest.main()
