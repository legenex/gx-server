"""Hermetic tests for Resource Control (D-037): the pure admission and
compatibility logic, and ResourceController driven by injected fakes (no
SSH, no HTTP to the cluster, no background thread)."""

from __future__ import annotations

import json
import secrets
import time
import unittest
from unittest import mock

from support import TempEnv

from gx_control_ui import resources as rc
from gx_control_ui.resources import (GIB, POLICIES, AdmissionBlocked, ResourceController, ResourceError,
                                     admission_view, enforced_need, pair_verdict)


# ------------------------------------------------------------------ fakes
class Cache:
    def __init__(self, value):
        self.value = value
        self.invalidations = 0

    def get(self, max_age=None):
        return self.value

    def invalidate(self):
        self.invalidations += 1


def facts(avail_gib, *, holds=None, pins=None, profile=None, reachable=True):
    return {"reachable": reachable,
            "memory": {"MemAvailable": int(avail_gib * GIB), "MemTotal": 121 * GIB,
                       "SwapTotal": 48 * GIB, "SwapFree": 40 * GIB},
            "psi": {"memory": {"full": {"avg10": 0.5}}},
            "guard": {"holds": holds or {}, "pins": pins or {}, "profile": profile, "ledger": {"x": 1}},
            "disk": {"free": 300 * GIB, "percent": 60.0}}


def services(n1=(), n2=(), media=None, orch=True):
    return {
        "swap_node1_running": {"ok": True, "body": {"running": [{"model": m, "state": s} for m, s in n1]}},
        "swap_node2_running": {"ok": True, "body": {"running": [{"model": m, "state": s} for m, s in n2]}},
        "media": {"ok": media is not None, "body": media},
        "orchestrator": {"ok": orch},
    }


class FakeCluster:
    def __init__(self, n1=None, n2=None, svc=None, gx="down"):
        self.node1 = Cache(n1 if n1 is not None else facts(60))
        self.node2 = Cache(n2 if n2 is not None else facts(100))
        self.services = Cache(svc if svc is not None else services())
        self.gx = gx
        self.unloaded: list[str] = []
        self.invalidated = 0

    def gxmax_state(self):
        return self.gx

    def swap_unload(self, alias):
        self.unloaded.append(alias)
        return True, "HTTP 200: ok"

    def swap_headers(self):
        return {}

    def invalidate(self):
        self.invalidated += 1


class FakeJob:
    def __init__(self, name):
        self.id = secrets.token_hex(8)
        self.name = name

    def as_dict(self, with_output=True):
        return {"id": self.id, "action": self.name}


class FakeActions:
    def __init__(self):
        self.submitted: list[tuple[str, object]] = []
        self.running_jobs: list[dict] = []

    def submit(self, name, *, user, ip, confirm=None):
        self.submitted.append((name, confirm))
        return FakeJob(name)

    def running(self):
        return self.running_jobs


class FakeMusic:
    def __init__(self, state="unloaded", queue=None):
        self.state = state
        self.queue = queue or {"active": 0, "current_job": None}
        self.calls: list[str] = []

    def model(self):
        return {"engine": {"state": self.state, "detail": "", "blocked_by": None, "last_load_seconds": 85.0,
                           "idle_seconds": 3.0}, "queue": self.queue, "jobs": {"completed": 1}}

    def lifecycle(self, op, *, user, if_idle=False):
        self.calls.append(op if not if_idle else f"{op}:if_idle")
        self.state = "ready" if op == "load" else "unloaded"
        return {"state": self.state, "container_gone": op == "unload"}


class FakeMedia:
    def __init__(self, jobs=None):
        self.jobs = jobs or []

    def snapshot(self):
        counts: dict[str, int] = {}
        for j in self.jobs:
            if not j.get("done"):
                counts[j["phase"]] = counts.get(j["phase"], 0) + 1
        return {"jobs": self.jobs, "counts": counts}


# ------------------------------------------------------------ pure logic
class EnforcedNeedTests(unittest.TestCase):
    def test_numbers_match_the_enforcing_components(self):
        self.assertEqual(enforced_need("gx-reason", resident=False), 75.0)
        self.assertEqual(enforced_need("gx-music", resident=False), 62.0)
        self.assertEqual(enforced_need("gx-mini", resident=False), 40.0)
        self.assertEqual(enforced_need("gx-max", resident=False), 100.0)
        # D-038: media now keeps the same 30 GiB reserve as everything else
        self.assertEqual(enforced_need("gx-image", resident=False), 87.0)
        self.assertEqual(enforced_need("gx-video", resident=False), 102.0)
        self.assertEqual(enforced_need("gx-video", resident=False, variant="keyframe_edit"), 137.0)
        self.assertEqual(enforced_need("gx-image", resident=False, variant="unknown"), 87.0)
        # warm: the router's measured growth; unknown -> the full footprint
        self.assertEqual(enforced_need("gx-image", resident=True), 87.0)
        self.assertEqual(enforced_need("gx-image", resident=True, warm_growth=12.0), 42.0)
        self.assertEqual(enforced_need("gx-video", resident=True, variant="keyframe_edit", warm_growth=8.0), 38.0)


