"""Conversation timing and transcript segmentation for a gx-call stream.

Pure Python (stdlib only) so it runs in the engine container and in the
hermetic test suite. It watches, per inference step,

* the caller audio that went into the model (16 kHz PCM16),
* the agent audio that came out (22.05 kHz float or PCM16),
* the agent text and caller transcription deltas,

and turns them into the gx-call.v1 events: speech started/stopped for both
sides, turn latency, first-audio latency, interruptions (barge-in) with
their latency, and transcript deltas/finals.

Two clocks are reported for every latency:

* ``*_ms`` is STREAM time: positions in the audio timeline (80 ms frames).
  This is what the caller hears relative to what they said, if the engine
  keeps up with real time.
* ``*_wall_ms`` is WALL time: when the engine actually produced the output
  relative to when the input arrived. It includes processing delay, so it is
  the honest number when the engine runs slower than real time.
"""

from __future__ import annotations

import math
import struct
import time
from dataclasses import dataclass, field
from typing import Callable

INPUT_RATE = 16000
OUTPUT_RATE = 22050

#: dBFS thresholds. Caller audio: absolute floor plus an adaptive margin over
#: the tracked noise floor. Agent audio: the model's TTS emits true silence
#: (codec silence tokens) between turns, so a fixed threshold is reliable.
USER_ABS_DBFS = -45.0
USER_MARGIN_DB = 12.0
AGENT_DBFS = -50.0


def rms_dbfs_pcm16(data: bytes) -> float:
    n = len(data) // 2
    if n == 0:
        return -120.0
    samples = struct.unpack(f"<{n}h", data[: n * 2])
    energy = sum(s * s for s in samples) / n
    if energy <= 0:
        return -120.0
    return 20 * math.log10(math.sqrt(energy) / 32768.0)


def rms_dbfs_float(samples) -> float:  # noqa: ANN001 - list/array/np of floats
    n = len(samples)
    if n == 0:
        return -120.0
    energy = float(sum(float(s) * float(s) for s in samples)) / n
    if energy <= 0:
        return -120.0
    return 20 * math.log10(math.sqrt(energy))


@dataclass
class TrackerConfig:
    frame_ms: int = 80
    user_onset_frames: int = 2          # 160 ms of speech starts a caller utterance
    user_offset_frames: int = 6         # 480 ms of silence ends it
    agent_offset_frames: int = 4        # 320 ms of agent silence ends agent speech
    interruption_window_ms: int = 4000  # agent must yield within this after barge-in
    agent_final_quiet_ms: int = 800     # agent text finalised after this much quiet


