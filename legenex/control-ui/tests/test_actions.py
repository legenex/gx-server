from __future__ import annotations

import json
import threading
import time
import unittest
from unittest import mock

from support import StubUpstream, TempEnv, env_vars, fake_key

from gx_control_ui import actions as actions_mod
from gx_control_ui.actions import FORCE_CONFIRM, GXMAX_CONFIRM, ActionRefused, ActionRunner
from gx_control_ui.models import ResultLog
from gx_control_ui.services import Cluster
from gx_control_ui.util import CmdResult


def wait_done(runner, job_id, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        j = runner.job(job_id)
        if j["state"] != "running":
            return j
        time.sleep(0.05)
    raise AssertionError("job did not finish")


class Base(unittest.TestCase):
    lifecycle_state = "down"

    def setUp(self):
        self.orch_calls = []
        state = {"value": self.lifecycle_state}
        self.state = state

        def acquire(handler, body):
            self.orch_calls.append(("acquire", body))
            state["value"] = "ready"
            return 200, {"status": "ready", "state": "ready", "last_startup_seconds": 512}

        def release(handler, body):
            self.orch_calls.append(("release", body))
            state["value"] = "down"
            return 200, {"status": "released", "state": "down"}

        self.stub = StubUpstream({
            ("GET", "/lifecycle/gx-max/status"): lambda h, b: (
                200, {"state": state["value"], "waiters": 0}),
            ("GET", "/lifecycle/gx-max/events"): (200, {"seq": 0, "events": [], "history": [], "active_job": None}),
            ("POST", "/lifecycle/gx-max/acquire"): acquire,
            ("POST", "/lifecycle/gx-max/release"): release,
            ("GET", "/health"): (200, {"status": "ok"}),
            ("POST", "/api/models/unload/gx-fast"): (200, "OK"),
            ("GET", "/upstream/gx-fast/health"): (200, "OK"),
        })
        self.env = TempEnv(orchestrator_base=self.stub.url, node1_swap_base=self.stub.url,
                           node2_swap_base=self.stub.url, media_base=self.stub.url)
        self.cluster = Cluster(self.env.cfg)
        self.facts = {"node1": self._facts(), "node2": {**self._facts(), "reachable": True}}
        self.cluster.node1._fn = lambda: self.facts["node1"]
        self.cluster.node2._fn = lambda: self.facts["node2"]
        # offline=True would short-circuit these; let them hit the stub instead
        self.cluster.lifecycle._fn = lambda: {
            "status": {"ok": True, "body": {"state": state["value"]}},
            "events": {"ok": True, "body": {}},
        }
        self.cluster.services._fn = lambda: {"media": {"ok": True, "body": {"busy": False, "video_queue_depth": 0}}}
        self.results = ResultLog(self.env.cfg.state_dir / "r.json")
        self.runner = ActionRunner(self.env.cfg, self.cluster, self.results)

    def _facts(self, containers=()):
        return {"reachable": True, "memory": {"MemAvailable": 110 * 2**30},
                "docker": {"containers": [{"name": n, "state": "running"} for n in containers]}}

    def tearDown(self):
        self.stub.close()
        self.env.cleanup()


class TestRegistry(Base):
    def test_no_generic_command_operation(self):
        names = set(self.runner.registry)
        for forbidden in ("shell", "exec", "command", "run", "system.shell", "system.upgrade",
                          "system.kernel_update", "system.firmware"):
            self.assertNotIn(forbidden, names)
        for n in names:
            self.assertRegex(n, r"^(model\.gx-[a-z]+\.(load|unload|restart|force_release)"
                                r"|system\.[a-z_0-9]+|infra\.[a-z_0-9]+)$")

    def test_gxmax_has_no_docker_path(self):
        import inspect
        src = inspect.getsource(actions_mod.build_registry)
        block = src[src.index("gx-max (orchestrator lifecycle only)"):src.index("llama-swap tiers")]
        self.assertNotIn("docker", block.replace("docker command", ""))
        self.assertIn("/lifecycle/gx-max/acquire", block)
        self.assertIn("/lifecycle/gx-max/release", block)

    def test_required_system_actions_exist(self):
        for n in ("system.integrity_audit", "system.kernel_verify", "system.refresh", "system.reconcile_node2",
                  "system.restart_ui", "infra.restart_litellm", "model.gx-max.load", "model.gx-max.unload",
                  "model.gx-max.force_release", "model.gx-image.unload", "model.gx-reason.load"):
            self.assertIn(n, self.runner.registry)

    def test_unknown_operation(self):
        with self.assertRaises(ActionRefused) as ctx:
            self.runner.submit("shell", user="admin", ip="t")
        self.assertEqual(ctx.exception.status, 404)


class TestGxMax(Base):
    def test_load_requires_typed_confirmation(self):
        for bad in (None, True, "GX-MAX", "yes"):
            with self.assertRaises(ActionRefused):
                self.runner.submit("model.gx-max.load", user="admin", ip="t", confirm=bad)
        self.assertEqual(self.orch_calls, [])

    def test_load_calls_orchestrator_acquire(self):
        job = self.runner.submit("model.gx-max.load", user="admin", ip="t", confirm=GXMAX_CONFIRM)
        done = wait_done(self.runner, job.id)
        self.assertEqual(done["state"], "succeeded", done["output"])
        self.assertEqual(self.orch_calls[0][0], "acquire")
        self.assertEqual(done["result"]["startup_seconds"], 512)
        self.assertTrue(self.results.get("gx-max")["load"]["ok"])

    def test_load_refused_when_ready_or_transitioning(self):
        for st in ("ready", "acquiring", "releasing"):
            self.state["value"] = st
            with self.assertRaises(ActionRefused):
                self.runner.submit("model.gx-max.load", user="a", ip="t", confirm=GXMAX_CONFIRM)
        self.assertEqual(self.orch_calls, [])

    def test_load_refused_while_media_busy_or_node2_down(self):
        self.cluster.services._fn = lambda: {"media": {"ok": True, "body": {"busy": True}}}
        with self.assertRaises(ActionRefused):
            self.runner.submit("model.gx-max.load", user="a", ip="t", confirm=GXMAX_CONFIRM)
        self.cluster.services._fn = lambda: {"media": {"ok": True, "body": {"busy": False}}}
        self.facts["node2"] = {"reachable": False}
        self.cluster.node2.invalidate()
        with self.assertRaises(ActionRefused):
            self.runner.submit("model.gx-max.load", user="a", ip="t", confirm=GXMAX_CONFIRM)

    def test_graceful_release_verifies_ranks_gone(self):
        self.state["value"] = "ready"
        job = self.runner.submit("model.gx-max.unload", user="a", ip="t", confirm=True)
        done = wait_done(self.runner, job.id)
        self.assertEqual(done["state"], "succeeded", done["output"])
        self.assertEqual(self.orch_calls, [("release", {"force": False, "restore": True})])
        self.assertTrue(any("no gx-max rank container" in line for line in done["output"]))

    def test_release_fails_if_a_rank_remains(self):
        self.state["value"] = "ready"
        self.facts["node2"] = {**self._facts(["gx-max-rank1"]), "reachable": True}
        job = self.runner.submit("model.gx-max.unload", user="a", ip="t", confirm=True)
        done = wait_done(self.runner, job.id)
        self.assertEqual(done["state"], "failed")

    def test_unload_refused_when_down(self):
        with self.assertRaises(ActionRefused):
            self.runner.submit("model.gx-max.unload", user="a", ip="t", confirm=True)

    def test_force_release(self):
        with self.assertRaises(ActionRefused):  # nothing to release
            self.runner.submit("model.gx-max.force_release", user="a", ip="t", confirm=FORCE_CONFIRM)
        self.state["value"] = "acquiring"
        with self.assertRaises(ActionRefused):
            self.runner.submit("model.gx-max.force_release", user="a", ip="t", confirm="force release")
        job = self.runner.submit("model.gx-max.force_release", user="a", ip="t", confirm=FORCE_CONFIRM)
        wait_done(self.runner, job.id)
        self.assertEqual(self.orch_calls[0], ("release", {"force": True, "restore": True}))


class TestSwapAndInfra(Base):
    def test_model_ops_refused_while_gxmax_not_down(self):
        self.state["value"] = "ready"
        for name in ("model.gx-fast.load", "infra.restart_swap_node1", "infra.restore_normal"):
            with self.assertRaises(ActionRefused, msg=name):
                self.runner.submit(name, user="a", ip="t", confirm=True)

    def test_model_ops_refused_when_rank_exists(self):
        self.facts["node1"] = self._facts(["gx-max-rank0"])
        with self.assertRaises(ActionRefused):
            self.runner.submit("model.gx-fast.load", user="a", ip="t")

    def test_admission_refusal(self):
        self.facts["node1"]["memory"]["MemAvailable"] = 40 * 2**30  # 40 - 25 < 30 reserve
        with self.assertRaises(ActionRefused) as ctx:
            self.runner.submit("model.gx-fast.load", user="a", ip="t")
        self.assertIn("admission", str(ctx.exception))

    def test_load_and_unload_gx_fast(self):
        key = fake_key()
        with env_vars(GX_SWAP_API_KEY=key):
            job = self.runner.submit("model.gx-fast.load", user="a", ip="t")
            done = wait_done(self.runner, job.id)
            self.assertEqual(done["state"], "succeeded", done["output"])
            call = [c for c in self.stub.calls if c[1] == "/upstream/gx-fast/health"][0]
            self.assertEqual(call[2]["Authorization"], f"Bearer {key}")
            self.assertNotIn(key, json.dumps(done))
            job = self.runner.submit("model.gx-fast.unload", user="a", ip="t", confirm=True)
            self.assertEqual(wait_done(self.runner, job.id)["state"], "succeeded")

    def test_group_serialisation(self):
        gate = threading.Event()
        original = self.cluster.swap_load
        self.cluster.swap_load = lambda alias, timeout: (gate.wait(5), (True, "ok"))[1]
        job = self.runner.submit("model.gx-fast.load", user="a", ip="t")
        with self.assertRaises(ActionRefused) as ctx:
            self.runner.submit("model.gx-mini.unload", user="a", ip="t", confirm=True)
        self.assertIn("still running", str(ctx.exception))
        gate.set()
        wait_done(self.runner, job.id)
        self.cluster.swap_load = original

    def test_media_unload_uses_fixed_ssh_command(self):
        seen = []
        def fake_run(args, timeout=0, **k):
            seen.append(args)
            return CmdResult(0, "freed", 1)
        with mock.patch.object(actions_mod, "run", fake_run):
            job = self.runner.submit("model.gx-image.unload", user="a", ip="t", confirm=True)
            self.assertEqual(wait_done(self.runner, job.id)["state"], "succeeded")
        self.assertIn("http://127.0.0.1:8188/free", seen[0][-1])
        self.assertEqual(seen[0][0], "ssh")

    def test_infra_restart_litellm_command(self):
        seen = []
        def fake_run(args, timeout=0, **k):
            seen.append(args)
            return CmdResult(0, "gx-litellm", 1)
        with mock.patch.object(actions_mod, "run", fake_run), \
                mock.patch.object(ActionRunner, "wait_http", lambda self, url, s, h=None: True):
            job = self.runner.submit("infra.restart_litellm", user="a", ip="t", confirm=True)
            self.assertEqual(wait_done(self.runner, job.id)["state"], "succeeded")
        self.assertEqual(seen[0], ["docker", "restart", "-t", "30", "gx-litellm"])


class TestAudit(Base):
    def test_audit_trail_and_redaction(self):
        secret = fake_key()
        with env_vars(GX_MEDIA_API_KEY=secret):
            with self.assertRaises(ActionRefused):
                self.runner.submit("model.gx-max.load", user="admin", ip="100.1.2.3")
            job = self.runner.submit("system.refresh", user="admin", ip="100.1.2.3")
            wait_done(self.runner, job.id)
            self.runner.audit(user="admin", ip="x", action="test", note=f"key {secret}")
        lines = [json.loads(line) for line in self.runner.audit_path.read_text().splitlines()]
        outcomes = [(e["action"], e["outcome"]) for e in lines if "outcome" in e]
        self.assertIn(("model.gx-max.load", "refused"), outcomes)
        self.assertIn(("system.refresh", "started"), outcomes)
        self.assertIn(("system.refresh", "succeeded"), outcomes)
        self.assertNotIn(secret, self.runner.audit_path.read_text())
        self.assertEqual(self.runner.audit_path.stat().st_mode & 0o777, 0o640)


if __name__ == "__main__":
    unittest.main()
