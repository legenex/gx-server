"""Fixes from the independent review of 923aa3f (D-053): switch validation
against a stub gateway, preflight, rollback readiness, startup reconciliation
of interrupted switches, no acquisition during a switch, the active-deployment
consumers and the health probe target."""
from __future__ import annotations

import http.server
import json
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from gx_orchestrator.deployments import ActiveDeploymentStore  # noqa: E402
from gx_orchestrator.profiles import load_registry  # noqa: E402
from gx_orchestrator.switching import SwitchHistory, reconcile_interrupted  # noqa: E402
from gx_orchestrator.validation import GatewayValidator, HostPreflight, rollback_readiness  # noqa: E402
from server_harness import OrchestratorHarness  # noqa: E402
from test_dsv4_primary import FakeManager, REPO_REGISTRY  # noqa: E402


def reg():
    return load_registry(REPO_REGISTRY)


class FakeFacts:
    def __init__(self, running=True, mem=120.0, images=True, counts=None):
        self.running = running
        self.mem = mem
        self.images = images
        self.counts = counts or {}
        self.tx = 0
        self.weight_queries = []

    def container_running(self, node, name):
        return self.running(node, name) if callable(self.running) else self.running

    def image_id(self, node, image):
        return "sha256:abc" if self.images else ""

    def safetensors_count(self, node, path):
        self.weight_queries.append((node, path))
        return self.counts.get((node, path), 0)

    def rdma_tx_bytes(self, hca="rocep1s0f0"):
        self.tx += 4096
        return self.tx

    def read_mem(self, node):
        return self.mem


# ----------------------------------------------------------- stub gateway
class _Gateway(http.server.BaseHTTPRequestHandler):
    deployment = "dsv4-flash-primary"
    served = "DeepSeek-V4-Flash-0731-abliterated"
    seen: list = []
    broken = set()

    def log_message(self, *a):
        pass

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        type(self).seen.append((dict(self.headers), body))
        msgs = body["messages"]
        hdrs = {"llm_provider-x-gx-deployment": type(self).deployment,
                "llm_provider-x-gx-served-model": type(self).served}
        if body.get("stream"):
            chunks = ["1", ", 2", ", 3", ", 4", ", 5"] if "stream" not in self.broken else ["1, 2, 3, 4, 5"]
            self.send_response(200)
            for k, v in hdrs.items():
                self.send_header(k, v)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for c in chunks:
                self.wfile.write(b"data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": c}}]}).encode() + b"\n\n")
            if "done" not in self.broken:
                self.wfile.write(b"data: [DONE]\n\n")
            self.close_connection = True
            return
        if any(m.get("role") == "tool" for m in msgs):
            msg = {"role": "assistant", "content": "It is 21 C and clear in Paris."}
        elif body.get("tools"):
            if body["model"] in self.broken:
                msg = {"role": "assistant", "content": "I would call get_weather."}
            else:
                msg = {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function",
                       "function": {"name": "get_weather", "arguments": json.dumps({"city": "Paris"})}}]}
        else:
            msg = {"role": "assistant", "content": "17*19=323"}
        data = json.dumps({"choices": [{"index": 0, "message": msg, "finish_reason": "stop"}]}).encode()
        self.send_response(200)
        for k, v in hdrs.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class TestGatewayValidator(unittest.TestCase):
    def setUp(self):
        _Gateway.seen = []
        _Gateway.broken = set()
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Gateway)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.shutdown)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}/v1"
        self.facts = FakeFacts()

    def run_checks(self, name="dsv4-flash-primary"):
        v = GatewayValidator(registry=reg(), gateway_base=self.base, gateway_key="k" * 20, facts=self.facts, timeout=10)
        return {c.name: c for c in v(name, "tok-123")}

    def test_all_checks_pass_and_use_gx_fast_with_room_to_answer(self):
        checks = self.run_checks()
        self.assertTrue(all(c.ok for c in checks.values()), {n: c.detail for n, c in checks.items() if not c.ok})
        self.assertEqual(set(checks), {"rank_node1", "rank_node2", "completion", "served_identity", "fabric_rdma",
                                       "streaming", "tool_call", "tool_result_continuation", "tool_call_thinking"})
        models = [b["model"] for _h, b in _Gateway.seen]
        self.assertEqual(models.count("gx-high"), 1)          # the thinking-mode tool call
        self.assertTrue(all(m == "gx-fast" for m in models if m != "gx-high"))
        self.assertTrue(all(b["max_tokens"] >= 256 for _h, b in _Gateway.seen))
        self.assertTrue(all({k.lower(): v for k, v in h.items()}.get("x-gx-switch-validation") == "tok-123"
                            for h, _b in _Gateway.seen))

    def test_v41_is_validated_the_same_way(self):
        _Gateway.deployment, _Gateway.served = "dsv41-rollback", "DeepSeek-v4.1-Flash-EXL3"
        self.addCleanup(lambda: (setattr(_Gateway, "deployment", "dsv4-flash-primary"),
                                 setattr(_Gateway, "served", "DeepSeek-V4-Flash-0731-abliterated")))
        checks = self.run_checks("dsv41-rollback")
        self.assertTrue(checks["completion"].ok and checks["served_identity"].ok)
        self.assertTrue(all(b["model"] in ("gx-fast", "gx-high") for _h, b in _Gateway.seen))

    def test_wrong_identity_single_chunk_stream_missing_done_and_swallowed_tool_call_fail(self):
        checks = self.run_checks("dsv41-rollback")      # gateway claims dsv4-flash-primary
        self.assertFalse(checks["served_identity"].ok)
        _Gateway.broken = {"stream"}
        self.assertFalse(self.run_checks()["streaming"].ok)
        _Gateway.broken = {"done"}
        self.assertFalse(self.run_checks()["streaming"].ok)
        _Gateway.broken = {"gx-high"}
        self.assertFalse(self.run_checks()["tool_call_thinking"].ok)

    def test_unknown_rank_state_is_not_a_pass(self):
        self.facts.running = None
        checks = self.run_checks()
        self.assertFalse(checks["rank_node2"].ok)
        self.assertIn("unknown", checks["rank_node2"].detail)


