"""The Projects page: scan /home/legenex/Documents/Projects (depth 2).

Per project: name, path, git facts (is_git, remote, branch, dirty, last
commit), mtime, cached du size (refreshable on demand) and scheduler
attribution (active agents / queue depth per project, from the orchestrator).

Everything is read via subprocess git with a FIXED argument list assembled
from validated values — no shell, no user input reaches a command line.
Sizes are cached in the control-ui state dir; a refresh re-measures on demand.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from .config import UIConfig
from .util import run


class ProjectsScanner:
    def __init__(self, cfg: UIConfig) -> None:
        self.cfg = cfg
        self.root = Path(cfg.projects_root)
        self.sizes_path = Path(cfg.state_dir) / "project-sizes.json"
        self._sizes: dict[str, dict] = self._load_sizes()
        self._lock = threading.Lock()

    # ------------------------------------------------------------- sizes
    def _load_sizes(self) -> dict[str, dict]:
        try:
            data = json.loads(self.sizes_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_sizes(self) -> None:
        try:
            self.sizes_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.sizes_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._sizes), encoding="utf-8")
            tmp.replace(self.sizes_path)
        except OSError:
            pass

    def _du(self, path: Path) -> int | None:
        res = run(["du", "-sb", str(path)], timeout=120)
        if res.ok and res.out.strip():
            try:
                return int(res.out.split()[0])
            except (ValueError, IndexError):
                return None
        return None

    def refresh_size(self, name: str) -> dict:
        path = self.root / name
        self._check_child(path)
        size = self._du(path)
        rec = {"bytes": size, "measured_at": time.time()}
        with self._lock:
            self._sizes[name] = rec
            self._save_sizes()
        return rec

    def _check_child(self, path: Path) -> None:
        """The scan is depth-2 and non-hidden under the configured root; a
        caller-named project must be a direct, existing, non-hidden child."""
        if path.parent != self.root or path.name.startswith(".") or not path.is_dir():
            raise ValueError("not a scanned project directory")

    # ---------------------------------------------------------------- git
    def _git(self, path: Path, args: list[str], timeout: float = 8.0) -> str:
        res = run(["git", "-C", str(path), *args], timeout=timeout)
        return res.out.strip() if res.ok else ""

    def _git_facts(self, path: Path) -> dict:
        out: dict[str, Any] = {"is_git": (path / ".git").exists()}
        if not out["is_git"]:
            return out
        out["branch"] = self._git(path, ["rev-parse", "--abbrev-ref", "HEAD"]) or None
        out["head"] = self._git(path, ["rev-parse", "HEAD"]) or None
        out["remote"] = self._git(path, ["remote", "get-url", "origin"]) or None
        status = self._git(path, ["status", "--porcelain"], timeout=15)
        out["dirty"] = bool(status)
        out["dirty_files"] = len(status.splitlines()) if status else 0
        log = self._git(path, ["log", "-1", "--format=%s\x1f%ct"], timeout=10)
        if log:
            subject, _, ts = log.partition("\x1f")
            out["last_commit"] = {"subject": subject[:200]}
            try:
                out["last_commit"]["ts"] = int(ts)
            except ValueError:
                pass
        return out

    # ------------------------------------------------------------- scan
    def scan(self, scheduler_snap: dict | None = None, *, refresh_sizes: bool = False) -> dict:
        projects: list[dict] = []
        attribution = _project_attribution(scheduler_snap)
        try:
            children = sorted(self.root.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            children = []
        for path in children:
            if path.name.startswith(".") or not path.is_dir():
                continue
            entry: dict[str, Any] = {"name": path.name, "path": str(path)}
            try:
                st = path.stat()
                entry["mtime"] = st.st_mtime
            except OSError:
                entry["mtime"] = None
            entry.update(self._git_facts(path))
            cached = self._sizes.get(path.name) or {}
            entry["size_bytes"] = cached.get("bytes")
            entry["size_measured_at"] = cached.get("measured_at")
            entry["scheduler"] = attribution.get(path.name)
            projects.append(entry)
        # Depth 2: one level of sub-directories per project (repos under a
        # grouping folder, e.g. Server/gx-cluster) — listed as children names.
        for entry in projects[:200]:
            path = Path(entry["path"])
            if entry.get("is_git"):
                entry["children"] = []
                continue
            try:
                entry["children"] = [c.name for c in sorted(path.iterdir())
                                     if not c.name.startswith(".") and c.is_dir()][:50]
            except OSError:
                entry["children"] = []
        return {"root": str(self.root), "projects": projects,
                "generated_at": time.time()}


def _project_attribution(snap: dict | None) -> dict[str, dict]:
    """project -> {active, queued} from the scheduler status snapshot."""
    out: dict[str, dict] = {}
    if not isinstance(snap, dict) or not snap.get("available"):
        return out
    records: list = []
    for key in ("queue", "requests", "records"):
        raw = snap.get(key)
        if isinstance(raw, list):
            records = raw
            break
    for rec in records:
        if not isinstance(rec, dict):
            continue
        project = str(rec.get("project") or "")
        if not project or project == "unknown":
            continue
        slot = out.setdefault(project, {"active": 0, "queued": 0})
        state = str(rec.get("state") or "")
        if state == "active":
            slot["active"] += 1
        elif state == "queued":
            slot["queued"] += 1
    return out