class AdmissionViewTests(unittest.TestCase):
    def test_fits(self):
        v = admission_view("gx-reason", 100.0, {})
        self.assertTrue(v["allowed"])
        self.assertEqual(v["code"], "fits")
        self.assertEqual((v["need_gib"], v["available_gib"]), (75.0, 100.0))
        self.assertEqual(v["reserve_gib"], 30.0)
        self.assertEqual(v["node"], "node2")

    def test_image_and_video_keep_the_30_gib_reserve(self):
        v = admission_view("gx-image", 100.0, {})
        self.assertEqual((v["reserve_gib"], v["growth_gib"], v["need_gib"], v["allowed"]), (30.0, 57.0, 87.0, True))
        v = admission_view("gx-video", 100.0, {})
        self.assertEqual((v["reserve_gib"], v["need_gib"], v["allowed"]), (30.0, 102.0, False))
        self.assertIsNone(admission_view("gx-max", 110.0, {})["reserve_gib"])

    def test_the_18_gib_case_video_next_to_music_is_not_admitted(self):
        # music loaded: 88 GiB left; a cold video would leave ~16 GiB
        v = admission_view("gx-video", 88.0, {"gx-music": {"active": False, "pending_gib": 6.0}})
        self.assertFalse(v["allowed"])
        self.assertEqual((v["code"], v["pending_gib"], v["short_gib"]), ("insufficient_memory", 6.0, 20.0))
        self.assertEqual(v["actions"][0]["unload"], ["gx-music"])
        self.assertIn("gx-video needs 72 GiB plus the 30 GiB reserve plus 6 GiB", v["reason"])
        # music busy: no plan, it waits
        v = admission_view("gx-video", 88.0, {"gx-music": {"active": True, "pending_gib": 6.0}})
        self.assertEqual(v["actions"], [])
        self.assertIn("busy", v["reason"])

    def test_coexistence_is_admitted_when_the_reserve_holds(self):
        v = admission_view("gx-image", 100.0, {"gx-music": {"active": False, "pending_gib": 6.0}})
        self.assertTrue(v["allowed"])
        self.assertIn("6 GiB still to be taken", v["reason"])

    def test_a_loading_tenant_counts_with_what_it_has_not_taken_yet(self):
        v = admission_view("gx-image", 110.0, {"gx-music": {"active": True, "pending_gib": 32.0}})
        self.assertFalse(v["allowed"])
        self.assertEqual(v["actions"], [], "a load in progress is never interrupted")

    def test_keyframe_edit_can_never_run_and_is_terminal(self):
        v = admission_view("gx-video", 116.0, {}, variant="keyframe_edit")
        self.assertEqual((v["allowed"], v["code"], v["terminal"], v["need_gib"]), (False, "exceeds_node", True, 137.0))
        self.assertIn("B-028", v["reason"])

    def test_already_resident_text_model(self):
        v = admission_view("gx-reason", 10.0, {"gx-reason": {"active": False}})
        self.assertEqual((v["allowed"], v["code"], v["need_gib"]), (True, "resident", 0.0))

    def test_warm_media_needs_its_measured_growth_plus_the_reserve(self):
        v = admission_view("gx-image", 40.0, {"gx-image": {"active": False}}, warm_growth=8.0)
        self.assertTrue(v["allowed"])
        self.assertEqual(v["need_gib"], 38.0)
        self.assertFalse(admission_view("gx-image", 10.0, {"gx-image": {"active": False}}, warm_growth=8.0)["allowed"])
        # growth unknown: judged like a cold job
        self.assertEqual(admission_view("gx-image", 40.0, {"gx-image": {"active": False}})["need_gib"], 87.0)

    def test_unknown_memory_is_refused(self):
        v = admission_view("gx-music", None, {})
        self.assertFalse(v["allowed"])
        self.assertEqual(v["code"], "unknown")
        self.assertIsNone(v["available_gib"])

    def test_gxmax_hold_blocks_everything_but_gxmax(self):
        v = admission_view("gx-image", 110.0, {}, holds={"gxmax": True})
        self.assertEqual((v["allowed"], v["code"], v["blocking"]), (False, "gx_max_active", ["gx-max"]))
        self.assertTrue(admission_view("gx-max", 110.0, {}, holds={"gxmax": True})["allowed"])

    def test_maintenance_blocks(self):
        v = admission_view("gx-reason", 110.0, {}, holds={"maintenance": True})
        self.assertEqual((v["allowed"], v["code"]), (False, "maintenance"))

    def test_insufficient_memory_plans_the_largest_idle_unpinned_tenant(self):
        residents = {"gx-image": {"active": False}, "gx-music": {"active": False}}
        v = admission_view("gx-video", 50.0, residents)
        self.assertFalse(v["allowed"])
        self.assertEqual(v["code"], "insufficient_memory")
        self.assertEqual(v["short_gib"], 52.0)
        self.assertEqual(v["blocking"], ["gx-image", "gx-music"])
        self.assertEqual(v["actions"][0]["id"], "unload_and_continue")
        self.assertEqual(v["actions"][0]["unload"], ["gx-image"])
        self.assertIn("gx-video needs 72 GiB plus the 30 GiB reserve on gx10-02, so 102 GiB must be available",
                      v["reason"])

    def test_plan_accumulates_until_the_gap_closes(self):
        residents = {"gx-music": {"active": False}, "gx-image": {"active": False}}
        v = admission_view("gx-reason", 0.0, residents)
        self.assertEqual(v["actions"][0]["unload"], ["gx-image", "gx-music"])

    def test_busy_and_pinned_tenants_are_never_planned(self):
        residents = {"gx-image": {"active": True}, "gx-music": {"active": False}}
        v = admission_view("gx-video", 20.0, residents, pins={"gx-music"})
        self.assertEqual(v["actions"], [])
        self.assertIn("busy", v["reason"])
        self.assertIn("pinned", v["reason"])
        self.assertEqual(v["blocking"], ["gx-image", "gx-music"])

    def test_no_action_when_freeing_everything_is_not_enough(self):
        v = admission_view("gx-reason", 1.0, {"gx-music": {"active": False}})
        self.assertEqual(v["need_gib"], 75.0)
        self.assertEqual(v["actions"], [])

    def test_residents_outside_the_policy_table_are_ignored(self):
        v = admission_view("gx-reason", 10.0, {"not-a-model": {"active": False}})
        self.assertEqual(v["blocking"], [])


