"""Acoustic reference analysis (measured, not guessed).

Runs INSIDE a throw-away helper container from the gx-music engine image
(``--network none``, no GPU, capped memory/CPU): that image already ships
numpy, scipy and ffmpeg; the supervisor itself stays stdlib-only and never
imports this module.

    python analysis_dsp.py /work/tmp/uploads/upl-....wav  ->  one JSON object on stdout

What is measured (classic MIR signal processing, no ML model):

* tempo: pulse-train cross-correlation on a log-band spectral-flux onset
  envelope with a log-normal tempo prior (centre 120 BPM), per-window
  stability and alternative candidates;
* beats: dynamic-programming beat tracker on the same envelope;
* time signature: periodicity of beat accents (2/3/4), low confidence by nature;
* key: Aarden-Essen profile correlation on the log-compressed harmonic chroma
  (median-filter harmonic/percussive separation first, so drums do not vote);
* loudness, dynamics, energy curve and its trend;
* spectrum: centroid, roll-off, flatness, bass and air ratios;
* texture: percussive vs harmonic energy;
* stereo width (mid/side);
* structure: Foote novelty on a chroma + timbre self-similarity matrix,
  segments labelled by similarity (A, B, ...) with their relative energy.

Genre, instrumentation, vocals and mood are NOT claimed here: they come from
ACE-Step's own audio understanding (model inference) and are labelled as such
by the caller.
"""

from __future__ import annotations

import json
import subprocess
import sys

import numpy as np
from scipy import ndimage, signal

METHOD = "gx-music DSP v1: numpy/scipy, STFT 2048/512 at 22.05 kHz, no ML model"
SR = 22050
N_FFT = 2048
HOP = 512
FPS = SR / HOP
MAX_SECONDS = 600
NOTES = ("C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B")
# Aarden-Essen key profiles (tonic first). On 31 ACE-Step renders with known
# planner keys they matched 19 keys versus 15 for Krumhansl-Kessler and 11 for
# Temperley (coordination/build-v3/mus.md).
KEY_MAJOR = np.array([17.77, 0.15, 14.93, 0.16, 19.8, 11.36, 0.29, 22.06, 0.15, 8.15, 0.23, 4.95])
KEY_MINOR = np.array([18.26, 0.74, 14.05, 16.86, 0.7, 14.44, 0.7, 18.62, 4.57, 1.93, 7.38, 1.76])


