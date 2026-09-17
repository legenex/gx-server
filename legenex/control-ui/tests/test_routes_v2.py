"""D-037 routes through the real HTTP server (127.0.0.1, ephemeral port,
offline mode): session/CSRF boundaries, the GX-Playground proxy trust,
Playground restrictions, Resource Control, Storage, Setup, the session music
routes, the public /v1/music API and audio file serving.

Upstreams are stubs on 127.0.0.1 (LiteLLM /key/info, the node-2 music
supervisor). Anything that would reach a node (SSH writes, storage scans,
the media router) is replaced on the App instance before a request is sent.
"""

from __future__ import annotations

import hashlib
import http.client
import dataclasses
import json
import re
import secrets
import sys
import threading
import time
import unittest
from unittest import mock

from support import UI_DIR, StubUpstream, TempEnv, fake_key

from gx_control_ui import auth
from gx_control_ui import server as srv
from gx_control_ui.media_library import NewAsset
from gx_control_ui.resources import GENERATIVE

sys.path.insert(0, str(UI_DIR / "e2e"))
import music_stub  # noqa: E402
from music_stub import MusicStub, sine_wav  # noqa: E402

PASSWORD = "Test-Password-For-Suite-37"
SECRET_SHAPE = re.compile(r"sk-[A-Za-z0-9]{20,}")
_WAV = sine_wav(0.25)
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def make_stub(key: str) -> MusicStub:
    with mock.patch.object(music_stub, "sine_wav", lambda *a, **kw: _WAV):
        return MusicStub(key, polls_to_finish=2)


