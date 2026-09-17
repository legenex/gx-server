"""gx-image model catalogue and edit-mask validation (Build V3, workstream IMG).

The catalogue has ONE source: the media router's pure module
``legenex/media/router/gx_media_router/image_models.py`` in the same checkout,
so the Control Center, GX-Playground and the router can never disagree on
model ids, sizes or edit modes. The Control Center validates a Create request
against it before anything is queued; the router validates again.

Edit masks arrive from the browser as a small PNG data URL (white = may
change). They are decoded here with the standard library, bounded, and
measured (coverage), so an empty or malformed mask is refused with a clear
message instead of producing an unchanged image on gx10-02.
"""

from __future__ import annotations

import base64
import binascii
import functools
import hashlib
import importlib
import importlib.util
import json
import logging
import re
import struct
import sys
import time
import zlib
from pathlib import Path
from types import ModuleType
from typing import Any

log = logging.getLogger("gx_control_ui.image_catalog")

_PACKAGE = "gx_media_router_catalog"
MASK_MAX_B64 = 48_000
MASK_MAX_SIDE = 1024
MASK_MIN_COVERAGE = 0.002
MAX_RECTS = 20
_DATA_URL = re.compile(r"^data:image/png;base64,(?P<data>[A-Za-z0-9+/=\s]+)$")


class CatalogError(ValueError):
    """A request does not fit the gx-image catalogue."""


@functools.lru_cache(maxsize=4)
def _module(repo_root: str) -> ModuleType:
    pkg_dir = Path(repo_root) / "legenex" / "media" / "router" / "gx_media_router"
    if _PACKAGE not in sys.modules:
        spec = importlib.util.spec_from_file_location(_PACKAGE, pkg_dir / "__init__.py",
                                                      submodule_search_locations=[str(pkg_dir)])
        if spec is None or spec.loader is None:  # pragma: no cover - broken checkout
            raise RuntimeError(f"cannot load the gx-image catalogue from {pkg_dir}")
        pkg = importlib.util.module_from_spec(spec)
        sys.modules[_PACKAGE] = pkg
        spec.loader.exec_module(pkg)
    return importlib.import_module(f"{_PACKAGE}.image_models")


class ImageCatalog:
    """Read-only view of the gx-image models for validation and the options API."""

    def __init__(self, repo_root: Path) -> None:
        self._mod = _module(str(repo_root))
        self._options: dict = self._mod.options()
        self.models: dict[str, dict] = {m["id"]: m for m in self._options["models"]}

    def options(self) -> dict:
        return self._options

    def default_for(self, kind: str) -> str:
        return self._options["default_generate"] if kind == "t2i" else self._options["default_edit"]

    def model(self, kind: str, value: object) -> dict:
        operation = {"t2i": "generate", "edit": "edit", "variation": "variation"}[kind]
        model_id = value if value not in (None, "") else self.default_for(kind)
        if not isinstance(model_id, str) or model_id not in self.models:
            raise CatalogError(f"image_model must be one of {', '.join(self.models)}")
        model = self.models[model_id]
        if operation not in model["operations"]:
            can = [m["label"] for m in self.models.values() if operation in m["operations"]]
            raise CatalogError(f"{model['label']} cannot {operation} images; choose {' or '.join(can)}")
        return model

    def model_for_workflow(self, workflow: str) -> str | None:
        return self._mod.model_for_workflow(workflow or "")

    @staticmethod
    def mode(model: dict, value: object) -> dict:
        modes = {m["id"]: m for m in model["edit_modes"]}
        mode_id = value if value not in (None, "") else model["defaults"].get("edit_mode")
        if mode_id not in modes:
            raise CatalogError(f"edit_mode must be one of {', '.join(modes)}")
        return modes[mode_id]

    def family(self, model_id: str | None) -> str:
        return self.models.get(model_id or "", {}).get("family", "")


