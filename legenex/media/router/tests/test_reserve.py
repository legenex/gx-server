"""Router 2.4 (D-038): the locked 30 GiB reserve for gx-image / gx-video next to gx-music.

The rule under test: a job starts only when

    MemAvailable - gx-music's pending growth - the job's growth >= 30 GiB

and when it does not, an IDLE, unpinned gx-music engine is unloaded through
its supervisor (verified: engine unloaded, container gone, ledger clean,
memory back) or the job WAITS with a specific reason. Nothing here needs a
GPU, ComfyUI or a supervisor: MemAvailable is a file, the supervisor is a
fake that moves that file the way the real node does.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gx_media_router.config import Config  # noqa: E402
from gx_media_router.errors import ExceedsNodeError, InsufficientMemoryError  # noqa: E402
from gx_media_router.service import MediaService  # noqa: E402
from gx_media_router.tenants import MusicState, MusicTenant, _parse  # noqa: E402
from gx_media_router.uploads import InputStore, MediaInfo  # noqa: E402
from gx_media_router.workflows import WorkflowRegistry  # noqa: E402

from tests.test_router import FakeComfy  # noqa: E402

WORKFLOW_DIR = Path(__file__).resolve().parents[2] / "workflows"
T2V = "wan22-t2v-a14b-uncensored"
KEYFRAME = "wan22-v2v-keyframe-edit"
IMAGE = "qwen-image-2512-uncensored"


class Node:
    """gx10-02's MemAvailable and guard directory."""

    def __init__(self, root: Path, avail: float) -> None:
        self.meminfo = root / "meminfo"
        self.guard = root / "guard"
        self.guard.mkdir()
        self.lock = threading.Lock()
        self.set(avail)

    def set(self, gib: float) -> None:
        with self.lock:
            tmp = self.meminfo.with_suffix(".tmp")
            tmp.write_text(f"MemTotal: 127535340 kB\nMemAvailable: {int(gib * 1024 * 1024)} kB\n")
            tmp.replace(self.meminfo)

    def add(self, gib: float) -> None:
        self.set(self.get() + gib)

    def get(self) -> float:
        line = [x for x in self.meminfo.read_text().splitlines() if x.startswith("MemAvailable")][0]
        return int(line.split()[1]) / (1024 * 1024)

    def ledger(self, entries: dict) -> None:
        (self.guard / "node2-residency.json").write_text(json.dumps(entries))

    def write(self, name: str, data) -> None:
        (self.guard / name).write_text(data if isinstance(data, str) else json.dumps(data))


class FakeMusic(MusicTenant):
    """The gx-music supervisor as the router sees it."""

    def __init__(self, node: Node, *, engine: str = "ready", loaded_gib: float = 26.0, busy: bool = False,
                 active: int = 0, pending: float = 6.0, pinned: bool = False, key: bool = True) -> None:
        super().__init__("http://music.test", "")
        self.node = node
        self.engine, self.loaded, self.busy, self.active = engine, loaded_gib, busy, active
        self.pending, self.pinned, self.key = pending, pinned, key
        self.unload_calls = 0
        self.leave_ledger = False
        self.refuse_unload: str | None = None
        if engine in ("ready", "loading"):
            node.ledger({"gx-music": {"class": "medium", "estimated_gib": 32.0, "container": "gx-music"}})

    @property
    def can_unload(self) -> bool:
        return self.key

    def state(self, *, fresh: bool = True) -> MusicState:
        return MusicState(reachable=True, engine=self.engine, busy=self.busy, active_jobs=self.active,
                          pending_gib=self.pending if self.engine in ("ready", "loading") else 0.0,
                          loaded_gib=self.loaded if self.engine == "ready" else None, pinned=self.pinned)

    def unload_if_idle(self):
        self.unload_calls += 1
        if self.refuse_unload:
            return False, {"reason": self.refuse_unload, "status": 409}
        if self.busy or self.active:
            return False, {"reason": "a track is being generated", "status": 409}
        self.engine = "unloaded"
        self.node.add(self.loaded)
        if not self.leave_ledger:
            self.node.ledger({})
        return True, {"reason": "requested", "container_gone": True}


class ReserveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.node = Node(self.root, 114.0)
        (self.root / "in").mkdir()
        self.comfy = FakeComfy()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def service(self, music: FakeMusic | None = None, **cfg) -> MediaService:
        config = Config(api_key="k", guard_dir=str(self.node.guard), meminfo_path=str(self.node.meminfo),
                        resource_wait_seconds=cfg.pop("resource_wait_seconds", 30),
                        resource_retry_seconds=cfg.pop("resource_retry_seconds", 0.05),
                        eviction_settle_seconds=2.0, **cfg)
        svc = MediaService(config, self.comfy, WorkflowRegistry(WORKFLOW_DIR),
                           inputs=InputStore(self.root / "in"), music=music or MusicTenant(""))
        svc._settle_seconds = 0.3
        svc._settle_poll = 0.02
        svc._settle_flat_seconds = 0.1
        svc._held_measure_seconds = 0  # measured explicitly where a test needs it
        return svc

    def video(self, svc: MediaService, workflow: str = T2V):
        return svc.submit_video(workflow, {"prompt": "a lighthouse at dusk", "width": 640, "height": 640,
                                           "length": 33, "fps": 16, "seed": 1})

    def image(self, svc: MediaService):
        return svc.generate_image(IMAGE, {"prompt": "a cat", "width": 512, "height": 512, "seed": 1})

    def wait_for(self, predicate, timeout: float = 10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.02)
        self.fail("condition not reached")

    def done(self, job):
        return self.wait_for(lambda: job.status in ("completed", "failed") and job)

    # ------------------------------------------------------------ 1 coexist
    def test_1_coexistence_is_admitted_when_projected_headroom_keeps_the_reserve(self):
        # music loaded, 6 GiB of its generation growth still pending; 110 GiB available:
        # 110 - 6 - 72 = 32 >= 30 -> video runs NEXT TO music, nothing is unloaded
        self.node.set(110)
        music = FakeMusic(self.node)
        svc = self.service(music)
        job = self.done(self.video(svc))
        self.assertEqual(job.status, "completed")
        self.assertEqual(music.unload_calls, 0)
        self.assertEqual(music.engine, "ready")
        # an image next to music: 100 - 6 - 57 = 37
        self.node.set(100)
        self.comfy.frees = 0
        self.assertEqual(self.image(svc).status, "completed")
        self.assertEqual(music.unload_calls, 0)

    # --------------------------------------------------- 2 projected < 30
    def test_2_video_that_would_break_the_reserve_does_not_start(self):
        # the 18.6 GiB case: music loaded (88 GiB left) + a 72 GiB cold video
        self.node.set(88)
        music = FakeMusic(self.node, busy=True, active=1)
        svc = self.service(music)
        job = self.video(svc)
        waiting = self.wait_for(lambda: job.waiting)
        public = job.public()
        self.assertEqual((public["status"], public["phase"], public["gx_status"]), ("queued", "waiting", "waiting"))
        self.assertEqual(waiting["code"], "insufficient_memory")
        self.assertEqual(waiting["blocker"], "gx-music")
        self.assertEqual(waiting["reserve_gib"], 30.0)
        self.assertEqual(waiting["available_gib"], 88.0)
        self.assertEqual(waiting["required_gib"], 108.0)  # 72 + 6 pending + 30
        self.assertIn("Waiting for gx-music to release enough gx10-02 memory", waiting["reason"])
        self.assertIn("working", waiting["next"])
        self.assertEqual(self.comfy.submitted, [], "nothing reached ComfyUI")
        self.assertEqual(music.unload_calls, 0, "busy music is never unloaded")
        self.assertIn(job.id, json.dumps(svc.health()["waiting"]))
        self.assertTrue(svc.health()["last_refusal"]["cold"])
        # a synchronous image in the same situation is refused with the numbers
        self.node.set(60)
        with self.assertRaises(InsufficientMemoryError) as ctx:
            self.image(svc)
        body = ctx.exception.payload()["error"]
        self.assertEqual(body["code"], "insufficient_memory")
        self.assertEqual(body["details"]["required_gib"], 93.0)
        self.assertEqual(body["details"]["reserve_gib"], 30.0)

    # ------------------------------------------- 3 idle music is evicted
    def test_3_idle_music_is_unloaded_verified_then_video_runs(self):
        self.node.set(88)
        music = FakeMusic(self.node)
        svc = self.service(music)
        job = self.done(self.video(svc))
        self.assertEqual(job.status, "completed")
        self.assertEqual(music.unload_calls, 1)
        ev = svc.health()["last_eviction"]
        self.assertTrue(ev["requested"] and ev["engine_unloaded"] and ev["container_gone"] and ev["ledger_clean"])
        self.assertEqual(ev["mem_available_before_gib"], 88.0)
        self.assertEqual(ev["mem_available_after_gib"], 114.0)
        self.assertEqual(len(self.comfy.submitted), 1)

    def test_3b_an_unverified_unload_does_not_admit(self):
        self.node.set(88)
        music = FakeMusic(self.node)
        music.leave_ledger = True  # supervisor says unloaded, ledger still lists gx-music
        svc = self.service(music, resource_wait_seconds=30)
        job = self.video(svc)
        waiting = self.wait_for(lambda: job.waiting and "could not be verified" in job.waiting["reason"]
                                and job.waiting)
        self.assertEqual(self.comfy.submitted, [])
        self.assertFalse(svc.health()["last_eviction"]["ledger_clean"])
        self.assertIn("retries", waiting["next"])
        # the guard's own reconcile clears the ledger: the next check admits
        self.node.ledger({})
        self.assertEqual(self.done(job).status, "completed")

    def test_3c_eviction_never_happens_when_it_cannot_make_enough_room(self):
        # gx-reason holds most of the node: even without music the video does not fit
        self.node.set(40)
        music = FakeMusic(self.node)
        svc = self.service(music)
        job = self.video(svc)
        waiting = self.wait_for(lambda: job.waiting)
        self.assertIn("gx-reason", waiting["blocker"])
        time.sleep(0.2)
        self.assertEqual(music.unload_calls, 0)

    # ---------------------------------------------- 4 active music waits
    def test_4_active_music_is_not_interrupted_and_the_video_waits_then_runs(self):
        self.node.set(88)
        music = FakeMusic(self.node, busy=True, active=1)
        svc = self.service(music)
        job = self.video(svc)
        self.wait_for(lambda: job.waiting)
        time.sleep(0.2)
        self.assertEqual((music.unload_calls, music.engine), (0, "ready"))
        # the track finishes; the engine is idle now -> evicted -> video runs
        music.busy, music.active = False, 0
        self.assertEqual(self.done(job).status, "completed")
        self.assertEqual(music.unload_calls, 1)

    def test_4b_a_video_that_waits_too_long_fails_with_the_reason(self):
        self.node.set(88)
        music = FakeMusic(self.node, busy=True, active=1)
        svc = self.service(music, resource_wait_seconds=0)
        job = self.done(self.video(svc))
        self.assertEqual(job.status, "failed")
        public = job.public()
        self.assertEqual(public["error"]["code"], "insufficient_memory")
        self.assertIn("Gave up", public["error"]["message"])
        self.assertIn("gx-music", public["error"]["message"])
        self.assertNotIn("waiting", public)

    # ---------------------------------------------------- 5 pinned music
    def test_5_pinned_music_makes_the_video_wait(self):
        self.node.set(88)
        self.node.write("pins.json", {"gx-music": {"by": "admin"}})
        music = FakeMusic(self.node)
        svc = self.service(music)
        job = self.video(svc)
        waiting = self.wait_for(lambda: job.waiting)
        self.assertIn("pinned", waiting["next"])
        self.assertEqual(waiting["eviction"], "gx-music is pinned")
        time.sleep(0.2)
        self.assertEqual(music.unload_calls, 0)
        self.assertEqual(self.comfy.submitted, [])
        # unpinned: evicted and run
        self.node.write("pins.json", {})
        self.assertEqual(self.done(job).status, "completed")

    def test_5b_music_profile_keeps_music(self):
        self.node.set(88)
        self.node.write("profile.json", {"profile": "music"})
        music = FakeMusic(self.node)
        svc = self.service(music)
        job = self.video(svc)
        waiting = self.wait_for(lambda: job.waiting)
        self.assertIn("music profile", waiting["eviction"])
        time.sleep(0.2)
        self.assertEqual(music.unload_calls, 0)

    # ------------------------------------------------------ 6 maintenance
    def test_6_maintenance_blocks_new_and_queued_heavy_work(self):
        svc = self.service(FakeMusic(self.node, engine="unloaded"))
        self.node.write("node2.maintenance-hold", "x")
        with self.assertRaises(Exception) as ctx:
            self.video(svc)
        self.assertEqual(getattr(ctx.exception, "code", None), "maintenance")
        os.unlink(self.node.guard / "node2.maintenance-hold")
        # a video already queued when Maintenance starts waits instead of starting
        svc.slot.acquire("an image job", 1.0)
        job = self.video(svc)
        self.node.write("node2.maintenance-hold", "x")
        svc.slot.release()
        waiting = self.wait_for(lambda: job.waiting)
        self.assertEqual(waiting["code"], "maintenance")
        self.assertEqual(self.comfy.submitted, [])
        os.unlink(self.node.guard / "node2.maintenance-hold")
        self.assertEqual(self.done(job).status, "completed")

    # ------------------------------------------------------- 7 gx-max hold
    def test_7_gxmax_hold_is_never_run_into_and_music_is_left_to_its_drain(self):
        self.node.set(88)
        music = FakeMusic(self.node)
        svc = self.service(music)
        svc.slot.acquire("an image job", 1.0)
        job = self.video(svc)
        self.node.write("node2.gxmax-hold", "x")
        svc.slot.release()
        waiting = self.wait_for(lambda: job.waiting)
        self.assertEqual((waiting["code"], waiting["blocker"]), ("gx_max_active", "gx-max"))
        time.sleep(0.2)
        # the router does not unload music itself during a takeover: the gx-max
        # drain (node2-holds.sh -> supervisor) owns that
        self.assertEqual(music.unload_calls, 0)
        self.assertEqual(self.comfy.submitted, [])
        os.unlink(self.node.guard / "node2.gxmax-hold")
        self.assertEqual(self.done(job).status, "completed")

    # ---------------------------------------------- never-fits and pending
    def test_keyframe_edit_can_never_keep_the_reserve_and_is_refused_at_submit(self):
        svc = self.service()
        staged = svc.stage(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 32, MediaInfo("video", "mp4", "video/mp4"))
        with self.assertRaises(ExceedsNodeError) as ctx:
            svc.submit_video(KEYFRAME, {"prompt": "night", "input_video": staged}, staged=(staged,))
        err = ctx.exception
        self.assertEqual((err.status, err.code, err.retryable), (422, "exceeds_node_reserve", False))
        self.assertEqual(err.details["required_gib"], 137.0)
        self.assertIn("cannot run", err.message)
        self.assertFalse((self.root / "in" / staged).exists(), "staged source removed")
        self.assertEqual(self.comfy.submitted, [])

    def test_loading_music_counts_as_pending_and_is_not_evicted(self):
        self.node.set(110)
        music = FakeMusic(self.node, engine="loading", pending=32.0)
        svc = self.service(music)
        job = self.video(svc)
        waiting = self.wait_for(lambda: job.waiting)
        self.assertEqual(waiting["pending_gib"], 32.0)
        self.assertIn("loading", waiting["next"])
        self.assertEqual(music.unload_calls, 0)
        # the load materialises (110 -> 84) and nothing is pending any more:
        # 84 - 72 < 30 -> still waits, now the idle engine is evicted
        music.engine, music.pending = "ready", 6.0
        self.node.set(84)
        self.assertEqual(self.done(job).status, "completed")
        self.assertEqual(music.unload_calls, 1)

    def test_pending_growth_of_a_running_job_is_published_for_gx_music(self):
        svc = self.service()
        self.assertEqual(svc.health()["memory"]["pending_gib"], 0.0)
        started, release = threading.Event(), threading.Event()

        def slow_wait(prompt_id, **kw):
            started.set()
            release.wait(5)
            return FakeComfy.wait(self.comfy, prompt_id, **kw)

        self.comfy.wait = slow_wait
        job = self.video(svc)
        started.wait(5)
        self.assertEqual(svc.health()["memory"]["pending_gib"], 72.0)  # nothing materialised yet
        self.node.set(114 - 50)
        self.assertEqual(svc.health()["memory"]["pending_gib"], 22.0)
        release.set()
        self.done(job)
        self.assertEqual(svc.health()["memory"]["pending_gib"], 0.0)

    def test_unreadable_memory_refuses_instead_of_guessing(self):
        svc = self.service()
        svc.cfg = dataclasses.replace(svc.cfg, meminfo_path=str(self.root / "missing"))
        with self.assertRaises(InsufficientMemoryError) as ctx:
            self.image(svc)
        self.assertIn("cannot be read", ctx.exception.message)

    def test_warm_job_of_unknown_size_is_re_judged_cold_after_freeing_its_own_weights(self):
        svc = self.service()
        self.assertTrue(self.image(svc).cold_start)
        # weights resident, their size unknown; 60 GiB available is short for 57 + 30
        self.node.set(60)
        frees = self.comfy.frees

        def comfy_free(**kw):
            FakeComfy.free(self.comfy, **kw)
            self.node.set(110)  # the freed weights come back

        self.comfy.free = comfy_free
        job = self.image(svc)
        self.assertTrue(job.cold_start)
        self.assertEqual(self.comfy.frees, frees + 1)

    def test_warm_job_with_measured_resident_size_needs_only_its_growth(self):
        svc = self.service()
        svc._held_measure_seconds = 1.0
        svc._held_settle_delay = 0.0
        self.node.set(114)

        def comfy_wait(prompt_id, **kw):
            self.node.set(114 - 57)  # peak during the job
            result = FakeComfy.wait(self.comfy, prompt_id, **kw)
            self.node.set(114 - 45)  # activations released, weights stay
            return result

        self.comfy.wait = comfy_wait
        self.assertTrue(self.image(svc).cold_start)
        self.wait_for(lambda: svc.health()["memory"]["resident_held_gib"] is not None)
        self.assertEqual(svc.health()["memory"]["resident_held_gib"], 45.0)
        self.assertEqual(svc.health()["memory"]["warm_growth_gib"], 12.0)
        # 69 GiB available: warm needs 12 + 30 = 42 -> runs without reloading
        frees = self.comfy.frees
        job = self.image(svc)
        self.assertFalse(job.cold_start)
        self.assertEqual(self.comfy.frees, frees)

    def test_held_measurement_is_discarded_when_music_changed_meanwhile(self):
        music = FakeMusic(self.node, engine="unloaded")
        svc = self.service(music)
        svc._held_measure_seconds = 0.5
        svc._held_settle_delay = 0.2

        def comfy_wait(prompt_id, **kw):
            result = FakeComfy.wait(self.comfy, prompt_id, **kw)
            music.engine = "ready"  # music loaded while we measured
            return result

        self.comfy.wait = comfy_wait
        self.image(svc)
        time.sleep(1.0)
        self.assertIsNone(svc.health()["memory"]["resident_held_gib"])

    def test_a_restarted_router_frees_what_comfyui_still_holds(self):
        # ComfyUI kept the video weights across a router recreate: 46 GiB left,
        # but the new router's record says nothing is resident
        self.node.set(46)
        svc = self.service()
        frees = self.comfy.frees

        def comfy_free(**kw):
            FakeComfy.free(self.comfy, **kw)
            self.node.set(114)

        self.comfy.free = comfy_free
        svc.slot.acquire("busy", 1.0)
        self.assertFalse(svc.reconcile_residency(), "never while a job holds the slot")
        svc.slot.release()
        self.assertTrue(svc.reconcile_residency())
        self.assertEqual(self.comfy.frees, frees + 1)
        self.assertFalse(svc.reconcile_residency(), "only once")
        self.assertEqual(self.done(self.video(svc)).status, "completed")

    def test_the_first_job_after_a_start_frees_first(self):
        self.node.set(46)
        svc = self.service()

        def comfy_free(**kw):
            FakeComfy.free(self.comfy, **kw)
            self.node.set(114)

        self.comfy.free = comfy_free
        self.assertEqual(self.done(self.video(svc)).status, "completed")
        self.assertFalse(svc._residency_unknown)
        self.assertEqual(svc.reconcile_residency(), False)


