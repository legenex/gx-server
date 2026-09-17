"""Reference analysis (gx_control_ui.music_reference) against the node-2
music stub and a scripted gx-auto. No request leaves 127.0.0.1."""

from __future__ import annotations

import functools
import json
import secrets
import socket
import unittest
from unittest import mock

from support import TempEnv
from test_music import StubBase, sine_wav

from gx_control_ui import netguard
from gx_control_ui import music_reference as ref
from gx_control_ui.media_library import NewAsset
from gx_control_ui.music_ai import MusicAI
from test_music_ai import GOOD, FakeChat


class ClassifyTests(unittest.TestCase):
    def test_accepted_links_are_rebuilt_from_the_id(self):
        vid = "dQw4w9WgXcQ"
        for url in (f"https://www.youtube.com/watch?v={vid}&t=10s&list=x", f"https://youtu.be/{vid}?si=abc",
                    f"https://m.youtube.com/watch?v={vid}", f"https://music.youtube.com/watch?v={vid}",
                    f"https://youtube.com/shorts/{vid}", f"http://www.youtube.com/embed/{vid}"):
            with self.subTest(url=url):
                self.assertEqual(ref.classify_url(url), ("youtube", f"https://www.youtube.com/watch?v={vid}"))
        sid = "4uLU6hMCjMI75M1A2tKUQC"
        for url in (f"https://open.spotify.com/track/{sid}?si=1", f"https://open.spotify.com/intl-de/track/{sid}"):
            self.assertEqual(ref.classify_url(url), ("spotify", f"https://open.spotify.com/track/{sid}"))
        self.assertEqual(ref.classify_url(f"https://open.spotify.com/album/{sid}")[1],
                         f"https://open.spotify.com/album/{sid}")

    def test_rejected_links(self):
        vid = "dQw4w9WgXcQ"
        bad = ["", None, 5, "not a url", "javascript:alert(1)", "file:///etc/passwd", "ftp://youtube.com/x",
               f"https://youtube.com.evil.example/watch?v={vid}", f"https://evil.example/youtube.com/watch?v={vid}",
               f"https://user:pw@www.youtube.com/watch?v={vid}", f"https://www.youtube.com:8443/watch?v={vid}",
               "https://www.youtube.com/watch?v=short", "https://www.youtube.com/watch?v=../../../../x",
               "https://www.youtube.com/results?search_query=x", "https://127.0.0.1/watch?v=" + vid,
               "http://169.254.169.254/latest/meta-data", "https://gx10-02:18820/v1/music/model",
               "https://192.168.100.11/watch?v=" + vid, "https://open.spotify.com/track/../../x",
               "https://open.spotify.com/user/abc", "https://spotify.com/track/4uLU6hMCjMI75M1A2tKUQC",
               "https://www.youtube.com/watch?v=" + vid + " x", "https://www.youtube.com/watch?v=" + "a" * 600]
        for url in bad:
            with self.subTest(url=str(url)[:60]), self.assertRaises(ref.ReferenceError_) as cm:
                ref.classify_url(url)
            self.assertEqual(cm.exception.status, 400)