class TestPreflightAndReadiness(unittest.TestCase):
    def test_preflight_memory_images_and_maintenance(self):
        with tempfile.TemporaryDirectory() as d:
            facts = FakeFacts(mem=112.0)
            pf = HostPreflight(registry=reg(), facts=facts, guard_dir=Path(d))
            self.assertTrue(all(c.ok for c in pf("dsv4-flash-primary")))
            (Path(d) / "node2.maintenance-hold").write_text("x")
            failed = [c.name for c in pf("dsv4-flash-primary") if not c.ok]
            self.assertEqual(failed, ["maintenance_node2"])
            (Path(d) / "node2.maintenance-hold").unlink()
            facts.images = False
            self.assertIn("image_node1", [c.name for c in pf("dsv4-flash-primary") if not c.ok])

    def test_preflight_refuses_a_node_without_boot_memory(self):
        import gx_orchestrator.validation as V
        old = V.time.sleep
        V.time.sleep = lambda s: None
        self.addCleanup(setattr, V.time, "sleep", old)
        with tempfile.TemporaryDirectory() as d:
            checks = HostPreflight(registry=reg(), facts=FakeFacts(mem=100.0), guard_dir=Path(d))("dsv4-flash-primary")
            self.assertIn("memory_node1", [c.name for c in checks if not c.ok])

    def test_rollback_readiness_checks_each_pack_on_its_own_node(self):
        r = reg()
        v4 = r.model("dsv4-flash-0731-abliterated").path
        v41 = r.model("dsv41-flash-exl3-uncensored").path
        facts = FakeFacts(counts={("node2", v4): 49, ("node1", v41): 39})
        out = rollback_readiness(r, facts, [{"state": "succeeded", "target": "dsv41-rollback", "ended": 1.0}],
                                 "dsv41-rollback", "ready")
        self.assertEqual(out["dsv4-flash-primary"]["weights"]["node"], "node2")
        self.assertEqual(out["dsv41-rollback"]["weights"]["node"], "node1")   # not matched by a dsv4 prefix (A5)
        self.assertTrue(out["dsv41-rollback"]["rollback_ready"])
        self.assertTrue(out["dsv41-rollback"]["loaded"])
        self.assertEqual(out["dsv41-rollback"]["last_test"], 1.0)
        facts.images = False
        self.assertFalse(rollback_readiness(r, facts, [], "dsv41-rollback", "ready")["dsv41-rollback"]["rollback_ready"])


