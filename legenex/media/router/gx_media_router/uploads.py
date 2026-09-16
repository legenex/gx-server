"""Source media for edits: parsing, validation and staging (D-031).

Callers upload an image (edit, image-to-video) or a video (video edit). The
bytes are untrusted, so before anything reaches ComfyUI:

* the container format is identified from magic bytes, never from a caller's
  filename or Content-Type;
* image dimensions are read from the header and bounded;
* sizes are bounded;
* the file is written under a server-generated name in ComfyUI's input
  directory (`gx-in/<uuid>.<ext>`), and removed after the job finishes.

A caller's filename never becomes a path. Standard library only.
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
import re
import struct
import threading
import time
import uuid
from dataclasses import dataclass
from email.parser import BytesParser
from email.policy import HTTP
from pathlib import Path

from .errors import ValidationError

log = logging.getLogger("gx-media.uploads")

_DATA_URL = re.compile(r"^data:(?P<mime>[\w.+-]+/[\w.+-]+)?(;[\w=.+-]+)*;base64,(?P<data>.*)$", re.S)


@dataclass(frozen=True)
class MediaInfo:
    kind: str          # "image" | "video"
    ext: str           # png | jpg | webp | mp4 | mov | webm
    media_type: str
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True)
class UploadedFile:
    field: str
    filename: str
    content_type: str
    data: bytes


# ----------------------------------------------------------------- sniffing
def _png_size(data: bytes) -> tuple[int, int] | None:
    if len(data) >= 24 and data[12:16] == b"IHDR":
        w, h = struct.unpack(">II", data[16:24])
        return w, h
    return None


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    i = 2
    n = len(data)
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if i + 4 > n:
            return None
        seg_len = struct.unpack(">H", data[i + 2:i + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            if i + 9 > n:
                return None
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return w, h
        if seg_len < 2:
            return None
        i += 2 + seg_len
    return None


def _webp_size(data: bytes) -> tuple[int, int] | None:
    chunk = data[12:16]
    if chunk == b"VP8 " and len(data) >= 30:
        w, h = struct.unpack("<HH", data[26:30])
        return w & 0x3FFF, h & 0x3FFF
    if chunk == b"VP8L" and len(data) >= 25:
        b = data[21:25]
        bits = int.from_bytes(b, "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if chunk == b"VP8X" and len(data) >= 30:
        w = int.from_bytes(data[24:27], "little") + 1
        h = int.from_bytes(data[27:30], "little") + 1
        return w, h
    return None


def sniff(data: bytes) -> MediaInfo | None:
    """Identify a supported container from its first bytes."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        size = _png_size(data)
        return MediaInfo("image", "png", "image/png", *(size or (None, None)))
    if data.startswith(b"\xff\xd8\xff"):
        size = _jpeg_size(data)
        return MediaInfo("image", "jpg", "image/jpeg", *(size or (None, None)))
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        size = _webp_size(data)
        return MediaInfo("image", "webp", "image/webp", *(size or (None, None)))
    if len(data) >= 12 and data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand == b"qt  ":
            return MediaInfo("video", "mov", "video/quicktime")
        if brand[:3] in (b"avi", b"hei", b"mif", b"msf"):
            return None  # AVIF/HEIF stills are not accepted
        return MediaInfo("video", "mp4", "video/mp4")
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        return MediaInfo("video", "webm", "video/webm")
    return None


def validate(data: bytes, *, expect: str, max_bytes: int, max_pixels: int, max_side: int) -> MediaInfo:
    if not data:
        raise ValidationError(f"the {expect} upload is empty", param=expect)
    if len(data) > max_bytes:
        raise ValidationError(f"the {expect} is {len(data)} bytes; the maximum is {max_bytes}", param=expect)
    info = sniff(data)
    if info is None or info.kind != expect:
        allowed = "PNG, JPEG or WebP" if expect == "image" else "MP4, MOV or WebM"
        raise ValidationError(f"unsupported {expect} format; send {allowed}", param=expect)
    if info.kind == "image":
        if not info.width or not info.height:
            raise ValidationError("could not read the image dimensions", param=expect)
        if info.width < 16 or info.height < 16:
            raise ValidationError("the image is smaller than 16x16", param=expect)
        if max(info.width, info.height) > max_side or info.width * info.height > max_pixels:
            raise ValidationError(
                f"the image is {info.width}x{info.height}; the limit is {max_side} px per side "
                f"and {max_pixels} pixels", param=expect)
    return info


