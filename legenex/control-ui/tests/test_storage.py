"""Hermetic tests for Storage & Cleanup (D-037).

storage_scan.py hard-codes the node paths (/srv/..., ~/.cache). To run it
without touching the real filesystem, the module's `os` and `shutil` are
replaced by a small shim that maps every absolute path into a temporary
directory (and maps results back), so the scanner sees a virtual /srv. Docker
and process listing are replaced by fakes; nothing is ever executed.
"""

from __future__ import annotations

import os
import posixpath
import shutil
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from support import TempEnv

from gx_control_ui import storage_scan as ss
from gx_control_ui.storage import (GIB, StorageError, StorageManager, _expand, health, install_preflight)

DAY = 86400.0
VHOME = "/home/tester"


# ------------------------------------------------------------ virtual fs
class VFS:
    """Maps absolute virtual paths to <tmp>/<path> and back."""

    def __init__(self, root: str) -> None:
        self.root = os.path.realpath(root)

    def real(self, p) -> str:
        p = os.fspath(p)
        if p.startswith("/") and not (p == self.root or p.startswith(self.root + "/")):
            return self.root + p
        return p

    def virt(self, p: str) -> str:
        if p == self.root:
            return "/"
        if p.startswith(self.root + "/"):
            return p[len(self.root):]
        return "/__outside__" + p

    # -- helpers for tests
    def write(self, vpath: str, data: bytes = b"x" * 5000, age: float = 0.0) -> str:
        real = Path(self.real(vpath))
        real.parent.mkdir(parents=True, exist_ok=True)
        real.write_bytes(data)
        if age:
            t = time.time() - age
            os.utime(real, (t, t))
        return vpath

    def mkdir(self, vpath: str, age: float = 0.0) -> str:
        real = Path(self.real(vpath))
        real.mkdir(parents=True, exist_ok=True)
        if age:
            t = time.time() - age
            os.utime(real, (t, t))
        return vpath

    def exists(self, vpath: str) -> bool:
        return os.path.lexists(self.real(vpath))

    def shim(self) -> tuple[types.SimpleNamespace, types.SimpleNamespace]:
        vfs = self

        class Entry:
            def __init__(self, e):
                self._e = e
                self.name = e.name
                self.path = vfs.virt(e.path)

            def stat(self, follow_symlinks=True):
                return self._e.stat(follow_symlinks=follow_symlinks)

            def is_symlink(self):
                return self._e.is_symlink()

            def is_dir(self, follow_symlinks=True):
                return self._e.is_dir(follow_symlinks=follow_symlinks)

        class Scan:
            def __init__(self, p):
                self._it = os.scandir(vfs.real(p))

            def __iter__(self):
                return (Entry(e) for e in self._it)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self._it.close()

        def walk(top, *a, **kw):
            for root, dirs, files in os.walk(vfs.real(top), *a, **kw):
                yield vfs.virt(root), dirs, files

        path = types.SimpleNamespace(
            join=posixpath.join, basename=posixpath.basename, normpath=posixpath.normpath,
            expanduser=lambda p: p.replace("~", VHOME, 1),
            isdir=lambda p: os.path.isdir(vfs.real(p)),
            islink=lambda p: os.path.islink(vfs.real(p)),
            lexists=lambda p: os.path.lexists(vfs.real(p)),
            realpath=lambda p: vfs.virt(os.path.realpath(vfs.real(p))),
        )
        fake_os = types.SimpleNamespace(
            path=path, fspath=os.fspath,
            lstat=lambda p: os.lstat(vfs.real(p)),
            scandir=Scan, walk=walk,
            listdir=lambda p: os.listdir(vfs.real(p)),
            unlink=lambda p: os.unlink(vfs.real(p)),
        )
        fake_shutil = types.SimpleNamespace(
            rmtree=lambda p, *a, **kw: shutil.rmtree(vfs.real(p), *a, **kw),
            disk_usage=lambda p: shutil.disk_usage(vfs.real(p)),
        )
        return fake_os, fake_shutil


class ScanBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vfs = VFS(self._tmp.name)
        fake_os, fake_shutil = self.vfs.shim()
        protected = (
            "/srv/projects/gx-cluster/secrets", "/srv/projects/gx-cluster/state",
            "/srv/projects/gx-cluster/media/metadata", "/srv/projects/gx-cluster/backups",
            f"{VHOME}/Documents/Projects/Server/gx-cluster", "/srv/models/music/acestep/checkpoints",
            "/srv/models/music-data/db", "/srv/logs/gx-git-sync", "/srv/logs/gx-control-ui/audit.log",
            "/srv/models/manifests",
        )
        roots = dict(ss.ROOTS, home_cache=f"{VHOME}/.cache")
        cats = tuple((c, tuple(f"{VHOME}/.cache" if p.endswith("/.cache") else p for p in prefixes))
                     for c, prefixes in ss.CATEGORY_PREFIXES)
        self.run_calls: list[list[str]] = []
        self.run_rc = 0
        self.patches = [
            mock.patch.object(ss, "os", fake_os),
            mock.patch.object(ss, "shutil", fake_shutil),
            mock.patch.object(ss, "HOME", VHOME),
            mock.patch.object(ss, "ROOTS", roots),
            mock.patch.object(ss, "BASE_PROTECTED", protected),
            mock.patch.object(ss, "CATEGORY_PREFIXES", cats),
            mock.patch.object(ss, "_run", self.fake_run),
            mock.patch.object(ss, "docker_state", lambda: dict(self.docker)),
            mock.patch.object(ss, "active_writers", lambda: list(self.writers)),
        ]
        for p in self.patches:
            p.start()
        self.docker = {"ok": False}
        self.writers: list[str] = []
        for d in ("/srv/models", "/srv/cache", "/srv/projects", "/srv/logs", f"{VHOME}/.cache"):
            self.vfs.mkdir(d)

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self._tmp.cleanup()

    def fake_run(self, args, timeout=60.0, input_text=None):
        self.run_calls.append(list(args))
        return self.run_rc, "", "boom" if self.run_rc else ""

    def ctx(self, **kw):
        base = {"node": "node1", "protect": [], "why": {}, "images": [], "protect_names": {},
                "known_containers": [], "mounts": [], "writers": []}
        base.update(kw)
        return base

    def by_target(self, cands):
        return {c["target"]: c for c in cands}


# ------------------------------------------------------------- scanner
class HelperTests(unittest.TestCase):
    def test_inside(self):
        self.assertTrue(ss.inside("/srv/models/a", "/srv/models"))
        self.assertTrue(ss.inside("/srv/models", "/srv/models/"))
        self.assertTrue(ss.inside("/srv/models/a/../b", "/srv/models"))
        self.assertFalse(ss.inside("/srv/models2", "/srv/models"))
        self.assertFalse(ss.inside("/srv/models/../cache", "/srv/models"))

    def test_parse_size(self):
        self.assertEqual(ss.parse_size("1.5GB"), 1_500_000_000)
        self.assertEqual(ss.parse_size("512MiB"), 512 * 2**20)
        self.assertEqual(ss.parse_size("10 kB"), 10_000)
        self.assertEqual(ss.parse_size("42"), 42)
        for bad in ("", None, "abc", "GB"):
            self.assertEqual(ss.parse_size(bad), 0, bad)

    def test_candidate_shape(self):
        c = ss.candidate("path", "/srv/x", "safe", 12.7, "why", name="x", category="logs", mtime=5.9)
        self.assertEqual((c["bytes"], c["mtime"], c["class"]), (12, 5, "safe"))
        self.assertIsNone(ss.candidate("path", "/x", "safe", 1, "r", name="x", category="c")["mtime"])


class ProtectionTests(ScanBase):
    def test_base_and_context_protection(self):
        self.assertIn("required by the cluster", ss.protection("/srv/projects/gx-cluster/secrets/x", self.ctx()))
        # a parent of a protected path is protected too
        self.assertIsNotNone(ss.protection("/srv/projects/gx-cluster", self.ctx()))
        ctx = self.ctx(protect=["/srv/models/gguf/mini"], why={"/srv/models/gguf/mini": "gx-mini current"})
        self.assertIn("gx-mini current", ss.protection("/srv/models/gguf/mini/model.gguf", ctx))
        self.assertIsNone(ss.protection("/srv/models/gguf/other", ctx))

    def test_mounts(self):
        ctx = self.ctx(mounts=["/srv/models", "/srv/models/music-data/uploads"])
        # inside a broad mount: judged by the other rules
        self.assertIsNone(ss.protection("/srv/models/gguf/old", ctx))
        self.assertIn("mounted", ss.protection("/srv/models/music-data/uploads/a.wav", ctx))
        # removing the path would remove a mount source
        ctx = self.ctx(mounts=["/srv/cache/torch/inductor"])
        self.assertIn("mounted", ss.protection("/srv/cache/torch", ctx))
        self.assertIsNone(ss.protection("/srv/cache/triton", ctx))

    def test_active_writers(self):
        ctx = self.ctx(writers=["/srv/models/staging/new"])
        self.assertIn("active download", ss.protection("/srv/models/staging/new/part", ctx))
        self.assertIn("active download", ss.protection("/srv/models/staging", ctx))
        self.assertIsNone(ss.protection("/srv/models/staging/other", ctx))

    def test_media_tree_names(self):
        self.vfs.write("/srv/models/video/loras/keep.safetensors")
        self.vfs.write("/srv/models/video/loras/sub/deep.safetensors")
        ctx = self.ctx(protect_names={"keep.safetensors": "used by media workflow t2v",
                                      "deep.safetensors": "gx-video checkpoint"})
        self.assertIn("used by media workflow t2v", ss.protection("/srv/models/video/loras/keep.safetensors", ctx))
        self.assertIn("contains deep.safetensors", ss.protection("/srv/models/video/loras/sub", ctx))
        self.assertIsNone(ss.protection("/srv/models/video/loras/other.safetensors", ctx))
        # names only protect inside the media trees
        self.assertIsNone(ss.protection("/srv/models/gguf/keep.safetensors", ctx))

    def test_root_of(self):
        self.vfs.write("/srv/models/gguf/a.gguf")
        self.assertEqual(ss.root_of("/srv/models/gguf/a.gguf"), "/srv/models")
        self.assertEqual(ss.root_of(f"{VHOME}/.cache/pip"), f"{VHOME}/.cache")
        self.assertIsNone(ss.root_of("/etc/passwd"))
        self.assertIsNone(ss.root_of("/srv/other"))
        # a symlink that resolves outside its root is refused
        os.symlink(self.vfs.real("/srv/logs"), self.vfs.real("/srv/models/escape"))
        self.assertIsNone(ss.root_of("/srv/models/escape"))


