"""The Updates page: registry pins vs live state, plus a CHECK FOR UPDATE.

Pins (registry.json schema 2):
  * the Mia runtime submodule commit + container image,
  * each model pack's Hugging Face revision.

Live (read from the machine, never guessed):
  * `git -C mia-dsv41 rev-parse HEAD` (the actual submodule checkout),
  * the local docker image digest (docker image inspect),
  * the model packs' on-disk revision manifests (.gx-manifest.json) when
    they exist.

CHECK FOR UPDATE queries the upstream APIs (GitHub commits endpoint for the
Mia repository's default branch, the Hugging Face model API for each pack's
pinned and latest revision) and reports drift. It NEVER updates anything —
no auto-update exists anywhere in the dashboard.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from .config import UIConfig
from .models import read_registry
from .util import HTTPError, http_json, run

GITHUB_API = "https://api.github.com"
HF_API = "https://huggingface.co/api"


def _pin_row(kind: str, name: str, pin: Any, live: Any, detail: str = "") -> dict:
    return {"kind": kind, "name": name, "pin": pin, "live": live,
            "match": pin is not None and live is not None and str(pin) == str(live),
            "detail": detail}


class UpdatesView:
    def __init__(self, cfg: UIConfig) -> None:
        self.cfg = cfg
        self._cache: tuple[float, dict] = (0.0, {})
        self._check_cache: tuple[float, dict] = (0.0, {})

    # ------------------------------------------------------------- live
    def _submodule_commit(self) -> str | None:
        res = run(["git", "-C", str(self.cfg.mia_dir), "rev-parse", "HEAD"], timeout=10)
        return res.out.strip() if res.ok and res.out.strip() else None

    def _submodule_remote(self) -> str | None:
        res = run(["git", "-C", str(self.cfg.mia_dir), "remote", "get-url", "origin"], timeout=10)
        return res.out.strip() if res.ok and res.out.strip() else None

    def _image_digest(self, image: str) -> str | None:
        res = run(["docker", "image", "inspect", image, "--format",
                   "{{index .RepoDigests 0}}"], timeout=20)
        out = res.out.strip()
        if not res.ok or not out:
            return None
        return out

    def _model_disk_revision(self, model_dir: Path) -> str | None:
        try:
            data = json.loads((model_dir / ".gx-manifest.json").read_text(encoding="utf-8"))
            return data.get("revision") if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    # -------------------------------------------------------------- view
    def view(self) -> dict:
        now = time.time()
        if self._cache[0] and now - self._cache[0] < 30:
            return self._cache[1]
        reg = read_registry(self.cfg.registry_path)
        pins: list[dict] = []
        for rid, rt in (reg.get("runtimes") or {}).items():
            if not isinstance(rt, dict):
                continue
            live_commit = self._submodule_commit()
            pins.append(_pin_row("runtime_commit", rid, rt.get("commit"), live_commit,
                                 "registry pin vs the checked-out mia-dsv41 submodule"))
            image = rt.get("image")
            if image:
                pins.append(_pin_row("image_digest", rid, image,
                                     self._image_digest(image),
                                     "registry image name vs the local image (digest shown when present)"))
        for mid, spec in (reg.get("models") or {}).items():
            if not isinstance(spec, dict):
                continue
            disk = self._model_disk_revision(Path(spec.get("path") or "/nonexistent"))
            pins.append(_pin_row("model_revision", mid, spec.get("revision"), disk,
                                 "registry pin vs the pack's .gx-manifest.json on disk"
                                 if disk else "no .gx-manifest.json on disk (cannot verify locally)"))
        out = {
            "generated_at": now,
            "registry_ok": reg.get("schema") == 2,
            "pins": pins,
            "drift": [p for p in pins if not p.get("match")],
            "mia_remote": self._submodule_remote(),
            "policy": "reporting only — the dashboard never updates anything itself",
        }
        self._cache = (now, out)
        return out

    # ------------------------------------------------------------- check
    def _github_head(self, repo: str) -> str | None:
        """repo like 'owner/name' from a git remote URL."""
        try:
            code, body = http_json("GET", f"{GITHUB_API}/repos/{repo}/commits/master", timeout=10)
            if code == 404:
                code, body = http_json("GET", f"{GITHUB_API}/repos/{repo}/commits/main", timeout=10)
            if 200 <= code < 300 and isinstance(body, dict):
                return body.get("sha")
        except HTTPError:
            return None
        return None

    def _hf_revisions(self, repo: str) -> dict | None:
        try:
            code, body = http_json("GET", f"{HF_API}/models/{repo}", timeout=10)
            if 200 <= code < 300 and isinstance(body, dict):
                return {"sha": body.get("sha")}
        except HTTPError:
            return None
        return None

    def check(self) -> dict:
        """Query upstream for drift. Cached 5 minutes; never mutates anything."""
        now = time.time()
        if self._check_cache[0] and now - self._check_cache[0] < 300:
            return self._check_cache[1]
        reg = read_registry(self.cfg.registry_path)
        results: list[dict] = []
        if self.cfg.offline:
            results.append({"kind": "check", "name": "offline mode",
                            "checked": False, "reason": "offline mode"})
        else:
            # Mia repo: derive 'owner/name' from the submodule's origin URL.
            remote = self._submodule_remote() or ""
            m = None
            for pattern in (r"github\.com[/:]([\w.\-]+/[\w.\-]+?)(?:\.git)?$",
                            r"ghcr\.io/([\w.\-]+)/"):
                m = re.search(pattern, remote)
                if m:
                    break
            if m:
                slug = m.group(1)
                head = self._github_head(slug)
                results.append({"kind": "github", "name": f"{slug} default branch HEAD",
                                "checked": head is not None, "upstream": head})
            else:
                results.append({"kind": "github", "name": "mia-dsv41 origin",
                                "checked": False,
                                "reason": f"cannot derive a GitHub slug from {remote or 'no remote'}"})
            for mid, spec in (reg.get("models") or {}).items():
                if not isinstance(spec, dict):
                    continue
                source = spec.get("source")
                if not source:
                    continue
                revisions = self._hf_revisions(source)
                results.append({"kind": "huggingface", "name": mid, "repo": source,
                                "checked": revisions is not None,
                                "upstream": (revisions or {}).get("sha"),
                                "pin": spec.get("revision"),
                                "match": bool(revisions and spec.get("revision") and
                                              revisions.get("sha") == spec.get("revision"))})
        out = {
            "generated_at": now,
            "results": results,
            "drift": [r for r in results if r.get("kind") == "huggingface" and not r.get("match")],
            "policy": "check only — nothing was or will be updated automatically",
        }
        self._check_cache = (now, out)
        return out
