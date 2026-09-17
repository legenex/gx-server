"""Build V3 platform (PLT): realtime registry and tickets, the authorize rule,
the activity feed, Playground preferences, the metrics shim and the HTTPS
cookie. Real HTTP server on 127.0.0.1 in offline mode.
"""

from __future__ import annotations

import http.client
import json
import secrets
import threading
import unittest

from support import TempEnv

from gx_control_ui import auth
from gx_control_ui import server as srv
from gx_control_ui.activity import ActivityFeed, normalise, norm_status
from gx_control_ui.realtime import RealtimeError, RealtimeRegistry, Target, new_session_id, owner_hash

PASSWORD = "Platform-" + secrets.token_hex(6)


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv()
        self.keyfile = self.env.root / "secrets" / "k"
        self.keyfile.write_text("k" * 40)
        self.clock = Clock()
        self.events = []
        self.reg = RealtimeRegistry({"call": Target("192.168.100.11", 18840, self.keyfile),
                                     "live": Target("192.168.100.11", 18850, self.keyfile)},
                                    clock=self.clock, on_event=lambda e, **kw: self.events.append((e, kw)))

    def tearDown(self):
        self.env.cleanup()

    def reg_session(self, service="call", owner="user:admin", **kw):
        sid = new_session_id(service)
        self.reg.register(service, sid, owner=owner, upstream_path=f"/v1/{service}/sessions/{sid}/ws", **kw)
        return sid

    def test_ids_and_validation(self):
        self.assertRegex(new_session_id("live"), r"^live_[0-9a-f]{32}$")
        with self.assertRaises(RealtimeError):
            new_session_id("voice")
        sid = new_session_id("call")
        bad = [
            dict(service="live", session_id=sid, owner="user:admin", upstream_path="/x"),
            dict(service="call", session_id="call_xyz", owner="user:admin", upstream_path="/x"),
            dict(service="call", session_id=sid, owner="admin", upstream_path="/x"),
            dict(service="call", session_id=sid, owner="user:admin", upstream_path="http://evil/x"),
            dict(service="call", session_id=sid, owner="user:admin", upstream_path="/a/../b"),
            dict(service="call", session_id=sid, owner="user:admin", upstream_path="//evil/x"),
            dict(service="call", session_id=sid, owner="user:admin", upstream_path="/x\r\nHost: y"),
            dict(service="call", session_id=sid, owner="user:admin", upstream_path="/x", ttl_s=5),
            dict(service="call", session_id=sid, owner="user:admin", upstream_path="/x", ttl_s=99999),
            dict(service="call", session_id=sid, owner="user:admin", upstream_path="/x", meta={"a": "x" * 3000}),
        ]
        for kw in bad:
            with self.assertRaises(RealtimeError, msg=kw):
                self.reg.register(kw.pop("service"), kw.pop("session_id"), **kw)
        self.reg.register("call", sid, owner="user:admin", upstream_path="/v1/call/ws?mode=agent&x=1")
        with self.assertRaises(RealtimeError) as ctx:
            self.reg.register("call", sid, owner="user:other", upstream_path="/x")
        self.assertEqual(ctx.exception.status, 409)

    def test_target_parsing_is_fabric_or_loopback(self):
        self.assertEqual(Target.parse("192.168.100.11:18840", self.keyfile).port, 18840)
        for raw in ("100.105.214.61:18840", "example.com:1", "192.168.100.11", "10.0.0.1:80"):
            with self.assertRaises(ValueError):
                Target.parse(raw, self.keyfile)

    def test_cookie_authorisation(self):
        sid = self.reg_session()
        grant = self.reg.authorize(service="call", session_id=sid, ticket=None, cookie_user="admin",
                                   origin="http://h:8090", host="h:8090")
        self.assertEqual(grant["target"], {"host": "192.168.100.11", "port": 18840})
        self.assertEqual(grant["authorization"], "Bearer " + "k" * 40)
        self.assertEqual(grant["path"], f"/v1/call/sessions/{sid}/ws")
        self.assertEqual(grant["owner"], owner_hash("user:admin"))
        self.assertEqual(grant["user"], "user:admin")
        cases = [
            (dict(cookie_user=None, origin="http://h:8090", host="h:8090"), 401),
            (dict(cookie_user="admin", origin="http://evil:8090", host="h:8090"), 403),
            (dict(cookie_user="admin", origin=None, host="h:8090"), 403),
            (dict(cookie_user="admin", origin="null", host="h:8090"), 403),
            (dict(cookie_user="other", origin="http://h:8090", host="h:8090"), 404),
        ]
        for kw, status in cases:
            with self.assertRaises(RealtimeError, msg=kw) as ctx:
                self.reg.authorize(service="call", session_id=sid, ticket=None, **kw)
            self.assertEqual(ctx.exception.status, status, kw)
        with self.assertRaises(RealtimeError) as ctx:
            self.reg.authorize(service="live", session_id=sid, ticket=None, cookie_user="admin",
                               origin="http://h:8090", host="h:8090")
        self.assertEqual(ctx.exception.status, 404)

    def test_ticket_rules(self):
        owner = "key:" + "0f" * 8
        sid = self.reg_session(owner=owner)
        with self.assertRaises(RealtimeError):
            self.reg.issue_ticket(sid, owner="key:" + "aa" * 8)
        t = self.reg.issue_ticket(sid, owner=owner)
        self.assertEqual(self.reg.redeem_ticket(t, sid), owner)
        with self.assertRaises(RealtimeError) as ctx:
            self.reg.redeem_ticket(t, sid)
        self.assertEqual(ctx.exception.code, "ticket_used")
        t2 = self.reg.issue_ticket(sid, owner=owner)
        self.clock.now += 61
        with self.assertRaises(RealtimeError) as ctx:
            self.reg.redeem_ticket(t2, sid)
        self.assertEqual(ctx.exception.code, "ticket_expired")
        t3 = self.reg.issue_ticket(sid, owner=owner)
        tampered = t3[:-1] + ("A" if t3[-1] != "A" else "B")
        for bad in (tampered, "v1.garbage", "", t3.replace(sid, new_session_id("call"))):
            with self.assertRaises(RealtimeError):
                self.reg.redeem_ticket(bad, sid)
        # a ticket from another registry (another process key) is refused
        other = RealtimeRegistry(self.reg.targets, clock=self.clock)
        other.register("call", sid, owner=owner, upstream_path="/x")
        with self.assertRaises(RealtimeError):
            self.reg.redeem_ticket(other.issue_ticket(sid, owner=owner), sid)
        # the ticket path ignores the cookie user entirely
        t4 = self.reg.issue_ticket(sid, owner=owner)
        grant = self.reg.authorize(service="call", session_id=sid, ticket=t4, cookie_user="admin",
                                   origin="http://evil", host="h")
        self.assertEqual(grant["via"], "ticket")

    def test_used_nonces_are_pruned(self):
        owner = "key:" + "0e" * 8
        sid = self.reg_session(owner=owner)
        self.reg.redeem_ticket(self.reg.issue_ticket(sid, owner=owner), sid)
        self.assertEqual(len(self.reg._used), 1)
        self.clock.now += 120
        self.reg_session()
        self.assertEqual(len(self.reg._used), 0)

    def test_expiry_end_and_events(self):
        sid = self.reg_session(ttl_s=60)
        self.assertEqual(self.reg.stats(), {"call": 1, "live": 0})
        self.clock.now += 61
        with self.assertRaises(RealtimeError) as ctx:
            self.reg.authorize(service="call", session_id=sid, ticket=None, cookie_user="admin",
                               origin="http://h", host="h")
        self.assertEqual(ctx.exception.status, 410)
        self.assertEqual(self.reg.list(owner="user:admin")[0]["disposition"], "expired")
        sid2 = self.reg_session()
        self.reg.end(sid2, "completed")
        self.reg.end(sid2, "failed")  # second end is ignored
        self.assertEqual([e for e, _ in self.events], ["realtime.session"])
        self.assertEqual(self.events[0][1]["disposition"], "completed")
        self.assertEqual(self.reg.get(sid2).disposition, "completed")
        with self.assertRaises(RealtimeError):
            self.reg.end(sid2, "exploded")
        self.assertEqual(self.reg.list(owner="user:admin", include_ended=False), [])
        self.assertEqual(len(self.reg.list(owner="user:nobody")), 0)

    def test_missing_or_short_key_is_503(self):
        sid = self.reg_session()
        self.keyfile.write_text("short")
        with self.assertRaises(RealtimeError) as ctx:
            self.reg.authorize(service="call", session_id=sid, ticket=None, cookie_user="admin",
                               origin="http://h", host="h")
        self.assertEqual((ctx.exception.status, ctx.exception.code), (503, "realtime_disabled"))

    def test_capacity(self):
        from gx_control_ui import realtime
        old = realtime.MAX_SESSIONS
        realtime.MAX_SESSIONS = 2
        try:
            self.reg_session()
            self.reg_session()
            with self.assertRaises(RealtimeError) as ctx:
                self.reg_session()
            self.assertEqual(ctx.exception.status, 429)
        finally:
            realtime.MAX_SESSIONS = old

    def test_config_targets(self):
        targets = self.env.cfg.realtime_targets()
        self.assertEqual((targets["call"].host, targets["call"].port), ("192.168.100.11", 18840))
        self.assertEqual(targets["live"].port, 18850)
        self.assertTrue(str(targets["live"].key_file).endswith("gx-live/api-key"))