class ClassifyTests(ScanBase):
    def build_tree(self):
        v = self.vfs
        v.write("/srv/projects/gx-cluster/media/tmp/old.bin", age=7 * 3600)
        v.write("/srv/projects/gx-cluster/media/tmp/new.bin")
        v.write("/srv/models/comfy-input/old.png", age=2 * DAY)
        v.write("/srv/models/comfy-temp/fresh.png", age=3600)
        v.write("/srv/logs/app/app.log.1", age=20 * DAY)
        v.write("/srv/logs/app/app.log.2", age=DAY)
        v.write("/srv/logs/app/app.log", age=30 * DAY)
        v.write("/srv/logs/gx-git-sync/sync.log.1", age=30 * DAY)
        v.write("/srv/models/staging/modelA/w.safetensors.incomplete", age=2 * DAY)
        v.mkdir("/srv/models/staging/.cache")
        v.write("/srv/models/gguf/unused/m.gguf")
        v.write("/srv/models/gguf/current/m.gguf")
        os.symlink(v.real("/srv/models/gguf/current"), v.real("/srv/models/gguf/link"))
        v.write(f"{VHOME}/.cache/pip/wheel.whl")
        v.write("/srv/cache/hf/blob")
        v.write(f"{VHOME}/.cache/huggingface/hub/models--org--name/blob")
        v.write("/srv/projects/gx-cluster/secrets/token")
        v.write("/srv/models/music-data/jobs/j1/track.wav")
        v.write("/srv/projects/gx-music-staging/evidence.txt")
        v.mkdir("/srv/logs/acceptance/run-old")
        v.write("/srv/logs/acceptance/run-old/log.txt", age=10 * DAY)
        os.utime(v.real("/srv/logs/acceptance/run-old"), (time.time() - 10 * DAY,) * 2)
        v.write("/srv/logs/acceptance/run-new/log.txt")
        v.write(f"{VHOME}/Documents/Projects/Server/gx-cluster/legenex/control-ui/test-results/t.zip",
                age=3 * DAY)
        os.utime(v.real(f"{VHOME}/Documents/Projects/Server/gx-cluster/legenex/control-ui/test-results"),
                 (time.time() - 3 * DAY,) * 2)

    def test_classification(self):
        self.build_tree()
        ctx = self.ctx(protect=["/srv/models/gguf/current"], why={"/srv/models/gguf/current": "gx-mini current"})
        c = self.by_target(ss.classify(ctx, {"ok": False}, time.time()))
        safe = {t for t, x in c.items() if x["class"] == "safe"}
        review = {t for t, x in c.items() if x["class"] == "review"}
        protected = {t for t, x in c.items() if x["class"] == "protected"}
        self.assertEqual(safe, {
            "/srv/projects/gx-cluster/media/tmp/old.bin",
            "/srv/models/comfy-input/old.png",
            "/srv/logs/app/app.log.1",
            f"{VHOME}/.cache/pip",
            "/srv/models/staging/modelA/w.safetensors.incomplete",
            f"{VHOME}/Documents/Projects/Server/gx-cluster/legenex/control-ui/test-results",
        })
        self.assertEqual(review, {
            "/srv/models/staging/modelA", "/srv/models/gguf/unused", "/srv/cache/hf",
            f"{VHOME}/.cache/huggingface/hub/models--org--name", "/srv/projects/gx-music-staging",
            "/srv/logs/acceptance/run-old",
        })
        self.assertIn("/srv/models/gguf/current", protected)
        self.assertIn("/srv/projects/gx-cluster/secrets", protected)
        self.assertIn("/srv/logs/gx-git-sync", protected)
        # nothing inside a protected tree, no symlink, nothing fresh
        for target in c:
            self.assertFalse(target.startswith("/srv/projects/gx-cluster/secrets/"), target)
            self.assertFalse(target.startswith("/srv/logs/gx-git-sync/"), target)
        for absent in ("/srv/models/gguf/link", "/srv/projects/gx-cluster/media/tmp/new.bin",
                       "/srv/models/comfy-temp/fresh.png", "/srv/logs/app/app.log.2", "/srv/logs/app/app.log",
                       "/srv/logs/acceptance/run-new", "/srv/models/staging/.cache",
                       "/srv/models/music-data/jobs"):
            self.assertNotIn(absent, c)
        self.assertEqual(c["/srv/projects/gx-cluster/media/tmp/old.bin"]["category"], "temporary_uploads")
        self.assertGreater(c["/srv/projects/gx-cluster/media/tmp/old.bin"]["bytes"], 0)
        self.assertEqual(c["/srv/models/gguf/current"]["category"], "models")
        self.assertEqual(len(c), len(ss.classify(ctx, {"ok": False}, time.time())))  # no duplicates

    def test_node2_offers_music_job_copies(self):
        self.vfs.write("/srv/models/music-data/jobs/j1/track.wav")
        c = self.by_target(ss.classify(self.ctx(node="node2"), {"ok": False}, time.time()))
        self.assertEqual(c["/srv/models/music-data/jobs"]["class"], "review")

    def test_protected_wins_over_a_suggestion(self):
        self.vfs.write("/srv/models/gguf/old/m.gguf")
        ctx = self.ctx(writers=["/srv/models/gguf/old"])
        c = self.by_target(ss.classify(ctx, {"ok": False}, time.time()))
        self.assertEqual(c["/srv/models/gguf/old"]["class"], "protected")
        self.assertIn("active download", c["/srv/models/gguf/old"]["reason"])

    def test_docker_objects(self):
        docker = {
            "ok": True, "build_cache_bytes": 5 * GIB, "running_images": ["sha256:run"],
            "used_images": ["sha256:run", "sha256:stopped"],
            "images": [
                {"id": "sha256:run", "repo": "gx-comfyui", "tag": "sm121", "size": "20GB"},
                {"id": "sha256:ref", "repo": "postgres", "tag": "16-alpine", "size": "300MB"},
                {"id": "sha256:stopped", "repo": "old/tool", "tag": "1", "size": "1GB"},
                {"id": "sha256:dangling", "repo": "<none>", "tag": "<none>", "size": "2GB"},
                {"id": "sha256:other", "repo": "random/image", "tag": "x", "size": "3GB"},
            ],
            "containers": [
                {"id": "c1", "name": "gx-comfyui", "running": True},
                {"id": "c2", "name": "gx-music", "running": False},
                {"id": "c3", "name": "musing_turing", "running": False},
                {"id": "c4", "name": "proj-web-1", "running": False, "compose": "proj"},
            ],
        }
        ctx = self.ctx(images=["postgres:16-alpine"], known_containers=["gx-music"])
        c = self.by_target(ss.classify(ctx, docker, time.time()))
        self.assertEqual(c["builder"]["class"], "safe")
        self.assertEqual(c["builder"]["bytes"], 5 * GIB)
        self.assertEqual({k: c[f"sha256:{k}"]["class"] for k in ("run", "ref", "stopped", "dangling", "other")},
                         {"run": "protected", "ref": "protected", "stopped": "review", "dangling": "safe",
                          "other": "review"})
        self.assertEqual(c["sha256:dangling"]["bytes"], 2_000_000_000)
        self.assertNotIn("c1", c)
        self.assertEqual((c["c2"]["class"], c["c3"]["class"], c["c4"]["class"]), ("review", "safe", "review"))

    def test_docker_unavailable_offers_nothing(self):
        cands = ss.classify(self.ctx(), {"ok": False, "build_cache_bytes": 10}, time.time())
        self.assertFalse(any(c["kind"].startswith("docker") for c in cands))

    def test_category_of(self):
        self.assertEqual(ss.category_of("/srv/models/staging/x"), "staging")
        self.assertEqual(ss.category_of("/srv/models/comfy-output/x"), "generated_media")
        self.assertEqual(ss.category_of(f"{VHOME}/.cache/pip"), "caches")
        self.assertEqual(ss.category_of("/srv/logs/x"), "logs")
        self.assertEqual(ss.category_of("/opt/x"), "other")


