"""Hermetic tests for the gx-music supervisor. Standard library only.

No GPU, no Docker daemon, no ACE-Step: a stub upstream server stands in for
``acestep.api_server`` and a fake Docker stands in for the engine container
and the ffmpeg/ffprobe helper container.

Run:  python3 -m unittest discover -s tests -t . -v     (from legenex/music)
"""

from __future__ import annotations

import dataclasses
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gx_music import audio, validation as v  # noqa: E402
from gx_music import config as config_mod  # noqa: E402
from gx_music import store as st  # noqa: E402
from gx_music.engine import READY, UNLOADED, EngineController, GuardAdapter  # noqa: E402
from gx_music.errors import (ConflictError, EngineError, MusicError, NotFoundError, ResourceWait,  # noqa: E402
                             ValidationError)
from gx_music.server import TagIndex, build_servers  # noqa: E402
from gx_music.service import MusicService  # noqa: E402

TURBO = v.Capabilities.for_model("acestep-v15-xl-turbo")
BASE = v.Capabilities.for_model("acestep-v15-xl-base")
KEY = "k" * 40


def make_wav(path: Path, seconds: float = 2.0, rate: int = 48000, freq: float = 440.0,
             amp: float = 0.5, float32: bool = True, channels: int = 2) -> None:
    n = int(seconds * rate)
    if float32:
        frames = b"".join(struct.pack("<" + "f" * channels, *([amp * math.sin(2 * math.pi * freq * i / rate)] * channels))
                          for i in range(n))
        fmt = struct.pack("<HHIIHH", 3, channels, rate, rate * 4 * channels, 4 * channels, 32)
    else:
        frames = b"".join(struct.pack("<" + "h" * channels,
                                      *([int(amp * 32767 * math.sin(2 * math.pi * freq * i / rate))] * channels))
                          for i in range(n))
        fmt = struct.pack("<HHIIHH", 1, channels, rate, rate * 2 * channels, 2 * channels, 16)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(frames)) + frames
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)


