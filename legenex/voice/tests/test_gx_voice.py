"""Hermetic tests for the gx-voice supervisor. Standard library only.

No GPU, no Docker daemon, no Qwen3-TTS: a stub HTTP server stands in for the
engine container (it writes sine-wave WAVs whose length follows the text), and
a fake Docker stands in for `docker run/exec/inspect` and the ffmpeg/ffprobe
helper (the fake copies WAVs and writes tagged bytes for compressed formats).

Run:  python3 -m unittest discover -s tests -t . -v     (from legenex/voice)
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import struct
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gx_voice import audio as au  # noqa: E402
from gx_voice import config as config_mod  # noqa: E402
from gx_voice import store as st  # noqa: E402
from gx_voice import validation as v  # noqa: E402
from gx_voice.engine import LOADING, READY, UNLOADED  # noqa: E402
from gx_voice.errors import (ConflictError, EngineError, NotFoundError, ResourceWait,  # noqa: E402
                             UnavailableError, ValidationError, VoiceError)
from gx_voice.server import build_servers  # noqa: E402
from gx_voice.service import _variant_order  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from voice_fakes import KEY, Rig, make_cfg, sine_pcm, write_wav  # noqa: E402

def preset(text: str, speaker: str = "ryan", **kw) -> dict:
    return {"operation": "tts", "segments": [{"text": text, "voice": {"kind": "preset", "speaker": speaker}}], **kw}


# ----------------------------------------------------------- validation --
class ValidationTests(unittest.TestCase):
    def test_defaults_and_router(self):
        r = v.job_request(preset("Hello there.", seed=7), max_chars=20000)
        self.assertEqual(r["seed"], 7)
        self.assertEqual(r["takes"], 1)
        self.assertEqual(r["language"], "auto")
        self.assertEqual(r["pause_ms"], 350)
        self.assertEqual(r["speed"], 1.0)
        self.assertEqual(v.variant_for({"kind": "preset", "speaker": "ryan"}), "custom")
        self.assertEqual(v.variant_for({"kind": "design", "description": "x"}), "design")
        self.assertEqual(v.variant_for({"kind": "reference", "reference_id": "ref-" + "a" * 32}), "base")

    def test_random_seed_recorded_and_fits_takes(self):
        r = v.job_request(preset("Hi.", takes=4), max_chars=20000)
        self.assertLessEqual(r["seed"] + 3, v.SEED_MAX)
        with self.assertRaises(ValidationError):
            v.job_request(preset("Hi.", takes=4, seed=v.SEED_MAX), max_chars=20000)

    def test_speaker_normalisation(self):
        self.assertEqual(v.speaker("Uncle Fu"), "uncle_fu")
        self.assertEqual(v.speaker("RYAN"), "ryan")
        with self.assertRaises(ValidationError):
            v.speaker("alloy")

    def test_invalid_requests(self):
        bad = [
            {}, {"operation": "sing", "segments": []}, preset(""), preset("x" * 10001),
            preset("hi", takes=5), preset("hi", takes=0), preset("hi", seed=-1), preset("hi", speed=3),
            preset("hi", pause_ms=6000), preset("hi", language="klingon"), preset("hi", extra=1),
            preset("hi", sampling={"temperature": 9}), preset("hi", sampling={"beam": 2}),
            {"operation": "tts", "segments": [{"text": "a", "voice": {"kind": "saved", "voice_id": "nope"}}]},
            {"operation": "tts", "segments": [{"text": "a", "voice": {"kind": "reference", "reference_id": "x"}}]},
            {"operation": "tts", "segments": [{"text": "a", "voice": {"kind": "clone"}}]},
            {"operation": "tts", "segments": [{"text": "a", "voice": {"kind": "preset", "speaker": "ryan"}},
                                               {"text": "b", "voice": {"kind": "preset", "speaker": "ryan"}}]},
            {"operation": "voice_design", "segments": [{"text": "a", "voice": {"kind": "preset", "speaker": "ryan"}}]},
            {"operation": "voice_clone", "segments": [{"text": "a", "voice": {"kind": "preset", "speaker": "ryan"}}]},
            {"operation": "dialogue", "segments": [{"text": "a", "voice": {"kind": "preset", "speaker": "ryan"},
                                                    "volume": 3}]},
            {"operation": "dialogue", "segments": [{"text": "a", "voice": {"kind": "preset", "speaker": "ryan"}}] * 61},
            preset("hi", seed=True), preset("hi", client_ref="bad ref!"),
        ]
        for body in bad:
            with self.subTest(body=str(body)[:80]), self.assertRaises(ValidationError):
                v.job_request(body, max_chars=20000)
        with self.assertRaises(ValidationError):
            v.job_request(preset("x" * 200), max_chars=100)

    def test_control_characters_and_whitespace(self):
        r = v.job_request(preset("  Hello\x00\x07 world \r\n"), max_chars=20000)
        self.assertEqual(r["segments"][0]["text"], "Hello world")

    def test_reference_without_transcript_is_x_vector(self):
        spec = v.voice_spec({"kind": "reference", "reference_id": "ref-" + "b" * 32})
        self.assertTrue(spec["x_vector_only"])
        spec = v.voice_spec({"kind": "reference", "reference_id": "ref-" + "b" * 32, "transcript": "Hi there."})
        self.assertFalse(spec["x_vector_only"])

    def test_chunking(self):
        text = ("First sentence. " * 30).strip() + "\n\nSecond paragraph! Short."
        paras = v.chunk_text(text, limit=120)
        self.assertEqual(len(paras), 2)
        self.assertTrue(all(len(c) <= 120 for p in paras for c in p))
        self.assertEqual(" ".join(paras[0]), ("First sentence. " * 30).strip())
        self.assertEqual(paras[1], ["Second paragraph! Short."])
        long_word = "a" * 700
        self.assertTrue(all(len(c) <= 300 for c in v.chunk_text(long_word)[0]))
        cjk = "你好。今天天气很好！我们去公园吧？" * 10
        self.assertTrue(all(len(c) <= 300 for c in v.chunk_text(cjk)[0]))

    def test_seeds_and_caps(self):
        self.assertEqual(v.chunk_seed(42, 0), 42)
        self.assertNotEqual(v.chunk_seed(42, 1), v.chunk_seed(43, 0))
        self.assertEqual(v.max_new_tokens("hi"), 192)
        self.assertEqual(v.max_new_tokens("x" * 5000), 8192)

    def test_speech_request(self):
        r = v.speech_request({"model": "gx-voice", "input": "Hello", "voice": "ryan"})
        self.assertEqual(r["format"], "mp3")
        self.assertEqual(r["speed"], 1.0)
        for bad in ({"model": "tts-1", "input": "x", "voice": "ryan"}, {"input": "", "voice": "ryan"},
                    {"input": "x"}, {"input": "x", "voice": "ryan", "response_format": "ogg"},
                    {"input": "x", "voice": "ryan", "speed": 3.0}, {"input": "x" * 4097, "voice": "ryan"},
                    {"input": "x", "voice": "ryan", "stream_format": "sse"}, {"input": "x", "voice": "a", "foo": 1}):
            with self.subTest(bad=str(bad)[:60]), self.assertRaises(ValidationError):
                v.speech_request(bad)

    def test_voice_record(self):
        rec = v.voice_record("vc_" + "1" * 24, {"name": " Narrator ", "voice": {"kind": "preset", "speaker": "aiden"},
                                                "instructions": "warm", "version": 3})
        self.assertEqual(rec["name"], "Narrator")
        self.assertEqual(rec["version"], 3)
        with self.assertRaises(ValidationError):
            v.voice_record("vc_" + "1" * 24, {"name": "x", "voice": {"kind": "design", "description": "d"}})
        with self.assertRaises(ValidationError):
            v.voice_record("bad", {"name": "x", "voice": {"kind": "preset", "speaker": "aiden"}})


# ---------------------------------------------------------------- audio --
class AudioTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_analyze_and_silence(self):
        p = self.tmp / "a.wav"
        write_wav(p, 1.0)
        info = au.analyze_wav(p)
        self.assertEqual(info.sample_rate, 24000)
        self.assertAlmostEqual(info.duration_s, 1.0, places=2)
        self.assertFalse(info.silent)
        self.assertGreater(info.rms_dbfs, -20)
        write_wav(p, 1.0, amp=0.0)
        self.assertTrue(au.analyze_wav(p).silent)

    def test_concat_is_deterministic_and_timed(self):
        a, b = sine_pcm(0.5), sine_pcm(0.25, freq=330)
        one = au.concat([(a, 0), (b, 350)], 24000)
        two = au.concat([(a, 0), (b, 350)], 24000)
        self.assertEqual(one, two)
        self.assertEqual(len(one), len(a) + len(b) + 2 * int(24000 * 0.35))

    def test_sniff_and_bad_wav(self):
        self.assertEqual(au.sniff(b"fLaC...."), "flac")
        self.assertEqual(au.sniff(b"ID3\x04"), "mp3")
        self.assertEqual(au.sniff(b"\x1a\x45\xdf\xa3xxxx"), "webm")
        self.assertIsNone(au.sniff(b"<html>"))
        bad = self.tmp / "b.wav"
        bad.write_bytes(b"RIFF\x00\x00\x00\x00WAVEjunk")
        with self.assertRaises(ValidationError):
            au.analyze_wav(bad)
        stereo = self.tmp / "s.wav"
        fmt = struct.pack("<HHIIHH", 1, 2, 24000, 96000, 4, 16)
        body = b"WAVEfmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", 4) + b"\x01\x00\x01\x00"
        stereo.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
        with self.assertRaises(ValidationError):
            au.read_pcm16(stereo)


# ---------------------------------------------------------------- store --
class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = st.Store(self.tmp / "db.sqlite3")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_job_lifecycle_and_recovery(self):
        jid = self.store.create_job(operation="tts", title="t", request={"x": 1}, client_ref=None)
        self.assertEqual(self.store.count_active(), 1)
        self.store.transition(jid, st.GENERATING, "speaking", 0.5)
        self.assertEqual(self.store.recover_interrupted(), 1)
        job = self.store.get_job(jid)
        self.assertEqual(job["status"], st.FAILED)
        self.assertEqual(job["error_code"], "interrupted")
        self.assertTrue(job["retryable"])
        self.assertIsNotNone(job["finished_at"])

    def test_voice_replica(self):
        rec = {"id": "vc_" + "a" * 24, "name": "Brand Voice", "spec": {"kind": "preset", "speaker": "ryan"},
               "instructions": "", "language": "english", "version": 1}
        self.store.put_voice(rec)
        self.store.put_voice({**rec, "version": 2, "name": "Brand  voice"})
        self.assertEqual(len(self.store.list_voices()), 1)
        self.assertEqual(self.store.voices_named("BRAND VOICE")[0]["version"], 2)
        self.assertTrue(self.store.delete_voice(rec["id"]))
        self.assertFalse(self.store.delete_voice(rec["id"]))


# ---------------------------------------------------------- engine policy --
class EnginePolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.rig = Rig(self.tmp)
        self.eng = self.rig.engine

    def tearDown(self):
        self.rig.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_block_reasons(self):
        self.assertIsNone(self.eng.policy_block_reason())
        self.rig.cfg.maintenance_hold_file.write_text("x")
        self.assertEqual(self.eng.policy_block_reason()[0], "maintenance")
        self.rig.cfg.gxmax_hold_file.write_text("x")
        self.assertEqual(self.eng.policy_block_reason()[0], "gx_max_active")
        os.utime(self.rig.cfg.gxmax_hold_file, (time.time() - 99999, time.time() - 99999))
        self.assertEqual(self.eng.policy_block_reason()[0], "maintenance")
        self.rig.cfg.maintenance_hold_file.unlink()
        self.rig.docker.running_set.discard("gx-llama-swap-node02")
        self.assertIn("drained", self.eng.gxmax_block_reason())
        self.rig.docker.running_set.add("gx-llama-swap-node02")
        self.rig.docker.existing.add("gx-max-rank1")
        self.assertIn("gx-max is running", self.eng.gxmax_block_reason())
        self.rig.docker.existing.discard("gx-max-rank1")
        self.rig.cfg.gxmax_deadman_pidfile.write_text(str(os.getpid()))
        self.assertIn("gx-max is running", self.eng.gxmax_block_reason())

    def test_load_waits_under_hold_and_admits_otherwise(self):
        self.rig.cfg.maintenance_hold_file.write_text("x")
        with self.assertRaises(ResourceWait) as ctx:
            self.eng.ensure_loaded()
        self.assertEqual(ctx.exception.code, "maintenance")
        self.assertEqual(self.rig.guard.launches, [])
        self.rig.cfg.maintenance_hold_file.unlink()
        self.assertIsNotNone(self.eng.ensure_loaded())
        self.assertEqual(self.eng.state, READY)
        self.assertIn("gx-voice-engine", self.rig.docker.running_set)
        run = next(c for c in self.rig.docker.calls if c[:2] == ["run", "-d"])
        self.assertIn("nvidia.com/gpu=all", run)
        self.assertIn(f"127.0.0.1:{self.rig.cfg.engine_port}:{self.rig.cfg.engine_port}", run)
        self.assertIn(f"{self.rig.cfg.models_dir}:/models:ro", run)
        self.assertNotIn(KEY, " ".join(run))
        self.assertIsNone(self.eng.ensure_loaded())

    def test_refusal_names_numbers_and_tries_room_once(self):
        self.rig.guard.refuse = ResourceWait("refused: reserve")
        with self.assertRaises(ResourceWait) as ctx:
            self.eng.ensure_loaded()
        self.assertIn("30 GiB reserve", ctx.exception.reason)
        self.assertEqual(len(self.rig.guard.launches), 1)  # free_node said "busy": no second try
        self.rig.docker.free_node_reply = {"freed": True, "models": ["qwen"]}
        with self.assertRaises(ResourceWait):
            self.eng.ensure_loaded()
        self.assertEqual(len(self.rig.guard.launches), 3)
        self.assertEqual(self.eng.state, UNLOADED)

    def test_make_room_respects_profiles(self):
        self.rig.docker.free_node_reply = {"freed": True}
        self.rig.cfg.profile_file.write_text(json.dumps({"profile": "max"}))
        self.assertIsNone(self.eng.make_room([], 0))
        self.rig.cfg.profile_file.write_text(json.dumps({"profile": "media"}))
        self.assertIsNone(self.eng.make_room([], 0))
        self.rig.cfg.profile_file.write_text(json.dumps({"profile": "auto"}))
        self.assertEqual(self.eng.make_room([], 0), "idle ComfyUI weights")

    def test_pending_memory_and_pins(self):
        self.assertEqual(self.eng.pending_gib(), 0.0)
        self.eng.state = LOADING
        self.eng.admit_avail_gib = None
        self.assertEqual(self.eng.pending_gib(), self.rig.cfg.engine_estimate_gib)
        self.eng.state = READY
        self.eng.resident_gib = 8.0
        self.assertEqual(self.eng.pending_gib(), round(self.rig.cfg.engine_estimate_gib - 8.0, 1))
        self.assertFalse(self.eng.pinned())
        self.rig.cfg.pins_file.write_text(json.dumps({"gx-voice": {"by": "t"}}))
        self.assertTrue(self.eng.pinned())
        self.rig.cfg.maintenance_hold_file.write_text("x")
        self.assertFalse(self.eng.pin_honoured())

    def test_reconcile_adopts_and_forgets(self):
        self.rig.docker.running_set.add("gx-voice-engine")
        self.eng.reconcile()
        self.assertEqual(self.eng.state, READY)
        self.assertEqual(self.rig.guard.registered, 1)
        self.rig.docker.running_set.discard("gx-voice-engine")
        self.eng.reconcile()
        self.assertEqual(self.eng.state, UNLOADED)
        self.assertGreaterEqual(self.rig.guard.released, 1)

    def test_engine_errors_are_user_safe(self):
        self.eng.ensure_loaded()
        self.rig.stub.fail_next = (507, {"error": {"code": "out_of_memory", "message": "CUDA OOM at /x/y.py"}})
        with self.assertRaises(EngineError) as ctx:
            self.eng.synthesize({"variant": "custom", "text": "x"})
        self.assertEqual(ctx.exception.code, "out_of_memory")
        self.assertNotIn("/x/y.py", ctx.exception.message)
        self.rig.stub.fail_next = (500, {"error": {"code": "engine_error", "message": "Traceback"}})
        with self.assertRaises(EngineError) as ctx:
            self.eng.synthesize({"variant": "custom", "text": "x"})
        self.assertNotIn("Traceback", ctx.exception.message)


# --------------------------------------------------------------- service --
class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.rig = Rig(self.tmp)
        self.svc = self.rig.service

    def tearDown(self):
        self.rig.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_preset_tts_multiple_takes(self):
        job = self.svc.submit(preset("Hello world. This is a test.", takes=3, seed=100,
                                     language="english"))
        self.assertEqual(job["status"], "queued")
        self.assertEqual(job["model"]["variants"]["custom"]["revision"], config_mod.VARIANTS["custom"].revision)
        done = self.rig.run_next()
        self.assertEqual(done["status"], "completed", done)
        self.assertEqual(len(done["takes"]), 3)
        self.assertEqual([t["seed"] for t in done["takes"]], [100, 101, 102])
        calls = self.rig.stub.calls
        self.assertEqual({c["variant"] for c in calls}, {"custom"})
        self.assertEqual({c["speaker"] for c in calls}, {"ryan"})
        self.assertEqual(calls[0]["language"], "english")
        for t in done["takes"]:
            self.assertGreater(t["duration_s"], 0.3)
            self.assertIn("wav", t["files"])
            self.assertIn("mp3", t["files"])
            self.assertTrue(t["waveform"])
        timings = done["timings"]
        for k in ("generate_s", "first_audio_s", "audio_s", "rtf", "total_s"):
            self.assertIn(k, timings)
        path, ctype = self.svc.content_path(done["id"], 2, "wav")
        self.assertEqual(ctype, "audio/wav")
        self.assertFalse((self.rig.cfg.jobs_dir / done["id"] / "units").exists() and
                         any((self.rig.cfg.jobs_dir / done["id"] / "units").iterdir()))

    def test_design_clone_and_saved_voice(self):
        job = self.svc.submit({"operation": "voice_design", "segments": [{
            "text": "Welcome to the show. " * 6, "voice": {"kind": "design", "description": "warm baritone narrator"},
            "instructions": "slow and calm"}]})
        done = self.rig.run_next()
        self.assertEqual(done["status"], "completed")
        self.assertEqual(self.rig.stub.calls[-1]["variant"], "design")
        self.assertEqual(self.rig.stub.calls[-1]["instruct"], "warm baritone narrator. slow and calm")
        # use the designed take as the reference of a clone (permitted: our own output)
        wav, _ = self.svc.content_path(job["id"], 0, "wav")
        ref = self.svc.add_reference(wav.read_bytes(), "designed.wav")
        self.assertTrue(ref["id"].startswith("ref-"))
        self.assertEqual(self.svc.add_reference(wav.read_bytes(), "again.wav")["id"], ref["id"])  # dedupe
        self.svc.submit({"operation": "voice_clone", "segments": [{
            "text": "A cloned line.", "voice": {"kind": "reference", "reference_id": ref["id"],
                                                "transcript": "Welcome to the show."},
            "instructions": "angry"}]})
        done = self.rig.run_next()
        self.assertEqual(done["status"], "completed")
        call = self.rig.stub.calls[-1]
        self.assertEqual(call["variant"], "base")
        self.assertEqual(call["reference"]["ref_id"], ref["id"])
        self.assertEqual(len(call["reference"]["cache_key"]), 64)
        self.assertFalse(call["reference"]["x_vector_only"])
        self.assertNotIn("instruct", call)
        self.assertTrue(any("not applied" in n for n in done["notes"]))
        # saved voice replica -> resolved to the reference
        vid = "vc_" + "c" * 24
        self.svc.put_voice(vid, {"name": "Narrator", "voice": {"kind": "reference", "reference_id": ref["id"],
                                                               "transcript": "Welcome to the show."},
                                 "language": "english"})
        self.svc.submit({"operation": "tts", "segments": [{"text": "Saved voice line.",
                                                            "voice": {"kind": "saved", "voice_id": vid}}]})
        done = self.rig.run_next()
        self.assertEqual(done["status"], "completed")
        self.assertEqual(self.rig.stub.calls[-1]["variant"], "base")
        self.assertEqual(self.rig.stub.calls[-1]["language"], "english")
        # a clip in use cannot be deleted
        with self.assertRaises(ConflictError):
            self.svc.delete_reference(ref["id"])
        self.svc.delete_voice(vid)
        self.assertTrue(self.svc.delete_reference(ref["id"])["deleted"])
        with self.assertRaises(NotFoundError):
            self.svc.submit({"operation": "tts", "segments": [{"text": "x",
                                                                "voice": {"kind": "saved", "voice_id": vid}}]})

    def test_dialogue_groups_variants_and_keeps_order(self):
        ref = self.rig.ref()
        self.rig.engine.variants_loaded = ["base"]
        self.svc.submit({"operation": "dialogue", "pause_ms": 500, "segments": [
            {"text": "Line one from Ryan.", "voice": {"kind": "preset", "speaker": "ryan"}},
            {"text": "Line two, cloned.", "voice": {"kind": "reference", "reference_id": ref}, "pause_ms": 200},
            {"text": "Line three from Serena.", "voice": {"kind": "preset", "speaker": "serena"}},
        ]})
        done = self.rig.run_next()
        self.assertEqual(done["status"], "completed", done)
        variants = [c["variant"] for c in self.rig.stub.calls]
        self.assertEqual(variants, ["base", "custom", "custom"])  # resident variant first, one switch
        self.assertEqual([c["text"] for c in self.rig.stub.calls],
                         ["Line two, cloned.", "Line one from Ryan.", "Line three from Serena."])
        expected = sum(max(0.3, 0.02 * len(t)) for t in ("Line one from Ryan.", "Line two, cloned.",
                                                          "Line three from Serena.")) + 0.2 + 0.5
        self.assertAlmostEqual(done["takes"][0]["duration_s"], expected, delta=0.01)

    def test_variant_order_helper(self):
        units = [{"variant": "custom", "i": 0}, {"variant": "base", "i": 1}, {"variant": "custom", "i": 2}]
        self.assertEqual([u["i"] for u in _variant_order(units, [])], [0, 2, 1])
        self.assertEqual([u["i"] for u in _variant_order(units, ["base"])], [1, 0, 2])

    def test_long_script_is_chunked_with_pauses(self):
        para = " ".join(f"Sentence number {i} is here." for i in range(30))
        self.svc.submit(preset(f"{para}\n\n{para}"))
        done = self.rig.run_next()
        self.assertEqual(done["status"], "completed")
        self.assertGreater(len(self.rig.stub.calls), 2)
        self.assertTrue(all(len(c["text"]) <= v.CHUNK_CHARS for c in self.rig.stub.calls))
        self.assertTrue(all(c["max_new_tokens"] == v.max_new_tokens(c["text"]) for c in self.rig.stub.calls))

    def test_silence_and_runaway_fail_honestly(self):
        self.rig.stub.silent = True
        self.svc.submit(preset("Hello."))
        done = self.rig.run_next()
        self.assertEqual(done["status"], "failed")
        self.assertIn("silence", done["error"]["message"])
        self.rig.stub.silent = False
        self.rig.stub.seconds_per_char = 100.0
        self.svc.submit(preset("Hello."))
        done = self.rig.run_next()
        self.assertEqual(done["status"], "failed")
        self.assertEqual(done["error"]["code"], "runaway")

    def test_speed_and_formats(self):
        self.svc.submit(preset("Quick line.", speed=1.25))
        done = self.rig.run_next()
        self.assertEqual(done["status"], "completed")
        self.assertTrue(any("atempo=1.250" in " ".join(c) for c in self.rig.docker.calls))
        for fmt, ctype in (("flac", "audio/flac"), ("opus", "audio/ogg"), ("aac", "audio/aac"),
                           ("pcm", "audio/L16;rate=24000;channels=1")):
            path, got = self.svc.content_path(done["id"], 0, fmt)
            self.assertEqual(got, ctype)
            self.assertTrue(path.is_file())
        with self.assertRaises(ValidationError):
            self.svc.content_path(done["id"], 0, "ogg")
        with self.assertRaises(NotFoundError):
            self.svc.content_path(done["id"], 3, "wav")

    def test_cancel_wait_delete_and_queue_limits(self):
        job = self.svc.submit(preset("One."))
        self.assertEqual(self.svc.cancel(job["id"])["status"], "cancelled")
        with self.assertRaises(ConflictError):
            self.svc.cancel(job["id"])
        self.svc.delete(job["id"])
        with self.assertRaises(NotFoundError):
            self.svc.job_view(job["id"])
        full = dataclasses.replace(self.rig.cfg, max_queue=1)
        self.svc.cfg = full
        self.svc.submit(preset("A."))
        with self.assertRaises(UnavailableError):
            self.svc.submit(preset("B."))

    def test_waits_while_maintenance_then_times_out(self):
        self.svc.cfg = dataclasses.replace(self.rig.cfg, resource_wait_s=0, resource_retry_s=2)
        self.rig.cfg.maintenance_hold_file.write_text("x")
        self.svc.submit(preset("Wait."))
        done = self.rig.run_next()
        self.assertEqual(done["status"], "failed")
        self.assertEqual(done["error"]["code"], "maintenance")
        self.assertIn("Maintenance", done["error"]["message"])

    def test_gxmax_claim_mid_job_interrupts(self):
        self.svc.submit({"operation": "dialogue", "segments": [
            {"text": "One.", "voice": {"kind": "preset", "speaker": "ryan"}},
            {"text": "Two.", "voice": {"kind": "preset", "speaker": "ryan"}}]})
        orig = self.rig.engine.synthesize

        def claim(params):  # noqa: ANN001
            out = orig(params)
            self.rig.docker.existing.add("gx-max-rank1")
            return out
        self.rig.engine.synthesize = claim
        done = self.rig.run_next()
        self.assertEqual(done["status"], "failed")
        self.assertEqual(done["error"]["code"], "engine_interrupted")

    def test_reaper(self):
        self.assertIsNone(self.svc.reap_once())
        self.rig.engine.ensure_loaded()
        self.rig.engine.last_activity = time.time() - 10_000
        self.rig.cfg.pins_file.write_text(json.dumps({"gx-voice": {}}))
        self.assertEqual(self.svc.reap_once(), "pinned")
        self.rig.cfg.pins_file.write_text("{}")
        self.assertEqual(self.svc.reap_once(), "idle")
        self.assertEqual(self.rig.engine.state, UNLOADED)
        self.assertNotIn("gx-voice-engine", self.rig.docker.running_set)
        self.rig.engine.ensure_loaded()
        self.rig.cfg.maintenance_hold_file.write_text("x")
        self.assertEqual(self.svc.reap_once(), "maintenance")
        self.rig.cfg.maintenance_hold_file.unlink()
        self.rig.engine.ensure_loaded()
        self.rig.cfg.gxmax_hold_file.write_text("x")
        self.assertEqual(self.svc.reap_once(), "gx-max")
        self.assertEqual(self.svc.reap_once(), "held")

    def test_unload_if_idle_rules(self):
        self.assertTrue(self.svc.unload(if_idle=True)["noop"])
        self.rig.engine.ensure_loaded()
        self.svc.submit(preset("Queued."))
        with self.assertRaises(ConflictError):
            self.svc.unload(if_idle=True)
        self.svc.cancel(self.rig.store.next_queued()["id"])
        self.rig.cfg.pins_file.write_text(json.dumps({"gx-voice": {}}))
        with self.assertRaises(ConflictError):
            self.svc.unload(if_idle=True)
        info = self.svc.unload()  # an explicit unload ignores the pin
        self.assertTrue(info["container_gone"])
        self.assertIn("mem_available_after_gib", info)

    def test_health_contract(self):
        h = self.svc.health()
        for k in ("status", "service", "state", "engine", "busy", "pinned", "idle_seconds", "queue",
                  "waiting", "memory", "blocked_by"):
            self.assertIn(k, h)
        self.assertEqual(h["state"], "unloaded")
        self.assertEqual(set(h["memory"]), {"estimate_gib", "resident_gib", "pending_gib", "reserve_gib"})
        self.rig.engine.ensure_loaded()
        self.assertEqual(self.svc.health()["state"], "ready")
        self.svc._current = "vox-x"  # noqa: SLF001
        self.assertEqual(self.svc.health()["state"], "busy")

    def test_reference_bounds(self):
        with self.assertRaises(ValidationError):
            self.svc.add_reference(b"<html>not audio</html>", "x.wav")
        with self.assertRaises(ValidationError):
            self.svc.add_reference(b"", "x.wav")
        short = self.tmp / "short.wav"
        write_wav(short, 1.0)
        with self.assertRaises(ValidationError):
            self.svc.add_reference(short.read_bytes(), "short.wav")
        silent = self.tmp / "silent.wav"
        write_wav(silent, 3.0, amp=0.0)
        with self.assertRaises(ValidationError):
            self.svc.add_reference(silent.read_bytes(), "silent.wav")
        self.assertEqual(list(self.rig.cfg.references_dir.iterdir()), [])

    def test_retention(self):
        job = self.svc.submit(preset("Old."))
        self.rig.run_next()
        self.rig.store.update_job(job["id"], finished_at=time.time() - 30 * 86400)
        self.assertEqual(self.svc.retention(days=14), 1)
        self.assertFalse((self.rig.cfg.jobs_dir / job["id"]).exists())

    def test_voice_by_name(self):
        self.assertEqual(self.svc.voice_by_name("Ryan"), {"kind": "preset", "speaker": "ryan"})
        self.assertEqual(self.svc.voice_by_name("preset:uncle_fu")["speaker"], "uncle_fu")
        with self.assertRaises(ValidationError):
            self.svc.voice_by_name("alloy")
        for i in (1, 2):
            self.svc.put_voice(f"vc_{i:024d}", {"name": "Twin", "voice": {"kind": "preset", "speaker": "ryan"}})
        with self.assertRaises(ValidationError):
            self.svc.voice_by_name("twin")


# ------------------------------------------------------------------ HTTP --
class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.rig = Rig(cls.tmp)
        cls.rig.service.start()
        cls.servers = build_servers(cls.rig.service, KEY, ("127.0.0.1",), cls.rig.cfg.port)
        for s in cls.servers:
            threading.Thread(target=s.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.rig.cfg.port}"

    @classmethod
    def tearDownClass(cls):
        cls.rig.service.stop()
        for s in cls.servers:
            s.shutdown()
            s.server_close()
        cls.rig.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def call(self, method, path, body=None, raw=None, key=KEY, headers=None):  # noqa: ANN001, ANN201
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        hdrs = {"Content-Type": "application/json" if raw is None else "application/octet-stream", **(headers or {})}
        if key:
            hdrs["Authorization"] = f"Bearer {key}"
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def wait(self, job_id):  # noqa: ANN001, ANN201
        for _ in range(200):
            _, _, body = self.call("GET", f"/v1/voice/jobs/{job_id}")
            job = json.loads(body)
            if job["status"] in ("completed", "failed", "cancelled"):
                return job
            time.sleep(0.05)
        self.fail("job did not finish")

    def test_health_is_open_everything_else_needs_the_key(self):
        status, _, body = self.call("GET", "/health", key=None)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["service"], "gx-voice")
        for path in ("/v1/voice/model", "/v1/voice/jobs", "/v1/models"):
            self.assertEqual(self.call("GET", path, key=None)[0], 401)
            self.assertEqual(self.call("GET", path, key="wrong" * 10)[0], 401)
        self.assertEqual(self.call("GET", "/v1/nope")[0], 404)

    def test_model_info(self):
        status, _, body = self.call("GET", "/v1/voice/model")
        self.assertEqual(status, 200)
        info = json.loads(body)
        self.assertEqual(set(info["variants"]), {"custom", "design", "base"})
        self.assertEqual(len(info["speakers"]), 9)
        self.assertNotIn(KEY, body.decode())

    def test_job_roundtrip_and_range(self):
        status, headers, body = self.call("POST", "/v1/voice/jobs", preset("Hello over HTTP.", takes=2))
        self.assertEqual(status, 202)
        job = json.loads(body)
        self.assertEqual(headers["Location"], f"/v1/voice/jobs/{job['id']}")
        done = self.wait(job["id"])
        self.assertEqual(done["status"], "completed", done)
        url = done["takes"][1]["files"]["wav"]["url"]
        status, headers, data = self.call("GET", url)
        self.assertEqual(status, 200)
        self.assertEqual(data[:4], b"RIFF")
        status, headers, part = self.call("GET", url, headers={"Range": "bytes=0-9"})
        self.assertEqual(status, 206)
        self.assertEqual(part, data[:10])
        self.assertEqual(self.call("GET", url, headers={"Range": "bytes=999999999-"})[0], 416)
        self.assertEqual(self.call("DELETE", f"/v1/voice/jobs/{job['id']}")[0], 200)
        self.assertEqual(self.call("GET", f"/v1/voice/jobs/{job['id']}")[0], 404)

    def test_bad_input(self):
        status, _, body = self.call("POST", "/v1/voice/jobs", raw=b"not json",
                                    headers={"Content-Type": "application/json"})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], "invalid_request")
        status, _, body = self.call("POST", "/v1/voice/jobs", preset("x", takes=9))
        self.assertEqual(status, 400)
        self.assertEqual(self.call("POST", "/v1/voice/references", raw=b"x" * (33 << 20))[0], 413)

    def test_openai_speech(self):
        status, headers, data = self.call("POST", "/v1/audio/speech",
                                          {"model": "gx-voice", "input": "Speak now.", "voice": "Aiden",
                                           "response_format": "wav"})
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "audio/wav")
        self.assertEqual(data[:4], b"RIFF")
        job_id = headers["X-GX-Job"]
        self.assertFalse((self.rig.cfg.jobs_dir / job_id).exists())  # audio not retained
        status, headers, data = self.call("POST", "/v1/audio/speech",
                                          {"model": "gx-voice", "input": "Speak.", "voice": "ryan",
                                           "response_format": "pcm"})
        self.assertEqual(status, 200)
        self.assertNotEqual(data[:4], b"RIFF")
        self.assertEqual(len(data) % 2, 0)
        status, _, body = self.call("POST", "/v1/audio/speech", {"model": "gx-voice", "input": "x", "voice": "alloy"})
        self.assertEqual(status, 400)
        self.assertIn("unknown voice", json.loads(body)["error"]["message"])

    def test_references_and_voices_over_http(self):
        p = self.tmp / "r.wav"
        write_wav(p, 3.0, amp=0.25)
        status, _, body = self.call("POST", "/v1/voice/references", raw=p.read_bytes(),
                                    headers={"X-Filename": "my%20clip.wav"})
        self.assertEqual(status, 201)
        ref = json.loads(body)
        self.assertEqual(ref["filename"], "my clip.wav")
        self.assertNotIn("path", ref)
        vid = "vc_" + "d" * 24
        status, _, _ = self.call("PUT", f"/v1/voice/voices/{vid}", {
            "name": "Clip Voice", "voice": {"kind": "reference", "reference_id": ref["id"]}})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(self.call("GET", f"/v1/voice/voices/{vid}")[2])["name"], "Clip Voice")
        self.assertEqual(self.call("GET", "/v1/voice/voices/vc_" + "e" * 24)[0], 404)
        self.assertEqual(self.call("DELETE", f"/v1/voice/references/{ref['id']}")[0], 409)
        status, headers, data = self.call("POST", "/v1/audio/speech",
                                          {"model": "gx-voice", "input": "By name.", "voice": "clip voice",
                                           "response_format": "mp3"})
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "audio/mpeg")
        self.assertEqual(self.call("DELETE", f"/v1/voice/voices/{vid}")[0], 200)
        self.assertEqual(self.call("DELETE", f"/v1/voice/references/{ref['id']}")[0], 200)

    def test_unload_and_load_endpoints(self):
        status, _, body = self.call("POST", "/v1/voice/unload", {"if_idle": True})
        self.assertEqual(status, 200)
        status, _, body = self.call("POST", "/v1/voice/load", {"variant": "design"})
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["variants_loaded"], ["design"])
        self.assertEqual(self.call("POST", "/v1/voice/load", {"variant": "huge"})[0], 400)
        status, _, body = self.call("POST", "/v1/voice/unload")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["container_gone"])


class ConfigTests(unittest.TestCase):
    def test_reserve_and_wildcards_refused(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            for env in ({"GX_GUARD_RESERVE_GIB": "20"}, {"GX_VOICE_BINDS": "0.0.0.0"},
                        {"GX_VOICE_ENGINE_ESTIMATE_GIB": "1"}, {"GX_VOICE_MAX_RESIDENT": "4"}):
                with self.subTest(env=env), self.assertRaises(VoiceError):
                    make_cfg(tmp, env)
            (tmp / "secrets").mkdir(exist_ok=True)
            (tmp / "secrets" / "short").write_text("abc")
            with self.assertRaises(VoiceError):
                config_mod.read_key(tmp / "secrets" / "short")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