class DeleteTests(ScanBase):
    def test_deletes_a_safe_file(self):
        target = self.vfs.write("/srv/projects/gx-cluster/media/tmp/old.bin", age=7 * 3600)
        res = ss.delete_item({"kind": "path", "target": target}, self.ctx(), {"ok": False}, time.time())
        self.assertTrue(res["ok"], res)
        self.assertGreater(res["freed"], 0)
        self.assertFalse(self.vfs.exists(target))

    def test_deletes_a_safe_directory(self):
        self.vfs.write(f"{VHOME}/.cache/pip/a/b.whl")
        res = ss.delete_item({"kind": "path", "target": f"{VHOME}/.cache/pip"}, self.ctx(), {"ok": False},
                             time.time())
        self.assertTrue(res["ok"], res)
        self.assertFalse(self.vfs.exists(f"{VHOME}/.cache/pip"))

    def test_nothing_inside_the_git_checkout_is_offered(self):
        # Fixed: Playwright traces inside the checkout used to be offered as SAFE
        # although delete_item always refuses paths outside ROOTS.
        rel = f"{VHOME}/Documents/Projects/Server/gx-cluster/legenex/control-ui/test-results"
        self.vfs.write(f"{rel}/trace.zip", age=3 * DAY)
        os.utime(self.vfs.real(rel), (time.time() - 3 * DAY,) * 2)
        offered = self.by_target(ss.classify(self.ctx(), {"ok": False}, time.time()))
        self.assertNotIn(rel, offered)
        res = ss.delete_item({"kind": "path", "target": rel}, self.ctx(), {"ok": False}, time.time())
        self.assertFalse(res["ok"])

    def test_review_needs_allow_review(self):
        self.vfs.write("/srv/models/gguf/old/m.gguf")
        item = {"kind": "path", "target": "/srv/models/gguf/old"}
        res = ss.delete_item(item, self.ctx(), {"ok": False}, time.time())
        self.assertFalse(res["ok"])
        self.assertIn("now classified review", res["error"])
        self.assertTrue(self.vfs.exists("/srv/models/gguf/old/m.gguf"))
        res = ss.delete_item(dict(item, allow_review=True), self.ctx(), {"ok": False}, time.time())
        self.assertTrue(res["ok"], res)

    def test_became_protected_after_the_scan(self):
        target = self.vfs.write("/srv/models/comfy-input/old.png", age=2 * DAY)
        res = ss.delete_item({"kind": "path", "target": target}, self.ctx(writers=[target]), {"ok": False},
                             time.time())
        self.assertFalse(res["ok"])
        self.assertIn("now classified protected", res["error"])
        self.assertTrue(self.vfs.exists(target))

    def test_modified_since_the_scan(self):
        target = self.vfs.write("/srv/models/comfy-input/old.png", age=2 * DAY)
        item = {"kind": "path", "target": target, "expect": {"mtime": int(time.time() - 3 * DAY)}}
        res = ss.delete_item(item, self.ctx(), {"ok": False}, time.time())
        self.assertFalse(res["ok"])
        self.assertIn("modified since", res["error"])
        self.assertTrue(self.vfs.exists(target))

    def test_not_a_candidate(self):
        for item in ({"kind": "path", "target": "/srv/models/gguf/nothing"},
                     {"kind": "path", "target": "/etc/passwd"},
                     {"kind": "path", "target": "/srv/projects/gx-cluster/secrets"},
                     {"kind": "shell", "target": "rm -rf /"},
                     {}):
            res = ss.delete_item(item, self.ctx(), {"ok": False}, time.time())
            self.assertFalse(res["ok"], item)
        self.vfs.write("/srv/projects/gx-cluster/secrets/token")
        res = ss.delete_item({"kind": "path", "target": "/srv/projects/gx-cluster/secrets"}, self.ctx(),
                             {"ok": False}, time.time())
        self.assertFalse(res["ok"])
        self.assertTrue(self.vfs.exists("/srv/projects/gx-cluster/secrets/token"))
        self.assertEqual(self.run_calls, [])

    def test_docker_image_and_container_by_id(self):
        docker = {"ok": True, "running_images": [], "used_images": [],
                  "images": [{"id": "sha256:dangling", "repo": "<none>", "tag": "<none>", "size": "2GB"},
                             {"id": "sha256:other", "repo": "random/image", "tag": "x", "size": "3GB"}],
                  "containers": [{"id": "c3", "name": "musing_turing", "running": False}]}
        res = ss.delete_item({"kind": "docker_image", "target": "sha256:dangling"}, self.ctx(), docker, time.time())
        self.assertEqual((res["ok"], res["freed"]), (True, 2_000_000_000))
        self.assertEqual(self.run_calls[-1], ["docker", "image", "rm", "sha256:dangling"])
        res = ss.delete_item({"kind": "docker_image", "target": "sha256:other"}, self.ctx(), docker, time.time())
        self.assertFalse(res["ok"])  # review item without allow_review: nothing executed
        self.assertEqual(len(self.run_calls), 1)
        res = ss.delete_item({"kind": "docker_container", "target": "c3"}, self.ctx(), docker, time.time())
        self.assertTrue(res["ok"])
        self.assertEqual(self.run_calls[-1], ["docker", "rm", "c3"])
        self.run_rc = 1
        res = ss.delete_item({"kind": "docker_image", "target": "sha256:dangling"}, self.ctx(), docker, time.time())
        self.assertEqual((res["ok"], res["freed"], res["error"]), (False, 0, "boom"))

    def test_build_cache_prune(self):
        docker = {"ok": True, "build_cache_bytes": 1000, "images": [], "containers": []}
        self.docker = {"ok": True, "build_cache_bytes": 100}
        res = ss.delete_item({"kind": "docker_build_cache", "target": "builder"}, self.ctx(), docker, time.time())
        self.assertEqual((res["ok"], res["freed"]), (True, 900))
        self.assertEqual(self.run_calls[-1], ["docker", "builder", "prune", "-a", "-f"])
        self.assertFalse(any("system" in c and "prune" in c for c in self.run_calls))


