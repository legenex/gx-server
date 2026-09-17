from __future__ import annotations

import unittest

from support import TempEnv

from gx_control_ui import views
from gx_control_ui.models import CATALOG, ResultLog, live_state
from gx_control_ui.services import ALL_ALIASES, Cluster

GIB = 2**30


def facts(avail=100, kernel_ok=True, psi_full=0.0, hostwatch="ok=5 warn=0 crit=0", containers=(), reachable=True):
    return {
        "reachable": reachable, "kernel": "6.17.0-1032-nvidia" if kernel_ok else "7.0.0-1-nvidia",
        "kernel_ok": kernel_ok,
        "memory": {"MemTotal": 121 * GIB, "MemAvailable": avail * GIB, "SwapTotal": 64 * GIB, "SwapFree": 60 * GIB},
        "psi": {"memory": {"some": {"avg10": 0.0}, "full": {"avg10": psi_full}}},
        "load": {"load1": 0.1, "nproc": 20},
        "hostwatch": {"ok": True, "detail": hostwatch, "status": "ok", "age_seconds": 30},
        "tailscale": {"ok": True},
        "units": [],
        "docker": {"containers": [{"name": c, "state": "running", "status": "Up"} for c in containers]},
        "git": {"head": "abc"},
        "rdma": [],
    }


class TestNodeSummary(unittest.TestCase):
    def test_healthy(self):
        s = views.node_summary("node1", facts(), "down")
        self.assertEqual(s["level"], "ok")
        self.assertEqual(s["mem_available_gib"], 100)

    def test_kernel_mismatch_is_critical(self):
        s = views.node_summary("node1", facts(kernel_ok=False), "down")
        self.assertEqual(s["level"], "crit")

    def test_memory_levels_are_phase_aware(self):
        self.assertEqual(views.node_summary("node1", facts(avail=20), "down")["level"], "warn")
        # gx-max serving: ~15-18 GiB free is the expected steady state
        self.assertEqual(views.node_summary("node1", facts(avail=16), "ready")["level"], "ok")
        # load transient: ~3 GiB is expected while acquiring
        self.assertEqual(views.node_summary("node1", facts(avail=3), "acquiring")["level"], "ok")
        self.assertEqual(views.node_summary("node1", facts(avail=2), "down")["level"], "crit")

    def test_pressure_and_hostwatch(self):
        self.assertEqual(views.node_summary("node2", facts(psi_full=10), "down")["level"], "warn")
        self.assertEqual(views.node_summary("node2", facts(psi_full=50), "down")["level"], "crit")
        self.assertEqual(views.node_summary("node2", facts(hostwatch="ok=4 warn=1 crit=0"), "down")["level"], "warn")
        self.assertEqual(views.node_summary("node2", facts(hostwatch="ok=4 warn=0 crit=1"), "down")["level"], "crit")

    def test_unreachable(self):
        s = views.node_summary("node2", {"reachable": False, "error": "ssh timeout",
                                         "fabric_probe": {"192.168.100.11": "refused"}}, "down")
        self.assertEqual(s["level"], "crit")
        self.assertFalse(s["reachable"])
        self.assertEqual(s["fabric_probe"]["192.168.100.11"], "refused")

    def test_workloads_listed(self):
        s = views.node_summary("node1", facts(containers=("gx-mini", "open-webui")), "down")
        self.assertEqual([w["name"] for w in s["workloads"]], ["gx-mini"])

    def test_worst(self):
        self.assertEqual(views.worst("ok", "warn", "ok"), "warn")
        self.assertEqual(views.worst("ok", "crit", "warn"), "crit")
        self.assertEqual(views.worst(), "ok")


class TestServiceLevels(unittest.TestCase):
    def test_drained_services_are_expected_while_gxmax_runs(self):
        svc = {"litellm_live": {"ok": True}, "litellm_ready": {"ok": True}, "orchestrator": {"ok": True},
               "swap_node1": {"ok": False}, "swap_node2": {"ok": False}, "media": {"ok": False},
               "sglang": {"ok": True}}
        rows = {r["name"]: r for r in views._service_levels(svc, "ready")}
        self.assertEqual(rows["llama-swap gx10-01"]["level"], "ok")
        self.assertIn("on purpose", rows["llama-swap gx10-01"]["note"])
        rows = {r["name"]: r for r in views._service_levels(svc, "down")}
        self.assertEqual(rows["llama-swap gx10-01"]["level"], "crit")
        self.assertEqual(rows["media router (gx10-02)"]["level"], "warn")

    def test_sglang_down_while_ready_is_critical(self):
        svc = {"sglang": {"ok": False}}
        rows = {r["name"]: r for r in views._service_levels(svc, "ready")}
        self.assertEqual(rows["SGLang gx-max :30000"]["level"], "crit")