class PairVerdictTests(unittest.TestCase):
    cap = {"node1": 113.0, "node2": 113.0}

    def verdict(self, a, b, cap=None, residents=None, profile="auto"):
        return pair_verdict(a, b, capacity=cap or self.cap, residents=residents or {}, profile=profile)

    def test_gxmax_is_exclusive_with_everything(self):
        for other in ("gx-mini", "gx-reason", "gx-music"):
            v = self.verdict("gx-max", other)
            self.assertEqual(v["verdict"], "exclusive")
            self.assertIn(f"{other} starts again", v["why"][-1])
            self.assertEqual(self.verdict(other, "gx-max")["verdict"], "exclusive")

    def test_different_nodes_coexist(self):
        v = self.verdict("gx-mini", "gx-reason")
        self.assertEqual(v["verdict"], "coexist")
        self.assertIn("different nodes", v["summary"])

    def test_image_and_video_are_serialized(self):
        self.assertEqual(self.verdict("gx-image", "gx-video")["verdict"], "serialized")
        self.assertEqual(self.verdict("gx-video", "gx-image")["verdict"], "serialized")

    def test_node1_uses_measured_footprints(self):
        self.assertEqual(self.verdict("gx-mini", "gx-fast")["verdict"], "coexist")
        self.assertEqual(self.verdict("gx-mini", "gx-fast", cap={"node1": 50.0})["verdict"], "exclusive")

    def test_node2_pairs(self):
        self.assertEqual(self.verdict("gx-reason", "gx-music")["verdict"], "coexist")
        self.assertEqual(self.verdict("gx-reason", "gx-video")["verdict"], "exclusive")
        # D-038: with the 30 GiB reserve an image no longer fits next to gx-reason (113 - 44 < 57 + 30)
        self.assertEqual(self.verdict("gx-reason", "gx-image", profile="text")["verdict"], "exclusive")
        # the 18.6 GiB case: a cold video never joins music, music never joins a video
        v = self.verdict("gx-video", "gx-music")
        self.assertEqual(v["verdict"], "exclusive")
        self.assertIn("does not fit", v["why"][1])
        # an image can join music (113 - 26 >= 87), not the other way round
        v = self.verdict("gx-image", "gx-music", profile="music")
        self.assertEqual(v["verdict"], "scheduled")
        self.assertIn("gx-image can join gx-music", v["summary"])
        self.assertIn("Music", v["why"][-1])

    def test_missing_capacity_falls_back_to_idle_capacity(self):
        self.assertEqual(pair_verdict("gx-reason", "gx-music", capacity={}, residents={}, profile="auto")["verdict"],
                         "coexist")

    def test_other_residents_are_mentioned(self):
        v = self.verdict("gx-reason", "gx-music", residents={"node2": {"gx-image", "gx-reason"}})
        self.assertTrue(any("Also loaded right now: gx-image" in w for w in v["why"]))

    def test_small_capacity_makes_node2_exclusive(self):
        self.assertEqual(self.verdict("gx-reason", "gx-music", cap={"node2": 60.0})["verdict"], "exclusive")


