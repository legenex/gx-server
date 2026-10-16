"""D-053: five logical modes, identity priority, the health lane, typed model
switching (state machine, auto-rollback, window, lock, history) and the
fixed-shape / rank-watch / circuit-breaker lifecycle additions."""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from gx_orchestrator import modes as M  # noqa: E402
from gx_orchestrator.deployments import (  # noqa: E402
    ActiveDeploymentStore, DeploymentManager, LifecycleProxy, fixed_profile_for,
)
from gx_orchestrator.identity import identity_from_headers, load_policy, parse_policy, sign_identity  # noqa: E402
from gx_orchestrator.lifecycle import (  # noqa: E402
    AcquisitionError, FAILURE_RANK, GxMaxLifecycle, LifecycleStatus, State,
)
from gx_orchestrator.profiles import DeploymentSpec, load_registry, parse_registry  # noqa: E402
from gx_orchestrator.scheduler import Scheduler  # noqa: E402
from gx_orchestrator.switching import (  # noqa: E402
    ABORTED, Check, INTERRUPTED, ModelSwitcher, ROLLBACK_FAILED, ROLLED_BACK, SUCCEEDED,
    SwitchError, SwitchHistory, SwitchLock, in_maintenance_window,
)
from registry_fixtures import fixture_registry_dict  # noqa: E402
from server_harness import API_KEY, IDENTITY_KEY, TRUSTED, FakeLifecycle, OrchestratorHarness, signed_identity  # noqa: E402

REPO_REGISTRY = Path(__file__).resolve().parents[3] / "legenex" / "models" / "registry.json"
IN_WINDOW = dt.datetime(2026, 10, 16, 0, 30, tzinfo=dt.timezone.utc)      # 02:30 SAST
OUT_WINDOW = dt.datetime(2026, 10, 16, 10, 0, tzinfo=dt.timezone.utc)     # 12:00 SAST


def prod_registry():
    return load_registry(REPO_REGISTRY)


def chat(text, **extra):
    return {"model": "gx-fast", "messages": [{"role": "user", "content": text}], **extra}


# --------------------------------------------------------------------- modes
class TestModes(unittest.TestCase):
    def setUp(self):
        self.reg = prod_registry()

    def test_fixed_aliases(self):
        for alias, mode in (("gx-fast", "fast"), ("gx-medium", "medium"), ("gx-high", "high"), ("gx-max", "max")):
            self.assertEqual(M.choose_mode(alias, chat("hi"), self.reg).mode, mode)

    def test_v4_dialect_kwargs(self):
        self.assertEqual(M.reasoning_kwargs("deepseek_v4", "none", self.reg), {"thinking": False})
        self.assertEqual(M.reasoning_kwargs("deepseek_v4", "low", self.reg),
                         {"thinking": True, "reasoning_effort": "low"})
        self.assertEqual(M.reasoning_kwargs("deepseek_v4", "high", self.reg),
                         {"thinking": True, "reasoning_effort": "high"})
        self.assertEqual(M.reasoning_kwargs("deepseek_v4", "max", self.reg),
                         {"thinking": True, "reasoning_effort": "max"})
        # V4.1 rollback keeps the registry mapping.
        self.assertEqual(M.reasoning_kwargs("deepseek_v41", "none", self.reg), {"enable_thinking": False})
        self.assertEqual(M.reasoning_kwargs("deepseek_v41", "max", self.reg), {"reasoning_effort": 100})

    def test_client_cannot_exceed_its_mode(self):
        spec = self.reg.mode("fast")
        payload = chat("x", reasoning_effort="max", chat_template_kwargs={"thinking": True, "reasoning_effort": "max"},
                       thinking=True, max_tokens=100_000)
        body, removed = M.shape_request(payload, spec, {"thinking": False})
        self.assertEqual(body["chat_template_kwargs"], {"thinking": False})
        self.assertNotIn("reasoning_effort", body)
        self.assertNotIn("thinking", body)
        self.assertEqual(body["max_tokens"], spec.max_output_tokens)
        self.assertIn("chat_template_kwargs", removed)

    def test_fast_admission_is_64k(self):
        # Owner Decision #10: Hermes Agent needs a >= 64,000-token window on gx-fast.
        spec = self.reg.mode("fast")
        self.assertEqual(spec.max_context_tokens, 73728)
        self.assertNotEqual(M.mode_budget(chat("word " * 60_000), spec, "gx-fast").status, "overflow")
        big = chat("word " * 90_000)
        self.assertEqual(M.mode_budget(big, spec, "gx-fast").status, "overflow")
        self.assertNotEqual(M.mode_budget(chat("hello"), spec, "gx-fast").status, "overflow")
        self.assertNotEqual(M.mode_budget(big, self.reg.mode("medium"), "gx-medium").status, "overflow")

    def test_auto_picks_by_intent_and_escalates_on_context(self):
        self.assertEqual(M.choose_mode("gx-auto", chat("hi"), self.reg).mode, "fast")
        self.assertEqual(M.choose_mode("gx-auto", chat("Fix the failing unit tests in parser.py"), self.reg).mode,
                         "medium")
        self.assertEqual(M.choose_mode("gx-auto", chat("Design the architecture for a sharded queue, trade-offs"),
                                       self.reg).mode, "high")
        hard = chat("Debug this intermittent race condition deadlock in the scheduler, it is flaky")
        self.assertEqual(M.choose_mode("gx-auto", hard, self.reg).mode, "max")
        # A plain question that needs > 72K of context cannot stay FAST.
        long_q = chat("summarise: " + "lorem ipsum " * 30_000)
        d = M.choose_mode("gx-auto", long_q, self.reg)
        self.assertIn(d.mode, ("medium", "high"))
        self.assertIn("escalated", d.reason)

    def test_auto_ignores_profile_and_reasoning_override_headers(self):
        d = M.choose_mode("gx-auto", chat("hi"), self.reg,
                          headers={"X-GX-Profile": "deep", "X-GX-Reasoning": "max"})
        self.assertEqual(d.mode, "fast")


