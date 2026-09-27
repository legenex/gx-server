"""Hermetic models/views tests: registry v2 cards, live state, honesty when the
orchestrator is offline."""

from __future__ import annotations

import json
import unittest

from support import TempEnv

from gx_control_ui import models
from gx_control_ui import views
from gx_control_ui.services import Cluster


class ModelsBase(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv()
        self.reg = models.read_registry(self.env.cfg.registry_path)
        self.cluster = Cluster(self.env.cfg)

    def tearDown(self):
        self.env.cleanup()


class TestRegistryReading(ModelsBase):
    def test_registry_v2_is_understood(self):
        self.assertEqual(self.reg["schema"], 2)
        self.assertTrue(models.registry_ok(self.reg))

    def test_cards_carry_production_and_uncensored(self):
        cards = models.cards(self.reg)
        self.assertEqual(set(cards), {"dsv41-flash-exl3-stock", "dsv41-flash-exl3-uncensored"})
        prod = cards["dsv41-flash-exl3-uncensored"]
        self.assertTrue(prod["production"] and prod["active"])
        self.assertTrue(prod["uncensored"])
        self.assertEqual(prod["source"], "dealignai/DeepSeek-V4.1-Flash-UNCENSORED-EXL3-2.9bpw")
        self.assertFalse(cards["dsv41-flash-exl3-stock"]["production"])

    def test_profiles_and_reasoning_from_the_registry(self):
        prof = models.profiles(self.reg)
        self.assertIn("fast", prof)
        self.assertEqual(prof["fast"]["max_num_seqs"], 1)
        self.assertEqual(prof["fast"]["reasoning_default"], "medium")
        levels = models.reasoning(self.reg)["levels"]
        self.assertEqual(levels[0], "none")
        self.assertEqual(levels[-1], "max")

    def test_fabric_rails_from_the_registry(self):
        rails = models.fabric_rails(self.reg)
        self.assertEqual(len(rails), 2)
        self.assertEqual(rails[0]["head"]["ip"], "192.168.100.10")
        self.assertEqual(rails[1]["worker"]["ip"], "192.168.101.11")
        self.assertEqual(rails[0]["head"]["hca"], "rocep1s0f0")

    def test_a_non_v2_registry_degrades_honestly(self):
        env = TempEnv()
        self.addCleanup(env.cleanup)
        (env.root / "registry.json").write_text(json.dumps({"schema": 1, "aliases": {"gx-mini": {}}}))
        reg = models.read_registry(env.cfg.registry_path)
        self.assertEqual(models.cards(reg), {})
        view = models.registry_view(self.cluster, env.cfg)
        self.assertFalse(view["registry_ok"])
        self.assertIn("not at schema 2", view["note"])
        self.assertNotIn("gx-mini", json.dumps(view))


class TestLiveState(ModelsBase):
    def test_live_state_offline_is_honest(self):
        out = models.live_state(self.cluster, self.reg)
        ids = [m.get("id") or m.get("alias") for m in out]
        self.assertIn("gx-max", ids)
        self.assertIn("gx-auto", ids)
        packs = [m for m in out if m.get("kind") == "model"]
        self.assertEqual(len(packs), 2)
        for m in packs:
            self.assertEqual(m["state"], "available")   # gx-max is down offline
            self.assertIn("gx-max is", m["state_detail"])
        auto = next(m for m in out if m.get("alias") == "gx-auto")
        self.assertEqual(auto["state"], "unavailable")   # scheduler offline
        gx = next(m for m in out if m.get("alias") == "gx-max")
        self.assertIn(gx["state"], ("unavailable", "error"))

    def test_no_retired_alias_ever_appears(self):
        text = json.dumps(models.live_state(self.cluster, self.reg))
        for banned in ("gx-mini", "gx-code", "gx-fast", "gx-reason", "llama-swap", "SGLang",
                       "ComfyUI", "gx-music"):
            self.assertNotIn(banned, text, banned)


class TestViewsShape(ModelsBase):
    def test_overview_mentions_the_two_rails_and_one_model(self):
        from gx_control_ui.server import App
        app = App(self.env.cfg)
        out = views.overview(app)
        self.assertEqual(len(out["rails"]), 2)
        self.assertIn("gxmax", out)
        self.assertIn("queue", out)
        self.assertTrue(out["registry_ok"])
        self.assertEqual([n["name"] for n in out["nodes"]], ["gx10-01", "gx10-02"])
        # the V4.1 explanation names the single model world
        self.assertIn("DeepSeek", views.cluster(app)["explanation"])

    def test_nodes_and_jobs_shape(self):
        from gx_control_ui.server import App
        app = App(self.env.cfg)
        nv = views.nodes(app)
        self.assertIn("node1", nv)
        self.assertIn("node2", nv)
        self.assertIn("units", nv["node1"])     # per-node unit lists
        self.assertEqual(nv["gxmax"]["state"], "unknown")  # offline: honest
        jv = views.jobs(app)
        self.assertIn("ui_jobs", jv)
        self.assertFalse(jv["queue"]["available"])


if __name__ == "__main__":
    unittest.main()