# ----------------------------------------------------------- controller
class ControllerBase(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv()
        self.writes: list[tuple[str, str]] = []
        self.writer_ok = True
        self.audits: list[dict] = []
        self.cluster = FakeCluster()
        self.actions = FakeActions()
        self.music = FakeMusic()
        self.media = FakeMedia()
        self.ctrl = self.make()

    def make(self):
        ctrl = ResourceController(self.env.cfg, self.cluster, self.actions, music=self.music, media=self.media,
                                  audit=lambda **kw: self.audits.append(kw), node2_writer=self.writer,
                                  start_thread=False)
        ctrl.probe_music = True
        self.freed = 0

        def media_free():
            self.freed += 1
            return True, '{"freed": true}'
        ctrl.media_free = media_free
        return ctrl

    def writer(self, name, content):
        self.writes.append((name, content))
        return self.writer_ok

    def tearDown(self):
        self.env.cleanup()

    def set_svc(self, **kw):
        self.cluster.services.value = services(**kw)
        self.ctrl._music_model = (0.0, {})  # drop the 4 s music cache


class SnapshotTests(ControllerBase):
    def setUp(self):
        super().setUp()
        self.cluster.node2.value = facts(50, pins={"gx-music": {"by": "admin", "since": 1.0}})
        self.music.state = "ready"
        self.set_svc(n1=[("gx-mini", "ready"), ("gx-fast", "starting")], n2=[("gx-reason", "ready")],
                     media={"resident_alias": "gx-image", "held_by": "", "video_queue_depth": 2, "busy": False})

    def test_runtime_states(self):
        snap = self.ctrl.snapshot()
        rt = snap["runtimes"]
        self.assertEqual(set(rt), set(POLICIES))
        self.assertEqual(rt["gx-mini"]["state"], "READY")
        self.assertEqual(rt["gx-fast"]["state"], "LOADING")
        self.assertEqual(rt["gx-reason"]["state"], "READY")
        self.assertEqual(rt["gx-image"]["state"], "READY")
        self.assertEqual(rt["gx-video"]["state"], "UNLOADED")
        self.assertEqual(rt["gx-video"]["queue"], 2)
        self.assertEqual(rt["gx-music"]["state"], "READY")
        self.assertEqual(rt["gx-max"]["state"], "UNLOADED")
        self.assertEqual(rt["gx-auto"]["state"], "READY")
        for state in (r["state"] for r in rt.values()):
            self.assertIn(state, rc.STATES)
        self.assertEqual(snap["profile"]["profile"], "auto")
        self.assertEqual(snap["queue"]["media_router_video"], 2)
        self.assertEqual(snap["nodes"]["node2"]["mem_available_gib"], 50.0)
        self.assertEqual(snap["nodes"]["node2"]["swap_used_gib"], 8.0)
        self.assertEqual(snap["nodes"]["node1"]["name"], "gx10-01")
        self.assertFalse(snap["maintenance"])
        self.assertEqual(set(snap["policies"]), set(POLICIES))
        json.dumps(snap)  # the whole snapshot is JSON serialisable

    def test_pins_and_pin_state(self):
        snap = self.ctrl.snapshot()
        self.assertEqual(list(snap["pins"]), ["gx-music"])
        self.assertTrue(snap["runtimes"]["gx-music"]["pinned"])
        self.assertTrue(snap["runtimes"]["gx-music"]["pin_state"]["honoured"])
        self.cluster.node2.value = facts(20, pins={"gx-music": {}})
        pin = self.ctrl.snapshot()["runtimes"]["gx-music"]["pin_state"]
        self.assertFalse(pin["honoured"])
        self.assertIn("reserve 30 GiB", pin["reason"])

    def test_invalid_pins_are_dropped(self):
        self.cluster.node2.value = facts(50, pins={"gx-mini": {}, "gx-image": "nope", "evil": {}})
        (self.env.cfg.guard_dir / "pins.json").write_text(json.dumps({"gx-reason": {"by": "x"}}))
        self.assertEqual(list(self.ctrl.pins()), ["gx-reason"])

    def test_generating_and_waiting_media(self):
        self.set_svc(media={"resident_alias": "gx-image", "held_by": "video-abc", "busy": True})
        self.assertEqual(self.ctrl.snapshot()["runtimes"]["gx-video"]["state"], "GENERATING")
        self.media.jobs = [{"id": "j1", "alias": "gx-image", "phase": "waiting", "done": False,
                            "waiting": {"reason": "Waiting for gx-reason to unload"}}]
        self.set_svc(media={"resident_alias": None, "held_by": ""})
        img = self.ctrl.snapshot()["runtimes"]["gx-image"]
        self.assertEqual((img["state"], img["detail"], img["queue"]),
                         ("WAITING", "Waiting for gx-reason to unload", 1))

    def test_unreachable_upstreams_are_errors(self):
        self.cluster.services.value = {"swap_node1_running": {"ok": False}, "media": {"ok": False},
                                       "orchestrator": {"ok": False}}
        self.music.model = lambda: (_ for _ in ()).throw(OSError("down"))
        rt = self.ctrl.snapshot()["runtimes"]
        for alias in ("gx-mini", "gx-fast", "gx-reason", "gx-image", "gx-video", "gx-music", "gx-auto"):
            self.assertEqual(rt[alias]["state"], "ERROR", alias)
        self.cluster.gx = "weird"
        self.assertEqual(self.ctrl.snapshot()["runtimes"]["gx-max"]["state"], "ERROR")

    def test_gxmax_owns_the_cluster(self):
        self.cluster.gx = "ready"
        rt = self.ctrl.snapshot()["runtimes"]
        self.assertEqual(rt["gx-max"]["state"], "READY")
        for alias in ("gx-mini", "gx-fast", "gx-reason", "gx-image", "gx-video"):
            self.assertEqual(rt[alias]["state"], "BLOCKED", alias)
        self.assertEqual(self.ctrl.admission("gx-image")["code"], "gx_max_active")
        self.assertEqual(self.ctrl.explain("gx-image")["code"], "gx_max_active")
        pin = self.ctrl.snapshot()["runtimes"]["gx-music"]["pin_state"]
        self.assertIn("gx-max", pin["reason"])

    def test_gxmax_hold_file_on_node2(self):
        self.cluster.node2.value = facts(50, holds={"gxmax": {"active": True}})
        snap = self.ctrl.snapshot()
        self.assertTrue(snap["gxmax"]["hold"])
        self.assertEqual(snap["runtimes"]["gx-reason"]["state"], "BLOCKED")
        self.assertEqual(self.ctrl.admission("gx-music", snap=snap)["code"], "gx_max_active")

    def test_maintenance_hold(self):
        self.set_svc(media={"resident_alias": None, "held_by": ""})
        self.cluster.node2.value = facts(100, holds={"maintenance": {"active": True}})
        snap = self.ctrl.snapshot()
        self.assertTrue(snap["maintenance"])
        self.assertEqual(snap["runtimes"]["gx-image"]["state"], "BLOCKED")
        self.assertTrue(self.ctrl.maintenance())
        self.assertEqual(self.ctrl.explain("gx-video")["code"], "maintenance")
        adm = self.ctrl.admission("gx-max", snap=snap)
        self.assertFalse(adm["allowed"])
        self.assertIn("Maintenance", adm["reason"])

    def test_local_maintenance_hold_file(self):
        self.assertFalse(self.ctrl.maintenance())
        (self.env.cfg.guard_dir / "node1.maintenance-hold").write_text("x")
        self.assertTrue(self.ctrl.maintenance())

    def test_admission_explains_what_to_unload(self):
        view = self.ctrl.admission("gx-video")
        self.assertFalse(view["allowed"])
        self.assertEqual(view["state"], "UNLOADED")
        self.assertEqual(view["blocking"], ["gx-image", "gx-reason", "gx-music"])
        self.assertEqual(view["actions"][0]["unload"], ["gx-image"])
        explained = self.ctrl.explain("gx-video")
        self.assertEqual(explained["reason"], "Waiting for gx-image to unload")
        self.assertEqual((explained["need_gib"], explained["reserve_gib"]), (102.0, 30.0))
        self.assertNotIn("eta", explained)
        self.assertNotIn("eta_seconds", explained)

    def test_admission_gxmax_lists_what_drains(self):
        view = self.ctrl.admission("gx-max")
        self.assertTrue(view["allowed"])
        self.assertEqual(view["code"], "takeover")
        self.assertEqual(set(view["will_drain"]), {"gx-mini", "gx-fast", "gx-reason", "gx-image", "gx-music"})
        self.assertEqual(view["nodes"]["node2"]["available_gib"], 50.0)

    def test_unknown_alias(self):
        with self.assertRaises(ResourceError) as cm:
            self.ctrl.admission("gx-nope")
        self.assertEqual(cm.exception.status, 404)

    def test_explain_engine_busy(self):
        self.set_svc(media={"resident_alias": "gx-image", "held_by": "image-1", "busy": True})
        self.assertEqual(self.ctrl.explain("gx-video")["code"], "engine_busy")

    def test_explain_pinned_blocker(self):
        self.cluster.node2.value = facts(10, pins={"gx-image": {}})
        self.set_svc(media={"resident_alias": "gx-image", "held_by": ""})
        out = self.ctrl.explain("gx-reason")
        self.assertIn("gx-image is pinned", out["next"])

    def test_compatibility_matrix(self):
        comp = self.ctrl.compatibility()
        self.assertEqual(len(comp["pairs"]), 21)
        self.assertEqual(comp["aliases"], list(rc.GENERATIVE))
        for p in comp["pairs"]:
            self.assertIn(p["verdict"], comp["legend"])
        # node2 capacity is capped at idle capacity + 4
        self.assertLessEqual(comp["capacity_gib"]["node2"], rc.IDLE_CAPACITY_GIB["node2"] + 4)
        self.cluster.node1.value = {"memory": {}}
        self.assertEqual(self.ctrl.compatibility()["capacity_gib"]["node1"], rc.IDLE_CAPACITY_GIB["node1"])


class ProfileTests(ControllerBase):
    def test_default_and_corrupt_profile_is_auto(self):
        self.assertEqual(self.ctrl.profile()["profile"], "auto")
        (self.env.cfg.guard_dir / "profile.json").write_text('{"profile": "root"}')
        self.assertEqual(self.ctrl.profile()["profile"], "auto")
        (self.env.cfg.guard_dir / "profile.json").write_text("{not json")
        self.assertEqual(self.ctrl.profile()["profile"], "auto")

    def test_unknown_profile(self):
        with self.assertRaises(ResourceError):
            self.ctrl.plan_profile("turbo")
        with self.assertRaises(ResourceError):
            self.ctrl.set_profile("turbo", user="admin")

    def test_set_simple_profile_writes_both_nodes_and_audits(self):
        out = self.ctrl.set_profile("media", user="admin", ip="10.0.0.1")
        self.assertEqual(out["profile"], "media")
        self.assertEqual(self.ctrl.profile()["profile"], "media")
        self.assertEqual(self.ctrl.profile()["by"], "admin")
        self.assertEqual(self.writes[0][0], "profile.json")
        self.assertEqual(json.loads(self.writes[0][1])["profile"], "media")
        self.assertEqual(self.audits[-1]["action"], "resources.profile")
        self.assertEqual(self.audits[-1]["previous"], "auto")
        self.assertFalse(any(p.name.endswith(".tmp") for p in self.env.cfg.guard_dir.iterdir()))

    def test_node2_write_failure_is_reported_not_fatal(self):
        self.writer_ok = False
        out = self.ctrl.set_profile("text", user="admin")
        self.assertTrue(any(s.startswith("WARNING") for s in out["steps"]))
        self.assertEqual(self.ctrl.profile()["profile"], "text")

    def test_max_needs_the_phrase_and_starts_the_takeover(self):
        plan = self.ctrl.plan_profile("max")
        self.assertTrue(plan["needs_confirm"])
        self.assertEqual(plan["confirm_phrase"], "gx-max")
        for bad in (None, True, "GX-MAX"):
            with self.assertRaises(ResourceError) as cm:
                self.ctrl.set_profile("max", user="admin", confirm=bad)
            self.assertEqual(cm.exception.status, 409)
        self.assertEqual(self.actions.submitted, [])
        out = self.ctrl.set_profile("max", user="admin", confirm="gx-max")
        self.assertEqual(self.actions.submitted, [("model.gx-max.load", "gx-max")])
        self.assertTrue(out["steps"][0].startswith("gx-max acquire started"))

    def test_leaving_max_releases_gxmax(self):
        self.ctrl._write_local("profile.json", {"profile": "max", "since": time.time()})
        self.cluster.gx = "ready"
        plan = self.ctrl.plan_profile("auto")
        self.assertIn("gx-max", plan["drains_now"])
        self.assertTrue(plan["needs_confirm"])
        with self.assertRaises(ResourceError):
            self.ctrl.set_profile("auto", user="admin")
        self.ctrl.set_profile("auto", user="admin", confirm=True)
        self.assertEqual(self.actions.submitted, [("model.gx-max.unload", True)])

    def test_playground_cannot_enter_maintenance(self):
        with self.assertRaises(ResourceError) as cm:
            self.ctrl.set_profile("maintenance", user="admin", source="playground")
        self.assertEqual(cm.exception.status, 403)
        self.assertEqual(self.writes, [])
        self.assertFalse((self.env.cfg.guard_dir / "profile.json").exists())

    def test_maintenance_enter_and_exit(self):
        self.set_svc(n2=[("gx-reason", "ready")], media={"resident_alias": None, "held_by": ""})
        plan = self.ctrl.plan_profile("maintenance")
        self.assertEqual(plan["drains_now"], ["gx-reason"])
        self.assertFalse(plan["needs_confirm"])
        out = self.ctrl.set_profile("maintenance", user="admin")
        hold = self.env.cfg.guard_dir / "node1.maintenance-hold"
        self.assertTrue(hold.exists())
        self.assertIn(("node2.maintenance-hold", "1"), self.writes)
        self.assertEqual(self.cluster.unloaded, ["gx-reason"])
        self.assertEqual(self.freed, 1)
        self.assertIn("gx-reason unloaded", out["steps"])
        self.assertTrue(self.ctrl.maintenance())
        out = self.ctrl.set_profile("auto", user="admin")
        self.assertFalse(hold.exists())
        self.assertIn(("node2.maintenance-hold", ""), self.writes)
        self.assertIn("gx10-01 maintenance hold removed", out["steps"])
        self.assertIn("Maintenance ends", " ".join(out["plan"]["conflicts"]))

    def test_maintenance_with_active_work_needs_confirmation(self):
        self.set_svc(n1=[("gx-fast", "starting")])
        with self.assertRaises(ResourceError) as cm:
            self.ctrl.set_profile("maintenance", user="admin")
        self.assertEqual(cm.exception.status, 409)

    def test_ssh_writer_refuses_other_files(self):
        with self.assertRaises(ResourceError):
            self.ctrl._ssh_write("../../etc/passwd", "x")


class PinTests(ControllerBase):
    def test_pin_node2_keeps_other_pins(self):
        self.cluster.node2.value = facts(80, pins={"gx-music": {"by": "a"}})
        self.assertEqual(self.ctrl.set_pin("gx-reason", True, user="admin"), {"alias": "gx-reason", "pinned": True})
        name, content = self.writes[-1]
        self.assertEqual(name, "pins.json")
        self.assertEqual(set(json.loads(content)), {"gx-music", "gx-reason"})
        self.ctrl.set_pin("gx-music", False, user="admin")
        self.assertEqual(json.loads(self.writes[-1][1]), {})
        self.assertEqual(self.audits[-1]["action"], "resources.unpin")

    def test_pin_refusals(self):
        for alias in ("gx-mini", "gx-fast", "gx-max", "gx-auto", "nope"):
            with self.assertRaises(ResourceError):
                self.ctrl.set_pin(alias, True, user="admin")
        self.writer_ok = False
        with self.assertRaises(ResourceError) as cm:
            self.ctrl.set_pin("gx-image", True, user="admin")
        self.assertEqual(cm.exception.status, 503)

    def test_control_routes_pin(self):
        self.assertTrue(self.ctrl.control("gx-video", "pin", user="admin")["pinned"])
        self.assertFalse(self.ctrl.control("gx-video", "unpin", user="admin")["pinned"])


class ControlTests(ControllerBase):
    def test_unknown_alias_and_unsupported_ops(self):
        with self.assertRaises(ResourceError) as cm:
            self.ctrl.control("gx-nope", "load", user="admin")
        self.assertEqual(cm.exception.status, 404)
        for alias, op in (("gx-auto", "load"), ("gx-image", "load_all"), ("gx-mini", "pin"),
                          ("gx-image", "drainx")):
            with self.assertRaises(ResourceError) as cm:
                self.ctrl.control(alias, op, user="admin")
            self.assertEqual(cm.exception.status, 400)

    def test_text_load_and_unload_go_through_the_action_runner(self):
        self.cluster.node2.value = facts(100)
        out = self.ctrl.control("gx-reason", "load", user="admin")
        self.assertEqual(out["started"]["action"], "model.gx-reason.load")
        self.ctrl.control("gx-mini", "unload", user="admin")
        self.assertEqual(self.actions.submitted[-1], ("model.gx-mini.unload", True))

    def test_gxmax_passes_the_confirmation_through(self):
        self.ctrl.control("gx-max", "load", user="admin", confirm="gx-max")
        self.ctrl.control("gx-max", "unload", user="admin")
        self.assertEqual(self.actions.submitted, [("model.gx-max.load", "gx-max"), ("model.gx-max.unload", True)])

    def test_blocked_load_raises_with_the_view(self):
        self.cluster.node2.value = facts(10)
        with self.assertRaises(AdmissionBlocked) as cm:
            self.ctrl.control("gx-reason", "load", user="admin")
        self.assertEqual(cm.exception.status, 409)
        self.assertEqual(cm.exception.view["code"], "insufficient_memory")
        self.assertEqual(self.actions.submitted, [])

    def test_unload_and_continue_frees_then_readmits(self):
        self.cluster.node2.value = facts(50)
        self.set_svc(media={"resident_alias": "gx-image", "held_by": ""})

        def media_free():
            self.cluster.node2.value = facts(107)
            self.set_svc(media={"resident_alias": None, "held_by": ""})
            return True, "freed"
        self.ctrl.media_free = media_free
        with mock.patch.object(rc.time, "sleep") as slept:
            with self.assertRaises(AdmissionBlocked):
                self.ctrl.control("gx-music", "load", user="admin")
            self.assertEqual(slept.call_count, 0)
            out = self.ctrl.control("gx-music", "load", user="admin", confirm="unload_and_continue")
        slept.assert_called_once_with(5)
        job_id = out["started"]["id"]
        deadline = time.time() + 5
        while self.ctrl.background_job(job_id)["state"] == "running" and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.music.calls, ["load"])
        freed = [a for a in self.audits if a["action"] == "resources.free.gx-image"]
        self.assertEqual(freed[0]["outcome"], "ok")
        self.assertEqual(freed[0]["reason"], "manual: load gx-music")

    def test_media_is_loaded_by_its_next_job(self):
        self.cluster.node2.value = facts(100)
        with self.assertRaises(ResourceError) as cm:
            self.ctrl.control("gx-video", "load", user="admin")
        self.assertIn("not available", str(cm.exception))

    def test_unload_generating_needs_confirmation(self):
        self.set_svc(n2=[("gx-reason", "ready")])
        self.ctrl._reason_activity = {"running": 2}
        self.assertEqual(self.ctrl.snapshot()["runtimes"]["gx-reason"]["state"], "GENERATING")
        with self.assertRaises(ResourceError) as cm:
            self.ctrl.control("gx-reason", "unload", user="admin")
        self.assertEqual(cm.exception.status, 409)
        self.ctrl.control("gx-reason", "unload", user="admin", confirm=True)
        self.assertEqual(self.actions.submitted[-1], ("model.gx-reason.unload", True))

    def test_media_unload_uses_the_router(self):
        self.assertTrue(self.ctrl.control("gx-image", "unload", user="admin")["done"])
        self.ctrl.media_free = lambda: (False, "busy")
        with self.assertRaises(ResourceError) as cm:
            self.ctrl.control("gx-video", "unload", user="admin")
        self.assertEqual(cm.exception.status, 409)
        self.assertEqual(self.audits[-1]["outcome"], "refused")

    def test_music_load_runs_in_the_background(self):
        self.cluster.node2.value = facts(100)
        out = self.ctrl.control("gx-music", "load", user="admin")
        job_id = out["started"]["id"]
        self.assertRegex(job_id, r"^[0-9a-f]{16}$")
        deadline = time.time() + 5
        while self.ctrl.background_job(job_id)["state"] == "running" and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.ctrl.background_job(job_id)["state"], "succeeded")
        self.assertEqual(self.music.calls, ["load"])
        self.assertEqual(self.ctrl.control("gx-music", "unload", user="admin")["done"], True)
        with self.assertRaises(ResourceError) as cm:
            self.ctrl.background_job("0" * 16)
        self.assertEqual(cm.exception.status, 404)


