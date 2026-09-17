"""Router 2.3 cluster policy (D-036): holds, pins, truthful residency.

The central safety property is that a free requested by another tenant
(gx-music through `free_node`, gx-reason's start command) resets the
router's resident-model bookkeeping, so the NEXT image or video job is judged
cold (57/72 GiB + the 30 GiB reserve) and never warm.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gx_media_router.config import Config  # noqa: E402
from gx_media_router.errors import InsufficientMemoryError, PolicyBlockedError  # noqa: E402
from gx_media_router.policy import Policy  # noqa: E402
from gx_media_router.service import MediaService  # noqa: E402
from gx_media_router.uploads import InputStore  # noqa: E402
from gx_media_router.workflows import WorkflowRegistry  # noqa: E402

from tests.test_router import FakeComfy  # noqa: E402

WORKFLOW_DIR = Path(__file__).resolve().parents[2] / "workflows"


def meminfo(path: Path, gib: float) -> None:
    path.write_text(f"MemTotal: 127535340 kB\nMemAvailable: {int(gib * 1024 * 1024)} kB\n")


class PolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_no_directory_means_no_rules(self):
        p = Policy("")
        self.assertIsNone(p.block())
        self.assertEqual(p.pinned(), [])
        self.assertFalse(p.state()["guard_dir_mounted"])

    def test_maintenance_blocks(self):
        (self.dir / "node2.maintenance-hold").write_text("x")
        block = Policy(str(self.dir)).block()
        self.assertEqual(block.code, "maintenance")
        self.assertIn("Maintenance", block.message)

    def test_fresh_gxmax_hold_blocks_and_stale_does_not(self):
        hold = self.dir / "node2.gxmax-hold"
        hold.write_text("x")
        self.assertEqual(Policy(str(self.dir)).block().code, "gx_max_active")
        old = time.time() - 3600
        os.utime(hold, (old, old))
        self.assertIsNone(Policy(str(self.dir)).block())

    def test_pins_are_read_and_validated(self):
        (self.dir / "pins.json").write_text(json.dumps({"gx-image": {"by": "admin"}, "gx-video": "junk",
                                                        "gx-reason": {}}))
        self.assertEqual(Policy(str(self.dir)).pinned(), ["gx-image"])
        (self.dir / "pins.json").write_text("not json")
        self.assertEqual(Policy(str(self.dir)).pinned(), [])


class ServicePolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.guard = root / "guard"
        self.guard.mkdir()
        self.meminfo = root / "meminfo"
        meminfo(self.meminfo, 110)
        cfg = Config(api_key="k", guard_dir=str(self.guard), meminfo_path=str(self.meminfo),
                     idle_free_seconds=600)
        self.comfy = FakeComfy()
        self.svc = MediaService(cfg, self.comfy, WorkflowRegistry(WORKFLOW_DIR), inputs=InputStore(root / "in"))
        self.svc._settle_seconds = 0.0

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def image(self, prompt: str = "a cat"):
        return self.svc.generate_image("qwen-image-2512-uncensored", {"prompt": prompt, "width": 512,
                                                                      "height": 512, "seed": 1})

    def test_new_jobs_refused_in_maintenance_before_anything_runs(self):
        (self.guard / "node2.maintenance-hold").write_text("x")
        with self.assertRaises(PolicyBlockedError) as ctx:
            self.image()
        self.assertEqual(ctx.exception.code, "maintenance")
        self.assertEqual(ctx.exception.status, 503)
        self.assertEqual(self.comfy.submitted, [])
        with self.assertRaises(PolicyBlockedError):
            self.svc.submit_video("wan22-t2v-a14b-uncensored", {"prompt": "x"})

    def test_free_request_makes_the_next_job_cold(self):
        job = self.image()
        self.assertTrue(job.cold_start)
        self.assertEqual(self.svc.health()["resident_alias"], "gx-image")
        warm = self.image("again")
        self.assertFalse(warm.cold_start, "same weights stay warm")
        # gx-music / gx-reason hand-over through the router
        result = self.svc.free_now()
        self.assertTrue(result["freed"])
        self.assertEqual(self.svc.health()["resident_models"], [])
        self.assertIsNone(self.svc.health()["resident_alias"])
        # 80 GiB available: a cold image needs 57 + 30 GiB, so it must be refused.
        meminfo(self.meminfo, 80)
        with self.assertRaises(InsufficientMemoryError) as ctx:
            self.image("after free")
        self.assertIn("so 87 GiB must be available", ctx.exception.message)
        refusal = self.svc.health()["last_refusal"]
        self.assertTrue(refusal["cold"])
        self.assertEqual(refusal["need_gib"], 87.0)
        self.assertIn("gx-reason", ctx.exception.message)

    def test_refused_cold_job_is_not_remembered_as_resident(self):
        meminfo(self.meminfo, 20)
        with self.assertRaises(InsufficientMemoryError):
            self.image()
        self.assertEqual(self.svc.health()["resident_models"], [])
        meminfo(self.meminfo, 100)
        self.assertTrue(self.image().cold_start)

    def test_idle_free_respects_a_pin_while_memory_allows(self):
        self.image()
        self.svc._last_activity -= 3600
        (self.guard / "pins.json").write_text(json.dumps({"gx-image": {"by": "admin"}}))
        self.assertTrue(self.svc.pin_honoured())
        self.assertFalse(self.svc.free_if_idle())
        # memory falls under the reserve: the pin is suspended and the idle free runs
        meminfo(self.meminfo, 25)
        self.assertFalse(self.svc.pin_honoured())
        self.assertTrue(self.svc.free_if_idle())
        self.assertEqual(self.svc.health()["resident_models"], [])

    def test_pin_never_overrides_maintenance(self):
        self.image()
        (self.guard / "pins.json").write_text(json.dumps({"gx-image": {}}))
        (self.guard / "node2.maintenance-hold").write_text("x")
        # no idle wait either: maintenance frees idle weights at once
        self.assertTrue(self.svc.free_if_idle())

    def test_unpinned_idle_timer_unchanged(self):
        self.image()
        self.assertFalse(self.svc.free_if_idle(), "not idle long enough")
        self.svc._last_activity -= 601
        self.assertTrue(self.svc.free_if_idle())

    def test_health_reports_policy_and_memory(self):
        h = self.svc.health()
        self.assertEqual(h["version"], "2.4.0")
        self.assertTrue(h["policy"]["guard_dir_mounted"])
        self.assertEqual(h["policy"]["profile"], "auto")
        self.assertEqual(h["memory"]["reserve_gib"], 30.0)
        self.assertEqual(h["memory"]["footprint_gib"], {"image": 57.0, "video": 72.0, "keyframe_edit": 107.0})
        self.assertEqual(h["memory"]["need_gib"]["video"], 102.0)
        self.assertEqual(h["memory"]["need_gib"]["keyframe_edit"], 137.0)
        self.assertEqual(h["memory"]["pending_gib"], 0.0)
        self.assertIsNone(h["tenants"]["gx-music"])
        self.assertEqual(h["waiting"], [])
        self.assertAlmostEqual(h["memory"]["available_gib"], 110.0, places=0)


if __name__ == "__main__":
    unittest.main()
