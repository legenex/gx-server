"""Hermetic filemanager tests: allowlist enforcement, traversal and symlink
escapes, trash + restore, purge token."""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from support import TempEnv

from gx_control_ui.filemanager import FileManager, FileManagerError, PURGE_TOKEN


class FileManagerBase(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv()
        self.root = self.env.root
        self.fm = FileManager(self.env.cfg)
        # a project tree inside the allowed roots
        self.projects = Path(self.env.cfg.file_roots[0])
        self.proj = self.projects / "alpha"
        (self.proj / "src").mkdir(parents=True)
        (self.proj / "src" / "main.py").write_text("print('hi')\n")
        (self.proj / "README.md").write_text("# alpha\n")
        # protected backups
        self.backups = Path(self.env.cfg.file_roots[1]) / "GX"
        self.backups.mkdir(parents=True)
        (self.backups / "protected.zip").write_bytes(b"PK-ZIP")

    def tearDown(self):
        self.env.cleanup()

    def delete(self, path: str):
        return self.fm.delete(path, user="tester")

    def resolve(self, path, **kw):
        return self.fm.resolve(path, **kw)


class TestAllowlist(FileManagerBase):
    def test_browse_inside_a_root(self):
        data = self.fm.browse(str(self.proj))
        names = [e["name"] for e in data["entries"]]
        self.assertEqual(sorted(names), ["README.md", "src"])

    def test_traversal_attempts_refused(self):
        secrets_dir = str(self.env.root / "secrets")
        for bad in ("/etc/passwd", "../../../etc/passwd", self.projects / "../../..",
                    self.projects / "alpha/../../../../../etc", "/srv", "/",
                    secrets_dir, self.proj / "src" / ".." / ".." / ".." / ".." / "secrets"):
            with self.assertRaises(FileManagerError, msg=repr(str(bad))) as ctx:
                self.fm.browse(str(bad))
            # absolute escape -> 403; a relative path -> 400 invalid_path.
            # Both are refusals; neither lets the browser name a target.
            self.assertIn(ctx.exception.status, (400, 403), str(bad))

    def test_absolute_paths_outside_every_root_refused(self):
        with self.assertRaises(FileManagerError):
            self.fm.resolve("/home/legenex/Documents/Projects", for_write=True)
        with self.assertRaises(FileManagerError):
            self.fm.resolve("/srv/models/../..", for_write=True)

    def test_trash_is_not_browsable(self):
        with self.assertRaises(FileManagerError) as ctx:
            self.fm.browse(str(self.env.cfg.trash_root))
        self.assertEqual(ctx.exception.code, "trash_locked")

    def test_symlink_escape_refused(self):
        # a symlink inside an allowed root pointing outside it
        victim = self.env.root / "secrets" / "auth.json"
        victim.write_text("{}")
        link = self.proj / "escape"
        link.symlink_to(victim)
        with self.assertRaises(FileManagerError):
            self.fm.browse(str(link))
        with self.assertRaises(FileManagerError):
            self.resolve(str(link), for_write=True)

    def test_symlink_inside_roots_is_fine(self):
        link = self.projects / "to-alpha"
        link.symlink_to(self.proj, target_is_directory=True)
        self.assertEqual(self.fm.browse(str(link))["path"], str(self.proj.resolve()))

    def test_writes_refuse_protected_backups(self):
        with self.assertRaises(FileManagerError) as ctx:
            self.fm.delete(str(self.backups / "protected.zip"), user="t")
        self.assertEqual(ctx.exception.code, "protected")
        with self.assertRaises(FileManagerError):
            self.fm.rename(str(self.backups / "protected.zip"), "new.zip")

    def test_writes_refuse_active_model_paths(self):
        model_dir = Path(self.env.cfg.file_roots[3]) / "dsv41" / "model"
        model_dir.mkdir(parents=True)
        (model_dir / "weights.bin").write_bytes(b"x" * 16)
        # point the fixture registry's model path at the real test dir
        reg = json.loads((self.env.root / "registry.json").read_text())
        reg["models"]["dsv41-flash-exl3-stock"]["path"] = str(model_dir)
        (self.env.root / "registry.json").write_text(json.dumps(reg))
        fm = FileManager(self.env.cfg)  # fresh cache reads the updated registry
        with self.assertRaises(FileManagerError) as ctx:
            fm.delete(str(model_dir / "weights.bin"), user="t")
        self.assertEqual(ctx.exception.code, "model_path")
        # reading is still allowed
        self.assertEqual(fm.download(str(model_dir / "weights.bin")).name, "weights.bin")

    def test_preview_is_text_only_and_capped(self):
        big = self.proj / "big.txt"
        big.write_text("A" * 200_000)
        data = self.fm.preview(str(big))
        self.assertEqual(len(data["text"]), 64 * 1024)
        self.assertTrue(data["truncated"])
        binary = self.proj / "blob.bin"
        binary.write_bytes(b"\x00\x01\x02\x03")
        with self.assertRaises(FileManagerError) as ctx:
            self.fm.preview(str(binary))
        self.assertEqual(ctx.exception.status, 415)

    def test_upload_rejects_bad_names_and_streams_to_disk(self):
        def writer(tmp: Path) -> int:
            tmp.write_bytes(b"hello upload")
            return 12
        entry = self.fm.upload(str(self.proj), "note.txt", writer)
        self.assertEqual((entry["name"], entry["size"]), ("note.txt", 12))
        for bad in ("../evil", ".hidden", "a/b", "", "x" * 300, None):
            with self.assertRaises(FileManagerError, msg=repr(bad)):
                self.fm.upload(str(self.proj), bad, writer)

    def test_rename_and_move_and_mkdir(self):
        entry = self.fm.rename(str(self.proj / "README.md"), "INTRO.md")
        self.assertEqual(entry["name"], "INTRO.md")
        dest = Path(self.env.cfg.file_roots[2]) / "shelf"
        dest.mkdir()
        moved = self.fm.move(str(self.proj / "INTRO.md"), str(dest))
        self.assertTrue((dest / "INTRO.md").is_file())
        made = self.fm.mkdir(str(self.proj / "newdir"))
        self.assertEqual(made["type"], "dir")
        with self.assertRaises(FileManagerError):
            self.fm.mkdir(str(self.proj / "newdir"))

    def test_search_finds_files_under_a_root_only(self):
        (self.proj / "src" / "unique-token-file.py").write_text("x")
        hits = self.fm.search(str(self.projects), "unique-token")
        self.assertEqual(len(hits["results"]), 1)
        with self.assertRaises(FileManagerError):
            self.fm.search("/etc", "passwd")


class TestTrash(FileManagerBase):
    def test_delete_moves_to_trash_with_manifest(self):
        target = self.proj / "src" / "main.py"
        size = target.stat().st_size
        rec = self.delete(str(target))
        self.assertFalse(target.exists())
        self.assertEqual(rec["original"], str(target))
        self.assertEqual(rec["size"], size)
        manifest = self.fm._manifest()
        self.assertEqual(len(manifest), 1)
        self.assertEqual(manifest[0]["id"], rec["id"])
        self.assertIn("manifest_sha", manifest[0])
        # the trashed file is physically in the trash root
        trash_root = Path(self.env.cfg.trash_root)
        self.assertTrue((trash_root / rec["trashed_name"]).is_file())

    def test_delete_refuses_outside_roots(self):
        with self.assertRaises(FileManagerError):
            self.delete("/etc/hosts")

    def test_restore_round_trip(self):
        rec = self.delete(str(self.proj / "README.md"))
        out = self.fm.restore(rec["id"], user="t")
        self.assertTrue(out["restored"])
        self.assertTrue((self.proj / "README.md").is_file())
        self.assertEqual(self.fm._manifest(), [])
        # the trash entry is gone
        self.assertFalse((Path(self.env.cfg.trash_root) / rec["trashed_name"]).exists())

    def test_restore_refuses_when_original_exists(self):
        rec = self.delete(str(self.proj / "README.md"))
        (self.proj / "README.md").write_text("recreated")
        with self.assertRaises(FileManagerError) as ctx:
            self.fm.restore(rec["id"], user="t")
        self.assertEqual(ctx.exception.status, 409)

    def test_restore_bad_id(self):
        with self.assertRaises(FileManagerError):
            self.fm.restore("not-an-id")
        with self.assertRaises(FileManagerError):
            self.fm.restore("deadbeef" * 2)

    def test_purge_requires_the_typed_token(self):
        self.delete(str(self.proj / "README.md"))
        with self.assertRaises(FileManagerError) as ctx:
            self.fm.purge(user="t", confirm="yes")
        self.assertIn(PURGE_TOKEN, str(ctx.exception))
        out = self.fm.purge(user="t", confirm=PURGE_TOKEN)
        self.assertEqual(out["count"], 1)
        self.assertTrue(out["purged"])
        self.assertEqual(self.fm._manifest(), [])
        self.assertEqual(list(Path(self.env.cfg.trash_root).glob("*-*-README.md")), [])

    def test_purge_without_confirm_argument(self):
        self.delete(str(self.proj / "README.md"))
        # direct call with no confirm: the action layer enforces the token; the
        # manager itself purges only when told to via the token or no argument
        out = self.fm.purge(user="t")
        self.assertEqual(out["count"], 1)


if __name__ == "__main__":
    unittest.main()
