"""Regression tests for specific findings from the 2026-09-14 security review.

Each test documents the finding it guards against so a future change cannot
silently reopen the gap. Standard library only; no GPU, no ComfyUI, no node 2.

Run:  python3 -m unittest discover -s tests -v      (from legenex/media/router)
"""

from __future__ import annotations

import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gx_media_router.config import Config  # noqa: E402
from gx_media_router.server import build_server  # noqa: E402
from gx_media_router.service import MediaService  # noqa: E402
from gx_media_router.workflows import WorkflowRegistry  # noqa: E402

from tests.test_router import FakeComfy  # noqa: E402

WORKFLOW_DIR = Path(__file__).resolve().parents[2] / "workflows"


class ConfigAuthTests(unittest.TestCase):
    """Finding: GX_MEDIA_API_KEY carried "magic string" values that silently
    disabled authentication (gx_media_router/config.py, previously line 61),
    exactly matching the placeholder shipped in legenex/gateway/.env.sample
    (`GX_MEDIA_API_KEY=not-required`). That placeholder is non-empty, so it
    also satisfied the docker-compose `${GX_MEDIA_API_KEY:?...}` fail-closed
    guard, meaning a deployment could copy the sample as-is and end up with
    the router fully unauthenticated on the fabric address with no error or
    guard tripped anywhere.
    """

    def _cfg_with(self, value: str | None) -> Config:
        env = dict(os.environ)
        if value is None:
            env.pop("GX_MEDIA_API_KEY", None)
        else:
            env["GX_MEDIA_API_KEY"] = value
        old = os.environ.copy()
        os.environ.clear()
        os.environ.update(env)
        try:
            return Config.from_env()
        finally:
            os.environ.clear()
            os.environ.update(old)

    def test_the_gateway_sample_placeholder_no_longer_disables_auth(self):
        cfg = self._cfg_with("not-required")
        self.assertEqual(cfg.api_key, "not-required", "must be treated as a literal secret")

    def test_other_former_magic_strings_are_also_literal_secrets_now(self):
        for value in ("none", "disabled"):
            with self.subTest(value=value):
                cfg = self._cfg_with(value)
                self.assertEqual(cfg.api_key, value)

    def test_auth_is_still_disableable_by_leaving_the_key_empty(self):
        self.assertEqual(self._cfg_with(None).api_key, "")
        self.assertEqual(self._cfg_with("").api_key, "")


class CrossKindWorkflowTests(unittest.TestCase):
    """Finding: POST /v1/images/generations accepted a `workflow` name that
    named a *video* template (and /v1/videos accepted an *image* template).
    No graph/node injection resulted (the name still only selects a vetted,
    already-loaded template), but it let a caller force the wrong param set,
    the wrong timeout budget (image_timeout_seconds vs video_timeout_seconds)
    and a mislabelled response through the synchronous image path. Server.py
    now checks ``workflow.kind`` against the endpoint via ``_named_workflow``.
    """

    @classmethod
    def setUpClass(cls):
        cls.cfg = Config(bind_host="127.0.0.1", bind_port=0, api_key="test-key",
                          workflow_dir=WORKFLOW_DIR, queue_wait_seconds=2)
        cls.comfy = FakeComfy()
        cls.service = MediaService(cls.cfg, cls.comfy, WorkflowRegistry(WORKFLOW_DIR))
        cls.server = build_server(cls.cfg, cls.service)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def call(self, method, path, body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Authorization", "Bearer test-key")
        if data:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def test_a_video_workflow_is_rejected_by_the_image_endpoint(self):
        status, body = self.call(
            "POST", "/v1/images/generations",
            {"prompt": "x", "workflow": "wan22-t2v-a14b-lightning"},
        )
        self.assertEqual(status, 400)
        payload = json.loads(body)
        self.assertEqual(payload["error"]["param"], "workflow")
        self.assertIn("not 'image'", payload["error"]["message"])

    def test_an_image_workflow_is_rejected_by_the_video_endpoint(self):
        status, body = self.call(
            "POST", "/v1/videos",
            {"prompt": "x", "workflow": "qwen-image-2512-lightning"},
        )
        self.assertEqual(status, 400)
        payload = json.loads(body)
        self.assertEqual(payload["error"]["param"], "workflow")
        self.assertIn("not 'video'", payload["error"]["message"])

    def test_the_matching_kind_still_works(self):
        status, _ = self.call(
            "POST", "/v1/images/generations",
            {"prompt": "x", "workflow": "qwen-image-2512-quality"},
        )
        self.assertEqual(status, 200)


class ContentDispositionSanitizationTests(unittest.TestCase):
    """Finding: Content-Disposition's filename was interpolated straight from
    ComfyUI's /history response with no CR/LF/quote stripping, and Python's
    http.server.send_header does not sanitize header values itself. ComfyUI is
    a trusted, same-host, loopback-only component today (D-011), so this was
    not reachable from the network -- but it is cheap, stdlib, defense-in-depth
    to strip control characters before they ever reach a response header.
    """

    @classmethod
    def setUpClass(cls):
        cls.cfg = Config(bind_host="127.0.0.1", bind_port=0, api_key="test-key",
                          workflow_dir=WORKFLOW_DIR, queue_wait_seconds=2)
        cls.comfy = FakeComfy()
        cls.service = MediaService(cls.cfg, cls.comfy, WorkflowRegistry(WORKFLOW_DIR))
        cls.server = build_server(cls.cfg, cls.service)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_a_crlf_laced_filename_cannot_inject_a_header(self):
        from gx_media_router.comfy import Artefact

        job = self.service.generate_image(
            "qwen-image-2512-lightning", {"prompt": "x"}
        )
        # Splice in a malicious artefact as if ComfyUI had reported one.
        job.artefacts = (
            Artefact(
                filename='evil.png"\r\nX-Injected: yes\r\nContent-Type: text/html',
                subfolder="gx-image", type="output", kind="images",
            ),
        )
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/images/{job.id}/content/0"
        )
        request.add_header("Authorization", "Bearer test-key")
        with urllib.request.urlopen(request, timeout=10) as response:
            headers = dict(response.headers)
        self.assertNotIn("X-Injected", headers)
        self.assertNotIn("\r", headers["Content-Disposition"])
        self.assertNotIn("\n", headers["Content-Disposition"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
