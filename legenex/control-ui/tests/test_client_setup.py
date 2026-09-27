"""Hermetic client-connection setup tests: gx-max and gx-auto only, no
gx-mini/gx-code/ComfyUI leftovers."""

from __future__ import annotations

import json
import unittest

from support import TempEnv, fake_key

from gx_control_ui import setup


class SetupBase(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv()

    def tearDown(self):
        self.env.cleanup()


class TestConnectionsInfo(SetupBase):
    def test_only_two_aliases(self):
        view = setup.connections_info(self.env.cfg)
        self.assertEqual(view["gateway"]["models"], ["gx-max", "gx-auto"])
        self.assertEqual(view["text_aliases"], ["gx-max", "gx-auto"])
        self.assertEqual(set(view["aliases"]), {"gx-max", "gx-auto"})

    def test_offline_gateway_is_honest_and_masks_nothing(self):
        view = setup.connections_info(self.env.cfg)
        self.assertEqual(view["gateway"]["status"], "unknown")  # offline: no fabricated health
        self.assertEqual(view["api_key"]["masked"], "sk-•••• (not loaded)")
        self.assertIsNone(view["api_key"]["key"])
        self.assertFalse(view["api_key"]["revealed"])

    def test_kilo_recommended_model_is_gx_auto(self):
        view = setup.connections_info(self.env.cfg)
        self.assertEqual(view["kilo"]["recommended_model"], "gx-auto")
        self.assertEqual(view["kilo"]["alternatives"], ["gx-max"])
        self.assertEqual(view["kilo"]["key_env"], "GX_API_KEY")

    def test_openwebui_points_at_the_loopback_gateway(self):
        view = setup.connections_info(self.env.cfg)
        self.assertEqual(view["openwebui"]["base_url"], "http://127.0.0.1:4000/v1")
        self.assertFalse(view["openwebui"]["enable_ollama"])
        self.assertEqual(view["openwebui"]["model_ids"], ["gx-max", "gx-auto"])

    def test_no_retired_alias_anywhere(self):
        text = json.dumps(setup.connections_info(self.env.cfg), default=str)
        for banned in ("gx-mini", "gx-code", "gx-fast", "gx-reason", "gx-image", "gx-video",
                       "ComfyUI", "SGLang", "llama-swap"):
            self.assertNotIn(banned, text, banned)


class TestKiloConfig(SetupBase):
    def test_config_document_shape(self):
        cfg = json.loads(setup.kilo_config(self.env.cfg, "http://127.0.0.1:4000/v1"))
        self.assertEqual(cfg["model"], "gx-cluster/gx-auto")
        provider = cfg["provider"]["gx-cluster"]
        self.assertEqual(provider["options"]["baseURL"], "http://127.0.0.1:4000/v1")
        self.assertEqual(provider["options"]["apiKey"], "{env:GX_API_KEY}")
        self.assertEqual(set(provider["models"]), {"gx-max", "gx-auto"})
        stock = provider["models"]["gx-max"]
        self.assertTrue(stock["reasoning"])
        self.assertTrue(stock["tool_call"])
        # the production pack's context comes from the registry fixture
        self.assertEqual(stock["limit"]["context"], 262144)

    def test_registry_facts_drive_capabilities(self):
        models = setup.kilo_models(self.env.cfg)
        for alias, m in models.items():
            self.assertEqual(m["name"], alias)
            self.assertTrue(m["reasoning"])
            self.assertEqual(m["modalities"]["output"], ["text"])
        # the fixture packs are multimodal (vision: true)
        self.assertIn("image", models["gx-max"]["modalities"]["input"])


class TestConnectionTests(SetupBase):
    def test_client_must_be_known(self):
        with self.assertRaises(ValueError):
            setup.test_connection(self.env.cfg, "gx-mini", fake_key())
        with self.assertRaises(ValueError):
            setup.test_connection(self.env.cfg, "kilo", "not-a-key")

    def test_live_target_must_be_known(self):
        with self.assertRaises(ValueError):
            setup.test_live(self.env.cfg, "ComfyUI")
        # offline: honest failure, no gateway probe
        out = setup.test_live(self.env.cfg, "gx-auto")
        self.assertFalse(out["ok"])
        self.assertEqual(out["summary"], "offline")

    def test_mask_key(self):
        key = fake_key("sk-")
        masked = setup.mask_key(key)
        self.assertNotIn(key, masked)
        self.assertTrue(masked.startswith("sk-"))
        self.assertTrue(masked.endswith(key[-4:]))


if __name__ == "__main__":
    unittest.main()