# ---------------------------------------------------------------- validation --
class ValidationTests(unittest.TestCase):
    def test_generation_defaults_and_caption(self):
        r = v.generation({"prompt": "dreamy pop", "style_tags": ["Pop", "synth", "pop"], "seed": 7},
                         TURBO, max_duration=600)
        # no lyrics and nothing asks for a voice: an instrumental, and the caption says so
        self.assertEqual(r.engine["prompt"], "dreamy pop, synth, instrumental")
        self.assertEqual(r.vocal_mode, "instrumental_no_lyrics")
        self.assertEqual(r.engine["lyrics"], "[Instrumental]")
        self.assertFalse(r.engine["use_cot_caption"])
        self.assertEqual(r.style_tags, ["Pop", "synth"])
        self.assertEqual(r.engine["seed"], 7)
        self.assertFalse(r.engine["use_random_seed"])
        self.assertEqual(r.engine["task_type"], "text2music")
        self.assertEqual(r.engine["inference_steps"], 8)
        self.assertEqual(r.engine["audio_format"], "wav32")

    def test_instrumental_overrides_lyrics(self):
        r = v.generation({"prompt": "x", "lyrics": "[Verse]\nhello", "instrumental": True}, TURBO, max_duration=600)
        self.assertEqual(r.engine["lyrics"], "[Instrumental]")
        self.assertEqual(r.engine["vocal_language"], "unknown")

    def test_batch_seeds_are_consecutive(self):
        r = v.generation({"prompt": "x", "seed": 10, "batch_size": 3}, TURBO, max_duration=600)
        self.assertEqual(r.seeds, [10, 11, 12])
        self.assertEqual(r.engine["seed"], "10,11,12")

    def test_random_seed_is_recorded(self):
        r = v.generation({"prompt": "x"}, TURBO, max_duration=600)
        self.assertEqual(len(r.seeds), 1)
        self.assertIsInstance(r.engine["seed"], int)

    def test_key_and_time_signature_normalisation(self):
        self.assertEqual(v.normalize_key("Am"), "A minor")
        self.assertEqual(v.normalize_key("f# maj"), "F# major")
        self.assertEqual(v.normalize_key("B♭ minor"), "Bb minor")
        self.assertEqual(v.normalize_time_signature("6/8"), "6")
        for bad in ("H major", "C dorian", 5):
            with self.assertRaises(ValidationError):
                v.normalize_key(bad)
        with self.assertRaises(ValidationError):
            v.normalize_time_signature("5/4")

    def test_turbo_rejects_cfg_controls(self):
        for field in ("guidance_scale", "shift", "use_adg"):
            with self.assertRaises(ValidationError):
                v.generation({"prompt": "x", field: 3}, TURBO, max_duration=600)

    def test_base_accepts_guidance(self):
        r = v.generation({"prompt": "x", "guidance_scale": 5}, BASE, max_duration=600)
        self.assertEqual(r.engine["guidance_scale"], 5.0)

    def test_ranges(self):
        bad = [{"prompt": "x", "bpm": 20}, {"prompt": "x", "duration": 5}, {"prompt": "x", "duration": 601},
               {"prompt": "x", "inference_steps": 21}, {"prompt": "x", "batch_size": 5},
               {"prompt": "x", "vocal_language": "xx"}, {"prompt": "x", "seed": -1},
               {"prompt": "x", "lm_temperature": float("nan")}, {"prompt": 5}, {},
               {"prompt": "x" * 513}, {"prompt": "x", "lyrics": "y" * 4097},
               {"prompt": "x", "style_tags": "rock"}, {"prompt": "x", "style_tags": ["a"] * 25},
               {"prompt": "x", "bpm": True}, {"prompt": "x", "thinking": "yes"}]
        for body in bad:
            with self.subTest(body=str(body)[:60]), self.assertRaises(ValidationError):
                v.generation(body, TURBO, max_duration=600)

    def test_description_alone_lets_the_planner_write_the_song(self):
        r = v.generation({"description": "a soft bossa nova about rain"}, TURBO, max_duration=600)
        self.assertNotIn("sample_mode", r.engine)  # upstream sample mode guesses "instrumental" from words
        self.assertEqual(r.vocal_mode, "planner_lyrics")
        self.assertEqual(r.plan["fill"], ["caption", "lyrics"])
        self.assertFalse(r.plan["instrumental"])
        self.assertEqual(r.plan["query"], "a soft bossa nova about rain")
        self.assertEqual(r.engine["prompt"], "")

    def test_description_with_instrumental_passes_the_explicit_flag(self):
        # the old sample-mode path ignored the toggle unless the words said "instrumental"
        r = v.generation({"description": "a soft bossa nova about rain", "instrumental": True},
                         TURBO, max_duration=600)
        self.assertEqual(r.vocal_mode, "instrumental")
        self.assertTrue(r.plan["instrumental"])
        self.assertEqual(r.plan["fill"], ["caption"])
        self.assertEqual(r.engine["lyrics"], "[Instrumental]")
        # and a description that merely mentions the word does not switch vocals off
        r = v.generation({"description": "female vocals over an instrumental break", "lyrics": "[Verse]\nhi"},
                         TURBO, max_duration=600)
        self.assertEqual(r.vocal_mode, "vocals")
        self.assertEqual(r.plan["fill"], ["caption"])
        self.assertFalse(r.plan["instrumental"])

    def test_description_style_tags_and_prompt_combine(self):
        body = {"description": "An emotional song about leaving Cape Town after the end of a relationship.",
                "style_tags": ["female vocals", "cinematic", "piano", "melancholic", "slow build"],
                "prompt": "Intimate close-mic female vocal, soft piano opening, gradually expanding strings, "
                          "restrained percussion, powerful final chorus.",
                "lyrics": "[Verse]\nTable Mountain in the mirror\n[Chorus]\nI am leaving", "duration": 40,
                "bpm": 72, "key": "D minor", "vocal_language": "en"}
        r = v.generation(body, TURBO, max_duration=600)
        self.assertIsNone(r.plan)
        self.assertEqual(r.vocal_mode, "vocals")
        self.assertEqual(r.engine["prompt"],
                         "Intimate close-mic female vocal, soft piano opening, gradually expanding strings, "
                         "restrained percussion, powerful final chorus, female vocals, cinematic, melancholic, "
                         "slow build. An emotional song about leaving Cape Town after the end of a relationship.")
        # "piano" is already in the style prompt, so it is not repeated
        self.assertEqual(r.engine["prompt"].count("piano"), 1)
        self.assertEqual((r.engine["bpm"], r.engine["key_scale"], r.engine["audio_duration"]), (72, "D minor", 40.0))
        self.assertFalse(r.engine["use_cot_language"])  # explicit language is not second-guessed
        cond = r.conditioning()
        self.assertEqual(cond["caption"], r.engine["prompt"])
        self.assertFalse(cond["instrumental"])
        self.assertFalse(cond["caption_rewrite"])
        pub = r.public()
        self.assertEqual(pub["description"], body["description"])
        self.assertEqual(pub["conditioning"]["vocal_mode"], "vocals")

    def test_long_description_is_shortened_with_a_note(self):
        r = v.generation({"prompt": "p" * 400, "description": "word " * 80, "instrumental": True},
                         TURBO, max_duration=600)
        self.assertLessEqual(len(r.engine["prompt"]), v.CAPTION_MAX)
        self.assertTrue(r.engine["prompt"].endswith("…"))
        self.assertIn("shortened", r.notes[0])
        with self.assertRaises(ValidationError) as cm:
            v.generation({"prompt": "p" * 300, "style_tags": [c * 40 for c in "abcdef"]}, TURBO, max_duration=600)
        self.assertEqual(cm.exception.code, "caption_too_long")

    def test_vocal_rules(self):
        cases = [
            # (body, vocal_mode | error code, lyrics sent, caption contains)
            ({"prompt": "house", "instrumental": True, "lyrics": "[Verse]\nhello"}, "instrumental",
             "[Instrumental]", "instrumental"),
            ({"prompt": "house", "lyrics": "[Verse]\nhello"}, "vocals", "[Verse]\nhello", "vocals"),
            ({"prompt": "house", "lyrics": "[Verse]\nhello", "vocal_intent": "female"}, "vocals",
             "[Verse]\nhello", "female vocals"),
            ({"prompt": "house", "lyrics": "[Verse]\nhi", "vocal_intent": "duet"}, "vocals", None,
             "male and female vocal duet"),
            ({"prompt": "house", "lyrics": "[Verse]\nhi", "vocal_intent": "rap"}, "vocals", None, "rap vocals"),
            ({"prompt": "house", "vocal_intent": "choir"}, "lyrics_required", None, None),
            ({"style_tags": ["female vocals", "energetic", "house"]}, "lyrics_required", None, None),
            ({"prompt": "a singer over strings"}, "lyrics_required", None, None),
            ({"prompt": "deep house, no vocals"}, "instrumental_no_lyrics", "[Instrumental]", "instrumental"),
            ({"prompt": "techno without singing"}, "instrumental_no_lyrics", "[Instrumental]", None),
            ({"prompt": "x", "instrumental": True, "vocal_intent": "female"}, "vocal_conflict", None, None),
            ({"prompt": "x", "lyrics": "[Instrumental]", "vocal_intent": "male"}, "vocal_conflict", None, None),
            ({"prompt": "x", "lyrics": "[Instrumental]"}, "instrumental", "[Instrumental]", None),
            ({"prompt": "x", "lyrics": "[Verse]\n[Chorus]\n"}, "lyrics_without_words", None, None),
            ({"prompt": "x", "vocal_intent": "female", "lyrics_source": "planner"}, "planner_lyrics", "",
             "female vocals"),
            ({"prompt": "x", "vocal_intent": "female", "lyrics_source": "assistant"}, "lyrics_required", None, None),
            ({"prompt": "x", "vocal_intent": "female", "lyrics_source": "planner", "thinking": False},
             "invalid_request", None, None),
        ]
        for body, expect, lyrics, caption in cases:
            with self.subTest(body=body):
                if expect in v.VOCAL_MODES:
                    r = v.generation(body, TURBO, max_duration=600)
                    self.assertEqual(r.vocal_mode, expect)
                    if lyrics is not None:
                        self.assertEqual(r.engine["lyrics"], lyrics)
                    if caption:
                        self.assertIn(caption, r.engine["prompt"])
                    if expect.startswith("instrumental"):
                        self.assertEqual(r.engine["vocal_language"], "unknown")
                        self.assertFalse(r.engine["use_cot_language"])
                else:
                    with self.assertRaises(ValidationError) as cm:
                        v.generation(body, TURBO, max_duration=600)
                    self.assertEqual(cm.exception.code, expect)
        # the message tells the user what to do
        with self.assertRaises(ValidationError) as cm:
            v.generation({"style_tags": ["female vocals"]}, TURBO, max_duration=600)
        self.assertIn("Add lyrics", cm.exception.message)

    def test_mentions_vocals(self):
        for text in ("female vocals", "a Singer", "rap verse", "choir", "vocal", "duet"):
            self.assertTrue(v.mentions_vocals(text), text)
        for text in ("", "no vocals", "without vocals, deep house", "non-vocal", "instrumental piano"):
            self.assertFalse(v.mentions_vocals(text), text)
        self.assertTrue(v.lyric_has_words("[Verse]\nhi"))
        self.assertFalse(v.lyric_has_words("[Verse]\n\n[Chorus - big]"))
        self.assertTrue(v.is_instrumental_lyrics(""))

    def test_remix_keeps_inherited_lyrics_empty(self):
        r = v.remix({"prompt": "jazz", "source": {"job_id": "mus-" + "a" * 32}, "vocal_intent": "female"},
                    TURBO, max_duration=600)
        self.assertEqual(r.engine["lyrics"], "")  # the service fills it from the parent
        self.assertIn("female vocals", r.engine["prompt"])

    def test_apply_plan(self):
        r = v.generation({"description": "a folk song about the sea", "vocal_intent": "female", "bpm": 90},
                         TURBO, max_duration=600)
        v.apply_plan(r, {"caption": "An acoustic folk ballad, warm guitar.", "lyrics": "[Verse]\nSea at dawn",
                         "bpm": 120, "keyscale": "D major", "timesignature": "3", "duration": 95,
                         "vocal_language": "en"}, max_duration=600)
        self.assertEqual(r.engine["prompt"], "An acoustic folk ballad, warm guitar, female vocals")
        self.assertEqual(r.engine["lyrics"], "[Verse]\nSea at dawn")
        self.assertEqual(r.engine["bpm"], 90)  # the user's value wins
        self.assertEqual((r.engine["key_scale"], r.engine["time_signature"], r.engine["audio_duration"]),
                         ("D major", "3", 95.0))
        self.assertTrue(r.plan["done"])
        before = dict(r.engine)
        v.apply_plan(r, {"caption": "other", "lyrics": "[Verse]\nother"}, max_duration=600)  # idempotent
        self.assertEqual(r.engine, before)
        # a vocal plan without words fails instead of rendering an instrumental
        r = v.generation({"description": "a folk song"}, TURBO, max_duration=600)
        for lyr in ("", "[Instrumental]", "[Verse]\n"):
            with self.subTest(lyr=lyr), self.assertRaises(EngineError) as cm:
                v.apply_plan(r, {"caption": "folk", "lyrics": lyr}, max_duration=600)
            self.assertEqual(cm.exception.code, "lyrics_missing")
        with self.assertRaises(EngineError):
            v.apply_plan(r, {"caption": "", "lyrics": "[Verse]\nok"}, max_duration=600)
        # garbage metadata from the planner is ignored, not trusted
        r = v.generation({"description": "a folk song", "instrumental": True}, TURBO, max_duration=600)
        v.apply_plan(r, {"caption": "folk", "lyrics": "la", "bpm": "fast", "keyscale": "H dorian",
                         "timesignature": "7", "duration": 9999}, max_duration=600)
        self.assertEqual(r.engine["prompt"], "folk, instrumental")
        self.assertEqual(r.engine["lyrics"], "[Instrumental]")
        for k in ("bpm", "key_scale", "time_signature", "audio_duration"):
            self.assertNotIn(k, r.engine)

    def test_check_vocals_before_render(self):
        r = v.generation({"prompt": "x", "lyrics": "[Verse]\nhi"}, TURBO, max_duration=600)
        v.check_vocals_before_render(r)
        r.engine["lyrics"] = ""
        with self.assertRaises(EngineError):
            v.check_vocals_before_render(r)

    def test_analysis_request(self):
        a = v.analysis({"source": {"upload_id": "upl-" + "c" * 32}, "understand": True}, TURBO, max_duration=600)
        self.assertEqual((a.operation, a.engine), ("analyze", {"understand": True}))
        for bad in ({}, {"source": {"upload_id": "x"}}, {"source": {"upload_id": "upl-" + "c" * 32}, "prompt": "x"},
                    {"source": {"upload_id": "upl-" + "c" * 32}, "understand": "yes"}, []):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                v.analysis(bad, TURBO, max_duration=600)

    def test_no_lm_gets_explicit_duration(self):
        r = v.generation({"prompt": "x", "thinking": False}, TURBO, max_duration=600)
        self.assertEqual(r.engine["audio_duration"], 60.0)

    def test_control_characters_stripped(self):
        r = v.generation({"prompt": "rock\x00\x07 song"}, TURBO, max_duration=600)
        self.assertEqual(r.prompt, "rock song")

    def test_remix_edit_extend(self):
        src = {"job_id": "mus-" + "a" * 32, "index": 0}
        r = v.remix({"prompt": "jazz", "source": src, "strength": 0.3}, TURBO, max_duration=600)
        self.assertEqual(r.engine["task_type"], "cover")
        self.assertEqual(r.engine["audio_cover_strength"], 0.3)
        self.assertEqual(r.parent_job_id, src["job_id"])
        e = v.edit({"source": src, "start": 5, "end": 10}, TURBO, max_duration=600)
        self.assertEqual((e.engine["repainting_start"], e.engine["repainting_end"]), (5.0, 10.0))
        self.assertEqual(e.engine["chunk_mask_mode"], "explicit")
        with self.assertRaises(ValidationError):
            v.edit({"source": src, "start": 5, "end": 5.5}, TURBO, max_duration=600)
        x = v.extend({"source": src, "seconds": 20}, TURBO, max_duration=600)
        v.resolve_extend(x, 30.0, 600)
        self.assertEqual((x.engine["repainting_start"], x.engine["repainting_end"]), (30.0, 50.0))
        y = v.extend({"source": src, "seconds": 10, "direction": "start"}, TURBO, max_duration=600)
        v.resolve_extend(y, 30.0, 600)
        self.assertEqual((y.engine["repainting_start"], y.engine["repainting_end"]), (-10.0, 0.0))
        z = v.extend({"source": src, "seconds": 100}, TURBO, max_duration=120)
        with self.assertRaises(ValidationError):
            v.resolve_extend(z, 30.0, 120)

    def test_source_ref_validation(self):
        for bad in ({}, {"job_id": "x"}, {"job_id": "mus-" + "a" * 32, "upload_id": "upl-" + "b" * 32},
                    {"job_id": "../../etc/passwd"}, "mus-abc"):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                v.parse_source(bad, "source")

    def test_turbo_has_no_extract(self):
        self.assertNotIn("extract", TURBO.task_types)
        caps = TURBO.as_dict(600)
        self.assertIsNone(caps["operations"]["extract"])
        self.assertNotIn("guidance_scale", caps["controls"])
        self.assertIn("guidance_scale", BASE.as_dict(600)["controls"])


