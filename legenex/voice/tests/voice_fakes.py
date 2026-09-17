"""Hermetic fakes for gx-voice: a stub engine, a fake Docker, a fake guard and a
fully wired supervisor ("Rig"). Used by this package's tests and by the
Control Center / GX-Playground hermetic suites (control-ui/e2e/voice_stub.py),
which therefore talk to the REAL gx-voice HTTP API and service.

The stub engine writes real, playable 24 kHz sine-wave WAVs whose length
follows the text; the fake Docker emulates `docker run/exec/inspect` and the
ffmpeg/ffprobe helper (WAV copies, tagged bytes for compressed formats).
Nothing here needs a GPU, a Docker daemon or a model.
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
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gx_voice import audio as au  # noqa: E402
from gx_voice import config as config_mod  # noqa: E402
from gx_voice import store as st  # noqa: E402
from gx_voice.engine import EngineController  # noqa: E402
from gx_voice.errors import ResourceWait  # noqa: E402
from gx_voice.server import build_servers  # noqa: E402
from gx_voice.service import VoiceService  # noqa: E402

KEY = "k" * 40


def sine_pcm(seconds: float, rate: int = 24000, freq: float = 220.0, amp: float = 0.3) -> bytes:
    n = int(seconds * rate)
    return b"".join(struct.pack("<h", int(amp * 32767 * math.sin(2 * math.pi * freq * i / rate))) for i in range(n))


def write_wav(path: Path, seconds: float, amp: float = 0.3) -> None:
    au.write_pcm16(path, 24000, sine_pcm(seconds, amp=amp))


def free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ------------------------------------------------------------------ fakes --
class StubEngine:
    """The engine's loopback API with deterministic fake synthesis."""

    def __init__(self, port: int, work: Path, key_fn) -> None:  # noqa: ANN001
        self.calls: list[dict] = []
        self.loaded: list[str] = []
        self.fail_next: tuple[int, dict] | None = None
        self.seconds_per_char = 0.02
        self.silent = False
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # noqa: ANN002
                return

            def _json(self, status, payload):  # noqa: ANN001
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                if self.headers.get("Authorization") != f"Bearer {key_fn()}":
                    return self._json(401, {"error": {"code": "unauthorized"}})
                self._json(200, {"ready": True, "loaded": outer.loaded})

            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if outer.fail_next:
                    status, payload = outer.fail_next
                    outer.fail_next = None
                    return self._json(status, payload)
                if self.path == "/variants/load":
                    outer.loaded = [body["variant"]]
                    return self._json(200, {"loaded": outer.loaded, "seconds": 0.1})
                outer.calls.append(body)
                load_s = 0.0
                if body["variant"] not in outer.loaded:
                    outer.loaded = [body["variant"]]
                    load_s = 0.5
                secs = max(0.3, outer.seconds_per_char * len(body["text"]))
                write_wav(work / body["out"], secs, amp=0.0 if outer.silent else 0.3)
                self._json(200, {"sample_rate": 24000, "frames": int(secs * 24000), "seconds": secs,
                                 "generate_s": secs / 2, "variant_load_s": load_s, "variant": body["variant"]})

        self.server = ThreadingHTTPServer(("127.0.0.1", port), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class FakeDocker:
    def __init__(self, data_root: Path) -> None:
        self.data_root = data_root
        self.running_set: set[str] = {"gx-llama-swap-node02"}
        self.existing: set[str] = set()
        self.calls: list[list[str]] = []
        self.free_node_reply = {"freed": False, "reason": "busy"}

    def _cp(self, rc=0, out="", err=""):  # noqa: ANN001
        return subprocess.CompletedProcess([], rc, out, err)

    def _host(self, p: str) -> Path:
        return self.data_root / p[len("/work/data/"):]

    def _tool(self, tool: str, args: list[str]):  # noqa: ANN201
        if tool == "ffprobe":
            path = self._host(args[-1])
            if au.sniff(path.read_bytes()[:12]) != "wav":
                return self._cp(1, "", "invalid data")
            info = au.analyze_wav(path)
            return self._cp(0, json.dumps({"streams": [{"codec_type": "audio", "sample_rate": str(info.sample_rate),
                                                         "channels": info.channels, "codec_name": "pcm_s16le"}],
                                           "format": {"duration": str(info.duration_s)}}))
        src = self._host(args[args.index("-i") + 1])
        dst = self._host(args[-1])
        if dst.suffix == ".wav":
            shutil.copyfile(src, dst)
        else:
            dst.write_bytes(b"ID3fake-" + dst.suffix.encode() + src.read_bytes()[:64])
        return self._cp(0)

    def run(self, args, timeout=120, stdin=None):  # noqa: ANN001, ANN201
        self.calls.append(list(args))
        cmd = args[0]
        if cmd == "inspect":
            name = args[-1]
            if "{{.State.Running}}" in args:
                return self._cp(0, "true\n") if name in self.running_set else self._cp(1)
            return self._cp(0, "id\n") if name in self.existing | self.running_set else self._cp(1)
        if cmd == "run" and "-d" in args:
            name = args[args.index("--name") + 1]
            self.running_set.add(name)
            self.existing.add(name)
            return self._cp(0, "cid\n")
        if cmd == "run" and "--entrypoint" in args:
            i = args.index("--entrypoint")
            tool = args[i + 1]
            rest = args[i + 2:]
            return self._tool(tool, rest[rest.index("-v") + 3:] if "-v" in rest else rest[1:])
        if cmd == "exec":
            if args[1] == "gx-media-router":
                return self._cp(0, json.dumps(self.free_node_reply) + "\n")
            return self._tool(args[2], args[3:])
        if cmd in ("stop", "rm"):
            name = args[-1]
            self.running_set.discard(name)
            if cmd == "rm":
                self.existing.discard(name)
            return self._cp(0)
        if cmd == "logs":
            return self._cp(0, "log tail")
        return self._cp(1, "", f"unexpected docker call {args}")

    def running(self, name: str) -> bool:
        return name in self.running_set

    def exists(self, name: str) -> bool:
        return name in self.running_set or name in self.existing


class FakeGuard:
    def __init__(self) -> None:
        self.refuse: ResourceWait | None = None
        self.launches: list[float] = []
        self.released = 0
        self.registered = 0
        self.on_admitted = None

    def launch(self, start, extra_gib=0.0):  # noqa: ANN001
        self.launches.append(extra_gib)
        if self.refuse:
            raise self.refuse
        if self.on_admitted:
            self.on_admitted()
        start()

    def register(self) -> None:
        self.registered += 1

    def release(self) -> None:
        self.released += 1


def make_cfg(tmp: Path, env_over: dict | None = None, key: str = KEY, **over) -> config_mod.Config:
    (tmp / "secrets").mkdir(exist_ok=True)
    (tmp / "secrets" / "api-key").write_text(key)
    env = {
        "GX_VOICE_SECRETS_DIR": str(tmp / "secrets"), "GX_VOICE_DATA_ROOT": str(tmp / "data"),
        "GX_VOICE_MODELS_DIR": str(tmp / "models"), "GX_VOICE_STATE_DIR": str(tmp / "state"),
        "GX_GUARD_STATE_DIR": str(tmp / "guard"), "GX_VOICE_ENGINE_PORT": str(free_port()),
        "GX_VOICE_PEER_HEALTH": "", "GX_VOICE_GXMAX_HOLD": str(tmp / "guard" / "node2.gxmax-hold"),
        "GX_VOICE_MAINTENANCE_HOLD": str(tmp / "guard" / "node2.maintenance-hold"),
        "GX_VOICE_PINS_FILE": str(tmp / "guard" / "pins.json"),
        "GX_VOICE_PROFILE_FILE": str(tmp / "guard" / "profile.json"),
        "GX_VOICE_GXMAX_DEADMAN_PID": str(tmp / "deadman.pid"),
        "GX_VOICE_MUSIC_URL": "http://127.0.0.1:9", "GX_VOICE_MUSIC_KEY_FILE": str(tmp / "nokey"),
        "GX_VOICE_BINDS": "127.0.0.1", "GX_VOICE_PORT": str(free_port()),
    }
    env.update(env_over or {})
    (tmp / "guard").mkdir(exist_ok=True)
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        cfg = config_mod.load()
    finally:
        for k, val in old.items():
            if val is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = val
    return dataclasses.replace(cfg, **over)


class Rig:
    """A supervisor wired to the stub engine, fake docker and fake guard."""

    def __init__(self, tmp: Path, key: str = KEY, **over) -> None:
        self.cfg = make_cfg(tmp, key=key, **over)
        self.docker = FakeDocker(self.cfg.data_root)
        self.guard = FakeGuard()
        self.cfg.data_root.mkdir(parents=True, exist_ok=True)
        self.engine = EngineController(self.cfg, docker=self.docker, guard=self.guard, sleep=lambda s: None)
        self.stub = StubEngine(self.cfg.engine_port, self.cfg.data_root, lambda: self.engine._key)  # noqa: SLF001
        self.store = st.Store(self.cfg.db_path)
        self.service = VoiceService(self.cfg, self.store, self.engine)

    def ref(self, seconds: float = 4.0) -> str:
        p = self.cfg.data_root / "src.wav"
        write_wav(p, seconds)
        return self.service.add_reference(p.read_bytes(), "voice.wav")["id"]

    def run_next(self) -> dict:
        job = self.store.next_queued()
        self.service._current = job["id"]  # noqa: SLF001
        try:
            self.service._run(job)  # noqa: SLF001
        except Exception as exc:  # noqa: BLE001
            self.service._fail(job["id"], exc)  # noqa: SLF001
        finally:
            self.service._current = None  # noqa: SLF001
        return self.service.job_view(job["id"])

    def close(self) -> None:
        self.stub.close()


def serve(rig: Rig) -> tuple[list, str]:
    """Start the rig's worker and its HTTP API on 127.0.0.1; returns (servers, base URL)."""
    rig.service.start()
    servers = build_servers(rig.service, rig.cfg.api_key, ("127.0.0.1",), rig.cfg.port)
    for s in servers:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    return servers, f"http://127.0.0.1:{rig.cfg.port}"


def stop(rig: Rig, servers: list) -> None:
    rig.service.stop()
    for s in servers:
        s.shutdown()
        s.server_close()
    rig.close()