class V2Base(unittest.TestCase):
    """One music stub and one LiteLLM stub per class; a fresh App per test."""

    @classmethod
    def setUpClass(cls):
        cls.music_key = secrets.token_hex(24)
        cls.stub = make_stub(cls.music_key)
        cls.k_all = fake_key()
        cls.k_music = fake_key()
        cls.k_mini = fake_key()
        cls.k_blocked = fake_key()
        cls.k_expired = fake_key()
        cls.key_infos = {
            cls.k_all: {"key_alias": "everything", "models": []},
            cls.k_music: {"key_alias": "music-app", "models": ["gx-music", "gx-mini"]},
            cls.k_mini: {"key_alias": "chat-only", "models": ["gx-mini"]},
            cls.k_blocked: {"key_alias": "blocked", "models": [], "blocked": True},
            cls.k_expired: {"key_alias": "old", "models": [], "expires": "2020-01-01T00:00:00Z"},
        }

        def key_info(handler, body):
            auth_header = handler.headers.get("Authorization") or ""
            info = cls.key_infos.get(auth_header[7:])
            if info is None:
                return 401, {"error": {"message": "Authentication Error"}}
            return 200, {"key": "hashed", "info": info}

        cls.litellm = StubUpstream({("GET", "/key/info"): key_info,
                                    ("GET", "/v1/models"): (401, {"error": {"message": "invalid key"}})})

    @classmethod
    def tearDownClass(cls):
        cls.stub.close()
        cls.stub.server.server_close()
        cls.litellm.close()

    def setUp(self):
        with self.stub._lock:
            self.stub.jobs.clear()
            self.stub.uploads.clear()
            self.stub.calls.clear()
            self.stub.engine = "unloaded"
        self.litellm.calls.clear()
        self.env = TempEnv(litellm_base=self.litellm.url, music_base=self.stub.url)
        self.env.cfg.music_key_file.write_text(self.music_key + "\n")
        auth.PasswordStore(self.env.cfg.password_file).set_password("admin", PASSWORD, n=2**10)
        self.app, servers = srv.build(self.env.cfg)
        self.httpd = servers[0]
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        # nothing may reach a node
        self.node2_writes: list[tuple[str, str]] = []
        self.app.resources._node2_writer = lambda name, content: self.node2_writes.append((name, content)) or True
        self.app.resources.media_free = lambda: (False, "offline test")
        self.storage_requests: list[tuple[str, dict]] = []
        self.app.storage._runner = self.storage_runner
        self.app.storage.verify = lambda: {"ok": True, "checks": []}
        self.cookie = None
        self.csrf = None

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.env.cleanup()

    # ------------------------------------------------------------ helpers
    def storage_runner(self, node, request, timeout):
        self.storage_requests.append((node, request))
        if request["mode"] == "scan":
            return {"usage": {}, "candidates": [
                {"kind": "path", "target": f"/srv/cache/pip-{node}", "class": "safe", "bytes": 10, "mtime": 1,
                 "name": "pip"}]}
        return {"results": [{"target": i["target"], "ok": True, "freed": 10} for i in request["items"]],
                "freed": 10 * len(request["items"]), "free_after": 1}

    def req(self, method, path, body=None, headers=None, raw_body=None, cookie=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        hdrs = {"Host": f"127.0.0.1:{self.port}"}
        if cookie and self.cookie:
            hdrs["Cookie"] = self.cookie
        if body is not None:
            raw_body = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
        hdrs.update(headers or {})
        conn.request(method, path, body=raw_body, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        ctype = resp.getheader("Content-Type", "")
        parsed = json.loads(data) if data and "json" in ctype else data
        return resp.status, dict(resp.getheaders()), parsed

    def login(self, password=PASSWORD, headers=None):
        status, hdrs, body = self.req("POST", "/api/login", {"username": "admin", "password": password},
                                      headers=headers, cookie=False)
        if status == 200 and headers is None:
            self.cookie = hdrs["Set-Cookie"].split(";")[0]
            self.csrf = body["csrf"]
        return status, body

    def post(self, path, body=None, csrf=True, origin=True, headers=None):
        hdrs = {}
        if csrf and self.csrf:
            hdrs["X-CSRF-Token"] = self.csrf
        if origin:
            hdrs["Origin"] = f"http://127.0.0.1:{self.port}"
        hdrs.update(headers or {})
        return self.req("POST", path, body if body is not None else {}, hdrs)

    def playground(self, fwd="100.64.7.8"):
        return {"X-GX-Proxy-Token": self.app.proxy_token, "X-GX-Forwarded-For": fwd}

    def audit(self):
        path = self.env.cfg.log_dir / "audit.log"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def api(self, method, path, key, body=None, headers=None, raw_body=None):
        hdrs = {"Authorization": f"Bearer {key}"} if key else {}
        hdrs.update(headers or {})
        return self.req(method, path, body, hdrs, raw_body=raw_body, cookie=False)

    def completed_import(self, job_id):
        for _ in range(10):
            if self.app.music.get(job_id)["status"] == "completed":
                break
        self.app.music.sweep()
        self.assertTrue(self.app.music.get(job_id)["imported"])


SESSION_GETS = ["/api/resources", "/api/resources/summary", "/api/resources/compatibility",
                "/api/resources/admission/gx-image", "/api/resources/explain/gx-video",
                "/api/resources/profile/plan?to=auto", "/api/resources/ops/0123456789abcdef",
                "/api/storage", "/api/setup", "/api/music/model", "/api/music/tags?q=syn", "/api/music/jobs",
                "/api/music/jobs/mus-" + "a" * 32, "/api/music/jobs/mus-" + "a" * 32 + "/lineage",
                "/api/creative/overview"]
SESSION_POSTS = ["/api/resources/profile", "/api/resources/gx-image/pin", "/api/resources/gx-mini/load",
                 "/api/storage/scan", "/api/storage/plan", "/api/storage/clean", "/api/setup/test",
                 "/api/music/jobs", "/api/music/jobs/mus-" + "a" * 32 + "/cancel"]


class BoundaryTests(V2Base):
    def test_session_required(self):
        for path in SESSION_GETS:
            status, _, body = self.req("GET", path)
            self.assertEqual((status, body["error"]["code"]), (401, "unauthenticated"), path)
        for path in SESSION_POSTS:
            status, _, body = self.post(path, {"profile": "text", "ids": ["c_" + "0" * 24]}, csrf=False)
            self.assertEqual(status, 401, path)
        status, _, _ = self.req("POST", "/api/music/upload", raw_body=b"RIFF", headers={"Content-Type": "audio/wav"})
        self.assertEqual(status, 401)
        self.assertEqual(self.stub.calls, [])

    def test_playground_token_alone_is_not_a_session(self):
        for path in ("/api/resources/summary", "/api/storage"):
            self.assertEqual(self.req("GET", path, headers=self.playground())[0], 401)

    def test_csrf_and_same_origin_required_on_posts(self):
        self.login()
        for path in SESSION_POSTS:
            status, _, body = self.post(path, {"profile": "maintenance", "ids": ["c_" + "0" * 24]}, csrf=False)
            self.assertEqual((status, body["error"]["code"]), (403, "csrf"), path)
            status, _, body = self.post(path, {"profile": "maintenance"}, headers={"X-CSRF-Token": "x" * 43})
            self.assertEqual((status, body["error"]["code"]), (403, "csrf"), path)
            status, _, body = self.post(path, {"profile": "maintenance"},
                                        headers={"Origin": "http://evil.example"})
            self.assertEqual((status, body["error"]["code"]), (403, "bad_origin"), path)
        status, _, _ = self.req("POST", "/api/music/upload", raw_body=b"RIFF",
                                headers={"Content-Type": "audio/wav"})
        self.assertEqual(status, 403)
        self.assertEqual(self.node2_writes, [])
        self.assertEqual(self.storage_requests, [])
        self.assertFalse((self.env.cfg.guard_dir / "profile.json").exists())
        self.assertFalse((self.env.cfg.guard_dir / "node1.maintenance-hold").exists())
        self.assertEqual([c for c in self.stub.calls if c[0] == "POST"], [])

    def test_get_routes_do_not_accept_post_and_unknown_aliases_404(self):
        self.login()
        self.assertEqual(self.post("/api/resources/summary")[0], 405)
        for path in ("/api/resources/gx-auto/load", "/api/resources/gx-evil/pin", "/api/resources/gx-image/format",
                     "/api/storage/delete"):
            self.assertEqual(self.post(path)[0], 404, path)
        self.assertEqual(self.req("GET", "/api/resources/admission/gx-auto")[0], 404)
        self.assertEqual(self.req("PUT", "/api/resources/profile")[0], 405)


class ProxyTrustTests(V2Base):
    def last_login(self):
        return [e for e in self.audit() if e.get("action") == "login"][-1]

    def test_forwarded_address_needs_the_token_from_loopback(self):
        self.login("wrong-password-1", headers=self.playground("100.64.1.2"))
        self.assertEqual(self.last_login()["ip"], "100.64.1.2")
        self.login("wrong-password-2", headers={"X-GX-Proxy-Token": "x" * 43, "X-GX-Forwarded-For": "100.64.1.3"})
        self.assertEqual(self.last_login()["ip"], "127.0.0.1")
        self.login("wrong-password-3", headers={"X-GX-Forwarded-For": "100.64.1.4"})
        self.assertEqual(self.last_login()["ip"], "127.0.0.1")
        self.login("wrong-password-4", headers={"X-GX-Proxy-Token": "", "X-GX-Forwarded-For": "100.64.1.5"})
        self.assertEqual(self.last_login()["ip"], "127.0.0.1")
        for bad in ("1.2.3.4; rm -rf /", "evil.example", "1.2.3.4, 5.6.7.8", "x" * 60):
            self.login("wrong-password-5", headers=self.playground(bad))
            self.assertEqual(self.last_login()["ip"], "127.0.0.1", bad)

    def test_ipv6_forwarded_address(self):
        self.login("wrong-password-1", headers=self.playground("fd7a:115c:a1e0::1"))
        self.assertEqual(self.last_login()["ip"], "fd7a:115c:a1e0::1")

    def test_login_throttle_is_per_forwarded_client(self):
        for i in range(5):
            self.assertEqual(self.login(f"wrong-password-{i}", headers=self.playground("100.64.9.9"))[0], 401)
        status, body = self.login(headers=self.playground("100.64.9.9"))
        self.assertEqual((status, body["error"]["code"]), (429, "throttled"))
        # a different Playground client and the direct loopback client are unaffected
        self.assertEqual(self.login(headers=self.playground("100.64.9.10"))[0], 200)
        self.assertEqual(self.login(headers={"X-GX-Proxy-Token": "bad-token", "X-GX-Forwarded-For": "100.64.9.9"})[0],
                         200)
        self.assertEqual(self.login()[0], 200)

    def test_token_file_is_private_and_stable(self):
        path = self.env.cfg.proxy_token_file
        self.assertEqual(path.read_text().strip(), self.app.proxy_token)
        self.assertGreaterEqual(len(self.app.proxy_token), 32)
        self.assertEqual(path.stat().st_mode & 0o077, 0)
        app2 = srv.App(self.env.cfg)
        self.assertEqual(app2.proxy_token, self.app.proxy_token)

    def test_short_token_file_is_replaced(self):
        self.env.cfg.proxy_token_file.write_text("short\n")
        app2 = srv.App(self.env.cfg)
        self.assertNotEqual(app2.proxy_token, "short")
        self.assertGreaterEqual(len(app2.proxy_token), 32)


class PlaygroundRestrictionTests(V2Base):
    def setUp(self):
        super().setUp()
        self.login()

    def test_runtime_controls_are_refused(self):
        for alias, op in (("gx-image", "pin"), ("gx-reason", "unpin"), ("gx-mini", "load"), ("gx-music", "unload"),
                          ("gx-reason", "drain"), ("gx-max", "load")):
            status, _, body = self.post(f"/api/resources/{alias}/{op}", {"confirm": "gx-max"},
                                        headers=self.playground())
            self.assertEqual((status, body["error"]["code"]), (403, "resourceerror"), (alias, op))
        self.assertEqual(self.node2_writes, [])
        self.assertFalse(any(e.get("action", "").startswith("resources.") for e in self.audit()))

    def test_storage_scan_and_clean_are_refused(self):
        status, _, body = self.post("/api/storage/scan", headers=self.playground())
        self.assertEqual((status, body["error"]["code"]), (403, "storageerror"))
        status, _, _ = self.post("/api/storage/clean", {"ids": ["c_" + "a" * 24], "confirm": True},
                                 headers=self.playground())
        self.assertEqual(status, 403)
        self.assertEqual(self.storage_requests, [])
        self.assertEqual(self.app.storage.status()["state"], "idle")

    def test_maintenance_profile_is_refused(self):
        status, _, body = self.post("/api/resources/profile", {"profile": "maintenance", "confirm": True},
                                    headers=self.playground())
        self.assertEqual((status, body["error"]["code"]), (403, "resourceerror"))
        self.assertFalse((self.env.cfg.guard_dir / "profile.json").exists())
        self.assertFalse((self.env.cfg.guard_dir / "node1.maintenance-hold").exists())
        self.assertEqual(self.node2_writes, [])

    def test_other_profiles_are_allowed_and_recorded_as_playground(self):
        status, _, body = self.post("/api/resources/profile", {"profile": "music"}, headers=self.playground())
        self.assertEqual((status, body["profile"]), (200, "music"))
        record = json.loads((self.env.cfg.guard_dir / "profile.json").read_text())
        self.assertEqual((record["source"], record["by"]), ("playground", "admin"))
        entry = [e for e in self.audit() if e.get("action") == "resources.profile"][-1]
        self.assertEqual((entry["source"], entry["ip"]), ("playground", "100.64.7.8"))
        self.assertEqual(json.loads(self.node2_writes[-1][1])["profile"], "music")

    def test_a_wrong_token_is_not_the_playground(self):
        wrong = {"X-GX-Proxy-Token": "x" * 43, "X-GX-Forwarded-For": "100.64.7.8"}
        status, _, body = self.post("/api/resources/gx-image/pin", headers=wrong)
        self.assertEqual((status, body), (202, {"alias": "gx-image", "pinned": True}))
        entry = [e for e in self.audit() if e.get("action") == "resources.pin"][-1]
        self.assertEqual(entry["ip"], "127.0.0.1")
        self.post("/api/resources/profile", {"profile": "text"}, headers=wrong)
        record = json.loads((self.env.cfg.guard_dir / "profile.json").read_text())
        self.assertEqual(record["source"], "control-center")

    def test_playground_reads_and_creates(self):
        status, _, summary = self.req("GET", "/api/resources/summary", headers=self.playground())
        self.assertEqual(status, 200)
        self.assertIn("rows", summary)
        status, _, job = self.post("/api/music/jobs", {"prompt": "lo-fi"}, headers=self.playground())
        self.assertEqual(status, 202, job)
        self.assertEqual(job["submitted_via"], "playground")
        entry = [e for e in self.audit() if e.get("action") == "music.generate"][-1]
        self.assertEqual(entry["ip"], "100.64.7.8")


class OpenWebUIIdentityApiTests(V2Base):
    """D-038 routes; the sync itself is covered by test_owui_identity."""

    def test_requires_a_session_and_csrf(self):
        self.assertEqual(self.req("GET", "/api/setup/openwebui/identity")[0], 401)
        self.assertEqual(self.post("/api/setup/openwebui/identity/sync")[0], 401)
        self.login()
        status, _, body = self.req("GET", "/api/setup/openwebui/identity")
        self.assertEqual((status, body["offline"], body["in_sync"]), (200, True, None))
        self.assertEqual(self.post("/api/setup/openwebui/identity/sync", csrf=False)[0], 403)
        status, _, body = self.post("/api/setup/openwebui/identity/sync")
        self.assertEqual(status, 400)
        self.assertIn("offline", body["error"]["message"])

    def test_sync_reports_and_audits(self):
        self.login()
        self.app.cfg = dataclasses.replace(self.app.cfg, offline=False)  # routes only; nothing else re-reads it
        calls = []

        class FakeIdentity:
            def plan(self):
                return {"in_sync": False, "items": [{"id": "gx-mini", "state": "missing"}]}

            def apply(self, *, user):
                calls.append(user)
                return {"in_sync": True, "items": [{"id": "gx-mini", "state": "ok"}], "written": ["gx-mini"]}

        self.app.identity = FakeIdentity()
        status, _, body = self.req("GET", "/api/setup/openwebui/identity")
        self.assertEqual((status, body["in_sync"]), (200, False))
        status, _, body = self.post("/api/setup/openwebui/identity/sync")
        self.assertEqual((status, body["written"]), (200, ["gx-mini"]))
        self.assertEqual(calls, ["admin"])
        status, _, body = self.post("/api/setup/openwebui/identity/sync", headers=self.playground())
        self.assertEqual(status, 403)
        self.assertEqual(calls, ["admin"])


class ResourcesApiTests(V2Base):
    def setUp(self):
        super().setUp()
        self.login()

    def test_summary_shape(self):
        status, headers, s = self.req("GET", "/api/resources/summary")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(set(s), {"profile", "profile_label", "profiles", "maintenance", "rows", "queued",
                                  "control_center_url", "generated_at"})
        self.assertEqual((s["profile"], s["profile_label"], s["maintenance"]), ("auto", "Auto", False))
        self.assertEqual([p["id"] for p in s["profiles"]], ["auto", "text", "media", "music", "max"])
        for p in s["profiles"]:
            self.assertEqual(set(p), {"id", "label", "summary"})
        self.assertEqual([r["key"] for r in s["rows"]], ["text", "image", "video", "music", "max"])
        allowed = {"Ready", "Loading", "Working", "Waiting", "Unloading", "Paused", "Unavailable", "Idle",
                   "Starting", "Running", "Releasing"}
        for row in s["rows"]:
            self.assertEqual(set(row), {"key", "label", "status", "detail"})
            self.assertIn(row["status"], allowed)
        self.assertEqual(s["control_center_url"], "http://127.0.0.1:8088/#/resources")
        self.assertEqual(s["queued"], 0)
        text = json.dumps(s)
        for leak in ("192.168.", "node2", "MemAvailable", "ssh", "/srv/"):
            self.assertNotIn(leak, text)

    def test_summary_counts_waiting_creative_jobs(self):
        self.app.media.snapshot = lambda: {"jobs": [], "counts": {"queued": 2, "waiting": 1, "generating": 1}}
        self.assertEqual(self.req("GET", "/api/resources/summary")[2]["queued"], 3)

    def test_full_map(self):
        status, _, snap = self.req("GET", "/api/resources")
        self.assertEqual(status, 200)
        self.assertEqual(set(snap["admission"]), set(GENERATIVE))
        self.assertEqual(set(snap["nodes"]), {"node1", "node2"})
        self.assertEqual(snap["admission"]["gx-max"]["code"], "takeover")
        self.assertEqual(snap["reserve_gib"], 30.0)
        self.assertNotRegex(json.dumps(snap), SECRET_SHAPE)

    def test_compatibility_admission_explain_plan(self):
        status, _, comp = self.req("GET", "/api/resources/compatibility")
        self.assertEqual((status, len(comp["pairs"])), (200, 21))
        status, _, view = self.req("GET", "/api/resources/admission/gx-video?variant=keyframe_edit")
        self.assertEqual((status, view["need_gib"], view["code"], view["terminal"]),
                         (200, 137.0, "exceeds_node", True))
        status, _, body = self.req("GET", "/api/resources/admission/gx-video?variant=../../x")
        self.assertEqual((status, body["error"]["code"]), (400, "invalid_request"))
        self.assertEqual(self.req("GET", "/api/resources/explain/gx-music")[0], 200)
        status, _, plan = self.req("GET", "/api/resources/profile/plan?to=max")
        self.assertEqual((status, plan["to"], plan["confirm_phrase"]), (200, "max", "gx-max"))
        self.assertEqual(self.req("GET", "/api/resources/profile/plan?to=root")[0], 400)
        self.assertEqual(self.req("GET", "/api/resources/profile/plan")[0], 400)
        self.assertEqual(self.req("GET", "/api/resources/ops/0123456789abcdef")[0], 404)

    def test_profile_changes(self):
        for bad in ({"profile": "root"}, {}, {"profile": ["auto"]}):
            status, _, _ = self.post("/api/resources/profile", bad)
            self.assertEqual(status, 400, bad)
        status, _, body = self.post("/api/resources/profile", {"profile": "max"})
        self.assertEqual((status, body["error"]["code"]), (409, "resourceerror"))
        self.assertIn("type gx-max", body["error"]["message"])
        status, _, body = self.post("/api/resources/profile", {"profile": "media"})
        self.assertEqual((status, body["profile"]), (200, "media"))
        self.assertEqual(self.req("GET", "/api/resources/summary")[2]["profile"], "media")
        status, _, body = self.post("/api/resources/profile", {"profile": "maintenance"})
        self.assertEqual(status, 200, body)
        self.assertTrue((self.env.cfg.guard_dir / "node1.maintenance-hold").exists())
        self.assertIn(("node2.maintenance-hold", "1"), self.node2_writes)
        self.assertEqual(self.req("GET", "/api/resources/summary")[2]["profile"], "maintenance")
        # offline there are no host facts; the local hold file is what the
        # action runner and Storage consult
        self.assertTrue(self.app.resources.maintenance())
        self.assertTrue(self.req("GET", "/api/storage")[2]["scan"]["maintenance"])
        self.post("/api/resources/profile", {"profile": "auto"})
        self.assertFalse((self.env.cfg.guard_dir / "node1.maintenance-hold").exists())

    def test_controls(self):
        status, _, body = self.post("/api/resources/gx-video/pin")
        self.assertEqual((status, body["pinned"]), (202, True))
        self.assertEqual(json.loads(self.node2_writes[-1][1]).keys(), {"gx-video"})
        status, _, body = self.post("/api/resources/gx-mini/pin")
        self.assertEqual(status, 400)
        status, _, body = self.post("/api/resources/gx-image/load")
        self.assertEqual(status, 400)
        # offline: gx10-02 memory is unknown, so a load is not admitted
        status, _, body = self.post("/api/resources/gx-reason/load")
        self.assertEqual((status, body["error"]["code"], body["admission"]["code"]), (409, "admission", "unknown"))
        status, _, body = self.post("/api/resources/gx-image/unload")
        self.assertEqual((status, body["error"]["code"]), (409, "resourceerror"))
        self.assertNotIn("Bearer", json.dumps(body))
        status, _, body = self.post("/api/resources/gx-max/load", {"confirm": "nope"})
        self.assertEqual(status, 400)
        self.assertIn("gx-max", body["error"]["message"])


class StorageApiTests(V2Base):
    def setUp(self):
        super().setUp()
        self.login()

    def scan(self):
        status, _, body = self.post("/api/storage/scan")
        self.assertEqual(status, 202)
        deadline = time.time() + 5
        while time.time() < deadline:
            scan = self.req("GET", "/api/storage")[2]["scan"]
            if scan["state"] != "scanning":
                return scan
            time.sleep(0.01)
        self.fail("scan did not finish")

    def test_overview(self):
        status, _, body = self.req("GET", "/api/storage")
        self.assertEqual(status, 200)
        self.assertEqual(body["thresholds"], {"critical_gib": 30, "low_gib": 75, "watch_gib": 150})
        self.assertEqual(body["overview"]["node2"]["health"]["level"], "unknown")
        self.assertEqual(set(body["library"]), {"image", "video", "audio"})
        self.assertEqual(body["scan"]["state"], "idle")

    def test_scan_plan_clean(self):
        scan = self.scan()
        self.assertEqual(scan["state"], "done")
        ids = [c["id"] for n in ("node1", "node2") for c in scan["result"][n]["candidates"]]
        self.assertEqual(len(ids), 2)
        status, _, plan = self.post("/api/storage/plan", {"ids": ids})
        self.assertEqual((status, plan["bytes"], plan["missing"]), (200, 20, []))
        self.assertEqual(self.post("/api/storage/plan", {"ids": "x"})[0], 400)
        self.assertEqual(self.post("/api/storage/plan", {"ids": [1]})[0], 400)
        status, _, out = self.post("/api/storage/clean", {"ids": ids[:1]})
        self.assertEqual((status, out["freed_bytes"], out["requested"]), (200, 10, 1))
        deletes = [(n, r) for n, r in self.storage_requests if r["mode"] == "delete"]
        self.assertEqual(len(deletes), 1)
        self.assertEqual(deletes[0][1]["items"][0]["target"], "/srv/cache/pip-node1")
        entry = [e for e in self.audit() if e.get("action") == "storage.cleanup"][-1]
        self.assertEqual((entry["user"], entry["items"]), ("admin", 1))
        # consumed: a replay is refused
        status, _, body = self.post("/api/storage/clean", {"ids": ids[:1]})
        self.assertEqual(status, 409)

    def test_clean_accepts_only_opaque_ids_from_the_latest_scan(self):
        self.scan()
        for bad in ({"ids": "c_" + "0" * 24}, {"ids": ["/srv/models"]}, {"ids": ["c_" + "0" * 23]},
                    {"ids": ["c_" + "G" * 24]}, {"ids": ["C_" + "0" * 24]}, {"ids": [{"target": "/srv"}]},
                    {"ids": ["c_" + "0" * 24 + "\n"]}, {}):
            status, _, body = self.post("/api/storage/clean", bad)
            self.assertEqual((status, body["error"]["code"]), (400, "invalid_request"), bad)
        status, _, body = self.post("/api/storage/clean", {"ids": ["c_" + "0" * 24]})
        self.assertEqual((status, body["error"]["code"]), (409, "storageerror"))
        self.assertEqual(self.post("/api/storage/clean", {"ids": []})[0], 400)
        self.assertFalse(any(r["mode"] == "delete" for _, r in self.storage_requests))


class SetupApiTests(V2Base):
    def setUp(self):
        super().setUp()
        self.login()

    def test_setup_info(self):
        status, _, body = self.req("GET", "/api/setup")
        self.assertEqual(status, 200)
        self.assertIn("gx-cluster", json.loads(body["kilo"]["config_example"])["provider"])
        self.assertNotRegex(json.dumps(body), SECRET_SHAPE)

    def test_connection_test(self):
        for bad in ({"client": "kilo", "secret": "nope"}, {"client": "cursor", "secret": fake_key()},
                    {"client": "kilo"}):
            status, _, body = self.post("/api/setup/test", bad)
            self.assertEqual((status, body["error"]["code"]), (400, "invalid_request"), bad)
        pasted = fake_key()
        status, _, body = self.post("/api/setup/test", {"client": "generic", "secret": pasted})
        self.assertEqual(status, 200)
        self.assertFalse(body["connected"])
        self.assertEqual(body["summary"], "The gateway refused the key.")
        self.assertEqual(self.litellm.calls[-1][2]["Authorization"], f"Bearer {pasted}")
        entry = [e for e in self.audit() if e.get("action") == "setup.test.generic"][-1]
        self.assertEqual(entry["outcome"], "failed")
        log_text = (self.env.cfg.log_dir / "audit.log").read_text()
        self.assertNotIn(pasted, log_text)
        self.assertNotIn(pasted, json.dumps(body))


class SessionMusicTests(V2Base):
    def setUp(self):
        super().setUp()
        self.login()

    def test_model_tags_jobs(self):
        status, _, model = self.req("GET", "/api/music/model")
        self.assertEqual((status, model["alias"]), (200, "gx-music"))
        self.assertIn("registry", model)
        self.assertIn("synthwave", self.req("GET", "/api/music/tags?q=syn")[2]["suggestions"])
        status, _, job = self.post("/api/music/jobs", {"operation": "generate", "prompt": "lo-fi"})
        self.assertEqual((status, job["submitted_via"]), (202, "control-center"))
        self.assertNotIn("links", job)
        status, _, got = self.req("GET", f"/api/music/jobs/{job['id']}")
        self.assertEqual((status, got["id"]), (200, job["id"]))
        self.assertEqual(self.req("GET", "/api/music/jobs")[0], 200)
        self.assertEqual(self.req("GET", "/api/music/jobs?status=DROP%20TABLE")[0], 400)
        self.assertEqual(self.req("GET", f"/api/music/jobs/{job['id']}/lineage")[0], 200)
        status, _, cancelled = self.post(f"/api/music/jobs/{job['id']}/cancel")
        self.assertEqual((status, cancelled["status"]), (200, "cancelled"))

    def test_submit_validation(self):
        for body in ({"operation": "extract", "prompt": "x"}, {"prompt": "x", "engine": "http://evil"}):
            status, _, out = self.post("/api/music/jobs", body)
            self.assertEqual(status, 400, out)
        status, _, out = self.post("/api/music/jobs", {"operation": "remix", "prompt": "x"})
        self.assertEqual(status, 400)
        status, _, out = self.req("GET", "/api/music/jobs/mus-" + "b" * 32)
        self.assertEqual((status, out["error"]["code"]), (404, "not_found"))

    def test_node2_unavailable(self):
        self.env.cfg.music_key_file.unlink()
        status, _, out = self.req("GET", "/api/music/model")
        self.assertEqual((status, out["error"]["code"]), (503, "not_configured"))

    def test_upload(self):
        status, _, asset = self.req("POST", "/api/music/upload", raw_body=_WAV,
                                    headers={"Content-Type": "audio/wav", "X-CSRF-Token": self.csrf,
                                             "X-Filename": "my%20take.wav", "X-Title": "Take%201"})
        self.assertEqual(status, 200, asset)
        self.assertEqual((asset["type"], asset["title"], asset["operation"]), ("audio", "Take 1", "upload"))
        status, _, out = self.req("POST", "/api/music/upload", raw_body=b"abc",
                                  headers={"Content-Type": "text/plain", "X-CSRF-Token": self.csrf})
        self.assertEqual(status, 400)


class PublicMusicApiTests(V2Base):
    def submit(self, key, body=None):
        status, headers, job = self.api("POST", "/v1/music/generations", key, body or {"prompt": "lo-fi",
                                                                                      "duration": 10})
        self.assertEqual(status, 202, job)
        return headers, job

    def test_authentication(self):
        status, _, body = self.api("GET", "/v1/music/model", None)
        self.assertEqual((status, body["error"]["code"], body["error"]["retryable"]), (401, "unauthorized", False))
        for header in ("Bearer not-a-key", "Basic " + self.k_all, "Bearer " + self.k_all + " extra", "Bearer "):
            status, _, _ = self.req("GET", "/v1/music/model", headers={"Authorization": header}, cookie=False)
            self.assertEqual(status, 401, header)
        self.assertEqual(self.litellm.calls, [])  # malformed keys never reach LiteLLM
        for key in (fake_key(), self.k_blocked, self.k_expired):
            self.assertEqual(self.api("GET", "/v1/music/model", key)[0], 401)
        status, _, body = self.api("GET", "/v1/music/model", self.k_mini)
        self.assertEqual((status, body["error"]["code"]), (403, "forbidden"))
        self.assertEqual(self.stub.calls, [])

    def test_session_cookie_is_not_accepted(self):
        self.login()
        status, _, _ = self.req("GET", "/v1/music/model")
        self.assertEqual(status, 401)
        status, _, _ = self.req("POST", "/v1/music/generations", {"prompt": "x"},
                                headers={"X-CSRF-Token": self.csrf, "Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(status, 401)
        self.assertEqual(self.stub.jobs, {})

    def test_key_lookups_are_cached(self):
        for _ in range(3):
            self.assertEqual(self.api("GET", "/v1/music/model", self.k_music)[0], 200)
        self.assertEqual(len([c for c in self.litellm.calls if c[1] == "/key/info"]), 1)

    def test_revoke_and_replace_take_effect_immediately(self):
        key = fake_key()
        self.key_infos[key] = {"key_alias": "short-lived", "models": ["gx-music"]}
        try:
            self.assertEqual(self.api("GET", "/v1/music/model", key)[0], 200)
            del self.key_infos[key]  # LiteLLM no longer knows it
            self.assertEqual(self.api("GET", "/v1/music/model", key)[0], 200)  # cached look-up
            self.app.keys.revoke = lambda key_id: {"revoked": key_id, "name": "short-lived"}
            self.login()
            status, _, body = self.post(f"/api/keys/{'a' * 64}/revoke", {"confirm": True})
            self.assertEqual(status, 200, body)
            self.assertEqual(self.api("GET", "/v1/music/model", key)[0], 401)
        finally:
            self.key_infos.pop(key, None)

    def test_model_view_is_limited(self):
        status, _, body = self.api("GET", "/v1/music/model", self.k_music)
        self.assertEqual(status, 200)
        self.assertEqual(set(body), {"alias", "task", "node", "identity", "runtime", "capabilities"})
        self.assertEqual(self.api("GET", "/v1/music/tags?q=ja", self.k_music)[0], 200)

    def test_submit_and_ownership(self):
        headers, job = self.submit(self.k_music)
        jid = job["id"]
        self.assertEqual(headers["Location"], f"/v1/music/{jid}")
        self.assertEqual(job["links"]["self"], f"/v1/music/{jid}")
        for hidden in ("submitted_via", "import_error"):
            self.assertNotIn(hidden, job)
        self.assertEqual(self.app.music._jobs[jid]["via"], "api")
        state = json.loads((self.env.cfg.state_dir / "music-jobs.json").read_text())
        self.assertTrue(state[jid]["owner"].startswith("key:"))
        self.assertNotIn(self.k_music, json.dumps(state))
        self.assertEqual(self.api("GET", f"/v1/music/{jid}", self.k_music)[0], 200)
        self.assertEqual(self.api("GET", f"/v1/music/{jid}/lineage", self.k_music)[0], 200)
        # another key (even one that allows everything) cannot see or touch it
        for method, path in (("GET", f"/v1/music/{jid}"), ("GET", f"/v1/music/{jid}/lineage"),
                             ("GET", f"/v1/music/{jid}/content"), ("POST", f"/v1/music/{jid}/cancel")):
            status, _, body = self.api(method, path, self.k_all)
            self.assertEqual((status, body["error"]["code"]), (404, "not_found"), path)
        self.assertEqual(self.api("GET", "/v1/music/jobs", self.k_all)[2], {"data": []})
        mine = self.api("GET", "/v1/music/jobs", self.k_music)[2]["data"]
        self.assertEqual([j["id"] for j in mine], [jid])
        status, _, body = self.api("POST", f"/v1/music/{jid}/cancel", self.k_music)
        self.assertEqual((status, body["status"]), (200, "cancelled"))
        entry = [e for e in self.audit() if e.get("action") == "music.generate"][-1]
        self.assertEqual(entry["user"], "key:music-app")

    def test_jobs_from_the_session_are_not_visible_to_keys(self):
        self.login()
        status, _, job = self.post("/api/music/jobs", {"prompt": "x"})
        self.assertEqual(status, 202)
        self.assertEqual(self.api("GET", f"/v1/music/{job['id']}", self.k_all)[0], 404)
        self.assertEqual(self.api("GET", f"/v1/music/{'mus-' + 'c' * 32}", self.k_all)[0], 404)

    def test_content_download(self):
        _, job = self.submit(self.k_music)
        jid = job["id"]
        status, _, body = self.api("GET", f"/v1/music/{jid}/content?format=wav", self.k_music)
        self.assertEqual((status, body["error"]["code"]), (409, "not_ready"))
        # Rendered on node 2 but not yet saved: the API says "saving", never "completed".
        for _ in range(10):
            if self.app.music.client.call("GET", f"/v1/music/{jid}")["status"] == "completed":
                break
        view = self.api("GET", f"/v1/music/{jid}", self.k_music)[2]
        self.assertEqual((view["status"], view["imported"]), ("saving", False))
        self.completed_import(jid)
        status, headers, data = self.api("GET", f"/v1/music/{jid}/content?format=wav", self.k_music)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "audio/wav")
        self.assertEqual(data, _WAV)
        self.assertIn(f'filename="{jid}-0.wav"', headers["Content-Disposition"])
        status, _, body = self.api("GET", f"/v1/music/{jid}/content", self.k_music)  # mp3 by default
        self.assertEqual((status, body["error"]["code"]), (404, "not_found"))
        status, _, body = self.api("GET", f"/v1/music/{jid}/content?format=ogg", self.k_music)
        self.assertEqual((status, body["error"]["code"]), (400, "invalid_request"))
        self.assertEqual(self.api("GET", f"/v1/music/{jid}/content?format=wav&index=3", self.k_music)[0], 409)
        status, headers, data = self.api("GET", f"/v1/music/{jid}/content?format=wav", self.k_music,
                                         headers={"Range": "bytes=0-3"})
        self.assertEqual((status, data), (206, b"RIFF"))
        view = self.api("GET", f"/v1/music/{jid}", self.k_music)[2]
        self.assertEqual(view["tracks"][0]["files"]["wav"]["url"], f"/v1/music/{jid}/content?index=0&format=wav")
        self.assertTrue(view["imported"])
        self.assertEqual(view["status"], "completed")

    def test_forbidden_and_unknown_operations(self):
        for path in ("/v1/music/load", "/v1/music/unload"):
            status, _, body = self.api("POST", path, self.k_all)
            self.assertEqual((status, body["error"]["code"]), (403, "forbidden"))
        self.assertEqual(self.stub.engine, "unloaded")
        _, job = self.submit(self.k_all)
        status, _, body = self.api("DELETE", f"/v1/music/{job['id']}", self.k_all)
        self.assertEqual((status, body["error"]["code"]), (405, "method_not_allowed"))
        self.assertEqual(self.api("POST", f"/v1/music/{job['id']}", self.k_all)[0], 405)
        self.assertEqual(self.api("GET", "/v1/music/nothing", self.k_all)[0], 404)
        self.assertEqual(self.api("GET", "/v1/music/../api/storage", self.k_all)[0], 404)
        self.assertEqual(self.req("DELETE", "/api/storage")[0], 405)

    def test_invalid_bodies(self):
        status, _, body = self.api("POST", "/v1/music/generations", self.k_all, raw_body=b"{nope",
                                   headers={"Content-Type": "application/json"})
        self.assertEqual((status, body["error"]["code"]), (400, "invalid_request"))
        status, _, body = self.api("POST", "/v1/music/generations", self.k_all, {"prompt": "x", "path": "/srv"})
        self.assertEqual((status, body["error"]["code"]), (400, "music_error"))
        status, _, body = self.api("POST", "/v1/music/remix", self.k_all, {"source": {"job_id": "x"}})
        self.assertEqual(status, 400)
        status, _, body = self.api("POST", "/v1/music/generations", self.k_all, raw_body=b"x" * (70 * 1024),
                                   headers={"Content-Type": "application/json"})
        self.assertEqual((status, body["error"]["code"]), (413, "too_large"))
        self.assertEqual(self.stub.jobs, {})

    def test_rate_limits(self):
        ident = hashlib.sha256(self.k_music.encode()).hexdigest()[:16]
        self.app.api_rate[ident] = [time.time()] * 120
        status, _, body = self.api("GET", "/v1/music/model", self.k_music)
        self.assertEqual((status, body["error"]["code"], body["error"]["retryable"]), (429, "rate_limited", True))
        self.assertEqual(self.api("GET", "/v1/music/model", self.k_all)[0], 200)  # per key
        self.app.api_rate[ident] = [time.time() - 61] * 120  # the window slides
        self.assertEqual(self.api("GET", "/v1/music/model", self.k_music)[0], 200)
        self.app.api_rate["submit:" + ident] = [time.time()] * 20
        status, _, body = self.api("POST", "/v1/music/generations", self.k_music, {"prompt": "x"})
        self.assertEqual((status, body["error"]["code"]), (429, "rate_limited"))
        self.assertEqual(self.stub.jobs, {})
        self.assertEqual(self.api("GET", "/v1/music/jobs", self.k_music)[0], 200)

    def test_rate_limit_counts_requests(self):
        from gx_control_ui import routes_v2

        with mock.patch.object(routes_v2._rate_ok, "__defaults__", (3,)):
            codes = [self.api("GET", "/v1/music/model", self.k_music)[0] for _ in range(4)]
        self.assertEqual(codes, [200, 200, 200, 429])

    def test_upload(self):
        status, _, body = self.api("POST", "/v1/music/uploads", self.k_music, raw_body=_WAV,
                                   headers={"Content-Type": "audio/wav", "X-Filename": "take.wav"})
        self.assertEqual(status, 201, body)
        self.assertRegex(body["id"], r"^upl-[0-9a-f]{32}$")
        status, _, body = self.api("POST", "/v1/music/uploads", self.k_music, raw_body=b"",
                                   headers={"Content-Type": "audio/wav"})
        self.assertEqual(status, 413)
        status, _, _ = self.api("POST", "/v1/music/uploads", self.k_mini, raw_body=_WAV,
                                headers={"Content-Type": "audio/wav"})
        self.assertEqual(status, 403)
        self.assertEqual(len(self.stub.uploads), 1)


class AudioFileAndLineageTests(V2Base):
    def setUp(self):
        super().setUp()
        self.login()
        lib = self.app.library
        mp3 = lib.tmp_file(".mp3")
        mp3.write_bytes(b"ID3-mp3-bytes")
        self.track = lib.add(NewAsset(type="audio", ext="wav", operation="generate", data=_WAV, title="Night/Drive",
                                      variant_paths={"mp3": mp3}))

    def test_audio_formats(self):
        tid = self.track["id"]
        status, headers, data = self.req("GET", f"/api/media/assets/{tid}/file?format=mp3")
        self.assertEqual((status, headers["Content-Type"], data), (200, "audio/mpeg", b"ID3-mp3-bytes"))
        self.assertTrue(headers["Content-Disposition"].startswith("inline;"))
        status, headers, data = self.req("GET", f"/api/media/assets/{tid}/file")
        self.assertEqual((status, headers["Content-Type"], data), (200, "audio/wav", _WAV))
        status, headers, _ = self.req("GET", f"/api/media/assets/{tid}/file?format=mp3&download=1")
        self.assertEqual(headers["Content-Disposition"], 'attachment; filename="Night-Drive.mp3"')
        status, headers, data = self.req("GET", f"/api/media/assets/{tid}/file?format=mp3",
                                         headers={"Range": "bytes=0-2"})
        self.assertEqual((status, data, headers["Content-Range"]), (206, b"ID3", "bytes 0-2/13"))
        status, _, body = self.req("GET", f"/api/media/assets/{tid}/file?format=flac")
        self.assertEqual((status, body["error"]["code"]), (404, "not_found"))
        for fmt in ("ogg", "..%2F..%2Fetc%2Fpasswd", "png"):
            status, _, body = self.req("GET", f"/api/media/assets/{tid}/file?format={fmt}")
            self.assertEqual(status, 404, fmt)
        status, _, body = self.req("GET", f"/api/media/assets/{tid}")
        self.assertEqual(body["stream_url"], f"/api/media/assets/{tid}/file?format=mp3")
        self.assertEqual(self.req("GET", f"/api/media/assets/{tid}/thumbnail")[0], 404)
        self.cookie = None
        self.assertEqual(self.req("GET", f"/api/media/assets/{tid}/file?format=mp3")[0], 401)

    def test_image_ignores_audio_formats(self):
        img = self.app.library.add(NewAsset(type="image", ext="png", operation="upload", data=PNG))
        status, _, _ = self.req("GET", f"/api/media/assets/{img['id']}/file?format=mp3")
        self.assertEqual(status, 404)
        status, headers, data = self.req("GET", f"/api/media/assets/{img['id']}/file?format=png")
        self.assertEqual((status, headers["Content-Type"], data), (200, "image/png", PNG))

    def test_delete_via_api_removes_variants(self):
        files = self.app.library.all_files(self.track)
        status, _, body = self.post("/api/media/delete", {"ids": [self.track["id"]], "confirm": True})
        self.assertEqual((status, body["deleted"]), (200, [self.track["id"]]))
        self.assertFalse(any(p.exists() for p in files))

    def test_lineage(self):
        lib = self.app.library
        remix = lib.add(NewAsset(type="audio", ext="wav", operation="remix", data=_WAV,
                                 parent_id=self.track["id"]))
        extend = lib.add(NewAsset(type="audio", ext="wav", operation="extend", data=_WAV, parent_id=remix["id"]))
        status, _, body = self.req("GET", f"/api/media/assets/{extend['id']}/lineage")
        self.assertEqual(status, 200)
        self.assertEqual(body["root"], self.track["id"])
        self.assertEqual([a["id"] for a in body["asset"]["ancestors"]], [remix["id"], self.track["id"]])
        self.assertEqual(body["tree"][0]["id"], remix["id"])
        self.assertEqual(body["tree"][0]["children"][0]["id"], extend["id"])
        self.assertEqual(body["tree"][0]["children"][0]["children"], [])
        status, _, body = self.req("GET", f"/api/media/assets/{self.track['id']}/lineage")
        self.assertEqual((body["root"], len(body["tree"])), (self.track["id"], 1))
        lib.delete([self.track["id"]])
        status, _, body = self.req("GET", f"/api/media/assets/{extend['id']}/lineage")
        self.assertEqual(status, 200)
        self.assertNotEqual(body["root"], self.track["id"])
        self.assertTrue(body["asset"]["ancestors"][-1]["deleted"])
        self.assertEqual(self.req("GET", f"/api/media/assets/{'a_' + '9' * 24}/lineage")[0], 404)
        self.assertEqual(self.req("GET", "/api/media/assets/a_nothex/lineage")[0], 404)
        self.cookie = None
        self.assertEqual(self.req("GET", f"/api/media/assets/{extend['id']}/lineage")[0], 401)


if __name__ == "__main__":
    unittest.main()
