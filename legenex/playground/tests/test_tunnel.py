"""Realtime tunnel, end to end and hermetic (plt.md section 1).

    this test --WS--> REAL gx-playground --authorize--> REAL gx-control-ui (offline)
                             '--WS--> stub node-2 service on 127.0.0.1

Covers: handshake + splice, header rewrite (bearer key in, cookies out), cookie
and ticket authorisation, origin mismatch, ticket reuse/expiry/other session,
foreign sessions, oversize and unmasked frames, idle timeout, per-owner caps,
replacement, upstream failures, path validation, the HTTPS listener with a
WSS tunnel and its Secure cookie, and the CA download.
"""

from __future__ import annotations

import http.client
import importlib
import json
import os
import secrets
import ssl
import sys
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
UI = HERE.parent / "control-ui"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(UI / "tests"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import TempEnv  # noqa: E402
from wsutil import Client, StubService  # noqa: E402

from gx_control_ui import auth  # noqa: E402
from gx_control_ui import server as cc  # noqa: E402
from gx_control_ui.realtime import Target, new_session_id  # noqa: E402

PASSWORD = "Tunnel-" + secrets.token_hex(8)


def _post(port: int, path: str, body: dict, headers: dict | None = None, *, tls: ssl.SSLContext | None = None):
    data = json.dumps(body).encode()
    conn = (http.client.HTTPSConnection("127.0.0.1", port, context=tls, timeout=10) if tls
            else http.client.HTTPConnection("127.0.0.1", port, timeout=10))
    scheme = "https" if tls else "http"
    conn.request("POST", path, body=data, headers={"Content-Type": "application/json",
                                                   "Origin": f"{scheme}://127.0.0.1:{port}", **(headers or {})})
    res = conn.getresponse()
    out = res.status, dict(res.getheaders()), res.read(), res.headers.get_all("Set-Cookie") or []
    conn.close()
    return out


class TunnelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = TempEnv()
        root = cls.env.root
        cls.key = secrets.token_urlsafe(32)
        cls.stub = StubService(cls.key)
        for svc in ("call", "live"):
            d = root / "secrets" / f"gx-{svc}"
            d.mkdir(parents=True, exist_ok=True)
            (d / "api-key").write_text(cls.key + "\n")
            os.chmod(d / "api-key", 0o600)
        store = auth.PasswordStore(cls.env.cfg.password_file)
        store.set_password("admin", PASSWORD, n=2**10)
        cls.cc_app, cls.cc_servers = cc.build(cls.env.cfg)
        targets = {"call": Target("127.0.0.1", cls.stub.port, root / "secrets" / "gx-call" / "api-key"),
                   "live": Target("127.0.0.1", cls.stub.port, root / "secrets" / "gx-live" / "api-key")}
        cls.cc_app.realtime.targets = targets
        cls.cc_port = cls.cc_servers[0].server_address[1]
        threading.Thread(target=cls.cc_servers[0].serve_forever, daemon=True).start()

        cls.tls_dir = root / "tls"
        os.environ["GX_PG_UPSTREAM"] = f"http://127.0.0.1:{cls.cc_port}"
        os.environ["GX_PG_PROXY_TOKEN_FILE"] = str(cls.env.cfg.proxy_token_file)
        os.environ["GX_PG_TLS_DIR"] = str(cls.tls_dir)
        import gx_playground.server as pg
        cls.pg = importlib.reload(pg)
        cls.pg.tlsmod.ensure(cls.tls_dir, ("gx10-01", "127.0.0.1"))
        web = root / "web"
        web.mkdir()
        (web / "index.html").write_text("<!doctype html><title>pg</title>")
        cls.servers = cls.pg.build(["127.0.0.1"], 0, web, tls_port=0)
        cls.port = cls.servers[0].server_address[1]
        # the TLS listener on its own ephemeral port, sharing the handler state
        cls.pg_tls = cls.pg.TLSServer(("127.0.0.1", 0), type(
            "TLSH", (cls.servers[0].RequestHandlerClass,), {"scheme": "https"}),
            cls.pg.tlsmod.server_context(cls.tls_dir))
        cls.tls_port = cls.pg_tls.server_address[1]
        for s in (cls.servers[0], cls.pg_tls):
            threading.Thread(target=s.serve_forever, daemon=True).start()
        cls.client_tls = ssl.create_default_context(cafile=str(cls.tls_dir / "ca.crt"))
        cls.limits = cls.pg.LIMITS
        status, _, body, cookies = _post(cls.port, "/api/login", {"username": "admin", "password": PASSWORD})
        assert status == 200, body
        cls.cookie = cookies[0].split(";")[0]

    @classmethod
    def tearDownClass(cls):
        for s in (cls.servers[0], cls.pg_tls, cls.cc_servers[0]):
            s.shutdown()
        cls.stub.stop()
        cls.env.cleanup()

    def setUp(self):
        # every test starts with no open tunnel (the previous test's close is asynchronous)
        deadline = time.time() + 10
        while self.servers[0].RequestHandlerClass.tunnels.count() and time.time() < deadline:
            time.sleep(0.05)

    def tearDown(self):
        self.pg.LIMITS = self.limits

    # ------------------------------------------------------------ helpers
    def session(self, service="call", owner="user:admin", suffix=""):
        sid = new_session_id(service)
        self.cc_app.realtime.register(service, sid, owner=owner,
                                      upstream_path=f"/v1/{service}/sessions/{sid}/ws{suffix}", ttl_s=600)
        return sid

    def open(self, sid, service="call", **kw):
        kw.setdefault("cookie", self.cookie)
        return Client(kw.pop("port", self.port), f"/rt/{service}/{sid}" + kw.pop("query", ""), **kw)

    # ------------------------------------------------------------ happy path
    def test_handshake_splice_and_header_rewrite(self):
        sid = self.session()
        c = self.open(sid, protocols=["gx.v1", "other"],
                      extra={"Authorization": "Bearer client-supplied", "X-GX-Proxy-Token": "forged"})
        self.assertEqual(c.status, 101, c.body)
        self.assertEqual(c.headers["sec-websocket-protocol"], "gx.v1")
        self.assertNotIn("sec-websocket-extensions", c.headers)
        c.send(0x1, b"hello")
        self.assertEqual(c.recv(), (0x1, b"hello"))
        big = os.urandom(200_000)
        c.send(0x2, big)
        self.assertEqual(c.recv(), (0x2, big))
        c.send(0x9, b"p")
        self.assertEqual(c.recv(), (0xA, b"p"))
        req = self.stub.requests[-1]
        self.assertEqual(req["path"], f"/v1/call/sessions/{sid}/ws")
        self.assertEqual(req["headers"]["authorization"], f"Bearer {self.key}")
        self.assertEqual(req["headers"]["x-gx-session"], sid)
        self.assertNotIn("cookie", req["headers"])
        self.assertNotIn("x-gx-proxy-token", req["headers"])
        self.assertNotIn("sec-websocket-extensions", req["headers"])
        self.assertRegex(req["headers"]["x-gx-owner"], r"^[0-9a-f]{16}$")
        c.send(0x8, (1000).to_bytes(2, "big"))
        self.assertEqual(c.close_code(), 1000)
        c.close()
        time.sleep(0.3)
        self.assertEqual(self.stub.closes[-1], 1000)
        self.assertEqual(self.cc_app.realtime.get(sid).connects, 1)

    def test_upstream_first_bytes_are_relayed(self):
        sid = self.session(suffix="/hello")
        c = self.open(sid)
        self.assertEqual(c.status, 101)
        self.assertEqual(c.recv(), (0x1, b"hello from node 2"))
        c.close()

    def test_live_service(self):
        sid = self.session("live")
        c = self.open(sid, service="live")
        self.assertEqual(c.status, 101)
        c.send(0x1, b"frame")
        self.assertEqual(c.recv(), (0x1, b"frame"))
        c.close()

    # ------------------------------------------------------------ auth failures
    def test_no_cookie_is_401(self):
        c = self.open(self.session(), cookie=None)
        self.assertEqual(c.status, 401)
        self.assertEqual(json.loads(c.body)["error"]["code"], "unauthenticated")

    def test_origin_mismatch_and_missing_origin(self):
        sid = self.session()
        c = self.open(sid, origin="http://evil.example")
        self.assertEqual((c.status, json.loads(c.body)["error"]["code"]), (403, "bad_origin"))
        c = self.open(sid, origin=None)
        self.assertEqual(c.status, 403)

    def test_foreign_and_unknown_sessions_are_404(self):
        sid = self.session(owner="user:someone")
        self.assertEqual(self.open(sid).status, 404)
        self.assertEqual(self.open(new_session_id("call")).status, 404)

    def test_ended_session_is_410(self):
        sid = self.session()
        self.cc_app.realtime.end(sid, "completed")
        self.assertEqual(self.open(sid).status, 410)

    def test_ticket_single_use_and_binding(self):
        owner = "key:" + "ab" * 8
        sid = self.session(owner=owner)
        other = self.session(owner=owner)
        ticket = self.cc_app.realtime.issue_ticket(sid, owner=owner)
        c = self.open(sid, cookie=None, origin=None, query=f"?ticket={ticket}")
        self.assertEqual(c.status, 101, c.body)
        c.send(0x1, b"api")
        self.assertEqual(c.recv(), (0x1, b"api"))
        c.close()
        again = self.open(sid, cookie=None, origin=None, query=f"?ticket={ticket}")
        self.assertEqual((again.status, json.loads(again.body)["error"]["code"]), (401, "ticket_used"))
        t2 = self.cc_app.realtime.issue_ticket(sid, owner=owner)
        wrong = self.open(other, cookie=None, origin=None, query=f"?ticket={t2}")
        self.assertEqual(wrong.status, 401)
        forged = self.open(sid, cookie=None, origin=None, query=f"?ticket={t2[:-2]}AA")
        self.assertEqual(forged.status, 401)
        # a cookie never substitutes for the ticket's owner
        self.assertEqual(self.open(sid).status, 404)

    def test_ticket_expiry(self):
        owner = "key:" + "cd" * 8
        sid = self.session(owner=owner)
        reg = self.cc_app.realtime
        real = reg._clock
        ticket = reg.issue_ticket(sid, owner=owner)
        reg._clock = lambda: real() + 61
        try:
            c = self.open(sid, cookie=None, origin=None, query=f"?ticket={ticket}")
        finally:
            reg._clock = real
        self.assertEqual((c.status, json.loads(c.body)["error"]["code"]), (401, "ticket_expired"))

    def test_bad_query_and_paths(self):
        sid = self.session()
        self.assertEqual(self.open(sid, query="?ticket=%00%01").status, 401)
        self.assertEqual(self.open(sid, query="?a=1&a=2&ticket=x&ticket=y").status, 400)
        self.assertEqual(Client(self.port, f"/rt/live/{sid}", cookie=self.cookie).status, 404)
        self.assertEqual(Client(self.port, "/rt/voice/call_" + "0" * 32, cookie=self.cookie).status, 404)
        self.assertEqual(Client(self.port, f"/rt/call/{sid}/x", cookie=self.cookie).status, 404)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", f"/rt/call/{sid}", headers={"Cookie": self.cookie})
        self.assertEqual(conn.getresponse().status, 426)
        conn.close()
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", f"/rt/call/{sid}", body=b"{}")
        self.assertEqual(conn.getresponse().status, 405)
        conn.close()

    def test_bad_client_key_is_400(self):
        import socket
        sid = self.session()
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        s.sendall((f"GET /rt/call/{sid} HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nUpgrade: websocket\r\n"
                   "Connection: Upgrade\r\nSec-WebSocket-Key: bad\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        self.assertIn(b" 400 ", s.recv(200))
        s.close()

    def test_authorize_endpoint_is_not_browser_reachable(self):
        # through the Playground: not in the allow-list
        status, _, _, _ = _post(self.port, "/api/realtime/authorize", {"service": "call", "session_id": "x"})
        self.assertEqual(status, 404)
        # directly on the Control Center without the proxy token
        status, _, _, _ = _post(self.cc_port, "/api/realtime/authorize",
                                {"service": "call", "session_id": "x"}, {"Cookie": self.cookie})
        self.assertEqual(status, 404)
        status, _, _, _ = _post(self.cc_port, "/api/realtime/authorize",
                                {"service": "call", "session_id": "x"}, {"X-GX-Proxy-Token": "wrong" * 8})
        self.assertEqual(status, 404)

    # ------------------------------------------------------------ limits
    def test_oversize_frame_closes_1009(self):
        self.pg.LIMITS = self.pg.Limits(max_frame=1024)
        c = self.open(self.session())
        self.assertEqual(c.status, 101)
        c.send(0x2, b"x" * 2048)
        self.assertEqual(c.close_code(), 1009)
        c.close()

    def test_declared_huge_length_closes_1009(self):
        c = self.open(self.session())
        c.send(0x2, b"", length_override=1 << 40)
        self.assertEqual(c.close_code(), 1009)
        c.close()

    def test_unmasked_client_frame_closes_1002(self):
        c = self.open(self.session())
        c.send(0x1, b"not masked", masked=False)
        self.assertEqual(c.close_code(), 1002)
        c.close()

    def test_idle_timeout_closes_1001(self):
        self.pg.LIMITS = self.pg.Limits(idle_s=1)
        c = self.open(self.session(suffix="/silent"))
        self.assertEqual(c.status, 101)
        t0 = time.monotonic()
        self.assertEqual(c.close_code(), 1001)
        self.assertLess(time.monotonic() - t0, 6)
        c.close()

    def test_byte_limit(self):
        self.pg.LIMITS = self.pg.Limits(max_bytes=5000)
        c = self.open(self.session())
        c.send(0x2, b"y" * 3000)
        c.recv()
        c.send(0x2, b"y" * 3000)
        self.assertEqual(c.close_code(), 1009)
        c.close()

    def test_per_owner_cap(self):
        self.pg.LIMITS = self.pg.Limits(per_owner=2)
        clients = [self.open(self.session()) for _ in range(2)]
        self.assertEqual([c.status for c in clients], [101, 101])
        third = self.open(self.session())
        self.assertEqual((third.status, json.loads(third.body)["error"]["code"]), (429, "too_many_connections"))
        for c in clients:
            c.close()
        time.sleep(1.5)
        fourth = self.open(self.session())
        self.assertEqual(fourth.status, 101)
        fourth.close()

    def test_new_connection_replaces_old(self):
        sid = self.session()
        first = self.open(sid)
        self.assertEqual(first.status, 101)
        second = self.open(sid)
        self.assertEqual(second.status, 101)
        self.assertEqual(first.close_code(), 1001)
        second.send(0x1, b"still here")
        self.assertEqual(second.recv(), (0x1, b"still here"))
        first.close()
        second.close()

    def test_upstream_down_and_refusing(self):
        sid = self.session()
        reg = self.cc_app.realtime
        saved = reg.targets["call"]
        reg.targets["call"] = Target("127.0.0.1", 9, saved.key_file)
        try:
            c = self.open(sid)
        finally:
            reg.targets["call"] = saved
        self.assertEqual((c.status, json.loads(c.body)["error"]["code"]), (502, "upstream_unavailable"))
        c = self.open(self.session(suffix="/refuse404"))
        self.assertEqual(c.status, 404)

    def test_missing_service_key_is_503(self):
        sid = self.session()
        reg = self.cc_app.realtime
        saved = reg.targets["call"]
        reg.targets["call"] = Target("127.0.0.1", self.stub.port, self.env.root / "nope")
        try:
            c = self.open(sid)
        finally:
            reg.targets["call"] = saved
        self.assertEqual((c.status, json.loads(c.body)["error"]["code"]), (503, "realtime_disabled"))

    def test_disabled(self):
        self.pg.LIMITS = self.pg.Limits(enabled=False)
        self.assertEqual(self.open(self.session()).status, 503)

    def test_client_disconnect_ends_upstream(self):
        sid = self.session()
        c = self.open(sid)
        c.send(0x1, b"bye")
        c.recv()
        c.close()
        deadline = time.time() + 5
        while time.time() < deadline and self.servers[0].RequestHandlerClass.tunnels.count():
            time.sleep(0.1)
        self.assertEqual(self.servers[0].RequestHandlerClass.tunnels.count(), 0)

    # ------------------------------------------------------------ HTTPS
    def test_https_listener_wss_and_secure_cookie(self):
        conn = http.client.HTTPSConnection("127.0.0.1", self.tls_port, context=self.client_tls, timeout=10)
        conn.request("GET", "/")
        res = conn.getresponse()
        res.read()
        self.assertEqual(res.status, 200)
        self.assertEqual(res.getheader("Permissions-Policy"),
                         "camera=(self), microphone=(self), geolocation=(), payment=(), usb=()")
        self.assertIn("connect-src 'self'", res.getheader("Content-Security-Policy"))
        self.assertIsNone(res.getheader("Strict-Transport-Security"))
        conn.close()
        status, _, body, cookies = _post(self.tls_port, "/api/login",
                                         {"username": "admin", "password": PASSWORD}, tls=self.client_tls)
        self.assertEqual(status, 200, body)
        self.assertTrue(cookies[0].startswith("__Host-gxui_session="))
        self.assertIn("Secure", cookies[0])
        secure_cookie = cookies[0].split(";")[0]
        # the HTTP cookie is still accepted over HTTPS (sign-in carries over)
        conn = http.client.HTTPSConnection("127.0.0.1", self.tls_port, context=self.client_tls, timeout=10)
        conn.request("GET", "/api/session", headers={"Cookie": self.cookie})
        self.assertTrue(json.loads(conn.getresponse().read())["authenticated"])
        conn.close()
        # WSS through the TLS listener with the Secure cookie
        sid = self.session()
        c = Client(self.tls_port, f"/rt/call/{sid}", cookie=secure_cookie, tls_context=self.client_tls)
        self.assertEqual(c.status, 101, c.body)
        payload = os.urandom(300_000)
        c.send(0x2, payload)
        self.assertEqual(c.recv(), (0x2, payload))
        c.send(0x8, (1000).to_bytes(2, "big"))
        self.assertEqual(c.close_code(), 1000)
        c.close()
        # logout over HTTPS clears both cookies
        csrf = json.loads(body)["csrf"]
        status, _, _, cookies = _post(self.tls_port, "/api/logout", {}, {"Cookie": secure_cookie,
                                                                        "X-CSRF-Token": csrf},
                                      tls=self.client_tls)
        self.assertEqual(status, 200)
        names = sorted(c.split("=")[0] for c in cookies)
        self.assertEqual(names, ["__Host-gxui_session", "gxui_session"])

    def test_http_listener_keeps_plain_cookie(self):
        status, _, _, cookies = _post(self.port, "/api/login", {"username": "admin", "password": PASSWORD})
        self.assertEqual(status, 200)
        self.assertTrue(cookies[0].startswith("gxui_session="))
        self.assertNotIn("Secure", cookies[0])

    def test_forged_proto_header_from_a_client_is_ignored(self):
        status, _, _, cookies = _post(self.port, "/api/login", {"username": "admin", "password": PASSWORD},
                                      {"X-GX-Forwarded-Proto": "https"})
        self.assertEqual(status, 200)
        self.assertTrue(cookies[0].startswith("gxui_session="))

    def test_ca_download_and_config(self):
        for port, ctx in ((self.port, None), (self.tls_port, self.client_tls)):
            conn = (http.client.HTTPSConnection("127.0.0.1", port, context=ctx, timeout=5) if ctx
                    else http.client.HTTPConnection("127.0.0.1", port, timeout=5))
            conn.request("GET", "/pg/ca.crt")
            res = conn.getresponse()
            data = res.read()
            self.assertEqual(res.status, 200)
            self.assertTrue(data.startswith(b"-----BEGIN CERTIFICATE-----"))
            self.assertNotIn(b"PRIVATE", data)
            self.assertIn("attachment", res.getheader("Content-Disposition"))
            conn.close()
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/pg/config")
        cfg = json.loads(conn.getresponse().read())
        conn.close()
        self.assertIn("tls", cfg)
        self.assertTrue(cfg["realtime"]["enabled"])

    def test_api_responses_deny_camera(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/session")
        res = conn.getresponse()
        res.read()
        self.assertEqual(res.getheader("Permissions-Policy"),
                         "camera=(), microphone=(), geolocation=(), payment=(), usb=()")
        conn.close()

    def test_tls_handshake_garbage_does_not_block(self):
        import socket
        s = socket.create_connection(("127.0.0.1", self.tls_port), timeout=5)
        s.sendall(b"GET / HTTP/1.1\r\n\r\n")  # plain HTTP to the TLS port
        s.close()
        conn = http.client.HTTPSConnection("127.0.0.1", self.tls_port, context=self.client_tls, timeout=10)
        conn.request("GET", "/pg/health")
        self.assertEqual(conn.getresponse().status, 200)
        conn.close()


if __name__ == "__main__":
    unittest.main()
