#!/usr/bin/env python3
"""Measure what is actually in a generated track — run INSIDE the gx-music
engine image, CPU only, offline.

Nothing here infers: every number is computed from the PCM samples, and the ASR
transcript comes from a real Whisper decode of the file. It answers exactly four
questions, and says which evidence supports which:

  vocals?      Whisper large-v3-turbo transcribes the file (CPU, no network).
               `asr.words` > 0 with a low `asr.avg_no_speech` means a singing or
               speaking voice is present; a music-only track transcribes to
               nothing or to a handful of low-confidence tokens.
  pitch?       Median f0 of voiced frames, from a normalised autocorrelation
               (YIN-style, 65-1000 Hz) of the 150-1200 Hz band-passed mix. This
               is a MIX-level measurement: no source separation is performed, so
               it is the pitch of the strongest periodic component, not proof of
               a singer's register on its own.
  voice-like?  Energy ratio of the 300-3400 Hz speech band and the 3-8 Hz
               ("syllabic") modulation depth of that band's envelope. Singing
               raises both relative to a steady instrumental bed.
  different?   Log-mel spectral distance and cosine similarity between two
               files, for "same seed, different style tags" comparisons.

    audio-forensics.py describe A.wav [B.wav ...] [--asr] [--model DIR]
    audio-forensics.py compare A.wav B.wav [more.wav ...]

Output is one JSON document on stdout.
"""

from __future__ import annotations

import argparse
import json
import sys
import wave
from pathlib import Path

import numpy as np

SR_TARGET = 16000  # Whisper's rate; the DSP runs at the file's own rate


# --------------------------------------------------------------------- io
def read_wav(path: Path) -> tuple[np.ndarray, int]:
    """Mono float32 in [-1, 1] plus the sample rate. Handles PCM16/24/32 and float32."""
    with wave.open(str(path), "rb") as w:
        sr, ch, width, n = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()
        raw = w.readframes(n)
    if width == 2:
        x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 4:
        # ACE-Step writes 32-bit float WAV; fall back to int32 if it is not.
        f = np.frombuffer(raw, dtype="<f4")
        x = f.astype(np.float32) if np.isfinite(f).all() and np.abs(f).max() <= 4.0 \
            else np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    elif width == 3:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        v = (b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16))
        v[v >= 1 << 23] -= 1 << 24
        x = v.astype(np.float32) / 8388608.0
    else:
        raise ValueError(f"unsupported sample width {width}")
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return x.astype(np.float32), sr


def resample(x: np.ndarray, sr: int, target: int) -> np.ndarray:
    if sr == target:
        return x
    n = int(round(len(x) * target / sr))
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)


# ------------------------------------------------------------------- dsp
def band_energy_ratio(x: np.ndarray, sr: int, lo: float, hi: float) -> float:
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x)))) ** 2
    freq = np.fft.rfftfreq(len(x), 1 / sr)
    total = spec.sum()
    return float(spec[(freq >= lo) & (freq < hi)].sum() / total) if total > 0 else 0.0


def bandpass(x: np.ndarray, sr: int, lo: float, hi: float) -> np.ndarray:
    """Zero-phase brick-wall band-pass in the frequency domain (offline analysis)."""
    n = len(x)
    spec = np.fft.rfft(x)
    freq = np.fft.rfftfreq(n, 1 / sr)
    spec[(freq < lo) | (freq > hi)] = 0
    return np.fft.irfft(spec, n).astype(np.float32)


