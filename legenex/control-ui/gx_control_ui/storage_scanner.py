"""The Storage page: per-node usage analysis. ANALYSIS ONLY.

Per node (gx10-01 locally; gx10-02 through the orchestrator's /health/detailed
when it publishes worker facts — until then the row says "unavailable"
honestly, never a guess):

* filesystems (from hostfacts disk data),
* du of /srv/models, /srv/cache, /srv/logs,
* docker usage via `docker system df` (subprocess; no root needed),
* the largest files/dirs under the allowed roots (top 20),
* stale cache detection (mtime) under /srv/cache,
* duplicate candidates: same-size files under /srv/models.

Cleanup is NOT performed here: every destructive path goes through the file
manager's trash (filemanager.py), which is the only thing that deletes.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from .config import UIConfig
from .util import HTTPError, bearer, http_json, run

GIB = 2**30
STALE_CACHE_DAYS = 14
TOP_N = 20
WALK_BUDGET_S = 20.0


def _du(path: Path) -> int | None:
    if not path.is_dir():
        return None
    res = run(["du", "-sb", str(path)], timeout=120)
    if res.ok and res.out.strip():
        try:
            return int(res.out.split()[0])
        except (ValueError, IndexError):
            return None
    return None


def _largest(roots: tuple[str, ...], top: int = TOP_N) -> list[dict]:
    """Largest files under the allowed roots (bounded walk, top N)."""
    files: list[tuple[int, str]] = []
    deadline = time.monotonic() + WALK_BUDGET_S
    for raw in roots:
        root = Path(raw)
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            if time.monotonic() > deadline:
                return _shape(files, top)
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "trash"]
            for name in filenames:
                try:
                    size = (Path(dirpath) / name).stat().st_size
                    files.append((size, os.path.join(dirpath, name)))
                except OSError:
                    pass
    return _shape(files, top)


def _shape(files: list[tuple[int, str]], top: int) -> list[dict]:
    files.sort(reverse=True)
    return [{"path": p, "bytes": s, "gib": round(s / GIB, 2)} for s, p in files[:top]]


def _stale_cache(cache: Path, days: int = STALE_CACHE_DAYS, limit: int = 100) -> list[dict]:
    out: list[dict] = []
    if not cache.is_dir():
        return out
    cutoff = time.time() - days * 86400
    for dirpath, dirnames, filenames in os.walk(cache):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            p = Path(dirpath) / name
            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_mtime < cutoff:
                out.append({"path": str(p), "bytes": st.st_size, "age_days": round(
                    (time.time() - st.st_mtime) / 86400, 1)})
                if len(out) >= limit:
                    return out
    return out


def _duplicates(models_dir: Path, limit: int = 50) -> list[dict]:
    """Same-size files under /srv/models: candidates, never proof of waste."""
    if not models_dir.is_dir():
        return []
    by_size: dict[int, list[str]] = {}
    deadline = time.monotonic() + WALK_BUDGET_S
    for dirpath, dirnames, filenames in os.walk(models_dir):
        if time.monotonic() > deadline:
            break
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            p = Path(dirpath) / name
            try:
                size = p.stat().st_size
            except OSError:
                continue
            if size >= GIB:  # only large files are interesting
                by_size.setdefault(size, []).append(str(p))
    out = [{"bytes": size, "gib": round(size / GIB, 2), "files": paths}
           for size, paths in by_size.items() if len(paths) > 1]
    out.sort(key=lambda d: -d["bytes"])
    return out[:limit]


def _docker_df(node: str) -> dict:
    """`docker system df` output; needs no root on this cluster."""
    if node == "node2":
        return {"available": False, "reason": "docker on gx10-02 is reachable only through the "
                "orchestrator worker facts, which are not published yet"}
    res = run(["docker", "system", "df", "--format", "{{json .}}"], timeout=30)
    rows = []
    for line in (res.out or "").splitlines():
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    if not res.ok:
        return {"available": False, "reason": "docker did not answer (is the user in the docker group?)"}
    return {"available": True, "rows": rows}


class StorageScanner:
    def __init__(self, cfg: UIConfig) -> None:
        self.cfg = cfg
        self.roots = tuple(Path(r) for r in cfg.file_roots)
        self._cache: tuple[float, dict] = (0.0, {})
        self._ttl = 60.0

    # ------------------------------------------------------------- worker
    def _worker_facts(self) -> dict:
        """gx10-02 facts via the orchestrator /health/detailed, or honest gap."""
        if self.cfg.offline:
            return {"available": False, "reason": "offline mode"}
        try:
            code, body = http_json("GET", f"{self.cfg.orchestrator_base}/health/detailed",
                                   headers=bearer(self.cfg.secret("GX_ORCHESTRATOR_API_KEY")), timeout=5)
        except HTTPError as exc:
            return {"available": False, "reason": exc.message}
        if not 200 <= code < 300 or not isinstance(body, dict):
            return {"available": False, "reason": f"orchestrator answered HTTP {code}"}
        nodes = body.get("nodes") or {}
        worker = next((n for n, s in nodes.items() if isinstance(s, dict) and s.get("role") == "worker"),
                      None)
        if worker is None:
            return {"available": False,
                    "reason": "the orchestrator does not publish worker node facts yet "
                              "(Worker A follow-up); gx10-02 rows show local SSH facts instead"}
        return {"available": True, "facts": nodes[worker]}

    # -------------------------------------------------------------- view
    def view(self, cluster, *, fresh: bool = False) -> dict:
        now = time.time()
        if not fresh and self._cache[0] and now - self._cache[0] < self._ttl:
            return self._cache[1]
        n1 = cluster.node1.get() or {}
        n2 = cluster.node2.get() or {}
        srv = {"models": _du(Path("/srv/models")), "cache": _du(Path("/srv/cache")),
               "logs": _du(Path("/srv/logs"))}
        out = {
            "generated_at": now,
            "head": {
                "name": "gx10-01",
                "reachable": True,
                "disk": (n1.get("disk") or {}) if not n1.get("offline") else None,
                "srv_bytes": srv,
                "docker": _docker_df("node1"),
                "note": "local scan",
            },
            "worker": {
                "name": "gx10-02",
                "reachable": bool(n2.get("reachable")) and not n2.get("offline"),
                "disk": (n2.get("disk") or {}) if n2.get("reachable") and not n2.get("offline") else None,
                "orchestrator_facts": self._worker_facts(),
                "note": "gx10-02 runs no management stack: the rank-1 mirror only",
            },
            "largest": _largest(tuple(str(r) for r in self.roots)),
            "stale_cache": _stale_cache(Path("/srv/cache")),
            "duplicate_candidates": _duplicates(Path("/srv/models")),
            "thresholds": {"critical_free_gib": 30, "low_free_gib": 75, "watch_free_gib": 150},
            "cleanup_note": "analysis only — every deletion goes through Files > trash",
        }
        self._cache = (now, out)
        return out
