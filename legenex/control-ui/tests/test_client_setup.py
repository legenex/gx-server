"""Hermetic client-connection setup tests: gx-max and gx-auto only, no
gx-mini/gx-code/ComfyUI leftovers."""

from __future__ import annotations

import re
import unittest

from support import TempEnv

from gx_control_ui import setup
from gx_control_ui.setup import connections_view, kilo_config, test_connection, test_live


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv()

    def tearDown(self):
        self.env.cleanup()

    def test_only_two_endpoints(self):
        cfgs = connections_view(self.env.cfg)["configs"]
        self.assertEqual({c["id"] for c in cfgs}, {"gx-max", "gx-auto"})

    def test_kilo_config_shape(self):
        cfg = kilo_config(self.env.cfg)
        self.assertIn(cfg["default_model"], ("gx-max", "gx-auto"))
        model_ids = [m["id"] for m in cfg["model_config"]]
        self.assertEqual(set(model_ids), {"gx-max", "gx-auto"})
        for m in cfg["model_config"]:
            self.assertEqual(m["context_length"], 262144)
            self.assertEqual(m["model_info"]["max_model_len"], 262144)
            self.assertEqual(m["model_info"]["owned_by"], "vLLM/LiteLLM")
            self.assertEqual(m["capabilities"]["function_calling"], True)
            self.assertEqual(m["capabilities"]["supports_reasoning"], True)
        text = str(cfg)
        for banned in ("gx-mini", "gx-code", "gx-fast", "gx-reason", "ComfyUI", "llama-swap"):
            self.assertNotIn(banned, text, banned)

    def test_gxauto_points_at_the_local_scheduler(self):
        cfg = kilo_config(self.env.cfg)
        auto = next(m for m in cfg["model_config"] if m["id"] == "gx-auto")
        self.assertIn("/api/key/", auto["litellm_params"]["api_base"])

    def test_connection_report(self):
        view = connections_view(self.env.cfg, fresh=True)
        for c in view["configs"]:
            self.assertIn(c["id"], ("gx-max", "gx-auto"))
            self.assertIn(c["state"], ("not_configured", "unreachable", "unhealthy", "healthy",
                                       "offline"))
            if c["id"] == "gx-max":
                self.assertIn(c["state"], ("not_configured", "unreachable", "offline"))

    def test_connection_tests_with_fake_addresses(self):
        self.assertEqual(test_connection(self.env.cfg, "gx-max",
                                        base="https://127.0.0.1:9/v1", key="sk-fallback")["code"], 1)
        out = test_live(self.env.cfg, "gx-auto", base="https://127.0.0.1:9/v1", key="sk-fallback")
        self.assertIn(out["code"], (1, 2))
        with self.assertRaises(ValueError):
            test_connection(self.env.cfg, "gx-mini")
        with self.assertRaises(ValueError):
            test_live(self.env.cfg, "ComfyUI")

    def test_hint_text_mentions_only_v41_models(self):
        view = connections_view(self.env.cfg)
        text = str(view["configs"]) + view["note"]
        for banned in ("gx-mini", "gx-code", "ComfyUI", "SGLang", "llama-swap"):
            self.assertNotIn(banned, text, banned)


if __name__ == "__main__":
    unittest.main()