class SchedulingTests(ControllerBase):
    def setUp(self):
        super().setUp()
        self.cluster.node2.value = facts(50)
        self.set_svc(media={"resident_alias": "gx-image", "held_by": "", "busy": False})

    def test_gate_allows_when_it_fits(self):
        self.cluster.node2.value = facts(110)
        self.assertIsNone(self.ctrl.creative_gate("gx-video"))
        # 100 GiB is no longer enough for a cold video (72 + 30)
        self.cluster.node2.value = facts(100)
        self.assertEqual(self.ctrl.creative_gate("gx-video")["code"], "freeing")

    def test_gate_unloads_idle_music_for_a_video_with_if_idle(self):
        self.set_svc(media={"resident_alias": None, "held_by": "", "busy": False})
        self.music.state = "ready"
        self.cluster.node2.value = facts(88)
        out = self.ctrl.creative_gate("gx-video")
        self.assertEqual(out["code"], "freeing")
        self.assertEqual(self.music.calls, ["unload:if_idle"])
        self.assertIn("gx-music", out["reason"])

    def test_gate_waits_for_active_music_with_a_specific_reason(self):
        self.set_svc(media={"resident_alias": None, "held_by": "", "busy": False})
        self.music.state = "ready"
        self.music.queue = {"active": 1, "current_job": "mus-1"}
        self.cluster.node2.value = facts(88)
        out = self.ctrl.creative_gate("gx-video")
        self.assertEqual(out["reason"], "Waiting for gx-music to release enough gx10-02 memory")
        self.assertIn("working", out["next"])
        self.assertEqual((out["need_gib"], out["available_gib"], out["reserve_gib"]), (102.0, 88.0, 30.0))
        self.assertEqual(self.music.calls, [])

    def test_gate_waits_for_pinned_music(self):
        self.set_svc(media={"resident_alias": None, "held_by": "", "busy": False})
        self.music.state = "ready"
        self.cluster.node2.value = facts(88, pins={"gx-music": {"by": "admin"}})
        out = self.ctrl.creative_gate("gx-video")
        self.assertEqual(out["reason"], "Waiting for gx-music to release enough gx10-02 memory")
        self.assertIn("pinned", out["next"])
        self.assertEqual(self.music.calls, [])

    def test_keyframe_edit_is_terminal(self):
        self.cluster.node2.value = facts(116)
        out = self.ctrl.creative_gate("gx-video", "keyframe_edit")
        self.assertTrue(out["terminal"])
        self.assertEqual(out["code"], "exceeds_node")

    def test_gate_frees_idle_weights_in_auto(self):
        out = self.ctrl.creative_gate("gx-video")
        self.assertEqual(out["code"], "freeing")
        self.assertEqual(self.freed, 1)
        self.assertIn("making room for gx-video", self.audits[-1]["reason"])

    def test_gate_waits_in_media_profile_instead_of_evicting_media(self):
        self.ctrl._write_local("profile.json", {"profile": "media", "since": 1.0})
        out = self.ctrl.creative_gate("gx-video")
        self.assertEqual(out["code"], "insufficient_memory")
        self.assertEqual(self.freed, 0)

    def test_gate_in_maintenance_explains(self):
        self.cluster.node2.value = facts(120, holds={"maintenance": {"active": True}})
        self.assertEqual(self.ctrl.creative_gate("gx-image")["code"], "maintenance")

    def test_router_busy_means_wait_for_the_memory_decision(self):
        self.set_svc(media={"resident_alias": "gx-image", "held_by": "video-x", "busy": True})
        self.cluster.node2.value = facts(113)
        out = self.ctrl.creative_gate("gx-image")
        self.assertEqual(out["code"], "engine_busy")
        self.assertIn("video-x", out["detail"])

    def test_router_pending_growth_is_subtracted(self):
        self.set_svc(media={"resident_alias": "gx-video", "held_by": "", "busy": False,
                            "memory": {"pending_gib": 0.0, "warm_growth_gib": 14.0}})
        self.cluster.node2.value = facts(50)
        view = self.ctrl.admission("gx-video")
        self.assertEqual((view["need_gib"], view["allowed"]), (44.0, True))
        self.set_svc(media={"resident_alias": "gx-image", "held_by": "image-y", "busy": True,
                            "memory": {"pending_gib": 40.0}})
        self.cluster.node2.value = facts(100)
        self.assertEqual(self.ctrl.snapshot()["runtimes"]["gx-image"]["pending_gib"], 40.0)
        view = self.ctrl.admission("gx-music")
        self.assertEqual((view["pending_gib"], view["allowed"]), (40.0, False))

    def test_preemptable_rules(self):
        snap = {"pins": {"gx-image": {}}}
        plan = ["gx-image", "gx-reason", "gx-music", "gx-video"]
        self.assertEqual(self.ctrl.preemptable("auto", "gx-video", plan, snap), ["gx-reason", "gx-music", "gx-video"])
        self.assertEqual(self.ctrl.preemptable("text", "gx-video", plan, snap), ["gx-music", "gx-video"])
        self.assertEqual(self.ctrl.preemptable("maintenance", "gx-video", plan, snap), [])
        self.assertEqual(self.ctrl.preemptable("max", "gx-video", plan, snap), [])
        self.assertEqual(self.ctrl.preemptable("media", "gx-video", plan, snap), ["gx-reason", "gx-music"])
        self.assertEqual(self.ctrl.preemptable("music", "gx-video", plan, snap), ["gx-video"])
        self.assertEqual(self.ctrl.preemptable("music", "gx-music", plan, snap), ["gx-reason", "gx-video"])
        self.ctrl._reason_activity = {"last_active": time.time()}
        self.assertNotIn("gx-reason", self.ctrl.preemptable("auto", "gx-video", plan, snap))
        self.ctrl._reason_activity = {"running": 1, "last_active": 0}
        self.assertNotIn("gx-reason", self.ctrl.preemptable("media", "gx-video", plan, snap))

    def test_free_tenants_reports_each(self):
        done = self.ctrl.free_tenants(["gx-reason", "gx-mini", "gx-music"], user="scheduler", reason="t")
        self.assertEqual(done, ["gx-reason", "gx-music"])
        self.assertEqual(self.cluster.unloaded, ["gx-reason"])
        self.assertEqual(self.music.calls, ["unload:if_idle"])
        outcomes = {a["action"]: a["outcome"] for a in self.audits}
        self.assertEqual(outcomes["resources.free.gx-mini"], "failed")


