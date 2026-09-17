"""Engine logic tests without a GPU or model: VAD state machine, tool-call
parsing, prompt composition and the session turn/barge-in/tool flow against a
scripted runtime. Needs numpy (the engine image, or legenex/live/.venv).

Run:  python -m unittest discover -s engine/tests -t engine      (from legenex/live)
"""

from __future__ import annotations

import json
import struct
import sys
import threading
import time
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gx_live_engine import model as model_mod  # noqa: E402
from gx_live_engine.session import HEADER, KIND_AUDIO_OUT, EngineSession  # noqa: E402
from gx_live_engine.vad import RATE, WINDOW, StreamingVad  # noqa: E402


def energy_prob(win: np.ndarray) -> float:
    return float(min(1.0, np.sqrt(np.mean(win ** 2)) * 10))


def tone(seconds: float, amp: float = 0.3) -> np.ndarray:
    t = np.arange(int(seconds * RATE)) / RATE
    return (amp * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(seconds * RATE), dtype=np.float32)


class VadTests(unittest.TestCase):
    def test_utterance_start_and_stop(self):
        vad = StreamingVad(energy_prob, threshold=0.5, silence_ms=700, min_speech_ms=250, pre_roll_ms=300)
        events = []
        audio = np.concatenate([silence(0.5), tone(1.0), silence(1.0)])
        for i in range(0, len(audio), 1600):  # 100 ms frames
            events += vad.feed(audio[i:i + 1600])
        self.assertEqual([e.kind for e in events], ["start", "stop"])
        start, stop = events
        # confirmed after min_speech_ms (7 windows of 32 ms, the onset window included)
        self.assertAlmostEqual(start.at_sample / RATE, 0.5 + 7 * 0.032, delta=0.05)
        self.assertAlmostEqual(stop.at_sample / RATE, 1.5 + 0.7, delta=0.08)
        # the utterance keeps the pre-roll and the trailing silence window
        self.assertGreater(len(stop.audio) / RATE, 1.0 + 0.5)
        self.assertLess(len(stop.audio) / RATE, 1.0 + 0.35 + 0.8)

    def test_blip_is_ignored(self):
        vad = StreamingVad(energy_prob, min_speech_ms=250)
        events = vad.feed(np.concatenate([silence(0.3), tone(0.1), silence(1.5)]))
        self.assertEqual(events, [])
        self.assertFalse(vad.in_speech)

    def test_onset_is_stricter_while_assistant_audio_plays(self):
        vad = StreamingVad(lambda w: 0.6, threshold=0.5)
        self.assertEqual(vad.feed(silence(1.0), playing=True), [])
        self.assertEqual([e.kind for e in vad.feed(silence(1.0), playing=False)], ["start"])

    def test_commit_and_max_length(self):
        vad = StreamingVad(energy_prob, max_utterance_s=2.0)
        vad.feed(tone(0.6))
        ev = vad.commit()
        self.assertEqual(ev.kind, "stop")
        self.assertIsNone(vad.commit())
        events = vad.feed(tone(3.0))
        # the cap ends the utterance at 2 s; continuing speech opens the next one
        self.assertEqual([e.kind for e in events][:3], ["start", "stop", "start"])
        self.assertLessEqual(len(events[1].audio), 2 * RATE + WINDOW)

    def test_reset_calls_model_reset(self):
        calls = []
        vad = StreamingVad(energy_prob, reset=lambda: calls.append(1))
        vad.feed(tone(0.5))
        vad.reset()
        self.assertFalse(vad.in_speech)
        self.assertEqual(len(calls), 2)  # constructor + reset


class ToolParseTests(unittest.TestCase):
    def test_parse(self):
        call, err = model_mod.parse_tool_call('<tool_call>\n{"name": "get_time", "arguments": {}}\n</tool_call>')
        self.assertEqual(call, {"name": "get_time", "arguments": {}})
        self.assertIsNone(err)
        call, _ = model_mod.parse_tool_call('<tool_call>{"name":"delegate_to_gx","arguments":"{\\"model\\":\\"gx-fast\\"}"}')
        self.assertEqual(call["arguments"], {"model": "gx-fast"})
        for bad in ("<tool_call>{not json}</tool_call>", "<tool_call>", '<tool_call>{"arguments":{}}</tool_call>',
                    '<tool_call>{"name":"x","arguments":"{bad"}</tool_call>'):
            call, err = model_mod.parse_tool_call(bad)
            self.assertIsNone(call, bad)
            self.assertTrue(err)


