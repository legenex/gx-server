"""Named feature migrations on the application database (D-040)."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from support import UI_DIR  # noqa: F401  (puts the package on sys.path)

from gx_control_ui.media_library import MIGRATIONS_DIR, MediaLibrary, MediaTools, NewAsset


class NamedMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.lib = MediaLibrary(self.root / "media", MediaTools(enabled=False))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_shipped_migrations_apply_once(self) -> None:
        names = [m["name"] for m in self.lib.migrations()]
        shipped = sorted(f.name for f in MIGRATIONS_DIR.glob("*.sql"))
        self.assertEqual(names, shipped)
        again = MediaLibrary(self.root / "media", MediaTools(enabled=False))
        self.assertEqual([m["name"] for m in again.migrations()], shipped)

    def test_out_of_order_arrival_is_not_skipped(self) -> None:
        extra = self.root / "extra"
        extra.mkdir()
        (extra / "090_late.sql").write_text("CREATE TABLE zz_late (id TEXT PRIMARY KEY)")
        self.assertEqual(self.lib._apply_named_migrations(extra), ["090_late.sql"])
        (extra / "080_early.sql").write_text("-- lands after 090\nCREATE TABLE zz_early (id TEXT PRIMARY KEY)")
        self.assertEqual(self.lib._apply_named_migrations(extra), ["080_early.sql"])
        self.assertTrue((self.root / "media" / "metadata" / "library.pre-080_early.db").exists())

    def test_failed_migration_rolls_back(self) -> None:
        extra = self.root / "bad"
        extra.mkdir()
        (extra / "099_bad.sql").write_text("CREATE TABLE zz_ok (id TEXT);\nTHIS IS NOT SQL")
        with self.assertRaises(sqlite3.Error):
            self.lib._apply_named_migrations(extra)
        with self.lib.connect() as con:
            self.assertIsNone(con.execute("SELECT name FROM sqlite_master WHERE name='zz_ok'").fetchone())
            self.assertIsNone(con.execute("SELECT 1 FROM schema_migrations WHERE name='099_bad.sql'").fetchone())

    def test_provenance_columns_round_trip(self) -> None:
        asset = self.lib.add(NewAsset(type="audio", ext="wav", operation="tts", data=b"RIFF" + b"\0" * 64,
                                      flow_id="flow_x", flow_run_id="run_y", flow_node_id="n1",
                                      source_kind="voice_take", source_ref="take_1"))
        self.assertEqual((asset["flow_id"], asset["flow_node_id"], asset["source_kind"]),
                         ("flow_x", "n1", "voice_take"))


if __name__ == "__main__":
    unittest.main()