# ------------------------------------------------------------------ identity
class TestIdentity(unittest.TestCase):
    def setUp(self):
        self.p = load_policy()  # the shipped identity-policy.json

    def test_shipped_policy(self):
        # Owner policy (mission §9): everyone gets the MODE default; only odin is pinned.
        for who in ("", "open-webui", "gx-computer", "team-librechat-kvm2", "hermes", "kilo-code"):
            self.assertEqual(self.p.resolve(who, "normal-worker")[0], "normal-worker", who)
            self.assertEqual(self.p.resolve(who, "interactive")[0], "interactive", who)
        self.assertEqual(self.p.resolve("odin", "interactive")[0], "health")
        # Raising to interactive: only the permitted identities.
        for who, ok in (("ai-os", True), ("agentos", True), ("gx-operator", True), ("hermes", False),
                        ("open-webui", False), ("team-librechat-kvm2", False), ("", False)):
            self.assertEqual(self.p.resolve(who, "normal-worker", "interactive")[0],
                             "interactive" if ok else "normal-worker", who)

    def test_raising_needs_permission_lowering_never_does(self):
        prio, why = self.p.resolve("hermes", "normal-worker", "interactive")
        self.assertEqual(prio, "normal-worker")
        self.assertIn("ignored", why)
        self.assertEqual(self.p.resolve("ai-os", "normal-worker", "interactive")[0], "interactive")  # may wake it
        self.assertEqual(self.p.resolve("gx-operator", "normal-worker", "health")[0], "normal-worker")
        # Review L2: a header that LOWERS priority is honoured for everyone.
        for who in ("", "hermes", "open-webui", "random"):
            self.assertEqual(self.p.resolve(who, "interactive", "background")[0], "background", who)

    def test_v41_uses_the_same_raise_lower_rule(self):
        # Review of c9c9ac0: with headers live, an anonymous key holder must not
        # wake both nodes by header on V4.1 either.
        self.assertEqual(self.p.resolve("", "normal-worker", "interactive", legacy=True)[0], "normal-worker")
        self.assertEqual(self.p.resolve("ai-os", "normal-worker", "interactive", legacy=True)[0], "interactive")
        self.assertEqual(self.p.resolve("hermes", "orchestrator", "background", legacy=True)[0], "background")
        self.assertEqual(self.p.resolve("", "orchestrator", "health", legacy=True)[0], "orchestrator")
        self.assertEqual(self.p.resolve("odin", "orchestrator", "", legacy=True)[0], "health")

    def test_reasoning_lower_always_raise_only_if_permitted(self):
        from gx_orchestrator.identity import reasoning_ranker
        rank = reasoning_ranker(prod_registry())
        self.assertEqual(self.p.resolve_reasoning("", "high", "low", rank)[0], "low")
        self.assertEqual(self.p.resolve_reasoning("", "high", "none", rank)[0], "none")
        self.assertEqual(self.p.resolve_reasoning("", "none", "max", rank)[0], "none")
        self.assertEqual(self.p.resolve_reasoning("hermes", "medium", "max", rank)[0], "medium")
        self.assertEqual(self.p.resolve_reasoning("ai-os", "medium", "max", rank)[0], "medium")
        self.assertEqual(self.p.resolve_reasoning("gx-operator", "medium", "max", rank)[0], "max")
        self.assertEqual(self.p.resolve_reasoning("", "medium", "40", rank)[0], "40")      # numeric, lower
        self.assertEqual(self.p.resolve_reasoning("", "medium", "90", rank)[0], "medium")  # numeric, higher
        self.assertEqual(self.p.resolve_reasoning("", "medium", "ultra", rank)[0], "ultra")  # -> caller 400

    def test_nobody_reaches_the_health_lane_by_header(self):
        for who in ("", "unknown", "ai-os", "gx-operator"):
            self.assertNotEqual(self.p.resolve(who, "interactive", "health")[0], "health")

    def test_policy_validation(self):
        with self.assertRaises(ValueError):
            parse_policy({"identities": {"x": {"max_priority": "health"}}})
        with self.assertRaises(ValueError):
            parse_policy({"identities": {"x": {"priority": "vip"}}})

    def test_identity_must_be_signed_by_the_gateway(self):
        key, now = IDENTITY_KEY, 1_800_000_000
        good = signed_identity("odin", key, now)
        self.assertEqual(identity_from_headers(good, key, now=now), "odin")
        # An orchestrator-key holder cannot claim odin without the gateway key.
        self.assertEqual(identity_from_headers({"X-GX-Identity": "odin"}, key, now=now), "")
        self.assertEqual(identity_from_headers(signed_identity("odin", "another-key-xxxxxxxxxx", now), key, now=now), "")
        self.assertEqual(identity_from_headers(dict(good, **{"X-GX-Identity": "open-webui"}), key, now=now), "")
        self.assertEqual(identity_from_headers(good, key, now=now + 301), "")   # stale -> replay bounded
        self.assertEqual(identity_from_headers(good, None, now=now), "")        # no key -> fail closed
        bad = signed_identity("bad name; rm", key, now)
        self.assertEqual(identity_from_headers(bad, key, now=now), "")
        # Single use: the same signed headers are refused the second time.
        from gx_orchestrator.identity import NonceCache
        seen = NonceCache()
        once = signed_identity("odin", key, now)
        self.assertEqual(identity_from_headers(once, key, now=now, nonces=seen), "odin")
        self.assertEqual(identity_from_headers(once, key, now=now + 1, nonces=seen), "")
        self.assertEqual(identity_from_headers(signed_identity("odin", key, now), key, now=now, nonces=seen), "odin")
        no_nonce = {k: v for k, v in once.items() if k != "X-GX-Identity-Nonce"}
        self.assertEqual(identity_from_headers(no_nonce, key, now=now), "")