def f0_track(x: np.ndarray, sr: int, fmin: float = 65.0, fmax: float = 1000.0,
             frame_s: float = 0.046, hop_s: float = 0.023, thresh: float = 0.55) -> dict:
    """Median f0 over voiced frames (normalised autocorrelation, YIN-style).

    A frame counts as voiced when its best normalised autocorrelation peak is
    above `thresh` and the frame is above -45 dBFS.
    """
    band = bandpass(x, sr, 150.0, 1200.0)
    frame, hop = int(frame_s * sr), int(hop_s * sr)
    lo_lag, hi_lag = int(sr / fmax), int(sr / fmin)
    f0s: list[float] = []
    clarity: list[float] = []
    voiced = total = 0
    for i in range(0, max(0, len(band) - frame), hop):
        seg = band[i:i + frame]
        rms = float(np.sqrt(np.mean(seg ** 2)))
        total += 1
        if rms < 10 ** (-45 / 20):
            continue
        seg = seg - seg.mean()
        ac = np.correlate(seg, seg, mode="full")[frame - 1:]
        if ac[0] <= 0:
            continue
        ac = ac / ac[0]
        window = ac[lo_lag:hi_lag]
        if not len(window):
            continue
        k = int(np.argmax(window)) + lo_lag
        if ac[k] < thresh:
            continue
        # parabolic interpolation around the peak for sub-sample accuracy
        if 0 < k < len(ac) - 1:
            a, b, c = ac[k - 1], ac[k], ac[k + 1]
            denom = a - 2 * b + c
            k = k + (0.5 * (a - c) / denom if denom else 0.0)
        voiced += 1
        f0s.append(sr / k)
        clarity.append(float(ac[int(round(k))] if int(round(k)) < len(ac) else b))
    if not f0s:
        return {"voiced_frames": 0, "frames": total, "voiced_fraction": 0.0, "median_hz": None,
                "p25_hz": None, "p75_hz": None, "median_clarity": None}
    arr = np.array(f0s)
    return {"voiced_frames": voiced, "frames": total, "voiced_fraction": round(voiced / max(1, total), 4),
            "median_hz": round(float(np.median(arr)), 1), "p25_hz": round(float(np.percentile(arr, 25)), 1),
            "p75_hz": round(float(np.percentile(arr, 75)), 1),
            "median_clarity": round(float(np.median(clarity)), 4)}