@dataclass
class Tracker:
    emit: Callable[[dict], None]
    cfg: TrackerConfig = field(default_factory=TrackerConfig)
    clock: Callable[[], float] = time.time

    def __post_init__(self) -> None:
        self.started_wall = self.clock()
        self.stream_ms = 0
        # caller VAD
        self.noise_floor = -60.0
        self.user_speaking = False
        self.user_run = 0
        self.user_quiet = 0
        self.user_onset_ms: int | None = None
        self.user_last_speech_ms = 0
        self.user_last_speech_wall = 0.0
        self.pending_turn: dict | None = None  # caller finished; waiting for the agent
        # agent activity
        self.agent_speaking = False
        self.agent_quiet = 0
        self.agent_last_audible_ms = 0
        self.first_audio_sent = False
        self.pending_interrupt: dict | None = None
        # transcripts
        self.user_text = ""
        self.user_utterance = 0
        self.agent_text = ""
        self.agent_turn = 0
        self.agent_last_text_wall = 0.0
        self.agent_turn_interrupted = False
        # metrics
        self.turn_latencies: list[int] = []
        self.turn_latencies_wall: list[int] = []
        self.interrupt_latencies: list[int] = []
        self.interruptions = 0
        self.first_audio_wall_ms: int | None = None

    # ------------------------------------------------------------ helpers
    def _event(self, kind: str, **fields) -> None:
        self.emit({"type": kind, "stream_ms": self.stream_ms, "t": round(self.clock() * 1000), **fields})

    # --------------------------------------------------------------- step
    def step(self, *, in_pcm16: bytes, in_wall: float, out_level_dbfs: float, out_ms: int,
             text_delta: str = "", asr_delta: str = "", asr_reset: bool = False) -> None:
        """Advance by one inference step (``len(in_pcm16)`` of caller audio).

        ``in_wall`` is when the newest input sample of this step arrived;
        ``out_level_dbfs`` is the level of the agent audio produced by this step
        and ``out_ms`` its duration.
        """
        frame_bytes = INPUT_RATE * self.cfg.frame_ms // 1000 * 2
        frames = max(1, len(in_pcm16) // frame_bytes)
        for i in range(frames):
            chunk = in_pcm16[i * frame_bytes:(i + 1) * frame_bytes]
            self._user_frame(rms_dbfs_pcm16(chunk), in_wall)
            self.stream_ms += self.cfg.frame_ms
        self._agent_audio(out_level_dbfs, out_ms)
        self._text(text_delta, asr_delta, asr_reset)

    def _user_frame(self, level: float, in_wall: float) -> None:
        threshold = max(USER_ABS_DBFS, self.noise_floor + USER_MARGIN_DB)
        speech = level > threshold
        if not speech:
            # slow-rising, fast-falling noise floor
            self.noise_floor = level if level < self.noise_floor else self.noise_floor * 0.98 + level * 0.02
        if speech:
            self.user_run += 1
            self.user_quiet = 0
            self.user_last_speech_ms = self.stream_ms + self.cfg.frame_ms
            self.user_last_speech_wall = in_wall
            if not self.user_speaking and self.user_run >= self.cfg.user_onset_frames:
                self.user_speaking = True
                onset = self.stream_ms - (self.cfg.user_onset_frames - 1) * self.cfg.frame_ms
                self.user_onset_ms = onset
                self.pending_turn = None
                self._event("user.speech.started", at_ms=onset)
                if self.agent_speaking and self.pending_interrupt is None:
                    self.pending_interrupt = {"at_ms": onset, "wall": in_wall}
                    # the client drops queued agent audio now (barge-in)
                    self._event("interruption.started", at_ms=onset, flush=True)
        else:
            self.user_run = 0
            if self.user_speaking:
                self.user_quiet += 1
                if self.user_quiet >= self.cfg.user_offset_frames:
                    self.user_speaking = False
                    self._event("user.speech.stopped", at_ms=self.user_last_speech_ms,
                                duration_ms=self.user_last_speech_ms - (self.user_onset_ms or 0))
                    self.pending_turn = {"end_ms": self.user_last_speech_ms,
                                         "end_wall": self.user_last_speech_wall}

    def _agent_audio(self, level: float, out_ms: int) -> None:
        audible = out_ms > 0 and level > AGENT_DBFS
        now = self.clock()
        # this step's agent audio covers [stream_ms - out_ms, stream_ms] of the timeline
        start_ms = self.stream_ms - out_ms
        if audible:
            self.agent_quiet = 0
            self.agent_last_audible_ms = self.stream_ms
            if not self.agent_speaking:
                self.agent_speaking = True
                self.agent_turn += 1 if self.agent_text == "" else 0
                fields: dict = {"turn": self.agent_turn}
                if not self.first_audio_sent:
                    self.first_audio_sent = True
                    self.first_audio_wall_ms = round((now - self.started_wall) * 1000)
                    fields["first_audio_wall_ms"] = self.first_audio_wall_ms
                if self.pending_turn is not None:
                    lat = max(0, start_ms - self.pending_turn["end_ms"])
                    lat_wall = round((now - self.pending_turn["end_wall"]) * 1000)
                    self.turn_latencies.append(lat)
                    self.turn_latencies_wall.append(lat_wall)
                    fields.update(turn_latency_ms=lat, turn_latency_wall_ms=lat_wall)
                    self.pending_turn = None
                self._event("agent.speech.started", at_ms=start_ms, **fields)
        elif out_ms > 0 and self.agent_speaking:
            self.agent_quiet += 1
            if self.agent_quiet >= self.cfg.agent_offset_frames:
                self.agent_speaking = False
                reason = "completed"
                extra: dict = {}
                if self.pending_interrupt is not None:
                    reason = "interrupted"
                    stop_ms = self.agent_last_audible_ms
                    lat = max(0, stop_ms - self.pending_interrupt["at_ms"])
                    lat_wall = round((now - self.pending_interrupt["wall"]) * 1000)
                    self.interruptions += 1
                    self.interrupt_latencies.append(lat)
                    self.agent_turn_interrupted = True
                    extra = {"interruption_latency_ms": lat, "interruption_latency_wall_ms": lat_wall}
                    self._event("interruption", at_ms=self.pending_interrupt["at_ms"], agent_stopped_ms=stop_ms,
                                latency_ms=lat, latency_wall_ms=lat_wall, yielded=True)
                    self.pending_interrupt = None
                self._event("agent.speech.stopped", turn=self.agent_turn, reason=reason, **extra)
        if self.pending_interrupt is not None and \
                self.stream_ms - self.pending_interrupt["at_ms"] > self.cfg.interruption_window_ms:
            self._event("interruption", at_ms=self.pending_interrupt["at_ms"], yielded=False)
            self.pending_interrupt = None

    def _text(self, text_delta: str, asr_delta: str, asr_reset: bool) -> None:
        now = self.clock()
        if asr_reset and self.user_text.strip():
            self._event("transcript.user.final", utterance=self.user_utterance, text=self.user_text.strip())
            self.user_text = ""
            self.user_utterance += 1
        if asr_delta:
            self.user_text += asr_delta
            self._event("transcript.user.delta", utterance=self.user_utterance, delta=asr_delta)
        if text_delta:
            if self.agent_text == "" and not self.agent_speaking:
                self.agent_turn += 1
            self.agent_text += text_delta
            self.agent_last_text_wall = now
            self._event("transcript.agent.delta", turn=self.agent_turn, delta=text_delta)
        if (self.agent_text and not self.agent_speaking
                and (now - self.agent_last_text_wall) * 1000 >= self.cfg.agent_final_quiet_ms):
            self._finalize_agent()

    def _finalize_agent(self) -> None:
        self._event("transcript.agent.final", turn=self.agent_turn, text=self.agent_text.strip(),
                    interrupted=self.agent_turn_interrupted)
        self.agent_text = ""
        self.agent_turn_interrupted = False

    def finish(self) -> dict:
        """Flush open transcripts and return the session's timing summary."""
        if self.user_text.strip():
            self._event("transcript.user.final", utterance=self.user_utterance, text=self.user_text.strip())
            self.user_text = ""
        if self.agent_text.strip():
            self._finalize_agent()
        return self.summary()

    def summary(self) -> dict:
        def stats(values: list[int]) -> dict | None:
            if not values:
                return None
            ordered = sorted(values)
            return {"count": len(values), "mean": round(sum(values) / len(values)),
                    "p50": ordered[len(ordered) // 2], "max": ordered[-1]}

        return {
            "stream_ms": self.stream_ms,
            "first_audio_wall_ms": self.first_audio_wall_ms,
            "turn_latency_ms": stats(self.turn_latencies),
            "turn_latency_wall_ms": stats(self.turn_latencies_wall),
            "interruptions": self.interruptions,
            "interruption_latency_ms": stats(self.interrupt_latencies),
            "agent_turns": self.agent_turn,
            "user_utterances": self.user_utterance,
        }