# ----------------------------------------------------------------- scheduler
class TestHealthLaneAndModeCaps(unittest.TestCase):
    def make(self, **kw):
        d = tempfile.mkdtemp()
        s = Scheduler(queue_path=Path(d) / "q.json", history_path=Path(d) / "h.jsonl", **kw)
        self.addCleanup(s.shutdown)
        return s

    def test_one_slot_is_reserved_for_health_on_wide_engines(self):
        s = self.make(capacity=8)
        for i in range(10):
            s.submit({"id": f"w{i}", "project": f"p{i}", "priority": "normal-worker"})
        self.assertEqual(s.status()["active"], 7)
        self.assertEqual(s.submit({"id": "odin", "project": "odin", "priority": "health"})["state"], "active")
        self.assertEqual(s.status()["reserved_health_slots"], 1)

    def test_no_reserve_on_narrow_engines(self):
        s = self.make(capacity=2)
        s.submit({"id": "a", "project": "a", "priority": "normal-worker"})
        self.assertEqual(s.submit({"id": "b", "project": "b", "priority": "normal-worker"})["state"], "active")

    def test_health_is_not_starved_by_a_full_queue(self):
        s = self.make(capacity=2, global_queued_cap=2)
        for i in range(4):
            s.submit({"id": f"w{i}", "project": f"p{i}", "priority": "interactive"})
        d = s.submit({"id": "odin", "project": "odin", "priority": "health"})
        self.assertEqual(d["state"], "queued")
        s.record_finished("w0")
        self.assertEqual(s.get("odin")["state"], "active")   # health goes before interactive

    def test_max_mode_concurrency_cap(self):
        s = self.make(capacity=8, mode_caps={"max": 2})
        for i in range(3):
            s.submit({"id": f"m{i}", "project": f"p{i}", "priority": "normal-worker", "mode": "max"})
        self.assertEqual(s.get("m2")["state"], "queued")
        self.assertEqual(s.submit({"id": "f", "project": "f", "priority": "interactive", "mode": "fast"})["state"],
                         "active")
        s.record_finished("m0")
        self.assertEqual(s.get("m2")["state"], "active")

    def test_pause_refuses_but_lets_validation_through(self):
        s = self.make(capacity=2)
        s.pause("switch in progress")
        d = s.submit({"id": "x", "project": "x"})
        self.assertEqual(d["state"], "rejected")
        self.assertTrue(d["paused"])
        self.assertEqual(s.submit({"id": "v", "project": "v", "bypass_pause": True})["state"], "active")
        s.resume()
        self.assertEqual(s.submit({"id": "y", "project": "y"})["state"], "active")


# ------------------------------------------------------------------ switching
class FakeLC:
    def __init__(self, name, start_ok=True, ready=False):
        self.name = name
        self.state = State.READY if ready else State.DOWN
        self.start_ok = start_ok
        self.released = 0
        self.acquired = 0
        self.shut = False

    def status(self):
        return LifecycleStatus(state=self.state, since=0, last_used=None, waiters=0)

    def release(self, *, force=False):
        self.released += 1
        self.state = State.DOWN

    def acquire(self, profile=None, timeout=None, *, bypass_backoff=False):
        self.acquired += 1
        if not self.start_ok:
            raise AcquisitionError(f"{self.name} failed to start")
        self.state = State.READY
        return "x"

    def shutdown(self):
        self.shut = True

    def latch(self, reason):
        self.latched = reason

    def unlatch(self):
        self.latched = ""


class FakeScheduler:
    def __init__(self, active=0, drain_left=0):
        self.active = active
        self.drain_left = drain_left
        self.paused_reason = ""
        self.pause_log = []

    @property
    def paused(self):
        return self.paused_reason

    def status(self):
        return {"active": self.active, "queued": 0}

    def pause(self, reason):
        self.paused_reason = reason
        self.pause_log.append("pause")

    def resume(self):
        self.paused_reason = ""
        self.pause_log.append("resume")

    def drain(self, timeout):
        return self.drain_left


class SwitchTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.reg = prod_registry()
        self.lcs = {}
        self.start_ok = {"dsv4-flash-primary": True, "dsv41-rollback": True}
        self.valid = {"dsv4-flash-primary": True, "dsv41-rollback": True}
        self.preflight_ok = True
        self.validated = []

        def factory(dep):
            lc = FakeLC(dep.name, start_ok=self.start_ok[dep.name], ready=not self.lcs)
            self.lcs.setdefault(dep.name, []).append(lc)
            return lc
        self.store = ActiveDeploymentStore(self.dir / "active.json")
        self.mgr = DeploymentManager(self.reg, self.store, factory)
        self.sched = FakeScheduler()
        self.hist = SwitchHistory(self.dir / "history.jsonl")
        self.lock = SwitchLock(self.dir / "switch.lock")

    def validator(self, name, token):
        self.validated.append((name, token))
        return [Check("completion", self.valid[name], "fake")]

    def preflight(self, name, token):
        return [Check("memory_node1", self.preflight_ok, "fake")]

    def switcher(self, clock=IN_WINDOW, **kw):
        return ModelSwitcher(manager=self.mgr, scheduler=self.sched, history=self.hist, lock=self.lock,
                             validator=self.validator, preflight=self.preflight,
                             clock=lambda: clock, run_async=False, **kw)


class TestSwitchStateMachine(SwitchTestBase):
    def test_initial_active_is_the_rollback_until_a_switch(self):
        self.assertEqual(self.mgr.active_name, "dsv41-rollback")

    def test_successful_switch(self):
        rec = self.switcher().request({"target": "dsv4-flash-primary", "confirm": True}, actor="op")
        last = self.hist.read()[-1]
        self.assertEqual(last["state"], SUCCEEDED, last)
        self.assertEqual(self.mgr.active_name, "dsv4-flash-primary")
        self.assertEqual(self.store.read()["deployment"], "dsv4-flash-primary")
        self.assertEqual(self.lcs["dsv41-rollback"][0].released, 1)
        self.assertTrue(last["previous_healthy"])
        self.assertEqual([s["step"] for s in last["steps"]],
                         ["window", "lock", "in_flight", "drain", "previous", "stop_previous", "preflight",
                          "start_target", "validate"])
        self.assertEqual(self.sched.pause_log, ["pause", "resume"])
        self.assertTrue(self.validated[0][1], "validator got a pause-bypass token")
        self.assertTrue(self.lcs["dsv41-rollback"][0].latched, "outgoing deployment latched before release")
        self.assertEqual(rec["id"], last["id"])

    def test_validation_failure_rolls_back(self):
        self.valid["dsv4-flash-primary"] = False
        self.switcher().request({"target": "dsv4-flash-primary", "confirm": True}, actor="op")
        last = self.hist.read()[-1]
        self.assertEqual(last["state"], ROLLED_BACK)
        self.assertEqual(self.mgr.active_name, "dsv41-rollback")
        self.assertEqual(self.store.read()["deployment"], "dsv41-rollback")
        self.assertEqual(self.lcs["dsv4-flash-primary"][0].released, 1)   # target torn down
        self.assertEqual(self.lcs["dsv41-rollback"][-1].state, State.READY)
        self.assertIn("validation failed", last["error"])

    def test_start_failure_rolls_back(self):
        self.start_ok["dsv4-flash-primary"] = False
        self.switcher().request({"target": "dsv4-flash-primary", "confirm": True}, actor="op")
        self.assertEqual(self.hist.read()[-1]["state"], ROLLED_BACK)
        self.assertEqual(self.mgr.active_name, "dsv41-rollback")

    def test_preflight_failure_restarts_previous_without_starting_target(self):
        self.preflight_ok = False
        self.switcher().request({"target": "dsv4-flash-primary", "confirm": True}, actor="op")
        self.assertEqual(self.hist.read()[-1]["state"], ROLLED_BACK)
        self.assertNotIn("dsv4-flash-primary", self.lcs)

    def test_rollback_failure_is_recorded(self):
        self.valid["dsv4-flash-primary"] = False
        self.valid["dsv41-rollback"] = False
        self.switcher().request({"target": "dsv4-flash-primary", "confirm": True}, actor="op")
        self.assertEqual(self.hist.read()[-1]["state"], ROLLBACK_FAILED)

    def test_in_flight_that_will_not_drain_aborts_before_stopping_anything(self):
        self.sched.drain_left = 2
        self.switcher().request({"target": "dsv4-flash-primary", "confirm": True}, actor="op")
        last = self.hist.read()[-1]
        self.assertEqual(last["state"], ABORTED)
        self.assertEqual(self.lcs["dsv41-rollback"][0].released, 0)
        self.assertEqual(self.mgr.active_name, "dsv41-rollback")
        self.assertEqual(self.sched.pause_log, ["pause", "resume"])

    def test_cut_in_flight_proceeds_and_reactivate_restarts_the_active(self):
        self.sched.drain_left = 2
        self.switcher().request({"target": "dsv4-flash-primary", "confirm": True, "cut_in_flight": True}, actor="op")
        self.assertEqual(self.hist.read()[-1]["state"], SUCCEEDED)
        self.sched.drain_left = 0
        self.switcher().request({"target": "dsv4-flash-primary", "confirm": True, "reactivate": True}, actor="op")
        self.assertEqual(self.hist.read()[-1]["state"], SUCCEEDED)
        self.assertEqual(len(self.lcs["dsv4-flash-primary"]), 2)

    def test_lock_is_released_when_the_switch_cannot_start(self):
        class Broken(SwitchHistory):
            def append(self, record):
                raise OSError("disk full")
        sw = ModelSwitcher(manager=self.mgr, scheduler=self.sched, history=Broken(self.dir / "h2.jsonl"),
                           lock=self.lock, validator=self.validator, preflight=self.preflight,
                           clock=lambda: IN_WINDOW, run_async=False)
        with self.assertRaises(SwitchError) as c:
            sw.request({"target": "dsv4-flash-primary", "confirm": True}, actor="op")
        self.assertEqual(c.exception.code, "switch_start_failed")
        probe = SwitchLock(self.lock.path)
        self.assertTrue(probe.try_acquire("after"))
        probe.release()

    def test_preflight_failure_unlatches_and_restarts_the_previous(self):
        self.preflight_ok = False
        self.switcher().request({"target": "dsv4-flash-primary", "confirm": True}, actor="op")
        prev = self.lcs["dsv41-rollback"][0]
        self.assertEqual(prev.latched, "")
        self.assertEqual(prev.state, State.READY)

    def test_request_validation(self):
        sw = self.switcher()
        with self.assertRaises(SwitchError) as c:
            sw.request({"target": "dsv4-flash-primary"}, actor="op")
        self.assertEqual(c.exception.code, "confirm_required")
        with self.assertRaises(SwitchError) as c:
            sw.request({"target": "nope", "confirm": True}, actor="op")
        self.assertEqual(c.exception.status, 400)
        with self.assertRaises(SwitchError) as c:
            sw.request({"target": "dsv41-rollback", "confirm": True}, actor="op")
        self.assertEqual(c.exception.code, "already_active")