class MainTests(ScanBase):
    def test_scan_mode(self):
        self.vfs.write("/srv/models/comfy-input/old.png", age=2 * DAY)
        self.writers = ["/srv/models/staging/x"]
        out = ss.main({"mode": "scan", "node": "node1", "protect": ["/srv/models/gguf", 42]})
        self.assertEqual(out["node"], "node1")
        self.assertIn("/srv/models/comfy-input/old.png", {c["target"] for c in out["candidates"]})
        self.assertEqual(out["active_writers"], ["/srv/models/staging/x"])
        usage = out["usage"]
        self.assertGreater(usage["total"], 0)
        for cat in ("models", "caches", "logs", "projects", "docker_images", "other", "temporary_uploads"):
            self.assertIn(cat, usage["categories"])
        self.assertIsInstance(out["largest"], list)
        self.assertEqual(out["docker"]["ok"], False)

    def test_check_and_delete_modes(self):
        target = self.vfs.write("/srv/models/comfy-input/old.png", age=2 * DAY)
        out = ss.main({"mode": "check", "items": [{"kind": "path", "target": target},
                                                  {"kind": "path", "target": "/nope"}]})
        self.assertEqual(out["items"][0]["class"], "safe")
        self.assertIsNone(out["items"][1])
        out = ss.main({"mode": "delete", "node": "node2", "items": [{"kind": "path", "target": target}]})
        self.assertEqual(out["node"], "node2")
        self.assertTrue(out["results"][0]["ok"])
        self.assertGreater(out["freed"], 0)
        self.assertIn("free_after", out)

    def test_unknown_mode(self):
        self.assertEqual(ss.main({"mode": "rm"}), {"error": "unknown mode"})

    def test_delete_is_bounded(self):
        out = ss.main({"mode": "delete", "items": [{"kind": "path", "target": f"/srv/x{i}"} for i in range(250)]})
        self.assertEqual(len(out["results"]), 200)


