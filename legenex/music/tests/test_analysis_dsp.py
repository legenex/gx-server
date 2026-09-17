"""Tests for the measured reference analysis (gx_music/analysis_dsp.py).

The analysis runs inside the engine image (numpy + scipy). These tests use
synthetic signals with a known tempo, key and structure. They are skipped
where numpy/scipy are missing (gx10-01's system Python); qa.sh then runs them
inside the engine image via scripts/dsp-selftest.sh when that image exists.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

HAVE_NUMPY = importlib.util.find_spec("numpy") is not None and importlib.util.find_spec("scipy") is not None

if HAVE_NUMPY:
    import numpy as np

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gx_music"))
    import analysis_dsp as A  # noqa: E402

NOTE = {"A": 57, "C": 60, "D": 62, "E": 64, "F": 65, "G": 67}


def tone(midi: float, seconds: float, amp: float = 0.2) -> "np.ndarray":
    t = np.arange(int(seconds * A.SR)) / A.SR
    f = 440.0 * 2 ** ((midi - 69) / 12)
    wave = sum(amp / k * np.sin(2 * np.pi * f * k * t) for k in (1, 2, 3))
    env = np.minimum(1.0, t * 20) * np.exp(-t * 0.8)
    return (wave * env).astype(np.float32)


def kick(seconds: float = 0.15) -> "np.ndarray":
    t = np.arange(int(seconds * A.SR)) / A.SR
    return (0.9 * np.sin(2 * np.pi * (50 + 80 * np.exp(-t * 30)) * t) * np.exp(-t * 25)).astype(np.float32)


def hat(seconds: float = 0.05, seed: int = 0) -> "np.ndarray":
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * A.SR)) / A.SR
    return (0.25 * rng.standard_normal(t.size) * np.exp(-t * 90)).astype(np.float32)


def place(buf: "np.ndarray", clip: "np.ndarray", at: float) -> None:
    i = int(at * A.SR)
    j = min(buf.size, i + clip.size)
    buf[i:j] += clip[: j - i]


def groove(bpm: float, seconds: float, chords: list[list[int]], beats_per_bar: int = 4,
           accent: bool = False) -> "np.ndarray":
    buf = np.zeros(int(seconds * A.SR), dtype=np.float32)
    beat = 60.0 / bpm
    k, h = kick(), hat()
    n = int(seconds / beat)
    for b in range(n):
        at = b * beat
        place(buf, k * (1.0 if (not accent or b % beats_per_bar == 0) else 0.35), at)
        place(buf, h, at + beat / 2)
        if b % beats_per_bar == 0:
            chord = chords[(b // beats_per_bar) % len(chords)]
            for m in chord:
                place(buf, tone(m, beat * beats_per_bar, 0.12), at)
    return buf


@unittest.skipUnless(HAVE_NUMPY, "numpy/scipy not installed here; run scripts/dsp-selftest.sh")
class DSPTests(unittest.TestCase):
    AMINOR = [[57, 60, 64], [53, 57, 60], [48, 52, 55], [55, 59, 62]]  # Am F C G

    def analyse(self, mono: "np.ndarray") -> dict:
        return A.analyse(np.stack([mono, mono * 0.9], axis=1))

    def test_tempo_and_key_of_a_known_groove(self):
        for bpm in (84.0, 122.0, 140.0):
            with self.subTest(bpm=bpm):
                r = self.analyse(groove(bpm, 40.0, self.AMINOR))
                self.assertAlmostEqual(r["tempo"]["bpm"], bpm, delta=bpm * 0.02, msg=r["tempo"])
                self.assertIn(r["key"]["value"], ("A minor", "C major"), r["key"])
                self.assertGreater(r["beats"]["count"], 40 * bpm / 60 * 0.8)

    def test_triple_meter(self):
        r = self.analyse(groove(100.0, 45.0, self.AMINOR, beats_per_bar=3, accent=True))
        self.assertEqual(r["time_signature"]["value"], "3/4", r["time_signature"])

    def test_structure_energy_and_report_shape(self):
        quiet = groove(120.0, 30.0, self.AMINOR) * 0.08
        loud = groove(120.0, 30.0, [[62, 66, 69], [55, 59, 62]])  # D major section
        r = self.analyse(np.concatenate([quiet, loud]))
        seg = r["structure"]["segments"]
        self.assertGreaterEqual(len(seg), 2)
        self.assertTrue(any(abs(s["start"] - 30.0) <= 3.0 for s in seg[1:]), seg)
        self.assertEqual(seg[0]["energy"], "low")
        self.assertEqual(r["energy"]["trend"], "builds")
        self.assertEqual(len(r["energy"]["curve"]), 64)
        for key in ("tempo", "key", "time_signature", "loudness", "spectrum", "texture", "stereo", "descriptors"):
            self.assertIn(key, r)
        self.assertIn("no ML model", r["method"])
        self.assertLess(r["stereo"]["width"], 0.1)

    def test_silence_and_noise_do_not_crash(self):
        r = self.analyse(np.zeros(A.SR * 12, dtype=np.float32))
        self.assertIsNone(r["key"]["value"])
        rng = np.random.default_rng(1)
        r = self.analyse((0.1 * rng.standard_normal(A.SR * 12)).astype(np.float32))
        self.assertIn("bpm", r["tempo"])

    def test_cli_rejects_bad_input(self):
        self.assertEqual(A.main(["x"]), 2)
        self.assertEqual(A.main(["x", "/nonexistent/file.wav"]), 1)


if __name__ == "__main__":
    unittest.main()