# --------------------------------------------------------------- PNG masks
def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def png_mask_stats(data: bytes) -> tuple[int, int, float]:
    """(width, height, fraction of pixels whose first channel is >= 128).

    Accepts 8-bit, non-interlaced greyscale, grey+alpha, RGB and RGBA PNGs
    (what a browser canvas produces). Raises CatalogError otherwise."""
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise CatalogError("the mask must be a PNG image")
    pos, width, height, channels = 8, 0, 0, 0
    idat = bytearray()
    while pos + 8 <= len(data):
        length, tag = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        if len(body) != length:
            raise CatalogError("the mask PNG is truncated")
        if tag == b"IHDR":
            width, height, depth, ctype, _comp, _filt, interlace = struct.unpack(">IIBBBBB", body)
            channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(ctype, 0)
            if depth != 8 or not channels or interlace:
                raise CatalogError("the mask must be an 8-bit, non-interlaced PNG")
            if not (16 <= width <= MASK_MAX_SIDE and 16 <= height <= MASK_MAX_SIDE):
                raise CatalogError(f"the mask must be 16 to {MASK_MAX_SIDE} pixels per side")
        elif tag == b"IDAT":
            idat += body
        elif tag == b"IEND":
            break
        pos += 12 + length
    if not width:
        raise CatalogError("the mask PNG has no header")
    stride = width * channels
    expected = (stride + 1) * height
    try:
        # bounded: never inflate more than one byte past the declared size
        inflater = zlib.decompressobj()
        raw = inflater.decompress(bytes(idat), expected + 1)
    except zlib.error:
        raise CatalogError("the mask PNG data is corrupt") from None
    if len(raw) != expected or inflater.unconsumed_tail:
        raise CatalogError("the mask PNG data has the wrong size")
    prev = bytearray(stride)
    on = 0
    for y in range(height):
        start = y * (stride + 1)
        ftype = raw[start]
        line = bytearray(raw[start + 1:start + 1 + stride])
        if ftype == 1:
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif ftype == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ftype == 3:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                upleft = prev[i - channels] if i >= channels else 0
                line[i] = (line[i] + _paeth(left, prev[i], upleft)) & 0xFF
        elif ftype != 0:
            raise CatalogError("the mask PNG uses an unknown filter")
        on += sum(1 for v in line[::channels] if v >= 128)
        prev = line
    return width, height, on / float(width * height)