# ------------------------------------------------------------------ decode --
def decode(path: str, max_seconds: int = MAX_SECONDS) -> np.ndarray:
    """ffmpeg -> float32 stereo at 22.05 kHz (shape: frames x 2)."""
    proc = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-t", str(max_seconds), "-i", path,
         "-ac", "2", "-ar", str(SR), "-f", "f32le", "-"],
        capture_output=True, check=False, timeout=300)
    if proc.returncode != 0:
        raise ValueError("the audio could not be decoded")
    data = np.frombuffer(proc.stdout, dtype="<f4")
    if data.size < SR * 2:
        raise ValueError("the audio is too short to analyse (under one second)")
    return data[: data.size // 2 * 2].reshape(-1, 2).astype(np.float32)


# --------------------------------------------------------------- features --
def stft_mag(x: np.ndarray) -> np.ndarray:
    _, _, z = signal.stft(x, fs=SR, window="hann", nperseg=N_FFT, noverlap=N_FFT - HOP,
                          boundary=None, padded=False)
    return np.abs(z).astype(np.float32)  # bins x frames


def band_matrix(n_bands: int = 72, fmin: float = 30.0, fmax: float = 10000.0) -> np.ndarray:
    """Triangular log-spaced filterbank (bands x bins)."""
    freqs = np.linspace(0, SR / 2, N_FFT // 2 + 1)
    edges = np.geomspace(fmin, fmax, n_bands + 2)
    fb = np.zeros((n_bands, freqs.size), dtype=np.float32)
    for i in range(n_bands):
        lo, mid, hi = edges[i], edges[i + 1], edges[i + 2]
        up = (freqs - lo) / max(mid - lo, 1e-9)
        down = (hi - freqs) / max(hi - mid, 1e-9)
        fb[i] = np.clip(np.minimum(up, down), 0, None)
    fb /= np.maximum(fb.sum(axis=1, keepdims=True), 1e-9)
    return fb


def onset_envelope(bands: np.ndarray) -> np.ndarray:
    logb = np.log1p(100.0 * bands)
    flux = np.maximum(0.0, np.diff(logb, axis=1)).mean(axis=0)
    flux = np.concatenate([[0.0], flux])
    # remove slow trend, keep the pulse
    trend = ndimage.uniform_filter1d(flux, size=int(FPS * 1.5))
    env = np.maximum(0.0, flux - trend)
    return env / (env.std() + 1e-9)


#: weights of beat, half-beat and quarter-beat taps in the pulse train.
#: Chosen on 31 ACE-Step renders with known planner tempos (27/31 within 4 %)
#: and synthetic grooves; heavier half-beat weights pull strong four-on-the-floor
#: kicks down to half tempo (coordination/build-v3/mus.md).
PULSE_WEIGHTS = (1.0, 0.25, 0.0)


def _pulse_scores(env: np.ndarray, lo_bpm: float = 50.0, hi_bpm: float = 220.0,
                  step: float = 0.5) -> tuple[np.ndarray, np.ndarray]:
    """Pulse-train cross-correlation (Percival & Tzanetakis 2014 style).

    For each candidate tempo a weighted pulse train (PULSE_WEIGHTS) is slid
    over every phase; the best phase (plus the spread over phases) scores the
    candidate, then a log-normal prior around 120 BPM breaks octave ties.
    Unlike plain autocorrelation this separates a tempo from its 3:2 relative,
    because only the true grid lines up with the beats.
    """
    bpms = np.arange(lo_bpm, hi_bpm + step / 2, step)
    n = env.size
    scores = np.zeros(bpms.size)
    for i, bpm in enumerate(bpms):
        period = 60.0 * FPS / bpm
        beats = np.arange(0.0, n - period, period)
        if beats.size < 4:
            continue
        phases = np.arange(0.0, period, 1.0)[:, None]
        def taps(offset: float) -> np.ndarray:
            idx = np.clip(np.rint(phases + beats[None, :] + offset).astype(int), 0, n - 1)
            return env[idx].mean(axis=1)
        on, half = taps(0.0), taps(period / 2)
        w_on, w_half, w_q = PULSE_WEIGHTS
        s = w_on * on + w_half * half
        if w_q:
            s = s + w_q * (taps(period / 4) + taps(3 * period / 4))
        scores[i] = s.max() + s.std()
    prior = np.exp(-0.5 * (np.log2(bpms / 120.0) / 1.0) ** 2)
    return bpms, scores * prior


def estimate_tempo(env: np.ndarray) -> dict:
    head = env[: int(FPS * 180)]  # three minutes are plenty for the global tempo
    bpms, score = _pulse_scores(head)
    order = np.argsort(score)[::-1]
    best = int(order[0])
    # refine around the winner in 0.1 BPM steps
    fine_b, fine_s = _pulse_scores(head, max(50.0, bpms[best] - 0.6), min(220.0, bpms[best] + 0.6), 0.1)
    bpm = float(fine_b[int(np.argmax(fine_s))])
    candidates = []
    for idx in order:
        cand = float(bpms[idx])
        if all(abs(cand - c["bpm"]) / c["bpm"] > 0.04 for c in candidates):
            candidates.append({"bpm": round(cand, 1), "score": round(float(score[idx]), 3)})
        if len(candidates) == 4:
            break
    top = float(score[order[0]])
    second = float(candidates[1]["score"]) if len(candidates) > 1 else 0.0
    confidence = float(np.clip((top - second) / (abs(top) + 1e-9) * 3.0, 0.0, 1.0))
    # stability: the global winner's family must also win on 16 s windows
    win, stride = int(16 * FPS), int(8 * FPS)
    local = []
    for start in range(0, max(1, env.size - win), stride):
        seg = env[start:start + win]
        if seg.size < win // 2 or seg.std() < 1e-6:
            continue
        b2, s2 = _pulse_scores(seg, max(50.0, bpm / 2 - 2), min(220.0, bpm * 2 + 2), 1.0)
        local.append(float(b2[int(np.argmax(s2))]))
        if len(local) >= 12:
            break
    agree = [t for t in local if min(abs(t - bpm), abs(t - 2 * bpm), abs(2 * t - bpm)) / bpm < 0.04]
    stability = len(agree) / len(local) if local else 0.0
    return {"bpm": round(bpm, 1), "confidence": round(confidence, 2), "stability": round(stability, 2),
            "candidates": candidates, "windows": len(local),
            "method": "pulse-train cross-correlation on a log-band spectral-flux onset envelope"}


def track_beats(env: np.ndarray, bpm: float, tightness: float = 100.0) -> np.ndarray:
    """Ellis (2007) dynamic-programming beat tracker; returns frame indices."""
    period = 60.0 * FPS / bpm
    n = env.size
    score = env.astype(np.float64).copy()
    backlink = np.full(n, -1)
    lo, hi = int(round(period / 2)), int(round(period * 2))
    offsets = np.arange(-hi, -lo + 1)
    penalty = -tightness * np.log(-offsets / period) ** 2
    for t in range(hi, n):
        prev = score[t + offsets] + penalty
        k = int(np.argmax(prev))
        score[t] = env[t] + prev[k]
        backlink[t] = t + offsets[k]
    # start from the best score in the last period
    tail = score[max(0, n - int(period)):]
    t = int(np.argmax(tail)) + max(0, n - int(period))
    beats = []
    while t >= 0:
        beats.append(t)
        t = int(backlink[t])
    return np.array(beats[::-1], dtype=int)


def estimate_meter(env: np.ndarray, beats: np.ndarray) -> dict:
    if beats.size < 16:
        return {"value": None, "confidence": 0.0, "method": "beat accent periodicity", "note": "too few beats"}
    acc = np.array([env[max(0, b - 2):b + 3].max() for b in beats])
    acc = acc - acc.mean()
    ac = np.correlate(acc, acc, mode="full")[acc.size - 1:]
    ac /= ac[0] + 1e-9
    scores = {m: float(ac[m]) if m < ac.size else -1.0 for m in (2, 3, 4)}
    # 4/4 also shows a 2-beat period; judge 3 against the better of 2 and 4
    duple = max(scores[2], scores[4])
    if scores[3] > duple + 0.05:
        value, conf = "3/4", scores[3] - duple
    else:
        value, conf = "4/4", duple - scores[3]
    return {"value": value, "confidence": round(float(np.clip(conf * 2, 0, 1)), 2),
            "periodicity": {str(k): round(v, 3) for k, v in scores.items()},
            "method": "beat accent periodicity (2/3/4 beats)"}


def hpss(mag: np.ndarray, kernel: int = 17) -> tuple[np.ndarray, np.ndarray]:
    harm = ndimage.median_filter(mag, size=(1, kernel), mode="nearest")
    perc = ndimage.median_filter(mag, size=(kernel, 1), mode="nearest")
    mask_h = harm ** 2 / (harm ** 2 + perc ** 2 + 1e-12)
    return mag * mask_h, mag * (1.0 - mask_h)


def chroma_matrix() -> np.ndarray:
    freqs = np.linspace(0, SR / 2, N_FFT // 2 + 1)
    cm = np.zeros((12, freqs.size), dtype=np.float32)
    valid = (freqs >= 55.0) & (freqs <= 5000.0)
    midi = 69 + 12 * np.log2(np.maximum(freqs, 1e-6) / 440.0)
    pc = np.mod(np.round(midi), 12).astype(int)
    dist = np.abs(midi - np.round(midi))
    weight = np.clip(1.0 - 2.0 * dist, 0, 1)  # bins between semitones count less
    for i in np.nonzero(valid)[0]:
        cm[pc[i], i] = weight[i]
    return cm


def estimate_key(chroma: np.ndarray, energy: np.ndarray) -> dict:
    prof = (np.log1p(10.0 * chroma) * energy[None, :]).sum(axis=1)
    if prof.sum() <= 0:
        return {"value": None, "confidence": 0.0}
    prof = prof / prof.sum()
    results = []
    for tonic in range(12):
        for mode, template in (("major", KEY_MAJOR), ("minor", KEY_MINOR)):
            r = float(np.corrcoef(prof, np.roll(template, tonic))[0, 1])
            results.append((r, f"{NOTES[tonic]} {mode}"))
    results.sort(reverse=True)
    best, second = results[0], results[1]
    return {"value": best[1], "correlation": round(best[0], 3),
            "confidence": round(float(np.clip((best[0] - second[0]) * 5, 0, 1)), 2),
            "alternatives": [{"key": k, "correlation": round(r, 3)} for r, k in results[1:4]],
            "pitch_class_profile": {NOTES[i]: round(float(prof[i]), 3) for i in range(12)},
            "method": "Aarden-Essen profile correlation on log harmonic chroma"}


def novelty_segments(feat: np.ndarray, fps: float, kernel_s: float = 12.0, min_len_s: float = 8.0) -> list[int]:
    """Foote checkerboard novelty on a cosine self-similarity matrix."""
    x = feat / (np.linalg.norm(feat, axis=0, keepdims=True) + 1e-9)
    ssm = x.T @ x
    k = max(4, int(kernel_s * fps / 2))
    g = signal.windows.gaussian(2 * k, std=k / 2)
    kern = np.outer(g, g)
    kern[:k, k:] *= -1
    kern[k:, :k] *= -1
    pad = np.pad(ssm, k, mode="edge")
    n = ssm.shape[0]
    nov = np.array([float((pad[i:i + 2 * k, i:i + 2 * k] * kern).sum()) for i in range(n)])
    nov = np.maximum(nov, 0)
    if nov.max() <= 0:
        return []
    nov /= nov.max()
    peaks, _ = signal.find_peaks(nov, height=0.25, distance=max(1, int(min_len_s * fps)))
    return [int(p) for p in peaks]


def label_segments(feat: np.ndarray, bounds: list[int]) -> list[str]:
    means = [feat[:, a:b].mean(axis=1) for a, b in zip(bounds[:-1], bounds[1:])]
    labels: list[str] = []
    protos: list[np.ndarray] = []
    for m in means:
        m = m / (np.linalg.norm(m) + 1e-9)
        best, best_sim = -1, 0.0
        for j, p in enumerate(protos):
            sim = float(m @ p)
            if sim > best_sim:
                best, best_sim = j, sim
        if best >= 0 and best_sim >= 0.985:
            labels.append(chr(ord("A") + best))
        else:
            protos.append(m)
            labels.append(chr(ord("A") + min(len(protos) - 1, 25)))
    return labels


def word(value: float, cuts: tuple[float, float], names: tuple[str, str, str]) -> str:
    return names[0] if value < cuts[0] else names[1] if value < cuts[1] else names[2]


# ------------------------------------------------------------------ main --
def analyse(stereo: np.ndarray) -> dict:
    mono = stereo.mean(axis=1)
    duration = mono.size / SR
    peak = float(np.abs(stereo).max())
    rms = float(np.sqrt(np.mean(mono.astype(np.float64) ** 2)))
    mid = (stereo[:, 0] + stereo[:, 1]) / 2
    side = (stereo[:, 0] - stereo[:, 1]) / 2
    width = float(np.sqrt(np.mean(side ** 2)) / (np.sqrt(np.mean(mid ** 2)) + 1e-9))

    mag = stft_mag(mono)
    frames = mag.shape[1]
    power = mag ** 2
    freqs = np.linspace(0, SR / 2, mag.shape[0])
    frame_energy = power.sum(axis=0)
    total = frame_energy.sum() + 1e-12
    centroid = float((freqs[:, None] * power).sum() / total)
    cumulative = np.cumsum(power.sum(axis=1))
    rolloff = float(freqs[np.searchsorted(cumulative, 0.85 * cumulative[-1])])
    low_ratio = float(power[freqs < 150].sum() / total)
    high_ratio = float(power[freqs > 5000].sum() / total)
    flatness = float(np.exp(np.mean(np.log(mag[:, frame_energy > 1e-8] + 1e-9)))
                     / (np.mean(mag[:, frame_energy > 1e-8]) + 1e-9)) if (frame_energy > 1e-8).any() else 0.0

    bands = band_matrix() @ mag
    env = onset_envelope(bands)
    tempo = estimate_tempo(env)
    beats = track_beats(env, tempo["bpm"]) if tempo["bpm"] else np.array([], dtype=int)
    meter = estimate_meter(env, beats)

    # harmonic/percussive split on a 2x time-decimated grid (same result, half the work)
    harm, perc = hpss(mag[:, ::2])
    h_e, p_e = float((harm ** 2).sum()), float((perc ** 2).sum())
    percussive_ratio = p_e / (h_e + p_e + 1e-12)
    chroma = chroma_matrix() @ harm
    key = estimate_key(chroma, np.sqrt(frame_energy[::2][: chroma.shape[1]]))

    # loudness / energy curve on 0.5 s blocks
    block = int(SR * 0.5)
    nblk = max(1, mono.size // block)
    blk_rms = np.sqrt(np.mean(mono[: nblk * block].reshape(nblk, block).astype(np.float64) ** 2, axis=1))
    blk_db = 20 * np.log10(blk_rms + 1e-9)
    loud = blk_db[blk_db > -70] if (blk_db > -70).any() else blk_db
    dyn_range = float(np.percentile(loud, 95) - np.percentile(loud, 10))
    curve_idx = np.linspace(0, nblk, 65).astype(int)
    curve = np.array([blk_rms[a:max(a + 1, b)].mean() for a, b in zip(curve_idx[:-1], curve_idx[1:])])
    curve = curve / (curve.max() + 1e-12)
    thirds = [float(np.mean(c)) for c in np.array_split(curve, 3)]
    if thirds[2] > thirds[0] * 1.35:
        trend = "builds"
    elif thirds[0] > thirds[2] * 1.35:
        trend = "fades"
    elif thirds[1] > max(thirds[0], thirds[2]) * 1.25:
        trend = "peaks in the middle"
    else:
        trend = "steady"
    rms_db = 20 * np.log10(rms + 1e-12)

    # structure on 1 s beat-agnostic frames: chroma + log band energy
    per = int(round(FPS))
    nseg = frames // per
    if nseg >= 20:
        coarse_bands = np.log1p(100 * bands[:, : nseg * per].reshape(bands.shape[0], nseg, per).mean(axis=2))
        ch = chroma[:, : (nseg * per) // 2]
        cper = max(1, per // 2)
        cn = ch.shape[1] // cper
        coarse_chroma = ch[:, : cn * cper].reshape(12, cn, cper).mean(axis=2)
        n = min(nseg, cn)
        feat = np.vstack([coarse_bands[::6, :n] / (coarse_bands[::6, :n].max() + 1e-9),
                          coarse_chroma[:, :n] / (coarse_chroma[:, :n].max(axis=0, keepdims=True) + 1e-9)])
        cuts = novelty_segments(feat, 1.0)
        bounds = [0, *[c for c in cuts if 4 <= c <= n - 4], n]
        labels = label_segments(feat, bounds)
        seg_energy = []
        for a, b in zip(bounds[:-1], bounds[1:]):
            lo, hi = int(a / 0.5), int(b / 0.5)
            seg_energy.append(float(20 * np.log10(blk_rms[lo:max(lo + 1, hi)].mean() + 1e-9)))
        top = max(seg_energy) if seg_energy else 0.0
        segments = [{"start": float(a), "end": float(min(b, duration)), "label": lab,
                     "energy_db": round(e - top, 1),
                     "energy": word(e - top, (-9.0, -3.5), ("low", "medium", "high"))}
                    for a, b, lab, e in zip(bounds[:-1], bounds[1:], labels, seg_energy)]
    else:
        segments = [{"start": 0.0, "end": round(duration, 2), "label": "A", "energy_db": 0.0, "energy": "high"}]

    energy_level = word(rms_db, (-22.0, -14.0), ("low", "medium", "high"))
    brightness = word(centroid, (1400.0, 2600.0), ("dark", "balanced", "bright"))
    bass = word(low_ratio, (0.12, 0.3), ("light", "moderate", "heavy"))
    texture = word(percussive_ratio, (0.3, 0.5), ("sustained / harmonic", "balanced", "percussive"))
    stereo_label = "mono" if width < 0.02 else word(width, (0.2, 0.45), ("narrow", "moderate", "wide"))
    descriptors = []
    bpm = tempo["bpm"]
    descriptors.append("slow tempo" if bpm < 85 else "mid tempo" if bpm < 115 else "fast tempo")
    if percussive_ratio >= 0.45 and energy_level == "high":
        descriptors.append("driving beat")
    if bass == "heavy":
        descriptors.append("heavy bass")
    if brightness != "balanced":
        descriptors.append(f"{brightness} tone")
    if dyn_range < 6:
        descriptors.append("compressed, dense mix")
    elif dyn_range > 14:
        descriptors.append("wide dynamics")
    if trend == "builds":
        descriptors.append("gradual build")
    if stereo_label == "wide":
        descriptors.append("wide stereo image")

    return {
        "method": METHOD,
        "duration_s": round(duration, 2),
        "tempo": tempo,
        "beats": {"count": int(beats.size),
                  "first_s": round(float(beats[0] / FPS), 2) if beats.size else None,
                  "median_interval_s": round(float(np.median(np.diff(beats)) / FPS), 3) if beats.size > 2 else None},
        "time_signature": meter,
        "key": key,
        "loudness": {"rms_dbfs": round(rms_db, 1), "peak_dbfs": round(20 * np.log10(peak + 1e-12), 1),
                     "crest_db": round(20 * np.log10((peak + 1e-12) / (rms + 1e-12)), 1),
                     "dynamic_range_db": round(dyn_range, 1)},
        "energy": {"level": energy_level, "trend": trend,
                   "curve": [round(float(v), 3) for v in curve]},
        "spectrum": {"centroid_hz": round(centroid), "rolloff_hz": round(rolloff),
                     "bass_ratio": round(low_ratio, 3), "air_ratio": round(high_ratio, 4),
                     "flatness": round(flatness, 3), "brightness": brightness, "bass_weight": bass},
        "texture": {"percussive_ratio": round(percussive_ratio, 3), "character": texture},
        "stereo": {"width": round(width, 3), "label": stereo_label},
        "structure": {"segments": segments, "count": len(segments),
                      "method": "Foote novelty on a chroma + timbre self-similarity matrix (1 s frames)"},
        "descriptors": descriptors,
    }


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(json.dumps({"error": "usage: analysis_dsp.py <audio file>"}))
        return 2
    try:
        result = analyse(decode(argv[1]))
    except (ValueError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"error": str(exc)}))
        return 1
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