class TestWindow(SwitchTestBase):
    def test_window_bounds_sast(self):
        utc = dt.timezone.utc
        self.assertFalse(in_maintenance_window(dt.datetime(2026, 10, 15, 22, 59, tzinfo=utc)))  # 00:59
        self.assertTrue(in_maintenance_window(dt.datetime(2026, 10, 15, 23, 0, tzinfo=utc)))    # 01:00
        self.assertTrue(in_maintenance_window(dt.datetime(2026, 10, 16, 2, 59, tzinfo=utc)))    # 04:59
        self.assertFalse(in_maintenance_window(dt.datetime(2026, 10, 16, 3, 0, tzinfo=utc)))    # 05:00

    def test_outside_window_refused(self):
        with self.assertRaises(SwitchError) as c:
            self.switcher(clock=OUT_WINDOW).request({"target": "dsv4-flash-primary", "confirm": True}, actor="op")
        self.assertEqual(c.exception.status, 423)
        self.assertEqual(self.hist.read(), [])

    def test_window_override_needs_a_reason_and_is_audited(self):
        sw = self.switcher(clock=OUT_WINDOW)
        with self.assertRaises(SwitchError) as c:
            sw.request({"target": "dsv4-flash-primary", "confirm": True, "override_window": True}, actor="op")
        self.assertEqual(c.exception.code, "reason_required")
        with self.assertRaises(SwitchError) as c:   # cut_in_flight is NOT a window override (A7)
            sw.request({"target": "dsv4-flash-primary", "confirm": True, "cut_in_flight": True}, actor="op")
        self.assertEqual(c.exception.code, "outside_window")
        sw.request({"target": "dsv4-flash-primary", "confirm": True, "override_window": True,
                    "emergency_reason": "V4.1 down, owner approved"}, actor="nick (token) via 1.2.3.4")
        last = self.hist.read()[-1]
        self.assertTrue(last["forced"])
        self.assertEqual(last["flags"], {"override_window": True, "cut_in_flight": False, "reactivate": False})
        self.assertEqual(last["emergency_reason"], "V4.1 down, owner approved")
        self.assertIn("OVERRIDDEN by nick (token) via 1.2.3.4", last["steps"][0]["detail"])

    def test_legacy_force_flag_is_refused(self):
        with self.assertRaises(SwitchError) as c:
            self.switcher().request({"target": "dsv4-flash-primary", "confirm": True, "force": True}, actor="op")
        self.assertEqual(c.exception.code, "force_removed")


