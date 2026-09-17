"""Storage scanner for ONE node, as a single JSON document (D-037).

Self-contained (stdlib only, no package imports) because it runs in two ways,
like hostfacts.py:

  * imported on gx10-01 for the local node, and
  * streamed verbatim to gx10-02 as `ssh legenex-02@gx10-02 python3 - <json>`.

Modes (the JSON request on argv[1] / stdin line):

  {"mode": "scan", "node": "node1", "protect": [...], "references": [...]}
      -> usage breakdown + classified cleanup candidates
  {"mode": "check", "node": "node2", "items": [{"kind","target"}], ...}
      -> the CURRENT classification of exactly those items (TOCTOU guard)
  {"mode": "delete", "node": "node2", "items": [{"kind","target","expect"}], ...}
      -> re-check each item and delete it only if it is still what the scan
         said (same class, same size/mtime fingerprint); returns bytes freed

Rules enforced HERE, on the node that owns the files, not only in the UI:

* Only paths under the fixed roots below; no symlink is followed or removed
  (lstat), and realpath must stay inside its root.
* Anything a running container mounts, anything under a protected prefix
  (models referenced by the registry / bindings / rollback / workflows /
  music, secrets, Git checkouts, state, the Library) is PROTECTED.
* An active download or build touching a path makes it PROTECTED.
* Docker objects are addressed by id and re-inspected before removal; a
  running or referenced image is never removed; there is no `prune -a` of
  images, only `docker builder prune` for the build cache.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
DAY = 86400.0

#: Where the scanner may look at all. Everything else is invisible to it.
ROOTS = {
    "models": "/srv/models",
    "cache": "/srv/cache",
    "projects": "/srv/projects",
    "logs": "/srv/logs",
    "home_cache": os.path.join(HOME, ".cache"),
}
#: Always protected, whatever else is true.
BASE_PROTECTED = (
    "/srv/projects/gx-cluster/secrets",
    "/srv/projects/gx-cluster/state",
    "/srv/projects/gx-cluster/media/metadata",
    "/srv/projects/gx-cluster/backups",
    os.path.join(HOME, "Documents/Projects/Server/gx-cluster"),
    "/srv/models/music/acestep/checkpoints",
    "/srv/models/music-data/db",
    "/srv/logs/gx-git-sync",
    "/srv/logs/gx-control-ui/audit.log",
    "/srv/models/manifests",
)
#: Categories for the usage breakdown (first match wins).
CATEGORY_PREFIXES = (
    ("staging", ("/srv/models/staging", "/srv/projects/gx-music-staging")),
    ("generated_media", ("/srv/projects/gx-cluster/media/images", "/srv/projects/gx-cluster/media/videos",
                         "/srv/projects/gx-cluster/media/audio", "/srv/models/music-data/jobs",
                         "/srv/models/comfy-output")),
    ("temporary_uploads", ("/srv/projects/gx-cluster/media/tmp", "/srv/models/comfy-input",
                           "/srv/models/music-data/uploads", "/srv/models/music-data/api_audio",
                           "/srv/models/comfy-temp")),
    ("models", ("/srv/models",)),
    ("caches", ("/srv/cache", os.path.join(HOME, ".cache"))),
    ("logs", ("/srv/logs",)),
    ("projects", ("/srv/projects",)),
)


def _run(args, timeout=60.0, input_text=None):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, input=input_text)
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, "", str(exc)


def du(path: str) -> int:
    """Bytes under `path`, not following symlinks, one filesystem."""
    total = 0
    try:
        st = os.lstat(path)
    except OSError:
        return 0
    if not stat.S_ISDIR(st.st_mode):
        return st.st_blocks * 512 if stat.S_ISREG(st.st_mode) else 0
    dev = st.st_dev
    stack = [path]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        s = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISDIR(s.st_mode):
                        if s.st_dev == dev:
                            stack.append(e.path)
                    elif stat.S_ISREG(s.st_mode):
                        total += s.st_blocks * 512
        except OSError:
            continue
    return total


def newest_mtime(path: str, limit: int = 20000) -> float:
    try:
        newest = os.lstat(path).st_mtime
    except OSError:
        return 0.0
    if not os.path.isdir(path) or os.path.islink(path):
        return newest
    n = 0
    for root, dirs, files in os.walk(path):
        for name in files + dirs:
            try:
                newest = max(newest, os.lstat(os.path.join(root, name)).st_mtime)
            except OSError:
                pass
            n += 1
            if n > limit:
                return newest
    return newest


def inside(path: str, prefix: str) -> bool:
    path, prefix = os.path.normpath(path), os.path.normpath(prefix)
    return path == prefix or path.startswith(prefix.rstrip("/") + "/")


def root_of(path: str) -> str | None:
    real = os.path.realpath(path)
    for r in ROOTS.values():
        if inside(path, r) and inside(real, r):
            return r
    return None


# ------------------------------------------------------------- docker facts
def docker_state() -> dict:
    out = {"ok": False, "containers": [], "images": [], "mounts": [], "running_images": [],
           "used_images": [], "volumes": [], "build_cache_bytes": 0, "build_cache_reclaimable": 0}
    rc, text, _ = _run(["docker", "ps", "-a", "-q", "--no-trunc"], 20)
    if rc != 0:
        return out
    out["ok"] = True
    ids = text.split()
    if ids:
        rc, text, _ = _run(["docker", "inspect", *ids], 60)
        try:
            data = json.loads(text) if rc == 0 else []
        except ValueError:
            data = []
        for c in data:
            state = (c.get("State") or {})
            labels = (c.get("Config") or {}).get("Labels") or {}
            image_id = c.get("Image")
            entry = {"id": c.get("Id", "")[:12], "name": (c.get("Name") or "").lstrip("/"),
                     "image": (c.get("Config") or {}).get("Image"), "image_id": image_id,
                     "running": bool(state.get("Running")), "status": state.get("Status"),
                     "finished_at": state.get("FinishedAt"),
                     "compose": labels.get("com.docker.compose.project"),
                     "size_rw": None}
            out["containers"].append(entry)
            out["used_images"].append(image_id)
            if entry["running"]:
                out["running_images"].append(image_id)
                for m in c.get("Mounts") or []:
                    if m.get("Source"):
                        out["mounts"].append(m["Source"])
    rc, text, _ = _run(["docker", "images", "--no-trunc", "--format", "{{json .}}"], 30)
    for line in text.splitlines() if rc == 0 else []:
        try:
            i = json.loads(line)
        except ValueError:
            continue
        out["images"].append({"id": i.get("ID"), "repo": i.get("Repository"), "tag": i.get("Tag"),
                              "size": i.get("Size"), "created": i.get("CreatedSince")})
    rc, text, _ = _run(["docker", "system", "df", "--format", "{{json .}}"], 60)
    for line in text.splitlines() if rc == 0 else []:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("Type") == "Build Cache":
            out["build_cache_bytes"] = parse_size(row.get("Size"))
            out["build_cache_reclaimable"] = parse_size(str(row.get("Reclaimable", "")).split(" ")[0])
        if row.get("Type") == "Images":
            out["images_bytes"] = parse_size(row.get("Size"))
        if row.get("Type") == "Local Volumes":
            out["volumes_bytes"] = parse_size(row.get("Size"))
        if row.get("Type") == "Containers":
            out["containers_bytes"] = parse_size(row.get("Size"))
    return out


def parse_size(text) -> int:
    text = str(text or "").strip()
    units = {"B": 1, "KB": 1000, "MB": 1000**2, "GB": 1000**3, "TB": 1000**4,
             "KIB": 1024, "MIB": 1024**2, "GIB": 1024**3}
    num = "".join(ch for ch in text if ch.isdigit() or ch == ".")
    unit = text[len(num):].strip().upper() or "B"
    try:
        return int(float(num) * units.get(unit, 1))
    except ValueError:
        return 0


def active_writers() -> list[str]:
    """Paths an active download/build/transcode is writing to (best effort)."""
    paths = []
    rc, text, _ = _run(["ps", "-eo", "args"], 10)
    for line in text.splitlines() if rc == 0 else []:
        if any(k in line for k in ("hf download", "huggingface-cli download", "hf-verify.py", "docker build",
                                   "buildx", "rsync ")):
            for tok in line.split():
                if tok.startswith("/srv/") or tok.startswith(HOME):
                    paths.append(tok.rstrip("/"))
    return paths


# ------------------------------------------------------------ classification
MEDIA_TREES = ("/srv/models/image", "/srv/models/video", "/srv/models/shared")
BROAD_MOUNTS = ("/srv/models", "/srv/cache", "/srv/projects", "/srv/logs")


def protection(path: str, ctx: dict) -> str | None:
    """Why `path` must not be cleaned, or None."""
    names = ctx.get("protect_names") or {}
    if any(inside(path, t) for t in MEDIA_TREES):
        base = os.path.basename(path)
        if base in names:
            return f"protected: {names[base]}"
        if os.path.isdir(path):
            for root, _dirs, files in os.walk(path):
                hit = next((f for f in files if f in names), None)
                if hit:
                    return f"protected: contains {hit} ({names[hit]})"
    for p in list(BASE_PROTECTED) + list(ctx.get("protect", [])):
        if inside(path, p) or inside(p, path):
            return f"protected: {ctx.get('why', {}).get(p) or 'required by the cluster'} ({p})"
    for m in ctx.get("mounts", []):
        # Removing the path would remove a mount source: protected. A path merely
        # inside a broad mount (ComfyUI mounts all of /srv/models and /srv/cache)
        # is judged by the other rules (references, names, writers).
        if inside(m, path) or (inside(path, m) and os.path.normpath(m) not in BROAD_MOUNTS):
            return f"protected: mounted by a running container ({m})"
    for w in ctx.get("writers", []):
        if inside(path, w) or inside(w, path):
            return "protected: an active download or build is using it"
    return None


def fingerprint(path: str) -> dict:
    try:
        st = os.lstat(path)
    except OSError:
        return {"exists": False}
    return {"exists": True, "mtime": int(newest_mtime(path)), "is_link": stat.S_ISLNK(st.st_mode)}


def candidate(kind: str, target: str, cls: str, size: int, reason: str, *, name: str, category: str,
              consequence: str = "", mtime: float | None = None) -> dict:
    return {"kind": kind, "target": target, "class": cls, "bytes": int(size), "reason": reason,
            "name": name, "category": category, "consequence": consequence,
            "mtime": int(mtime) if mtime else None}


def files_older_than(directory: str, seconds: float, now: float) -> list[tuple[str, int, float]]:
    out = []
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return out
    for e in entries:
        try:
            st = e.stat(follow_symlinks=False)
        except OSError:
            continue
        if stat.S_ISLNK(st.st_mode):
            continue
        size = du(e.path)
        mt = newest_mtime(e.path)
        if now - mt > seconds:
            out.append((e.path, size, mt))
    return out


def classify(ctx: dict, docker: dict, now: float) -> list[dict]:
    node = ctx["node"]
    cands: list[dict] = []

    def add_path(path: str, cls: str, reason: str, category: str, consequence: str = "", name: str = "") -> None:
        if not os.path.lexists(path) or os.path.islink(path) or root_of(path) is None:
            return
        why = protection(path, ctx)
        if why:
            cls, reason = "protected", why
        cands.append(candidate("path", path, cls, du(path), reason, name=name or os.path.basename(path),
                               category=category, consequence=consequence, mtime=newest_mtime(path)))

    # --- SAFE: temporary data past its retention ---------------------------
    for d, age, what in (
        ("/srv/projects/gx-cluster/media/tmp", 6 * 3600, "Library temporary file older than 6 h"),
        ("/srv/models/comfy-input", DAY, "staged media-router input older than 24 h (the router purges these)"),
        ("/srv/models/comfy-temp", DAY, "ComfyUI temporary file older than 24 h"),
        ("/srv/models/music-data/api_audio", 6 * 3600, "music engine scratch older than 6 h"),
    ):
        for path, size, mt in files_older_than(d, age, now):
            add_path(path, "safe", what, "temporary_uploads", "none: temporary data")
    # rotated logs
    for root, _dirs, files in os.walk("/srv/logs"):
        if inside(root, "/srv/logs/gx-git-sync") or inside(root, "/srv/logs/acceptance"):
            continue
        for f in files:
            path = os.path.join(root, f)
            rotated = f.endswith((".gz", ".xz", ".zst")) or any(f.endswith(f".log.{i}") for i in range(1, 20))
            if rotated:
                try:
                    mt = os.lstat(path).st_mtime
                except OSError:
                    continue
                if now - mt > 14 * DAY:
                    add_path(path, "safe", "rotated log older than 14 days", "logs", "none: old log")
    # pip / playwright download caches in the home cache
    for rel, why in (("pip", "pip download cache"), ("pip-tools", "pip-tools cache")):
        path = os.path.join(HOME, ".cache", rel)
        if os.path.isdir(path):
            add_path(path, "safe", why, "caches", "re-downloaded when needed")
    for rel in ("pip", "pip-tools"):
        path = os.path.join("/srv/cache", rel)
        if os.path.isdir(path):
            add_path(path, "safe", f"{rel} download cache", "caches", "re-downloaded when needed")
    # interrupted Hugging Face downloads (not being written)
    for root, dirs, files in os.walk("/srv/models/staging"):
        for f in files:
            if f.endswith(".incomplete"):
                path = os.path.join(root, f)
                try:
                    mt = os.lstat(path).st_mtime
                except OSError:
                    continue
                if now - mt > DAY:
                    add_path(path, "safe", "partial download not written for 24 h", "staging",
                             "the download restarts that file if it is resumed")

    # --- docker ------------------------------------------------------------
    if docker.get("ok"):
        if docker.get("build_cache_bytes"):
            cands.append(candidate("docker_build_cache", "builder", "safe", docker["build_cache_bytes"],
                                   "Docker build cache (all unused build layers; `docker builder prune -a`): "
                                   "never used by a running workload",
                                   name="Docker build cache", category="docker_build_cache",
                                   consequence="the next image build starts without cached layers"))
        referenced = set(ctx.get("images", []))
        running = set(docker.get("running_images", []))
        used = set(docker.get("used_images", []))
        for img in docker.get("images", []):
            ref = f"{img['repo']}:{img['tag']}"
            size = parse_size(img.get("size"))
            if img["id"] in running:
                cls, reason = "protected", "image of a running container"
            elif ref in referenced or img["repo"] in referenced:
                cls, reason = "protected", "image referenced by the cluster configuration (needed to start it)"
            elif img["id"] in used:
                cls, reason = "review", "used by a stopped container"
            elif img["repo"] == "<none>":
                cls, reason = "safe", "dangling image (no name, no container)"
            else:
                cls, reason = "review", "not used by any container and not referenced by the configuration"
            cands.append(candidate("docker_image", img["id"], cls, size, reason, name=ref,
                                   category="docker_images",
                                   consequence="pulled or rebuilt again if something needs it later"))
        for c in docker.get("containers", []):
            if c["running"]:
                continue
            known = c["name"] in ctx.get("known_containers", []) or c.get("compose")
            cls = "review" if known else "safe"
            reason = ("stopped container of a known service (it may be started again)" if known
                      else "stopped ephemeral container")
            cands.append(candidate("docker_container", c["id"], cls, 0, reason, name=c["name"],
                                   category="docker_containers", consequence="its writable layer is lost"))

    # --- REVIEW: potentially useful ------------------------------------------
    if os.path.isdir("/srv/projects/gx-music-staging"):
        add_path("/srv/projects/gx-music-staging", "review",
                 "Stage A staging tree: gx-music now runs from the Git checkout (D-036)", "staging",
                 "the Stage A evidence and handoff copy are deleted (a copy is under state/gx-music-handoff)")
    for d in sorted(os.listdir("/srv/logs/acceptance")) if os.path.isdir("/srv/logs/acceptance") else []:
        path = os.path.join("/srv/logs/acceptance", d)
        if now - newest_mtime(path) > 7 * DAY:
            add_path(path, "review", "acceptance evidence older than 7 days", "logs",
                     "the evidence behind TEST_RESULTS.md is gone")
    for base in ("/srv/models/staging",):
        for d in sorted(os.listdir(base)) if os.path.isdir(base) else []:
            if d.startswith("."):
                continue
            add_path(os.path.join(base, d), "review", "staged model not assigned to an alias", "staging",
                     "the staged download must be repeated to install it")
    for base in ("/srv/models/gguf", "/srv/models/vllm", "/srv/models/deepseek"):
        for d in sorted(os.listdir(base)) if os.path.isdir(base) else []:
            path = os.path.join(base, d)
            if os.path.isdir(path) and not os.path.islink(path):
                add_path(path, "review", "model directory not referenced by any alias, rollback or binding",
                         "models", "the model must be downloaded again (Model Manager) to use it")
    for tree in MEDIA_TREES:
        for sub in ("diffusion_models", "loras", "checkpoints", "controlnet", "upscale_models",
                    "latent_upscale_models", "model_patches", "embeddings"):
            d = os.path.join(tree, sub)
            for path, size, mt in files_older_than(d, 0, now + 1) if os.path.isdir(d) else []:
                if size >= 100 * 1024 * 1024:
                    add_path(path, "review", "model file not referenced by any media workflow or alias", "models",
                             "the file must be downloaded again if a workflow needs it later")
    for rel in ("hf", "hf-stage", "huggingface", "torch", "triton", "vllm"):
        path = os.path.join("/srv/cache", rel)
        if os.path.isdir(path):
            add_path(path, "review", f"runtime cache /srv/cache/{rel} (may speed up the next start)", "caches",
                     "the next model start may be slower (compiled kernels / downloads are rebuilt)")
    hub = os.path.join(HOME, ".cache", "huggingface", "hub")
    if os.path.isdir(hub):
        for d in sorted(os.listdir(hub)):
            if d.startswith("models--"):
                add_path(os.path.join(hub, d), "review", "Hugging Face hub cache copy (models are served from "
                         "/srv/models, not from this cache)", "caches", "re-downloaded if a tool asks for it")
    if os.path.isdir("/srv/models/comfy-output"):
        add_path("/srv/models/comfy-output", "review",
                 "ComfyUI output copies on gx10-02 (the Library on gx10-01 keeps its own copy)",
                 "generated_media", "router job content URLs for old jobs stop working")
    if node == "node2" and os.path.isdir("/srv/models/music-data/jobs"):
        add_path("/srv/models/music-data/jobs", "review",
                 "gx10-02 copies of generated tracks (the Library on gx10-01 keeps its own copy)",
                 "generated_media", "remix/edit of those tracks re-uploads them from the Library")
    # --- PROTECTED, shown so the user can see why ------------------------------
    for p in ctx.get("protect", []):
        if os.path.lexists(p) and not any(c["target"] == p for c in cands):
            cands.append(candidate("path", p, "protected", du(p),
                                   f"protected: {ctx.get('why', {}).get(p) or 'required by the cluster'}",
                                   name=os.path.basename(p) or p, category=category_of(p), mtime=None))
    for p in BASE_PROTECTED:
        if os.path.lexists(p) and inside(p, "/srv"):
            if not any(c["target"] == p for c in cands):
                cands.append(candidate("path", p, "protected", du(p), "protected: cluster state, secrets, "
                                       "Git or Library metadata", name=p, category=category_of(p)))
    # A path both suggested and protected: protected wins; drop duplicates.
    seen: dict[tuple, dict] = {}
    for c in cands:
        key = (c["kind"], c["target"])
        if key not in seen or c["class"] == "protected":
            seen[key] = c
    return list(seen.values())


def category_of(path: str) -> str:
    for cat, prefixes in CATEGORY_PREFIXES:
        if any(inside(path, p) for p in prefixes):
            return cat
    return "other"


def usage(docker: dict) -> dict:
    total, used, free = shutil.disk_usage("/")
    cats: dict[str, int] = {}
    for cat, prefixes in CATEGORY_PREFIXES:
        size = 0
        for p in prefixes:
            if not os.path.isdir(p):
                continue
            # do not count a prefix already counted by a more specific category
            size += du(p)
        cats[cat] = size
    # nested prefixes were counted twice: subtract the specific ones from the general ones
    specific_models = sum(du(p) for p in ("/srv/models/staging", "/srv/models/music-data/jobs",
                                          "/srv/models/comfy-output", "/srv/models/comfy-input",
                                          "/srv/models/music-data/uploads", "/srv/models/music-data/api_audio",
                                          "/srv/models/comfy-temp") if os.path.isdir(p))
    cats["models"] = max(0, cats.get("models", 0) - specific_models)
    specific_projects = sum(du(p) for p in ("/srv/projects/gx-music-staging",
                                            "/srv/projects/gx-cluster/media/images",
                                            "/srv/projects/gx-cluster/media/videos",
                                            "/srv/projects/gx-cluster/media/audio",
                                            "/srv/projects/gx-cluster/media/tmp") if os.path.isdir(p))
    cats["projects"] = max(0, cats.get("projects", 0) - specific_projects)
    cats["docker_images"] = int(docker.get("images_bytes") or 0)
    cats["docker_build_cache"] = int(docker.get("build_cache_bytes") or 0)
    cats["docker_volumes"] = int(docker.get("volumes_bytes") or 0)
    cats["docker_containers"] = int(docker.get("containers_bytes") or 0)
    known = sum(cats.values())
    cats["other"] = max(0, used - known)
    return {"total": total, "used": used, "free": free,
            "percent": round(used / (used + free) * 100, 1) if used + free else None, "categories": cats}


def largest(paths: list[str], n: int = 15) -> list[dict]:
    rows = []
    for base in paths:
        if not os.path.isdir(base):
            continue
        for e in os.scandir(base):
            if e.is_symlink():
                continue
            rows.append({"path": e.path, "bytes": du(e.path)})
    rows.sort(key=lambda r: -r["bytes"])
    return rows[:n]


# ------------------------------------------------------------------- actions
def delete_item(item: dict, ctx: dict, docker: dict, now: float) -> dict:
    kind, target = item.get("kind"), str(item.get("target") or "")
    result = {"kind": kind, "target": target, "ok": False, "freed": 0}
    current = {(c["kind"], c["target"]): c for c in classify(ctx, docker, now)}.get((kind, target))
    if current is None:
        result["error"] = "no longer a cleanup candidate (changed since the scan)"
        return result
    allowed = ("safe",) if not item.get("allow_review") else ("safe", "review")
    if current["class"] not in allowed:
        result["error"] = f"refused: now classified {current['class']} ({current['reason']})"
        return result
    expect = item.get("expect") or {}
    if expect.get("mtime") and current.get("mtime") and int(current["mtime"]) > int(expect["mtime"]) + 1:
        result["error"] = "refused: it changed after the scan (modified since)"
        return result
    if kind == "path":
        if os.path.islink(target) or root_of(target) is None:
            result["error"] = "refused: path outside the allowed roots or a symlink"
            return result
        size = du(target)
        try:
            if os.path.isdir(target):
                shutil.rmtree(target)
            else:
                os.unlink(target)
        except OSError as exc:
            result["error"] = f"delete failed: {exc.strerror}"
            return result
        result.update(ok=not os.path.lexists(target), freed=size)
    elif kind == "docker_image":
        rc, _, err = _run(["docker", "image", "rm", target], 120)
        result.update(ok=rc == 0, freed=current["bytes"] if rc == 0 else 0, error=None if rc == 0 else err[-300:])
    elif kind == "docker_container":
        rc, _, err = _run(["docker", "rm", target], 60)
        result.update(ok=rc == 0, error=None if rc == 0 else err[-300:])
    elif kind == "docker_build_cache":
        before = docker.get("build_cache_bytes") or 0
        rc, out, err = _run(["docker", "builder", "prune", "-a", "-f"], 600)
        after = docker_state().get("build_cache_bytes") or 0
        result.update(ok=rc == 0, freed=max(0, before - after), error=None if rc == 0 else err[-300:])
    else:
        result["error"] = "unsupported kind"
    return result


def main(req: dict) -> dict:
    now = time.time()
    node = req.get("node") or "node1"
    docker = docker_state()
    ctx = {"node": node, "protect": [p for p in req.get("protect", []) if isinstance(p, str)],
           "why": req.get("why") or {}, "images": req.get("images") or [],
           "protect_names": req.get("protect_names") or {},
           "known_containers": req.get("known_containers") or [],
           "mounts": docker.get("mounts", []), "writers": active_writers()}
    mode = req.get("mode")
    if mode == "scan":
        t0 = time.time()
        cands = classify(ctx, docker, now)
        return {"node": node, "collected_at": now, "usage": usage(docker), "candidates": cands,
                "largest": largest(["/srv/models", "/srv/projects", "/srv/cache", "/srv/logs"]),
                "docker": {k: docker.get(k) for k in ("ok", "build_cache_bytes", "images_bytes", "volumes_bytes")},
                "active_writers": ctx["writers"], "seconds": round(time.time() - t0, 1)}
    if mode == "check":
        cur = {(c["kind"], c["target"]): c for c in classify(ctx, docker, now)}
        return {"node": node, "items": [cur.get((i.get("kind"), i.get("target"))) for i in req.get("items", [])]}
    if mode == "delete":
        results = [delete_item(i, ctx, docker, now) for i in req.get("items", [])[:200]]
        total, used, free = shutil.disk_usage("/")
        return {"node": node, "results": results, "free_after": free,
                "freed": sum(r["freed"] for r in results if r["ok"])}
    return {"error": "unknown mode"}


if __name__ == "__main__":
    raw = sys.argv[1] if len(sys.argv) > 1 else sys.stdin.readline()
    print(json.dumps(main(json.loads(raw))))
