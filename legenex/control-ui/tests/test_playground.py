from __future__ import annotations

import base64
import unittest

from support import StubUpstream, TempEnv, env_vars, fake_key

from gx_control_ui.models import ResultLog
from gx_control_ui.playground import (
    Playground, PlaygroundError, build_chat, summarise_request, validate_image_data_url,
)

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64).decode()
JPEG = base64.b64encode(b"\xff\xd8\xff\xe0" + b"\x00" * 64).decode()


class FakeCluster:
    def __init__(self, cfg, state="down"):
        self.cfg = cfg
        self.state = state
        self.key = fake_key()

    def gxmax_state(self):
        return self.state

    def litellm_headers(self):
        return {"Authorization": f"Bearer {self.key}"}

    def media_headers(self):
        return {"Authorization": f"Bearer {self.key}"}


class TestValidation(unittest.TestCase):
    def test_model_whitelist(self):
        for bad in ("gpt-4", "gx-image", "", None, "gx-mini; drop"):
            with self.assertRaises(PlaygroundError):
                build_chat({"model": bad, "prompt": "hi"})

    def test_prompt_required_and_bounded(self):
        with self.assertRaises(PlaygroundError):
            build_chat({"model": "gx-mini", "prompt": "   "})
        with self.assertRaises(PlaygroundError):
            build_chat({"model": "gx-mini", "prompt": "x" * 16001})
        with self.assertRaises(PlaygroundError):
            build_chat({"model": "gx-mini", "prompt": "a\x00b"})
        with self.assertRaises(PlaygroundError):
            build_chat({"model": "gx-mini", "prompt": ["not", "a", "string"]})

    def test_numeric_bounds(self):
        for body in ({"temperature": 3}, {"temperature": -1}, {"temperature": True}, {"max_tokens": 0},
                     {"max_tokens": 99999}, {"max_tokens": "abc"}, {"temperature": {"x": 1}}):
            with self.assertRaises(PlaygroundError, msg=body):
                build_chat({"model": "gx-mini", "prompt": "hi", **body})

    def test_request_shape(self):
        req = build_chat({"model": "gx-fast", "prompt": "hi", "system": "be brief", "temperature": "0.2",
                          "max_tokens": 64, "stream": True, "tools": True})
        self.assertEqual(req["messages"][0], {"role": "system", "content": "be brief"})
        self.assertEqual(req["temperature"], 0.2)
        self.assertTrue(req["stream"])
        self.assertEqual(req["stream_options"], {"include_usage": True})
        self.assertEqual(req["tools"][0]["function"]["name"], "get_weather")
        self.assertNotIn("api_key", str(req).lower())

    def test_image_validation(self):
        self.assertTrue(validate_image_data_url(f"data:image/png;base64,{PNG}").startswith("data:image/png"))
        self.assertTrue(validate_image_data_url(f"data:image/jpeg;base64,{JPEG}"))
        for bad in (f"data:image/jpeg;base64,{PNG}", f"data:image/svg+xml;base64,{PNG}",
                    "http://example.com/x.png", "data:image/png;base64,@@@", ""):
            with self.assertRaises(PlaygroundError, msg=bad[:40]):
                validate_image_data_url(bad)
        big = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * (8 * 1024 * 1024 + 1)).decode()
        with self.assertRaises(PlaygroundError):
            validate_image_data_url(f"data:image/png;base64,{big}")

    def test_vision_only_on_vision_tiers(self):
        with self.assertRaises(PlaygroundError):
            build_chat({"model": "gx-max", "prompt": "what is this", "image": f"data:image/png;base64,{PNG}"})
        req = build_chat({"model": "gx-mini", "prompt": "what is this", "image": f"data:image/png;base64,{PNG}"})
        self.assertEqual(req["messages"][-1]["content"][1]["type"], "image_url")
        shown = summarise_request(req)
        self.assertLess(len(shown["messages"][-1]["content"][1]["image_url"]["url"]), 80)


