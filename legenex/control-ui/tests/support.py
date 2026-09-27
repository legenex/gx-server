"""Hermetic test helpers: temp dirs, offline config, stub upstream servers.

Nothing here touches the real cluster. Fake credentials are assembled at run
time so no credential-shaped literal exists in the repository (the autosync
secret gate would rightly refuse it).
"""

from __future__ import annotations

import http.server
import json
import os
import secrets
import sys
import tempfile
import threading
from pathlib import Path

UI_DIR = Path(__file__).resolve().parents[1]
REPO = UI_DIR.parents[1]
sys.path.insert(0, str(UI_DIR))
os.environ.setdefault("GX_UI_ACCESS_LOG", "0")

from gx_control_ui.config import UIConfig  # noqa: E402


def fake_key(prefix: str = "sk-") -> str:
    return prefix + secrets.token_hex(24)


class TempEnv:
    """Temp state/secret/log/guard/file roots plus a UIConfig pointing at them."""

    def __init__(self, **overrides) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.root = root
        for d in ("secrets", "state", "logs", "guard", "srvlogs", "files", "archive", "trash"):
            (root / d).mkdir()
        (root / "guard" / "node1-residency.json").write_text("{}")
        (root / "guard" / "node2-residency.json").write_text("{}")
        # A minimal registry so every module reads the same fixture world.
        (root / "registry.json").write_text(json.dumps(SAMPLE_REGISTRY))
        params = dict(
            hosts=("127.0.0.1",), port=0, repo_root=REPO, static_dir=UI_DIR / "web",
            docs_dir=UI_DIR / "docs", secret_dir=root / "secrets", state_dir=root / "state",
            log_dir=root / "logs", guard_dir=root / "guard", gx_state_root=root,
            srv_logs=root / "srvlogs", offline=True,
            hf_token_file=root / "secrets" / "hf-token",
            projects_root=root / "files" / "projects",
            file_roots=(str(root / "files" / "projects"), str(root / "files" / "backups"),
                        str(root / "files" / "archive"), str(root / "files" / "models"),
                        str(root / "files" / "cache"), str(root / "srvlogs")),
            file_protected=(str(root / "files" / "backups" / "GX"), str(root / "files" / "projects" / "gx-backup")),
            trash_root=root / "files" / "cache" / "trash",  # like /srv/cache/trash: inside a root
            watchdog_incidents=root / "state" / "watchdog" / "incidents.jsonl",
            registry_path=root / "registry.json",
            mia_dir=root / "mia-dsv41",      # absent: update pins show honest drift
            bench_dir=root / "bench",        # absent: benchmark_run refuses honestly
            orchestrator_base="http://127.0.0.1:9", litellm_base="http://127.0.0.1:9",
            mia_base="http://127.0.0.1:9", agentos_base="http://127.0.0.1:9",
            public_gateway_url="http://127.0.0.1:4000/v1",
            public_control_url="http://127.0.0.1:8088/",
        )
        for alias_dir in ("projects", "backups", "archive", "models", "cache"):
            (root / "files" / alias_dir).mkdir(exist_ok=True)
        params.update(overrides)
        self.cfg = UIConfig(**params)

    def cleanup(self) -> None:
        self._tmp.cleanup()


