"""Audio inspection and assembly with the standard library only.

* ``sniff`` decides a container type from magic bytes (uploads are never
  trusted by extension or Content-Type).
* ``analyze_wav`` parses 16-bit PCM or 32-bit float WAV and returns the
  duration, level and a compact waveform envelope. It is what proves a take
  is real audio and not silence.
* ``read_pcm16`` / ``write_pcm16`` / ``concat`` assemble takes from utterances
  deterministically: same inputs, same bytes.
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
    """Return 'wav' | 'flac' | 'mp3' | 'ogg' | 'm4a' | 'webm' | None."""
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
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "webm"
    return None


@dataclasses.dataclass
class WavInfo:
    sample_rate: int
    channels: int
    bits: int
    frames: int
    duration_s: float
    peak: float
    rms_dbfs: float
    silent: bool
    waveform: list[list[float]]

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


def _chunks(data: bytes):
    pos = 12
    while pos + 8 <= len(data):
        cid, size = data[pos:pos + 4], struct.unpack_from("<I", data, pos + 4)[0]
        start = pos + 8
        yield cid, start, min(size, len(data) - start)
        pos = start + size + (size & 1)


def _parse(data: bytes) -> tuple[int, int, int, int, bytes]:
    if sniff(data[:12]) != "wav":
        raise ValidationError("not a RIFF/WAVE file")
    fmt = None
    pcm = None
    for cid, start, size in _chunks(data):
        if cid == b"fmt ":
            tag, channels, rate, _, _, bits = struct.unpack_from("<HHIIHH", data, start)
            if tag == WAVE_FORMAT_EXTENSIBLE and size >= 40:
                tag = struct.unpack_from("<H", data, start + 24)[0]
            fmt = (tag, channels, rate, bits)
        elif cid == b"data":
            pcm = data[start:start + size]
    if fmt is None or pcm is None:
        raise ValidationError("WAVE file has no fmt or data chunk")
    tag, channels, rate, bits = fmt
    if channels < 1 or rate < 8000:
        raise ValidationError("WAVE header is invalid")
    return tag, channels, rate, bits, pcm


def _samples(tag: int, bits: int, raw: bytes) -> tuple[array.array, float]:
    if tag == WAVE_FORMAT_PCM and bits == 16:
        s = array.array("h")
        s.frombytes(raw[: len(raw) - len(raw) % 2])
        scale = 32768.0
    elif tag == WAVE_FORMAT_IEEE_FLOAT and bits == 32:
        s = array.array("f")
        s.frombytes(raw[: len(raw) - len(raw) % 4])
        scale = 1.0
    else:
        raise ValidationError(f"unsupported WAVE encoding (format {tag}, {bits} bit)")
    if sys.byteorder != "little":  # pragma: no cover - aarch64/x86 are little-endian
        s.byteswap()
    return s, scale


def analyze_wav(path: Path, *, buckets: int = 400, silence_dbfs: float = -60.0) -> WavInfo:
    tag, channels, rate, bits, raw = _parse(path.read_bytes())
    samples, scale = _samples(tag, bits, raw)
    frames = len(samples) // channels
    if frames == 0:
        raise ValidationError("WAVE file contains no audio")
    total = frames * channels
    step = max(1, frames // buckets) * channels
    peak = 0.0
    sq = 0.0
    wave: list[list[float]] = []
    for off in range(0, total, step):
        seg = samples[off:min(off + step, total)]
        lo, hi = min(seg) / scale, max(seg) / scale
        peak = max(peak, abs(lo), abs(hi))
        sq += math.sumprod(seg, seg)
        wave.append([round(lo, 4), round(hi, 4)])
    rms = math.sqrt(sq / total) / scale
    rms_db = 20 * math.log10(rms) if rms > 0 else -999.0
    return WavInfo(sample_rate=rate, channels=channels, bits=bits, frames=frames,
                   duration_s=round(frames / rate, 3), peak=round(peak, 5), rms_dbfs=round(rms_db, 2),
                   silent=rms <= 0 or rms_db < silence_dbfs, waveform=wave[:buckets])


def read_pcm16(path: Path) -> tuple[int, bytes]:
    """(sample_rate, raw little-endian mono PCM16) of a mono PCM16 WAV."""
    tag, channels, rate, bits, raw = _parse(path.read_bytes())
    if tag != WAVE_FORMAT_PCM or bits != 16 or channels != 1:
        raise ValidationError("expected a mono 16-bit PCM WAV")
    return rate, raw[: len(raw) - len(raw) % 2]


def wav_bytes(rate: int, pcm: bytes) -> bytes:
    fmt = struct.pack("<HHIIHH", WAVE_FORMAT_PCM, 1, rate, rate * 2, 2, 16)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(pcm)) + pcm
    if len(pcm) % 2:
        body += b"\x00"
    return b"RIFF" + struct.pack("<I", len(body)) + body


def write_pcm16(path: Path, rate: int, pcm: bytes) -> None:
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(wav_bytes(rate, pcm))
    tmp.replace(path)


def silence(rate: int, ms: int) -> bytes:
    return b"\x00\x00" * int(rate * max(0, ms) / 1000)


def fade(pcm: bytes, rate: int, ms: int = 8) -> bytes:
    """Short linear fade-in/out so joins never click."""
    s = array.array("h")
    s.frombytes(pcm)
    n = min(len(s) // 2, int(rate * ms / 1000))
    for i in range(n):
        g = i / n
        s[i] = int(s[i] * g)
        s[-1 - i] = int(s[-1 - i] * g)
    return s.tobytes()


def concat(parts: list[tuple[bytes, int]], rate: int) -> bytes:
    """Join utterances: [(pcm, silence_ms_before), ...] -> pcm. Deterministic."""
    out = bytearray()
    for i, (pcm, gap) in enumerate(parts):
        if i:
            out += silence(rate, gap)
        out += fade(pcm, rate)
    return bytes(out)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()