class ConfigAndParsingTests(unittest.TestCase):
    def test_reserve_cannot_be_lowered_below_30(self):
        with mock.patch.dict(os.environ, {"GX_MEDIA_RESERVE_GIB": "20"}):
            with self.assertRaises(SystemExit):
                Config.from_env()
        with mock.patch.dict(os.environ, {"GX_MEDIA_FOOTPRINT_VIDEO_GIB": "40"}):
            with self.assertRaises(SystemExit):
                Config.from_env()
        with mock.patch.dict(os.environ, {"GX_MEDIA_RESERVE_GIB": "35", "GX_MEDIA_MUSIC_URL": "http://x:1/"}):
            cfg = Config.from_env()
        self.assertEqual((cfg.reserve_gib, cfg.music_url, cfg.pin_reserve_gib), (35.0, "http://x:1", 35.0))

    def test_health_body_parsing(self):
        s = _parse({"engine": "ready", "busy": False, "active_jobs": 2, "pinned": True,
                    "memory": {"pending_gib": 5.5, "loaded_gib": 25.9, "estimate_gib": 32}})
        self.assertEqual((s.engine, s.active_jobs, s.pending_gib, s.loaded_gib, s.pinned),
                         ("ready", 2, 5.5, 25.9, True))
        # an older supervisor without memory numbers: a load counts in full
        old = _parse({"engine": "loading", "active_jobs": 1})
        self.assertEqual(old.pending_gib, 32.0)
        self.assertFalse(_parse("junk").reachable)

    def test_disabled_and_unreachable_supervisor(self):
        self.assertIsNone(MusicTenant("").state())
        dead = MusicTenant("http://127.0.0.1:9", timeout=0.2).state()
        self.assertFalse(dead.reachable)
        self.assertFalse(MusicTenant("http://127.0.0.1:9", "/nonexistent").can_unload)
        ok, body = MusicTenant("http://127.0.0.1:9").unload_if_idle()
        self.assertFalse(ok)
        self.assertIn("key", body["reason"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
