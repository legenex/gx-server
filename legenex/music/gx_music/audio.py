"""Audio file inspection with the standard library only.

* ``sniff`` decides a container type from magic bytes (uploads are never
  trusted by extension or Content-Type).
* ``analyze_wav`` parses a RIFF/WAVE file (PCM 16/24/32 or IEEE float 32)
  and returns duration, format, peak/RMS level and a compact waveform
  envelope for the UI. It is what proves an output is real audio and not
  silence.
"""

from __future__ import annotations

import array
import dataclasses
import hashlib
import math
import struct
import sys
from pathlib import Path

from .errors import ValidationError

WAVE_FORMAT_PCM = 1
WAVE_FORMAT_IEEE_FLOAT = 3
WAVE_FORMAT_EXTENSIBLE = 0xFFFE


def sniff(head: bytes) -> str | None:
    """Return 'wav' | 'flac' | 'mp3' | 'ogg' | 'm4a' | None."""
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if head[:4] == b"fLaC":
        return "flac"
    if head[:3] == b"ID3":
        return "mp3"
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0 and (head[1] & 0x06) != 0:
        return "mp3"
    if head[:4] == b"OggS":
        return "ogg"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "m4a"
    return None


@dataclasses.dataclass
class WavInfo:
    sample_rate: int
    channels: int
    bits: int
    encoding: str  # "float" | "pcm"
    frames: int
    duration_s: float
    peak: float
    rms: float
    rms_dbfs: float
    silent: bool
    waveform: list[list[float]]  # [[min, max], ...] per bucket, mono mix

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


def _chunks(data: bytes):
    pos = 12
    while pos + 8 <= len(data):
        cid, size = data[pos:pos + 4], struct.unpack_from("<I", data, pos + 4)[0]
        body_start = pos + 8
        yield cid, body_start, min(size, len(data) - body_start)
        pos = body_start + size + (size & 1)


def analyze_wav(path: Path, *, buckets: int = 600, silence_dbfs: float = -60.0) -> WavInfo:
    data = path.read_bytes()
    if sniff(data[:12]) != "wav":
        raise ValidationError("not a RIFF/WAVE file")
    fmt = None
    pcm: tuple[int, int] | None = None
    for cid, start, size in _chunks(data):
        if cid == b"fmt ":
            tag, channels, rate, _, _, bits = struct.unpack_from("<HHIIHH", data, start)
            if tag == WAVE_FORMAT_EXTENSIBLE and size >= 40:
                tag = struct.unpack_from("<H", data, start + 24)[0]
            fmt = (tag, channels, rate, bits)
        elif cid == b"data":
            pcm = (start, size)
    if fmt is None or pcm is None:
        raise ValidationError("WAVE file has no fmt or data chunk")
    tag, channels, rate, bits = fmt
    if channels < 1 or rate < 8000:
        raise ValidationError("WAVE header is invalid")
    start, size = pcm
    raw = data[start:start + size]
    if tag == WAVE_FORMAT_IEEE_FLOAT and bits == 32:
        samples = array.array("f")
        samples.frombytes(raw[: len(raw) - len(raw) % 4])
        scale, encoding = 1.0, "float"
    elif tag == WAVE_FORMAT_PCM and bits == 16:
        samples = array.array("h")
        samples.frombytes(raw[: len(raw) - len(raw) % 2])
        scale, encoding = 32768.0, "pcm"
    elif tag == WAVE_FORMAT_PCM and bits == 32:
        samples = array.array("i")
        samples.frombytes(raw[: len(raw) - len(raw) % 4])
        scale, encoding = 2147483648.0, "pcm"
    elif tag == WAVE_FORMAT_PCM and bits == 24:
        n = len(raw) // 3
        ints = [int.from_bytes(raw[i * 3:i * 3 + 3], "little", signed=True) for i in range(n)]
        samples = array.array("i", ints)
        scale, encoding = 8388608.0, "pcm"
    else:
        raise ValidationError(f"unsupported WAVE encoding (format {tag}, {bits} bit)")
    if sys.byteorder != "little":  # pragma: no cover - aarch64/x86 are little-endian
        samples.byteswap()
    frames = len(samples) // channels
    if frames == 0:
        raise ValidationError("WAVE file contains no audio")
    # Bucketed slices keep the heavy lifting in C (min/max/sumprod), so a
    # 4-minute 48 kHz stereo master is analysed in well under a second.
    peak = 0.0
    sq = 0.0
    total = frames * channels
    step = max(1, frames // buckets) * channels
    wave: list[list[float]] = []
    for off in range(0, total, step):
        seg = samples[off:min(off + step, total)]
        lo, hi = min(seg) / scale, max(seg) / scale
        peak = max(peak, abs(lo), abs(hi))
        sq += math.sumprod(seg, seg)
        wave.append([round(lo, 4), round(hi, 4)])
    rms = math.sqrt(sq / total) / scale
    rms_db = 20 * math.log10(rms) if rms > 0 else -math.inf
    return WavInfo(
        sample_rate=rate, channels=channels, bits=bits, encoding=encoding, frames=frames,
        duration_s=round(frames / rate, 3), peak=round(peak, 5), rms=round(rms, 6),
        rms_dbfs=round(rms_db, 2) if rms > 0 else -999.0,
        silent=(rms_db < silence_dbfs) if rms > 0 else True,
        waveform=wave[:buckets],
    )


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def pcm_fingerprint(path: Path) -> str:
    """Hash of the sample payload only (header-independent), for determinism tests."""
    data = path.read_bytes()
    for cid, start, size in _chunks(data):
        if cid == b"data":
            return hashlib.sha256(data[start:start + size]).hexdigest()
    raise ValidationError("WAVE file has no data chunk")