class TestLiveState(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv()
        self.cluster = Cluster(self.env.cfg)
        self.results = ResultLog(self.env.cfg.state_dir / "r.json")
        self.svc = {
            "orchestrator": {"ok": True, "body": {"tiers": {
                "gx-mini": {"state": "ready", "usable": True}, "gx-fast": {"state": "stopped", "usable": True},
                "gx-reason": {"state": "stopped", "usable": True}, "gx-max": {"state": "stopped", "usable": True}}}},
            "swap_node1": {"ok": True, "body": {"data": [{"id": "gx-mini", "status": {"value": "loaded"}},
                                                         {"id": "gx-fast", "status": {"value": "unloaded"}}]}},
            "swap_node2": {"ok": True, "body": {"data": [{"id": "gx-reason", "status": {"value": "loading"}}]}},
            "media": {"ok": True, "body": {"busy": False, "comfyui": {"reachable": True}}},
            "sglang": {"ok": False},
        }
        self.state = "down"
        self.cluster.services._fn = lambda: self.svc
        self.cluster.lifecycle._fn = lambda: {"status": {"ok": True, "body": {"state": self.state}}}
        self.cluster.node1._fn = lambda: facts()
        self.cluster.node2._fn = lambda: facts()

    def tearDown(self):
        self.env.cleanup()

    def states(self):
        for c in (self.cluster.services, self.cluster.lifecycle):
            c.invalidate()
        return {m["alias"]: m["state"] for m in live_state(self.cluster, self.results)}

    def test_normal(self):
        self.assertEqual(self.states(), {
            "gx-mini": "loaded", "gx-fast": "unloaded", "gx-reason": "loading", "gx-max": "unloaded",
            "gx-auto": "loaded", "gx-image": "ready", "gx-video": "ready"})

    def test_gxmax_ready_drains_others(self):
        self.state = "ready"
        self.svc["sglang"] = {"ok": True}
        s = self.states()
        self.assertEqual(s["gx-max"], "loaded")
        for alias in ("gx-mini", "gx-fast", "gx-reason", "gx-image", "gx-video"):
            self.assertEqual(s[alias], "unavailable", alias)

    def test_gxmax_ready_without_health_is_error(self):
        self.state = "ready"
        self.assertEqual(self.states()["gx-max"], "error")

    def test_unreachable_upstreams(self):
        self.svc["swap_node2"] = {"ok": False}
        self.svc["media"] = {"ok": False}
        self.svc["orchestrator"] = {"ok": False}
        s = self.states()
        self.assertEqual(s["gx-reason"], "unavailable")
        self.assertEqual(s["gx-image"], "unavailable")
        self.assertEqual(s["gx-auto"], "unavailable")

    def test_media_busy_and_comfy_down(self):
        self.svc["media"] = {"ok": True, "body": {"busy": True, "held_for_seconds": 3, "comfyui": {"reachable": True}}}
        self.assertEqual(self.states()["gx-video"], "loading")
        self.svc["media"] = {"ok": True, "body": {"busy": False, "comfyui": {"reachable": False}}}
        self.assertEqual(self.states()["gx-image"], "error")

    def test_catalog_is_complete_and_locked(self):
        self.assertEqual(tuple(CATALOG), ALL_ALIASES)
        gx = CATALOG["gx-max"]
        self.assertEqual(gx["model"], "dealignai/DeepSeek-V4-Flash-0731-CRACK-NVFP4")  # D-032
        self.assertIn("SGLang", gx["engine"])
        self.assertEqual(gx["topology"]["rank0"], "gx10-01")
        self.assertEqual(gx["topology"]["rank1"], "gx10-02")
        self.assertFalse(CATALOG["gx-max"]["vision"])
        self.assertTrue(CATALOG["gx-mini"]["vision"])

    def test_result_log_persists(self):
        self.results.record("gx-mini", "inference", True, "391", latency_ms=12)
        again = ResultLog(self.env.cfg.state_dir / "r.json")
        self.assertEqual(again.get("gx-mini")["inference"]["detail"], "391")


class TestGitView(unittest.TestCase):
    def test_match_and_notes(self):
        env = TempEnv()
        try:
            cl = Cluster(env.cfg)
            cl.remote_git._fn = lambda: {"ok": True, "head": "aaa"}

            class A:
                cluster = cl
            n1 = {"git": {"head": "aaa"}, "units": []}
            n2 = {"git": {"head": "aaa", "push_url": "DISABLED-gx10-02-is-pull-only"}, "units": []}
            g = views.git_view(A, n1, n2)
            self.assertTrue(g["match"])
            self.assertTrue(g["node2_push_disabled"])
            n2["git"]["head"] = "bbb"
            g = views.git_view(A, n1, n2)
            self.assertFalse(g["match"])
            self.assertIn("gx10-02", g["note"])
            self.assertEqual(g["level"], "warn")
        finally:
            env.cleanup()


if __name__ == "__main__":
    unittest.main()


class TestRegistryMatchesBindings(__import__("unittest").TestCase):
    """The model cards come from legenex/models/registry.json; it must describe
    exactly what the live binding files serve."""

    def test_registry_paths_match_the_binding_files(self):
        import json
        from gx_control_ui.model_manager import read_macros
        from support import REPO
        reg = json.loads((REPO / "legenex/models/registry.json").read_text())["aliases"]
        n1 = read_macros((REPO / "legenex/gateway/llama-swap/node01.yaml").read_text())
        n2 = read_macros((REPO / "legenex/gateway/llama-swap/node02.yaml").read_text())
        conf = read_macros((REPO / "legenex/lifecycle/gx-max.conf").read_text())
        self.assertEqual("/srv" + n1["gx_mini_model"].rsplit("/", 1)[0], reg["gx-mini"]["path"])
        self.assertEqual("/srv" + n1["gx_fast_model_dir"], reg["gx-fast"]["path"])
        self.assertEqual("/srv" + n2["gx_reason_model_dir"], reg["gx-reason"]["path"])
        self.assertEqual(conf["GXMAX_MODEL_DIR"], reg["gx-max"]["path"])
        self.assertEqual(conf["GXMAX_QUANT_CELL"], "fp4")
        for alias in ("gx-mini", "gx-fast", "gx-reason", "gx-max"):
            self.assertRegex(reg[alias]["revision"], r"^[0-9a-f]{40}$", alias)
