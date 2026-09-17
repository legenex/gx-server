"""Hermetic backend for the browser E2E suite.

Runs the REAL control-UI server code with a temporary password store and
synthetic cluster readings (no SSH, no docker, no cluster calls). Upstream
LiteLLM / media / orchestrator calls go to an in-process stub.

    GX_E2E_PASSWORD=... python3 e2e/fixture_server.py 18089
"""

from __future__ import annotations

import base64
import os
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "tests"))

from support import StubUpstream, TempEnv  # noqa: E402

from gx_control_ui import auth  # noqa: E402
from gx_control_ui import server as srv  # noqa: E402

GIB = 2**30


def _png(width: int = 64, height: int = 64) -> str:
    """A valid RGB PNG (gradient), built with zlib so the browser can render it."""
    import struct
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + b"".join(bytes((x * 4 % 256, y * 4 % 256, 128)) for x in range(width))
                   for y in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
    return base64.b64encode(png).decode()


PNG = _png(256, 256)


def node_facts(role: str, avail: float) -> dict:
    return {
        "role": role, "reachable": True, "hostname": "gx10-01" if role == "node1" else "gx10-02",
        "kernel": "6.17.0-1032-nvidia", "kernel_ok": True, "collected_at": time.time(),
        "uptime_seconds": 120000,
        "memory": {"MemTotal": 121.6 * GIB, "MemAvailable": avail * GIB, "MemFree": 80 * GIB,
                   "Cached": 20 * GIB, "SwapTotal": 64 * GIB, "SwapFree": 61 * GIB, "SwapCached": 0,
                   "Shmem": 0.5 * GIB, "Mlocked": 3 * GIB},
        "swaps": [{"name": "/swapfile-sglang", "type": "file", "size": 48 * GIB, "used": 0.2 * GIB, "priority": "-3"},
                  {"name": "/swap.img", "type": "file", "size": 16 * GIB, "used": 2.8 * GIB, "priority": "-2"}],
        "psi": {k: {"some": {"avg10": 0.0, "avg60": 0.0, "avg300": 0.0},
                    "full": {"avg10": 0.0, "avg60": 0.0, "avg300": 0.0}} for k in ("memory", "io", "cpu")},
        "load": {"load1": 0.2, "load5": 0.2, "load15": 0.2, "nproc": 20},
        "temperature": {"zones": [], "cpu_max_c": 47.5,
                        "gpu": {"name": "NVIDIA GB10", "celsius": 46, "util_pct": 3, "power_w": 11}},
        "rdma": [
            {"device": "rocep1s0f0", "netdev": "enp1s0f0np0", "state": "4: ACTIVE", "phys_state": "5: LinkUp",
             "rate": "200 Gb/sec (2X NDR)", "xmit_bytes": int(time.time() * 1000), "rcv_bytes": 5 * GIB},
            {"device": "roceP2p1s0f0", "netdev": "enP2p1s0f0np0", "state": "4: ACTIVE", "phys_state": "5: LinkUp",
             "rate": "200 Gb/sec (2X NDR)", "xmit_bytes": int(time.time() * 900), "rcv_bytes": 4 * GIB},
        ],
        "interfaces": [{"name": "enp1s0f0np0", "state": "UP", "mtu": 9000, "addrs": ["192.168.100.10/24"]}],
        "tailscale": {"ok": True, "backend": "Running", "ips": ["100.105.214.61"],
                      "peers": [{"host": "gx10-02", "online": True, "direct": True}]},
        "docker": {"ok": True, "containers": [
            {"name": "gx-mini", "image": "legenex/llama-cpp-spark:latest", "state": "running", "status": "Up 4 hours"},
            {"name": "gx-litellm", "image": "ghcr.io/berriai/litellm:main-stable", "state": "running",
             "status": "Up 4 hours (healthy)"},
        ] if role == "node1" else [
            {"name": "gx-media-router", "image": "gx-media-router:1.0.0", "state": "running", "status": "Up"},
        ]},
        "docker_stats": {"gx-mini": {"cpu": "0.1%", "mem": "1.2GiB / 14GiB"}},
        "units": [{"unit": "gx-hostwatch.timer", "active": "active", "sub": "waiting", "enabled": "enabled"},
                  {"unit": "gx-git-watch.service" if role == "node1" else "gx-git-reconcile.timer",
                   "active": "active", "sub": "running" if role == "node1" else "waiting", "enabled": "enabled"}],
        "git": {"ok": True, "head": "a" * 40, "branch": "main", "subject": "autosync(gx10-01): e2e",
                "date": "2026-09-16T12:00:00+02:00", "dirty_files": 0,
                "push_url": "https://github.com/legenex/gx-server.git" if role == "node1"
                else "DISABLED-gx10-02-is-pull-only"},
        "hostwatch": {"ok": True, "status": "ok", "detail": "ok=5 warn=0 crit=0", "age_seconds": 20,
                      "checks": {"memory_available": {"level": "OK", "status": "ok", "detail": "MemAvailable=106GiB"}}},
        "guard_lock": "free",
        "gxmax_watcher": {"present": False, "alive": False},
        "ssh_ms": 900,
    }