# --------------------------------------------------------------------- audio --
class AudioTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_sniff(self):
        self.assertEqual(audio.sniff(b"RIFF\x00\x00\x00\x00WAVE"), "wav")
        self.assertEqual(audio.sniff(b"fLaC\x00"), "flac")
        self.assertEqual(audio.sniff(b"ID3\x04"), "mp3")
        self.assertEqual(audio.sniff(b"\xff\xfb\x90\x00"), "mp3")
        self.assertEqual(audio.sniff(b"OggS\x00"), "ogg")
        self.assertIsNone(audio.sniff(b"<html>"))
        self.assertIsNone(audio.sniff(b"\x7fELF"))

    def test_float_wav_analysis(self):
        p = self.tmp / "a.wav"
        make_wav(p, seconds=1.0, amp=0.5)
        info = audio.analyze_wav(p, buckets=100)
        self.assertEqual((info.sample_rate, info.channels, info.bits, info.encoding), (48000, 2, 32, "float"))
        self.assertAlmostEqual(info.duration_s, 1.0, places=2)
        self.assertAlmostEqual(info.peak, 0.5, places=2)
        self.assertAlmostEqual(info.rms, 0.5 / math.sqrt(2), places=2)
        self.assertFalse(info.silent)
        self.assertLessEqual(len(info.waveform), 101)

    def test_pcm16_and_silence(self):
        p = self.tmp / "s.wav"
        make_wav(p, seconds=0.5, amp=0.0, float32=False)
        info = audio.analyze_wav(p)
        self.assertTrue(info.silent)
        self.assertEqual(info.encoding, "pcm")

    def test_fingerprint_ignores_header(self):
        a, b = self.tmp / "a.wav", self.tmp / "b.wav"
        make_wav(a, seconds=0.2)
        make_wav(b, seconds=0.2)
        self.assertEqual(audio.pcm_fingerprint(a), audio.pcm_fingerprint(b))
        make_wav(b, seconds=0.2, freq=220)
        self.assertNotEqual(audio.pcm_fingerprint(a), audio.pcm_fingerprint(b))

    def test_rejects_non_wav(self):
        p = self.tmp / "x.wav"
        p.write_bytes(b"not audio at all")
        with self.assertRaises(ValidationError):
            audio.analyze_wav(p)