class TestReconcileInterrupted(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ActiveDeploymentStore(Path(self.tmp.name) / "active.json")
        self.hist = SwitchHistory(Path(self.tmp.name) / "h.jsonl")
        self.hist.append({"id": "s1", "state": "running", "target": "dsv4-flash-primary", "previous": "dsv41-rollback"})

    def run_with(self, up):
        return reconcile_interrupted(self.hist.mark_interrupted(), registry=reg(), store=self.store,
                                     history=self.hist, running=lambda name: up.get(name, {"node1": False, "node2": False}))

    def test_target_fully_up_completes_the_pointer(self):
        out = self.run_with({"dsv4-flash-primary": {"node1": True, "node2": True}})
        self.assertEqual(out["outcome"], "pointer_completed")
        self.assertEqual(self.store.read()["deployment"], "dsv4-flash-primary")
        self.assertFalse(out["latch"])
        self.assertEqual(self.hist.read()[-1]["reconcile"]["outcome"], "pointer_completed")

    def test_two_kits_up_needs_an_operator_and_latches(self):
        out = self.run_with({"dsv4-flash-primary": {"node1": True, "node2": False},
                             "dsv41-rollback": {"node1": True, "node2": True}})
        self.assertEqual(out["outcome"], "needs_operator")
        self.assertIn("legenex/models/dsv4-flash/stop.sh", out["action"])
        self.assertTrue(out["latch"])
        self.assertIsNone(self.store.read())    # pointer untouched

    def test_nothing_running_names_the_next_action_without_starting(self):
        out = self.run_with({})
        self.assertEqual(out["outcome"], "needs_operator")
        self.assertIn("/lifecycle/gx-max/acquire", out["action"])
        self.assertFalse(out["latch"])

    def test_probe_errors_latch(self):
        out = self.run_with({"dsv41-rollback": {"node1": True, "node2": None}})
        self.assertEqual(out["outcome"], "needs_operator")
        self.assertTrue(out["latch"])

    def test_no_interrupted_switch_is_a_no_op(self):
        self.hist.mark_interrupted()
        self.assertEqual(reconcile_interrupted([], registry=reg(), store=self.store, history=self.hist,
                                               running=lambda n: {})["outcome"], "none")


class TestNoAcquireDuringASwitch(OrchestratorHarness):
    def test_paused_scheduler_refuses_before_any_acquisition(self):
        from gx_orchestrator.lifecycle import State
        self.lifecycle._state = State.DOWN
        self.scheduler.pause("model switch s1 to dsv4-flash-primary in progress")
        self.addCleanup(self.scheduler.resume)
        for model, hdr in (("gx-auto", {"X-GX-Priority": "interactive"}), ("gx-fast", {})):
            status, _h, body = self.chat({"model": model, "messages": [{"role": "user", "content": "hi"}]}, headers=hdr)
            self.assertEqual(status, 503, model)
            self.assertEqual(body["error"]["code"], "model_switch_in_progress")
        self.assertEqual(self.lifecycle.acquire_calls, [], "a request during a switch must never start a kit")


class TestActiveDeploymentConsumers(unittest.TestCase):
    def test_active_model_follows_the_pointer_not_the_alias(self):
        r = reg()
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(r.active_model(d).source, "dealignai/DeepSeek-V4.1-Flash-UNCENSORED-EXL3-2.9bpw")
            ActiveDeploymentStore(Path(d) / "orchestrator" / "active-deployment.json").write("dsv4-flash-primary")
            self.assertEqual(r.active_model(d).source, "cebeuq/DeepSeek-V4-Flash-0731-abliterated")
            self.assertEqual(r.active_runtime(d).served_model_id, "DeepSeek-V4-Flash-0731-abliterated")
            self.assertEqual(r.alias("gx-max").model, "dsv41-flash-exl3-uncensored")   # landing binding (L4)

    def test_status_cli_reports_the_active_model(self):
        from gx_orchestrator.config import Config
        from gx_orchestrator.status_cli import registry_model_facts
        with tempfile.TemporaryDirectory() as d:
            ActiveDeploymentStore(Path(d) / "orchestrator" / "active-deployment.json").write("dsv4-flash-primary")
            facts = registry_model_facts(Config(registry_path=REPO_REGISTRY, state_dir=Path(d)))
        self.assertEqual(facts["deployment"], "dsv4-flash-primary")
        self.assertEqual(facts["model_id"], "DeepSeek-V4-Flash-0731-abliterated")

    def test_health_probes_the_active_runtime(self):
        from gx_orchestrator.config import Config
        from gx_orchestrator.health import ClusterHealth
        urls = []

        def get(url, timeout):
            urls.append(url)
            raise OSError("down")
        h = ClusterHealth(Config(), reg(), http_get=get,
                          active=lambda: ("http://127.0.0.1:8890/v1", "DeepSeek-V4-Flash-0731-abliterated", "dsv4f-worker"))
        out = h._probe_head()
        self.assertEqual(urls, ["http://127.0.0.1:8890/health"])
        self.assertEqual(out["model_id"], "DeepSeek-V4-Flash-0731-abliterated")


if __name__ == "__main__":
    unittest.main()
