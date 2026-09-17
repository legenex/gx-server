"""MUS routes (routes_mus.py) and the music additions to the public API,
through the real Control Center server with the node-2 music stub."""

from __future__ import annotations

import json
import time
import unittest

from test_music_ai import GOOD, FakeChat
from test_routes_v2 import V2Base

from gx_control_ui import netguard

MUS_POSTS = ["/api/music/preview", "/api/music/ai/build", "/api/music/ai/improve", "/api/music/reference/analyze"]
FORM = {"title": "", "description": "a song about rain", "style_tags": ["lo-fi"], "style_prompt": "soft piano",
        "instrumental": False, "vocal_intent": "auto", "vocal_language": "", "lyrics": "", "bpm": None,
        "key": None, "time_signature": None, "duration": None, "seed": None, "thinking": True,
        "inference_steps": None, "infer_method": None, "lm_temperature": None}


class MusRouteTests(V2Base):
    def setUp(self):
        super().setUp()
        self.chat = FakeChat(*([GOOD] * 6))
        self.app.music_ai.chat = self.chat
        self.app.music_ai.sleep = lambda s: None
        self.app.music_reference.poll = 0.02
        self.app.music_reference.fetch = lambda url, **kw: netguard.FetchResult(
            url, 200, {}, json.dumps({"title": "A public video", "author_name": "Someone"}).encode())

    def test_session_and_csrf_are_required(self):
        for path in MUS_POSTS:
            status, _, _ = self.req("POST", path, {})
            self.assertEqual(status, 401, path)
        self.assertEqual(self.req("GET", "/api/music/reference/" + "a" * 24)[0], 401)
        self.login()
        for path in MUS_POSTS:
            status, _, _ = self.post(path, {}, csrf=False)
            self.assertEqual(status, 403, path)
            status, _, _ = self.post(path, {}, headers={"Origin": "http://evil.example"})
            self.assertEqual(status, 403, path)
        self.assertEqual(self.chat.calls, [])

    def test_preview(self):
        self.login()
        body = {"description": "leaving Cape Town", "style_tags": ["cinematic"], "prompt": "intimate vocal",
                "vocal_intent": "female", "lyrics": "[Verse]\nGoodbye"}
        status, _, out = self.post("/api/music/preview", body)
        self.assertEqual(status, 200, out)
        self.assertEqual(out["conditioning"]["caption"], "intimate vocal, cinematic, female vocals. leaving Cape Town.")
        self.assertEqual(out["vocal_mode"], "vocals")
        status, _, out = self.post("/api/music/preview", {"style_tags": ["female vocals"]})
        self.assertEqual((status, out["error"]["code"]), (400, "lyrics_required"))
        status, _, out = self.post("/api/music/preview", {**body, "lyrics": "", "lyrics_source": "assistant"})
        self.assertEqual(status, 200, out)
        self.assertTrue(out["lyrics_pending"])
        self.assertEqual(out["conditioning"]["lyrics"], "")
        self.assertEqual(self.chat.calls, [])  # a preview never asks gx-auto for lyrics
        status, _, out = self.post("/api/music/preview", {"prompt": "x", "api_url": "http://evil"})
        self.assertEqual(status, 400)

    def test_write_with_ai_fills_lyrics_before_queueing(self):
        self.login()
        self.chat.replies = [{"lyrics": "[Verse]\nRain on the window\n[Chorus]\nStay"}]
        status, _, job = self.post("/api/music/jobs", {"description": "a song about rain", "prompt": "soft piano",
                                                       "vocal_intent": "female", "lyrics_source": "assistant"},
                                   headers=self.playground())
        self.assertEqual(status, 202, job)
        sent = self.stub.jobs[job["id"]]["request"]
        self.assertEqual(sent["lyrics"], "[Verse]\nRain on the window\n[Chorus]\nStay")
        self.assertEqual(sent["lyrics_source"], "assistant")
        self.assertEqual(self.chat.calls[0]["schema_name"], "gx_music_lyrics")
        entry = [e for e in self.audit() if e.get("action") == "music.generate"][-1]
        self.assertTrue(entry["lyrics_written"])
        # without the writer option a vocal request with no lyrics is refused by gx10-02
        status, _, out = self.post("/api/music/jobs", {"style_tags": ["female vocals", "house"]})
        self.assertEqual((status, out["error"]["code"]), (400, "lyrics_required"))

    def test_build_and_improve(self):
        self.login()
        status, _, out = self.post("/api/music/ai/build", {"prompt": "dark but uplifting afro house, female vocal",
                                                           "current": FORM, "locked": ["description"],
                                                           "write_lyrics": True})
        self.assertEqual(status, 200, out)
        self.assertEqual(out["settings"]["description"], "a song about rain")
        self.assertEqual(out["settings"]["bpm"], 122)
        self.assertEqual(self.chat.calls[0]["schema"]["additionalProperties"], False)
        entry = [e for e in self.audit() if e.get("action") == "music.ai.build"][-1]
        self.assertEqual(entry["outcome"], "ok")
        self.assertNotIn("prompt", entry)  # the request text is not logged
        status, _, out = self.post("/api/music/ai/improve", {"current": {**FORM, "bpm": 90}, "locked": [],
                                                             "improve_lyrics": False})
        self.assertEqual(status, 200, out)
        self.assertEqual(out["settings"]["bpm"], 90)
        self.assertTrue(any(c["field"] == "bpm" and c["action"] == "suggested" for c in out["changes"]))
        for bad in ({"prompt": "house", "current": {"engine_url": "x"}}, {"prompt": "house", "locked": "bpm"},
                    {"prompt": "house", "write_lyrics": "yes"}, {"prompt": ""}):
            status, _, out = self.post("/api/music/ai/build", bad)
            self.assertEqual(status, 400, (bad, out))
        status, _, out = self.post("/api/music/ai/improve", {"current": {**FORM, "description": "",
                                                                         "style_tags": [], "style_prompt": ""}})
        self.assertEqual((status, out["error"]["code"]), (400, "nothing_to_improve"))

    def test_gateway_failure_is_reported_not_faked(self):
        self.login()
        self.chat.replies = ["{broken", "{broken", "{broken"]
        status, _, out = self.post("/api/music/ai/build", {"prompt": "house music", "current": FORM})
        self.assertEqual((status, out["error"]["code"]), (502, "ai_invalid"))

    def test_reference_url_and_upload(self):
        self.login()
        status, _, s = self.post("/api/music/reference/analyze",
                                 {"source": {"kind": "url", "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}},
                                 headers=self.playground())
        self.assertEqual(status, 202, s)
        out = self.wait_reference(s["id"])
        self.assertEqual(out["state"], "done", out)
        self.assertFalse(out["audio_analysed"])
        self.assertEqual(out["source"]["title"], "A public video")
        status, _, bad = self.post("/api/music/reference/analyze",
                                   {"source": {"kind": "url", "url": "http://127.0.0.1:8088/api/keys"}})
        self.assertEqual((status, bad["error"]["code"]), (400, "invalid_request"))
        # upload -> Library asset -> measured analysis
        wav = self.stub.wav
        status, _, asset = self.req("POST", "/api/music/upload", raw_body=wav,
                                    headers={"Content-Type": "audio/wav", "X-CSRF-Token": self.csrf,
                                             "Origin": f"http://127.0.0.1:{self.port}", "X-Filename": "ref.wav"})
        self.assertEqual(status, 200, asset)
        status, _, s = self.post("/api/music/reference/analyze",
                                 {"source": {"kind": "asset", "asset_id": asset["id"]}, "understand": True})
        self.assertEqual(status, 202, s)
        out = self.wait_reference(s["id"])
        self.assertEqual(out["state"], "done", out)
        self.assertTrue(out["audio_analysed"])
        self.assertEqual(out["field_sources"]["bpm"], "measured")
        # analyses never show up as creative jobs
        status, _, jobs = self.req("GET", "/api/music/jobs")
        self.assertEqual([j for j in jobs["jobs"] if j.get("operation") == "analyze"], [])
        # upload type limits
        status, _, _ = self.req("POST", "/api/music/upload", raw_body=b"<svg/>",
                                headers={"Content-Type": "image/svg+xml", "X-CSRF-Token": self.csrf,
                                         "Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(status, 400)

    def wait_reference(self, sid, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            status, _, out = self.req("GET", f"/api/music/reference/{sid}")
            self.assertEqual(status, 200, out)
            if out["state"] in ("done", "failed"):
                return out
            time.sleep(0.05)
        self.fail("reference analysis did not finish")

    def test_public_api_preview_and_new_fields(self):
        status, _, out = self.api("POST", "/v1/music/preview", self.k_music,
                                  {"prompt": "house", "vocal_intent": "male", "lyrics": "[Verse]\nhey"})
        self.assertEqual(status, 200, out)
        self.assertEqual(out["conditioning"]["caption"], "house, male vocals")
        status, _, out = self.api("POST", "/v1/music/preview", self.k_mini, {"prompt": "house"})
        self.assertEqual(status, 403)
        status, _, job = self.api("POST", "/v1/music/generations", self.k_music,
                                  {"description": "a song", "prompt": "rock", "vocal_intent": "male",
                                   "lyrics": "[Verse]\nhey", "lm_caption_rewrite": False})
        self.assertEqual(status, 202, job)
        self.assertEqual(job["request"]["description"], "a song")
        status, _, out = self.api("POST", "/v1/music/generations", self.k_music, {"style_tags": ["male vocals"]})
        self.assertEqual((status, out["error"]["code"]), (400, "lyrics_required"))


if __name__ == "__main__":
    unittest.main()
