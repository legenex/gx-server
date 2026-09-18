"""API tests against a real HTTP server bound to 127.0.0.1 on an ephemeral
port, in offline mode (no cluster access)."""

from __future__ import annotations

import http.client
import json
import threading
import time
import unittest

from support import TempEnv

from gx_control_ui import auth
from gx_control_ui import server as srv

PASSWORD = "Test-Password-For-Suite-1"


class ServerBase(unittest.TestCase):
    configure = True

    def setUp(self):
        self.env = TempEnv()
        if self.configure:
            auth.PasswordStore(self.env.cfg.password_file).set_password("admin", PASSWORD, n=2**10)
        self.app, servers = srv.build(self.env.cfg)
        self.httpd = servers[0]
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.cookie = None
        self.csrf = None

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.env.cleanup()

    def req(self, method, path, body=None, headers=None, raw_body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        hdrs = {"Host": f"127.0.0.1:{self.port}"}
        if self.cookie:
            hdrs["Cookie"] = self.cookie
        if body is not None:
            raw_body = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
        hdrs.update(headers or {})
        conn.request(method, path, body=raw_body, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        try:
            parsed = json.loads(data) if data and "json" in resp.getheader("Content-Type", "") else data
        except ValueError:
            parsed = data
        return resp.status, dict(resp.getheaders()), parsed

    def login(self, password=PASSWORD):
        status, headers, body = self.req("POST", "/api/login", {"username": "admin", "password": password})
        if status == 200:
            self.cookie = headers["Set-Cookie"].split(";")[0]
            self.csrf = body["csrf"]
        return status, headers, body

    def post(self, path, body=None, csrf=True, origin=True):
        headers = {}
        if csrf and self.csrf:
            headers["X-CSRF-Token"] = self.csrf
        if origin:
            headers["Origin"] = f"http://127.0.0.1:{self.port}"
        return self.req("POST", path, body if body is not None else {}, headers)


SESSION_ROUTES = ["/api/overview", "/api/nodes", "/api/cluster", "/api/models", "/api/jobs", "/api/actions",
                  "/api/logs", "/api/logs/orchestrator", "/api/system", "/api/docs", "/api/docs/getting-started",
                  "/api/playground/config", "/api/actions/jobs/0123456789abcdef"]


class TestPublic(ServerBase):
    def test_health_and_ready(self):
        status, headers, body = self.req("GET", "/api/health")
        self.assertEqual((status, body["status"]), (200, "ok"))
        status, _, body = self.req("GET", "/api/ready")
        self.assertEqual(status, 200, body)
        self.assertTrue(body["ready"])

    def test_security_headers_everywhere(self):
        for path in ("/api/health", "/", "/js/app.js", "/api/overview", "/nope.js"):
            _, headers, _ = self.req("GET", path)
            self.assertIn("default-src 'self'", headers.get("Content-Security-Policy", ""), path)
            self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
            self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
            self.assertEqual(headers.get("X-Frame-Options"), "DENY")
            self.assertEqual(headers.get("Referrer-Policy"), "no-referrer")
            self.assertNotIn("Python", headers.get("Server", ""))

    def test_static_and_spa_fallback(self):
        status, headers, body = self.req("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"GX Cluster Control", body)
        self.assertNotIn(b"<script>", body)  # no inline script (CSP)
        status, _, body2 = self.req("GET", "/models")
        self.assertEqual(body2, body)
        status, headers, _ = self.req("GET", "/css/app.css")
        self.assertTrue(headers["Content-Type"].startswith("text/css"))
        etag = headers["ETag"]
        status, _, _ = self.req("GET", "/css/app.css", headers={"If-None-Match": etag})
        self.assertEqual(status, 304)
        for bad in ("/../gx_control_ui/auth.py", "/js/../../gx_control_ui/auth.py", "/%2e%2e/etc/passwd", "/x.env"):
            status, _, _ = self.req("GET", bad)
            self.assertEqual(status, 404, bad)

    def test_static_contains_no_secret_material(self):
        for path, (data, _, _, _) in self.app.static.files.items():
            text = data.decode("utf-8", "ignore")
            self.assertNotRegex(text, r"sk-[A-Za-z0-9]{20,}", path)
            self.assertNotIn("LITELLM_MASTER_KEY", text, path)

    def test_session_status_unauthenticated(self):
        status, _, body = self.req("GET", "/api/session")
        self.assertEqual(body, {"authenticated": False, "configured": True})

    def test_unauthenticated_calls_rejected(self):
        for path in SESSION_ROUTES:
            status, _, body = self.req("GET", path)
            self.assertEqual(status, 401, path)
            self.assertEqual(body["error"]["code"], "unauthenticated")
        for path in ("/api/actions/system.refresh", "/api/models/gx-max/load", "/api/playground/chat",
                     "/api/logout"):
            status, _, _ = self.post(path, {"confirm": "gx-max"}, csrf=False)
            self.assertEqual(status, 401, path)

    def test_forged_cookie_rejected(self):
        self.cookie = "gxui_session=" + "A" * 43
        status, _, _ = self.req("GET", "/api/overview")
        self.assertEqual(status, 401)

    def test_methods_and_unknown(self):
        self.assertEqual(self.req("PUT", "/api/overview")[0], 405)
        self.assertEqual(self.req("DELETE", "/api/health")[0], 405)
        self.assertEqual(self.req("GET", "/api/shell")[0], 404)
        self.assertEqual(self.req("POST", "/api/health", {})[0], 405)


class TestLogin(ServerBase):
    def test_bad_login_then_good(self):
        status, _, body = self.login("wrong-password-xx")
        self.assertEqual(status, 401)
        self.assertNotIn("Set-Cookie", self.req("POST", "/api/login", {"username": "admin", "password": "nope"})[1])
        status, headers, body = self.login()
        self.assertEqual(status, 200)
        cookie = headers["Set-Cookie"]
        for flag in ("HttpOnly", "SameSite=Strict", "Path=/"):
            self.assertIn(flag, cookie)
        self.assertNotIn("Secure", cookie)  # plain HTTP over Tailscale; auto mode
        self.assertEqual(body["user"], "admin")
        self.assertNotIn(PASSWORD, json.dumps(body))

    def test_secure_cookie_behind_https_proxy(self):
        status, headers, _ = self.req("POST", "/api/login", {"username": "admin", "password": PASSWORD},
                                      headers={"X-Forwarded-Proto": "https"})
        self.assertIn("Secure", headers["Set-Cookie"])

    def test_malformed_login(self):
        for body in ({}, {"username": "admin"}, {"username": 1, "password": 2}):
            self.assertEqual(self.req("POST", "/api/login", body)[0], 400)
        self.assertEqual(self.req("POST", "/api/login", raw_body=b"{not json",
                                  headers={"Content-Type": "application/json"})[0], 400)
        self.assertEqual(self.req("POST", "/api/login", raw_body=b"username=a",
                                  headers={"Content-Type": "application/x-www-form-urlencoded"})[0], 400)

    def test_login_throttle(self):
        for _ in range(5):
            self.login("wrong-password-xx")
        status, _, body = self.login()
        self.assertEqual(status, 429)
        self.assertEqual(body["error"]["code"], "throttled")

    def test_cross_origin_login_refused(self):
        status, _, _ = self.req("POST", "/api/login", {"username": "admin", "password": PASSWORD},
                                headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 403)

    def test_logout(self):
        self.login()
        self.assertEqual(self.req("GET", "/api/overview")[0], 200)
        status, headers, _ = self.post("/api/logout")
        self.assertEqual(status, 200)
        self.assertIn("Max-Age=0", headers["Set-Cookie"])
        self.assertEqual(self.req("GET", "/api/overview")[0], 401)

    def test_password_change_logs_everyone_out(self):
        self.login()
        self.assertEqual(self.req("GET", "/api/models")[0], 200)
        time.sleep(0.02)
        auth.PasswordStore(self.env.cfg.password_file).set_password("admin", "Brand-New-Password-2", n=2**10)
        self.assertEqual(self.req("GET", "/api/models")[0], 401)
        self.assertEqual(self.login(PASSWORD)[0], 401)
        self.assertEqual(self.login("Brand-New-Password-2")[0], 200)


class TestUnconfigured(ServerBase):
    configure = False

    def test_not_ready_and_login_refused(self):
        status, _, body = self.req("GET", "/api/ready")
        self.assertEqual(status, 503)
        self.assertIn("no admin password configured", body["problems"])
        status, _, body = self.login("anything-at-all")
        self.assertEqual(status, 503)
        self.assertEqual(self.req("GET", "/api/session")[2]["configured"], False)


class TestAuthenticatedApi(ServerBase):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.login()[0], 200)

    def test_all_pages_load(self):
        for path in SESSION_ROUTES[:-1]:
            status, headers, body = self.req("GET", path)
            self.assertEqual(status, 200, (path, body))
            self.assertEqual(headers.get("Cache-Control"), "no-store")

    def test_models_lists_the_canonical_eleven_aliases(self):
        # L-10 as amended by D-036 (gx-music) and D-040 (gx-voice, gx-call, gx-live).
        # The last three are specialized services, not LiteLLM chat models, and the
        # Models page must still represent them.
        _, _, body = self.req("GET", "/api/models")
        aliases = [m["alias"] for m in body["models"]]
        self.assertEqual(aliases, ["gx-mini", "gx-fast", "gx-reason", "gx-max", "gx-auto", "gx-image", "gx-video",
                                   "gx-music", "gx-voice", "gx-call", "gx-live"])
        self.assertEqual(len(aliases), len(set(aliases)), "no alias may appear twice")
        music = next(m for m in body["models"] if m["alias"] == "gx-music")
        self.assertEqual(music["repository"], "ACE-Step/acestep-v15-xl-turbo")
        self.assertEqual(music["revision"], "d4a0b288b83ebb7e25a8c0b32c573c22e134e8ee")
        self.assertEqual(music["task"], "music-generation")
        self.assertEqual(set(music["actions"]), {"load", "unload"})
        gx = next(m for m in body["models"] if m["alias"] == "gx-max")
        self.assertEqual(gx["model"], "dealignai/DeepSeek-V4-Flash-0731-CRACK-NVFP4")  # D-032
        self.assertEqual(gx["topology"]["tp"], 2)
        self.assertEqual(gx["topology"]["nnodes"], 2)
        self.assertEqual(set(gx["actions"]), {"load", "unload", "restart", "force_release"})
        self.assertEqual(gx["actions"]["load"]["confirm_phrase"], "gx-max")
        self.assertNotIn("gx-vision", json.dumps(body))

    def test_overview_shape(self):
        _, _, body = self.req("GET", "/api/overview")
        for key in ("overall", "nodes", "services", "rails", "tailscale", "gxmax", "git", "queue", "problems",
                    "locks", "ledger", "models"):
            self.assertIn(key, body)
        self.assertEqual([n["name"] for n in body["nodes"]], ["gx10-01", "gx10-02"])
        self.assertEqual(len(body["rails"]), 2)

    def test_csrf_and_origin_enforced(self):
        status, _, body = self.post("/api/actions/system.refresh", csrf=False)
        self.assertEqual((status, body["error"]["code"]), (403, "csrf"))
        self.csrf, good = "bogus", self.csrf
        self.assertEqual(self.post("/api/actions/system.refresh")[0], 403)
        self.csrf = good
        headers = {"X-CSRF-Token": good, "Origin": "http://evil.example"}
        self.assertEqual(self.req("POST", "/api/actions/system.refresh", {}, headers)[0], 403)
        headers = {"X-CSRF-Token": good, "Referer": "http://evil.example/page"}
        self.assertEqual(self.req("POST", "/api/actions/system.refresh", {}, headers)[0], 403)
        status, _, body = self.post("/api/actions/system.refresh")
        self.assertEqual(status, 202, body)

    def test_session_csrf_is_the_live_token(self):
        status, _, body = self.req("GET", "/api/session")
        self.assertEqual(status, 200)
        self.assertEqual(body["csrf"], self.csrf)
        self.csrf = body["csrf"]
        status, _, out = self.post("/api/logout")
        self.assertEqual(status, 200, out)

    def test_action_errors(self):
        self.assertEqual(self.post("/api/actions/shell")[0], 404)
        self.assertEqual(self.post("/api/actions/..%2f..")[0], 404)
        status, _, body = self.post("/api/models/gx-max/load", {})
        self.assertEqual(status, 400)
        self.assertIn("gx-max", body["error"]["message"])
        self.assertEqual(self.post("/api/models/gx-vision/load")[0], 404)
        self.assertEqual(self.post("/api/models/gx-max/exec")[0], 404)

    def test_body_limits(self):
        huge = {"confirm": "x" * (srv.MAX_BODY + 10)}
        status, _, body = self.post("/api/actions/system.refresh", huge)
        self.assertEqual(status, 413)
        status, _, _ = self.req("POST", "/api/actions/system.refresh", raw_body=b"[1,2]",
                                headers={"Content-Type": "application/json", "X-CSRF-Token": self.csrf})
        self.assertEqual(status, 400)

    def test_logs_endpoint(self):
        status, _, body = self.req("GET", "/api/logs")
        self.assertGreaterEqual(len(body["streams"]), 20)
        self.assertEqual(self.req("GET", "/api/logs/not-a-stream")[0], 404)
        self.assertEqual(self.req("GET", "/api/logs/..%2F..%2Fetc%2Fpasswd")[0], 404)
        status, headers, _ = self.req("GET", "/api/logs/rank1?lines=10&format=text")
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["Content-Disposition"])

    def test_docs_endpoints(self):
        _, _, body = self.req("GET", "/api/docs")
        self.assertGreaterEqual(len(body["pages"]), 6)
        _, _, page = self.req("GET", "/api/docs/models")
        self.assertIn("gx-max", page["html"])
        _, _, res = self.req("GET", "/api/docs?q=deadman")
        self.assertTrue(res["results"])
        self.assertEqual(self.req("GET", "/api/docs/..%2f")[0], 404)

    def test_playground_validation_via_api(self):
        status, _, body = self.post("/api/playground/chat", {"model": "gpt-4", "prompt": "hi"})
        self.assertEqual(status, 400)
        status, _, body = self.post("/api/playground/video", {"prompt": ""})
        self.assertEqual(status, 400)
        self.assertEqual(self.req("GET", "/api/playground/video/..%2fx")[0], 404)
        # A gateway-encoded id is routed (it reaches the upstream, which is down here).
        encoded = "video_" + "bGl0ZWxsbTpjdXN0b21fbGxtX3Byb3ZpZGVy" * 3 + "=="
        self.assertNotEqual(self.req("GET", f"/api/playground/video/{encoded}")[0], 404)

    def test_system_view_hides_secret_values(self):
        _, _, body = self.req("GET", "/api/system")
        for row in body["secrets"]:
            self.assertEqual(set(row), {"name", "state"})
        self.assertEqual(body["kernel_pin"], "6.17.0-1032-nvidia")
        names = [a["name"] for a in body["actions"]]
        self.assertIn("system.integrity_audit", names)
        self.assertFalse([n for n in names if "upgrade" in n or "firmware" in n or "kernel_update" in n])


class TestKeepAlive(ServerBase):
    """Regression: an unread POST body used to poison the next request on a
    keep-alive connection ("{}GET ..." -> 501)."""

    def test_body_consumed_on_every_post(self):
        self.login()
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        base = {"Host": f"127.0.0.1:{self.port}", "Cookie": self.cookie, "Content-Type": "application/json"}
        # rejected before the handler runs (no CSRF), then an accepted logout
        for headers in ({}, {"X-CSRF-Token": self.csrf}):
            conn.request("POST", "/api/logout", body=b'{"x": 1}', headers={**base, **headers})
            resp = conn.getresponse()
            resp.read()
        self.assertEqual(resp.status, 200)
        conn.request("GET", "/api/overview", headers=base)
        resp = conn.getresponse()
        resp.read()
        self.assertEqual(resp.status, 401)
        conn.close()

    def test_oversized_body_closes_connection(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.putrequest("POST", "/api/login")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(srv.MAX_BODY + 1))
        conn.endheaders()
        resp = conn.getresponse()
        self.assertEqual(resp.status, 413)
        self.assertEqual(resp.getheader("Connection"), "close")
        conn.close()


class TestRangeParsing(unittest.TestCase):
    def test_parse_range(self):
        pr = srv.parse_range
        self.assertIsNone(pr(None, 100))
        self.assertIsNone(pr("bytes=0-1,5-6", 100))
        self.assertEqual(pr("bytes=0-", 100), (0, 99))
        self.assertEqual(pr("bytes=10-19", 100), (10, 19))
        self.assertEqual(pr("bytes=90-500", 100), (90, 99))
        self.assertEqual(pr("bytes=-10", 100), (90, 99))
        self.assertEqual(pr("bytes=100-", 100), "invalid")
        self.assertEqual(pr("bytes=5-2", 100), "invalid")
        self.assertIsNone(pr("bytes=a-b", 100))


class TestBindSafety(unittest.TestCase):
    def test_wildcard_bind_refused(self):
        from gx_control_ui.config import _resolve_hosts
        for bad in ("0.0.0.0", "127.0.0.1,0.0.0.0", "::"):
            with self.assertRaises(ValueError):
                _resolve_hosts(bad)
        self.assertEqual(_resolve_hosts("127.0.0.1, 127.0.0.1"), ("127.0.0.1",))


if __name__ == "__main__":
    unittest.main()


class TestAcceptanceAccount(ServerBase):
    """D-035: a second, loopback-only account for automated live tests."""

    def setUp(self):
        super().setUp()
        # Generated at run time: no credential-shaped literal in the repository.
        self.acc_pw = "Acc-" + __import__("secrets").token_hex(8)
        auth.PasswordStore(self.env.cfg.acceptance_file).set_password("acceptance", self.acc_pw, n=2**10)

    def test_signs_in_from_loopback(self):
        status, _, body = self.req("POST", "/api/login", {"username": "acceptance", "password": self.acc_pw})
        self.assertEqual(status, 200)
        self.assertEqual(body["user"], "acceptance")

    def test_refused_from_any_other_address(self):
        original = srv.Handler._client_ip
        srv.Handler._client_ip = lambda self: "100.104.35.71"
        try:
            status, _, _ = self.req("POST", "/api/login", {"username": "acceptance", "password": self.acc_pw})
        finally:
            srv.Handler._client_ip = original
        self.assertEqual(status, 401)

    def test_is_not_the_admin_credential(self):
        status, _, _ = self.req("POST", "/api/login", {"username": "admin", "password": self.acc_pw})
        self.assertEqual(status, 401)
        status, _, _ = self.req("POST", "/api/login", {"username": "acceptance", "password": PASSWORD})
        self.assertEqual(status, 401)
