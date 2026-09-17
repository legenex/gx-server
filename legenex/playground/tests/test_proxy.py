"""GX-Playground proxy: allow-list, origin checks, header hygiene, streaming.

Hermetic: a stub backend on 127.0.0.1 records what the proxy forwards.
"""

from __future__ import annotations

import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))


class Backend(BaseHTTPRequestHandler):
    calls: list = []

    def log_message(self, *a):
        pass

    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        Backend.calls.append({"method": self.command, "path": self.path, "headers": dict(self.headers),
                              "body": body})
        if self.path.startswith("/api/health"):
            payload = json.dumps({"status": "ok", "version": "test"}).encode()
        elif self.path.startswith("/api/big"):
            payload = b"x" * (3 << 20)
        else:
            payload = json.dumps({"ok": True, "path": self.path, "len": len(body)}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Set-Cookie", "gxui_session=abc; HttpOnly; SameSite=Strict; Path=/")
        self.send_header("X-Internal-Secret", "must-not-leak")
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = do_DELETE = _handle  # noqa: N815


class ProxyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.backend = ThreadingHTTPServer(("127.0.0.1", 0), Backend)
        threading.Thread(target=cls.backend.serve_forever, daemon=True).start()
        cls.tmp = tempfile.TemporaryDirectory()
        token = Path(cls.tmp.name) / "proxy-token"
        token.write_text("t" * 40 + "\n")
        web = Path(cls.tmp.name) / "web"
        web.mkdir()
        (web / "index.html").write_text("<!doctype html><title>pg</title>")
        (web / "app.js").write_text("console.log(1);\n" * 200)
        os.environ["GX_PG_UPSTREAM"] = f"http://127.0.0.1:{cls.backend.server_address[1]}"
        os.environ["GX_PG_PROXY_TOKEN_FILE"] = str(token)
        import importlib

        import gx_playground.server as srv
        cls.srv = importlib.reload(srv)
        cls.servers = cls.srv.build(["127.0.0.1"], 0, web)
        cls.port = cls.servers[0].server_address[1]
        threading.Thread(target=cls.servers[0].serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.servers[0].shutdown()
        cls.backend.shutdown()
        cls.tmp.cleanup()

    def setUp(self):
        Backend.calls.clear()

    def req(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, body=body, headers=headers or {})
        res = conn.getresponse()
        data = res.read()
        conn.close()
        return res.status, dict(res.getheaders()), data

    def test_static_and_spa_routes_have_security_headers(self):
        for path in ("/", "/music", "/library/a_x", "/images"):
            st, h, body = self.req("GET", path)
            self.assertEqual(st, 200, path)
            self.assertIn("<title>pg</title>", body.decode())
            self.assertIn("script-src 'self'", h["Content-Security-Policy"])
            self.assertEqual(h["X-Frame-Options"], "DENY")
        self.assertEqual(self.req("GET", "/etc/passwd")[0], 404)
        self.assertEqual(self.req("GET", "/../README.md")[0], 404)

    def test_allow_list(self):
        allowed = [("GET", "/api/session"), ("GET", "/api/media/assets"), ("GET", "/api/music/jobs"),
                   ("GET", "/api/resources/summary"), ("GET", "/api/creative/overview"),
                   ("GET", "/api/music/jobs/mus-" + "a" * 32 + "/lineage"), ("GET", "/v1/music/model"),
                   ("GET", "/api/music/reference/" + "ab" * 12)]
        for method, path in allowed:
            self.assertEqual(self.req(method, path)[0], 200, path)
        denied = [("GET", "/api/keys"), ("GET", "/api/resources"), ("GET", "/api/storage"),
                  ("GET", "/api/system"), ("GET", "/api/logs/orchestrator"), ("GET", "/api/manager/inventory"),
                  ("GET", "/api/setup"), ("GET", "/api/actions"), ("GET", "/api/playground/config"),
                  ("GET", "/api/music/../keys"), ("GET", "/v1/chat/completions"),
                  # MUS: only the exact AI/reference paths, with the right method and id shape
                  ("GET", "/api/music/ai/build"), ("GET", "/api/music/reference/analyze"),
                  ("GET", "/api/music/reference/XYZ"), ("GET", "/api/music/reference/" + "ab" * 12 + "/x"),
                  ("GET", "/api/music/ai/../../keys")]
        for method, path in denied:
            self.assertEqual(self.req(method, path)[0], 404, path)
        self.assertEqual(Backend.calls and [c["path"] for c in Backend.calls if "keys" in c["path"]], [])
        self.assertEqual(self.req("PUT", "/api/session")[0], 405)

    def test_allow_list_video_loras(self):
        """Build V3 WAN: only the exact /api/video paths, methods and id shapes."""
        entry, preset, gen = "l_" + "0a" * 8, "wp_" + "1b" * 8, "2c" * 8
        origin = {"Content-Type": "application/json", "Host": f"127.0.0.1:{self.port}",
                  "Origin": f"http://127.0.0.1:{self.port}"}
        for path in ("/api/video/config", "/api/video/loras", f"/api/video/loras/{entry}", "/api/video/presets",
                     f"/api/video/presets/{preset}", "/api/video/generations", f"/api/video/generations/{gen}",
                     f"/api/video/generations/{gen}/workflow", "/api/video/errors"):
            self.assertEqual(self.req("GET", path)[0], 200, path)
        for path in ("/api/video/loras/rescan", "/api/video/loras/order", f"/api/video/loras/{entry}",
                     "/api/video/pairs", "/api/video/pairs/remove", "/api/video/pairs/restore", "/api/video/presets",
                     f"/api/video/presets/{preset}", f"/api/video/presets/{preset}/duplicate",
                     f"/api/video/presets/{preset}/delete", f"/api/video/presets/{preset}/resolve",
                     "/api/video/workflow", "/api/video/generate", f"/api/video/jobs/{gen}/cancel"):
            self.assertEqual(self.req("POST", path, b"{}", origin)[0], 200, path)
        denied = [("GET", "/api/video/generate"), ("GET", "/api/video/loras/rescan"),
                  ("POST", "/api/video/config"), ("POST", "/api/video/generations"),
                  ("GET", "/api/video/loras/../../keys"), ("GET", "/api/video/loras/l_XYZ"),
                  ("GET", "/api/video/presets/wp_" + "1b" * 8 + "/delete"),
                  ("GET", f"/api/video/generations/{gen}/workflow/x"), ("GET", "/api/video/loras/%2e%2e"),
                  ("POST", "/api/video/presets/wp_x/delete"), ("POST", "/api/video/jobs/zz/cancel")]
        for method, path in denied:
            st = self.req(method, path, b"{}" if method == "POST" else None, origin if method == "POST" else None)[0]
            self.assertEqual(st, 404, f"{method} {path}")

    def test_allow_list_voice(self):
        """Build V3 VOI: only the exact /api/voice and /v1/voice paths, methods and id shapes."""
        voice, job = "vc_" + "0a" * 12, "vj_" + "1b" * 16
        origin = {"Content-Type": "application/json", "Host": f"127.0.0.1:{self.port}",
                  "Origin": f"http://127.0.0.1:{self.port}"}
        for path in ("/api/voice/model", "/api/voice/voices", "/api/voice/jobs", f"/api/voice/voices/{voice}",
                     "/api/voice/voices/preset:uncle_fu", f"/api/voice/voices/{voice}/versions",
                     f"/api/voice/jobs/{job}", f"/api/voice/jobs/{job}/takes/3/audio", "/v1/voice/model",
                     "/v1/voice/voices", "/v1/voice/voices/preset:ryan", f"/v1/voice/jobs/{job}",
                     f"/v1/voice/jobs/{job}/takes/0/content"):
            self.assertEqual(self.req("GET", path)[0], 200, path)
        for path in ("/api/voice/voices", "/api/voice/jobs", "/api/voice/upload", f"/api/voice/voices/{voice}",
                     f"/api/voice/voices/{voice}/delete", f"/api/voice/jobs/{job}/cancel",
                     f"/api/voice/jobs/{job}/delete", f"/api/voice/jobs/{job}/takes/1/save", "/v1/voice/speech",
                     "/v1/voice/design", "/v1/voice/clone", "/v1/voice/dialogue", "/v1/voice/uploads",
                     f"/v1/voice/jobs/{job}/cancel", f"/v1/voice/jobs/{job}/takes/2/save"):
            self.assertEqual(self.req("POST", path, b"{}", origin)[0], 200, path)
        denied = [("POST", "/api/voice/load"), ("POST", "/api/voice/unload"), ("POST", "/v1/voice/unload"),
                  ("POST", "/v1/voice/load"), ("GET", "/api/voice/jobs/vj_x"), ("GET", f"/api/voice/jobs/{job}/takes/4/audio"),
                  ("POST", "/api/voice/voices/preset:ryan"), ("POST", "/api/voice/voices/preset:ryan/delete"),
                  ("GET", "/api/voice/voices/../../keys"), ("DELETE", f"/v1/voice/jobs/{job}"),
                  ("GET", f"/v1/voice/jobs/{job}/takes/0/content/x"), ("GET", "/v1/audio/speech"),
                  ("POST", "/v1/audio/speech"), ("GET", "/api/voice/voices/vc_ZZ")]
        for method, path in denied:
            st = self.req(method, path, b"{}" if method == "POST" else None, origin if method == "POST" else None)[0]
            self.assertEqual(st, 404, f"{method} {path}")

    def test_post_requires_same_origin(self):
        body = b"{}"
        base = {"Content-Type": "application/json", "Host": f"127.0.0.1:{self.port}"}
        st, _, _ = self.req("POST", "/api/resources/profile", body, {**base, "Origin": "http://evil.example"})
        self.assertEqual(st, 403)
        st, _, _ = self.req("POST", "/api/resources/profile", body, base)  # no Origin/Referer at all
        self.assertEqual(st, 403)
        st, _, _ = self.req("POST", "/api/resources/profile", body,
                            {**base, "Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(st, 200)
        # API clients (no browser) may POST to the key-authenticated music API
        st, _, _ = self.req("POST", "/v1/music/generations", body, base)
        self.assertEqual(st, 200)

    def test_forwarded_headers_and_hygiene(self):
        st, h, _ = self.req("GET", "/api/session", headers={
            "Cookie": "gxui_session=abc", "X-GX-Proxy-Token": "forged", "X-GX-Forwarded-For": "6.6.6.6",
            "Authorization": "Bearer should-not-pass", "X-CSRF-Token": "c"})
        call = Backend.calls[-1]
        hd = {k.lower(): v for k, v in call["headers"].items()}
        self.assertEqual(hd["x-gx-proxy-token"], "t" * 40)
        self.assertEqual(hd["x-gx-forwarded-for"], "127.0.0.1")
        self.assertEqual(hd["cookie"], "gxui_session=abc")
        self.assertEqual(hd["x-csrf-token"], "c")
        self.assertNotIn("authorization", hd)
        self.assertIn("gxui_session", h.get("Set-Cookie", ""))
        self.assertNotIn("X-Internal-Secret", h)

    def test_public_api_gets_authorization_but_never_cookies(self):
        self.req("GET", "/v1/music/jobs", headers={"Authorization": "Bearer k", "Cookie": "gxui_session=abc"})
        hd = {k.lower(): v for k, v in Backend.calls[-1]["headers"].items()}
        self.assertEqual(hd["authorization"], "Bearer k")
        self.assertNotIn("cookie", hd)

    def test_streams_large_bodies_both_ways(self):
        data = os.urandom(5 << 20)
        st, _, body = self.req("POST", "/api/media/upload", data, {
            "Content-Type": "image/png", "Origin": f"http://127.0.0.1:{self.port}",
            "Host": f"127.0.0.1:{self.port}"})
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(body)["len"], len(data))
        self.srv.ALLOW = self.srv.ALLOW + ((frozenset({"GET"}), __import__("re").compile(r"/api/big")),)
        try:
            st, h, body = self.req("GET", "/api/big")
            self.assertEqual(len(body), 3 << 20)
        finally:
            self.srv.ALLOW = self.srv.ALLOW[:-1]

    def test_oversize_and_chunked_bodies_refused(self):
        st, _, _ = self.req("POST", "/api/media/upload", b"", {
            "Content-Length": str(self.srv.MAX_BODY + 1), "Origin": f"http://127.0.0.1:{self.port}",
            "Host": f"127.0.0.1:{self.port}"})
        self.assertEqual(st, 413)

    def test_health_reports_upstream(self):
        st, _, body = self.req("GET", "/pg/health")
        self.assertEqual(st, 200)
        self.assertTrue(json.loads(body)["upstream"]["ok"])

    def test_wildcard_bind_refused(self):
        with self.assertRaises(ValueError):
            self.srv.resolve_hosts("127.0.0.1,0.0.0.0")


if __name__ == "__main__":
    unittest.main()
