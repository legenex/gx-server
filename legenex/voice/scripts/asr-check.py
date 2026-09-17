#!/usr/bin/env python3
"""Transcribe acceptance takes and compare them with the requested text.

Runs INSIDE the gx-voice engine image on the CPU (no GPU, no network):

    docker run --rm --network none --cpus 8 --memory 8g --user 1000:1000 \\
      -v /srv/models/voice/whisper-large-v3-turbo:/asr:ro -v <dir>:/data \\
      --entrypoint python gx-voice-engine:qwen3tts-022e286-t214 \\
      /data/asr-check.py /data/cases.json

cases.json: [{"file": "take.wav", "text": "expected words", "language": "english"}, ...]
Prints one JSON object: per case the transcript, the word error rate after
normalisation (lower case, no punctuation) and the audio duration.
Model: openai/whisper-large-v3-turbo @ 41f01f3f (MIT), used only for this check.
"""

from __future__ import annotations

import json
import re
import sys
import time
import unicodedata
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
from transformers import pipeline


def normalise(text: str) -> list[str]:
    text = unicodedata.normalize("NFKC", text).lower()
    text = re.sub(r"[^\w\s']", " ", text)
    return text.replace("'", "").split()


def wer(ref: list[str], hyp: list[str]) -> float:
    d = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        prev, d[0] = d[0], i
        for j, w in enumerate(hyp, 1):
            cur = min(d[j] + 1, d[j - 1] + 1, prev + (r != w))
            prev, d[j] = d[j], cur
    return round(d[len(hyp)] / max(1, len(ref)), 4)


def main() -> int:
    cases_file = Path(sys.argv[1])
    cases = json.loads(cases_file.read_text())
    torch.set_num_threads(8)
    t0 = time.time()
    asr = pipeline("automatic-speech-recognition", model="/asr", device="cpu", torch_dtype=torch.float32)
    load_s = round(time.time() - t0, 1)
    out = []
    for case in cases:
        audio, sr = sf.read(str(cases_file.parent / case["file"]), dtype="float32", always_2d=False)
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        if sr != 16000:
            import librosa
            audio = librosa.resample(audio, orig_sr=sr, target_sr=16000)
        t1 = time.time()
        res = asr({"raw": np.asarray(audio, dtype=np.float32), "sampling_rate": 16000},
                  generate_kwargs={"language": case.get("language", "english"), "task": "transcribe"},
                  return_timestamps=len(audio) > 30 * 16000)
        hyp = res["text"].strip()
        out.append({"file": case["file"], "expected": case["text"], "transcript": hyp,
                    "wer": wer(normalise(case["text"]), normalise(hyp)),
                    "duration_s": round(len(audio) / 16000, 2), "asr_s": round(time.time() - t1, 1)})
    print(json.dumps({"model": "openai/whisper-large-v3-turbo@41f01f3fe87f28c78e2fbf8b568835947dd65ed9",
                      "device": "cpu", "load_s": load_s, "cases": out}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
