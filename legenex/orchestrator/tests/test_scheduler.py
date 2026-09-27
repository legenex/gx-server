"""Tests for the request-level admission scheduler (ARCHITECTURE-V41 §4).

Hermetic: no network, no docker, no real clock beyond injected short
timeouts. Every limit (global capacity, per-project caps, queue caps),
the priority order, the round-robin fairness, the persistence round-trip,
the timeout reaper, both cancel paths and the restart recovery are pinned.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gx_orchestrator import scheduler as S  # noqa: E402


class SchedCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.sched = self.make()

    def make(self, **kw):
        params = dict(
            queue_path=self.dir / "queue.json",
            history_path=self.dir / "history.jsonl",
            capacity=2,
            default_timeout=0.2,  # short: the reaper tests need it to fire
        )
        params.update(kw)
        sched = S.Scheduler(**params)
        self.addCleanup(sched.shutdown)
        return sched

    def submit(self, rid, project="p1", priority="normal-worker", **kw):
        return self.sched.submit({"id": rid, "project": project, "priority": priority, **kw})


class TestAdmission(SchedCase):
    def test_first_request_is_admitted_active(self):
        decision = self.submit("r1")
        self.assertEqual(decision["state"], S.DECISION_ACTIVE)
        self.assertEqual(self.sched.status()["active"], 1)

    def test_capacity_overflow_queues_with_position(self):
        self.submit("r1")
        self.submit("r2")
        decision = self.submit("r3")
        self.assertEqual(decision["state"], S.DECISION_QUEUED)
        self.assertEqual(decision["position"], 1)

    def test_set_capacity_promotes_the_queue(self):
        # Distinct projects: the per-project active cap (2) would otherwise
        # hold r3 back even with free global slots.
        self.submit("r1", project="pa")
        self.submit("r2", project="pb")
        self.assertEqual(self.submit("r3", project="pc")["state"], S.DECISION_QUEUED)
        self.sched.set_capacity(4)
        self.assertEqual(self.sched.status()["active"], 3)
        self.assertEqual(self.sched.status()["queued"], 0)

    def test_global_queued_cap_is_a_429_shaped_rejection_with_position(self):
        small = self.make(capacity=1, global_queued_cap=2)
        small.submit({"id": "a1"})
        self.assertEqual(small.submit({"id": "a2"})["state"], S.DECISION_QUEUED)
        self.assertEqual(small.submit({"id": "a3"})["state"], S.DECISION_QUEUED)
        decision = small.submit({"id": "a4"})
        self.assertEqual(decision["state"], S.DECISION_REJECTED)
        self.assertIn("global queue full", decision["reason"])
        self.assertEqual(decision["position"], 3)

    def test_per_project_queued_cap(self):
        small = self.make(capacity=1, per_project_queued_cap=2)
        small.submit({"id": "a1", "project": "p1"})
        small.submit({"id": "a2", "project": "p1"})
        small.submit({"id": "a3", "project": "p1"})
        decision = small.submit({"id": "a4", "project": "p1"})
        self.assertEqual(decision["state"], S.DECISION_REJECTED)
        self.assertIn("project 'p1' queue full", decision["reason"])
        # Another project is unaffected: the cap is per project.
        self.assertEqual(small.submit({"id": "b1", "project": "p2"})["state"], S.DECISION_QUEUED)

    def test_per_project_active_cap(self):
        # capacity 4, per-project active cap 2: a third request from p1 queues
        # even though global slots remain.
        big = self.make(capacity=4)
        self.assertEqual(big.submit({"id": "a1", "project": "p1"})["state"], S.DECISION_ACTIVE)
        self.assertEqual(big.submit({"id": "a2", "project": "p1"})["state"], S.DECISION_ACTIVE)
        self.assertEqual(big.submit({"id": "a3", "project": "p1"})["state"], S.DECISION_QUEUED)
        self.assertEqual(big.submit({"id": "b1", "project": "p2"})["state"], S.DECISION_ACTIVE)

    def test_duplicate_id_is_rejected(self):
        self.submit("r1")
        self.assertEqual(self.submit("r1")["state"], S.DECISION_REJECTED)

    def test_unknown_priority_falls_back_to_normal_worker(self):
        self.submit("r1", priority="made-up")
        rec = self.sched.get("r1")
        self.assertEqual(rec["priority"], "normal-worker")


class TestPriorities(SchedCase):
    def test_strict_priority_order_on_release(self):
        # Two slots held by placeholder records; three queued at different
        # priorities. Freed slots must go to the strict priority order.
        self.submit("hold1")
        self.submit("hold2")
        self.submit("low", priority="background")
        self.submit("normal", priority="normal-worker")
        self.submit("inter", priority="interactive")
        self.sched.record_finished("hold1", {})
        active = [r["id"] for r in self.sched.status()["records"] if r["state"] == "active"]
        self.assertIn("inter", active)
        self.assertNotIn("normal", active)
        self.assertNotIn("low", active)
        # Submit the orchestrator-priority request BEFORE freeing the next
        # slot, so the promotion order is observable.
        self.submit("orch", priority="orchestrator")
        self.sched.record_finished("inter", {})
        active = {r["id"] for r in self.sched.status()["records"] if r["state"] == "active"}
        self.assertIn("orch", active)
        self.assertNotIn("normal", active)
        self.assertNotIn("low", active)

    def test_round_robin_within_priority_across_projects(self):
        big = self.make(capacity=1, per_project_active_cap=1)
        # One active slot; two projects at the same priority with two records
        # each. Promotions must alternate projects, never drain one first.
        for rid, project in (("a1", "pa"), ("a2", "pa"), ("b1", "pb"), ("b2", "pb")):
            big.submit({"id": rid, "project": project, "priority": "normal-worker"})
        order = []
        while len(order) < 4:
            active = [r for r in big.status()["records"] if r["state"] == "active"]
            self.assertEqual(len(active), 1)
            order.append(active[0]["project"])
            big.record_finished(active[0]["id"], {})
        self.assertEqual(order, ["pa", "pb", "pa", "pb"])

    def test_higher_priority_submitted_later_is_served_first(self):
        one = self.make(capacity=1)
        one.submit({"id": "bg", "priority": "background"})
        one.record_finished("bg", {})
        one.submit({"id": "normal", "priority": "normal-worker"})
        # 'normal' holds the only slot; interactive queues and must be next.
        one.submit({"id": "top", "priority": "interactive"})
        one.record_finished("normal", {})
        active = [r["id"] for r in one.status()["records"] if r["state"] == "active"]
        self.assertEqual(active, ["top"])


class TestLifecycleOfRecords(SchedCase):
    def test_record_finished_stores_metrics_and_frees_the_slot(self):
        self.submit("r1")
        self.submit("r2")
        self.sched.record_finished("r1", {"prompt_tokens": 10, "completion_tokens": 5,
                                          "ttft_ms": 123.0, "tps": 20.0, "cached_tokens": 2})
        hist = self.sched.history()
        done = [h for h in hist if h["id"] == "r1"][0]
        self.assertEqual(done["state"], S.STATE_DONE)
        self.assertEqual(done["prompt_tokens"], 10)
        self.assertEqual(done["cached_tokens"], 2)
        self.assertEqual(done["ttft_ms"], 123.0)
        # r2 was promoted by the freed slot.
        self.assertEqual(self.sched.get("r2")["state"], S.STATE_ACTIVE)

    def test_record_error_finalises_and_promotes(self):
        self.submit("r1")
        self.submit("r2")
        self.sched.record_error("r1", "engine exploded")
        self.assertEqual(self.sched.get("r2")["state"], S.STATE_ACTIVE)
        entry = [h for h in self.sched.history() if h["id"] == "r1"][0]
        self.assertEqual(entry["state"], S.STATE_ERROR)
        self.assertIn("engine exploded", entry["error"])

    def test_cancel_queued_removes_immediately(self):
        self.submit("r1")
        self.submit("r2")
        self.assertEqual(self.submit("r3")["state"], S.DECISION_QUEUED)
        outcome = self.sched.cancel("r3", "operator asked")
        self.assertTrue(outcome["cancelled"])
        self.assertEqual(outcome["state"], S.STATE_CANCELLED)
        self.assertIsNone(self.sched.get("r3"))

    def test_cancel_active_marks_cancelling_and_finish_finalises_cancelled(self):
        self.submit("r1")
        outcome = self.sched.cancel("r1", "stop it")
        self.assertTrue(outcome["cancelled"])
        self.assertEqual(outcome["state"], S.STATE_CANCELLING)
        self.assertTrue(self.sched.is_cancelling("r1"))
        # The relaying thread finishes; the cancel wins the final state.
        self.sched.record_finished("r1", {"completion_tokens": 3})
        entry = [h for h in self.sched.history() if h["id"] == "r1"][0]
        self.assertEqual(entry["state"], S.STATE_CANCELLED)
        self.assertEqual(entry["completion_tokens"], 3)

    def test_retry_requeues_a_terminal_record(self):
        self.submit("r1")
        self.sched.record_error("r1", "transient")
        outcome = self.sched.retry("r1")
        self.assertIn(outcome.get("state"), (S.DECISION_ACTIVE, S.DECISION_QUEUED))
        rec = self.sched.get("r1")
        self.assertEqual(rec["state"], S.STATE_ACTIVE)
        self.assertEqual(rec["error"], "")
        self.assertIsNone(rec["done_ts"])
        self.assertIsNotNone(rec["start_ts"], "a retried record is re-admitted, not resurrected mid-flight")

    def test_retry_unknown_id_fails_clearly(self):
        outcome = self.sched.retry("ghost")
        self.assertFalse(outcome.get("retried", False))


class TestWait(SchedCase):
    def test_wait_returns_active_when_promoted(self):
        self.sched.set_capacity(1)
        self.submit("r1")
        result = {}

        def waiter():
            result["state"] = self.sched.wait("r2", timeout=10)

        self.submit("r2")  # queued behind r1
        t = threading.Thread(target=waiter)
        t.start()
        time.sleep(0.1)
        self.sched.record_finished("r1", {})
        t.join(10)
        self.assertEqual(result["state"], S.STATE_ACTIVE)

    def test_wait_wakes_on_cancel(self):
        self.sched.set_capacity(1)
        self.submit("r1")
        self.submit("r2")
        result = {}

        def waiter():
            result["state"] = self.sched.wait("r2", timeout=10)

        t = threading.Thread(target=waiter)
        t.start()
        time.sleep(0.1)
        self.sched.cancel("r2", "gone")
        t.join(10)
        self.assertEqual(result["state"], S.STATE_CANCELLED)

    def test_wait_unknown_id_does_not_hang(self):
        self.assertEqual(self.sched.wait("ghost", timeout=0.5), S.STATE_ERROR)

    def test_drain_waits_for_active_to_finish(self):
        self.submit("r1")
        self.submit("r2")  # both active at capacity 2

        def finisher():
            time.sleep(0.2)
            self.sched.record_finished("r1", {})
            self.sched.record_finished("r2", {})

        threading.Thread(target=finisher, daemon=True).start()
        remaining = self.sched.drain(timeout=5)
        self.assertEqual(remaining, 0)


class TestTimeoutReaper(SchedCase):
    def test_queued_request_expires(self):
        one = self.make(capacity=1)
        one.submit({"id": "r1"})
        one.submit({"id": "r2"})  # queued; default_timeout 0.2s
        time.sleep(1.5)
        entry = [h for h in one.history() if h["id"] == "r2"]
        self.assertTrue(entry, "queued request was not reaped")
        self.assertEqual(entry[0]["state"], S.STATE_TIMEOUT)
        self.assertIn("soft timeout", entry[0]["error"])

    def test_active_request_expires_and_frees_the_slot(self):
        self.submit("r1")
        self.submit("r2")
        time.sleep(1.5)
        states = {h["id"]: h["state"] for h in self.sched.history()}
        self.assertEqual(states.get("r1"), S.STATE_TIMEOUT)
        # Every record eventually expired; nothing is stuck active.
        self.assertEqual(self.sched.status()["active"], 0)

    def test_per_request_timeout_overrides_the_default(self):
        patient = self.make(capacity=1, default_timeout=0.2)
        patient.submit({"id": "r1", "timeout": 30})
        time.sleep(1.0)
        self.assertEqual(patient.get("r1")["state"], S.STATE_ACTIVE)


class TestPersistence(SchedCase):
    def test_queue_survives_a_restart(self):
        one = self.make(capacity=1)
        one.submit({"id": "r1", "project": "p1"})
        one.submit({"id": "r2", "project": "p1"})
        one.shutdown()
        # A NEW scheduler instance over the same files = a control-plane restart.
        two = self.make(capacity=1)
        snap = two.status()
        self.assertEqual(snap["queued"], 1)
        self.assertEqual(snap["active"], 0)  # see the restart test below

    def test_active_at_restore_is_error_control_plane_restart(self):
        one = self.make(capacity=1)
        one.submit({"id": "r1"})
        one.shutdown()
        two = self.make(capacity=1)
        # r1 was ACTIVE when the old process died; it must be marked error,
        # never silently resumed as active.
        self.assertIsNone(two.get("r1"))
        entry = [h for h in two.history() if h["id"] == "r1"]
        self.assertTrue(entry)
        self.assertEqual(entry[0]["state"], S.STATE_ERROR)
        self.assertEqual(entry[0]["error"], "control-plane restart")

    def test_every_mutation_persists_atomically(self):
        self.submit("r1")
        raw = json.loads((self.dir / "queue.json").read_text())
        self.assertEqual(len(raw["records"]), 1)
        self.assertFalse((self.dir / "queue.tmp").exists(), "the tmp file must be renamed away")
        self.submit("r2")
        raw = json.loads((self.dir / "queue.json").read_text())
        self.assertEqual(len(raw["records"]), 2)

    def test_history_jsonl_ring_buffer(self):
        small = self.make(capacity=1, history_cap=5)
        for i in range(8):
            small.submit({"id": f"r{i}"})
            small.record_finished(f"r{i}", {})
        lines = (self.dir / "history.jsonl").read_text().splitlines()
        # The FILE is a ring too: compacted back to the cap.
        self.assertLessEqual(len(lines), 5)
        self.assertLessEqual(len(small.history(limit=100)), 5)
        # A restart re-seeds the in-memory ring from the durable JSONL.
        reborn = self.make(capacity=1, history_cap=5)
        self.assertEqual(len(reborn.history(limit=100)), len(lines))

    def test_status_shape_for_the_dashboard(self):
        self.submit("r1", project="proj")
        self.submit("r2", project="proj")
        snap = self.sched.status()
        self.assertEqual(snap["capacity"], 2)
        self.assertEqual(snap["active"], 2)
        self.assertEqual(snap["queued"], 0)
        self.assertEqual(snap["projects"]["proj"], {"active": 2, "queued": 0})
        self.assertIn("oldest_wait_seconds", snap)
        self.assertIn("limits", snap)
        for rec in snap["records"]:
            self.assertIn(rec["state"], ("queued", "active", "cancelling"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