# ----------------------------------------------------------- storage.py
class HealthTests(unittest.TestCase):
    def test_levels(self):
        self.assertEqual(health(None, None)["level"], "unknown")
        self.assertEqual(health(19 * GIB, 98.0)["label"], "CRITICAL")
        self.assertEqual(health(29 * GIB, 50.0)["label"], "CRITICAL")
        self.assertEqual(health(500 * GIB, 97.0)["label"], "CRITICAL")
        self.assertEqual(health(60 * GIB, 50.0)["label"], "LOW")
        self.assertEqual(health(200 * GIB, 93.0)["label"], "LOW")
        self.assertEqual(health(100 * GIB, 50.0)["label"], "WATCH")
        self.assertEqual(health(200 * GIB, 86.0)["label"], "WATCH")
        ok = health(328 * GIB, 70.0)
        self.assertEqual((ok["level"], ok["label"], ok["free_gib"]), ("ok", "HEALTHY", 328.0))
        self.assertEqual(health(328 * GIB, None)["percent"], 0.0)


class PreflightTests(unittest.TestCase):
    def test_safe_in_place(self):
        p = install_preflight(500 * GIB, 100 * GIB)
        self.assertEqual((p["status"], p["ok"], p["staging_bytes"], p["peak_bytes"]), ("SAFE", True, 0, 100 * GIB))
        self.assertEqual(p["free_after_bytes"], 400 * GIB)
        self.assertIn("written in place", p["explanation"])

    def test_blocked_when_too_little_remains(self):
        self.assertEqual(install_preflight(140 * GIB, 100 * GIB)["status"], "BLOCKED")
        self.assertEqual(install_preflight(150 * GIB, 100 * GIB, cache_copy=True)["status"], "BLOCKED")
        self.assertEqual(install_preflight(50 * GIB, 100 * GIB)["status"], "BLOCKED")

    def test_tight_when_only_the_staging_peak_hurts(self):
        p = install_preflight(220 * GIB, 100 * GIB, cache_copy=True)
        self.assertEqual((p["status"], p["ok"], p["staging_bytes"]), ("TIGHT", False, 100 * GIB))
        self.assertTrue(p["cache_duplication"])
        self.assertIn("staging copy", p["explanation"])
        self.assertEqual(install_preflight(220 * GIB, 100 * GIB, staged_in_place=False)["status"], "TIGHT")

    def test_custom_headroom(self):
        self.assertEqual(install_preflight(120 * GIB, 100 * GIB, headroom_gib=10)["status"], "SAFE")
        self.assertEqual(install_preflight(120 * GIB, 100 * GIB, headroom_gib=10)["required_headroom_bytes"],
                         10 * GIB)


class ExpandTests(unittest.TestCase):
    def test_expand(self):
        self.assertEqual(_expand("wan2.2_t2v_{high,low}_noise.safetensors"),
                         ["wan2.2_t2v_high_noise.safetensors", "wan2.2_t2v_low_noise.safetensors"])
        self.assertEqual(_expand("plain.gguf"), ["plain.gguf"])
        self.assertEqual(_expand("broken{a.gguf"), ["broken{a.gguf"])
        self.assertEqual(_expand(""), [""])


class Cache:
    def __init__(self, value):
        self.value = value

    def get(self, max_age=None):
        return self.value

    def invalidate(self):
        pass


class FakeCluster:
    def __init__(self):
        self.node1 = Cache({"disk": {"free": 328 * GIB, "percent": 70.0, "total": 900 * GIB}})
        self.node2 = Cache({"reachable": False})
        self.invalidated = 0

    def invalidate(self):
        self.invalidated += 1


class FakeManager:
    def registry(self):
        return {"aliases": {
            "gx-mini": {"path": "/srv/models/gguf/mini", "node": "gx10-01",
                        "previous": {"path": "/srv/models/gguf/mini-old"}},
            "gx-video": {"node": "gx10-02", "components": [
                {"file": "diffusion_models/gxunit_{high,low}_probe.safetensors", "role": "checkpoint"},
                {"file": "notes.txt"}]},
        }}

    def references(self):
        return {"/srv/models/gguf/fast": ["gx-fast binding"], "/opt/elsewhere": ["ignored"]}