class TestLockAndHistory(SwitchTestBase):
    def test_lock_contention_in_process(self):
        other = SwitchLock(self.lock.path)
        self.assertTrue(other.try_acquire("someone else"))
        try:
            with self.assertRaises(SwitchError) as c:
                self.switcher().request({"target": "dsv4-flash-primary", "confirm": True}, actor="op")
            self.assertEqual(c.exception.code, "switch_in_progress")
            self.assertIn("someone else", str(c.exception))
        finally:
            other.release()
        self.switcher().request({"target": "dsv4-flash-primary", "confirm": True}, actor="op")
        self.assertEqual(self.hist.read()[-1]["state"], SUCCEEDED)

    def test_lock_contention_across_processes(self):
        code = ("import fcntl,sys,time;f=open(sys.argv[1],'a+');fcntl.flock(f,fcntl.LOCK_EX);"
                "print('held',flush=True);time.sleep(30)")
        proc = subprocess.Popen([sys.executable, "-c", code, str(self.lock.path)], stdout=subprocess.PIPE, text=True)
        self.addCleanup(proc.kill)
        self.assertEqual(proc.stdout.readline().strip(), "held")
        self.assertFalse(SwitchLock(self.lock.path).try_acquire("me"))
        proc.stdout.close()
        proc.kill()
        proc.wait()
        mine = SwitchLock(self.lock.path)
        self.assertTrue(mine.try_acquire("me"))   # a dead holder frees it
        mine.release()

    def test_history_persists_and_interrupted_switches_are_marked(self):
        self.switcher().request({"target": "dsv4-flash-primary", "confirm": True}, actor="op")
        h2 = SwitchHistory(self.hist.path)
        self.assertEqual(h2.read()[-1]["state"], SUCCEEDED)
        h2.append({"id": "dead1", "state": "running", "target": "dsv41-rollback"})
        self.assertEqual(len(h2.mark_interrupted()), 1)
        self.assertEqual(h2.read()[-1]["state"], INTERRUPTED)
        self.assertEqual(h2.mark_interrupted(), [])

    def test_restart_keeps_the_switched_deployment(self):
        self.switcher().request({"target": "dsv4-flash-primary", "confirm": True}, actor="op")
        mgr2 = DeploymentManager(self.reg, ActiveDeploymentStore(self.dir / "active.json"),
                                 lambda dep: FakeLC(dep.name))
        self.assertEqual(mgr2.active_name, "dsv4-flash-primary")


# ------------------------------------------------------------ server (modes)
class FakeManager:
    def __init__(self, registry, name, lifecycle):
        self._registry = registry
        self._name = name
        self.lifecycle = lifecycle

    @property
    def active_name(self):
        return self._name

    @property
    def active(self):
        return self._registry.deployment(self._name)