class ActivityTests(unittest.TestCase):
    def test_normalise_redacts_and_filters(self):
        item = normalise("voice", {"id": "j1", "title": "TTS " + "sk" + "-" + "a" * 30, "status": "completed", "at": 5,
                                   "error": "Bearer " + "abcdefghijklmnopqrstuv" + " failed", "link": "javascript:alert(1)",
                                   "detail": {"prompt": "secret", "voice": "alloy", "api_key": "x",
                                              "chars": 12}})
        self.assertEqual(item["status"], "ok")
        self.assertNotIn("sk-aaaa", item["title"])
        self.assertNotIn("abcdefghijklmnop", item["error"])
        self.assertIsNone(item["link"])
        self.assertEqual(item["detail"], {"voice": "alloy", "chars": 12})
        self.assertIsNone(normalise("x", {"at": "not a time"}))
        self.assertEqual(normalise("x", {"at": 1, "link": "#/voice?job=abc"})["link"], "#/voice?job=abc")
        self.assertEqual(norm_status("queued"), "waiting")
        self.assertEqual(norm_status(None), "ok")

    def test_query_merges_sorts_filters_and_isolates_failures(self):
        feed = ActivityFeed()
        feed.register("alpha", lambda u, s, n: [{"id": "1", "at": 10, "status": "failed", "title": "one", "error": "boom"},
                                            {"id": "2", "at": 30, "status": "ok", "title": "two"}])
        feed.register("beta", lambda u, s, n: [{"id": "3", "at": 20, "status": "running", "title": f"for {u}"}])

        def broken(u, s, n):
            raise RuntimeError("down")

        feed.register("gamma", broken)
        out = feed.query("admin")
        self.assertEqual([i["id"] for i in out["items"]], ["2", "3", "1"])
        self.assertEqual(out["unavailable"], ["gamma"])
        self.assertEqual(out["counts"]["failed"], 1)
        self.assertEqual([i["id"] for i in feed.query("admin", status="failed")["items"]], ["1"])
        self.assertEqual([i["id"] for i in feed.query("admin", kind="beta")["items"]], ["3"])
        self.assertEqual([i["id"] for i in feed.query("admin", q="BOOM")["items"]], ["1"])
        self.assertEqual([i["id"] for i in feed.query("admin", since=15)["items"]], ["2", "3"])
        self.assertEqual(len(feed.query("admin", limit=1)["items"]), 1)
        for bad in (dict(kind="zzz"), dict(status="exploded")):
            with self.assertRaises(ValueError):
                feed.query("admin", **bad)
        with self.assertRaises(ValueError):
            feed.register("Bad Kind", broken)


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv()
        auth.PasswordStore(self.env.cfg.password_file).set_password("admin", PASSWORD, n=2**10)
        self.app, servers = srv.build(self.env.cfg)
        self.httpd = servers[0]
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        status, hdrs, body = self.req("POST", "/api/login", {"username": "admin", "password": PASSWORD})
        self.assertEqual(status, 200)
        self.cookie = hdrs["Set-Cookie"].split(";")[0]
        self.csrf = body["csrf"]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.env.cleanup()

    def req(self, method, path, body=None, headers=None, cookie=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        hdrs = {"Host": f"127.0.0.1:{self.port}", "Origin": f"http://127.0.0.1:{self.port}"}
        if cookie and getattr(self, "cookie", None):
            hdrs["Cookie"] = self.cookie
        raw = None
        if body is not None:
            raw = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
        hdrs.update(headers or {})
        conn.request(method, path, body=raw, headers=hdrs)
        res = conn.getresponse()
        data = res.read()
        conn.close()
        cookies = res.headers.get_all("Set-Cookie") or []
        h = dict(res.getheaders())
        if cookies:
            h["Set-Cookie"] = cookies[0]
            h["_cookies"] = cookies
        return res.status, h, (json.loads(data) if data and "json" in res.getheader("Content-Type", "") else data)

    def test_session_required(self):
        for path in ("/api/activity", "/api/realtime/sessions", "/api/preferences", "/api/catalog"):
            status, _, _ = self.req("GET", path, cookie=False)
            self.assertEqual(status, 401, path)
        status, _, _ = self.req("POST", "/api/preferences", {"preferences": {"theme": "light"}},
                                headers={"X-CSRF-Token": "nope"})
        self.assertEqual(status, 403)

    def test_authorize_needs_the_proxy_token(self):
        body = {"service": "call", "session_id": new_session_id("call"),
                "origin": f"http://127.0.0.1:{self.port}", "host": f"127.0.0.1:{self.port}"}
        self.assertEqual(self.req("POST", "/api/realtime/authorize", body)[0], 404)
        token = {"X-GX-Proxy-Token": self.app.proxy_token, "X-GX-Forwarded-For": "100.64.1.2"}
        status, _, out = self.req("POST", "/api/realtime/authorize", body, headers=token)
        self.assertEqual(status, 404)  # unknown session, but the route answered for the proxy
        self.assertEqual(out["error"]["code"], "not_found")
        status, _, out = self.req("POST", "/api/realtime/authorize", {"service": 3}, headers=token)
        self.assertEqual(status, 400)
        audit = (self.env.cfg.log_dir / "audit.log").read_text()
        self.assertIn("realtime.authorize.call", audit)
        self.assertIn("100.64.1.2", audit)

    def test_authorize_grant_through_http(self):
        keyfile = self.env.root / "secrets" / "call-key"
        keyfile.write_text("z" * 40)
        self.app.realtime.targets = {"call": Target("127.0.0.1", 9, keyfile)}
        sid = new_session_id("call")
        self.app.realtime.register("call", sid, owner="user:admin", upstream_path=f"/v1/call/sessions/{sid}/ws")
        token = {"X-GX-Proxy-Token": self.app.proxy_token}
        body = {"service": "call", "session_id": sid, "origin": "http://h:8090", "host": "h:8090"}
        status, _, grant = self.req("POST", "/api/realtime/authorize", body, headers=token)
        self.assertEqual(status, 200, grant)
        self.assertEqual(grant["authorization"], "Bearer " + "z" * 40)
        status, _, out = self.req("POST", "/api/realtime/authorize", body, headers=token, cookie=False)
        self.assertEqual((status, out["error"]["code"]), (401, "unauthenticated"))
        # the session list shows the connect, never the key
        status, _, lst = self.req("GET", "/api/realtime/sessions")
        self.assertEqual(lst["sessions"][0]["connects"], 1)
        self.assertNotIn("zzzz", json.dumps(lst))

    def test_preferences_roundtrip_and_validation(self):
        status, _, out = self.req("GET", "/api/preferences")
        self.assertEqual((status, out["preferences"]), (200, {}))
        self.assertIn("theme", out["allowed"])
        hdr = {"X-CSRF-Token": self.csrf}
        status, _, out = self.req("POST", "/api/preferences",
                                  {"preferences": {"theme": "light", "default_image_size": "1024x1024"}}, hdr)
        self.assertEqual((status, out["preferences"]), (200, {"theme": "light", "default_image_size": "1024x1024"}))
        status, _, out = self.req("POST", "/api/preferences", {"preferences": {"theme": None}}, hdr)
        self.assertEqual(out["preferences"], {"default_image_size": "1024x1024"})
        for bad in ({"theme": "neon"}, {"evil": "x"}, {"default_image_size": "1e9x1"}, {}, {"theme": 1},
                    {"default_image_size": "1024x1024'; DROP TABLE plt_preferences; --"}):
            status, _, _ = self.req("POST", "/api/preferences", {"preferences": bad}, hdr)
            self.assertEqual(status, 400, bad)
        # an invalid batch writes nothing
        status, _, _ = self.req("POST", "/api/preferences",
                                {"preferences": {"theme": "dark", "density": "huge"}}, hdr)
        self.assertEqual(status, 400)
        self.assertNotIn("theme", self.req("GET", "/api/preferences")[2]["preferences"])
        names = [m["name"] for m in self.app.library.migrations()]
        self.assertIn("080_platform.sql", names)

    def test_activity_feed(self):
        self.app.actions.audit(user="admin", ip="127.0.0.1", action="music.generate", outcome="queued")
        self.app.actions.audit(user="someone-else", ip="127.0.0.1", action="music.generate", outcome="queued")
        sid = new_session_id("live")
        keyfile = self.env.root / "secrets" / "live-key"
        keyfile.write_text("q" * 40)
        self.app.realtime.targets = {"live": Target("127.0.0.1", 9, keyfile)}
        self.app.realtime.register("live", sid, owner="user:admin", upstream_path="/v1/live/ws")
        status, _, out = self.req("GET", "/api/activity?limit=50")
        self.assertEqual(status, 200)
        kinds = {i["kind"] for i in out["items"]}
        self.assertIn("account", kinds)
        self.assertIn("realtime", kinds)
        self.assertTrue(all(i.get("detail", {}).get("ip") in (None, "127.0.0.1") for i in out["items"]))
        titles = [i["title"] for i in out["items"] if i["kind"] == "account"]
        self.assertIn("music.generate", titles)
        self.assertIn("login", titles)
        self.assertEqual(sum(1 for t in titles if t == "music.generate"), 1)
        status, _, out = self.req("GET", "/api/activity?kind=realtime")
        self.assertEqual([i["id"] for i in out["items"]], [sid])
        for bad in ("?kind=Bad!", "?status=zzz", "?since=abc", "?kind=unknownkind"):
            self.assertEqual(self.req("GET", "/api/activity" + bad)[0], 400, bad)

    def test_https_cookie_only_via_the_playground(self):
        hdrs = {"X-GX-Proxy-Token": self.app.proxy_token, "X-GX-Forwarded-Proto": "https"}
        status, h, _ = self.req("POST", "/api/login", {"username": "admin", "password": PASSWORD},
                                headers=hdrs, cookie=False)
        self.assertEqual(status, 200)
        self.assertTrue(h["Set-Cookie"].startswith("__Host-gxui_session="))
        self.assertIn("Secure", h["Set-Cookie"])
        secure = h["Set-Cookie"].split(";")[0]
        # the secure cookie only counts on an HTTPS request
        conn_hdrs = {"Cookie": secure}
        self.assertFalse(self.req("GET", "/api/session", headers=conn_hdrs, cookie=False)[2]["authenticated"])
        self.assertTrue(self.req("GET", "/api/session", headers={**conn_hdrs, **hdrs}, cookie=False)[2]
                        ["authenticated"])
        # without the proxy token the header is ignored
        status, h, _ = self.req("POST", "/api/login", {"username": "admin", "password": PASSWORD},
                                headers={"X-GX-Forwarded-Proto": "https"}, cookie=False)
        self.assertTrue(h["Set-Cookie"].startswith("gxui_session="))
        # both cookies present over HTTPS: the secure one wins
        both = {"Cookie": f"gxui_session=bogus; {secure}", **hdrs}
        self.assertTrue(self.req("GET", "/api/session", headers=both, cookie=False)[2]["authenticated"])

    def test_catalog(self):
        status, _, out = self.req("GET", "/api/catalog")
        self.assertEqual(status, 200)
        aliases = {m["alias"] for m in out["models"]}
        self.assertTrue({"gx-image", "gx-video", "gx-music"} <= aliases)
        text = json.dumps(out)
        self.assertNotIn("/srv/projects/gx-cluster/secrets", text)
        self.assertNotIn("Bearer", text)


class MetricsShimTests(unittest.TestCase):
    def test_metric_lines_and_files(self):
        import io

        from gx_control_ui import obs
        buf = io.StringIO()
        old = obs.METRICS.stream
        obs.METRICS.stream = buf
        try:
            obs.metric("realtime.session", service_name="x", disposition="completed", prompt="never")
        finally:
            obs.METRICS.stream = old
        line = json.loads(buf.getvalue())
        self.assertEqual((line["service"], line["event"]), ("gx-control-ui", "realtime.session"))
        self.assertNotIn("prompt", line)
        env = TempEnv()
        try:
            (env.root / "m").mkdir()
            (env.root / "m" / "a.jsonl").write_text(json.dumps({"kind": "metric", "ts": "2026-09-17T10:00:00+02:00",
                                                               "user": "user:admin", "event": "failure"}) + "\n")
            files = obs.metrics_files(env.root / "m")
            self.assertEqual(len(files), 1)
            self.assertEqual(len(obs.read_metrics(files, user="user:admin")), 1)
        finally:
            env.cleanup()


if __name__ == "__main__":
    unittest.main()
