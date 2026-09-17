"""gx-music on gx10-01 (D-036/D-037): the only way anything reaches the node-2
music supervisor.

    browser / API client
      -> gx10-01 (session or gateway key)          this module
      -> 192.168.100.11:18820 (RoCE fabric, bearer key from secrets/gx-music/api-key)
      -> ACE-Step 1.5 XL turbo on gx10-02

What it does:
* validates the request shape and resolves Library references: a track in
  the Library becomes `{job_id, index}` when node 2 still has the job, and is
  otherwise uploaded to node 2 once (the upload id is remembered);
* forwards the job and remembers it in a small state file (outside Git);
* a worker follows every job it submitted and, when a job completes,
  downloads WAV, FLAC and MP3 of every track, verifies them against the
  SHA-256 the supervisor reported, and adds each track to the canonical
  Media Library with its full recipe and lineage (idempotent);
* explains waiting jobs with the live resource view (Resource Control).

Nothing here ever returns a node-2 path, the node-2 key, or an engine URL.
"""

from __future__ import annotations

import builtins
import hashlib
import json
import logging
import re
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any
from collections.abc import Callable

from .media_library import LibraryError, MediaLibrary, NewAsset
from .redact import redact
from .util import HTTPError, http

log = logging.getLogger("gx.ui.music")

JOB_ID = re.compile(r"^mus-[a-f0-9]{32}$")
UPLOAD_ID = re.compile(r"^upl-[a-f0-9]{32}$")
OPERATIONS = {"generate": "/v1/music/generations", "remix": "/v1/music/remix",
              "edit": "/v1/music/edits", "extend": "/v1/music/extend"}
LIBRARY_OPERATION = {"generate": "generate", "remix": "remix", "edit": "repaint", "extend": "extend"}
TERMINAL = ("completed", "failed", "cancelled")
FORMATS = ("wav", "flac", "mp3")
AUDIO_TYPES = {"audio/wav": "wav", "audio/x-wav": "wav", "audio/wave": "wav", "audio/flac": "flac",
               "audio/x-flac": "flac", "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/ogg": "ogg",
               "audio/mp4": "m4a", "audio/x-m4a": "m4a"}
#: Fields a client may send; everything else is dropped before forwarding.
REQUEST_FIELDS = {
    "title", "prompt", "style_tags", "lyrics", "instrumental", "description", "vocal_language", "duration",
    "bpm", "key", "time_signature", "seed", "batch_size", "inference_steps", "infer_method", "thinking",
    "enhance_prompt", "lm_temperature", "lm_cfg_scale", "lm_top_p", "output_format", "reference",
    "guidance_scale", "source", "strength", "noise_strength", "start", "end", "mode", "crossfade",
    "seconds", "direction",
}
MAX_BODY_KEYS = 64


