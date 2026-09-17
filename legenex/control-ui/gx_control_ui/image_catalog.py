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
import re
import struct
import sys
import zlib
from pathlib import Path
from types import ModuleType
from typing import Any

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
