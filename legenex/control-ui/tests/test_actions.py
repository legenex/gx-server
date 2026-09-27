"""Hermetic ActionRunner tests: the V4.1 action set against a fake
orchestrator, with confirmation, args validation and audit."""

from __future__ import annotations

import json
import os
import time
import unittest
from pathlib import Path
from unittest import mock

from support import StubUpstream, TempEnv

from gx_control_ui.actions import ActionRunner, ActionRefused
from gx_control_ui.models import ResultLog
from gx_control_ui.services import Cluster


class ActionsBase(unittest.TestCase):
    def setUp(self):
        self.stub = StubUpstream({
            ("GET", "/lifecycle/gx-max/status"): (200, {"state": "down"}),
            ("GET", "/lifecycle/gx-max/events"): (200, {"seq": 0, "events": [], "history": []}),
            ("GET", "/lifecycle/gx-max/events?limit=400"): (200, {"seq": 0, "events": [], "history": []}),
            ("POST", "/lifecycle/gx-max/acquire"): (200, {"state": "ready", "last_startup_seconds": 300}),
            ("POST", "/lifecycle/gx-max/release"): (200, {"state": "down"}),
            ("POST", "/lifecycle/gx-max/drain"): (200, {"state": "draining"}),
        })
        self.env = TempEnv(orchestrator_base=self.stub.url, offline=False)
        self.results = ResultLog(self.env.cfg.state_dir / "results.json")
        self.runner = ActionRunner(self.env.cfg, Cluster(self.env.cfg), self.results)

    def tearDown(self):
        self.stub.close()
        self.env.cleanup()

    def submit(self, name, confirm=None, args=None):
        return self.runner.submit(name, user="admin", ip="127.0.0.1", confirm=confirm, args=args)

    def wait(self, job, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            fresh = self.runner.job(job.id)
            if fresh["state"] != "running":
                return fresh
            time.sleep(0.02)
        return self.runner.job(job.id)


class TestRegistry(ActionsBase):
    def test_the_v41_action_set(self):
        names = set(self.runner.registry)
        self.assertLessEqual({"gxmax_start", "gxmax_stop", "gxmax_restart", "gxmax_drain",
                              "health_check", "benchmark_run", "scheduler_cancel", "scheduler_retry",
                              "trash_restore", "purge_trash", "update_check", "system.refresh",
                              "infra.restart_litellm", "infra.restart_orchestrator"}, names)
        for banned_prefix in ("model.gx-mini", "model.gx-code", "model.gx-image", "model.gx-music"):
            self.assertFalse([n for n in names if n.startswith(banned_prefix)], banned_prefix)
        for spec in self.runner.registry.values():
            self.assertIn(spec.danger, ("safe", "caution", "danger"))
            self.assertNotIn("llama-swap", spec.description)

    def test_unknown_operation_and_args(self):
        with self.assertRaises(ActionRefused):
            self.submit("shell")
        with self.assertRaises(ActionRefused):
            self.submit("gxmax_start", confirm="gx-max", args={"evil": "x"})
        with self.assertRaises(ActionRefused):
            self.submit("gxmax_start", confirm="gx-max", args={"profile": 42})


class TestGxMaxLifecycle(ActionsBase):
    def test_start_requires_the_typed_confirmation_and_a_valid_profile(self):
        with self.assertRaises(ActionRefused):
            self.submit("gxmax_start")
        with self.assertRaises(ActionRefused):
            self.submit("gxmax_start", confirm="gx-max", args={"profile": "nope"})
        job = self.submit("gxmax_start", confirm="gx-max", args={"profile": "fast"})
        done = self.wait(job)
        self.assertEqual(done["state"], "succeeded", done["output"])
        method, path, headers, body = self.stub.calls[-1]
        self.assertEqual((method, path), ("POST", "/lifecycle/gx-max/acquire"))
        self.assertEqual(body["profile"], "fast")
        self.assertEqual(self.results.get("gx-max")["load"]["profile"], "fast")

    def test_start_refused_while_ready(self):
        self.stub.routes[("GET", "/lifecycle/gx-max/status")] = (200, {"state": "ready"})
        with self.assertRaises(ActionRefused) as ctx:
            self.submit("gxmax_start", confirm="gx-max")
        self.assertIn("already READY", str(ctx.exception))

    def test_start_refused_in_maintenance(self):
        self.runner.maintenance = lambda: True
        with self.assertRaises(ActionRefused) as ctx:
            self.submit("gxmax_start", confirm="gx-max")
        self.assertIn("Maintenance", str(ctx.exception))

    def test_stop_calls_the_release_endpoint(self):
        self.stub.routes[("GET", "/lifecycle/gx-max/status")] = (200, {"state": "ready"})
        job = self.submit("gxmax_stop", confirm=True)
        done = self.wait(job)
        self.assertEqual(done["state"], "succeeded", done["output"])
        method, path, headers, body = self.stub.calls[-1]
        self.assertEqual((method, path, body), ("POST", "/lifecycle/gx-max/release",
                                                {"force": False, "restore": True}))

    def test_stop_refused_when_down(self):
        with self.assertRaises(ActionRefused):
            self.submit("gxmax_stop", confirm=True)

    def test_drain(self):
        job = self.submit("gxmax_drain", confirm=True)
        done = self.wait(job)
        self.assertEqual(done["state"], "succeeded", done["output"])
        self.assertEqual(self.stub.calls[-1][1], "/lifecycle/gx-max/drain")


class TestSchedulerRelay(ActionsBase):
    def test_cancel_and_retry_relay_the_request_id(self):
        for name in ("scheduler_cancel", "scheduler_retry"):
            job = self.submit(name, confirm=True, args={"request_id": "req-123"})
            done = self.wait(job)
            self.assertEqual(done["state"], "succeeded", (name, done["output"]))
            method, path, headers, body = self.stub.calls[-1]
            self.assertEqual(body, {"id": "req-123"})

    def test_bad_request_id_fails_honestly(self):
        job = self.submit("scheduler_cancel", confirm=True, args={"request_id": "x y"})
        done = self.wait(job)
        self.assertEqual(done["state"], "failed")
        self.assertIn("invalid request id", " ".join(done["output"]))


class TestSafety(ActionsBase):
    def test_audit_log_written(self):
        self.submit("system.refresh", confirm=True)
        audit = Path(self.env.cfg.log_dir) / "audit.log"
        time.sleep(0.1)
        text = audit.read_text()
        self.assertIn('"action": "system.refresh"', text)
        self.assertIn('"outcome": "started"', text)
        self.assertIn('"user": "admin"', text)

    def test_benchmark_refuses_when_the_suite_is_missing(self):
        self.env.cfg.bench_dir  # the fixture repo has no ops/bench
        job = self.submit("benchmark_run", confirm=True, args={"name": "startup"})
        done = self.wait(job)
        self.assertEqual(done["state"], "failed")
        self.assertIn("not present", " ".join(done["output"]))


if __name__ == "__main__":
    unittest.main()
