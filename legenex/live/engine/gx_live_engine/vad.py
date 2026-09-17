"""Streaming voice activity detection (Silero VAD, ONNX on CPU).

Feeds 16 kHz mono float32 audio in any chunk size, evaluates 512-sample
windows (32 ms) and turns probabilities into speech start / stop events with
hysteresis. Keeps a short pre-roll so the first syllable is not lost.

The probability function is injectable so the state machine is tested
without the model.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from collections.abc import Callable

import numpy as np

WINDOW = 512
RATE = 16000
WINDOW_MS = WINDOW * 1000 // RATE  # 32


@dataclass
class VadEvent:
    kind: str                     # "start" | "stop"
    at_sample: int                # absolute sample index in the stream
    audio: np.ndarray | None = None   # the utterance (on "stop")


class StreamingVad:
    def __init__(self, prob: Callable[[np.ndarray], float], *, threshold: float = 0.5,
                 silence_ms: int = 700, min_speech_ms: int = 250, pre_roll_ms: int = 300,
                 max_utterance_s: float = 30.0, reset: Callable[[], None] | None = None) -> None:
        self.prob = prob
        self._reset_model = reset
        self.threshold = threshold
        self.release = max(0.15, threshold - 0.15)
        self.silence_windows = max(1, silence_ms // WINDOW_MS)
        self.min_speech_windows = max(1, min_speech_ms // WINDOW_MS)
        self.pre_roll = deque(maxlen=max(1, pre_roll_ms // WINDOW_MS))
        self.max_samples = int(max_utterance_s * RATE)
        self.stricter_while_playing = 0.15
        self.reset()

    def reset(self) -> None:
        self._pending = np.zeros(0, dtype=np.float32)
        self.samples = 0
        self.in_speech = False
        self.confirmed = False
        self._speech_windows = 0
        self._silent_windows = 0
        self._utt: list[np.ndarray] = []
        self._utt_len = 0
        self._start_sample = 0
        self.pre_roll.clear()
        if self._reset_model is not None:
            self._reset_model()

    def feed(self, audio: np.ndarray, *, playing: bool = False) -> list[VadEvent]:
        """Feed audio; returns events. ``playing`` makes onset stricter (echo guard)."""
        events: list[VadEvent] = []
        buf = np.concatenate([self._pending, audio.astype(np.float32, copy=False)])
        n = len(buf) // WINDOW
        onset = min(0.95, self.threshold + (self.stricter_while_playing if playing else 0.0))
        for i in range(n):
            win = buf[i * WINDOW:(i + 1) * WINDOW]
            p = float(self.prob(win))
            if not self.in_speech:
                self.pre_roll.append(win)
                if p >= onset:
                    self.in_speech = True
                    self.confirmed = False
                    self._speech_windows = 1
                    self._silent_windows = 0
                    self._utt = list(self.pre_roll)
                    self._utt_len = sum(len(w) for w in self._utt)
                    self._start_sample = self.samples + (i + 1) * WINDOW - self._utt_len
                    self.pre_roll.clear()
                continue
            self._utt.append(win)
            self._utt_len += WINDOW
            if p >= self.release:
                self._speech_windows += 1
                self._silent_windows = 0
            else:
                self._silent_windows += 1
            at = self.samples + (i + 1) * WINDOW
            if not self.confirmed and self._speech_windows >= self.min_speech_windows:
                self.confirmed = True
                events.append(VadEvent("start", at))
            if not self.confirmed and self._silent_windows >= self.silence_windows:
                # a blip: too short to be speech
                self.in_speech = False
                self._utt, self._utt_len = [], 0
                continue
            if self.confirmed and (self._silent_windows >= self.silence_windows or self._utt_len >= self.max_samples):
                events.append(VadEvent("stop", at, np.concatenate(self._utt)))
                self.in_speech = False
                self.confirmed = False
                self._utt, self._utt_len = [], 0
        self.samples += n * WINDOW
        self._pending = buf[n * WINDOW:]
        return events

    def commit(self) -> VadEvent | None:
        """Force the end of the current utterance (push-to-talk)."""
        if not self.in_speech or not self._utt:
            return None
        audio = np.concatenate(self._utt + ([self._pending] if len(self._pending) else []))
        ev = VadEvent("stop", self.samples, audio)
        self.in_speech = False
        self.confirmed = False
        self._utt, self._utt_len = [], 0
        return ev


def silero_prob() -> tuple[Callable[[np.ndarray], float], Callable[[], None]]:
    import torch
    from silero_vad import load_silero_vad

    model = load_silero_vad(onnx=True)

    def prob(win: np.ndarray) -> float:
        return float(model(torch.from_numpy(win), RATE))

    return prob, model.reset_states