# --------------------------------------------------------------------- fakes --
class StubUpstream:
    """Emulates acestep.api_server: /health, /release_task, /query_result."""

    def __init__(self, data_root: Path, key: str) -> None:
        self.data_root = data_root
        self.key = key
        self.tasks: dict[str, dict] = {}
        self.requests: list[dict] = []
        self.fail_next = False
        self.silent_next = False
        self.polls_before_done = 2
        self.samples: list[dict] = []
        self.sample_reply = {"caption": "A warm acoustic folk ballad with gentle guitar.",
                             "lyrics": "[Verse]\nMorning tide\n[Chorus]\nCarry me home", "bpm": 96,
                             "keyscale": "G major", "timesignature": "4", "duration": 75, "vocal_language": "en"}
        stub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, data, code=200):
                body = json.dumps({"data": data, "code": code, "error": None}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/health":
                    self._reply({"status": "ok", "models_initialized": True, "llm_initialized": True})

            def do_POST(self):
                if self.headers.get("Authorization") != f"Bearer {stub.key}":
                    self.send_response(401)
                    self.end_headers()
                    return
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path == "/v1/create_sample":
                    stub.samples.append(body)
                    self._reply(stub.sample_reply)
                    return
                if self.path == "/release_task":
                    stub.requests.append(body)
                    tid = f"t{len(stub.requests)}"
                    stub.tasks[tid] = {"body": body, "polls": 0, "fail": stub.fail_next, "silent": stub.silent_next}
                    stub.fail_next = stub.silent_next = False
                    self._reply({"task_id": tid, "status": "queued"})
                elif self.path == "/query_result":
                    tid = body["task_id_list"][0]
                    t = stub.tasks[tid]
                    t["polls"] += 1
                    if t["polls"] <= stub.polls_before_done:
                        item = {"status": 0, "progress": 0.4, "stage": "diffusion"}
                        self._reply([{"task_id": tid, "status": 0, "result": json.dumps([item])}])
                        return
                    if t["fail"]:
                        item = {"status": 2, "error": "CUDA out of memory"}
                        self._reply([{"task_id": tid, "status": 2, "result": json.dumps([item])}])
                        return
                    if t["body"].get("full_analysis_only"):
                        item = {"status_message": "Full Hardware Analysis Success", "bpm": 118,
                                "keyscale": "A Minor", "timesignature": "4", "duration": 12,
                                "genre": "synth-pop", "prompt": "An upbeat synth-pop song with female vocals.",
                                "lyrics": "[Verse]\nCity lights", "language": "en", "audio_codes": "<|x|>" * 50}
                        self._reply([{"task_id": tid, "status": 1, "result": json.dumps([item])}])
                        return
                    n = int(t["body"].get("batch_size") or 1)
                    items = []
                    seeds = str(t["body"]["seed"]).split(",")
                    for i in range(n):
                        name = f"api_audio/{tid}-{i}.wav"
                        dur = float(t["body"].get("audio_duration") or 3.0)
                        if t["body"].get("task_type") == "repaint":
                            dur = max(dur, float(t["body"].get("repainting_end") or 0) or dur)
                        make_wav(stub.data_root / name, seconds=1.2,
                                 freq=200 + 50 * int(seeds[i % len(seeds)]) % 400,
                                 amp=0.0 if t["silent"] else 0.3)
                        items.append({"file": f"/v1/audio?path=/work/tmp/{name}", "status": 1,
                                      "prompt": t["body"]["prompt"], "lyrics": t["body"]["lyrics"],
                                      "metas": {"bpm": 120, "keyscale": "C major", "timesignature": "4",
                                                "duration": dur}})
                    self._reply([{"task_id": tid, "status": 1, "result": json.dumps(items)}])

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


class FakeDocker:
    def __init__(self, data_root: Path) -> None:
        self.data_root = data_root
        self.containers: dict[str, bool] = {}
        self.calls: list[list[str]] = []
        self.probe_duration = 12.5
        self.free_requests = 0
        self.free_reply = {"freed": True, "models": ["qwen-image"]}
        self.analysis_calls: list[list[str]] = []
        self.analysis_reply = {"method": "gx-music DSP v1", "duration_s": 12.5,
                               "tempo": {"bpm": 118.0, "confidence": 0.7}, "key": {"value": "A minor"},
                               "time_signature": {"value": "4/4"}, "descriptors": ["mid tempo"]}

    def _cp(self, args, rc=0, out="", err=""):
        return subprocess.CompletedProcess(args, rc, out, err)

    def run(self, args, timeout=120):
        self.calls.append(args)
        if args[0] == "run" and "--entrypoint" in args:
            tool = args[args.index("--entrypoint") + 1]
            if tool.endswith("python"):
                self.analysis_calls.append(args)
                path = self._host(args[-1])
                if not path.exists():
                    return self._cp(args, 1, json.dumps({"error": "the audio could not be decoded"}))
                return self._cp(args, 0, json.dumps(self.analysis_reply))
            if tool == "ffprobe":
                path = self._host(args[-1])
                if not path.exists() or audio.sniff(path.read_bytes()[:16]) is None:
                    return self._cp(args, 1, "", "invalid data")
                return self._cp(args, 0, json.dumps({
                    "streams": [{"codec_type": "audio", "sample_rate": "48000", "channels": 2, "codec_name": "pcm"}],
                    "format": {"duration": str(self.probe_duration)}}))
            if tool == "ffmpeg":
                src = self._host(args[args.index("-i") + 1])
                shutil.copy(src, self._host(args[-1]))
                return self._cp(args)
        if args[0] == "run":
            self.containers[args[args.index("--name") + 1]] = True
            return self._cp(args, 0, "cid\n")
        if args[0] == "stop":
            self.containers[args[-1]] = False
            return self._cp(args)
        if args[0] == "rm":
            self.containers.pop(args[-1], None)
            return self._cp(args)
        if args[0] == "logs":
            return self._cp(args, 0, "log tail")
        if args[0] == "exec" and args[-1] == "gx_media_router.free_node":
            self.free_requests += 1
            return self._cp(args, 0, json.dumps(self.free_reply) + "\n")
        return self._cp(args, 1)

    def _host(self, container_path: str) -> Path:
        return self.data_root / container_path[len("/work/tmp/"):]

    def running(self, name):
        return self.containers.get(name, False)

    def exists(self, name):
        return name in self.containers


class FakeGuard:
    on_admitted = None

    def __init__(self):
        self.refuse = 0
        self.registered = False
        self.launches = 0
        self.extras: list[float] = []

    def launch(self, start, extra_gib=0.0):
        self.extras.append(extra_gib)
        if self.refuse:
            self.refuse -= 1
            raise ResourceWait("waiting for memory on the media node (other models are using it)")
        if self.on_admitted is not None:
            self.on_admitted()
        start()
        self.launches += 1
        self.registered = True

    def register(self):
        self.registered = True

    def release(self):
        self.registered = False


def make_config(root: Path, engine_port: int, **over) -> config_mod.Config:
    model = config_mod.ModelIdentity("acestep-v15-xl-turbo", "ACE-Step/acestep-v15-xl-turbo", "d4a0", "acestep-5Hz-lm-4B",
                                     "ACE-Step/acestep-5Hz-lm-4B", "0a3e", "ACE-Step/Ace-Step1.5", "1967",
                                     "https://github.com/ace-step/ACE-Step-1.5", "ca1e", "img:test")
    base = dict(
        binds=("127.0.0.1",), port=0, api_key=KEY, data_root=root / "data", checkpoints_dir=root / "ckpt",
        state_dir=root / "state", guard_dir=root / "guard", orchestrator_dir=root, node="node2", model=model,
        engine_container="gx-music", engine_port=engine_port, engine_key_file=root / "secrets" / "engine-key",
        engine_estimate_gib=32, engine_memory_cap="56g", engine_lm_backend="vllm", engine_start_timeout=60,
        idle_unload_s=600, resource_wait_s=30, resource_retry_s=2, generation_timeout_s=60,
        max_upload_bytes=1 << 20, max_queue=5, max_duration_s=600, evict_comfy=False, evict_reason=False,
        media_router_container="gx-media-router", media_router_url="",
        gxmax_hold_file=root / "guard" / "node2.gxmax-hold",
        maintenance_hold_file=root / "guard" / "node2.maintenance-hold", pins_file=root / "guard" / "pins.json",
        gxmax_hold_ttl_s=1200, gxmax_rank_container="gx-max-rank1",
        gxmax_deadman_pidfile=root / "guard" / "deadman.pid", control_plane_container="", reserve_gib=30)
    base.update(over)
    return config_mod.Config(**base)


class Harness:
    def __init__(self, **over):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "data").mkdir()
        (self.root / "ckpt").mkdir()
        (self.root / "guard").mkdir()
        self.docker = FakeDocker(self.root / "data")
        self.guard = FakeGuard()
        # Start the stub on a free port first, then point the config at it.
        tmp_cfg = make_config(self.root, 1, **over)
        engine_key = EngineController(tmp_cfg, docker=self.docker, guard=self.guard)._key
        self.upstream = StubUpstream(self.root / "data", engine_key)
        self.cfg = dataclasses.replace(tmp_cfg, engine_port=self.upstream.port)
        self.engine = EngineController(self.cfg, docker=self.docker, guard=self.guard)
        self.store = st.Store(self.cfg.db_path)
        self.service = MusicService(self.cfg, self.store, self.engine)
        self.service.start()
        self.servers = build_servers(self.service, KEY, ("127.0.0.1",), 0)
        self.base = f"http://127.0.0.1:{self.servers[0].server_address[1]}"
        threading.Thread(target=self.servers[0].serve_forever, daemon=True).start()

    def close(self):
        self.service.stop()
        self.servers[0].shutdown()
        self.upstream.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def call(self, method, path, body=None, key=KEY, raw=None, headers=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        h = {"Content-Type": "application/json"} if raw is None else {"Content-Type": "application/octet-stream"}
        if key:
            h["Authorization"] = f"Bearer {key}"
        h.update(headers or {})
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                payload = r.read()
                return r.status, (json.loads(payload) if r.headers.get("Content-Type") == "application/json" else payload), r.headers
        except urllib.error.HTTPError as e:
            payload = e.read()
            try:
                return e.code, json.loads(payload), e.headers
            except ValueError:
                return e.code, payload, e.headers

    def wait(self, job_id, statuses=("completed", "failed", "cancelled"), timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            _, job, _ = self.call("GET", f"/v1/music/{job_id}")
            if job["status"] in statuses:
                return job
            time.sleep(0.2)
        raise AssertionError(f"job {job_id} stuck in {job['status']}")



class ConditioningIntegrationTests(unittest.TestCase):
    """Description + style + vocal rules through the real service and HTTP."""

    def setUp(self):
        self.h = Harness()

    def tearDown(self):
        self.h.close()

    def test_preview_is_exactly_what_is_sent(self):
        body = {"description": "A song about leaving Cape Town.", "style_tags": ["cinematic", "piano"],
                "prompt": "intimate close-mic vocal", "vocal_intent": "female",
                "lyrics": "[Verse]\nGoodbye mountain", "duration": 30, "seed": 3}
        code, prev, _ = self.h.call("POST", "/v1/music/preview", body)
        self.assertEqual(code, 200)
        self.assertEqual(prev["vocal_mode"], "vocals")
        caption = prev["conditioning"]["caption"]
        self.assertEqual(caption, "intimate close-mic vocal, cinematic, piano, female vocals. "
                                  "A song about leaving Cape Town.")
        self.assertEqual(self.h.store.list_jobs(), [])  # a preview never queues
        _, job, _ = self.h.call("POST", "/v1/music/generations", body)
        job = self.h.wait(job["id"])
        self.assertEqual(job["status"], "completed", job.get("error"))
        sent = self.h.upstream.requests[-1]
        self.assertEqual(sent["prompt"], caption)
        self.assertEqual(sent["lyrics"], "[Verse]\nGoodbye mountain")
        self.assertFalse(sent["use_cot_caption"])
        self.assertNotIn("sample_mode", sent)
        self.assertNotIn("sample_query", sent)
        self.assertEqual(job["request"]["conditioning"]["caption"], caption)
        self.assertEqual(job["request"]["description"], body["description"])
        self.assertEqual(job["tracks"][0]["vocal_mode"], "vocals")
        code, err, _ = self.h.call("POST", "/v1/music/preview", {"style_tags": ["female vocals"]})
        self.assertEqual((code, err["error"]["code"]), (400, "lyrics_required"))

    def test_regression_female_vocal_tags_without_lyrics_are_refused_not_rendered(self):
        # the request of job mus-051483f1 (2026-09-17) that rendered an instrumental
        code, err, _ = self.h.call("POST", "/v1/music/generations",
                                   {"style_tags": ["female vocals", "energetic", "house", "driving beat", "modern"],
                                    "title": "Waves", "instrumental": False})
        self.assertEqual(code, 400)
        self.assertEqual(err["error"]["code"], "lyrics_required")
        self.assertEqual(self.h.upstream.requests, [])

    def test_planner_writes_lyrics_with_an_explicit_instrumental_flag(self):
        _, job, _ = self.h.call("POST", "/v1/music/generations",
                                {"description": "a folk song about the sea", "style_tags": ["acoustic"],
                                 "vocal_intent": "female", "lyrics_source": "planner", "seed": 4})
        job = self.h.wait(job["id"])
        self.assertEqual(job["status"], "completed", job.get("error"))
        self.assertEqual(self.h.upstream.samples[-1], {
            "query": "a folk song about the sea\nStyle: acoustic\nVocals: female vocals",
            "instrumental": False, "vocal_language": "unknown", "temperature": 0.85})
        sent = self.h.upstream.requests[-1]
        self.assertEqual(sent["lyrics"], "[Verse]\nMorning tide\n[Chorus]\nCarry me home")
        self.assertEqual(sent["prompt"], "acoustic, female vocals. a folk song about the sea.")
        self.assertEqual((sent["bpm"], sent["key_scale"], sent["audio_duration"]), (96, "G major", 75.0))
        self.assertTrue(job["request"]["plan"]["done"])
        self.assertIn("plan_s", job["timings"])
        self.assertEqual(job["request"]["lyrics"], sent["lyrics"])

    def test_instrumental_description_never_gets_planner_lyrics(self):
        _, job, _ = self.h.call("POST", "/v1/music/generations",
                                {"description": "a calm song for studying", "instrumental": True})
        job = self.h.wait(job["id"])
        self.assertEqual(job["status"], "completed", job.get("error"))
        self.assertTrue(self.h.upstream.samples[-1]["instrumental"])
        sent = self.h.upstream.requests[-1]
        self.assertEqual(sent["lyrics"], "[Instrumental]")
        self.assertEqual(sent["prompt"], "A warm acoustic folk ballad with gentle guitar, instrumental")
        self.assertEqual(sent["vocal_language"], "unknown")

    def test_planner_without_words_fails_honestly(self):
        self.h.upstream.sample_reply = {**self.h.upstream.sample_reply, "lyrics": "[Instrumental]"}
        _, job, _ = self.h.call("POST", "/v1/music/generations", {"description": "a pop song"})
        job = self.h.wait(job["id"])
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error"]["code"], "lyrics_missing")
        self.assertEqual(self.h.upstream.requests, [])  # nothing was rendered

    def test_analysis_measured_then_understood(self):
        src = self.h.root / "ref.wav"
        make_wav(src, seconds=1.5)
        _, up, _ = self.h.call("POST", "/v1/music/uploads", raw=src.read_bytes(), headers={"X-Filename": "ref.wav"})
        code, job, _ = self.h.call("POST", "/v1/music/analyses",
                                   {"source": {"upload_id": up["id"]}, "understand": False})
        self.assertEqual(code, 202)
        self.assertEqual(job["operation"], "analyze")
        done = self.h.wait(job["id"])
        self.assertEqual(done["status"], "completed", done.get("error"))
        self.assertEqual(done["analysis"]["measured"]["tempo"]["bpm"], 118.0)
        self.assertIsNone(done["analysis"]["understanding"])
        self.assertEqual(self.h.engine.state, UNLOADED)  # measuring never loads the model
        helper = self.h.docker.analysis_calls[-1]
        for flag in ("--network", "none", "--read-only"):
            self.assertIn(flag, helper)
        self.assertNotIn("nvidia.com/gpu=all", helper)
        self.assertTrue(any(a.endswith(":/work/tmp:ro") for a in helper))

        _, job, _ = self.h.call("POST", "/v1/music/analyses", {"source": {"upload_id": up["id"]}, "understand": True})
        done = self.h.wait(job["id"])
        self.assertEqual(done["status"], "completed", done.get("error"))
        u = done["analysis"]["understanding"]
        self.assertTrue(u["vocals_detected"])
        self.assertEqual((u["bpm"], u["key"], u["language"]), (118, "A minor", "en"))
        self.assertIn("model inference", u["method"])
        self.assertNotIn("audio_codes", json.dumps(done))
        sent = self.h.upstream.requests[-1]
        self.assertTrue(sent["full_analysis_only"])
        self.assertTrue(sent["src_audio_path"].startswith("/work/tmp/uploads/"))
        self.assertIn("understand_s", done["timings"])
        # analyses stay out of the creative listing
        _, lst, _ = self.h.call("GET", "/v1/music/jobs?operation=creative")
        self.assertEqual(lst["data"], [])
        _, lst, _ = self.h.call("GET", "/v1/music/jobs?operation=analyze")
        self.assertEqual(len(lst["data"]), 2)

    def test_analysis_failures(self):
        code, err, _ = self.h.call("POST", "/v1/music/analyses", {"source": {"upload_id": "upl-" + "d" * 32}})
        self.assertEqual(code, 404)
        src = self.h.root / "ref.wav"
        make_wav(src, seconds=1.5)
        _, up, _ = self.h.call("POST", "/v1/music/uploads", raw=src.read_bytes(), headers={"X-Filename": "r.wav"})
        self.h.docker.analysis_reply = {"error": "the audio is too short to analyse (under one second)"}
        _, job, _ = self.h.call("POST", "/v1/music/analyses", {"source": {"upload_id": up["id"]}})
        done = self.h.wait(job["id"])
        self.assertEqual(done["status"], "failed")
        self.assertEqual(done["error"]["code"], "analysis_failed")
        self.assertIn("too short", done["error"]["message"])


# ------------------------------------------------------------------- service --
class ServiceIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()

    def tearDown(self):
        self.h.close()

    def test_auth_and_health(self):
        code, body, _ = self.h.call("GET", "/health", key=None)
        self.assertEqual(code, 200)
        self.assertEqual(body["engine"], UNLOADED)
        for key in (None, "wrong"):
            code, body, _ = self.h.call("GET", "/v1/music/model", key=key)
            self.assertEqual(code, 401)
            self.assertEqual(body["error"]["code"], "unauthorized")

    def test_generation_end_to_end(self):
        code, job, hdrs = self.h.call("POST", "/v1/music/generations",
                                      {"prompt": "lofi beat", "style_tags": ["chill"], "seed": 5, "duration": 30,
                                       "instrumental": True, "batch_size": 2})
        self.assertEqual(code, 202)
        self.assertEqual(hdrs["Location"], f"/v1/music/{job['id']}")
        job = self.h.wait(job["id"])
        self.assertEqual(job["status"], "completed", job.get("error"))
        self.assertEqual(len(job["tracks"]), 2)
        t0 = job["tracks"][0]
        self.assertEqual(t0["seed"], 5)
        self.assertEqual(job["tracks"][1]["seed"], 6)
        self.assertEqual(t0["sample_rate"], 48000)
        self.assertEqual(set(t0["files"]), {"wav", "flac", "mp3"})
        self.assertNotIn("/work/tmp", json.dumps(job))
        self.assertNotIn(str(self.h.root), json.dumps(job))
        self.assertEqual(self.h.engine.state, READY)
        self.assertIn("model_load_s", job["timings"])
        sent = self.h.upstream.requests[0]
        self.assertEqual(sent["lyrics"], "[Instrumental]")
        self.assertEqual(sent["lm_backend"], "vllm")
        self.assertEqual(sent["audio_format"], "wav32")
        # content + range
        code, data, hdrs = self.h.call("GET", f"/v1/music/{job['id']}/content?index=1&format=wav")
        self.assertEqual(code, 200)
        self.assertEqual(audio.sniff(data[:12]), "wav")
        code, part, hdrs = self.h.call("GET", f"/v1/music/{job['id']}/content?format=wav",
                                       headers={"Range": "bytes=0-99"})
        self.assertEqual(code, 206)
        self.assertEqual(len(part), 100)
        self.assertTrue(hdrs["Content-Range"].startswith("bytes 0-99/"))
        code, _, _ = self.h.call("GET", f"/v1/music/{job['id']}/content?format=ogg")
        self.assertEqual(code, 400)
        code, _, _ = self.h.call("GET", f"/v1/music/{job['id']}/content?index=5")
        self.assertEqual(code, 404)

    def test_remix_edit_extend_lineage_and_inheritance(self):
        _, j, _ = self.h.call("POST", "/v1/music/generations",
                              {"prompt": "folk song", "lyrics": "[Verse]\nla la", "seed": 1, "duration": 30})
        parent = self.h.wait(j["id"])
        _, r, _ = self.h.call("POST", "/v1/music/remix",
                              {"prompt": "jazz version", "source": {"job_id": parent["id"]}, "strength": 0.4})
        remix = self.h.wait(r["id"])
        self.assertEqual(remix["status"], "completed", remix.get("error"))
        self.assertEqual(remix["parent_job_id"], parent["id"])
        sent = self.h.upstream.requests[-1]
        self.assertEqual(sent["task_type"], "cover")
        self.assertEqual(sent["lyrics"], "[Verse]\nla la")  # inherited
        self.assertTrue(sent["src_audio_path"].startswith(f"/work/tmp/jobs/{parent['id']}/"))

        # probe duration isn't used for job sources: duration comes from the parent track
        dur = parent["tracks"][0]["duration_s"]
        _, e, _ = self.h.call("POST", "/v1/music/edits", {"source": {"job_id": parent["id"]}, "start": 0.0,
                                                          "end": max(1.0, dur)})
        self.assertEqual(e.get("status"), "queued", e)
        edit = self.h.wait(e["id"])
        self.assertEqual(edit["status"], "completed", edit.get("error"))
        sent = self.h.upstream.requests[-1]
        self.assertEqual(sent["prompt"], "folk song, vocals")  # inherited: the caption the parent really used
        self.assertEqual(sent["task_type"], "repaint")

        _, x, _ = self.h.call("POST", "/v1/music/extend", {"source": {"job_id": remix["id"]}, "seconds": 15})
        ext = self.h.wait(x["id"])
        self.assertEqual(ext["status"], "completed", ext.get("error"))
        sent = self.h.upstream.requests[-1]
        rdur = remix["tracks"][0]["duration_s"]
        self.assertAlmostEqual(sent["repainting_start"], rdur, places=2)
        self.assertAlmostEqual(sent["repainting_end"], rdur + 15, places=2)

        _, lin, _ = self.h.call("GET", f"/v1/music/{parent['id']}/lineage")
        kids = {c["id"]: c for c in lin["descendants"]}
        self.assertEqual(set(kids), {remix["id"], edit["id"]})
        self.assertEqual(kids[remix["id"]]["children"][0]["id"], ext["id"])
        _, lin2, _ = self.h.call("GET", f"/v1/music/{ext['id']}/lineage")
        self.assertEqual([a["id"] for a in lin2["ancestors"]], [remix["id"], parent["id"]])

    def test_edit_range_validated_against_source(self):
        _, j, _ = self.h.call("POST", "/v1/music/generations", {"prompt": "x", "duration": 30})
        parent = self.h.wait(j["id"])
        code, body, _ = self.h.call("POST", "/v1/music/edits",
                                    {"source": {"job_id": parent["id"]}, "start": 100, "end": 120})
        self.assertEqual(code, 400)
        self.assertIn("past the end", body["error"]["message"])

    def test_missing_source_is_404(self):
        code, body, _ = self.h.call("POST", "/v1/music/remix",
                                    {"prompt": "x", "source": {"job_id": "mus-" + "0" * 32}})
        self.assertEqual(code, 404)

    def test_upload_flow(self):
        wav = self.h.root / "ref.wav"
        make_wav(wav, seconds=0.3)
        code, up, _ = self.h.call("POST", "/v1/music/uploads", raw=wav.read_bytes(),
                                  headers={"X-Filename": "my%20ref.wav"})
        self.assertEqual(code, 201, up)
        self.assertEqual(up["container"], "wav")
        self.assertEqual(up["filename"], "my ref.wav")
        self.assertEqual(up["duration_s"], 12.5)
        code, j, _ = self.h.call("POST", "/v1/music/generations",
                                 {"prompt": "x", "reference": {"upload_id": up["id"]}})
        job = self.h.wait(j["id"])
        self.assertEqual(job["status"], "completed")
        self.assertTrue(self.h.upstream.requests[-1]["reference_audio_path"].startswith("/work/tmp/uploads/"))
        # remix of an upload
        code, r, _ = self.h.call("POST", "/v1/music/remix", {"prompt": "rock", "source": {"upload_id": up["id"]}})
        self.assertEqual(self.h.wait(r["id"])["status"], "completed")

    def test_upload_rejections(self):
        code, body, _ = self.h.call("POST", "/v1/music/uploads", raw=b"<html>evil</html>")
        self.assertEqual((code, body["error"]["code"]), (400, "invalid_source"))
        code, body, _ = self.h.call("POST", "/v1/music/uploads", raw=b"RIFF" + b"\0" * (2 << 20))
        self.assertEqual(code, 413)
        self.h.docker.probe_duration = 0.2
        wav = self.h.root / "short.wav"
        make_wav(wav, seconds=0.1)
        code, body, _ = self.h.call("POST", "/v1/music/uploads", raw=wav.read_bytes())
        self.assertEqual(code, 400)
        self.assertEqual(list((self.h.cfg.uploads_dir).glob("*")), [])

    def test_engine_failure_is_humanised(self):
        self.h.upstream.fail_next = True
        _, j, _ = self.h.call("POST", "/v1/music/generations", {"prompt": "x"})
        job = self.h.wait(j["id"])
        self.assertEqual(job["status"], "failed")
        self.assertIn("ran out of memory", job["error"]["message"])
        self.assertNotIn("CUDA", job["error"]["message"])

    def test_silence_is_rejected(self):
        self.h.upstream.silent_next = True
        _, j, _ = self.h.call("POST", "/v1/music/generations", {"prompt": "x"})
        job = self.h.wait(j["id"])
        self.assertEqual(job["status"], "failed")
        self.assertIn("silence", job["error"]["message"])

    def test_resource_wait_then_success(self):
        self.h.guard.refuse = 1
        _, j, _ = self.h.call("POST", "/v1/music/generations", {"prompt": "x"})
        self.h.wait(j["id"], statuses=("waiting_for_resource",), timeout=10)
        job = self.h.wait(j["id"])
        self.assertEqual(job["status"], "completed")
        self.assertIn("resource_wait_s", job["timings"])

    def test_resource_wait_gives_up(self):
        self.h.guard.refuse = 10**6
        self.h.service.cfg = dataclasses.replace(self.h.cfg, resource_wait_s=0)
        _, j, _ = self.h.call("POST", "/v1/music/generations", {"prompt": "x"})
        job = self.h.wait(j["id"])
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error"]["code"], "insufficient_memory")
        self.assertTrue(job["error"]["retryable"])

    def test_gxmax_hold_blocks_load_and_forces_unload(self):
        self.h.cfg.gxmax_hold_file.write_text("x")
        _, j, _ = self.h.call("POST", "/v1/music/generations", {"prompt": "x"})
        job = self.h.wait(j["id"], statuses=("waiting_for_resource",), timeout=10)
        self.assertIn("gx-max", job["detail"])
        self.assertEqual(self.h.guard.launches, 0)
        code, _, _ = self.h.call("POST", "/v1/music/load")
        self.assertEqual(code, 503)
        self.h.cfg.gxmax_hold_file.unlink()
        self.assertEqual(self.h.wait(j["id"])["status"], "completed")
        # rank container appears while loaded -> reaper-style check unloads
        self.h.docker.containers["gx-max-rank1"] = True
        self.assertIsNotNone(self.h.engine.gxmax_block_reason())
        self.h.engine.unload("gx-max drain")
        self.assertFalse(self.h.docker.exists("gx-music"))
        self.assertFalse(self.h.guard.registered)

    def test_gxmax_reclaim_requeues_the_interrupted_job(self):
        svc = self.h.service
        _, j, _ = self.h.call("POST", "/v1/music/generations", {"prompt": "x"})
        jid = j["id"]
        self.h.wait(jid)  # let the first run finish normally, then simulate a reclaim on a new job
        _, j2, _ = self.h.call("POST", "/v1/music/generations", {"prompt": "y"})
        svc.stop()  # stop the worker so the test drives the job itself
        self.h.cfg.gxmax_hold_file.write_text("x")
        err = EngineError("the music engine stopped during generation (the node was reclaimed)",
                          code="engine_interrupted", retryable=True)
        self.assertTrue(svc._requeue_if_reclaimed(j2["id"], err))
        job = self.h.store.get_job(j2["id"])
        self.assertEqual(job["status"], "waiting_for_resource")
        self.assertIn("gx-max", job["detail"])
        # bounded
        for _ in range(svc.MAX_REQUEUES - 1):
            self.assertTrue(svc._requeue_if_reclaimed(j2["id"], err))
        self.assertFalse(svc._requeue_if_reclaimed(j2["id"], err))
        # other errors, or no reclaim, are real failures
        self.h.cfg.gxmax_hold_file.unlink()
        self.assertFalse(svc._requeue_if_reclaimed("mus-" + "0" * 32, err))
        self.assertFalse(svc._requeue_if_reclaimed(j2["id"], EngineError("boom")))

    def test_reaper_is_quiet_while_gxmax_holds_an_unloaded_node(self):
        self.h.cfg.gxmax_hold_file.write_text("x")
        self.assertEqual(self.h.service.reap_once(), "held")
        self.assertFalse(any(c[0] == "stop" for c in self.h.docker.calls))

    def test_maintenance_blocks_new_loads_and_unloads_an_idle_engine(self):
        eng = self.h.engine
        eng.ensure_loaded()
        self.assertTrue(eng.is_loaded())
        self.h.cfg.maintenance_hold_file.write_text("x")
        # a running track is allowed to finish
        self.h.service._current = "mus-running"
        self.assertIsNone(self.h.service.reap_once())
        self.assertTrue(eng.is_loaded())
        self.h.service._current = None
        self.assertEqual(self.h.service.reap_once(), "maintenance")
        self.assertFalse(self.h.docker.exists("gx-music"))
        code, body, _ = self.h.call("POST", "/v1/music/load")
        self.assertEqual(code, 503)
        self.assertEqual(body["error"]["code"], "maintenance")
        _, j, _ = self.h.call("POST", "/v1/music/generations", {"prompt": "x"})
        job = self.h.wait(j["id"], statuses=("waiting_for_resource",), timeout=10)
        self.assertIn("Maintenance", job["detail"])
        self.h.cfg.maintenance_hold_file.unlink()
        self.assertEqual(self.h.wait(j["id"])["status"], "completed")

    def test_pin_keeps_the_engine_only_while_allowed(self):
        eng = self.h.engine
        eng.ensure_loaded()
        eng.last_activity -= 10_000
        self.h.cfg.pins_file.write_text(json.dumps({"gx-music": {"by": "admin"}}))
        with mock.patch("gx_music.engine.meminfo", return_value={"MemAvailable": 80.0}):
            self.assertTrue(eng.pin_honoured())
            self.assertEqual(self.h.service.reap_once(), "pinned")
            self.assertTrue(eng.is_loaded())
            model = self.h.call("GET", "/v1/music/model")[1]
            self.assertTrue(model["policy"]["pinned"])
        # under the reserve the pin is suspended
        with mock.patch("gx_music.engine.meminfo", return_value={"MemAvailable": 20.0}):
            self.assertFalse(eng.pin_honoured())
            self.assertEqual(self.h.service.reap_once(), "idle")
        self.assertFalse(eng.is_loaded())

    def test_pin_never_overrides_maintenance(self):
        eng = self.h.engine
        self.h.cfg.pins_file.write_text(json.dumps({"gx-music": {}}))
        self.h.cfg.maintenance_hold_file.write_text("x")
        with mock.patch("gx_music.engine.meminfo", return_value={"MemAvailable": 100.0}):
            self.assertFalse(eng.pin_honoured())

    def test_other_gxmax_signals(self):
        eng = self.h.engine
        self.h.docker.containers["gx-max-rank1"] = False  # created/exited still blocks
        self.assertIsNotNone(eng.gxmax_block_reason())
        del self.h.docker.containers["gx-max-rank1"]
        self.h.cfg.gxmax_deadman_pidfile.write_text(str(os.getpid()))
        self.assertIsNotNone(eng.gxmax_block_reason())
        self.h.cfg.gxmax_deadman_pidfile.write_text("999999999")
        self.assertIsNone(eng.gxmax_block_reason())
        eng.cfg = dataclasses.replace(self.h.cfg, control_plane_container="gx-llama-swap-node02")
        self.assertIn("drained", eng.gxmax_block_reason())
        self.h.docker.containers["gx-llama-swap-node02"] = True
        self.assertIsNone(eng.gxmax_block_reason())
        eng.cfg = self.h.cfg

    def test_load_aborts_when_gxmax_appears(self):
        eng = self.h.engine
        original = eng.health
        calls = {"n": 0}

        def slow_health(timeout=5):
            calls["n"] += 1
            if calls["n"] == 1:
                self.h.docker.containers["gx-max-rank1"] = True
                return {"models_initialized": False}
            return original(timeout)

        eng.health = slow_health
        with self.assertRaises(ResourceWait):
            eng.ensure_loaded()
        self.assertFalse(self.h.docker.exists("gx-music"))
        self.assertFalse(self.h.guard.registered)

    def test_concurrent_load_does_not_tear_down(self):
        eng = self.h.engine
        original = eng.health
        gate = threading.Event()

        def gated(timeout=5):
            gate.wait(5)
            return original(timeout)

        eng.health = gated
        results = []
        t1 = threading.Thread(target=lambda: results.append(eng.ensure_loaded()))
        t1.start()
        time.sleep(0.5)
        t2 = threading.Thread(target=lambda: results.append(eng.ensure_loaded()))
        t2.start()
        time.sleep(0.5)
        gate.set()
        t1.join(10)
        t2.join(10)
        self.assertEqual(self.h.guard.launches, 1)
        self.assertEqual(eng.state, READY)
        self.assertEqual(sorted(r is None for r in results), [False, True])
        stops = [c for c in self.h.docker.calls if c[0] == "stop"]
        self.assertEqual(stops, [])

    def test_stale_hold_is_ignored(self):
        hold = self.h.cfg.gxmax_hold_file
        hold.write_text("x")
        old = time.time() - 5000
        os.utime(hold, (old, old))
        self.assertIsNone(self.h.engine.gxmax_block_reason())

    def test_cancel_queued_and_conflicts(self):
        self.h.guard.refuse = 10**6
        _, j, _ = self.h.call("POST", "/v1/music/generations", {"prompt": "x"})
        self.h.wait(j["id"], statuses=("waiting_for_resource",), timeout=10)
        code, body, _ = self.h.call("POST", f"/v1/music/{j['id']}/cancel")
        self.assertEqual((code, body["status"]), (200, "cancelled"))
        code, _, _ = self.h.call("POST", f"/v1/music/{j['id']}/cancel")
        self.assertEqual(code, 409)
        code, _, _ = self.h.call("DELETE", f"/v1/music/{j['id']}")
        self.assertEqual(code, 200)
        code, _, _ = self.h.call("GET", f"/v1/music/{j['id']}")
        self.assertEqual(code, 404)

    def test_queue_limit(self):
        self.h.guard.refuse = 10**6
        for _ in range(5):
            self.assertEqual(self.h.call("POST", "/v1/music/generations", {"prompt": "x"})[0], 202)
        code, body, hdrs = self.h.call("POST", "/v1/music/generations", {"prompt": "x"})
        self.assertEqual((code, body["error"]["code"]), (503, "queue_full"))
        self.assertEqual(hdrs["Retry-After"], "30")

    def test_load_unload_endpoints_release_everything(self):
        code, snap, _ = self.h.call("POST", "/v1/music/load")
        self.assertEqual((code, snap["state"]), (200, READY))
        self.assertTrue(self.h.guard.registered)
        code, info, _ = self.h.call("POST", "/v1/music/unload")
        self.assertEqual(code, 200)
        self.assertTrue(info["container_gone"])
        self.assertFalse(self.h.guard.registered)
        self.assertEqual(self.h.engine.state, UNLOADED)

    def test_request_hygiene(self):
        code, body, _ = self.h.call("POST", "/v1/music/generations", raw=b"not json",
                                    headers={"Content-Type": "application/json"})
        self.assertEqual(code, 400)
        code, _, _ = self.h.call("POST", "/v1/music/generations", raw=b"{}",
                                 headers={"Content-Type": "text/plain"})
        self.assertEqual(code, 400)
        code, _, _ = self.h.call("GET", "/v1/music/../../etc/passwd")
        self.assertEqual(code, 404)
        code, _, _ = self.h.call("GET", "/v1/music/jobs?status=DROP%20TABLE")
        self.assertEqual(code, 400)
        code, body, _ = self.h.call("GET", "/v1/music/nope")
        self.assertEqual(code, 404)
        code, _, _ = self.h.call("GET", "/v1/music/jobs?operation=drop")
        self.assertEqual(code, 400)

    def test_model_info_and_tags(self):
        code, info, _ = self.h.call("GET", "/v1/music/model")
        self.assertEqual(code, 200)
        self.assertEqual(info["alias"], "gx-music")
        self.assertEqual(info["capabilities"]["task_types"], ["text2music", "cover", "repaint"])
        self.assertEqual(info["identity"]["dit_repo"], "ACE-Step/acestep-v15-xl-turbo")
        code, tags, _ = self.h.call("GET", "/v1/music/tags?q=synthwa&limit=3")
        self.assertEqual(code, 200)
        self.assertTrue(all("synthwa" in t.lower() for t in tags["suggestions"]))
        self.assertIn("genre", tags["groups"])

    def test_interrupted_jobs_fail_on_restart(self):
        jid = self.h.store.create_job(operation="generate", title="t", request={}, model={},
                                      parent_job_id=None, parent_index=None, source=None)
        self.h.store.transition(jid, st.GENERATING)
        self.assertEqual(self.h.store.recover_interrupted(), 1)
        job = self.h.store.get_job(jid)
        self.assertEqual((job["status"], job["error_code"]), ("failed", "interrupted"))

    def test_reconcile_adopts_running_engine(self):
        self.h.docker.containers["gx-music"] = True
        eng = EngineController(self.h.cfg, docker=self.h.docker, guard=self.h.guard)
        eng.reconcile()
        self.assertEqual(eng.state, READY)
        self.assertTrue(self.h.guard.registered)


class MediaEvictionTests(unittest.TestCase):
    """Music frees idle ComfyUI weights only through the media router."""

    def _controller(self, evict: bool, reply: dict, refusals: int):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        docker, guard = FakeDocker(root), FakeGuard()
        docker.free_reply = reply
        guard.refuse = refusals
        cfg = make_config(root, 1, evict_comfy=evict)
        return EngineController(cfg, docker=docker, guard=guard), docker, guard

    def test_router_free_then_admitted(self):
        ctl, docker, guard = self._controller(True, {"freed": True, "models": ["m"]}, 1)
        with mock.patch("gx_music.engine.time.sleep"):
            ctl._admit_and_start()
        self.assertEqual(docker.free_requests, 1)
        self.assertEqual(guard.launches, 1)
        self.assertNotIn(["exec", "gx-comfyui"], [c[:2] for c in docker.calls])

    def test_router_busy_means_wait_not_kill(self):
        ctl, docker, guard = self._controller(True, {"freed": False, "reason": "busy (job-1)"}, 1)
        with self.assertRaises(ResourceWait):
            ctl._admit_and_start()
        self.assertEqual(docker.free_requests, 1)
        self.assertEqual(guard.launches, 0)
        self.assertFalse(any(c[0] in ("stop", "kill") for c in docker.calls))

    def test_free_asked_at_most_once_per_attempt(self):
        ctl, docker, guard = self._controller(True, {"freed": True, "models": []}, 2)
        with mock.patch("gx_music.engine.time.sleep"), self.assertRaises(ResourceWait):
            ctl._admit_and_start()
        self.assertEqual(docker.free_requests, 1)

    def test_disabled_never_asks(self):
        ctl, docker, guard = self._controller(False, {"freed": True}, 1)
        with self.assertRaises(ResourceWait):
            ctl._admit_and_start()
        self.assertEqual(docker.free_requests, 0)


class ReserveCoordinationTests(unittest.TestCase):
    """D-038: gx-music and the media router keep the 30 GiB reserve together."""

    def setUp(self):
        self.h = Harness()

    def tearDown(self):
        self.h.close()

    def test_health_publishes_what_the_router_needs_and_nothing_private(self):
        code, body, _ = self.h.call("GET", "/health", key=None)
        self.assertEqual(code, 200)
        self.assertEqual((body["busy"], body["pinned"], body["active_jobs"]), (False, False, 0))
        self.assertEqual(body["memory"]["pending_gib"], 0.0)
        self.assertEqual(body["memory"]["estimate_gib"], 32)
        self.assertEqual(body["memory"]["reserve_gib"], 30)
        self.assertNotIn(KEY, json.dumps(body))
        eng = self.h.engine
        # loading: 32 GiB estimate, 10 GiB already gone since admission -> 22 pending
        with mock.patch("gx_music.engine.meminfo", return_value={"MemAvailable": 100.0}):
            eng._record_admission()
        eng.state = "loading"
        with mock.patch("gx_music.engine.meminfo", return_value={"MemAvailable": 90.0}):
            self.assertEqual(eng.pending_gib(), 22.0)
        # ready: measured 26 GiB resident -> 6 GiB generation headroom stays pending
        eng.state, eng.loaded_gib = "ready", 26.0
        self.assertEqual(eng.pending_gib(), 6.0)
        eng.state = "unloaded"
        self.assertEqual(eng.pending_gib(), 0.0)

    def test_load_measures_the_resident_size(self):
        values = iter([114.0, 88.0, 88.0, 88.0, 88.0])
        with mock.patch("gx_music.engine.meminfo", side_effect=lambda: {"MemAvailable": next(values, 88.0)}):
            self.h.engine.ensure_loaded()
        self.assertEqual(self.h.engine.loaded_gib, 26.0)
        self.assertEqual(self.h.engine.pending_gib(), 6.0)
        self.h.engine.unload("test")
        self.assertIsNone(self.h.engine.loaded_gib)

    def test_running_media_job_growth_is_added_to_the_music_admission(self):
        eng = self.h.engine
        media = {"reachable": True, "busy": True, "held_by": "video-abc", "resident_alias": "gx-video",
                 "pending_gib": 40.0, "waiting": 0}
        self.h.guard.refuse = 1
        with mock.patch.object(eng, "media_state", return_value=media), \
                mock.patch("gx_music.engine.meminfo", return_value={"MemAvailable": 88.0}):
            with self.assertRaises(ResourceWait) as ctx:
                eng._admit_and_start()
        self.assertEqual(self.h.guard.extras, [40.0])
        reason = ctx.exception.reason
        self.assertIn("Waiting for gx-video to finish on gx10-02", reason)
        self.assertIn("32 GiB plus the 30 GiB reserve plus 40 GiB", reason)
        self.assertIn("102 GiB must be available; 88 GiB is", reason)
        self.assertEqual(eng.last_wait["reason"], reason)
        # nothing pending on the router: only the engine's own estimate is asked for
        with mock.patch.object(eng, "media_state", return_value={}):
            eng._admit_and_start()
        self.assertEqual(self.h.guard.extras[-1], 0.0)

    def test_the_real_guard_refuses_music_next_to_a_video_that_would_break_the_reserve(self):
        repo = Path(__file__).resolve().parents[2]
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        (root / "guard").mkdir()
        cfg = make_config(root, 1, orchestrator_dir=repo / "orchestrator")
        adapter = GuardAdapter(cfg)
        meminfo = root / "meminfo"
        started = []
        real = adapter.rg.guard_launch

        def guard_with(avail):
            meminfo.write_text(f"MemAvailable: {int(avail * 1048576)} kB\n")
            return lambda *a, **kw: real(*a, **{**kw, "meminfo_path": str(meminfo)})

        # a video is loaded: 42 GiB available; 42 - 32 < 30 -> refused, nothing started
        with mock.patch.object(adapter.rg, "guard_launch", guard_with(42.0)):
            with self.assertRaises(ResourceWait):
                adapter.launch(lambda: started.append(1))
        # 88 GiB available but the router still has 40 GiB of a cold video to take: refused
        with mock.patch.object(adapter.rg, "guard_launch", guard_with(88.0)):
            with self.assertRaises(ResourceWait):
                adapter.launch(lambda: started.append(1), extra_gib=40.0)
        self.assertEqual(started, [])
        # the same 88 GiB with nothing pending: admitted (88 - 32 = 56 >= 30)
        with mock.patch.object(adapter.rg, "guard_launch", guard_with(88.0)):
            adapter.launch(lambda: started.append(1))
        self.assertEqual(started, [1])
        ledger = json.loads((root / "guard" / "node2-residency.json").read_text())
        self.assertEqual(ledger["gx-music"]["estimated_gib"], 32)

    def test_if_idle_unload_never_takes_the_engine_from_waiting_work(self):
        eng = self.h.engine
        eng.ensure_loaded()
        jid = self.h.store.create_job(operation="generate", title="t", request={}, model={},
                                      parent_job_id=None, parent_index=None, source=None)
        code, body, _ = self.h.call("POST", "/v1/music/unload", {"if_idle": True})
        self.assertEqual(code, 409)
        self.assertIn("queued", body["error"]["message"])
        self.assertTrue(eng.is_loaded())
        self.h.store.transition(jid, st.CANCELLED)
        self.h.cfg.pins_file.write_text(json.dumps({"gx-music": {"by": "admin"}}))
        with mock.patch("gx_music.engine.meminfo", return_value={"MemAvailable": 80.0}):
            code, body, _ = self.h.call("POST", "/v1/music/unload", {"if_idle": True})
        self.assertEqual(code, 409)
        self.assertIn("pinned", body["error"]["message"])
        self.h.cfg.pins_file.write_text("{}")
        code, body, _ = self.h.call("POST", "/v1/music/unload", {"if_idle": True})
        self.assertEqual(code, 200)
        self.assertTrue(body["container_gone"])
        self.assertIn("make room", body["reason"])
        self.assertFalse(eng.is_loaded())
        # already unloaded: a no-op, still reports the container state
        code, body, _ = self.h.call("POST", "/v1/music/unload", {"if_idle": True})
        self.assertEqual((code, body.get("noop"), body["container_gone"]), (200, True, True))
        # the Control Center's plain unload (no body) is unchanged
        eng.ensure_loaded()
        req = urllib.request.Request(self.h.base + "/v1/music/unload", method="POST",
                                     headers={"Authorization": f"Bearer {KEY}"})
        with urllib.request.urlopen(req, timeout=30) as r:
            self.assertEqual(r.status, 200)
        self.assertFalse(eng.is_loaded())

    def test_reserve_cannot_be_configured_below_30(self):
        with mock.patch.dict(os.environ, {"GX_GUARD_RESERVE_GIB": "20"}):
            with self.assertRaises(MusicError):
                config_mod._float("GX_GUARD_RESERVE_GIB", 30.0, 30.0, 120.0)
        self.assertIn('_float("GX_GUARD_RESERVE_GIB", 30.0, 30.0, 120.0)',
                      (Path(config_mod.__file__)).read_text())


class TagIndexTests(unittest.TestCase):
    def test_search(self):
        idx = TagIndex(Path(__file__).resolve().parents[1] / "gx_music" / "genres_vocab.txt")
        self.assertGreater(len(idx.sorted), 1000)
        res = idx.search("bossa", 5)
        self.assertTrue(res and all("bossa" in r.lower() for r in res))
        self.assertEqual(idx.search("", 5), [])


class StoreTests(unittest.TestCase):
    def test_schema_idempotent_and_events_bounded(self):
        with tempfile.TemporaryDirectory() as d:
            s = st.Store(Path(d) / "x.db")
            st.Store(Path(d) / "x.db")
            for i in range(20):
                s.event("e", i=i)
            self.assertEqual(s.events(5)[0]["i"], 19)


if __name__ == "__main__":
    unittest.main()
