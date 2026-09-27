"""Hermetic Resource Control tests: the guard file protocol, profile changes,
gx-max admission explanation and pins — all against temp guard dirs."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from support import TempEnv

from gx_control_ui.actions import ActionRunner
from gx_control_ui.models import ResultLog
from gx_control_ui.resources import ResourceController, ResourceError
from gx_control_ui.services import Cluster


class ResourcesBase(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv()
        self.writes: list[tuple[str, str]] = []
        self.results = ResultLog(self.env.cfg.state_dir / "results.json")
        self.cluster = Cluster(self.env.cfg)
        self.actions = ActionRunner(self.env.cfg, self.cluster, self.results)
        self.rc = ResourceController(self.env.cfg, self.cluster, self.actions,
                                     node2_writer=self.fake_write)
        self.actions.maintenance = self.rc.maintenance

    def fake_write(self, name: str, content: str) -> bool:
        self.writes.append((name, content))
        return True

    def tearDown(self):
        self.env.cleanup()


class TestGuardProtocol(ResourcesBase):
    def test_profile_round_trip_and_guard_files(self):
        out = self.rc.set_profile("max", user="admin", confirm="gx-max")
        self.assertEqual(out["profile"], "max")
        data = json.loads((Path(self.env.cfg.guard_dir) / "profile.json").read_text())
        self.assertEqual(data["profile"], "max")
        self.assertEqual(data["by"], "admin")
        self.assertIn(("profile.json", json.dumps(data, sort_keys=True)), self.writes)

    def test_max_profile_confirmation_required(self):
        with self.assertRaises(ResourceError):
            self.rc.set_profile("max", user="admin")

    def test_maintenance_holds_written_and_cleared(self):
        self.rc.set_profile("maintenance", user="admin")
        self.assertTrue((Path(self.env.cfg.guard_dir) / "node1.maintenance-hold").exists())
        self.assertIn(("node2.maintenance-hold", "1"), self.writes)
        self.assertTrue(self.rc.maintenance())
        self.rc.set_profile("auto", user="admin")
        self.assertFalse((Path(self.env.cfg.guard_dir) / "node1.maintenance-hold").exists())
        self.assertFalse(self.rc.maintenance())

    def test_unknown_profile_refused(self):
        with self.assertRaises(ResourceError):
            self.rc.set_profile("media", user="admin")
        with self.assertRaises(ResourceError):
            self.rc.plan_profile("music")

    def test_pins(self):
        self.rc.set_pin("gx-max", True, user="admin")
        self.assertEqual(self.rc.pins()["gx-max"]["by"], "admin")
        self.rc.set_pin("gx-max", False, user="admin")
        self.assertEqual(self.rc.pins(), {})
        with self.assertRaises(ResourceError):
            self.rc.set_pin("gx-image", True, user="admin")


class TestAdmission(ResourcesBase):
    def test_admission_explains_the_v41_sizing(self):
        snap = self.rc.snapshot()
        view = self.rc.admission(snap)
        self.assertEqual(view["alias"], "gx-max")
        self.assertEqual(view["nodes"]["node1"]["need_gib"], 135.0)  # 105 + 30 reserve
        self.assertIn("resource guard", view["enforced_by"])
        # offline mode: no memory facts -> not admitted, honestly
        self.assertFalse(view["allowed"])

    def test_snapshot_shape(self):
        snap = self.rc.snapshot()
        for key in ("profile", "profiles", "serving_profiles", "maintenance", "gxmax", "nodes",
                    "queue", "pins", "reserve_gib"):
            self.assertIn(key, snap)
        self.assertEqual(snap["gxmax"]["sizing"]["rank_gib"], 105.0)
        self.assertIn("balanced", snap["serving_profiles"])
        self.assertIn("fast", snap["serving_profiles"])
        self.assertIsNone(snap["queue"]["queued"])  # offline: no fabricated queue


if __name__ == "__main__":
    unittest.main()