class FakeTok:
    def apply_chat_template(self, msgs, tools=None, **kw):
        body = "# Tools\n<tools>\n" + "\n".join(json.dumps(t) for t in tools) + "\n</tools>"
        return f"<|im_start|>system\n{body}<|im_end|>\n<|im_start|>user\nx<|im_end|>\n"


class PromptTests(unittest.TestCase):
    def test_system_message(self):
        rt = model_mod.ModelRuntime("/nonexistent")
        rt.tok = FakeTok()
        rt.ref_audio = np.zeros(10, dtype=np.float32)
        tools = [{"type": "function", "function": {"name": "get_time"}}]
        msg = rt.system_message({"language": "en", "instructions": "Call me Tony."}, tools)
        self.assertEqual(msg["role"], "system")
        prefix, ref, suffix = msg["content"]
        self.assertEqual(prefix, model_mod.VOICE_PREFIX["en"])
        self.assertIs(ref, rt.ref_audio)
        self.assertIn("Call me Tony.", suffix)
        self.assertIn('"get_time"', suffix)
        self.assertTrue(suffix.endswith(model_mod.TOOL_RULE))
        plain = rt.system_message({"language": "zh"}, [])
        self.assertNotIn("<tools>", plain["content"][2])
        self.assertEqual(plain["content"][0], model_mod.VOICE_PREFIX["zh"])


class ScriptedRuntime:
    """Stands in for ModelRuntime: records calls, emits scripted audio."""

    def __init__(self):
        self.calls = []
        self.script = []          # list of SpeakResult-producing callables
        self.lock = threading.Lock()

    def start_session(self, sid, config, tools):
        self.calls.append(("start", sid, len(tools)))
        return 0.01

    def end_session(self):
        self.calls.append(("end",))

    @staticmethod
    def decode_jpeg(data):
        if data[:3] != b"\xff\xd8\xff":
            raise ValueError("not a jpeg")
        return ("image", len(data))

    def prefill_audio(self, sid, audio, frame):
        self.calls.append(("audio", len(audio), frame))
        return 0.02

    def prefill_text(self, sid, text, frame):
        self.calls.append(("text", text, frame))
        return 0.01

    def prefill_tool_response(self, sid, name, content):
        self.calls.append(("tool_response", name, content))
        return 0.01

    def transcribe(self, audio, language="en"):
        self.calls.append(("asr", len(audio)))
        return "what is this"

    def speak(self, sid, *, audio_out, max_tokens, cancel, on_audio, on_text, **kw):
        step = self.script.pop(0) if self.script else "talk"
        res = model_mod.SpeakResult()
        if step == "talk":
            on_audio(np.full(2400, 0.1, dtype=np.float32), "Hello ")
            on_audio(np.full(2400, 0.1, dtype=np.float32), "there.")
            res.text = "Hello there."
            res.first_audio_s = 0.1
            res.first_text_s = 0.05
            res.audio_samples = 4800
        elif step == "long":
            for _ in range(200):
                if cancel.is_set():
                    res.status = "interrupted"
                    break
                on_audio(np.zeros(240, dtype=np.float32), "la ")
                time.sleep(0.02)
        elif step == "tool":
            res.status = "tool"
            res.tool_call = {"name": "get_time", "arguments": {}}
        elif step == "badtool":
            res.status = "tool"
            res.tool_error = "the model produced a tool call that is not valid JSON"
        return res