class TickTests(ControllerBase):
    def test_max_profile_ends_when_gxmax_is_down(self):
        self.ctrl._write_local("profile.json", {"profile": "max", "since": time.time() - 120})
        self.ctrl.tick()
        prof = self.ctrl.profile()
        self.assertEqual((prof["profile"], prof["by"]), ("auto", "scheduler"))
        self.assertEqual(json.loads(self.writes[0][1])["profile"], "auto")

    def test_max_profile_stays_while_an_acquire_runs_or_is_recent(self):
        self.ctrl._write_local("profile.json", {"profile": "max", "since": time.time() - 120})
        self.actions.running_jobs = [{"action": "model.gx-max.load"}]
        self.ctrl.tick()
        self.assertEqual(self.ctrl.profile()["profile"], "max")
        self.actions.running_jobs = []
        self.ctrl._write_local("profile.json", {"profile": "max", "since": time.time()})
        self.ctrl.tick()
        self.assertEqual(self.ctrl.profile()["profile"], "max")

    def test_profile_is_reasserted_on_node2(self):
        self.ctrl._write_local("profile.json", {"profile": "text", "since": time.time()})
        self.cluster.node2.value = facts(90, profile={"profile": "auto"})
        self.ctrl.tick()
        self.assertEqual(json.loads(self.writes[-1][1])["profile"], "text")
        self.writes.clear()
        self.cluster.node2.value = facts(90, profile={"profile": "text"})
        self.ctrl.tick()
        self.assertEqual(self.writes, [])

    def test_reason_activity_resets_when_unloaded(self):
        self.ctrl._reason_activity = {"running": 3, "last_active": 1.0}
        self.ctrl.tick()
        self.assertEqual(self.ctrl._reason_activity, {})
        self.set_svc(n2=[("gx-reason", "ready")])
        self.ctrl.tick()  # offline: in-flight count unknown -> 0
        self.assertEqual(self.ctrl._reason_activity["running"], 0)
        self.assertIn("last_active", self.ctrl._reason_activity)


if __name__ == "__main__":
    unittest.main()