class MusicError(Exception):
    def __init__(self, message: str, status: int = 400, code: str = "music_error") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class MusicClient:
    """Fabric client for the node-2 supervisor. The key is read per call from
    its 0600 file, so a rotation needs no restart and nothing caches it."""

    def __init__(self, base: str, key_file: Path) -> None:
        self.base = base.rstrip("/")
        self.key_file = Path(key_file)

    def _key(self) -> str:
        try:
            key = self.key_file.read_text(encoding="utf-8").strip()
        except OSError:
            raise MusicError("gx-music is not configured on gx10-01 (API key file missing)", 503,
                             "not_configured") from None
        if len(key) < 32:
            raise MusicError("gx-music API key on gx10-01 is invalid", 503, "not_configured")
        return key

    def call(self, method: str, path: str, *, body: Any = None, raw: bytes | None = None,
             headers: dict[str, str] | None = None, timeout: float = 30) -> Any:
        hdrs = {"Authorization": f"Bearer {self._key()}", **(headers or {})}
        try:
            res = http(method, self.base + path, body=body, raw_body=raw, headers=hdrs, timeout=timeout)
        except HTTPError:
            raise MusicError("the music service on gx10-02 is not reachable", 503, "node_unavailable") from None
        try:
            data = res.json()
        except ValueError:
            data = None
        if 200 <= res.status < 300:
            return data
        err = (data or {}).get("error") if isinstance(data, dict) else None
        message = err.get("message") if isinstance(err, dict) else f"HTTP {res.status}"
        code = err.get("code") if isinstance(err, dict) else "upstream_error"
        status = res.status if res.status in (400, 404, 409, 413, 422, 503) else 502
        raise MusicError(redact(str(message))[:400], status, str(code)[:40])

    def download(self, path: str, dest: Path, timeout: float = 300) -> int:
        """Stream a file from node 2 into `dest`; returns the byte count."""
        import urllib.request

        req = urllib.request.Request(self.base + path, headers={"Authorization": f"Bearer {self._key()}"})
        total = 0
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp, dest.open("wb") as fh:
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    fh.write(chunk)
                    total += len(chunk)
        except OSError as exc:
            dest.unlink(missing_ok=True)
            raise MusicError(f"downloading the track from gx10-02 failed: {type(exc).__name__}", 502,
                             "download_failed") from None
        return total

    def health(self) -> dict:
        try:
            res = http("GET", self.base + "/health", timeout=4)
            return {"ok": res.status == 200, **(res.json() if res.status == 200 else {})}
        except (HTTPError, ValueError):
            return {"ok": False}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(4 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def clean_request(operation: str, body: Any) -> dict:
    """Coarse shape checks. Node 2 does the full, capability-aware validation."""
    if operation not in OPERATIONS:
        raise MusicError(f"unknown music operation {operation!r}", 404, "not_found")
    if not isinstance(body, dict):
        raise MusicError("request body must be a JSON object")
    if len(body) > MAX_BODY_KEYS:
        raise MusicError("too many fields")
    unknown = sorted(set(body) - REQUEST_FIELDS - {"source_asset_id", "reference_asset_id"})
    if unknown:
        raise MusicError(f"unsupported field(s): {', '.join(unknown[:8])}")
    return dict(body)


class MusicJobs:
    def __init__(self, client: MusicClient, library: MediaLibrary, state_file: Path, *,
                 audit: Callable[..., None] | None = None, results=None,
                 explain: Callable[[str], dict | None] | None = None, poll_interval: float = 3.0,
                 start_worker: bool = True) -> None:
        self.client = client
        self.library = library
        self.state_file = Path(state_file)
        self.audit = audit or (lambda **kw: None)
        self.results = results
        self.explain = explain or (lambda alias: None)
        self.poll_interval = poll_interval
        self._lock = threading.RLock()
        self._jobs: dict[str, dict] = self._load()
        self._cache: dict[str, tuple[float, dict]] = {}
        self._wake = threading.Event()
        self.last_error: str | None = None
        if start_worker:
            threading.Thread(target=self._loop, name="music-jobs", daemon=True).start()

    # ------------------------------------------------------------ state
    def _load(self) -> dict:
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        with self._lock:
            # keep the most recent 500 jobs
            items = sorted(self._jobs.items(), key=lambda kv: kv[1].get("submitted_at", 0))[-500:]
            self._jobs = dict(items)
            tmp.write_text(json.dumps(self._jobs, indent=1), encoding="utf-8")
        tmp.replace(self.state_file)

    def known(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._jobs

    # ------------------------------------------------------- references
    def _resolve_ref(self, ref: Any, field: str) -> tuple[dict, str | None]:
        """{asset_id} | {job_id,index} | {upload_id} -> node-2 source + parent asset."""
        if not isinstance(ref, dict):
            raise MusicError(f"{field} must be an object")
        if "asset_id" in ref:
            asset = self.library.get(str(ref["asset_id"]))
            if asset["type"] != "audio":
                raise MusicError(f"{field}: the selected Library item is not audio")
            settings = asset.get("settings") or {}
            job_id, index = settings.get("music_job_id"), settings.get("track_index")
            if job_id and JOB_ID.match(str(job_id)) and isinstance(index, int):
                try:
                    job = self.client.call("GET", f"/v1/music/{job_id}")
                    if job.get("status") == "completed" and index < len(job.get("tracks") or []):
                        return {"job_id": job_id, "index": index}, asset["id"]
                except MusicError as exc:
                    if exc.status != 404:
                        raise
            return {"upload_id": self._upload_asset(asset)}, asset["id"]
        if "job_id" in ref:
            if not JOB_ID.match(str(ref["job_id"])):
                raise MusicError(f"{field}: invalid job id")
            index = ref.get("index", 0)
            if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < 8:
                raise MusicError(f"{field}: invalid track index")
            parent = next((a["id"] for a in self.library.find_by_job(str(ref["job_id"]))
                           if (a.get("settings") or {}).get("track_index") == index), None)
            return {"job_id": ref["job_id"], "index": index}, parent
        if "upload_id" in ref:
            if not UPLOAD_ID.match(str(ref["upload_id"])):
                raise MusicError(f"{field}: invalid upload id")
            return {"upload_id": ref["upload_id"]}, None
        raise MusicError(f"{field} needs asset_id, job_id or upload_id")

    def _upload_asset(self, asset: dict) -> str:
        # The upload id is internal bookkeeping (stripped from public views).
        with self.library._connect() as con:  # noqa: SLF001 - same package
            row = con.execute("SELECT settings FROM assets WHERE id=?", (asset["id"],)).fetchone()
        cached = json.loads(row["settings"] or "{}").get("node2_upload_id") if row else None
        if cached and UPLOAD_ID.match(str(cached)):
            try:
                self.client.call("GET", f"/v1/music/uploads/{cached}")
                return str(cached)
            except MusicError as exc:
                if exc.status != 404:
                    raise
        fmt = "wav" if "wav" in (asset.get("variants") or {}) else asset["ext"]
        path = self.library.file_path(asset, fmt if asset["ext"] != fmt else None)
        if not path.is_file() or path.stat().st_size > 64 * 1024 * 1024:
            fmt = "mp3" if "mp3" in (asset.get("variants") or {}) else asset["ext"]
            path = self.library.file_path(asset, fmt if asset["ext"] != fmt else None)
        if not path.is_file():
            raise MusicError("the Library file for that track is missing", 404, "not_found")
        data = path.read_bytes()
        name = re.sub(r"[^A-Za-z0-9._-]+", "-", asset.get("title") or asset["id"])[:80] + "." + fmt
        up = self.client.call("POST", "/v1/music/uploads", raw=data, timeout=180,
                              headers={"Content-Type": "application/octet-stream",
                                       "X-Filename": urllib.parse.quote(name)})
        self.library.update_settings(asset["id"], node2_upload_id=up["id"])
        return str(up["id"])

    # ------------------------------------------------------------ submit
    def submit(self, operation: str, body: Any, *, user: str, via: str = "ui", ip: str = "") -> dict:
        req = clean_request(operation, body)
        parent_asset = None
        if "source_asset_id" in req:
            req["source"], parent_asset = self._resolve_ref({"asset_id": req.pop("source_asset_id")}, "source")
        elif isinstance(req.get("source"), dict):
            req["source"], parent_asset = self._resolve_ref(req["source"], "source")
        reference_asset = None
        if "reference_asset_id" in req:
            req["reference"], reference_asset = self._resolve_ref(
                {"asset_id": req.pop("reference_asset_id")}, "reference")
        elif isinstance(req.get("reference"), dict):
            req["reference"], reference_asset = self._resolve_ref(req["reference"], "reference")
        if operation != "generate" and "source" not in req:
            raise MusicError("choose the track to work on (source)")
        job = self.client.call("POST", OPERATIONS[operation], body=req, timeout=60)
        job_id = job.get("id")
        if not JOB_ID.match(str(job_id)):
            raise MusicError("the music service returned an invalid job", 502, "upstream_error")
        with self._lock:
            self._jobs[job_id] = {"user": user, "via": via, "operation": operation, "submitted_at": time.time(),
                                  "parent_asset": parent_asset, "reference_asset": reference_asset,
                                  "imported": False, "assets": []}
        self._save()
        self.audit(user=user, ip=ip, action=f"music.{operation}", outcome="queued", job=job_id, via=via)
        self._wake.set()
        return self.decorate(job)

    def upload(self, data: bytes, filename: str, content_type: str, *, title: str | None, user: str) -> dict:
        """A reference/source upload: kept in the Library (type audio, operation upload)."""
        ext = AUDIO_TYPES.get(content_type.split(";")[0].strip().lower())
        if ext is None:
            raise MusicError("upload WAV, FLAC, MP3, OGG or M4A audio")
        if not data:
            raise MusicError("the upload is empty")
        # Node 2 sniffs, probes and bounds the file (1-600 s, 64 MB); do that first.
        up = self.client.call("POST", "/v1/music/uploads", raw=data, timeout=180,
                              headers={"Content-Type": "application/octet-stream",
                                       "X-Filename": urllib.parse.quote(filename[:120] or f"upload.{ext}")})
        tmp = self.library.tmp_file("." + ext)
        tmp.write_bytes(data)
        asset = self.library.add(NewAsset(
            type="audio", ext=up.get("container") if up.get("container") in AUDIO_TYPES.values() else ext,
            operation="upload", data_path=tmp, title=title or filename or None,
            duration=up.get("duration_s"), sample_rate=up.get("sample_rate"), channels=up.get("channels"),
            settings={"uploaded_by": user, "original_filename": filename[:120]}))
        self.library.update_settings(asset["id"], node2_upload_id=up["id"])
        return self.library.get(asset["id"])

    # -------------------------------------------------------------- read
    def decorate(self, job: dict) -> dict:
        """A node-2 job view, plus Library links, local import state and the
        cluster-level explanation of a wait."""
        out = dict(job)
        out.pop("links", None)
        with self._lock:
            local = dict(self._jobs.get(job.get("id", ""), {}))
        out["library_assets"] = local.get("assets", [])
        out["imported"] = bool(local.get("imported"))
        out["import_error"] = local.get("import_error")
        out["parent_asset_id"] = local.get("parent_asset")
        out["reference_asset_id"] = local.get("reference_asset")
        out["submitted_via"] = local.get("via")
        phase = out.get("status")
        if phase == "completed" and not out["imported"] and job.get("id") in self._jobs:
            out["phase"] = "saving"
            out["phase_detail"] = "saving the tracks to the Library"
        else:
            out["phase"] = phase
            out["phase_detail"] = out.get("detail") or ""
        if phase == "waiting_for_resource":
            out["waiting"] = self.explain("gx-music")
        for t in out.get("tracks") or []:
            t["files"] = {fmt: {k: v for k, v in (meta or {}).items() if k != "url"}
                          for fmt, meta in (t.get("files") or {}).items()}
        return out

    def get(self, job_id: str) -> dict:
        if not JOB_ID.match(job_id):
            raise MusicError("invalid job id", 404, "not_found")
        return self.decorate(self.client.call("GET", f"/v1/music/{job_id}"))

    def list(self, status: str | None = None, limit: int = 50, mine_only: bool = False) -> list[dict]:
        q = f"?limit={max(1, min(200, int(limit)))}" + (f"&status={status}" if status else "")
        data = self.client.call("GET", f"/v1/music/jobs{q}")
        jobs = [self.decorate(j) for j in (data or {}).get("data", [])]
        if mine_only:
            jobs = [j for j in jobs if self.known(j.get("id", ""))]
        return jobs

    def lineage(self, job_id: str) -> dict:
        if not JOB_ID.match(job_id):
            raise MusicError("invalid job id", 404, "not_found")
        return self.client.call("GET", f"/v1/music/{job_id}/lineage")

    def cancel(self, job_id: str, *, user: str) -> dict:
        if not JOB_ID.match(job_id):
            raise MusicError("invalid job id", 404, "not_found")
        job = self.client.call("POST", f"/v1/music/{job_id}/cancel")
        self.audit(user=user, ip="", action="music.cancel", outcome="ok", job=job_id)
        return self.decorate(job)

    def model(self) -> dict:
        now = time.time()
        cached = self._cache.get("model")
        if cached and now - cached[0] < 5:
            return cached[1]
        info = self.client.call("GET", "/v1/music/model")
        self._cache["model"] = (now, info)
        return info

    def tags(self, q: str, limit: int) -> dict:
        q = urllib.parse.quote(q[:48])
        return self.client.call("GET", f"/v1/music/tags?q={q}&limit={max(1, min(50, limit))}")

    def lifecycle(self, op: str, *, user: str, if_idle: bool = False) -> dict:
        """Load or unload the engine. ``if_idle`` (the scheduler making room)
        makes the supervisor refuse while music is queued or pinned (D-038)."""
        if op not in ("load", "unload"):
            raise MusicError("unknown operation", 404, "not_found")
        body = {"if_idle": True} if op == "unload" and if_idle else None
        result = self.client.call("POST", f"/v1/music/{op}", body=body, timeout=1200 if op == "load" else 120)
        self.audit(user=user, ip="", action=f"music.{op}", outcome="ok", if_idle=if_idle)
        return result

    # ------------------------------------------------------------ worker
    def _loop(self) -> None:
        while True:
            try:
                self.sweep()
                self.last_error = None
            except Exception as exc:  # noqa: BLE001 - the worker must never die
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("music sweep failed: %s", self.last_error)
            self._wake.wait(self.poll_interval)
            self._wake.clear()

    def pending(self) -> builtins.list[str]:
        with self._lock:
            return [jid for jid, j in self._jobs.items() if not j.get("imported") and not j.get("final")]

    def sweep(self) -> None:
        for job_id in self.pending():
            try:
                job = self.client.call("GET", f"/v1/music/{job_id}")
            except MusicError as exc:
                if exc.status == 404:
                    self._mark(job_id, final=True, import_error="the job no longer exists on gx10-02")
                    continue
                raise
            status = job.get("status")
            if status in ("failed", "cancelled"):
                self._mark(job_id, final=True)
                if self.results and status == "failed":
                    self.results.record("gx-music", "inference", False, (job.get("error") or {}).get("message", ""))
                continue
            if status == "completed":
                self.import_job(job)

    def _mark(self, job_id: str, **values: Any) -> None:
        with self._lock:
            if job_id in self._jobs:
                self._jobs[job_id].update(values)
        self._save()

    def import_job(self, job: dict) -> builtins.list[str]:
        """Download and register every track of a completed job (idempotent)."""
        job_id = job["id"]
        with self._lock:
            local = dict(self._jobs.get(job_id, {}))
        existing = {(a.get("settings") or {}).get("track_index"): a["id"] for a in self.library.find_by_job(job_id)}
        parent_asset = local.get("parent_asset")
        parent_job = job.get("parent_job_id")
        if not parent_asset and parent_job:
            # A source given as {"job_id", "index"} (music API): link to that track's asset.
            pindex = int(job.get("parent_index") or 0)
            parent_asset = next((a["id"] for a in self.library.find_by_job(parent_job)
                                 if (a.get("settings") or {}).get("track_index") == pindex), None)
            if parent_asset is None:
                with self._lock:
                    parent_pending = parent_job in self._jobs and not self._jobs[parent_job].get("final")
                if parent_pending:
                    return []  # the parent is still being saved; the next sweep imports this job
        req = job.get("request") or {}
        params = req.get("parameters") or {}
        ids: list[str] = []
        for track in job.get("tracks") or []:
            index = int(track.get("index", 0))
            if index in existing:
                ids.append(existing[index])
                continue
            paths: dict[str, Path] = {}
            try:
                for fmt in FORMATS:
                    meta = (track.get("files") or {}).get(fmt)
                    if not meta:
                        continue
                    dest = self.library.tmp_file("." + fmt)
                    paths[fmt] = dest  # registered first, so a failed check below still cleans it up
                    size = self.client.download(f"/v1/music/{job_id}/content?index={index}&format={fmt}", dest)
                    if meta.get("sha256") and _sha256(dest) != meta["sha256"]:
                        raise MusicError(f"{fmt} of track {index} failed its SHA-256 check", 502, "checksum")
                    if meta.get("bytes") and size != meta["bytes"]:
                        raise MusicError(f"{fmt} of track {index} has the wrong size", 502, "checksum")
                if "wav" not in paths and not paths:
                    raise MusicError("the track has no downloadable audio", 502, "no_audio")
                primary = "wav" if "wav" in paths else next(iter(paths))
                model = job.get("model") or {}
                title = job.get("title") or "Untitled"
                if len(job.get("tracks") or []) > 1:
                    title = f"{title} ({index + 1})"
                asset = self.library.add(NewAsset(
                    type="audio", ext=primary, operation=LIBRARY_OPERATION.get(job.get("operation", ""), "generate"),
                    data_path=paths.pop(primary), variant_paths=paths, title=title,
                    model_alias="gx-music", model_repo=model.get("dit_repo"), model_revision=model.get("dit_revision"),
                    workflow=f"ace-step:{model.get('dit_name', '')}",
                    prompt=track.get("caption") or req.get("prompt"), seed=track.get("seed"),
                    steps=params.get("inference_steps"), strength=params.get("strength"),
                    duration=track.get("duration_s"), lyrics=track.get("lyrics") or req.get("lyrics"),
                    tags=list(req.get("style_tags") or []),
                    bpm=_num(track.get("bpm")), music_key=track.get("key"),
                    time_signature=_ts(track.get("time_signature")),
                    sample_rate=track.get("sample_rate"), channels=track.get("channels"),
                    waveform=track.get("waveform"),
                    parent_id=parent_asset, job_id=job_id,
                    settings={"music_job_id": job_id, "track_index": index, "operation": job.get("operation"),
                              "request": req, "timings": job.get("timings"), "model": model,
                              "engine_seed": track.get("engine_seed"), "genres": track.get("genres"),
                              "peak": track.get("peak"), "rms_dbfs": track.get("rms_dbfs"),
                              "bit_depth": track.get("bit_depth"), "reference_asset": local.get("reference_asset"),
                              "parent_job_id": job.get("parent_job_id"), "lm_model": track.get("lm_model"),
                              "dit_model": track.get("dit_model")},
                ))
                ids.append(asset["id"])
            except (MusicError, LibraryError) as exc:
                for p in paths.values():
                    p.unlink(missing_ok=True)
                self._mark(job_id, import_error=str(exc))
                raise
        if job_id in self._jobs:
            self._mark(job_id, imported=True, final=True, assets=ids, import_error=None)
        if self.results:
            timings = job.get("timings") or {}
            self.results.record("gx-music", "inference", True, f"{job.get('operation')} ok",
                                seconds=timings.get("generate_s"))
        self.audit(user=local.get("user", "?"), ip="", action=f"music.{job.get('operation')}", outcome="saved",
                   job=job_id, assets=ids)
        return ids


def _num(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _ts(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value)
    return {"2": "2/4", "3": "3/4", "4": "4/4", "6": "6/8"}.get(text, text[:8])
