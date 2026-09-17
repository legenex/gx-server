"""Hermetic tests for client setup (setup.py): the Kilo Code config, the
copy-paste examples, and the connection test against stub LiteLLM and
orchestrator servers on 127.0.0.1."""

from __future__ import annotations

import json
import re
import unittest
import urllib.parse

from support import StubUpstream, TempEnv, fake_key

from gx_control_ui import setup as client_setup
from gx_control_ui.setup import KILO_MODELS, _fingerprint, _kilo_shaped, examples, kilo_config
from gx_control_ui.setup import test_connection as check_connection

SECRET_SHAPE = re.compile(r"sk-[A-Za-z0-9]{20,}")
BASE = "http://gateway.example:4000/v1"
EXAMPLE_KEYS = {"curl_models", "curl_chat", "python", "javascript", "gx_max", "music"}


class KiloConfigTests(unittest.TestCase):
    def test_valid_json_with_the_expected_fields(self):
        doc = json.loads(kilo_config(BASE))
        self.assertEqual(doc["$schema"], "https://app.kilo.ai/config.json")
        self.assertEqual(doc["model"], "gx-cluster/gx-auto")
        provider = doc["provider"]["gx-cluster"]
        self.assertEqual(provider["npm"], "@ai-sdk/openai-compatible")
        self.assertEqual(provider["options"]["baseURL"], BASE)
        self.assertEqual(provider["options"]["apiKey"], "{env:GX_API_KEY}")
        self.assertEqual(provider["options"]["timeout"], 900000)
        models = provider["models"]
        self.assertEqual(set(models), set(KILO_MODELS))
        self.assertNotIn("gx-image", models)
        for alias, m in models.items():
            self.assertEqual(m["name"], alias)
            self.assertTrue(m["tool_call"])
            self.assertEqual(m["modalities"]["output"], ["text"])
            self.assertGreater(m["limit"]["context"], m["limit"]["output"])
        self.assertFalse(models["gx-max"]["attachment"])
        self.assertEqual(models["gx-max"]["modalities"]["input"], ["text"])
        self.assertTrue(models["gx-reason"]["reasoning"])
        self.assertFalse(models["gx-auto"]["reasoning"])
        self.assertIn("image", models["gx-auto"]["modalities"]["input"])

    def test_custom_placeholder_and_no_secret(self):
        text = kilo_config(BASE, key_placeholder="YOUR_GX_API_KEY")
        self.assertEqual(json.loads(text)["provider"]["gx-cluster"]["options"]["apiKey"], "YOUR_GX_API_KEY")
        self.assertNotRegex(text, SECRET_SHAPE)

    def test_base_url_is_json_escaped(self):
        doc = json.loads(kilo_config('http://x/"v1\\'))
        self.assertEqual(doc["provider"]["gx-cluster"]["options"]["baseURL"], 'http://x/"v1\\')


class ExamplesTests(unittest.TestCase):
    def test_examples_use_placeholders_only(self):
        ex = examples(BASE, "http://playground.example:8090")
        self.assertLessEqual(EXAMPLE_KEYS, set(ex))
        for name, text in ex.items():
            self.assertNotRegex(text, SECRET_SHAPE, name)
            self.assertNotIn("LITELLM_MASTER_KEY", text, name)
            # every example uses the placeholder or the environment variable, never a key
            self.assertIn("GX_API_KEY", text, name)
            self.assertRegex(text, r"YOUR_GX_API_KEY|\$GX_API_KEY|process\.env\.GX_API_KEY", name)
        placeholders = [n for n, t in ex.items() if "YOUR_GX_API_KEY" in t]
        self.assertTrue(placeholders)
        self.assertIn("process.env.GX_API_KEY", ex["javascript"])
        self.assertIn(f"{BASE}/models", ex["curl_models"])
        self.assertIn("http://playground.example:8090/v1/music/generations", ex["music"])
        self.assertIn('"model": "gx-max"', ex["gx_max"])
        # the embedded JSON bodies are valid
        body = re.search(r"-d '(\{.*\})'", ex["curl_chat"]).group(1)
        self.assertEqual(json.loads(body)["model"], "gx-mini")
        body = re.search(r"-d '(\{.*\})'", ex["music"]).group(1)
        self.assertTrue(json.loads(body)["instrumental"])

    def test_setup_info_offline(self):
        env = TempEnv(public_gateway_url=BASE)
        try:
            info = client_setup.setup_info(env.cfg)
        finally:
            env.cleanup()
        self.assertEqual(info["gateway_url"], BASE)
        self.assertEqual(info["local_gateway_url"], "http://127.0.0.1:9/v1")
        self.assertEqual(info["music_api_url"], "http://127.0.0.1:8090/v1/music")
        self.assertEqual(info["kilo"]["verified_version"], client_setup.KILO_VERIFIED)
        self.assertEqual(json.loads(info["kilo"]["config_example"])["provider"]["gx-cluster"]["options"]["baseURL"],
                         BASE)
        self.assertIn(f"Base URL: {BASE}", info["kilo"]["manual_steps"])
        self.assertNotIn("gx-max", [r["to"] for r in info["gx_auto"]["routes"]])
        self.assertNotRegex(json.dumps(info), SECRET_SHAPE)
        self.assertLessEqual(EXAMPLE_KEYS, set(info["generic"]["examples"]))


class ConnectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.good = fake_key()
        cls.narrow = fake_key()
        cls.state = {"answer": "pong", "decision": True}

        def models(handler, body):
            auth = handler.headers.get("Authorization")
            if auth == f"Bearer {cls.good}":
                return 200, {"data": [{"id": a} for a in ("gx-auto", "gx-mini", "gx-fast")]}
            if auth == f"Bearer {cls.narrow}":
                return 200, {"data": [{"id": "gx-fast"}]}
            return 401, {"error": {"message": "Authentication Error, invalid key", "type": "auth_error"}}

        def chat(handler, body):
            if not cls.state["answer"]:
                return 500, {"error": {"message": "upstream exploded"}}
            return 200, {"choices": [{"message": {"role": "assistant", "content": " " + cls.state["answer"] + " "}}]}

        def decisions(handler, body):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(handler.path).query)
            fp = query.get("fingerprint", [""])[0]
            data = []
            if cls.state["decision"] and fp == _fingerprint(_kilo_shaped("Reply with the single word: pong")):
                data = [{"event": "request", "tier": None},
                        {"event": "decision", "tier": "gx-mini", "intent": "trivial", "signals": ["short"]}]
            return 200, {"data": data}

        cls.litellm = StubUpstream({("GET", "/v1/models"): models, ("POST", "/v1/chat/completions"): chat})
        cls.orch = StubUpstream({("GET", "/routing/decisions"): decisions})

    @classmethod
    def tearDownClass(cls):
        cls.litellm.close()
        cls.orch.close()

    def setUp(self):
        self.env = TempEnv(litellm_base=self.litellm.url + "/", orchestrator_base=self.orch.url)
        self.state.update(answer="pong", decision=True)
        self.litellm.calls.clear()
        self.orch.calls.clear()

    def tearDown(self):
        self.env.cleanup()

    def test_kilo_connected_with_routing_decision(self):
        out = check_connection(self.env.cfg, "kilo", self.good)
        self.assertTrue(out["connected"], out)
        self.assertEqual(out["summary"], "CONNECTED")
        self.assertEqual(out["routing"], {"tier": "gx-mini", "intent": "trivial", "signals": ["short"]})
        self.assertEqual([c["check"] for c in out["checks"]],
                         ["GET /v1/models", "gx-auto visible to this key", "real completion on gx-auto",
                          "gx-auto routing decision (orchestrator journal)"])
        method, path, headers, body = self.litellm.calls[-1]
        self.assertEqual((method, path), ("POST", "/v1/chat/completions"))
        self.assertEqual(headers["Authorization"], f"Bearer {self.good}")
        self.assertEqual(body["model"], "gx-auto")
        self.assertEqual(body["messages"], _kilo_shaped("Reply with the single word: pong"))
        self.assertEqual((body["max_tokens"], body["temperature"]), (16, 0))
        self.assertIn("fingerprint=", self.orch.calls[0][1])
        # the pasted key is never echoed back
        self.assertNotIn(self.good, json.dumps(out))

    def test_kilo_without_a_decision_is_not_connected(self):
        self.state["decision"] = False
        out = check_connection(self.env.cfg, "kilo", self.good)
        self.assertFalse(out["connected"])
        self.assertIn("no decision found", out["summary"])
        self.assertIsNone(out["routing"]["tier"])

    def test_generic_and_openwebui(self):
        for client in ("generic", "openwebui"):
            out = check_connection(self.env.cfg, client, self.good)
            self.assertTrue(out["connected"], out)
            self.assertNotIn("routing", out)
            self.assertEqual(self.litellm.calls[-1][3]["model"], "gx-mini")
            self.assertEqual(self.litellm.calls[-1][3]["messages"],
                             [{"role": "user", "content": "Reply with the single word: pong"}])
        self.assertEqual(self.orch.calls, [])

    def test_key_without_the_aliases(self):
        out = check_connection(self.env.cfg, "generic", self.narrow)
        self.assertFalse(out["connected"])
        self.assertEqual(out["checks"][1]["detail"], "the key does not allow these aliases")

    def test_rejected_key(self):
        out = check_connection(self.env.cfg, "kilo", fake_key())
        self.assertFalse(out["connected"])
        self.assertEqual(out["summary"], "The gateway refused the key.")
        self.assertEqual(out["checks"][0]["status"], 401)
        self.assertIn("Authentication Error", out["checks"][0]["detail"])
        self.assertEqual(len(self.litellm.calls), 1)  # no completion attempted

    def test_failed_completion(self):
        self.state["answer"] = ""
        out = check_connection(self.env.cfg, "generic", self.good)
        self.assertFalse(out["connected"])
        self.assertIn("upstream exploded", out["summary"])

    def test_unreachable_gateway(self):
        env = TempEnv(litellm_base="http://127.0.0.1:9")
        try:
            out = check_connection(env.cfg, "generic", self.good)
        finally:
            env.cleanup()
        self.assertFalse(out["connected"])
        self.assertEqual(out["checks"][-1]["check"], "gateway reachable")

    def test_invalid_input(self):
        for secret in ("abc", "", None, 42, "sk-short", "sk-" + "a" * 7, "Bearer " + fake_key(),
                       fake_key("pk-"), "sk-" + "a" * 20 + " x"):
            with self.assertRaises(ValueError, msg=repr(secret)):
                check_connection(self.env.cfg, "kilo", secret)
        for client in ("cursor", "", None):
            with self.assertRaises(ValueError):
                check_connection(self.env.cfg, client, self.good)
        self.assertEqual(self.litellm.calls, [])


if __name__ == "__main__":
    unittest.main()