class OembedTests(unittest.TestCase):
    def fake(self, status=200, body=b"", truncated=False, exc=None):
        calls = []

        def fetch(url, **kw):
            calls.append((url, kw))
            if exc:
                raise exc
            return netguard.FetchResult(url, status, {}, body, truncated)
        return fetch, calls

    def test_only_the_fixed_endpoint_is_called_through_netguard_options(self):
        fetch, calls = self.fake(body=json.dumps({"title": "Song\x07 Title", "author_name": "Channel",
                                                  "thumbnail_url": "https://i.ytimg.com/x.jpg",
                                                  "html": "<iframe>"}).encode())
        meta = ref.fetch_oembed("youtube", "https://www.youtube.com/watch?v=dQw4w9WgXcQ", fetch)
        self.assertEqual(meta, {"platform": "youtube", "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                                "title": "Song Title", "author": "Channel", "provider": "Youtube"})
        url, kw = calls[0]
        self.assertEqual(url, "https://www.youtube.com/oembed?format=json&url="
                              "https%3A%2F%2Fwww.youtube.com%2Fwatch%3Fv%3DdQw4w9WgXcQ")
        self.assertFalse(kw["allow_http"])
        self.assertLessEqual(kw["max_bytes"], 256 * 1024)
        self.assertNotIn("thumbnail", json.dumps(meta))

    def test_failures(self):
        cases = [
            (dict(exc=netguard.BlockedURL("private address")), "blocked_url", 400),
            (dict(exc=OSError("down")), "metadata_unavailable", 503),
            (dict(status=404, body=b"Not Found"), "metadata_unavailable", 404),
            (dict(status=401, body=b""), "metadata_unavailable", 404),
            (dict(status=500, body=b""), "metadata_unavailable", 502),
            (dict(body=b"{}" * 10, truncated=True), "metadata_unavailable", 502),
            (dict(body=b"<html>"), "metadata_unavailable", 502),
            (dict(body=b"[1]"), "metadata_unavailable", 502),
        ]
        for kw, code, status in cases:
            with self.subTest(kw=str(kw)[:50]):
                fetch, _ = self.fake(**kw)
                with self.assertRaises(ref.ReferenceError_) as cm:
                    ref.fetch_oembed("spotify", "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC", fetch)
                self.assertEqual((cm.exception.code, cm.exception.status), (code, status))

    def test_real_netguard_refuses_a_rebinding_answer(self):
        # the oEmbed host resolves to loopback: netguard must refuse before connecting
        with mock.patch.object(socket, "getaddrinfo",
                               return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]):
            with self.assertRaises(ref.ReferenceError_) as cm:
                ref.fetch_oembed("youtube", "https://www.youtube.com/watch?v=dQw4w9WgXcQ", netguard.fetch)
        self.assertEqual(cm.exception.code, "blocked_url")


class SessionTests(StubBase):
    def setUp(self):
        super().setUp()
        self.chat = FakeChat(*[{**GOOD, "bpm": 99, "key": "E major", "lyrics": "[Verse]\nnot allowed",
                                "seed": 77}] * 4)
        self.ai = MusicAI(self.chat, sleep=lambda s: None)
        self.fetched: list[str] = []

        def fetch(url, **kw):
            self.fetched.append(url)
            return netguard.FetchResult(url, 200, {}, json.dumps({"title": "Live Set", "author_name": "DJ X"}).encode())
        self.analyzer = ref.ReferenceAnalyzer(self.jobs, self.ai, fetch=fetch, poll=0.01, start_threads=False,
                                              audit=lambda **kw: self.audits.append(kw))

    def uploaded(self, **extra):
        return self.jobs.upload(sine_wav(0.2), "ref.wav", "audio/wav", title="My reference", user="admin")

    def test_url_is_metadata_only_and_labelled(self):
        s = self.analyzer.start({"source": {"kind": "url", "url": "https://youtu.be/dQw4w9WgXcQ"},
                                 "hint": "darker"}, user="admin")
        self.assertEqual(s["state"], "starting")
        self.analyzer.run(s["id"])
        out = self.analyzer.get(s["id"], user="admin")
        self.assertEqual(out["state"], "done", out["error"])
        self.assertFalse(out["audio_analysed"])
        self.assertIsNone(out["measured"])
        self.assertIsNone(out["understanding"])
        self.assertIn("not downloaded or analysed", out["notice"])
        self.assertEqual(out["source"]["title"], "Live Set")
        self.assertEqual(set(out["field_sources"].values()), {"inferred"})
        self.assertEqual(out["suggestions"]["settings"]["lyrics"], "")
        self.assertIsNone(out["suggestions"]["settings"]["seed"])
        facts = self.chat.calls[0]["messages"][-1]["content"]
        self.assertIn("NO audio was analysed", facts)
        self.assertEqual(len(self.fetched), 1)
        self.assertTrue(self.fetched[0].startswith("https://www.youtube.com/oembed?"))
        self.assertEqual([c for c in self.stub.calls if "analyses" in c[1]], [])  # no audio job

    def test_uploaded_audio_is_measured_heard_and_suggested(self):
        asset = self.uploaded()
        s = self.analyzer.start({"source": {"kind": "asset", "asset_id": asset["id"]}, "understand": True},
                                user="admin")
        self.analyzer.run(s["id"])
        out = self.analyzer.get(s["id"], user="admin")
        self.assertEqual(out["state"], "done", out["error"])
        self.assertTrue(out["audio_analysed"])
        self.assertEqual(out["measured"]["tempo"]["bpm"], 122.0)
        self.assertTrue(out["understanding"]["vocals_detected"])
        self.assertNotIn("lyrics", out["understanding"])          # the reference's words are never returned
        self.assertNotIn("hold the light", json.dumps(out))
        settings, sources = out["suggestions"]["settings"], out["field_sources"]
        self.assertEqual((settings["bpm"], sources["bpm"]), (122, "measured"))       # measurement wins over AI
        self.assertEqual((settings["key"], sources["key"]), ("A minor", "measured"))
        self.assertEqual((settings["time_signature"], sources["time_signature"]), ("4/4", "measured"))
        # the stub track is 2 s: below the 10 s minimum, so the duration is not taken from it
        self.assertEqual((settings["duration"], sources["duration"]), (150, "inferred"))
        self.assertEqual((settings["instrumental"], sources["instrumental"]), (False, "model"))
        self.assertEqual(sources["style_prompt"], "inferred")
        self.assertEqual(settings["lyrics"], "")
        facts = self.chat.calls[0]["messages"][-1]["content"]
        self.assertIn("measured_by_signal_processing", facts)
        self.assertIn("heard_by_ace_step_model", facts)
        self.assertNotIn("hold the light", facts)
        # the node-2 analysis was requested for the upload, with understanding
        body = [c for c in self.stub.calls if c[1] == "/v1/music/analyses"]
        self.assertEqual(len(body), 1)

    def test_measure_only_without_suggestions(self):
        asset = self.uploaded()
        s = self.analyzer.start({"source": {"kind": "asset", "asset_id": asset["id"]}, "understand": False,
                                 "suggest": False}, user="admin")
        self.analyzer.run(s["id"])
        out = self.analyzer.get(s["id"], user="admin")
        self.assertEqual(out["state"], "done", out["error"])
        self.assertIsNone(out["understanding"])
        self.assertIsNone(out["suggestions"])
        self.assertEqual(self.chat.calls, [])

    def test_validation_and_ownership(self):
        image = self.library.add(NewAsset(type="image", ext="png", operation="generate",
                                          data=b"\x89PNG\r\n\x1a\n" + b"\0" * 32))
        bad = [None, [], {"source": {}}, {"source": {"kind": "file", "path": "/etc/passwd"}},
               {"source": {"kind": "asset", "asset_id": "../../x"}},
               {"source": {"kind": "asset", "asset_id": image["id"]}},
               {"source": {"kind": "url", "url": "https://10.0.0.1/watch?v=dQw4w9WgXcQ"}},
               {"source": {"kind": "url", "url": "https://youtu.be/dQw4w9WgXcQ"}, "understand": "yes"},
               {"source": {"kind": "url", "url": "https://youtu.be/dQw4w9WgXcQ"}, "hint": "x" * 301},
               {"source": {"kind": "url", "url": "https://youtu.be/dQw4w9WgXcQ"}, "api_key": "x"}]
        for body in bad:
            with self.subTest(body=str(body)[:60]), self.assertRaises(ref.ReferenceError_):
                self.analyzer.start(body, user="admin")
        s = self.analyzer.start({"source": {"kind": "url", "url": "https://youtu.be/dQw4w9WgXcQ"}}, user="admin")
        with self.assertRaises(ref.ReferenceError_) as cm:
            self.analyzer.get(s["id"], user="mallory")
        self.assertEqual(cm.exception.status, 404)
        with self.assertRaises(ref.ReferenceError_):
            self.analyzer.get(secrets.token_hex(12), user="admin")
        self.analyzer.start({"source": {"kind": "url", "url": "https://youtu.be/dQw4w9WgXcQ"}}, user="admin")
        with self.assertRaises(ref.ReferenceError_) as cm:
            self.analyzer.start({"source": {"kind": "url", "url": "https://youtu.be/dQw4w9WgXcQ"}}, user="admin")
        self.assertEqual(cm.exception.status, 429)

    def test_failures_end_the_session_honestly(self):
        s = self.analyzer.start({"source": {"kind": "url", "url": "https://youtu.be/dQw4w9WgXcQ"}}, user="admin")
        self.analyzer.fetch = functools.partial(lambda url, **kw: (_ for _ in ()).throw(OSError("down")))
        self.analyzer.run(s["id"])
        out = self.analyzer.get(s["id"], user="admin")
        self.assertEqual(out["state"], "failed")
        self.assertEqual(out["error"]["code"], "metadata_unavailable")
        asset = self.uploaded()
        with self.stub._lock:
            self.stub.uploads.clear()  # node 2 lost the upload and the Library file is re-sent
        self.library.update_settings(asset["id"], node2_upload_id="upl-" + "0" * 32)
        s = self.analyzer.start({"source": {"kind": "asset", "asset_id": asset["id"]}}, user="admin")
        self.analyzer.run(s["id"])
        self.assertEqual(self.analyzer.get(s["id"], user="admin")["state"], "done")

    def test_sessions_expire(self):
        s = self.analyzer.start({"source": {"kind": "url", "url": "https://youtu.be/dQw4w9WgXcQ"}}, user="admin")
        self.analyzer._sessions[s["id"]]["created_at"] -= ref.SESSION_TTL + 1
        self.analyzer._sessions[s["id"]]["state"] = "done"
        self.analyzer.start({"source": {"kind": "url", "url": "https://youtu.be/dQw4w9WgXcQ"}}, user="other")
        with self.assertRaises(ref.ReferenceError_):
            self.analyzer.get(s["id"], user="admin")


if __name__ == "__main__":
    unittest.main()