# ----------------------------------------------------------------- decoding
def decode_data_url(value: object, field: str) -> bytes:
    """Accept `data:<mime>;base64,...` or bare base64."""
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{field} must be a base64 string or data URL", param=field)
    match = _DATA_URL.match(value.strip())
    payload = match.group("data") if match else value.strip()
    try:
        return base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        raise ValidationError(f"{field} is not valid base64", param=field) from None


def parse_multipart(content_type: str, body: bytes) -> tuple[dict[str, str], list[UploadedFile]]:
    """Parse multipart/form-data. Returns (text fields, files)."""
    if "boundary=" not in content_type:
        raise ValidationError("multipart request without a boundary")
    head = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("latin-1")
    message = BytesParser(policy=HTTP).parsebytes(head + body)
    if not message.is_multipart():
        raise ValidationError("malformed multipart body")
    fields: dict[str, str] = {}
    files: list[UploadedFile] = []
    for part in message.iter_parts():
        disposition = part.get("Content-Disposition", "")
        if "form-data" not in disposition:
            continue
        name = part.get_param("name", header="content-disposition")
        if not isinstance(name, str) or not name or len(name) > 64:
            continue
        payload = part.get_payload(decode=True) or b""
        filename = part.get_filename()
        if filename is not None:
            files.append(UploadedFile(name, str(filename)[:128], part.get_content_type(), payload))
        else:
            if len(payload) > 65536:
                raise ValidationError(f"form field {name!r} is too large", param=name)
            fields[name] = payload.decode("utf-8", "replace")
    return fields, files


def coerce_form(fields: dict[str, str]) -> dict[str, object]:
    """Turn form strings into the JSON types the validators expect."""
    out: dict[str, object] = {}
    for key, raw in fields.items():
        value: object = raw
        text = raw.strip()
        if key in {"prompt", "negative_prompt", "model", "size", "response_format", "quality",
                   "workflow", "input_fidelity", "background", "output_format", "user"}:
            out[key] = raw
            continue
        if text.lower() in {"true", "false"}:
            value = text.lower() == "true"
        elif re.fullmatch(r"-?\d{1,19}", text):
            value = int(text)
        elif re.fullmatch(r"-?\d+\.\d*|-?\d*\.\d+", text):
            value = float(text)
        elif text.startswith(("{", "[")):
            import json
            try:
                value = json.loads(text)
            except ValueError:
                value = raw
        out[key] = value
    return out


# ----------------------------------------------------------------- staging
class InputStore:
    """Writes validated sources into ComfyUI's input directory."""

    SUBDIR = "gx-in"

    def __init__(self, root: Path, ttl_seconds: int = 24 * 3600) -> None:
        self.root = Path(root)
        self.ttl = ttl_seconds
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return self.root.is_dir() and os.access(self.root, os.W_OK)

    def put(self, data: bytes, info: MediaInfo) -> str:
        """Store `data`; return the ComfyUI-relative name (`gx-in/<uuid>.<ext>`)."""
        if not self.available:
            raise ValidationError("source uploads are not enabled on this router (input directory unavailable)")
        directory = self.root / self.SUBDIR
        with self._lock:
            directory.mkdir(mode=0o770, exist_ok=True)
            name = f"{uuid.uuid4().hex}.{info.ext}"
            tmp = directory / f".{name}.part"
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, directory / name)
        return f"{self.SUBDIR}/{name}"

    def remove(self, relative: str) -> None:
        if not re.fullmatch(rf"{self.SUBDIR}/[0-9a-f]{{32}}\.(png|jpg|webp|mp4|mov|webm)", relative or ""):
            return
        try:
            (self.root / relative).unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("could not remove staged input %s: %s", relative, exc)

    def purge_stale(self) -> int:
        directory = self.root / self.SUBDIR
        removed = 0
        if not directory.is_dir():
            return 0
        cutoff = time.time() - self.ttl
        for path in directory.iterdir():
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
        return removed


def fit_to_pixels(width: int, height: int, target_pixels: int, multiple: int = 16,
                  max_side: int = 2048) -> tuple[int, int]:
    """Scale (w, h) to about `target_pixels`, keeping aspect, snapped to `multiple`."""
    scale = (target_pixels / float(width * height)) ** 0.5
    w = max(multiple, int(round(width * scale / multiple)) * multiple)
    h = max(multiple, int(round(height * scale / multiple)) * multiple)
    while max(w, h) > max_side:
        w = max(multiple, w - multiple) if w >= h else w
        h = max(multiple, h - multiple) if h > w else h
    return w, h
