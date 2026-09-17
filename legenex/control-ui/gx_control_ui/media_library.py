"""The permanent media library (D-034).

Every image and video produced through the Control UI (and every source a
user uploads for editing) is stored here, outside Git:

    <root>/images/<id>.<ext>
    <root>/videos/<id>.<ext>
    <root>/thumbnails/<id>.jpg
    <root>/metadata/library.db      SQLite, schema versioned by PRAGMA user_version

Rules:
* Asset ids are server-generated (`a_<24 hex>`); no caller string ever
  becomes a path. Files are resolved from the database row only.
* An edit, variation or animation is a NEW asset whose `parent_id` points at
  its source. Nothing is ever overwritten.
* Deleting an asset removes its files and its row; children keep their own
  files and remember the deleted parent's id (`parent_deleted`).
* Every write is a transaction; the database is the source of truth for the
  listing, and files are written before their row is committed.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import secrets
import sqlite3
import subprocess
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass
from collections.abc import Iterator
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
ASSET_ID = re.compile(r"^a_[0-9a-f]{24}$")
MEDIA_TYPES = {
    "png": ("image", "image/png"), "jpg": ("image", "image/jpeg"), "webp": ("image", "image/webp"),
    "mp4": ("video", "video/mp4"), "mov": ("video", "video/quicktime"), "webm": ("video", "video/webm"),
}
SORTS = {"newest": "created_at DESC", "oldest": "created_at ASC", "title": "COALESCE(title, prompt) ASC",
         "size": "file_size DESC"}
OPERATIONS = ("generate", "edit", "variation", "i2v", "v2v", "upload")
MAX_ZIP_ITEMS = 200
MAX_ZIP_BYTES = 4 * 1024 ** 3

_MIGRATIONS = {
    1: """
    CREATE TABLE assets (
        id            TEXT PRIMARY KEY,
        type          TEXT NOT NULL CHECK (type IN ('image','video')),
        ext           TEXT NOT NULL,
        media_type    TEXT NOT NULL,
        filename      TEXT NOT NULL,
        title         TEXT,
        created_at    REAL NOT NULL,
        operation     TEXT NOT NULL,
        model_alias   TEXT,
        model_repo    TEXT,
        model_revision TEXT,
        workflow      TEXT,
        prompt        TEXT,
        negative_prompt TEXT,
        seed          INTEGER,
        steps         INTEGER,
        guidance      REAL,
        strength      REAL,
        width         INTEGER,
        height        INTEGER,
        duration      REAL,
        fps           REAL,
        frame_count   INTEGER,
        distinct_frames INTEGER,
        file_size     INTEGER NOT NULL,
        sha256        TEXT,
        parent_id     TEXT,
        parent_deleted INTEGER NOT NULL DEFAULT 0,
        favourite     INTEGER NOT NULL DEFAULT 0,
        is_test       INTEGER NOT NULL DEFAULT 0,
        job_id        TEXT,
        router_job_id TEXT,
        settings      TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX assets_created ON assets(created_at);
    CREATE INDEX assets_parent ON assets(parent_id);
    CREATE INDEX assets_type ON assets(type);
    """,
}


class LibraryError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class NewAsset:
    type: str
    ext: str
    operation: str
    data_path: Path | None = None
    data: bytes | None = None
    title: str | None = None
    model_alias: str | None = None
    model_repo: str | None = None
    model_revision: str | None = None
    workflow: str | None = None
    prompt: str | None = None
    negative_prompt: str | None = None
    seed: int | None = None
    steps: int | None = None
    guidance: float | None = None
    strength: float | None = None
    width: int | None = None
    height: int | None = None
    duration: float | None = None
    fps: float | None = None
    frame_count: int | None = None
    distinct_frames: int | None = None
    parent_id: str | None = None
    job_id: str | None = None
    router_job_id: str | None = None
    is_test: bool = False
    settings: dict | None = None


def new_id() -> str:
    return "a_" + secrets.token_hex(12)


def _sha256_file(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(4 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class MediaTools:
    """Thumbnails and video probing. ffmpeg runs in a local image, no network."""

    def __init__(self, ffmpeg_image: str = "linuxserver/ffmpeg:latest", enabled: bool = True) -> None:
        self.image = ffmpeg_image
        self.enabled = enabled

    def _docker(self, directory: Path, args: list[str], timeout: float) -> subprocess.CompletedProcess:
        cmd = ["docker", "run", "--rm", "--network", "none", "--memory", "1g", "--cpus", "2",
               "-v", f"{directory}:/m:ro", "--entrypoint", args[0], self.image, *args[1:]]
        return subprocess.run(cmd, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)

    def probe(self, path: Path) -> dict:
        """Frame count, size, fps, duration and distinct-frame count of a video."""
        if not self.enabled:
            return {}
        out: dict[str, Any] = {}
        try:
            res = self._docker(path.parent, [
                "ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
                "-show_entries", "stream=codec_name,width,height,r_frame_rate,nb_read_frames",
                "-show_entries", "format=duration", "-of", "json", f"/m/{path.name}"], 180)
            if res.returncode == 0:
                data = json.loads(res.stdout or b"{}")
                stream = (data.get("streams") or [{}])[0]
                num, _, den = str(stream.get("r_frame_rate", "0/1")).partition("/")
                fps = float(num) / float(den or 1) if float(den or 1) else None
                out.update({"codec": stream.get("codec_name"), "width": stream.get("width"),
                            "height": stream.get("height"), "fps": round(fps, 3) if fps else None,
                            "frame_count": int(stream.get("nb_read_frames") or 0) or None,
                            "duration": float((data.get("format") or {}).get("duration") or 0) or None})
            res = self._docker(path.parent, ["ffmpeg", "-v", "error", "-i", f"/m/{path.name}", "-an",
                                             "-f", "framemd5", "-"], 180)
            if res.returncode == 0:
                hashes = [ln.split(b",")[-1].strip() for ln in res.stdout.splitlines()
                          if ln and not ln.startswith(b"#")]
                out["distinct_frames"] = len(set(hashes))
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
        return out

    def video_thumbnail(self, path: Path, dest: Path) -> bool:
        if not self.enabled:
            return False
        try:
            res = self._docker(path.parent, ["ffmpeg", "-v", "error", "-i", f"/m/{path.name}", "-frames:v", "1",
                                             "-vf", "scale=512:-2", "-f", "image2", "-c:v", "mjpeg", "-q:v", "4",
                                             "-"], 120)
        except (OSError, subprocess.SubprocessError):
            return False
        if res.returncode != 0 or not res.stdout:
            return False
        dest.write_bytes(res.stdout)
        return True

    @staticmethod
    def image_thumbnail(path: Path, dest: Path) -> tuple[int | None, int | None]:
        """Writes a 512 px JPEG thumbnail; returns the source size."""
        try:
            from PIL import Image  # system Pillow; optional
        except ImportError:
            return None, None
        try:
            with Image.open(path) as im:
                im.load()
                size = im.size
                thumb = im.convert("RGB")
                thumb.thumbnail((512, 512))
                thumb.save(dest, "JPEG", quality=82)
                return size
        except (OSError, ValueError):
            return None, None


class MediaLibrary:
    def __init__(self, root: Path, tools: MediaTools | None = None) -> None:
        self.root = Path(root)
        self.tools = tools or MediaTools()
        self._lock = threading.RLock()
        for sub in ("images", "videos", "thumbnails", "metadata", "tmp"):
            (self.root / sub).mkdir(parents=True, exist_ok=True, mode=0o750)
        self.db_path = self.root / "metadata" / "library.db"
        self._migrate()

    # ------------------------------------------------------------ database
    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.db_path, timeout=15, isolation_level=None)
        try:
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA foreign_keys=ON")
            con.execute("PRAGMA journal_mode=WAL")
            yield con
        finally:
            con.close()

    def _migrate(self) -> None:
        with self._lock, self._connect() as con:
            current = con.execute("PRAGMA user_version").fetchone()[0]
            for version in sorted(_MIGRATIONS):
                if version <= current:
                    continue
                con.execute("BEGIN IMMEDIATE")
                try:
                    for statement in _MIGRATIONS[version].split(";"):
                        if statement.strip():
                            con.execute(statement)
                    con.execute(f"PRAGMA user_version={version}")
                    con.execute("COMMIT")
                except Exception:
                    con.execute("ROLLBACK")
                    raise

    @property
    def schema_version(self) -> int:
        with self._connect() as con:
            return int(con.execute("PRAGMA user_version").fetchone()[0])

    # --------------------------------------------------------------- paths
    def file_path(self, row: dict | sqlite3.Row) -> Path:
        folder = "images" if row["type"] == "image" else "videos"
        return self.root / folder / f"{row['id']}.{row['ext']}"

    def thumb_path(self, asset_id: str) -> Path:
        return self.root / "thumbnails" / f"{asset_id}.jpg"

    def tmp_file(self, suffix: str = "") -> Path:
        fd, name = tempfile.mkstemp(dir=self.root / "tmp", suffix=suffix)
        os.close(fd)
        return Path(name)

    # ---------------------------------------------------------------- write
    def add(self, asset: NewAsset) -> dict:
        if asset.type not in ("image", "video") or asset.ext not in MEDIA_TYPES \
                or MEDIA_TYPES[asset.ext][0] != asset.type:
            raise LibraryError(f"unsupported {asset.type} format {asset.ext!r}")
        if asset.operation not in OPERATIONS:
            raise LibraryError(f"unknown operation {asset.operation!r}")
        if asset.parent_id is not None and not ASSET_ID.match(asset.parent_id):
            raise LibraryError("invalid parent id")
        asset_id = new_id()
        folder = "images" if asset.type == "image" else "videos"
        dest = self.root / folder / f"{asset_id}.{asset.ext}"
        tmp = dest.with_suffix(dest.suffix + ".part")
        if asset.data is not None:
            tmp.write_bytes(asset.data)
        elif asset.data_path is not None:
            os.replace(asset.data_path, tmp)
        else:
            raise LibraryError("no media data")
        os.chmod(tmp, 0o640)
        os.replace(tmp, dest)

        width, height = asset.width, asset.height
        thumb = self.thumb_path(asset_id)
        probe: dict = {}
        if asset.type == "image":
            w, h = self.tools.image_thumbnail(dest, thumb)
            width, height = width or w, height or h
        else:
            probe = self.tools.probe(dest)
            self.tools.video_thumbnail(dest, thumb)
            width = width or probe.get("width")
            height = height or probe.get("height")
        row = {
            "id": asset_id, "type": asset.type, "ext": asset.ext, "media_type": MEDIA_TYPES[asset.ext][1],
            "filename": f"{asset_id}.{asset.ext}", "title": (asset.title or None),
            "created_at": time.time(), "operation": asset.operation,
            "model_alias": asset.model_alias, "model_repo": asset.model_repo,
            "model_revision": asset.model_revision, "workflow": asset.workflow,
            "prompt": asset.prompt, "negative_prompt": asset.negative_prompt, "seed": asset.seed,
            "steps": asset.steps, "guidance": asset.guidance, "strength": asset.strength,
            "width": width, "height": height,
            "duration": asset.duration or probe.get("duration"),
            "fps": asset.fps or probe.get("fps"),
            "frame_count": asset.frame_count or probe.get("frame_count"),
            "distinct_frames": asset.distinct_frames or probe.get("distinct_frames"),
            "file_size": dest.stat().st_size, "sha256": _sha256_file(dest),
            "parent_id": asset.parent_id, "favourite": 0, "is_test": int(bool(asset.is_test)),
            "job_id": asset.job_id, "router_job_id": asset.router_job_id,
            "settings": json.dumps(asset.settings or {}, sort_keys=True),
        }
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        try:
            with self._lock, self._connect() as con:
                con.execute("BEGIN IMMEDIATE")
                if asset.parent_id and not con.execute("SELECT 1 FROM assets WHERE id=?",
                                                       (asset.parent_id,)).fetchone():
                    con.execute("ROLLBACK")
                    raise LibraryError("parent asset does not exist", 404)
                con.execute(f"INSERT INTO assets ({cols}) VALUES ({marks})", tuple(row.values()))  # noqa: S608
                con.execute("COMMIT")
        except Exception:
            dest.unlink(missing_ok=True)
            thumb.unlink(missing_ok=True)
            raise
        return self.get(asset_id)

    def update(self, asset_id: str, *, title: str | None = None, favourite: bool | None = None) -> dict:
        self._check_id(asset_id)
        sets: list[str] = []
        args: list[Any] = []
        if title is not None:
            title = title.strip()
            if len(title) > 200 or "\x00" in title:
                raise LibraryError("title must be at most 200 characters")
            sets.append("title=?")
            args.append(title or None)
        if favourite is not None:
            sets.append("favourite=?")
            args.append(1 if favourite else 0)
        if not sets:
            raise LibraryError("nothing to update")
        with self._lock, self._connect() as con:
            cur = con.execute(f"UPDATE assets SET {', '.join(sets)} WHERE id=?", (*args, asset_id))  # noqa: S608
            if cur.rowcount != 1:
                raise LibraryError("no such asset", 404)
        return self.get(asset_id)

    def delete(self, ids: list[str]) -> dict:
        ids = list(dict.fromkeys(ids))
        if not ids or len(ids) > 500:
            raise LibraryError("select between 1 and 500 assets")
        for asset_id in ids:
            self._check_id(asset_id)
        removed = []
        with self._lock, self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            rows = con.execute(f"SELECT * FROM assets WHERE id IN ({','.join('?' * len(ids))})",  # noqa: S608
                               ids).fetchall()
            for row in rows:
                con.execute("UPDATE assets SET parent_deleted=1 WHERE parent_id=?", (row["id"],))
                con.execute("DELETE FROM assets WHERE id=?", (row["id"],))
                removed.append(dict(row))
            con.execute("COMMIT")
        for row in removed:
            self.file_path(row).unlink(missing_ok=True)
            self.thumb_path(row["id"]).unlink(missing_ok=True)
        return {"deleted": [r["id"] for r in removed], "missing": sorted(set(ids) - {r["id"] for r in removed})}

    # ----------------------------------------------------------------- read
    @staticmethod
    def _check_id(asset_id: str) -> None:
        if not isinstance(asset_id, str) or not ASSET_ID.match(asset_id):
            raise LibraryError("invalid asset id")

    def _public(self, row: sqlite3.Row, con: sqlite3.Connection | None = None) -> dict:
        d = dict(row)
        d["favourite"] = bool(d["favourite"])
        d["is_test"] = bool(d["is_test"])
        d["parent_deleted"] = bool(d["parent_deleted"])
        try:
            d["settings"] = json.loads(d.get("settings") or "{}")
        except ValueError:
            d["settings"] = {}
        d["url"] = f"/api/media/assets/{d['id']}/file"
        d["thumbnail_url"] = f"/api/media/assets/{d['id']}/thumbnail"
        d["download_url"] = f"/api/media/assets/{d['id']}/file?download=1"
        return d

    def get(self, asset_id: str, *, lineage: bool = False) -> dict:
        self._check_id(asset_id)
        with self._connect() as con:
            row = con.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
            if row is None:
                raise LibraryError("no such asset", 404)
            out = self._public(row)
            if lineage:
                out["children"] = [self._brief(r) for r in con.execute(
                    "SELECT * FROM assets WHERE parent_id=? ORDER BY created_at", (asset_id,))]
                ancestors: list[dict] = []
                parent = row["parent_id"]
                seen = {asset_id}
                while parent and parent not in seen and len(ancestors) < 50:
                    seen.add(parent)
                    prow = con.execute("SELECT * FROM assets WHERE id=?", (parent,)).fetchone()
                    if prow is None:
                        ancestors.append({"id": parent, "deleted": True})
                        break
                    ancestors.append(self._brief(prow))
                    parent = prow["parent_id"]
                out["ancestors"] = ancestors
        return out

    def _brief(self, row: sqlite3.Row) -> dict:
        return {"id": row["id"], "type": row["type"], "operation": row["operation"],
                "title": row["title"], "prompt": (row["prompt"] or "")[:160], "created_at": row["created_at"],
                "thumbnail_url": f"/api/media/assets/{row['id']}/thumbnail"}

    def search(self, *, q: str = "", type_: str = "", model: str = "", operation: str = "",
               favourite: bool | None = None, sort: str = "newest", limit: int = 60, offset: int = 0,
               include_tests: bool = True) -> dict:
        where: list[str] = []
        args: list[Any] = []
        if q:
            if len(q) > 200:
                raise LibraryError("search text is too long")
            like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            where.append("(prompt LIKE ? ESCAPE '\\' OR title LIKE ? ESCAPE '\\' OR id = ?)")
            args += [like, like, q]
        if type_:
            if type_ not in ("image", "video"):
                raise LibraryError("type must be image or video")
            where.append("type=?")
            args.append(type_)
        if model:
            where.append("(model_alias=? OR model_repo=?)")
            args += [model, model]
        if operation:
            if operation not in OPERATIONS:
                raise LibraryError("unknown operation filter")
            where.append("operation=?")
            args.append(operation)
        if favourite is not None:
            where.append("favourite=?")
            args.append(1 if favourite else 0)
        if not include_tests:
            where.append("is_test=0")
        if sort not in SORTS:
            raise LibraryError("unknown sort")
        limit = max(1, min(200, int(limit)))
        offset = max(0, int(offset))
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        with self._connect() as con:
            total = con.execute(f"SELECT COUNT(*) FROM assets {clause}", args).fetchone()[0]  # noqa: S608
            rows = con.execute(f"SELECT * FROM assets {clause} ORDER BY {SORTS[sort]} LIMIT ? OFFSET ?",  # noqa: S608
                               (*args, limit, offset)).fetchall()
            facets = {
                "models": [r[0] for r in con.execute(
                    "SELECT DISTINCT model_alias FROM assets WHERE model_alias IS NOT NULL ORDER BY 1")],
                "operations": [r[0] for r in con.execute("SELECT DISTINCT operation FROM assets ORDER BY 1")],
            }
            counts = {r[0]: r[1] for r in con.execute("SELECT type, COUNT(*) FROM assets GROUP BY type")}
        return {"total": total, "items": [self._public(r) for r in rows], "facets": facets,
                "counts": {"image": counts.get("image", 0), "video": counts.get("video", 0)},
                "limit": limit, "offset": offset}

    def stats(self) -> dict:
        with self._connect() as con:
            rows = con.execute("SELECT type, operation, COUNT(*), COALESCE(SUM(file_size),0) FROM assets "
                               "GROUP BY type, operation").fetchall()
        return {"by": [{"type": r[0], "operation": r[1], "count": r[2], "bytes": r[3]} for r in rows],
                "schema_version": self.schema_version}

    # ------------------------------------------------------------------ zip
    def build_zip(self, ids: list[str]) -> tuple[Path, int]:
        """A ZIP of exactly the selected assets (plus their metadata). No other files."""
        ids = list(dict.fromkeys(ids))
        if not ids or len(ids) > MAX_ZIP_ITEMS:
            raise LibraryError(f"select between 1 and {MAX_ZIP_ITEMS} assets")
        for asset_id in ids:
            self._check_id(asset_id)
        with self._connect() as con:
            rows = con.execute(f"SELECT * FROM assets WHERE id IN ({','.join('?' * len(ids))})",  # noqa: S608
                               ids).fetchall()
        if len(rows) != len(ids):
            raise LibraryError("some selected assets no longer exist", 404)
        total = sum(int(r["file_size"]) for r in rows)
        if total > MAX_ZIP_BYTES:
            raise LibraryError("the selection is larger than 4 GiB; download fewer items", 413)
        out = self.tmp_file(".zip")
        manifest = []
        used: set[str] = set()
        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_STORED) as zf:
            for row in rows:
                base = _safe_name(row["title"] or row["prompt"] or row["id"])
                name = f"{base}-{row['id'][2:10]}.{row['ext']}"
                while name in used:
                    name = f"{base}-{secrets.token_hex(3)}.{row['ext']}"
                used.add(name)
                zf.write(self.file_path(row), arcname=name)
                meta = self._public(row)
                meta.pop("url", None)
                meta.pop("thumbnail_url", None)
                meta.pop("download_url", None)
                manifest.append({"file": name, **meta})
            zf.writestr("gx-media-manifest.json", json.dumps(manifest, indent=2, default=str))
        return out, len(rows)

    def cleanup_tmp(self, max_age: float = 3600) -> None:
        cutoff = time.time() - max_age
        for p in (self.root / "tmp").iterdir():
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                continue


def _safe_name(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", text.strip())[:48].strip("-.")
    return cleaned or "asset"


def read_range(path: Path, start: int, end: int) -> bytes:
    with path.open("rb") as fh:
        fh.seek(start)
        return fh.read(end - start + 1)


def image_bytes_info(data: bytes) -> tuple[str, int | None, int | None]:
    """(ext, width, height) of an uploaded image, or raise LibraryError."""
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow is present on gx10-01
        Image = None  # type: ignore[assignment]
    head = data[:16]
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        ext = "png"
    elif head.startswith(b"\xff\xd8\xff"):
        ext = "jpg"
    elif head[:4] == b"RIFF" and data[8:12] == b"WEBP":
        ext = "webp"
    else:
        raise LibraryError("unsupported image: send PNG, JPEG or WebP")
    if Image is None:
        return ext, None, None
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.verify()
        with Image.open(io.BytesIO(data)) as im:
            w, h = im.size
    except Exception as exc:  # noqa: BLE001 - any decoder failure is a bad upload
        raise LibraryError(f"the image could not be decoded: {type(exc).__name__}") from None
    if w * h > 16_777_216 or max(w, h) > 4096:
        raise LibraryError(f"the image is {w}x{h}; the limit is 4096 px per side and 16 MP")
    return ext, w, h


def video_ext(head: bytes) -> str:
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "mov" if head[8:12] == b"qt  " else "mp4"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "webm"
    raise LibraryError("unsupported video: send MP4, MOV or WebM")
