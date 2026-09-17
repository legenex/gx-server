"""Resource Control for the Build V3 node-2 supervisors (gx-voice, gx-call,
gx-live): measured-only policies, admission, compatibility, live state from
their open /health, and LOAD / UNLOAD / DRAIN through their sanctioned API
(stub supervisors on 127.0.0.1)."""

from __future__ import annotations

import secrets
import time
import unittest
from unittest import mock

from support import StubUpstream, TempEnv
from test_resources import FakeActions, FakeCluster, FakeMedia, FakeMusic, facts

from gx_control_ui import resources as rc
from gx_control_ui.node2_services import SPECS, Node2Service, ServiceError, runtime_view
from gx_control_ui.resources import POLICIES, ResourceController, ResourceError, admission_view, pair_verdict

FP = {"node": "gx10-02", "cold_gib": 34.0, "resident_gib": 31.0, "startup_s": 102.0, "measured": "2026-09-17",
      "evidence": "/srv/logs/acceptance/build-v3/liv/probe1"}


def reset_policies():
    for alias in rc.V3_ALIASES:
        POLICIES[alias] = rc.v3_policy(alias)


class PolicyTests(unittest.TestCase):
    def tearDown(self):
        reset_policies()

    def test_unmeasured_is_never_invented(self):
        for alias in rc.V3_ALIASES:
            p = POLICIES[alias]
            self.assertFalse(p.measured_ok)
            self.assertEqual((p.cold_gib, p.footprint_gib), (0.0, 0.0))
            self.assertEqual(p.measured, "not measured yet")
            self.assertIn("pin", p.controls)
            self.assertIn(alias, rc.PIN_ALIASES)
            self.assertIn(alias, rc.NODE2_TENANTS)
            self.assertIn(alias, rc.GENERATIVE)
        view = admission_view("gx-live", 80.0, {})
        self.assertEqual((view["code"], view["allowed"]), ("unmeasured", None))
        self.assertIn("not measured yet", view["reason"])
        v = pair_verdict("gx-live", "gx-music", capacity={"node2": 113.0}, residents={}, profile="auto")
        self.assertEqual((v["verdict"], v["summary"]), ("unknown", "Not measured yet"))
        # other node and gx-max rules still apply
        self.assertEqual(pair_verdict("gx-live", "gx-mini", capacity={}, residents={}, profile="auto")["verdict"],
                         "coexist")
        self.assertEqual(pair_verdict("gx-live", "gx-max", capacity={}, residents={}, profile="auto")["verdict"],
                         "exclusive")

    def test_measurements_drive_admission_and_compatibility(self):
        rc.apply_measurements({"gx-live": {"measured_footprint": FP}, "gx-call": {"node": "gx10-02"}})
        live = POLICIES["gx-live"]
        self.assertTrue(live.measured_ok)
        self.assertEqual((live.cold_gib, live.footprint_gib), (34.0, 31.0))
        self.assertIn("102 s", live.cold_start_s)
        self.assertFalse(POLICIES["gx-call"].measured_ok)
        ok = admission_view("gx-live", 70.0, {})
        self.assertEqual((ok["allowed"], ok["need_gib"]), (True, 64.0))
        short = admission_view("gx-live", 60.0, {"gx-music": {"active": False, "pending_gib": 0.0}})
        self.assertFalse(short["allowed"])
        self.assertEqual(short["actions"][0]["unload"], ["gx-music"])
        # pending memory of a loading peer counts
        pend = admission_view("gx-live", 70.0, {"gx-voice": {"active": True, "pending_gib": 10.0}})
        self.assertFalse(pend["allowed"])
        self.assertIn("size not measured", pend["reason"])
        # an unmeasured idle tenant is never offered as memory to free
        self.assertEqual(pend["actions"], [])
        v = pair_verdict("gx-live", "gx-music", capacity={"node2": 113.0}, residents={}, profile="auto")
        self.assertEqual(v["verdict"], "coexist")
        v = pair_verdict("gx-live", "gx-video", capacity={"node2": 113.0}, residents={}, profile="auto")
        self.assertEqual(v["verdict"], "exclusive")
        # a registry without the footprint returns to "not measured"
        rc.apply_measurements({})
        self.assertFalse(POLICIES["gx-live"].measured_ok)

    def test_runtime_view_mapping(self):
        base = {"reachable": True, "memory": {"pending_gib": 4.5, "resident_gib": 20, "estimate_gib": 34}}
        cases = {"unloaded": "UNLOADED", "waiting": "WAITING", "loading": "LOADING", "ready": "READY",
                 "busy": "GENERATING", "unloading": "DRAINING", "failed": "ERROR", "error": "ERROR",
                 "weird": "ERROR"}
        for raw, want in cases.items():
            view = runtime_view("gx-voice", {**base, "state": raw}, gx_busy=False, maint=False)
            self.assertEqual(view["state"], want, raw)
        view = runtime_view("gx-voice", {**base, "state": "waiting", "waiting": {"reason": "needs memory"},
                                         "queue": {"active": 3}}, gx_busy=False, maint=False)
        self.assertEqual((view["detail"], view["queue"], view["pending_gib"]), ("needs memory", 3, 4.5))
        self.assertEqual(runtime_view("gx-call", {"reachable": False, "error": "x"}, gx_busy=False,
                                      maint=False)["state"], "ERROR")
        self.assertEqual(runtime_view("gx-call", {**base, "state": "ready"}, gx_busy=True, maint=False)["state"],
                         "BLOCKED")
        self.assertEqual(runtime_view("gx-call", {**base, "state": "unloaded"}, gx_busy=False, maint=True)["state"],
                         "BLOCKED")
        bad = runtime_view("gx-call", {"reachable": True, "state": "ready", "memory": {"pending_gib": "nan?"},
                                       "active_sessions": 1, "queue": 2}, gx_busy=False, maint=False)
        self.assertEqual((bad["pending_gib"], bad["active_sessions"], bad["queue"]), (0.0, 1, 2))


