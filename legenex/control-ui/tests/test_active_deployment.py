"""D-053 review L4: the Control Center shows the ACTIVE deployment's model,
not the landing alias binding."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401 - puts the package on sys.path

from gx_control_ui import models

REGISTRY = Path(__file__).resolve().parents[2] / "models" / "registry.json"


class TestActiveDeployment(unittest.TestCase):
    def setUp(self):
        self.reg = json.loads(REGISTRY.read_text())
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write_pointer(self, name):
        p = Path(self.tmp.name) / "orchestrator" / "active-deployment.json"
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps({"deployment": name}))

    def test_without_a_switch_the_rollback_is_shown(self):
        self.assertEqual(models.active_deployment(self.reg, self.tmp.name), "dsv41-rollback")
        self.assertEqual(models.production_model_id(self.reg, self.tmp.name), "dsv41-flash-exl3-uncensored")

    def test_after_a_switch_the_primary_is_shown(self):
        self.write_pointer("dsv4-flash-primary")
        self.assertEqual(models.production_model_id(self.reg, self.tmp.name), "dsv4-flash-0731-abliterated")

    def test_unknown_pointer_falls_back_to_the_registry(self):
        self.write_pointer("nope")
        self.assertEqual(models.active_deployment(self.reg, self.tmp.name), "dsv41-rollback")

    def test_pre_d053_registry_uses_the_alias(self):
        legacy = {k: v for k, v in self.reg.items() if k not in ("deployments", "initial_active_deployment")}
        self.assertEqual(models.production_model_id(legacy, self.tmp.name), "dsv41-flash-exl3-uncensored")


if __name__ == "__main__":
    unittest.main()
