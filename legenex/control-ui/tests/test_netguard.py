"""SSRF defences for user-supplied URLs (D-040)."""

from __future__ import annotations

import http.server
import threading
import unittest

from support import UI_DIR  # noqa: F401

from gx_control_ui import netguard
from gx_control_ui.netguard import BlockedURL, check_url, fetch, ip_blocked


class URLChecks(unittest.TestCase):
    def test_blocks_internal_addresses(self) -> None:
        for addr in ("127.0.0.1", "10.1.2.3", "192.168.100.11", "192.168.101.12", "100.105.214.61",
                     "169.254.169.254", "172.17.0.1", "0.0.0.0", "::1", "fd00::1", "fe80::1",
                     "::ffff:127.0.0.1", "224.0.0.1", "fd00:ec2::254"):
            self.assertTrue(ip_blocked(addr), addr)
        self.assertFalse(ip_blocked("8.8.8.8"))

    def test_rejects_bad_urls(self) -> None:
        for url in ("file:///etc/passwd", "gopher://x.example", "http://localhost/", "http://gx10-02:18800/",
                    "http://gx10-01.taila7ef6a.ts.net/", "http://user:pw@example.com/",
                    "http://example.com:22/", "http://127.0.0.1/", "http://[::1]/", "http://2130706433/",
                    "http://intranet/", "https://example.com/\r\nX: y", "http://169.254.169.254/latest/"):
            with self.assertRaises(BlockedURL, msg=url):
                check_url(url)

    def test_https_only_mode(self) -> None:
        with self.assertRaises(BlockedURL):
            check_url("http://example.com/", allow_http=False)
        check_url("https://example.com/x?y=1", allow_http=False)

    def test_dns_answer_with_internal_address_is_refused(self) -> None:
        orig = netguard.socket.getaddrinfo
        netguard.socket.getaddrinfo = lambda *a, **k: [(2, 1, 6, "", ("8.8.8.8", 80)),
                                                       (2, 1, 6, "", ("10.0.0.5", 80))]
        try:
            with self.assertRaises(BlockedURL):
                netguard.resolve_public("rebind.example", 80)
        finally:
            netguard.socket.getaddrinfo = orig


class _Redirect(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a) -> None:  # noqa: D401
        pass

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/hop":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/secret")
            self.end_headers()
            return
        body = b"x" * 5000
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FetchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Redirect)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        netguard.ALLOWED_PORTS = frozenset(netguard.ALLOWED_PORTS | {cls.port})

    @classmethod
    def tearDownClass(cls) -> None:
        cls.srv.shutdown()

    def _resolver(self, host: str, port: int) -> str:
        # Test stand-in: public.example -> the local stub; everything else via the real check.
        return "127.0.0.1" if host == "public.example" else netguard.resolve_public(host, port)

    def test_redirect_to_internal_is_revalidated(self) -> None:
        with self.assertRaises(BlockedURL):
            fetch(f"http://public.example:{self.port}/hop", resolver=self._resolver)

    def test_body_is_capped(self) -> None:
        res = fetch(f"http://public.example:{self.port}/big", resolver=self._resolver, max_bytes=100)
        self.assertEqual(res.status, 200)
        self.assertTrue(res.truncated)
        self.assertEqual(len(res.body), 100)


if __name__ == "__main__":
    unittest.main()
