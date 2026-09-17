"""Measured-footprint sync (plt.md section 7): parse, validate, record; never invent."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from support import REPO

from gx_control_ui.footprints import measured_footprint, parse, sync

GOOD = ("FOOTPRINT gx-voice node=gx10-02 cold_gib=14.5 resident_gib=11.2 startup_s=38 measured=2026-09-17 "
        "evidence=/srv/logs/acceptance/build-v3/voi/load.log")


class ParseTests(unittest.TestCase):
    def test_valid_line_and_last_wins(self):
        text = "\n".join(["notes", GOOD, "`" + GOOD.replace("14.5", "15") + "`"])
        self.assertEqual(parse(text)["gx-voice"]["cold_gib"], 15.0)
        self.assertEqual(parse(GOOD)["gx-voice"]["evidence"], "/srv/logs/acceptance/build-v3/voi/load.log")

    def test_invalid_lines_are_ignored(self):
        bad = [
            GOOD.replace("gx-voice", "gx-image"),
            GOOD.replace("gx10-02", "gx10-03"),
            GOOD.replace("cold_gib=14.5", "cold_gib=abc"),
            GOOD.replace("resident_gib=11.2", "resident_gib=20"),     # resident above cold
            GOOD.replace("cold_gib=14.5", "cold_gib=500"),           # above node size
            GOOD.replace("/srv/logs/acceptance", "/etc"),           # evidence outside the evidence tree
            GOOD.replace("measured=2026-09-17", "measured=yesterday"),
            GOOD + " ; rm -rf /",
            "FOOTPRINT gx-voice cold_gib=1",
        ]
        for line in bad:
            self.assertEqual(parse(line), {}, line)

    def test_display_never_invents(self):
        self.assertEqual(measured_footprint("gx-call", {}), {"measured": False, "label": "not measured yet"})
        self.assertEqual(measured_footprint("gx-call", None)["label"], "not measured yet")
        fp = measured_footprint("gx-voice", {"measured_footprint": parse(GOOD)["gx-voice"]})
        self.assertTrue(fp["measured"])
        self.assertEqual(fp["resident_gib"], 11.2)
        self.assertEqual(measured_footprint("gx-music", {"memory": "24-28 GiB"})["summary"], "24-28 GiB")


class SyncTests(unittest.TestCase):
    def test_sync_writes_only_registered_aliases(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "legenex" / "models").mkdir(parents=True)
            (root / "coordination" / "build-v3").mkdir(parents=True)
            shutil.copy(REPO / "legenex" / "models" / "registry.json", root / "legenex" / "models" / "registry.json")
            reg_path = root / "legenex" / "models" / "registry.json"
            reg = json.loads(reg_path.read_text())
            reg["aliases"]["gx-voice"] = {"node": "gx10-02"}
            reg["aliases"].pop("gx-call", None)
            reg_path.write_text(json.dumps(reg, indent=2))
            (root / "coordination" / "build-v3" / "voi.md").write_text(GOOD + "\n")
            (root / "coordination" / "build-v3" / "cal.md").write_text(GOOD.replace("gx-voice", "gx-call") + "\n")
            out = sync(root)
            self.assertEqual(sorted(out["changed"]), ["gx-voice"])
            after = json.loads(reg_path.read_text())
            self.assertEqual(after["aliases"]["gx-voice"]["measured_footprint"]["resident_gib"], 11.2)
            self.assertNotIn("gx-call", after["aliases"])
            self.assertEqual(after["aliases"]["gx-music"], reg["aliases"]["gx-music"])
            self.assertEqual(sync(root)["changed"], {})


if __name__ == "__main__":
    unittest.main()