KEYS: dict = {}


def key_generate(handler, body):
    token = f"{len(KEYS) + 1:064x}"
    KEYS[token] = {"token": token, "key_alias": body["key_alias"], "key_name": "sk-...e2e0",
                   "models": body["models"], "metadata": body.get("metadata") or {}, "expires": None,
                   "created_at": "2026-09-17T00:00:00Z", "last_active": None}
    return 200, {"key": "sk-e2e-" + "x" * 30, "token": token, "expires": None}


def key_delete(handler, body):
    for k in body.get("keys", []):
        KEYS.pop(k, None)
    return 200, {"deleted_keys": body.get("keys", [])}


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18089
    password = os.environ["GX_E2E_PASSWORD"]
    KEYS.clear()
    KEYS["e" * 64] = {"token": "e" * 64, "key_alias": "kilo-code", "key_name": "sk-...kilo",
                      "models": ["gx-mini", "gx-fast"], "metadata": {}, "expires": None,
                      "created_at": "2026-09-16T20:41:59Z", "last_active": "2026-09-16T21:54:17Z"}
    stub = StubUpstream({
        ("GET", "/key/list"): lambda h, b: (200, {"keys": list(KEYS.values())}),
        ("POST", "/key/generate"): key_generate,
        ("POST", "/key/delete"): key_delete,
        ("GET", "/v1/models"): (200, {"data": [{"id": "gx-mini"}, {"id": "gx-fast"}]}),
        ("POST", "/v1/chat/completions"): (200, {
            "id": "e2e", "model": "gx-mini",
            "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
            "choices": [{"message": {"role": "assistant", "content": "391"}}]}),
        ("POST", "/v1/images/generations"): (200, {"created": 1, "data": [{"b64_json": PNG}],
                                                    "gx": {"workflow": "e2e", "seed": 1, "elapsed_seconds": 1,
                                                           "node": "gx10-02"}}),
        ("POST", "/v1/videos"): (202, {"id": "e2e-video-1", "status": "queued"}),
        ("GET", "/v1/videos/e2e-video-1"): (200, {"id": "e2e-video-1", "status": "completed"}),
        ("GET", "/v1/videos/e2e-video-1/content"): (200, b"\x00\x00\x00\x18ftypmp42"),
    })
    env = TempEnv(litellm_base=stub.url, media_base=stub.url, port=port)
    auth.PasswordStore(env.cfg.password_file).set_password("admin", password, n=2**12)
    app, servers = srv.build(env.cfg)
    cl = app.cluster
    history = [{"kind": "acquire", "started": time.time() - 4000, "ended": time.time() - 3450,
                "elapsed_seconds": 550, "startup_seconds": 512, "outcome": "ready",
                "phases": [{"phase": p} for p in ("preflight", "draining", "admission", "loading_rank1",
                                                  "loading_rank0", "warming", "ready", "serving")]}]
    cl.node1._fn = lambda: node_facts("node1", 106.4)
    cl.node2._fn = lambda: node_facts("node2", 112.8)
    cl.remote_git._fn = lambda: {"ok": True, "head": "a" * 40, "checked_at": time.time()}
    cl.lifecycle._fn = lambda: {
        "status": {"ok": True, "body": {"state": "down", "phase": "idle", "waiters": 0, "seconds_in_state": 60,
                                        "last_startup_seconds": 512, "idle_ttl": 1800, "detail": "released",
                                        "last_error": ""}},
        "events": {"ok": True, "body": {"seq": 2, "active_job": None, "history": history, "events": [
            {"seq": 1, "ts": time.time() - 3500, "source": "acquire", "line": "=== starting rank1 on node2 ==="},
            {"seq": 2, "ts": time.time() - 3450, "source": "acquire", "line": "=== gx-max READY on x after 512s ==="},
        ]}},
    }
    cl.services._fn = lambda: {
        "orchestrator": {"ok": True, "ms": 2, "body": {"tiers": {
            "gx-mini": {"state": "ready", "usable": True, "reason": "loaded"},
            "gx-fast": {"state": "stopped", "usable": True, "reason": "unloaded"},
            "gx-reason": {"state": "stopped", "usable": True, "reason": "unloaded"},
            "gx-max": {"state": "stopped", "usable": True, "reason": "stopped"}}}},
        "litellm_live": {"ok": True, "ms": 3}, "litellm_ready": {"ok": True, "ms": 5},
        "swap_node1": {"ok": True, "ms": 2, "body": {"data": [{"id": "gx-mini", "status": {"value": "loaded"}},
                                                              {"id": "gx-fast", "status": {"value": "unloaded"}}]}},
        "swap_node1_running": {"ok": True, "body": {"running": [{"model": "gx-mini", "state": "ready", "ttl": 0}]}},
        "swap_node2": {"ok": True, "ms": 3, "body": {"data": [{"id": "gx-reason", "status": {"value": "unloaded"}}]}},
        "swap_node2_running": {"ok": True, "body": {"running": []}},
        "media": {"ok": True, "ms": 4, "body": {"status": "ok", "busy": False, "video_queue_depth": 0,
                                                "workflows": ["qwen-image-2512-lightning"],
                                                "comfyui": {"reachable": True, "comfyui_version": "0.35.0",
                                                            "vram_free_bytes": 80 * GIB, "device": "cuda:0",
                                                            "queue_depth": 0}}},
        "sglang": {"ok": False, "status": 0},
    }
    app._fabric_cache = {"at": time.time() + 10**9, "192.168.100.11": "open", "192.168.101.11": "open"}
    # Model Manager: synthetic inventory (no SSH, no /srv/models).
    app.manager._node1_inventory = lambda: {
        "disk": {"total": 900 * GIB, "free": 160 * GIB, "used": 740 * GIB}, "manifests": [],
        "dirs": [{"category": "gguf", "name": "Qwen3.5-4B-Uncensored-HauhauCS-Aggressive",
                  "path": "/srv/models/gguf/Qwen3.5-4B-Uncensored-HauhauCS-Aggressive", "size": 3 * GIB,
                  "manifest": {"repository": "HauhauCS/Qwen3.5-4B-Uncensored-HauhauCS-Aggressive",
                               "revision": "c09cdbcdb1fefad6d335809d445621b5f5ba0c6e"}},
                 {"category": "staging", "name": "unused-e2e", "path": "/srv/models/staging/unused-e2e",
                  "size": 20 * 2**20, "manifest": None}]}
    app.manager._node2_inventory = lambda: {"disk": {"total": 900 * GIB, "free": 56 * GIB, "used": 844 * GIB},
                                            "dirs": [], "manifests": []}
    app.manager._start = lambda kind, label, user, params, fn: {"id": "0" * 16, "label": label, "state": "running"}
    # Media library: two seeded images.
    from gx_control_ui.media_library import NewAsset  # noqa: PLC0415
    big = base64.b64decode(_png(256, 256))
    for i, prompt in enumerate(("e2e seeded lighthouse", "e2e seeded bicycle")):
        app.library.add(NewAsset(type="image", ext="png", operation="generate", data=big, prompt=prompt,
                                 model_alias="gx-image", seed=i, title=f"Seed {i}"))
    for s in servers:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    print(f"e2e fixture server on http://127.0.0.1:{port}", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        stub.close()
        env.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