#: A registry v2 fixture matching ARCHITECTURE-V41.md section 2 (small but real).
SAMPLE_REGISTRY = {
    "schema": 2,
    "cluster": {"name": "test-cluster", "head": "gx10-01", "worker": "gx10-02"},
    "nodes": {
        "gx10-01": {"role": "head", "user": "legenex", "lan_ip": "10.60.21.37",
                    "tailscale_ip": "100.105.214.61",
                    "fabric": {"rail1": "192.168.100.10", "rail2": "192.168.101.10"},
                    "hcas": ["rocep1s0f0", "roceP2p1s0f0"], "ssh": "gx10-01"},
        "gx10-02": {"role": "worker", "user": "legenex-02", "lan_ip": "10.60.21.41",
                    "tailscale_ip": "100.73.238.4",
                    "fabric": {"rail1": "192.168.100.11", "rail2": "192.168.101.11"},
                    "hcas": ["rocep1s0f0", "roceP2p1s0f0"], "ssh": "10.60.21.41"},
    },
    "fabric": {"nccl": {"NCCL_IB_GID_INDEX": "3"}},
    "runtimes": {
        "mia-dsv41": {"kind": "mia-2x-gb10-exl3", "submodule": "mia-dsv41",
                      "commit": "6f7d1590ad49a2b8995188e45d7b9db31e677452",
                      "image": "ghcr.io/miaai-lab/test-image:2.9bpw",
                      "api": "http://127.0.0.1:8888/v1",
                      "served_model_id": "DeepSeek-v4.1-Flash-EXL3"},
    },
    "models": {
        "dsv41-flash-exl3-stock": {
            "source": "Mia-AiLab/DeepSeek-V4.1-Flash-EXL3-2.9bpw",
            "revision": "64ba41b6c916a587db06eae2e19b7845f7be6e6b",
            "path": "/tmp/gx-test/models/stock", "uncensored": False,
            "engram_dir": "/tmp/gx-test/models/engram", "quant": "exl3-2.9bpw-mul1",
            "vision": True, "tools": True, "max_context": 600000},
        "dsv41-flash-exl3-uncensored": {
            "source": "dealignai/DeepSeek-V4.1-Flash-UNCENSORED-EXL3-2.9bpw",
            "revision": "8a27b35fc5b145fa05ee965c7d7b243b047915f7",
            "path": "/tmp/gx-test/models/uncensored", "uncensored": True,
            "engram_dir": "/tmp/gx-test/models/engram", "quant": "exl3-2.9bpw-mul1",
            "vision": True, "tools": True, "max_context": 262144},
    },
    "aliases": {
        "gx-max": {"model": "dsv41-flash-exl3-uncensored", "runtime": "mia-dsv41",
                   "mode": "direct", "description": "Explicit DeepSeek V4.1 Flash (uncensored)"},
        "gx-auto": {"model": "dsv41-flash-exl3-uncensored", "runtime": "mia-dsv41",
                    "mode": "auto", "description": "Profile/reasoning auto-selection"},
    },
    "profiles": {
        "fast": {"max_num_seqs": 1, "spec_method": "dspark", "dspark_tokens": 3,
                 "max_model_len": 600000, "reasoning_default": "medium", "target": "single interactive"},
        "balanced": {"max_num_seqs": 2, "spec_method": "dspark", "dspark_tokens": 3,
                     "max_model_len": 600000, "reasoning_default": "medium", "target": "AgentOS default"},
        "swarm": {"max_num_seqs": 4, "spec_method": "none", "max_model_len": 262144,
                  "reasoning_default": "low", "target": "many logical agents"},
    },
    "reasoning": {"levels": ["none", "minimal", "low", "medium", "high", "xhigh", "max"],
                  "mapping": {"none": {"enable_thinking": False}},
                  "numeric_range": [1, 100]},
    "capabilities": {"vision": True, "tools": True, "structured_output": True, "reasoning": True},
}


class StubUpstream:
    """A tiny JSON HTTP server whose routes are plain dicts.

    routes[(method, path)] = (status, body) or a callable(handler, body) -> (status, body)
    Every request is recorded in .calls as (method, path, headers, body).
    """

    def __init__(self, routes: dict) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str, dict, object]] = []
        stub = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):  # noqa: A003
                pass

            def _handle(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw) if raw else None
                except ValueError:
                    body = raw
                path = self.path.split("?")[0]
                stub.calls.append((method, self.path, dict(self.headers), body))
                route = stub.routes.get((method, path))
                if route is None:
                    status, payload = 404, {"error": "no route"}
                elif callable(route):
                    status, payload = route(self, body)
                else:
                    status, payload = route
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json" if not isinstance(payload, bytes) else "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                self._handle("GET")

            def do_POST(self):  # noqa: N802
                self._handle("POST")

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class env_vars:
    """Context manager to set environment variables temporarily."""

    def __init__(self, **values) -> None:
        self.values = values
        self.saved: dict[str, str | None] = {}

    def __enter__(self):
        for k, v in self.values.items():
            self.saved[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