class SessionHarness(unittest.TestCase):
    def setUp(self):
        self.rt = ScriptedRuntime()
        self.events = []
        self.binary = []
        self.cond = threading.Condition()
        self.vad = StreamingVad(energy_prob, silence_ms=300, min_speech_ms=100)

        def send_json(ev):
            with self.cond:
                self.events.append(ev)
                self.cond.notify_all()

        def send_binary(data):
            with self.cond:
                self.binary.append(data)
                self.events.append({"type": "_audio", "response": struct.unpack_from("!BBHI", data)[2]})
                self.cond.notify_all()

        self.sess = EngineSession(self.rt, "live_" + "0" * 32, {"language": "en", "max_response_tokens": 64},
                                  [{"x": 1}], send_json=send_json, send_binary=send_binary, vad=self.vad)
        self.sess.start()
        self.wait("session.ready")

    def tearDown(self):
        self.sess.stop()
        self.sess.worker.join(5)

    def wait(self, kind, n=1, timeout=10, **match):
        deadline = time.time() + timeout
        with self.cond:
            while time.time() < deadline:
                found = [e for e in self.events if e["type"] == kind
                         and all(e.get(k) == v for k, v in match.items())]
                if len(found) >= n:
                    return found[n - 1]
                self.cond.wait(0.1)
        raise AssertionError(f"{kind} x{n} not seen: {[e['type'] for e in self.events]}")

    def mic(self, audio, frame_ms=100):
        n = RATE * frame_ms // 1000
        for i in range(0, len(audio), n):
            chunk = (audio[i:i + n] * 32767).astype("<i2").tobytes()
            self.sess.on_binary(HEADER.pack(1, 1, 0, i // n) + chunk)

    def camera(self, data=b"\xff\xd8\xff\xe0jpeg"):
        self.sess.on_binary(HEADER.pack(2, 1, 0, 0) + data)


class SessionTests(SessionHarness):
    def test_spoken_turn(self):
        self.camera()
        self.mic(np.concatenate([tone(0.6), silence(0.5)]))
        done = self.wait("response.done")
        kinds = [e["type"] for e in self.events]
        for k in ("input.speech.started", "input.speech.stopped", "response.started",
                  "transcript.assistant.delta", "_audio"):
            self.assertIn(k, kinds)
        self.assertLess(kinds.index("response.started"), kinds.index("_audio"))
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["text"], "Hello there.")
        self.assertIsInstance(done["metrics"]["first_audio_ms"], int)
        self.assertIsInstance(done["metrics"]["turn_ms"], int)
        kind, ver, resp, seq = HEADER.unpack_from(self.binary[0])
        self.assertEqual((kind, ver, resp, seq), (KIND_AUDIO_OUT, 1, done["response"], 0))
        self.assertEqual(len(self.binary[0]) - 8, 2400 * 2)
        started = self.wait("response.started")
        self.assertTrue(started["camera_frame"])
        audio_call = next(c for c in self.rt.calls if c[0] == "audio")
        self.assertEqual(audio_call[2][0], "image")
        transcript = self.wait("transcript.user")
        self.assertEqual((transcript["text"], transcript["source"]), ("what is this", "speech"))

    def test_barge_in_interrupts_generation(self):
        self.rt.script = ["long", "talk"]
        self.sess.on_event({"type": "input.text", "text": "tell me a long story"})
        self.wait("_audio", n=3)
        self.mic(np.concatenate([tone(0.5), silence(0.5)]))
        intr = self.wait("response.interrupted")
        self.assertEqual(intr["reason"], "barge_in")
        self.assertIsInstance(intr["latency_ms"], int)
        self.assertLess(intr["latency_ms"], 500)
        started = self.wait("input.speech.started")
        self.assertTrue(started["during_response"])
        first = self.wait("response.done", response=1)
        self.assertEqual(first["status"], "interrupted")
        second = self.wait("response.done", response=2)
        self.assertEqual(second["status"], "completed")

    def test_text_cancels_running_answer(self):
        self.rt.script = ["long", "talk"]
        self.sess.on_event({"type": "input.text", "text": "one"})
        self.wait("_audio", n=2)
        self.sess.on_event({"type": "input.text", "text": "two"})
        intr = self.wait("response.interrupted")
        self.assertEqual(intr["reason"], "new_input")
        self.wait("response.done", response=2)
        texts = [c for c in self.rt.calls if c[0] == "text"]
        self.assertEqual([t[1] for t in texts], ["one", "two"])

    def test_tool_request_and_result(self):
        self.rt.script = ["tool", "talk"]
        self.sess.on_event({"type": "input.text", "text": "what time is it"})
        req = self.wait("tool.request")
        self.assertEqual(req["name"], "get_time")
        self.assertRegex(req["call_id"], r"^call_[0-9a-f]{16}$")
        done = self.wait("response.done", response=1)
        self.assertTrue(done["tool_call"])
        self.assertFalse([e for e in self.events if e["type"] == "_audio" and e["response"] == 1])
        self.sess.on_event({"type": "tool.result", "call_id": req["call_id"], "name": "get_time", "ok": True,
                            "content": "17:30"})
        started = self.wait("response.started", response=2)
        self.assertEqual(started["trigger"], "tool")
        self.wait("response.done", response=2)
        self.assertIn(("tool_response", "get_time", "17:30"), self.rt.calls)

    def test_invalid_tool_call_is_reported_to_the_model(self):
        self.rt.script = ["badtool", "talk"]
        self.sess.on_event({"type": "input.text", "text": "x"})
        self.wait("response.done", response=2)
        resp = [c for c in self.rt.calls if c[0] == "tool_response"]
        self.assertTrue(resp[0][2].startswith("error: "))
        self.assertFalse([e for e in self.events if e["type"] == "tool.request"])

    def test_camera_off_and_stale_frames_are_not_used(self):
        self.camera()
        self.sess.on_event({"type": "session.update", "camera": False})
        self.sess.on_event({"type": "input.text", "text": "a"})
        self.wait("response.done", response=1)
        self.sess.on_event({"type": "session.update", "camera": True})
        self.camera()
        self.sess.frame = (self.sess.frame[0] - 60, self.sess.frame[1])
        self.sess.on_event({"type": "input.text", "text": "b"})
        self.wait("response.done", response=2)
        frames = [c[2] for c in self.rt.calls if c[0] == "text"]
        self.assertEqual(frames, [None, None])

    def test_corrupt_frame_does_not_kill_the_turn(self):
        self.camera(b"\xff\xd8\xff")  # passes the header check, decode fails in the fake
        self.sess.frame = (self.sess.frame[0], b"notjpeg")
        self.sess.on_event({"type": "input.text", "text": "a"})
        self.wait("response.done")
        err = self.wait("error")
        self.assertEqual(err["code"], "bad_camera_frame")
        self.assertFalse(err["fatal"])

    def test_mute_and_detach(self):
        self.sess.on_event({"type": "session.update", "muted": True})
        self.mic(np.concatenate([tone(0.6), silence(0.5)]))
        time.sleep(0.3)
        self.assertFalse([e for e in self.events if e["type"] == "input.speech.started"])
        self.sess.on_event({"type": "session.update", "muted": False})
        self.rt.script = ["long"]
        self.sess.on_event({"type": "input.text", "text": "go"})
        self.wait("_audio", n=2)
        self.sess.on_event({"type": "client.detached"})
        intr = self.wait("response.interrupted")
        self.assertEqual(intr["reason"], "client_detached")
        self.mic(np.concatenate([tone(0.6), silence(0.5)]))
        time.sleep(0.2)
        self.assertFalse([e for e in self.events if e["type"] == "input.speech.started"])

    def test_cancel_while_only_playing(self):
        self.sess.on_event({"type": "input.text", "text": "a"})
        self.wait("response.done")
        self.sess.on_event({"type": "playback.state", "playing": True, "buffered_ms": 800})
        self.sess.on_event({"type": "response.cancel"})
        intr = self.wait("response.interrupted")
        self.assertEqual((intr["reason"], intr["latency_ms"]), ("client_cancel", 0))

    def test_stop_ends_the_model_session(self):
        self.sess.on_event({"type": "engine.session.end"})
        self.sess.worker.join(5)
        self.assertIn(("end",), self.rt.calls)


if __name__ == "__main__":
    unittest.main()