def syllabic_modulation(x: np.ndarray, sr: int) -> dict:
    """Depth of the 3-8 Hz modulation of the 300-3400 Hz envelope.

    Sung or spoken words switch that band on and off a few times a second; a pad,
    a drone or a steady groove does not.
    """
    band = bandpass(x, sr, 300.0, 3400.0)
    env_sr = 100
    step = max(1, sr // env_sr)
    env = np.sqrt(np.convolve(band ** 2, np.ones(step) / step, mode="same")[::step])
    env = env - env.mean()
    if not len(env) or not np.any(env):
        return {"mod_3_8hz": 0.0, "mod_total": 0.0, "ratio": 0.0}
    spec = np.abs(np.fft.rfft(env * np.hanning(len(env)))) ** 2
    freq = np.fft.rfftfreq(len(env), 1 / env_sr)
    syl = float(spec[(freq >= 3) & (freq <= 8)].sum())
    tot = float(spec[(freq >= 0.5) & (freq <= 20)].sum())
    return {"mod_3_8hz": syl, "mod_total": tot, "ratio": round(syl / tot, 4) if tot else 0.0}


def logmel(x: np.ndarray, sr: int, n_mels: int = 48, n_fft: int = 2048, hop: int = 512) -> np.ndarray:
    win = np.hanning(n_fft)
    frames = [np.abs(np.fft.rfft(x[i:i + n_fft] * win)) for i in range(0, max(0, len(x) - n_fft), hop)]
    if not frames:
        return np.zeros((n_mels,))
    mag = np.stack(frames, axis=1)
    freq = np.fft.rfftfreq(n_fft, 1 / sr)
    def hz2mel(f): return 2595 * np.log10(1 + f / 700)
    def mel2hz(m): return 700 * (10 ** (m / 2595) - 1)
    edges = mel2hz(np.linspace(hz2mel(40), hz2mel(min(16000, sr / 2)), n_mels + 2))
    bank = np.zeros((n_mels, len(freq)), dtype=np.float32)
    for i in range(n_mels):
        lo, mid, hi = edges[i], edges[i + 1], edges[i + 2]
        up = (freq >= lo) & (freq <= mid)
        dn = (freq > mid) & (freq <= hi)
        bank[i, up] = (freq[up] - lo) / max(1e-9, mid - lo)
        bank[i, dn] = (hi - freq[dn]) / max(1e-9, hi - mid)
    return np.log10(bank @ mag + 1e-8).mean(axis=1)


def describe_file(path: Path) -> dict:
    x, sr = read_wav(path)
    peak = float(np.abs(x).max()) if len(x) else 0.0
    rms = float(np.sqrt(np.mean(x ** 2))) if len(x) else 0.0
    return {
        "file": path.name, "bytes": path.stat().st_size, "sample_rate": sr,
        "duration_s": round(len(x) / sr, 2) if sr else 0.0,
        "peak": round(peak, 5), "rms_dbfs": round(20 * np.log10(rms), 2) if rms > 0 else None,
        "silent": bool(peak < 1e-4),
        "band_ratio_300_3400hz": round(band_energy_ratio(x, sr, 300, 3400), 4),
        "band_ratio_below_300hz": round(band_energy_ratio(x, sr, 20, 300), 4),
        "f0": f0_track(x, sr),
        "syllabic": syllabic_modulation(x, sr),
        "logmel48": [round(v, 4) for v in logmel(x, sr).tolist()],
    }


def compare(a: dict, b: dict) -> dict:
    va, vb = np.array(a["logmel48"]), np.array(b["logmel48"])
    cos = float(va @ vb / (np.linalg.norm(va) * np.linalg.norm(vb) + 1e-12))
    return {"a": a["file"], "b": b["file"],
            "logmel_l2_distance": round(float(np.linalg.norm(va - vb)), 4),
            "logmel_cosine": round(cos, 6),
            "mean_abs_db_difference": round(float(np.abs(va - vb).mean() * 10), 3)}


# ------------------------------------------------------------------- asr
def transcribe(paths: list[Path], model_dir: str) -> dict:
    """Real Whisper decode on the CPU. No network: the weights are a local dir."""
    import torch
    from transformers import WhisperForConditionalGeneration, WhisperProcessor
    torch.set_num_threads(max(1, (torch.get_num_threads() or 4)))
    proc = WhisperProcessor.from_pretrained(model_dir, local_files_only=True)
    model = WhisperForConditionalGeneration.from_pretrained(
        model_dir, local_files_only=True, dtype=torch.float32).eval()
    out: dict[str, dict] = {}
    for p in paths:
        x, sr = read_wav(p)
        audio = resample(x, sr, SR_TARGET)
        texts, no_speech = [], []
        chunk = SR_TARGET * 30
        for i in range(0, len(audio), chunk):
            seg = audio[i:i + chunk]
            if len(seg) < SR_TARGET:  # < 1 s tail
                break
            feats = proc(seg, sampling_rate=SR_TARGET, return_tensors="pt").input_features
            with torch.no_grad():
                gen = model.generate(feats, language="en", task="transcribe", max_new_tokens=220,
                                     return_dict_in_generate=True, output_scores=False)
            txt = proc.batch_decode(gen.sequences, skip_special_tokens=True)[0].strip()
            texts.append(txt)
            with torch.no_grad():
                logits = model(feats, decoder_input_ids=torch.tensor(
                    [[model.generation_config.decoder_start_token_id]])).logits[0, 0]
            ns_id = proc.tokenizer.convert_tokens_to_ids("<|nospeech|>")
            probs = torch.softmax(logits, dim=-1)
            no_speech.append(float(probs[ns_id]) if ns_id is not None and ns_id >= 0 else float("nan"))
        text = " ".join(t for t in texts if t).strip()
        out[p.name] = {"text": text, "words": len(text.split()),
                       "avg_no_speech": round(float(np.nanmean(no_speech)), 4) if no_speech else None,
                       "segments": len(texts)}
        print(f"[asr] {p.name}: {out[p.name]['words']} words, no_speech={out[p.name]['avg_no_speech']}",
              file=sys.stderr, flush=True)
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["describe", "compare"])
    ap.add_argument("files", nargs="+")
    ap.add_argument("--asr", action="store_true", help="also run Whisper on every file")
    ap.add_argument("--model", default="/models/whisper-large-v3-turbo")
    args = ap.parse_args(argv)
    paths = [Path(f) for f in args.files]
    described = {p.name: describe_file(p) for p in paths}
    doc: dict = {"files": described}
    if args.mode == "compare":
        doc["comparisons"] = [compare(described[paths[i].name], described[paths[j].name])
                              for i in range(len(paths)) for j in range(i + 1, len(paths))]
    if args.asr:
        doc["asr"] = transcribe(paths, args.model)
    print(json.dumps(doc, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