def parse_mask(body: dict, source: dict | None = None) -> tuple[bytes, dict] | None:
    """Validate an optional edit mask from a Create request.

    Returns (png bytes, metadata for the job/Library) or None."""
    raw = body.get("mask")
    if raw in (None, ""):
        if body.get("mask_rects"):
            raise CatalogError("mask_rects describe a mask; send the mask image as well")
        return None
    if not isinstance(raw, str) or len(raw) > MASK_MAX_B64:
        raise CatalogError("the mask must be a PNG data URL of at most 48 KB")
    match = _DATA_URL.match(raw.strip())
    if not match:
        raise CatalogError("the mask must be a data:image/png;base64 URL")
    try:
        data = base64.b64decode(match.group("data"), validate=False)
    except (binascii.Error, ValueError):
        raise CatalogError("the mask is not valid base64") from None
    width, height, coverage = png_mask_stats(data)
    if coverage < MASK_MIN_COVERAGE:
        raise CatalogError("the mask is empty: paint or draw the area to change")
    if source and source.get("width") and source.get("height"):
        ratio = source["width"] / source["height"]
        if abs(width / height - ratio) > 0.02 * ratio:
            raise CatalogError(f"the mask is {width}x{height} but the source is "
                               f"{source['width']}x{source['height']}; redraw it on this source")
    origin = body.get("mask_source") or "painted"
    if origin not in ("painted", "rectangles", "painted+rectangles"):
        raise CatalogError("mask_source must be painted, rectangles or painted+rectangles")
    rects: list[dict[str, float]] = []
    raw_rects = body.get("mask_rects") or []
    if not isinstance(raw_rects, list) or len(raw_rects) > MAX_RECTS:
        raise CatalogError(f"mask_rects must be a list of at most {MAX_RECTS} rectangles")
    for item in raw_rects:
        if not isinstance(item, dict):
            raise CatalogError("each mask rectangle needs x, y, w and h")
        rect: dict[str, float] = {}
        for key in ("x", "y", "w", "h"):
            value: Any = item.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.0 <= float(value) <= 1.0:
                raise CatalogError("mask rectangle values are fractions between 0 and 1")
            rect[key] = round(float(value), 4)
        if rect["w"] <= 0 or rect["h"] <= 0 or rect["x"] + rect["w"] > 1.0001 or rect["y"] + rect["h"] > 1.0001:
            raise CatalogError("a mask rectangle lies outside the image")
        rects.append(rect)
    meta = {"source": origin, "width": width, "height": height, "coverage": round(coverage, 4),
            "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "rects": rects}
    return data, meta


# ------------------------------------------------------- perceptual hashing
#: A difference hash over a 9x8 luminance grid: 64 bits, one per horizontal
#: neighbour comparison. Two pictures of the same scene differ in a handful of
#: bits; an edit that really changed something differs in many. It is a
#: similarity signal for the history, never a gate on a generation.
DHASH_W, DHASH_H = 9, 8
#: Hamming distance at or below which two images are called near-duplicates.
NEAR_DUPLICATE_BITS = 6


def _png_grey_grid(data: bytes, cols: int, rows: int) -> list[list[float]] | None:
    """Average luminance of a cols x rows grid of an 8-bit PNG, or None."""
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    pos, width, height, channels = 8, 0, 0, 0
    idat = bytearray()
    while pos + 8 <= len(data):
        length, tag = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        if len(body) != length:
            return None
        if tag == b"IHDR":
            width, height, depth, ctype, _comp, _filt, interlace = struct.unpack(">IIBBBBB", body)
            channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(ctype, 0)
            if depth != 8 or not channels or interlace or not (cols <= width and rows <= height):
                return None
        elif tag == b"IDAT":
            idat += body
        elif tag == b"IEND":
            break
        pos += 12 + length
    if not width:
        return None
    stride = width * channels
    expected = (stride + 1) * height
    try:
        inflater = zlib.decompressobj()
        raw = inflater.decompress(bytes(idat), expected + 1)
    except zlib.error:
        return None
    if len(raw) != expected or inflater.unconsumed_tail:
        return None
    #: which output column every source column belongs to, computed once
    col_of = [min(cols - 1, x * cols // width) for x in range(width)]
    sums = [[0.0] * cols for _ in range(rows)]
    counts = [[0] * cols for _ in range(rows)]
    prev = bytearray(stride)
    colour = channels >= 3
    for y in range(height):
        start = y * (stride + 1)
        ftype = raw[start]
        line = bytearray(raw[start + 1:start + 1 + stride])
        if ftype == 1:
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif ftype == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ftype == 3:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                upleft = prev[i - channels] if i >= channels else 0
                line[i] = (line[i] + _paeth(left, prev[i], upleft)) & 0xFF
        elif ftype != 0:
            return None
        row_index = min(rows - 1, y * rows // height)
        srow, crow = sums[row_index], counts[row_index]
        if colour:
            for x in range(width):
                base = x * channels
                c = col_of[x]
                srow[c] += 0.299 * line[base] + 0.587 * line[base + 1] + 0.114 * line[base + 2]
                crow[c] += 1
        else:
            for x in range(width):
                c = col_of[x]
                srow[c] += line[x * channels]
                crow[c] += 1
        prev = line
    out = []
    for r in range(rows):
        if not all(counts[r]):
            return None
        out.append([sums[r][c] / counts[r][c] for c in range(cols)])
    return out


def _grid_via_pillow(path: Path, cols: int, rows: int) -> list[list[float]] | None:
    """The same grid through Pillow when it is installed (much faster)."""
    try:
        from PIL import Image  # system Pillow; optional, as in media_library
    except ImportError:
        return None
    try:
        with Image.open(path) as im:
            im.load()
            small = im.convert("L").resize((cols, rows), Image.BILINEAR)
    except (OSError, ValueError):
        return None
    px = list(small.getdata())
    return [[float(px[r * cols + c]) for c in range(cols)] for r in range(rows)]


def dhash(path: Path) -> str | None:
    """A 16-character hex difference hash of an image file, or None."""
    grid = _grid_via_pillow(path, DHASH_W, DHASH_H)
    if grid is None:
        try:
            data = path.read_bytes()
        except OSError:
            return None
        grid = _png_grey_grid(data, DHASH_W, DHASH_H)
    if grid is None:
        return None
    bits = 0
    for row in grid:
        for c in range(DHASH_W - 1):
            bits = (bits << 1) | int(row[c] < row[c + 1])
    return f"{bits:016x}"


def hamming(a: str | None, b: str | None) -> int | None:
    """Bit distance between two dhash strings (0-64), or None."""
    if not a or not b:
        return None
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except ValueError:
        return None


# ------------------------------------------------ history and provenance
class ImageHistory:
    """Durable history, per-model provenance and edit lineage for gx-image.

    One row per image job (``img_generations``), one per produced asset
    (``img_outputs``), one per checkpoint actually used (``img_checkpoints``);
    see ``migrations/070_images.sql``. It observes MediaJobs, so nothing in the
    job path has to know about it, and an exception here never fails a job
    (MediaJobs isolates observers).
    """

    KINDS = ("t2i", "edit", "variation")

    def __init__(self, library: Any, media: Any, catalog: ImageCatalog | None = None,
                 *, compare: bool = True) -> None:
        self.library = library
        self.media = media
        self.catalog = catalog
        #: compute the dhash/lineage distance of edits (needs to read the files)
        self.compare = compare
        media.observers.append(self.observe)

    # ---------------------------------------------------------- database
    def _db(self) -> Any:
        return self.library.connect()

    # ---------------------------------------------------------- observer
    def observe(self, job: Any, event: str) -> None:
        if job.kind not in self.KINDS:
            return
        now = time.time()
        # MediaJobs queues a job before it delivers "submitted", so a job that
        # fails immediately can report its end first. The insert is therefore
        # idempotent and runs for every event; a late "submitted" is a no-op.
        self._insert(job, now)
        if event == "submitted":
            return
        sets: dict[str, Any] = {"status": job.phase, "updated_at": now, "router_job": job.router_job,
                                "started_at": job.started}
        if event in ("ready", "failed", "cancelled"):
            sets.update(self._final(job, event, now))
        with self._db() as con:
            con.execute(f"UPDATE img_generations SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?",  # noqa: S608
                        (*sets.values(), job.id))

    def _insert(self, job: Any, now: float) -> None:
        p = job.params
        model = (self.catalog.models.get(p.get("image_model") or "") if self.catalog else None) or {}
        mask = p.get("mask") if isinstance(p.get("mask"), dict) else {}
        width, _, height = str(p.get("size") or "x").partition("x")
        row = {
            "id": job.id, "created_at": job.created, "updated_at": now, "user": job.user,
            "status": job.phase, "kind": job.kind,
            "image_model": p.get("image_model"), "image_model_label": model.get("label"),
            "image_model_family": model.get("family") or job.family or None,
            "edit_mode": p.get("edit_mode"), "edit_quality": p.get("edit_quality"),
            "strength_applied": p.get("strength"),
            "masked": 1 if mask else 0, "mask_coverage": mask.get("coverage"),
            "mask_sha256": mask.get("sha256"), "mask_source": mask.get("source"),
            "prompt": p.get("prompt") or "", "negative_prompt": p.get("negative_prompt"),
            "adapter_strength": p.get("adapter_strength"),
            "uncensored": None if p.get("uncensored") is None else int(bool(p["uncensored"])),
            "quality_tags": None if p.get("quality_tags") is None else int(bool(p["quality_tags"])),
            "quality": p.get("quality"),
            "width": int(width) if width.isdigit() else None,
            "height": int(height) if height.isdigit() else None,
            "seed": p.get("seed"), "steps": p.get("steps"), "guidance": p.get("guidance"),
            "batch": int(p.get("n") or 1), "title": p.get("title"),
            "source_asset_id": p.get("source_id"),
            "request": json.dumps({k: v for k, v in p.items() if k != "mask"}, sort_keys=True, default=str),
            "flow_id": p.get("flow_id"), "flow_run_id": p.get("flow_run_id"),
            "flow_node_id": p.get("flow_node_id"),
        }
        cols = ", ".join(row)
        with self._db() as con:
            con.execute(f"INSERT OR IGNORE INTO img_generations ({cols}) "  # noqa: S608
                        f"VALUES ({', '.join('?' * len(row))})", tuple(row.values()))

    def _final(self, job: Any, event: str, now: float) -> dict:
        out: dict[str, Any] = {"finished_at": job.ended or now, "duration_seconds": job.elapsed_generation}
        if event != "ready":
            out["error_code"] = job.error_code or ("cancelled" if event == "cancelled" else None)
            out["error_message"] = job.error_hint or job.error
            out["error_detail"] = (job.error or job.detail or "")[:4000]
            return out
        first = True
        for index, asset_id in enumerate(job.assets):
            try:
                asset = self.library.get(asset_id)
            except Exception as exc:  # noqa: BLE001 - LibraryError and friends
                log.warning("image history: asset %s for job %s: %s", asset_id, job.id, exc)
                continue
            settings = asset.get("settings") or {}
            gx = settings.get("router") or {}
            if first:
                first = False
                edit = gx.get("edit") or {}
                out.update(workflow=asset.get("workflow") or gx.get("workflow"),
                           model_repository=asset.get("model_repo"),
                           model_revision=asset.get("model_revision"),
                           denoise=edit.get("denoise"),
                           strength_applied=gx.get("strength", job.params.get("strength")),
                           prompt_sent=gx.get("prompt_sent"), prompt_suffix=gx.get("prompt_suffix"),
                           adapter_strength=gx.get("adapter_strength", job.params.get("adapter_strength")),
                           router=json.dumps(gx, sort_keys=True, default=str))
                self._checkpoint(asset.get("workflow") or gx.get("workflow"),
                                 settings.get("image_model") or job.params.get("image_model"),
                                 asset.get("model_repo"), asset.get("model_revision"), now)
            self._output(job, asset, index, settings, now)
        return out

    def _checkpoint(self, workflow: str | None, model_id: str | None,
                    repository: str | None, revision: str | None, now: float) -> None:
        if not workflow or not model_id:
            return
        with self._db() as con:
            con.execute(
                "INSERT INTO img_checkpoints (image_model, workflow, repository, revision, first_seen, "
                "last_seen, generations) VALUES (?, ?, ?, ?, ?, ?, 1) "
                "ON CONFLICT(image_model, workflow, repository, revision) DO UPDATE SET "
                "last_seen=excluded.last_seen, generations=generations+1",
                (model_id, workflow, repository or "", revision or "", now, now))

    def _output(self, job: Any, asset: dict, index: int, settings: dict, now: float) -> None:
        source_id = job.params.get("source_id") if job.kind in ("edit", "variation") else None
        row = {
            "asset_id": asset["id"], "generation_id": job.id, "idx": index, "created_at": now,
            "operation": asset.get("operation") or job.kind,
            "image_model": settings.get("image_model") or job.params.get("image_model"),
            "workflow": asset.get("workflow"),
            "width": asset.get("width"), "height": asset.get("height"),
            "bytes": asset.get("file_size"), "sha256": asset.get("sha256"),
            "source_asset_id": source_id,
        }
        row.update(self._similarity(asset, source_id))
        cols = ", ".join(row)
        with self._db() as con:
            con.execute(f"INSERT OR REPLACE INTO img_outputs ({cols}) "  # noqa: S608
                        f"VALUES ({', '.join('?' * len(row))})", tuple(row.values()))

    def _similarity(self, asset: dict, source_id: str | None) -> dict:
        """How far the result moved from its source, as a dhash bit distance."""
        empty: dict[str, Any] = {"dhash": None, "source_dhash": None, "similarity_method": None,
                                 "similarity_distance": None, "similarity_score": None,
                                 "near_duplicate": None}
        if not self.compare:
            return empty
        try:
            out_hash = dhash(self.library.file_path(asset))
        except Exception as exc:  # noqa: BLE001 - never break a finished job over a metric
            log.info("image history: no hash for %s: %s", asset["id"], exc)
            return empty
        empty["dhash"] = out_hash
        if not source_id or out_hash is None:
            return empty
        try:
            src_hash = dhash(self.library.file_path(self.library.get(source_id)))
        except Exception as exc:  # noqa: BLE001
            log.info("image history: no source hash for %s: %s", source_id, exc)
            return empty
        distance = hamming(out_hash, src_hash)
        empty["source_dhash"] = src_hash
        if distance is None:
            return empty
        empty.update(similarity_method="dhash64", similarity_distance=distance,
                     similarity_score=round(1.0 - distance / 64.0, 4),
                     near_duplicate=int(distance <= NEAR_DUPLICATE_BITS))
        return empty

    # ------------------------------------------------------------- reads
    @staticmethod
    def _public(row: Any, outputs: list[dict] | None = None) -> dict:
        d = dict(row)
        for key in ("request", "router"):
            try:
                d[key] = json.loads(d.get(key) or "{}")
            except (TypeError, ValueError):
                d[key] = {}
        d["masked"] = bool(d.get("masked"))
        for key in ("uncensored", "quality_tags"):
            d[key] = None if d.get(key) is None else bool(d[key])
        if outputs is not None:
            d["outputs"] = [{**o, "near_duplicate": None if o.get("near_duplicate") is None
                             else bool(o["near_duplicate"])} for o in outputs]
        return d

    def generations(self, *, limit: int = 50, offset: int = 0, kind: str | None = None,
                    image_model: str | None = None, source_id: str | None = None) -> dict:
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        where, args = [], []
        for column, value in (("kind", kind), ("image_model", image_model), ("source_asset_id", source_id)):
            if value:
                where.append(f"{column}=?")
                args.append(value)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        with self._db() as con:
            total = con.execute(f"SELECT COUNT(*) FROM img_generations{clause}", tuple(args)).fetchone()[0]  # noqa: S608
            rows = con.execute(f"SELECT * FROM img_generations{clause} ORDER BY created_at DESC "  # noqa: S608
                               "LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()
            ids = [r["id"] for r in rows]
            outputs: dict[str, list[dict]] = {i: [] for i in ids}
            if ids:
                marks = ", ".join("?" * len(ids))
                for o in con.execute(f"SELECT * FROM img_outputs WHERE generation_id IN ({marks}) "  # noqa: S608
                                     "ORDER BY generation_id, idx", tuple(ids)).fetchall():
                    outputs[o["generation_id"]].append(dict(o))
        return {"object": "list", "total": total, "limit": limit, "offset": offset,
                "data": [self._public(r, outputs[r["id"]]) for r in rows]}

    def generation(self, generation_id: str) -> dict:
        with self._db() as con:
            row = con.execute("SELECT * FROM img_generations WHERE id=?", (generation_id,)).fetchone()
            if row is None:
                raise CatalogError("no such image generation")
            outputs = [dict(o) for o in con.execute(
                "SELECT * FROM img_outputs WHERE generation_id=? ORDER BY idx", (generation_id,)).fetchall()]
        return self._public(row, outputs)

    def lineage(self, asset_id: str) -> list[dict]:
        """The recorded edit chain that leads to `asset_id`, oldest first."""
        chain: list[dict] = []
        seen: set[str] = set()
        current: str | None = asset_id
        with self._db() as con:
            while current and current not in seen and len(chain) < 32:
                seen.add(current)
                row = con.execute("SELECT * FROM img_outputs WHERE asset_id=?", (current,)).fetchone()
                if row is None:
                    break
                chain.append(dict(row))
                current = row["source_asset_id"]
        return list(reversed(chain))

    def model_usage(self) -> dict:
        """What the cluster actually ran, per model: checkpoints and counts."""
        with self._db() as con:
            checkpoints = [dict(r) for r in con.execute(
                "SELECT * FROM img_checkpoints ORDER BY image_model, workflow").fetchall()]
            counts = {r["image_model"]: dict(r) for r in con.execute(
                "SELECT image_model, COUNT(*) AS generations, SUM(status='ready') AS ready, "
                "MAX(created_at) AS last_used FROM img_generations WHERE image_model IS NOT NULL "
                "GROUP BY image_model").fetchall()}
        return {"object": "list", "checkpoints": checkpoints, "models": list(counts.values())}
