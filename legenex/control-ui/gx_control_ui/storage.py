"""Storage & Cleanup (D-037): both nodes, classified candidates, safe cleanup.

The browser only ever sends OPAQUE candidate ids that this process generated
during the last scan. There is no path, no command and no free text in a
cleanup request. The node that owns the files re-classifies every item right
before it deletes it (storage_scan.py "delete" mode), so a candidate that
became active, mounted or protected after the scan is refused.

Health thresholds (free space on /, where /srv lives):
    CRITICAL  < 30 GiB free or >= 97 % used
    LOW       < 75 GiB free or >= 92 % used
    WATCH     < 150 GiB free or >= 85 % used
    HEALTHY   otherwise
The 2026-09-17 gx10-02 state (19 GiB free, 98 %) is CRITICAL; 328 GiB free is
HEALTHY.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import shlex
import threading
import time
from pathlib import Path
from typing import Any
from collections.abc import Callable

from . import storage_scan
from .util import run, ssh_args

GIB = 2**30
NODES = ("node1", "node2")
MIN_INSTALL_HEADROOM_GIB = 50.0


class StorageError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def health(free_bytes: int | None, percent: float | None) -> dict:
    if free_bytes is None:
        return {"level": "unknown", "label": "UNKNOWN", "reason": "disk not readable"}
    free = free_bytes / GIB
    pct = percent or 0.0
    if free < 30 or pct >= 97:
        level, label = "crit", "CRITICAL"
    elif free < 75 or pct >= 92:
        level, label = "warn", "LOW"
    elif free < 150 or pct >= 85:
        level, label = "watch", "WATCH"
    else:
        level, label = "ok", "HEALTHY"
    return {"level": level, "label": label, "free_gib": round(free, 1), "percent": pct,
            "reason": f"{free:.0f} GiB free ({pct:.0f} % used)"}


def install_preflight(free_bytes: int, download_bytes: int, *, staged_in_place: bool = True,
                      cache_copy: bool = False, headroom_gib: float = MIN_INSTALL_HEADROOM_GIB) -> dict:
    """Disk arithmetic shown BEFORE any download starts (Model Manager).

    `hf download --local-dir` writes each file into the target through a
    temporary `.incomplete` file, so the staging requirement is one extra
    copy of the largest file, not of the whole model, unless the files are
    first downloaded into a cache and then copied (`cache_copy`).
    """
    staging = download_bytes if cache_copy or not staged_in_place else 0
    peak = download_bytes + staging
    after = free_bytes - download_bytes
    after_peak = free_bytes - peak
    safe = after_peak >= headroom_gib * GIB
    status = "SAFE" if safe else ("BLOCKED" if after_peak < 0 or after < headroom_gib * GIB else "TIGHT")
    return {
        "current_free_bytes": free_bytes, "download_bytes": download_bytes, "staging_bytes": staging,
        "final_installed_bytes": download_bytes, "cache_duplication": bool(cache_copy),
        "peak_bytes": peak, "free_after_bytes": after, "free_at_peak_bytes": after_peak,
        "required_headroom_bytes": int(headroom_gib * GIB), "status": status,
        "ok": status == "SAFE",
        "explanation": (
            f"Download {download_bytes / GIB:.1f} GiB"
            + (f" + staging copy {staging / GIB:.1f} GiB" if staging else " (written in place)")
            + f"; peak {peak / GIB:.1f} GiB; {free_bytes / GIB:.1f} GiB free now; "
              f"{after / GIB:.1f} GiB free afterwards; at least {headroom_gib:.0f} GiB must stay free."),
    }


class StorageManager:
    def __init__(self, cfg, cluster, manager, library, *, audit: Callable[..., None] | None = None,
                 maintenance: Callable[[], bool] | None = None,
                 node_runner: Callable[[str, dict, float], dict] | None = None) -> None:
        self.cfg = cfg
        self.cluster = cluster
        self.manager = manager
        self.library = library
        self.audit = audit or (lambda **kw: None)
        self.maintenance = maintenance or (lambda: False)
        self._runner = node_runner or self._run_on_node
        self._secret = secrets.token_bytes(32)
        self._lock = threading.Lock()
        self.state: dict[str, Any] = {"state": "idle", "progress": {}, "result": None, "started": None,
                                      "finished": None, "error": None}
        self._candidates: dict[str, tuple[str, dict]] = {}
        self.last_cleanup: dict | None = None

    # -------------------------------------------------------------- nodes
    def _src(self) -> str:
        return Path(storage_scan.__file__).read_text(encoding="utf-8")

    def _run_on_node(self, node: str, request: dict, timeout: float) -> dict:
        payload = json.dumps(request)
        if node == "node1":
            return storage_scan.main(request)
        res = run(ssh_args(self.cfg.node2_ssh, 10) + ["python3", "-", shlex.quote(payload)],
                  timeout=timeout, input_text=self._src(), merge_stderr=False)
        if not res.ok:
            raise StorageError("gx10-02 is not reachable over SSH", 503)
        try:
            return json.loads(res.out)
        except ValueError:
            raise StorageError("gx10-02 returned an unreadable scan", 502) from None

    # ------------------------------------------------------ protect lists
    def context(self, node: str) -> dict:
        """What must never be cleaned on `node`, and why."""
        why: dict[str, str] = {}
        reg = self.manager.registry()
        for path, users in self.manager.references().items():
            if path.startswith("/srv/"):
                why[path] = "; ".join(users)
        for alias, spec in (reg.get("aliases") or {}).items():
            prev = spec.get("previous") or {}
            if prev.get("path") and prev.get("on_disk", True):
                why.setdefault(prev["path"], f"{alias} rollback (Model Manager)")
            if spec.get("path"):
                why.setdefault(spec["path"], f"{alias} current checkpoint")
        names: dict[str, str] = {}
        if node == "node2":
            why.update({
                "/srv/models/music": "gx-music weights (ACE-Step 1.5 XL)",
                "/srv/models/music-data/db": "gx-music job database",
                "/srv/models/music-data/uploads": "gx-music reference uploads in use",
            })
            for tree in ("image", "video", "shared"):
                for sub in ("text_encoders", "vae", "clip_vision", "audio_encoders"):
                    why[f"/srv/models/{tree}/{sub}"] = "shared text encoders / VAE (gx-image, gx-video)"
            # Referenced weights are protected by file name; other large files in
            # the media trees are offered for REVIEW by the node.
            for wf in sorted((self.cfg.repo_root / "legenex/media/workflows").glob("*.api.json")):
                try:
                    models = json.loads(wf.read_text()).get("_gx", {}).get("models") or []
                except (OSError, ValueError):
                    continue
                for m in models:
                    names[str(m)] = f"used by media workflow {wf.name.removesuffix('.api.json')}"
            for alias, spec in (reg.get("aliases") or {}).items():
                for comp in spec.get("components") or []:
                    for name in _expand(Path(str(comp.get("file") or "")).name):
                        if name.endswith((".safetensors", ".gguf", ".ckpt", ".pt", ".bin")):
                            names.setdefault(name, f"{alias} {comp.get('role', 'component')}")
        for sub in ("images", "videos", "audio", "thumbnails", "metadata"):
            why[f"/srv/projects/gx-cluster/media/{sub}"] = "Library assets: manage them in GX-Playground > Library"
        images = {
            "ghcr.io/berriai/litellm:main-stable", "gx-llama-swap:latest", "postgres:16-alpine",
            "gx-comfyui:sm121", "gx-media-router:2.3.0", "legenex/llama-cpp-spark:latest",
            "jstarkg/vllm-gb10-flashnext:0.28-sm121-r6", "lmsysorg/sglang:dev-v4f-2dgx-v2",
            "linuxserver/ffmpeg:latest", "gx-music-engine:acestep15-ca1e85f-t214",
            "gx-music-engine:acestep15-ca1e85f-base", "ghcr.io/open-webui/open-webui:main",
        }
        known = ["gx-mini", "gx-fast", "gx-reason", "gx-litellm", "gx-litellm-db", "gx-llama-swap-node01",
                 "gx-llama-swap-node02", "gx-comfyui", "gx-media-router", "gx-music", "gx-max-rank0",
                 "gx-max-rank1", "open-webui"]
        return {"protect": sorted(why), "why": why, "protect_names": names, "images": sorted(images),
                "known_containers": known}

    # --------------------------------------------------------------- scan
    def start_scan(self, *, user: str) -> dict:
        with self._lock:
            already = self.state["state"] == "scanning"
            if not already:
                self.state = {"state": "scanning", "progress": {n: "queued" for n in NODES}, "result": None,
                              "started": time.time(), "finished": None, "error": None}
        if already:  # status() takes the lock itself: never call it while holding it
            return self.status()
        self.audit(user=user, ip="", action="storage.scan", outcome="started")
        threading.Thread(target=self._scan, args=(user,), daemon=True, name="storage-scan").start()
        return self.status()

    def _scan(self, user: str) -> None:
        results: dict[str, Any] = {}
        candidates: dict[str, tuple[str, dict]] = {}
        for node in NODES:
            self.state["progress"][node] = "scanning"
            try:
                req = {"mode": "scan", "node": node, **self.context(node)}
                data = self._runner(node, req, 900)
                if "error" in data and "usage" not in data:
                    raise StorageError(str(data["error"]))
                for c in data.get("candidates", []):
                    cid = self._cid(node, c)
                    c["id"] = cid
                    c["node"] = node
                    if c["class"] != "protected":
                        candidates[cid] = (node, c)
                results[node] = data
                self.state["progress"][node] = "done"
            except Exception as exc:  # noqa: BLE001 - reported to the page
                results[node] = {"error": str(exc)}
                self.state["progress"][node] = "failed"
        results["library"] = self.library.usage()
        with self._lock:
            self._candidates = candidates
            failed = [n for n in NODES if "error" in results.get(n, {})]
            self.state.update(state="done" if not failed else ("partial" if len(failed) < 2 else "failed"),
                              result=results, finished=time.time(),
                              error=("; ".join(f"{n}: {results[n]['error']}" for n in failed) or None))
        self.audit(user=user, ip="", action="storage.scan", outcome=self.state["state"],
                   candidates=len(candidates))

    def _cid(self, node: str, c: dict) -> str:
        msg = f"{node}|{c['kind']}|{c['target']}|{c.get('mtime')}|{c.get('bytes')}".encode()
        return "c_" + hmac.new(self._secret, msg, hashlib.sha256).hexdigest()[:24]

    def status(self) -> dict:
        with self._lock:
            st = dict(self.state)
        st["elapsed_seconds"] = round((st.get("finished") or time.time()) - st["started"], 1) \
            if st.get("started") else None
        st["maintenance"] = self.maintenance()
        st["last_cleanup"] = self.last_cleanup
        return st

    def overview(self) -> dict:
        """Live disk + health for both nodes (cheap; no scan needed)."""
        out = {}
        for node, facts in (("node1", self.cluster.node1.get() or {}), ("node2", self.cluster.node2.get() or {})):
            disk = facts.get("disk") or {}
            out[node] = {"name": "gx10-01" if node == "node1" else "gx10-02", **disk,
                         "health": health(disk.get("free"), disk.get("percent")) if disk else
                         health(None, None)}
        return out

    # ------------------------------------------------------------ cleanup
    def clean(self, ids: list[str], *, user: str, ip: str = "", allow_review: bool = False,
              confirm: Any = None) -> dict:
        if not isinstance(ids, list) or not ids or len(ids) > 200:
            raise StorageError("select between 1 and 200 cleanup items")
        with self._lock:
            chosen = []
            for cid in ids:
                if not isinstance(cid, str) or cid not in self._candidates:
                    raise StorageError("an item is no longer part of the latest scan; scan again", 409)
                chosen.append(self._candidates[cid])
        review = [c for _, c in chosen if c["class"] == "review"]
        if review and not allow_review:
            raise StorageError("REVIEW items need explicit confirmation")
        if review and confirm is not True:
            raise StorageError("confirm the REVIEW items and their consequences", 409)
        if review and not self.maintenance():
            raise StorageError("REVIEW items are only removed in Maintenance mode (Resource Control)", 409)
        if any(c["class"] == "protected" for _, c in chosen):
            raise StorageError("protected items cannot be cleaned", 403)
        by_node: dict[str, list[dict]] = {}
        for node, c in chosen:
            by_node.setdefault(node, []).append({"kind": c["kind"], "target": c["target"],
                                                 "expect": {"mtime": c.get("mtime")},
                                                 "allow_review": c["class"] == "review"})
        dry_total = sum(c["bytes"] for _, c in chosen)
        results: dict[str, Any] = {}
        for node, items in by_node.items():
            try:
                req = {"mode": "delete", "node": node, "items": items, **self.context(node)}
                results[node] = self._runner(node, req, 1800)
            except Exception as exc:  # noqa: BLE001
                results[node] = {"error": str(exc), "results": [], "freed": 0}
        freed = sum(int(r.get("freed") or 0) for r in results.values())
        refused = [x for r in results.values() for x in r.get("results", []) if not x.get("ok")]
        with self._lock:
            for cid in ids:
                self._candidates.pop(cid, None)
        self.last_cleanup = {"at": time.time(), "by": user, "requested": len(ids), "dry_run_bytes": dry_total,
                             "freed_bytes": freed, "refused": refused, "per_node": {
                                 n: {"freed": r.get("freed"), "free_after": r.get("free_after"),
                                     "error": r.get("error")} for n, r in results.items()}}
        self.audit(user=user, ip=ip, action="storage.cleanup", outcome="ok" if not refused else "partial",
                   items=len(ids), freed_bytes=freed, review=len(review),
                   targets=[f"{n}:{c['kind']}:{c['name']}" for n, c in chosen][:50])
        self.cluster.invalidate()
        self.last_cleanup["verification"] = self.verify()
        return self.last_cleanup

    def plan(self, ids: list[str]) -> dict:
        """Dry run: what would be removed and how much space comes back."""
        with self._lock:
            chosen = [self._candidates[c] for c in ids if c in self._candidates]
        return {"items": [dict(c, node=n) for n, c in chosen], "bytes": sum(c["bytes"] for _, c in chosen),
                "missing": [c for c in ids if c not in self._candidates],
                "review": sum(1 for _, c in chosen if c["class"] == "review"),
                "maintenance_required": any(c["class"] == "review" for _, c in chosen),
                "maintenance": self.maintenance()}

    def verify(self) -> dict:
        """After cleanup: registry paths, music checkpoints, Library, disk."""
        checks = []
        reg = self.manager.registry()
        n1_paths = [s["path"] for s in reg.get("aliases", {}).values()
                    if s.get("path") and s.get("node") in ("gx10-01", "gx10-01 + gx10-02")]
        for p in n1_paths:
            checks.append({"check": f"gx10-01 {p}", "ok": Path(p).is_dir()})
        n2_paths = [s["path"] for s in reg.get("aliases", {}).values()
                    if s.get("path") and "gx10-02" in str(s.get("node"))]
        quoted = " ".join(shlex.quote(p) for p in n2_paths + ["/srv/models/image", "/srv/models/video",
                                                                "/srv/models/shared"])
        res = run(ssh_args(self.cfg.node2_ssh, 10) + [f"for p in {quoted}; do test -d \"$p\" && echo ok "
                                                      "|| echo MISSING; done"], timeout=30)
        lines = res.out.split() if res.ok else []
        wanted = n2_paths + ["/srv/models/image", "/srv/models/video", "/srv/models/shared"]
        for p, state in zip(wanted, (lines + ["?"] * len(wanted))[:len(wanted)], strict=True):
            checks.append({"check": f"gx10-02 {p}", "ok": state == "ok"})
        try:
            stats = self.library.stats()
            checks.append({"check": "Library database", "ok": stats.get("schema_version", 0) >= 1})
        except Exception:  # noqa: BLE001
            checks.append({"check": "Library database", "ok": False})
        return {"ok": all(c["ok"] for c in checks), "checks": checks}


def _expand(name: str) -> list[str]:
    """`wan2.2_t2v_{high,low}_noise.safetensors` -> both names (one brace group)."""
    if "{" not in name or "}" not in name:
        return [name]
    head, _, rest = name.partition("{")
    options, _, tail = rest.partition("}")
    return [f"{head}{opt}{tail}" for opt in options.split(",")]
