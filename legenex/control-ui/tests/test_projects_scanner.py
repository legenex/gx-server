"""Hermetic projects_scanner tests over a tmp projects fixture."""

from __future__ import annotations

import unittest
from pathlib import Path

from support import TempEnv

from gx_control_ui.projects_scanner import ProjectsScanner


class ProjectsBase(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv()
        self.root = Path(self.env.cfg.projects_root)
        # project 1: a git repo
        self.git_dir = self.root / "repo-app"
        self.git_dir.mkdir(parents=True)
        self._git(self.git_dir, "init", "-q")
        self._git(self.git_dir, "config", "user.email", "t@example")
        self._git(self.git_dir, "config", "user.name", "t")
        (self.git_dir / "code.py").write_text("x = 1\n")
        self._git(self.git_dir, "add", "-A")
        self._git(self.git_dir, "commit", "-qm", "initial commit")
        self._git(self.git_dir, "remote", "add", "origin", "https://example/repo-app.git")
        # project 2: a plain directory
        (self.root / "plain-dir").mkdir()
        (self.root / "plain-dir" / "notes.txt").write_text("hello\n")
        # hidden and files are skipped
        (self.root / ".hidden").mkdir()
        (self.root / "README.md").write_text("not a project\n")

    @staticmethod
    def _git(path: Path, *args: str) -> None:
        import subprocess
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, timeout=15)

    def tearDown(self):
        self.env.cleanup()


class TestScan(ProjectsBase):
    def test_scan_finds_git_and_plain_projects(self):
        sc = ProjectsScanner(self.env.cfg)
        out = sc.scan()
        names = [p["name"] for p in out["projects"]]
        self.assertIn("repo-app", names)
        self.assertIn("plain-dir", names)
        self.assertNotIn(".hidden", names)
        self.assertNotIn("README.md", names)  # files are skipped, only dirs
        repo = next(p for p in out["projects"] if p["name"] == "repo-app")
        self.assertTrue(repo["is_git"])
        self.assertIn(repo["branch"], ("master", "main"))
        self.assertEqual(repo["remote"], "https://example/repo-app.git")
        self.assertFalse(repo["dirty"])
        self.assertEqual(repo["last_commit"]["subject"], "initial commit")
        self.assertEqual(repo["children"], [])
        plain = next(p for p in out["projects"] if p["name"] == "plain-dir")
        self.assertFalse(plain["is_git"])
        self.assertIsNone(plain.get("branch"))

    def test_dirty_flag(self):
        (self.git_dir / "new.py").write_text("y = 2\n")
        sc = ProjectsScanner(self.env.cfg)
        out = sc.scan()
        repo = next(p for p in out["projects"] if p["name"] == "repo-app")
        self.assertTrue(repo["dirty"])
        self.assertEqual(repo["dirty_files"], 1)

    def test_scheduler_attribution(self):
        sc = ProjectsScanner(self.env.cfg)
        snap = {"available": True, "queue": [
            {"project": "repo-app", "state": "queued"},
            {"project": "repo-app", "state": "active"},
            {"project": "plain-dir", "state": "active"},
            {"project": "unknown", "state": "active"},
        ]}
        out = sc.scan(snap)
        repo = next(p for p in out["projects"] if p["name"] == "repo-app")
        self.assertEqual(repo["scheduler"], {"active": 1, "queued": 1})
        plain = next(p for p in out["projects"] if p["name"] == "plain-dir")
        self.assertEqual(plain["scheduler"], {"active": 1, "queued": 0})
        # no scheduler -> no fabricated attribution
        out = sc.scan({"available": False})
        repo = next(p for p in out["projects"] if p["name"] == "repo-app")
        self.assertIsNone(repo["scheduler"])

    def test_size_refresh_is_cached_and_on_demand(self):
        sc = ProjectsScanner(self.env.cfg)
        rec = sc.refresh_size("repo-app")
        self.assertGreaterEqual(rec["bytes"], 0)
        self.assertIn("repo-app", (self.env.cfg.state_dir / "project-sizes.json").read_text())
        out = sc.scan()
        repo = next(p for p in out["projects"] if p["name"] == "repo-app")
        self.assertEqual(repo["size_bytes"], rec["bytes"])
        with self.assertRaises(ValueError):
            sc.refresh_size("../escape")
        with self.assertRaises(ValueError):
            sc.refresh_size("nonexistent")


if __name__ == "__main__":
    unittest.main()
