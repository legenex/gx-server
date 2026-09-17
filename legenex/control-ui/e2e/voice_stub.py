"""A hermetic gx-voice for gx10-01 tests and the offline browser suite.

This is the REAL node-2 supervisor (legenex/voice/gx_voice: validation,
router, queue, store and HTTP API) wired to the stub engine and fake Docker
from legenex/voice/tests/voice_fakes.py. Takes are short, real, playable
24 kHz sine-wave WAVs; no GPU, Docker daemon or model is involved.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

VOICE_DIR = Path(__file__).resolve().parents[2] / "voice"
for p in (VOICE_DIR, VOICE_DIR / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from voice_fakes import Rig, serve, stop, write_wav  # noqa: E402


class VoiceStub:
    def __init__(self, key: str, *, seconds_per_char: float = 0.02) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.rig = Rig(Path(self._tmp.name), key=key)
        self.rig.stub.seconds_per_char = seconds_per_char
        self.servers, self.url = serve(self.rig)

    @property
    def engine_calls(self) -> list[dict]:
        return self.rig.stub.calls

    def close(self) -> None:
        stop(self.rig, self.servers)
        self._tmp.cleanup()


def reference_wav(path: Path, seconds: float = 3.0) -> bytes:
    """A valid 24 kHz mono reference clip (2-60 s) for clone tests."""
    write_wav(path, seconds, amp=0.25)
    return path.read_bytes()