class FakeLibrary:
    def usage(self):
        return {"audio": {"count": 1, "bytes": 10}}

    def stats(self):
        return {"schema_version": 2}


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv()
        self.cluster = FakeCluster()
        self.audits: list[dict] = []
        self.maint = False
        self.requests: list[tuple[str, dict]] = []
        self.scan_results = {
            "node1": {"usage": {}, "candidates": [
                {"kind": "path", "target": "/srv/cache/pip", "class": "safe", "bytes": 10, "mtime": 1,
                 "name": "pip"},
                {"kind": "path", "target": "/srv/models/gguf/old", "class": "review", "bytes": 20, "mtime": 2,
                 "name": "old"},
                {"kind": "path", "target": "/srv/projects/gx-cluster/secrets", "class": "protected", "bytes": 1,
                 "mtime": None, "name": "secrets"},
            ]},
            "node2": {"usage": {}, "candidates": [
                {"kind": "docker_image", "target": "sha256:x", "class": "safe", "bytes": 5, "mtime": None,
                 "name": "<none>:<none>"}]},
        }
        self.sm = StorageManager(self.env.cfg, self.cluster, FakeManager(), FakeLibrary(),
                                 audit=lambda **kw: self.audits.append(kw), maintenance=lambda: self.maint,
                                 node_runner=self.runner)
        self.sm.verify = lambda: {"ok": True, "checks": []}

    def tearDown(self):
        self.env.cleanup()

    def runner(self, node, request, timeout):
        self.requests.append((node, request))
        if request["mode"] == "scan":
            data = self.scan_results[node]
            if isinstance(data, Exception):
                raise data
            return {k: ([dict(c) for c in v] if k == "candidates" else v) for k, v in data.items()}
        return {"node": node, "results": [{"target": i["target"], "ok": True, "freed": 7} for i in request["items"]],
                "freed": 7 * len(request["items"]), "free_after": 400 * GIB}

    def scan(self):
        self.sm.start_scan(user="admin")
        deadline = time.time() + 5
        while self.sm.status()["state"] == "scanning" and time.time() < deadline:
            time.sleep(0.005)
        return self.sm.status()

    def ids(self, status):
        return {c["target"]: c["id"] for n in ("node1", "node2") for c in status["result"][n]["candidates"]}

    def test_context_protects_registry_and_media(self):
        ctx1 = self.sm.context("node1")
        self.assertIn("/srv/models/gguf/mini", ctx1["protect"])
        self.assertIn("/srv/models/gguf/mini-old", ctx1["protect"])
        self.assertIn("/srv/models/gguf/fast", ctx1["protect"])
        self.assertNotIn("/opt/elsewhere", ctx1["protect"])
        self.assertIn("/srv/projects/gx-cluster/media/audio", ctx1["protect"])
        self.assertEqual(ctx1["protect_names"], {})
        self.assertIn("gx-litellm", ctx1["known_containers"])
        ctx2 = self.sm.context("node2")
        self.assertIn("/srv/models/music", ctx2["protect"])
        self.assertIn("/srv/models/video/vae", ctx2["protect"])
        self.assertEqual(ctx2["protect_names"]["gxunit_low_probe.safetensors"], "gx-video checkpoint")
        self.assertNotIn("notes.txt", ctx2["protect_names"])

    def test_scan_assigns_opaque_ids_and_skips_protected(self):
        st = self.scan()
        self.assertEqual(st["state"], "done")
        self.assertEqual(st["progress"], {"node1": "done", "node2": "done"})
        ids = self.ids(st)
        for cid in ids.values():
            self.assertRegex(cid, r"^c_[0-9a-f]{24}$")
        self.assertEqual(len(set(ids.values())), 4)
        self.assertEqual(set(self.sm._candidates), {ids["/srv/cache/pip"], ids["/srv/models/gguf/old"],
                                                    ids["sha256:x"]})
        self.assertEqual(st["result"]["library"], {"audio": {"count": 1, "bytes": 10}})
        self.assertEqual([a["outcome"] for a in self.audits], ["started", "done"])
        self.assertIsNotNone(st["elapsed_seconds"])
        self.assertEqual(self.requests[0][1]["mode"], "scan")
        self.assertIn("protect", self.requests[0][1])

    def test_partial_and_failed_scans(self):
        self.scan_results["node2"] = StorageError("gx10-02 is not reachable over SSH", 503)
        st = self.scan()
        self.assertEqual(st["state"], "partial")
        self.assertEqual(st["progress"]["node2"], "failed")
        self.assertIn("node2: gx10-02 is not reachable", st["error"])
        self.scan_results["node1"] = {"error": "unknown mode"}
        self.assertEqual(self.scan()["state"], "failed")

    def test_ids_are_unguessable_per_process(self):
        st = self.scan()
        other = StorageManager(self.env.cfg, self.cluster, FakeManager(), FakeLibrary(), node_runner=self.runner)
        c = st["result"]["node1"]["candidates"][0]
        self.assertNotEqual(other._cid("node1", c), c["id"])

    def test_clean_safe_items(self):
        ids = self.ids(self.scan())
        chosen = [ids["/srv/cache/pip"], ids["sha256:x"]]
        plan = self.sm.plan(chosen + ["c_" + "0" * 24])
        self.assertEqual((plan["bytes"], plan["review"], plan["missing"]), (15, 0, ["c_" + "0" * 24]))
        out = self.sm.clean(chosen, user="admin", ip="10.0.0.1")
        self.assertEqual((out["requested"], out["dry_run_bytes"], out["freed_bytes"], out["refused"]),
                         (2, 15, 14, []))
        deletes = [r for n, r in self.requests if r["mode"] == "delete"]
        self.assertEqual(len(deletes), 2)
        self.assertEqual(deletes[0]["items"][0], {"kind": "path", "target": "/srv/cache/pip",
                                                  "expect": {"mtime": 1}, "allow_review": False})
        self.assertIn("protect", deletes[0])
        self.assertEqual(self.audits[-1]["action"], "storage.cleanup")
        self.assertEqual(out["verification"], {"ok": True, "checks": []})
        self.assertEqual(self.sm.status()["last_cleanup"]["freed_bytes"], 14)
        # ids are consumed
        with self.assertRaises(StorageError) as cm:
            self.sm.clean(chosen, user="admin")
        self.assertEqual(cm.exception.status, 409)

    def test_clean_rejections(self):
        ids = self.ids(self.scan())
        for bad in ([], "c_x", None, ["x"] * 201):
            with self.assertRaises(StorageError):
                self.sm.clean(bad, user="admin")
        for bad in (["c_" + "f" * 24], [ids["/srv/projects/gx-cluster/secrets"]], [123]):
            with self.assertRaises(StorageError) as cm:
                self.sm.clean(bad, user="admin")
            self.assertEqual(cm.exception.status, 409)
        review = [ids["/srv/models/gguf/old"]]
        with self.assertRaises(StorageError) as cm:
            self.sm.clean(review, user="admin")
        self.assertEqual(cm.exception.status, 400)
        with self.assertRaises(StorageError) as cm:
            self.sm.clean(review, user="admin", allow_review=True)
        self.assertEqual(cm.exception.status, 409)
        with self.assertRaises(StorageError) as cm:
            self.sm.clean(review, user="admin", allow_review=True, confirm=True)
        self.assertIn("Maintenance", str(cm.exception))
        self.assertFalse(any(r["mode"] == "delete" for _, r in self.requests))
        self.maint = True
        self.assertTrue(self.sm.plan(review)["maintenance_required"])
        out = self.sm.clean(review, user="admin", allow_review=True, confirm=True)
        self.assertEqual(out["freed_bytes"], 7)
        delete = [r for _, r in self.requests if r["mode"] == "delete"][0]
        self.assertTrue(delete["items"][0]["allow_review"])
        self.assertEqual(self.audits[-1]["review"], 1)

    def test_node_failure_during_clean_is_reported(self):
        ids = self.ids(self.scan())

        def failing(node, request, timeout):
            raise StorageError("gx10-02 is not reachable over SSH", 503)
        self.sm._runner = failing
        out = self.sm.clean([ids["sha256:x"]], user="admin")
        self.assertEqual(out["freed_bytes"], 0)
        self.assertIn("not reachable", out["per_node"]["node2"]["error"])

    # Fixed: start_scan() used to call status() while holding its own
    # non-reentrant lock, deadlocking a second scan request.
    def test_second_scan_request_while_scanning_returns(self):
        gate = threading.Event()
        original = self.runner

        def slow(node, request, timeout):
            gate.wait(5)
            return original(node, request, timeout)
        self.sm._runner = slow
        self.sm.start_scan(user="admin")
        result: list[dict] = []
        second = threading.Thread(target=lambda: result.append(self.sm.start_scan(user="admin")), daemon=True)
        second.start()
        second.join(1.0)
        gate.set()
        # On failure the daemon threads stay blocked; they only hold this
        # test's private StorageManager lock.
        self.assertFalse(second.is_alive(), "start_scan deadlocked while a scan was running")
        self.assertEqual(result[0]["state"], "scanning")
        deadline = time.time() + 5
        while self.sm.status()["state"] == "scanning" and time.time() < deadline:
            time.sleep(0.005)
        self.assertEqual(len([r for _, r in self.requests if r["mode"] == "scan"]), 2)

    def test_overview(self):
        ov = self.sm.overview()
        self.assertEqual(ov["node1"]["health"]["label"], "HEALTHY")
        self.assertEqual(ov["node1"]["name"], "gx10-01")
        self.assertEqual(ov["node2"]["health"]["level"], "unknown")

    def test_status_reports_maintenance(self):
        self.assertFalse(self.sm.status()["maintenance"])
        self.maint = True
        self.assertTrue(self.sm.status()["maintenance"])
        self.assertIsNone(self.sm.status()["elapsed_seconds"])


if __name__ == "__main__":
    unittest.main()