class Supervisor:
    """A stub node-2 supervisor with the published contract."""

    def __init__(self, prefix: str, key: str) -> None:
        self.key = key
        self.state = "ready"
        self.sessions = 0
        self.unloads: list[dict] = []
        self.loads = 0

        def health(handler, body):
            return 200, {"service": f"gx-{prefix}", "state": self.state, "busy": bool(self.sessions),
                         "pinned": False, "active_sessions": self.sessions, "queue": 0,
                         "memory": {"pending_gib": 0.0, "resident_gib": 31.0, "estimate_gib": 34.0}}

        def authed(fn):
            def wrapper(handler, body):
                if handler.headers.get("Authorization") != f"Bearer {self.key}":
                    return 401, {"error": {"message": "unauthorized"}}
                return fn(handler, body)
            return wrapper

        @authed
        def unload(handler, body):
            self.unloads.append(body)
            if body.get("if_idle") and self.sessions:
                return 409, {"error": {"message": "a session is live", "code": "busy"}}
            self.state, self.sessions = "unloaded", 0
            return 200, {"container_gone": True, "reason": body.get("reason")}

        @authed
        def load(handler, body):
            self.loads += 1
            self.state = "ready"
            return 200, {"state": "ready"}

        self.stub = StubUpstream({("GET", "/health"): health, ("POST", f"/v1/{prefix}/unload"): unload,
                                  ("POST", f"/v1/{prefix}/load"): load})


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv()
        self.key = secrets.token_urlsafe(24)
        self.sup = {a: Supervisor(SPECS[a].prefix, self.key) for a in rc.V3_ALIASES}
        self.clients = {}
        for alias, sup in self.sup.items():
            kf = self.env.root / "secrets" / alias / "api-key"
            kf.parent.mkdir(parents=True)
            kf.write_text(self.key + "\n")
            self.clients[alias] = Node2Service(SPECS[alias], sup.stub.url, kf)
        self.cluster = FakeCluster(n2=facts(80))
        self.audits = []
        self.registry = {"aliases": {"gx-live": {"measured_footprint": FP}}}
        self.ctrl = ResourceController(self.env.cfg, self.cluster, FakeActions(), music=FakeMusic(),
                                       media=FakeMedia(), audit=lambda **kw: self.audits.append(kw),
                                       node2_writer=lambda n, c: True, start_thread=False,
                                       services=self.clients, registry=lambda: self.registry)
        self.ctrl.probe_services = True
        self.ctrl.probe_music = True

    def tearDown(self):
        for sup in self.sup.values():
            sup.stub.close()
        self.env.cleanup()
        reset_policies()

    def wait_bg(self, job):
        for _ in range(100):
            state = self.ctrl.background_job(job["id"])
            if state["state"] != "running":
                return state
            time.sleep(0.05)
        self.fail("background job did not finish")

    def test_snapshot_shows_live_state_and_measured_numbers(self):
        self.sup["gx-call"].state = "busy"
        self.sup["gx-call"].sessions = 1
        self.sup["gx-voice"].state = "unloaded"
        snap = self.ctrl.snapshot()
        rt = snap["runtimes"]
        self.assertEqual(rt["gx-live"]["state"], "READY")
        self.assertEqual(rt["gx-call"]["state"], "GENERATING")
        self.assertEqual(rt["gx-call"]["active_sessions"], 1)
        self.assertEqual(rt["gx-voice"]["state"], "UNLOADED")
        self.assertEqual(rt["gx-live"]["footprint_gib"], 31.0)
        self.assertIsNone(rt["gx-call"]["footprint_gib"])
        self.assertFalse(rt["gx-call"]["measured_ok"])
        self.assertIn("gx-live", snap["nodes"]["node2"]["runtimes"])
        self.assertEqual(snap["policies"]["gx-live"]["cold_gib"], 34.0)
        residents = self.ctrl.residents(snap, "node2")
        self.assertTrue(residents["gx-call"]["active"])
        self.assertIn("gx-live", residents)
        comp = self.ctrl.compatibility(snap)
        verdicts = {(p["a"], p["b"]): p["verdict"] for p in comp["pairs"]}
        self.assertEqual(verdicts[("gx-music", "gx-call")], "unknown")
        self.assertIn(verdicts[("gx-music", "gx-live")], ("coexist", "scheduled", "exclusive"))

    def test_unreachable_and_not_installed(self):
        self.sup["gx-voice"].stub.close()
        self.clients["gx-voice"].invalidate()
        (self.env.root / "secrets" / "gx-call" / "api-key").unlink()
        snap = self.ctrl.snapshot()
        self.assertEqual(snap["runtimes"]["gx-voice"]["state"], "ERROR")
        self.assertIn("not reachable", snap["runtimes"]["gx-voice"]["detail"])
        self.assertIn("not installed", snap["runtimes"]["gx-call"]["detail"])
        with self.assertRaises(ResourceError) as ctx:
            self.ctrl.control("gx-call", "load", user="admin")
        self.assertEqual(ctx.exception.status, 503)

    def test_gxmax_hold_blocks(self):
        self.cluster.gx = "acquiring"
        self.assertEqual(self.ctrl.snapshot()["runtimes"]["gx-live"]["state"], "BLOCKED")

    def test_load_and_unload_through_the_supervisor(self):
        self.sup["gx-voice"].state = "unloaded"
        started = self.ctrl.control("gx-voice", "load", user="admin")
        self.assertEqual(self.wait_bg(started["started"])["state"], "succeeded")
        self.assertEqual(self.sup["gx-voice"].loads, 1)
        done = self.ctrl.control("gx-voice", "unload", user="admin")
        self.assertTrue(done["done"])
        self.assertEqual(self.sup["gx-voice"].unloads[-1], {"if_idle": False, "reason": "manual"})
        self.assertIn("resources.unload.gx-voice", [a["action"] for a in self.audits])

    def test_live_session_needs_confirmation_to_unload(self):
        self.sup["gx-call"].state = "busy"
        self.sup["gx-call"].sessions = 1
        with self.assertRaises(ResourceError) as ctx:
            self.ctrl.control("gx-call", "unload", user="admin")
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(self.sup["gx-call"].unloads, [])
        self.ctrl.control("gx-call", "unload", user="admin", confirm=True)
        self.assertEqual(self.sup["gx-call"].unloads[-1]["if_idle"], False)

    def test_scheduler_unload_is_if_idle_and_verified(self):
        ok, _ = self.ctrl._unload("gx-live")
        self.assertTrue(ok)
        self.assertEqual(self.sup["gx-live"].unloads[-1], {"if_idle": True, "reason": "scheduler"})
        self.sup["gx-call"].sessions = 1
        ok, msg = self.ctrl._unload("gx-call")
        self.assertFalse(ok)
        self.assertIn("live", msg)

    def test_scheduler_never_takes_a_live_session(self):
        self.sup["gx-live"].sessions = 1
        self.sup["gx-live"].state = "busy"
        snap = self.ctrl.snapshot()
        self.assertEqual(self.ctrl.preemptable("media", "gx-video", ["gx-live"], snap), [])
        self.sup["gx-live"].sessions = 0
        self.sup["gx-live"].state = "ready"
        self.clients["gx-live"].invalidate()
        snap = self.ctrl.snapshot()
        self.assertEqual(self.ctrl.preemptable("media", "gx-video", ["gx-live"], snap), ["gx-live"])

    def test_drain_waits_for_the_session_then_unloads(self):
        self.sup["gx-live"].sessions = 1
        self.sup["gx-live"].state = "busy"
        real_sleep = time.sleep
        with mock.patch.object(rc.time, "sleep", lambda s: real_sleep(min(s, 0.05))):
            job = self.ctrl.control("gx-live", "drain", user="admin")["started"]
            real_sleep(0.4)
            self.assertEqual(self.ctrl.background_job(job["id"])["state"], "running")
            self.assertEqual(self.sup["gx-live"].unloads, [])
            self.sup["gx-live"].sessions = 0
            self.sup["gx-live"].state = "ready"
            state = self.wait_bg(job)
        self.assertEqual(state["state"], "succeeded")
        self.assertTrue(self.sup["gx-live"].unloads)

    def test_pin_writes_node2_pins(self):
        writes = []
        self.ctrl._node2_writer = lambda name, content: writes.append((name, content)) or True
        self.ctrl.control("gx-voice", "pin", user="admin")
        self.assertEqual(writes[-1][0], "pins.json")
        self.assertIn("gx-voice", writes[-1][1])

    def test_profile_plans_mention_idle_supervisors(self):
        plan = self.ctrl.plan_profile("media")
        self.assertIn("gx-live", plan["may_drain"])
        plan = self.ctrl.plan_profile("maintenance")
        self.assertIn("gx-live", plan["drains_now"])

    def test_maintenance_asks_idle_supervisors_to_unload(self):
        steps = self.ctrl.enter_maintenance(user="admin")
        self.assertTrue(any(s.startswith("gx-live unloaded") for s in steps))
        self.assertEqual(self.sup["gx-live"].unloads[-1]["if_idle"], True)

    def test_client_errors(self):
        c = self.clients["gx-voice"]
        (self.env.root / "secrets" / "gx-voice" / "api-key").write_text("short")
        self.assertFalse(c.configured)
        with self.assertRaises(ServiceError):
            c.load()
        self.assertTrue(Node2Service.unloaded({"noop": True}))
        self.assertFalse(Node2Service.unloaded({"container_gone": False}))
        self.assertFalse(Node2Service.unloaded("nope"))


if __name__ == "__main__":
    unittest.main()