class TestServerModes(OrchestratorHarness):
    """The handler with a mode-routing (V4 Flash) deployment active."""

    def setUp(self):
        super().setUp()
        reg = prod_registry()
        rt = reg.runtimes["dsv4-flash-tp2"]
        runtimes = dict(reg.runtimes)
        runtimes["dsv4-flash-tp2"] = replace(rt, api=self.upstream.base)
        self.reg = replace(reg, runtimes=runtimes)
        self.upstream.model_id = rt.served_model_id
        self.mgr = FakeManager(self.reg, "dsv4-flash-primary", self.lifecycle)
        self._handler_cls.registry = self.reg
        self._handler_cls.manager = self.mgr
        os.environ["GX_OPERATOR_TOKEN"] = "operator-token-for-tests-123"
        self.addCleanup(os.environ.pop, "GX_OPERATOR_TOKEN", None)

    def kwargs_sent(self):
        return self.upstream.last_request()["chat_template_kwargs"]

    def test_each_alias_injects_its_mode(self):
        for alias, kw in (("gx-fast", {"thinking": False}),
                          ("gx-medium", {"thinking": True, "reasoning_effort": "low"}),
                          ("gx-high", {"thinking": True, "reasoning_effort": "high"}),
                          ("gx-max", {"thinking": True, "reasoning_effort": "max"})):
            status, headers, _ = self.chat({"model": alias, "messages": [{"role": "user", "content": "hi"}],
                                            "chat_template_kwargs": {"thinking": True, "reasoning_effort": "max"},
                                            "reasoning_effort": "max"})
            self.assertEqual(status, 200, alias)
            self.assertEqual(self.kwargs_sent(), kw, alias)
            self.assertNotIn("reasoning_effort", self.upstream.last_request())
            self.assertEqual(self.upstream.last_request()["model"], "DeepSeek-V4-Flash-0731-abliterated")
            self.assertEqual(headers.get("X-GX-Deployment"), "dsv4-flash-primary")
            self.assertEqual(headers.get("X-GX-Mode"), alias.removeprefix("gx-"))

    def test_fast_rejects_context_beyond_64k_and_bounds_output(self):
        status, _, body = self.chat({"model": "gx-fast", "messages": [{"role": "user", "content": "word " * 90_000}]})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["mode"], "fast")
        self.assertEqual(len(self.upstream.requests), 0)
        status, _, _ = self.chat({"model": "gx-fast", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 200)
        self.assertLessEqual(self.upstream.last_request()["max_tokens"], 8192)

    def test_priority_from_signed_identity_and_header_rules(self):
        msg = [{"role": "user", "content": "hi"}]
        # Lowering by header is honoured (bench scripts send background).
        _, headers, _ = self.chat({"model": "gx-fast", "messages": msg}, headers={"X-GX-Priority": "background"})
        self.assertEqual(headers.get("X-GX-Priority"), "background")
        # Raising is not, for an anonymous caller.
        _, headers, _ = self.chat({"model": "gx-high", "messages": msg}, headers={"X-GX-Priority": "interactive"})
        self.assertEqual(headers.get("X-GX-Priority"), "normal-worker")
        _, headers, _ = self.chat({"model": "gx-high", "messages": msg}, headers=signed_identity("odin"))
        self.assertEqual(headers.get("X-GX-Priority"), "health")
        # An unsigned odin claim (orchestrator-key holder) gets nothing.
        _, headers, _ = self.chat({"model": "gx-high", "messages": msg}, headers={"X-GX-Identity": "odin"})
        self.assertEqual(headers.get("X-GX-Priority"), "normal-worker")
        _, headers, _ = self.chat({"model": "gx-high", "messages": msg},
                                  headers={"X-GX-Priority": "interactive", **signed_identity(TRUSTED)})
        self.assertEqual(headers.get("X-GX-Priority"), "interactive")       # signed + permitted
        _, headers, _ = self.chat({"model": "gx-high", "messages": msg},
                                  headers={"X-GX-Priority": "interactive", "X-GX-Identity": TRUSTED})
        self.assertEqual(headers.get("X-GX-Priority"), "normal-worker")     # unsigned claim
        # A replayed signed identity is anonymous the second time.
        replay = signed_identity("odin")
        _, headers, _ = self.chat({"model": "gx-high", "messages": msg}, headers=replay)
        self.assertEqual(headers.get("X-GX-Priority"), "health")
        _, headers, _ = self.chat({"model": "gx-high", "messages": msg}, headers=replay)
        self.assertEqual(headers.get("X-GX-Priority"), "normal-worker")

    def test_mode_reasoning_header_lowers_but_never_raises_unpermitted(self):
        msg = [{"role": "user", "content": "hi"}]
        self.chat({"model": "gx-max", "messages": msg}, headers={"X-GX-Reasoning": "none"})
        self.assertEqual(self.kwargs_sent(), {"thinking": False})
        self.chat({"model": "gx-fast", "messages": msg}, headers={"X-GX-Reasoning": "max"})
        self.assertEqual(self.kwargs_sent(), {"thinking": False})
        self.chat({"model": "gx-fast", "messages": msg},
                  headers={"X-GX-Reasoning": "max", **signed_identity("gx-operator")})
        self.assertEqual(self.kwargs_sent(), {"thinking": True, "reasoning_effort": "max"})

    def test_gx_auto_uses_a_mode_and_keeps_the_engine(self):
        status, headers, _ = self.chat({"model": "gx-auto", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("X-GX-Mode"), "fast")
        self.assertEqual(self.kwargs_sent(), {"thinking": False})

    def test_models_endpoints_need_the_operator_token(self):
        status, _, _ = self.get("/models/status")
        self.assertEqual(status, 403)
        status, _, _ = self.get("/models/status", headers={"X-GX-Operator-Token": "wrong-token-xxxxxxxxx"})
        self.assertEqual(status, 403)
        status, _, body = self.post("/models/activate", {"target": "dsv41-rollback", "confirm": True})
        self.assertEqual(status, 403)

    def test_switch_in_progress_is_a_clear_503(self):
        self.scheduler.pause("model switch abc to dsv41-rollback in progress")
        self.addCleanup(self.scheduler.resume)
        status, headers, body = self.chat({"model": "gx-fast", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 503)
        self.assertEqual(body["error"]["code"], "model_switch_in_progress")
        self.assertEqual(headers.get("Retry-After"), "60")


# ----------------------------------------------------------------- lifecycle
class _Health:
    healthy = True


def _stub_server(model_id):
    import http.server

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path.endswith("/health"):
                code, body = (200, b"ok") if _Health.healthy else (503, b"no")
            else:
                code, body = 200, json.dumps({"data": [{"id": model_id}]}).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n))
            H.last = req
            body = json.dumps({"choices": [{"message": {"content": "323"}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, H


class TestFixedShapeLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.reg = prod_registry()
        self.dep = self.reg.deployment("dsv4-flash-primary")
        self.srv, self.H = _stub_server("DeepSeek-V4-Flash-0731-abliterated")
        self.addCleanup(self.srv.shutdown)
        self.api = f"http://127.0.0.1:{self.srv.server_address[1]}/v1"
        (self.dir / "stop.sh").write_text("#!/usr/bin/env bash\necho stop >> stop.log\nexit 0\n")
        self.containers = {"node1": True, "node2": True}
        _Health.healthy = True

    def make(self, **kw):
        lc = GxMaxLifecycle(self.dir, api_base=self.api, model_id="DeepSeek-V4-Flash-0731-abliterated",
                            registry=self.reg, idle_ttl=0, acquire_timeout=10,
                            read_mem_gib=lambda n: 100.0,
                            container_running=lambda n: self.containers[n],
                            deployment=self.dep, fixed_profile=fixed_profile_for(self.dep),
                            settle_seconds=1.0, mem_return_wait_s=0.5, **kw)
        self.addCleanup(lc.shutdown)
        return lc

    def test_adopts_with_the_fixed_shape_and_never_cycles_for_a_profile(self):
        caps = []
        lc = self.make(on_ready=lambda p, n: caps.append(n))
        self.assertEqual(lc.status().state, State.READY)
        self.assertEqual(self.H.last["chat_template_kwargs"], {"thinking": False})   # V4 dialect probe
        self.assertEqual(caps, [8])
        self.assertEqual(lc.acquire("fast"), "dsv4-flash-primary-resident")
        self.assertEqual(lc.acquire("deep"), "dsv4-flash-primary-resident")
        self.assertFalse((self.dir / "stop.log").exists(), "a profile name must never restart the engine")

    def test_rank_failure_marks_down_and_runs_the_stop_path(self):
        lc = self.make()
        lc._RECONCILE_INTERVAL = 0.0
        self.containers["node2"] = False
        import gx_orchestrator.lifecycle as L
        old = L._RANK_CHECK_INTERVAL
        L._RANK_CHECK_INTERVAL = 0.0
        self.addCleanup(setattr, L, "_RANK_CHECK_INTERVAL", old)
        lc._last_reconcile = 0
        lc.status()                     # miss 1
        lc._last_reconcile = 0
        st = lc.status()                # miss 2 -> rank failure
        self.assertIn(st.state, (State.RELEASING, State.DOWN))
        self.assertEqual(st.failure_kind, FAILURE_RANK)
        deadline = time.time() + 5
        _Health.healthy = False   # the stop path took the API down with it
        while time.time() < deadline and lc._state is not State.DOWN:   # no reconcile in the loop
            time.sleep(0.05)
        self.assertEqual(lc._state, State.DOWN)   # DOWN only after the stop path finished (A6)
        self.assertTrue((self.dir / "stop.log").exists())

    def test_rank_probe_error_is_unknown_not_dead(self):
        lc = self.make()
        import gx_orchestrator.lifecycle as L
        old = L._RANK_CHECK_INTERVAL
        L._RANK_CHECK_INTERVAL = 0.0
        self.addCleanup(setattr, L, "_RANK_CHECK_INTERVAL", old)
        self.containers["node2"] = None          # SSH failed: could not tell
        for _ in range(4):
            lc._last_reconcile = 0
            self.assertEqual(lc.status().state, State.READY)
        self.assertFalse((self.dir / "stop.log").exists())

    def test_container_state_classification(self):
        from gx_orchestrator.lifecycle import container_state_from
        self.assertIs(container_state_from(0, "true\n", "", remote=True), True)
        self.assertIs(container_state_from(0, "false\n", "", remote=True), False)
        self.assertIs(container_state_from(1, "", "Error: No such object: dsv4f-worker", remote=True), False)
        self.assertIsNone(container_state_from(255, "", "ssh: connect to host ... timed out", remote=True))
        self.assertIsNone(container_state_from(1, "", "Cannot connect to the Docker daemon", remote=False))

    def test_latched_lifecycle_refuses_every_acquire(self):
        lc = self.make()
        lc.latch("switch x to dsv41-rollback")
        with self.assertRaises(AcquisitionError):
            lc.acquire(None, bypass_backoff=True)
        lc.unlatch()
        self.assertTrue(lc.acquire(None))

    def test_circuit_breaker_stops_request_driven_restarts(self):
        _Health.healthy = False
        (self.dir / "start.sh").write_text("#!/usr/bin/env bash\nexit 3\n")
        lc = self.make(failure_backoff_base=0, circuit_breaker_after=2)
        for _ in range(2):
            with self.assertRaises(AcquisitionError):
                lc.acquire(None)
        self.assertTrue(lc.status().circuit_open)
        with self.assertRaises(AcquisitionError) as c:
            lc.acquire(None)
        self.assertIn("automatic restarts are OFF", str(c.exception))
        _Health.healthy = True
        (self.dir / "start.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
        lc.acquire(None, bypass_backoff=True)    # an operator acquire closes the circuit
        self.assertFalse(lc.status().circuit_open)

    def test_gid_overlay_uses_the_deployment_keys_and_env_files(self):
        (self.dir / "dsv4-flash.env.canonical").write_text(
            "DSV4F_HEAD_IB=rocep1s0f0\nDSV4F_HEAD_IP=192.168.100.10\n"
            "DSV4F_WORKER_IB=rocep1s0f0\nDSV4F_WORKER_IP=192.168.100.11\n")
        tables = {"node1": {3: ("0000:0000:0000:0000:0000:ffff:c0a8:640a", "RoCE v2")},
                  "node2": {4: ("0000:0000:0000:0000:0000:ffff:c0a8:640b", "RoCE v2")}}
        _Health.healthy = False
        lc = self.make(read_gid_table=lambda node, ib: tables[node], gid_settle_seconds=0)
        out = lc._resolve_gid_overlay({"DSV4F_GID_PREFERRED": "3"})
        self.assertEqual(out, {"DSV4F_HEAD_GID": "3", "DSV4F_WORKER_GID": "4"})


class TestRegistryDeployments(unittest.TestCase):
    def test_legacy_registry_gets_one_implicit_deployment(self):
        reg = parse_registry(fixture_registry_dict())
        self.assertEqual(list(reg.deployments), ["legacy"])
        self.assertFalse(reg.deployments["legacy"].mode_routing)

    def test_fixed_profile_from_engine_shape(self):
        p = fixed_profile_for(prod_registry().deployment("dsv4-flash-primary"))
        self.assertEqual((p.max_num_seqs, p.max_model_len, p.dspark_tokens), (8, 131072, 5))
        self.assertIsNone(fixed_profile_for(prod_registry().deployment("dsv41-rollback")))


if __name__ == "__main__":
    unittest.main()
