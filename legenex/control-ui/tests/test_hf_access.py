"""Hugging Face access diagnosis (B-030).

A gated repository serves its METADATA and refuses its FILES until the account
has been granted access. Collapsing those two into one "access denied" message
is what sent an earlier pass round in circles minting tokens for a gate no
token can open. These tests pin the distinction and the human action.

Hermetic: a local HTTP server stands in for huggingface.co. No network, no
real token.
"""

from __future__ import annotations

import json
import secrets
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from support import UI_DIR  # noqa: F401  (puts gx_control_ui on sys.path)

from gx_control_ui import hf as hfmod
from gx_control_ui.hf import HFClient, HFError

TOKEN = "hf_" + secrets.token_hex(16)
SHA = "a" * 40
REPO = "acme/gated-model"


class FakeHub(BaseHTTPRequestHandler):
    """Behaves like the Hub: metadata 200, files 401 without a token and 403 with one."""

    #: set per test: "granted" | "not_granted" | "no_gate"
    mode = "not_granted"
    #: set per test: the fineGrained permission block
    gated_permission = True

    def log_message(self, *a):  # keep the test output clean
        pass

    def _send(self, code, payload=b"", headers=None):
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):  # noqa: N802
        authed = self.headers.get("Authorization") == f"Bearer {TOKEN}"
        if self.path.startswith("/api/whoami-v2"):
            if not authed:
                return self._send(401, b"{}")
            return self._send(200, json.dumps({
                "name": "legenex", "type": "user",
                "auth": {"accessToken": {"role": "fineGrained", "displayName": "GX-CLUSTER",
                                         "createdAt": "2026-09-17T18:19:43.793Z",
                                         "fineGrained": {"canReadGatedRepos": type(self).gated_permission,
                                                         "global": [], "scoped": []}}},
            }).encode(), {"Content-Type": "application/json"})
        if self.path.startswith("/api/models/"):
            if REPO not in self.path:
                return self._send(404, b"Repository Not Found", {"X-Error-Code": "RepoNotFound"})
            # Metadata is served whatever the gate says — that is the whole point.
            return self._send(200, json.dumps({
                "sha": SHA, "gated": "auto" if type(self).mode != "no_gate" else False,
                "private": False, "author": "acme", "tags": [], "cardData": {},
                "siblings": [{"rfilename": "config.json", "size": 10},
                             {"rfilename": "model.safetensors", "size": 1 << 30}],
            }).encode(), {"Content-Type": "application/json"})
        if "/resolve/" in self.path:
            if type(self).mode == "no_gate" or type(self).mode == "granted":
                return self._send(200, b'{"model_type": "qwen3"}', {"Content-Type": "application/json"})
            if not authed:
                return self._send(401, b"unauthenticated", {"X-Error-Code": "InvalidCredentials"})
            msg = (f"Access to model {REPO} is restricted and you are not in the authorized list. "
                   f"Visit https://huggingface.co/{REPO} to ask for access.")
            return self._send(403, msg.encode(), {"X-Error-Code": "GatedRepo", "X-Error-Message": msg})
        return self._send(404, b"nope")


class HFAccessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeHub)
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()
        cls.api = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        cls._real_api = hfmod.API
        hfmod.API = cls.api

    @classmethod
    def tearDownClass(cls):
        hfmod.API = cls._real_api
        cls.srv.shutdown()

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.token_file = Path(self.tmp.name) / "hf" / "token"
        FakeHub.mode = "not_granted"
        FakeHub.gated_permission = True

    def tearDown(self):
        self.tmp.cleanup()

    def client(self, with_token=True):
        if with_token:
            self.token_file.parent.mkdir(parents=True, exist_ok=True)
            self.token_file.write_text(TOKEN + "\n")
            self.token_file.chmod(0o600)
        return HFClient(self.token_file, cache_seconds=0)

    # ------------------------------------------------------------- token state
    def test_no_token_is_reported_as_not_configured(self):
        self.assertEqual(self.client(with_token=False).token_state(), {"configured": False})

    def test_valid_token_reports_user_type_and_gated_permission(self):
        st = self.client().token_state()
        self.assertTrue(st["configured"])
        self.assertTrue(st["valid"])
        self.assertEqual(st["user"], "legenex")
        self.assertEqual(st["type"], "fineGrained")
        self.assertEqual(st["token_name"], "GX-CLUSTER")
        self.assertIs(st["gated_repos"], True)

    def test_token_without_the_gated_permission_is_reported(self):
        FakeHub.gated_permission = False
        self.assertIs(self.client().token_state()["gated_repos"], False)

    def test_a_group_readable_token_file_is_refused_and_explained(self):
        c = self.client()
        self.token_file.chmod(0o644)
        st = c.token_state()
        self.assertTrue(st["configured"])
        self.assertFalse(st["valid"])
        self.assertEqual(st["code"], "bad_permissions")
        self.assertIn("0600", st["error"])

    def test_the_token_is_never_included_in_the_state(self):
        self.assertNotIn(TOKEN, json.dumps(self.client().token_state()))

    # ----------------------------------------------------------- access verdict
    def test_metadata_succeeding_does_not_mean_the_files_are_accessible(self):
        info = self.client().info(REPO)
        self.assertEqual(info["revision"], SHA)      # metadata read fine
        self.assertFalse(info["accessible"])          # files did not

    def test_gated_but_not_granted_is_distinguished_from_no_token(self):
        info = self.client().info(REPO)
        a = info["access"]
        self.assertFalse(a["ok"])
        self.assertEqual(a["reason"], "gated_not_granted")
        self.assertEqual(a["http_status"], 403)
        self.assertEqual(a["token_user"], "legenex")
        self.assertIn("authorized list", a["message"])
        # The action must send the human to the browser, not to a new token.
        self.assertIn("accept the model", a["action"])
        self.assertIn("A new token cannot fix this", a["action"])

    def test_missing_token_says_to_supply_one(self):
        info = self.client(with_token=False).info(REPO)
        a = info["access"]
        self.assertFalse(a["ok"])
        self.assertEqual(a["reason"], "unauthenticated")
        self.assertEqual(a["http_status"], 401)
        self.assertIn("token", a["action"])
        self.assertNotIn("A new token cannot fix this", a["action"])

    def test_a_token_lacking_the_gated_permission_adds_that_to_the_action(self):
        FakeHub.gated_permission = False
        a = self.client().info(REPO)["access"]
        self.assertEqual(a["reason"], "gated_not_granted")
        self.assertIn("lacks the gated-repo permission", a["action"])

    def test_granted_access_is_reported_as_granted(self):
        FakeHub.mode = "granted"
        info = self.client().info(REPO)
        self.assertTrue(info["accessible"])
        self.assertEqual(info["access"]["reason"], "granted")
        self.assertIsNone(info["access"]["message"])

    def test_an_ungated_repository_is_not_probed(self):
        FakeHub.mode = "no_gate"
        a = self.client().info(REPO)["access"]
        self.assertTrue(a["ok"])
        self.assertEqual(a["reason"], "public")

    def test_error_codes_are_machine_readable(self):
        with self.assertRaises(HFError) as cm:
            self.client().info("acme/missing-thing")
        self.assertEqual(cm.exception.code, "not_found")


if __name__ == "__main__":
    unittest.main()
