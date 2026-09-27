"""Hermetic AgentOS adapter tests: disconnected honesty, snapshot relay, and
the no-fabricated-controls rule (AGENTOS-MAP.md)."""

from __future__ import annotations

import unittest

from support import StubUpstream, TempEnv

from gx_control_ui import agents_view
from gx_control_ui.agentos_adapter import AgentOSAdapter, SUPPORTED_CONTROLS


class Disconnected(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv()          # agentos_base points at 127.0.0.1:9 (closed)
        self.adapter = AgentOSAdapter(self.env.cfg)

    def tearDown(self):
        self.env.cleanup()

    def test_honest_not_connected(self):
        out = self.adapter.snapshot()
        self.assertEqual(out["connected"], False)
        self.assertTrue(out["reason"])          # a reason, never a shrug
        self.assertEqual(out["supported_controls"], [])
        for key in ("hermes", "kanban", "projects", "overview", "buzz"):
            self.assertIsNone(out[key])

    def test_probe_health_offline(self):
        out = self.adapter.probe_health()
        self.assertEqual(out["connected"], False)
        self.assertEqual(out["reason"], "offline mode")


class AgainstStub(unittest.TestCase):
    def setUp(self):
        self.stub = StubUpstream({
            ("GET", "/api/health"): (200, {"ok": True}),
            ("GET", "/api/hermes"): (200, {
                "profiles": [{"profile": "architect", "model": "gx-auto", "gateway": "gw-1"},
                             {"profile": "coder", "model": "gx-auto", "gateway": "gw-2"}],
                "gateways": [{"profile": "architect", "running": True},
                             {"profile": "coder", "running": False}]}),
            ("GET", "/api/kanban"): (200, {"counts": {"ready": 2, "todo": 3},
                                           "cards": [{"id": "c1", "title": "ship", "status": "todo"}]}),
            ("GET", "/api/projects"): (200, {"projects": [{"slug": "gx-cluster"}]}),
            ("GET", "/api/overview"): (200, {"system": {"ok": True}}),
            ("GET", "/api/buzz"): (500, {"error": "buzz down"}),
        })
        self.env = TempEnv(agentos_base=self.stub.url, offline=False)
        self.adapter = AgentOSAdapter(self.env.cfg)

    def tearDown(self):
        self.stub.close()
        self.env.cleanup()

    def test_snapshot_relays_read_only_endpoints(self):
        out = self.adapter.snapshot()
        self.assertTrue(out["connected"])
        self.assertEqual(out["hermes"]["profiles"][0]["profile"], "architect")
        self.assertEqual(out["kanban"]["cards"][0]["title"], "ship")
        self.assertEqual(out["projects"]["projects"][0]["slug"], "gx-cluster")
        # a failing sub-endpoint is honest, not fatal
        self.assertEqual(out["buzz"], {"unavailable": True, "status": 500})
        paths = [c[1] for c in self.stub.calls]
        self.assertEqual(paths[0], "/api/health")
        for p in ("/api/hermes", "/api/kanban", "/api/projects", "/api/overview", "/api/buzz"):
            self.assertIn(p, paths)

    def test_no_control_endpoints_are_ever_called(self):
        self.adapter.snapshot()
        paths = [c[1] for c in self.stub.calls]
        for banned in ("/api/actions/pause", "/api/agents/stop", "/api/cancel"):
            self.assertNotIn(banned, paths)
        self.assertEqual(SUPPORTED_CONTROLS, ())


class AgentsViewShape(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv()
        self.adapter = AgentOSAdapter(self.env.cfg)
        self.sched = {"available": True, "queue": [
            {"agent": "coder", "project": "gx-cluster", "state": "active"},
            {"agent": "coder", "project": "gx-cluster", "state": "queued"},
            {"agent": "reviewer", "project": "gx-cluster", "state": "error"}]}

    def tearDown(self):
        self.env.cleanup()

    def test_agents_rows_merge_sources_without_fabrication(self):
        out = agents_view.agents_view(self.env.cfg, self.adapter, self.sched)
        # agentos is disconnected here: rows come from the scheduler only
        self.assertEqual(out["agentos"]["connected"], False)
        by_name = {a["name"]: a for a in out["agents"]}
        self.assertEqual(by_name["coder"]["requests"], {"active": 1, "queued": 1, "done": 0, "error": 0})
        self.assertEqual(by_name["reviewer"]["requests"]["error"], 1)
        self.assertEqual(out["supported_controls"], [])
        for a in out["agents"]:
            # coarse, derived states only — never fine-grained or invented ones
            self.assertIn(a["state"], ("working", "idle"))

    def test_tasks_view_shape(self):
        out = agents_view.tasks_view(self.env.cfg, self.adapter, self.sched)
        self.assertEqual(out["kanban_cards"], [])
        self.assertFalse(out["agentos_connected"])
        self.assertEqual(len(out["scheduler_records"]), 3)
        self.assertIn("dependency graph", out["note"])


if __name__ == "__main__":
    unittest.main()
