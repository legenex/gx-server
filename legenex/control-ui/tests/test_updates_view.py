"""Hermetic updates_view tests: pin compare against fixtures, check-for-update
against a fake GitHub/HF, and the never-auto-update policy."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from support import TempEnv

from gx_control_ui.updates_view import UpdatesView

PINNED_COMMIT = "6f7d1590ad49a2b8995188e45d7b9db31e677452"
PINNED_REV = "8a27b35fc5b145fa05ee965c7d7b243b047915f7"


class PinBase(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv()
        self.uv = UpdatesView(self.env.cfg)

    def tearDown(self):
        self.env.cleanup()


class TestPinCompare(PinBase):
    def test_pins_and_drift_shape(self):
        out = self.uv.view()
        kinds = {p["kind"] for p in out["pins"]}
        self.assertEqual(kinds, {"runtime_commit", "image_digest", "model_revision"})
        commit_pin = next(p for p in out["pins"] if p["kind"] == "runtime_commit")
        self.assertEqual(commit_pin["pin"], PINNED_COMMIT)
        # the live submodule does not exist in the fixture world -> drift
        self.assertIsNone(commit_pin["live"])
        self.assertFalse(commit_pin["match"])
        self.assertIn(out["policy"], json.dumps(out, ensure_ascii=False))

    def test_live_commit_match(self):
        self.uv._submodule_commit = lambda: PINNED_COMMIT  # type: ignore[method-assign]
        out = self.uv.view()
        commit_pin = next(p for p in out["pins"] if p["kind"] == "runtime_commit")
        self.assertTrue(commit_pin["match"])
        self.assertEqual(out["drift"], [p for p in out["pins"] if p["kind"] != "runtime_commit"])

    def test_model_disk_manifest_match(self):
        spec = json.loads((self.env.root / "registry.json").read_text())["models"]["dsv41-flash-exl3-stock"]
        model_dir = Path(self.env.root / "files" / "models" / "stock")
        model_dir.mkdir(parents=True, exist_ok=True)
        (model_dir / ".gx-manifest.json").write_text(json.dumps({"revision": spec["revision"]}))
        reg = json.loads((self.env.root / "registry.json").read_text())
        reg["models"]["dsv41-flash-exl3-stock"]["path"] = str(model_dir)
        (self.env.root / "registry.json").write_text(json.dumps(reg))
        uv = UpdatesView(self.env.cfg)
        pin = next(p for p in uv.view()["pins"]
                   if p["kind"] == "model_revision" and p["name"] == "dsv41-flash-exl3-stock")
        self.assertTrue(pin["match"])
        rev = next(p for p in uv.view()["pins"]
                   if p["kind"] == "model_revision" and p["name"] == "dsv41-flash-exl3-uncensored")
        self.assertFalse(rev["match"])  # no manifest on disk -> cannot verify


class TestCheck(unittest.TestCase):
    """check() against a live-mode env; upstream probes are stubbed."""

    def setUp(self):
        self.env = TempEnv(offline=False)
        self.uv = UpdatesView(self.env.cfg)

    def tearDown(self):
        self.env.cleanup()

    def test_check_queries_upstream_and_reports_only(self):
        self.uv._hf_revisions = lambda repo: {"sha": "64ba41b6c916a587db06eae2e19b7845f7be6e6b"} \
            if "Mia-AiLab" in repo else {"sha": "ffffffffffffffffffffffffffffffffffffffff"}  # type: ignore[method-assign]
        out = self.uv.check()
        self.assertIn("check only", out["policy"])
        # a pinned revision matches; the drifted one is reported, not acted on
        rows = {r["name"]: r for r in out["results"] if r["kind"] == "huggingface"}
        self.assertTrue(rows["dsv41-flash-exl3-stock"]["match"])
        self.assertFalse(rows["dsv41-flash-exl3-uncensored"]["match"])
        self.assertEqual(len(out["drift"]), 1)
        # nothing anywhere suggests an update will be applied
        self.assertNotIn("will update", json.dumps(out))

    def test_check_offline_is_honest(self):
        env = TempEnv()  # offline
        self.addCleanup(env.cleanup)
        uv = UpdatesView(env.cfg)
        out = uv.check()
        self.assertFalse(out["results"][0]["checked"])
        self.assertEqual(out["results"][0]["name"], "offline mode")

    def test_check_result_is_cached(self):
        first = self.uv.check()
        second = self.uv.check()
        self.assertEqual(first["generated_at"], second["generated_at"])


if __name__ == "__main__":
    unittest.main()
