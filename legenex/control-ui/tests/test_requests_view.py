"""Hermetic requests_view tests against a fake orchestrator (StubUpstream)."""

from __future__ import annotations

import time
import unittest

from support import StubUpstream, TempEnv

from gx_control_ui import requests_view as rv


def _record(i=0, **over):
    rec = {"id": f"req-{i}", "project": "proj-a", "agent": "worker", "task": "build",
           "priority": "normal-worker", "profile": "balanced", "reasoning": "medium",
           "state": "queued", "enqueue_ts": 1700000000.0 + i, "start_ts": None, "done_ts": None,
           "timeout_at": None, "prompt_tokens": 100, "completion_tokens": 0,
           "cached_tokens": 0, "ttft_ms": None, "tps": None, "error": None}
    rec.update(over)
    return rec


class RequestsBase(unittest.TestCase):
    def setUp(self):
        self.records = [_record(0), _record(1, state="active", agent="planner", project="proj-b"),
                        _record(2, state="error", profile="fast", project="proj-c")]
        self.stub = StubUpstream({
            ("GET", "/scheduler/status"): (200, {"queued": self.records[:1], "active": self.records[1:2],
                                                 "counts": {"queued": 1, "active": 1}}),
            ("GET", "/scheduler/history"): (200, {"records": self.records}),
            ("POST", "/scheduler/cancel"): (200, {"id": "req-0", "state": "cancelled"}),
            ("POST", "/scheduler/retry"): (200, {"id": "req-2", "state": "queued"}),
        })
        self.env = TempEnv(orchestrator_base=self.stub.url, offline=False)
        self.addCleanup(self.stub.close)
        self.addCleanup(self.env.cleanup)


class TestStatus(RequestsBase):
    def test_status_passthrough(self):
        out = rv.status(self.env.cfg)
        self.assertTrue(out["available"])
        self.assertEqual(out["counts"], {"queued": 1, "active": 1})
        self.assertEqual(self.stub.calls[0][0], "GET")
        self.assertEqual(self.stub.calls[0][1], "/scheduler/status")

    def test_status_honest_when_unreachable(self):
        env = TempEnv(orchestrator_base="http://127.0.0.1:9", offline=False)
        self.addCleanup(env.cleanup)
        out = rv.status(env.cfg)
        self.assertFalse(out["available"])
        self.assertIn("reason", out)

    def test_offline_is_honest(self):
        env = TempEnv()
        self.addCleanup(env.cleanup)
        out = rv.status(env.cfg)
        self.assertEqual(out, {"available": False, "reason": "offline mode"})


class TestHistory(RequestsBase):
    def test_history_relayed_and_filtered(self):
        out = rv.history(self.env.cfg, state="active")
        self.assertTrue(out["available"])
        self.assertEqual([r["id"] for r in out["records"]], ["req-1"])
        out = rv.history(self.env.cfg, project="proj-a")
        self.assertEqual([r["id"] for r in out["records"]], ["req-0"])
        out = rv.history(self.env.cfg, profile="fast")
        self.assertEqual([r["id"] for r in out["records"]], ["req-2"])
        out = rv.history(self.env.cfg, agent="planner", since=1699999999.0, until=1700000002.0)
        self.assertEqual([r["id"] for r in out["records"]], ["req-1"])

    def test_unknown_fields_are_dropped_and_no_prompt_bodies(self):
        self.stub.routes[("GET", "/scheduler/history")] = (200, {
            "records": [{**_record(0), "prompt": "SECRET PROMPT BODY",
                         "messages": [{"role": "user", "content": "secret"}],
                         "novel_future_field": "x"}]})
        out = rv.history(self.env.cfg)
        text = repr(out)
        self.assertNotIn("SECRET PROMPT BODY", text)
        self.assertNotIn("messages", text)
        self.assertEqual(sorted(out["records"][0]), sorted(rv.RECORD_FIELDS))

    def test_honest_when_the_list_is_not_json(self):
        env = TempEnv(orchestrator_base="http://127.0.0.1:9", offline=False)
        self.addCleanup(env.cleanup)
        out = rv.history(env.cfg)
        self.assertFalse(out["available"])
        self.assertEqual(out["records"], [])


class TestCancelRetry(RequestsBase):
    def test_cancel_relays_with_the_id(self):
        out = rv.cancel(self.env.cfg, "req-0")
        self.assertEqual(out["state"], "cancelled")
        method, path, headers, body = self.stub.calls[-1]
        self.assertEqual((method, path, body), ("POST", "/scheduler/cancel", {"id": "req-0"}))

    def test_retry_relays(self):
        out = rv.retry(self.env.cfg, "req-2")
        self.assertEqual(out["state"], "queued")
        self.assertEqual(self.stub.calls[-1][1], "/scheduler/retry")

    def test_bad_ids_refused_before_any_request(self):
        for bad in ("", "abc; rm -rf /", "../../x", "x" * 200, "has space"):
            with self.assertRaises(rv.RequestsError):
                rv.cancel(self.env.cfg, bad)
            with self.assertRaises(rv.RequestsError):
                rv.retry(self.env.cfg, bad)
        self.assertEqual(len(self.stub.calls), 0)  # nothing reached the fake orchestrator

    def test_upstream_error_surfaced(self):
        self.stub.routes[("POST", "/scheduler/cancel")] = (409, {"error": "not queued"})
        with self.assertRaises(rv.RequestsError) as ctx:
            rv.cancel(self.env.cfg, "req-0")
        self.assertEqual(ctx.exception.status, 409)


if __name__ == "__main__":
    unittest.main()
