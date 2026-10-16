"""REAL integration test: the actual LiteLLM proxy image + gx_budget_hook forward
the X-GX-* headers to the upstream, and nothing a client tries to inject.

Starts a throwaway LiteLLM container (the SAME image the live gx-litellm runs,
resolved from `docker inspect gx-litellm`, or $GX_LITELLM_IMAGE) on a private
loopback port with a test config and this repo's hook, pointed at a stub
upstream that records the headers it receives. No database, no live service
touched, the container is removed afterwards.

    GX_LITELLM_IT=1 python3 -m unittest discover -s legenex/gateway/litellm/tests -p 'test_it_*.py' -v

Skipped unless GX_LITELLM_IT=1 (it needs docker and ~1 minute).
"""
from __future__ import annotations

import http.server
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

LITELLM_DIR = Path(__file__).resolve().parents[1]
ORCH = LITELLM_DIR.parents[1] / "orchestrator"
sys.path.insert(0, str(ORCH))

from gx_orchestrator.identity import identity_from_headers  # noqa: E402


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _image() -> str:
    if os.environ.get("GX_LITELLM_IMAGE"):
        return os.environ["GX_LITELLM_IMAGE"]
    p = subprocess.run(["docker", "inspect", "-f", "{{.Image}}", "gx-litellm"], capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 and p.stdout.strip() else "ghcr.io/berriai/litellm:main-stable"


class _Upstream(http.server.BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *a):
        pass

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        type(self).seen.append(({k: v for k, v in self.headers.items()}, body))
        data = json.dumps({"id": "c1", "object": "chat.completion", "created": int(time.time()), "model": body.get("model"),
                           "choices": [{"index": 0, "finish_reason": "stop",
                                        "message": {"role": "assistant", "content": "323"}}],
                           "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@unittest.skipUnless(os.environ.get("GX_LITELLM_IT") == "1", "set GX_LITELLM_IT=1 (needs docker)")
class TestRealLiteLLMForwarding(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="gx-litellm-it-")
        os.chmod(cls.tmp, 0o755)
        _Upstream.seen = []
        cls.up = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
        threading.Thread(target=cls.up.serve_forever, daemon=True).start()
        cls.port = _free_port()
        cls.master = "sk-gx-it-" + secrets.token_hex(12)
        cls.idkey = secrets.token_hex(24)
        cfg = {
            "general_settings": {"master_key": cls.master, "background_health_checks": False},
            "model_list": [{"model_name": "gx-fast", "litellm_params": {
                "model": "openai/gx-fast", "api_base": f"http://127.0.0.1:{cls.up.server_address[1]}/v1",
                "api_key": "stub-upstream-key", "num_retries": 0}}],
            "litellm_settings": {"callbacks": ["gx_hooks.gx_budget_hook.proxy_handler_instance"],
                                 "num_retries": 0, "drop_params": True},
        }
        (Path(cls.tmp) / "config.yaml").write_text(json.dumps(cfg))   # JSON is valid YAML
        os.chmod(Path(cls.tmp) / "config.yaml", 0o644)
        cls.name = "gx-litellm-it-" + secrets.token_hex(4)
        cls.image = _image()
        subprocess.run([
            "docker", "run", "-d", "--rm", "--name", cls.name, "--network", "host",
            "-v", f"{cls.tmp}/config.yaml:/app/config.yaml:ro",
            "-v", f"{LITELLM_DIR / 'gx_hooks'}:/app/gx_hooks:ro",
            "-v", f"{ORCH / 'gx_orchestrator'}:/app/gx_lib/gx_orchestrator:ro",
            "-e", f"GX_IDENTITY_HMAC_KEY={cls.idkey}", "-e", "GX_TEXT_METRICS_LOG=/tmp/gx-text.jsonl",
            cls.image, "--config", "/app/config.yaml", "--host", "127.0.0.1", "--port", str(cls.port),
            "--num_workers", "1"], check=True, capture_output=True)
        deadline = time.time() + 180
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{cls.port}/health/liveliness", timeout=3):
                    return
            except (OSError, urllib.error.URLError):
                time.sleep(2)
        logs = subprocess.run(["docker", "logs", "--tail", "40", cls.name], capture_output=True, text=True)
        cls.tearDownClass()
        raise RuntimeError("litellm did not come up:\n" + logs.stdout + logs.stderr)

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["docker", "rm", "-f", cls.name], capture_output=True)
        cls.up.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def call(self, headers, extra_body=None):
        body = {"model": "gx-fast", "messages": [{"role": "user", "content": "17*19="}], **(extra_body or {})}
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/v1/chat/completions",
                                     data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {self.master}", **headers})
        with urllib.request.urlopen(req, timeout=60) as resp:
            self.assertEqual(resp.status, 200)
        return {k.lower(): v for k, v in _Upstream.seen[-1][0].items()}

    def test_x_gx_headers_arrive_and_spoofs_are_replaced(self):
        got = self.call({
            "X-GX-Project": "it-project", "X-GX-Agent": "it-agent", "X-GX-Task": "t-1",
            "X-GX-Intent": "interactive", "X-GX-Priority": "background", "X-GX-Profile": "fast",
            "X-GX-Reasoning": "none", "X-GX-Switch-Validation": "tok-it",
            "X-GX-Identity": "odin", "X-GX-Identity-Sig": "forged", "X-Evil": "1",
        }, extra_body={"extra_headers": {"X-GX-Identity": "odin", "X-Injected": "1"},
                       "headers": {"X-GX-Priority": "health"}})
        for k, v in (("x-gx-project", "it-project"), ("x-gx-agent", "it-agent"), ("x-gx-task", "t-1"),
                     ("x-gx-intent", "interactive"), ("x-gx-priority", "background"), ("x-gx-profile", "fast"),
                     ("x-gx-reasoning", "none"), ("x-gx-switch-validation", "tok-it")):
            self.assertEqual(got.get(k), v, k)
        self.assertNotEqual(got.get("x-gx-identity"), "odin")
        self.assertTrue(got.get("x-gx-identity"))
        hdrs = {"X-GX-Identity": got["x-gx-identity"], "X-GX-Identity-Ts": got.get("x-gx-identity-ts"),
                "X-GX-Identity-Nonce": got.get("x-gx-identity-nonce"), "X-GX-Identity-Sig": got.get("x-gx-identity-sig")}
        self.assertEqual(identity_from_headers(hdrs, self.idkey), got["x-gx-identity"])
        for leaked in ("x-evil", "x-injected"):
            self.assertNotIn(leaked, got)
        # The upstream only ever sees the deployment's own key, never the client's.
        self.assertEqual(got.get("authorization"), "Bearer stub-upstream-key")

    def test_a_plain_request_still_carries_a_signed_identity(self):
        got = self.call({})
        self.assertTrue(got.get("x-gx-identity"))
        self.assertNotIn("x-gx-project", got)


if __name__ == "__main__":
    unittest.main()
