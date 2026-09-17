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
from gx_music.engine import READY, UNLOADED, EngineController  # noqa: E402
from gx_music.errors import ConflictError, EngineError, NotFoundError, ResourceWait, ValidationError  # noqa: E402
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
        self.assertEqual(r.engine["prompt"], "dreamy pop, synth")
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

    def test_description_mode(self):
        r = v.generation({"description": "a soft bossa nova about rain"}, TURBO, max_duration=600)
        self.assertTrue(r.engine["sample_mode"])
        self.assertEqual(r.engine["sample_query"], "a soft bossa nova about rain")

    def test_description_mode_rejects_overridden_controls(self):
        for extra in ({"duration": 40}, {"bpm": 90}, {"prompt": "x"}, {"style_tags": ["rock"]}):
            with self.subTest(extra=extra), self.assertRaises(ValidationError):
                v.generation({"description": "a song", **extra}, TURBO, max_duration=600)

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

    def _cp(self, args, rc=0, out="", err=""):
        return subprocess.CompletedProcess(args, rc, out, err)

    def run(self, args, timeout=120):
        self.calls.append(args)
        if args[0] == "run" and "--entrypoint" in args:
            tool = args[args.index("--entrypoint") + 1]
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
    def __init__(self):
        self.refuse = 0
        self.registered = False
        self.launches = 0

    def launch(self, start):
        if self.refuse:
            self.refuse -= 1
            raise ResourceWait("waiting for memory on the media node (other models are using it)")
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
        media_router_container="gx-media-router", gxmax_hold_file=root / "guard" / "node2.gxmax-hold",
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
        self.assertEqual(sent["prompt"], "folk song")  # inherited
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
