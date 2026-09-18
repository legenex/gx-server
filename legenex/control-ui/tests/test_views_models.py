from __future__ import annotations

import unittest

from support import TempEnv

from gx_control_ui import views
from gx_control_ui.models import CATALOG, ResultLog, live_state
from gx_control_ui.services import ALL_ALIASES, Cluster

GIB = 2**30
#: L-10 as amended by D-036 (gx-music) and D-040 (gx-voice, gx-call, gx-live).
CANONICAL = ("gx-mini", "gx-fast", "gx-reason", "gx-max", "gx-auto", "gx-image", "gx-video", "gx-music",
             "gx-voice", "gx-call", "gx-live")


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
            "music": {"ok": True, "body": {"status": "ok", "engine": "unloaded", "active_jobs": 0}},
            "sglang": {"ok": False},
            # The node-2 supervisors: resident processes whose engine is unloaded.
            "tenant_gx-voice": {"ok": True, "body": {
                "status": "ok", "service": "gx-voice", "state": "unloaded", "busy": False, "active_jobs": 0,
                "memory": {"estimate_gib": 12.0, "resident_gib": 0.0}, "version": "1.0.0"}},
            "tenant_gx-call": {"ok": True, "body": {
                "status": "ok", "service": "gx-call", "state": "unloaded", "busy": False, "active_sessions": 0,
                "memory": {"estimate_gib": 48.0, "resident_gib": 0.0, "reserve_gib": 30.0}, "version": "1.0.0"}},
            "tenant_gx-live": {"ok": True, "body": {
                "status": "ok", "service": "gx-live", "state": "unloaded", "busy": False, "active_sessions": 0,
                "memory": {"estimate_gib": 34.0, "resident_gib": 0.0}, "version": "1.0.0"}},
        }
        self.state = "down"
        self.cluster.services._fn = lambda: self.svc
        self.cluster.lifecycle._fn = lambda: {"status": {"ok": True, "body": {"state": self.state}}}
        self.cluster.node1._fn = lambda: facts()
        self.cluster.node2._fn = lambda: facts()

    def tearDown(self):
        self.env.cleanup()

    def cards(self):
        for c in (self.cluster.services, self.cluster.lifecycle):
            c.invalidate()
        return {m["alias"]: m for m in live_state(self.cluster, self.results)}

    def states(self):
        return {a: m["state"] for a, m in self.cards().items()}

    def test_normal(self):
        self.assertEqual(self.states(), {
            "gx-mini": "loaded", "gx-fast": "unloaded", "gx-reason": "loading", "gx-max": "unloaded",
            "gx-auto": "loaded", "gx-image": "ready", "gx-video": "ready", "gx-music": "ready",
            "gx-voice": "ready", "gx-call": "ready", "gx-live": "ready"})

    def test_every_alias_appears_exactly_once(self):
        aliases = [m["alias"] for m in live_state(self.cluster, self.results)]
        self.assertEqual(aliases, list(CANONICAL))
        self.assertEqual(len(aliases), len(set(aliases)))

    def test_music_states(self):
        self.svc["music"]["body"]["engine"] = "ready"
        self.assertEqual(self.states()["gx-music"], "loaded")
        self.svc["music"] = {"ok": False}
        self.assertEqual(self.states()["gx-music"], "unavailable")

    def test_gxmax_ready_drains_others(self):
        self.state = "ready"
        self.svc["sglang"] = {"ok": True}
        s = self.states()
        self.assertEqual(s["gx-max"], "loaded")
        for alias in ("gx-mini", "gx-fast", "gx-reason", "gx-image", "gx-video", "gx-music",
                      "gx-voice", "gx-call", "gx-live"):
            self.assertEqual(s[alias], "unavailable", alias)
        call = self.cards()["gx-call"]
        self.assertEqual((call["service_state"], call["engine_state"]), ("BLOCKED", "BLOCKED"))

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
        # L-10 as amended by D-036 and D-040: eleven aliases, in registry order,
        # each exactly once. The registry is the list; ALIAS_ROLE only adds the
        # behaviour that belongs to the alias rather than to the model.
        self.assertEqual(tuple(CATALOG), CANONICAL)
        self.assertEqual(len(set(CATALOG)), len(CANONICAL))
        for alias in ALL_ALIASES:  # the eight that predate Build V3 are still there
            self.assertIn(alias, CATALOG, alias)
        gx = CATALOG["gx-max"]
        self.assertEqual(gx["model"], "dealignai/DeepSeek-V4-Flash-0731-CRACK-NVFP4")  # D-032
        self.assertIn("SGLang", gx["engine"])
        self.assertEqual(gx["topology"]["rank0"], "gx10-01")
        self.assertEqual(gx["topology"]["rank1"], "gx10-02")
        self.assertFalse(CATALOG["gx-max"]["vision"])
        self.assertTrue(CATALOG["gx-mini"]["vision"])

    def test_specialised_aliases_have_a_kind_and_a_playground_page(self):
        kinds = {a: CATALOG[a]["kind"] for a in CANONICAL}
        self.assertEqual({a: kinds[a] for a in ("gx-mini", "gx-fast", "gx-reason", "gx-max", "gx-auto")},
                         dict.fromkeys(("gx-mini", "gx-fast", "gx-reason", "gx-max", "gx-auto"), "llm"))
        self.assertEqual({a: kinds[a] for a in ("gx-image", "gx-video", "gx-music")},
                         dict.fromkeys(("gx-image", "gx-video", "gx-music"), "media"))
        self.assertEqual({a: kinds[a] for a in ("gx-voice", "gx-call", "gx-live")},
                         dict.fromkeys(("gx-voice", "gx-call", "gx-live"), "audio-realtime"))
        self.assertEqual(CATALOG["gx-voice"]["playground"], "#/voice")
        self.assertEqual(CATALOG["gx-call"]["playground"], "#/call")
        self.assertEqual(CATALOG["gx-live"]["playground"], "#/live")

    def test_specialised_cards_do_not_need_chat_metadata(self):
        for alias in ("gx-voice", "gx-call", "gx-live", "gx-music"):
            card = CATALOG[alias]
            with self.subTest(alias):
                # No context window, no tool/vision flags, no llama-swap entry...
                self.assertIsNone(card["context"], alias)
                self.assertIsNone(card["max_output"], alias)
                self.assertNotEqual(card["task"], "chat", alias)
                # ...but everything the card actually renders is there.
                self.assertTrue(card["purpose"] and card["endpoint"], alias)
                self.assertTrue(card["model"] and card["model"] != "—", alias)
                self.assertEqual(card["nodes"], ["gx10-02"], alias)
                self.assertTrue(card["capabilities"], alias)

    def test_voice_and_call_are_bound_and_measured(self):
        voice, call = CATALOG["gx-voice"], CATALOG["gx-call"]
        self.assertEqual(voice["repository"], "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice")
        self.assertEqual(call["repository"], "nvidia/NVIDIA-NemotronLabs-VoiceChat-11B")
        self.assertEqual(call["revision"], "a4c40ca5b4fe77db13e9840ca4a2b91becf030c8")
        self.assertEqual(call["image"], "gx-call-engine:voicechat-097dfe9-t215")
        self.assertEqual(call["service"]["port"], 18840)
        # The backbone the engine cannot serve without (t215 acceptance).
        dep = call["depends_on"][0]
        self.assertEqual(dep["repository"], "nvidia/NVIDIA-Nemotron-Nano-9B-v2")
        self.assertEqual(dep["revision"], "6533e8de2c68e4536bf7c411d7a3ce5734111476")
        self.assertEqual(call["measured_footprint"]["resident_gib"], 34.5)
        for alias in ("gx-voice", "gx-call", "gx-live"):
            self.assertRegex(CATALOG[alias]["revision"], r"^[0-9a-f]{40}$", alias)

    def test_an_unloaded_engine_is_ready_not_offline(self):
        card = self.cards()["gx-call"]
        self.assertEqual(card["state"], "ready")
        self.assertEqual((card["service_state"], card["engine_state"]), ("READY", "UNLOADED"))
        self.assertIn("on demand", card["state_detail"])
        self.assertEqual(card["live"]["supervisor"]["memory"]["estimate_gib"], 48.0)

    def test_service_and_engine_states_are_separate(self):
        body = self.svc["tenant_gx-call"]["body"]
        for raw, state, engine in (("ready", "loaded", "LOADED"), ("loading", "loading", "LOADING"),
                                   ("busy", "loaded", "BUSY"), ("waiting", "loading", "QUEUED"),
                                   ("unloading", "unloading", "BUSY"), ("failed", "error", "ERROR")):
            body["state"] = raw
            card = self.cards()["gx-call"]
            with self.subTest(raw):
                self.assertEqual(card["state"], state)
                self.assertEqual(card["engine_state"], engine)
                # The supervisor answered, so the service itself is up.
                self.assertEqual(card["service_state"], "READY")

    def test_unreachable_supervisor_is_the_only_service_error(self):
        self.svc["tenant_gx-voice"] = {"ok": False, "status": 0, "error": "connection refused"}
        card = self.cards()["gx-voice"]
        self.assertEqual(card["state"], "unavailable")
        self.assertEqual((card["service_state"], card["engine_state"]), ("ERROR", "ERROR"))
        self.assertIn("not reachable", card["state_detail"])

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