class TestPlaygroundCalls(unittest.TestCase):
    def setUp(self):
        self.completion = {"id": "x", "model": "gx-mini",
                           "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
                           "choices": [{"message": {"role": "assistant", "content": "391"}}]}
        self.stub = StubUpstream({
            ("POST", "/v1/chat/completions"): (200, self.completion),
            ("POST", "/v1/images/generations"): (200, {"created": 1, "data": [{"b64_json": PNG}], "gx": {"seed": 5}}),
            ("POST", "/v1/videos"): (202, {"id": "abc-123", "status": "queued"}),
            ("GET", "/v1/videos/abc-123"): (200, {"id": "abc-123", "status": "completed"}),
            ("GET", "/v1/videos/abc-123/content"): (200, b"\x00\x00\x00\x18ftypmp42"),
        })
        self.env = TempEnv(litellm_base=self.stub.url, media_base=self.stub.url)
        self.results = ResultLog(self.env.cfg.state_dir / "r.json")
        self.cluster = FakeCluster(self.env.cfg)
        self.pg = Playground(self.env.cfg, self.cluster, self.results)

    def tearDown(self):
        self.stub.close()
        self.env.cleanup()

    def test_chat_uses_server_side_key_and_hides_it(self):
        res = self.pg.chat({"model": "gx-mini", "prompt": "17*23?"})
        self.assertTrue(res["ok"])
        self.assertEqual(res["model_used"], "gx-mini")
        self.assertEqual(res["usage"]["total_tokens"], 5)
        method, path, headers, body = self.stub.calls[-1]
        self.assertEqual(headers.get("Authorization"), f"Bearer {self.cluster.key}")
        self.assertNotIn(self.cluster.key, str(res))
        self.assertTrue(self.results.get("gx-mini")["inference"]["ok"])

    def test_empty_answer_is_not_ok(self):
        self.completion["choices"][0]["message"]["content"] = ""
        res = self.pg.chat({"model": "gx-mini", "prompt": "hi"})
        self.assertFalse(res["ok"])

    def test_gxmax_requires_takeover_confirmation(self):
        with self.assertRaises(PlaygroundError) as ctx:
            self.pg.chat({"model": "gx-max", "prompt": "hi"})
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(self.stub.calls, [])
        self.pg.chat({"model": "gx-max", "prompt": "hi", "confirm_takeover": True})
        self.cluster.state = "ready"
        self.pg.chat({"model": "gx-max", "prompt": "hi"})
        self.assertEqual(len(self.stub.calls), 2)

    def test_stream_guard_and_slot_release(self):
        with self.assertRaises(PlaygroundError):
            self.pg.chat_stream({"model": "gx-max", "prompt": "hi"})
        gen = self.pg.chat_stream({"model": "gx-mini", "prompt": "hi"})
        del gen  # never started: must not leak a slot
        for _ in range(Playground.MAX_CONCURRENT + 1):
            self.pg.chat({"model": "gx-mini", "prompt": "hi"})

    def test_image(self):
        res = self.pg.image({"prompt": "a fox", "size": "1024x1024", "seed": "7"})
        self.assertTrue(res["ok"])
        self.assertTrue(res["images"][0]["data_url"].startswith("data:image/png;base64,"))
        self.assertEqual(self.stub.calls[-1][3]["seed"], 7)
        for bad in ({"prompt": ""}, {"prompt": "x", "size": "9999x9999"}, {"prompt": "x", "quality": "ultra"}):
            with self.assertRaises(PlaygroundError):
                self.pg.image(bad)

    def test_media_refused_while_gxmax_owns_cluster(self):
        self.cluster.state = "ready"
        with self.assertRaises(PlaygroundError):
            self.pg.image({"prompt": "x"})
        with self.assertRaises(PlaygroundError):
            self.pg.video_submit({"prompt": "x"})

    def test_video_flow(self):
        sub = self.pg.video_submit({"prompt": "waves", "seconds": 2, "size": "640x640"})
        self.assertEqual(sub["job"]["id"], "abc-123")
        self.assertEqual(self.pg.video_status("abc-123")["job"]["status"], "completed")
        data, ctype = self.pg.video_content("abc-123")
        self.assertTrue(data.startswith(b"\x00\x00\x00\x18ftyp"))
        calls = len(self.stub.calls)
        self.pg.video_content("abc-123")  # served from the cache
        self.assertEqual(len(self.stub.calls), calls)
        for bad in ("../x", "a/b", "x" * 65, ""):
            with self.assertRaises(PlaygroundError):
                self.pg.video_status(bad)
        with self.assertRaises(PlaygroundError):
            self.pg.video_submit({"prompt": "x", "seconds": 60})
        with self.assertRaises(PlaygroundError):
            self.pg.video_submit({"prompt": "x", "size": "123x45"})

    def test_upstream_down(self):
        env = TempEnv(litellm_base="http://127.0.0.1:9")
        try:
            pg = Playground(env.cfg, FakeCluster(env.cfg), ResultLog(env.cfg.state_dir / "r.json"))
            with env_vars(), self.assertRaises(PlaygroundError) as ctx:
                pg.chat({"model": "gx-mini", "prompt": "hi"})
            self.assertEqual(ctx.exception.status, 502)
        finally:
            env.cleanup()


if __name__ == "__main__":
    unittest.main()
