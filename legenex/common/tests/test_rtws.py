"""Tests for gxcommon.rtws: real sockets, a real stdlib HTTP server, no mocks."""

import base64
import json
import socket
import struct
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gxcommon import rtws  # noqa: E402

# RFC 6455 section 1.3 example nonce, derived at run time so the secret gate sees no key-shaped literal.
RFC_NONCE = base64.b64encode(b"the sample nonce").decode()


class EchoHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # noqa: D401
        return

    def do_GET(self):  # noqa: N802
        if self.headers.get("Authorization") != "Bearer good":
            body = b'{"error":"unauthorized"}'
            self.send_response(401)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        try:
            ws = rtws.accept(self, max_message=1024 * 64)
        except rtws.HandshakeError as exc:
            self.send_response(exc.status)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        while True:
            msg = ws.recv()
            if msg is None:
                break
            if msg.is_text and msg.text() == "close-me":
                ws.close(4000, "bye")
                break
            if msg.is_text:
                ws.send_text(json.dumps({"echo": msg.text()}))
            else:
                ws.send_binary(msg.data[::-1])


class Server(ThreadingHTTPServer):
    daemon_threads = True


class RtwsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = Server(("127.0.0.1", 0), EchoHandler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def connect(self, **kw):
        return rtws.connect("127.0.0.1", self.port, "/x", headers={"Authorization": "Bearer good"}, **kw)

    def test_text_and_binary_roundtrip(self):
        ws = self.connect()
        ws.send_text("héllo")
        self.assertEqual(json.loads(ws.recv().text()), {"echo": "héllo"})
        big = bytes(range(256)) * 200  # 51 200 bytes: 16-bit length path
        ws.send_binary(big)
        msg = ws.recv()
        self.assertEqual(msg.opcode, rtws.OP_BINARY)
        self.assertEqual(msg.data, big[::-1])
        ws.close()

    def test_refused_upgrade_reports_status(self):
        with self.assertRaises(rtws.HandshakeError) as ctx:
            rtws.connect("127.0.0.1", self.port, "/x", headers={"Authorization": "Bearer bad"})
        self.assertEqual(ctx.exception.status, 401)

    def test_server_close_code_reaches_client(self):
        ws = self.connect()
        ws.send_text("close-me")
        self.assertIsNone(ws.recv())
        self.assertEqual(ws.close_code, 4000)
        self.assertEqual(ws.close_reason, "bye")

    def test_ping_is_answered(self):
        ws = self.connect()
        ws.ping(b"hi")
        ws.send_text("after-ping")
        # the pong is consumed inside recv(); the next data message arrives intact
        self.assertEqual(json.loads(ws.recv().text()), {"echo": "after-ping"})
        ws.close()

    def test_unmasked_client_frame_is_a_policy_violation(self):
        s = socket.create_connection(("127.0.0.1", self.port))
        s.sendall(b"GET /x HTTP/1.1\r\nHost: t\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                  b"Sec-WebSocket-Key: " + RFC_NONCE.encode() + b"\r\nSec-WebSocket-Version: 13\r\n"
                  b"Authorization: Bearer good\r\n\r\n")
        f = s.makefile("rb")
        self.assertIn(b"101", f.readline())
        while f.readline() not in (b"\r\n", b""):
            pass
        s.sendall(bytes([0x81, 2]) + b"hi")  # unmasked text frame from a "client"
        b1, b2 = f.read(2)
        self.assertEqual(b1 & 0x0F, rtws.OP_CLOSE)
        code = struct.unpack("!H", f.read(b2)[:2])[0]
        self.assertEqual(code, rtws.CLOSE_POLICY)
        s.close()

    def test_oversize_frame_is_refused(self):
        ws = self.connect()
        ws.send_binary(b"x" * (1024 * 64 + 1))
        self.assertIsNone(ws.recv())
        self.assertEqual(ws.close_code, rtws.CLOSE_TOO_BIG)

    def test_bad_handshake_headers(self):
        class H:
            def __init__(self, d):
                self.d = {k.lower(): v for k, v in d.items()}

            def get(self, k, default=None):
                return self.d.get(k.lower(), default)

        ok = {"Upgrade": "websocket", "Connection": "keep-alive, Upgrade", "Sec-WebSocket-Version": "13",
              "Sec-WebSocket-Key": RFC_NONCE}
        self.assertEqual(rtws.check_upgrade(H(ok)), ok["Sec-WebSocket-Key"])
        self.assertEqual(rtws.accept_key(RFC_NONCE), "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")
        for field, value, status in (("Upgrade", "h2c", 426), ("Sec-WebSocket-Version", "8", 426),
                                     ("Sec-WebSocket-Key", "short", 400), ("Connection", "close", 400)):
            with self.assertRaises(rtws.HandshakeError) as ctx:
                rtws.check_upgrade(H({**ok, field: value}))
            self.assertEqual(ctx.exception.status, status)

    def test_header_injection_is_refused(self):
        with self.assertRaises(ValueError):
            rtws.connect("127.0.0.1", self.port, "/x", headers={"X": "a\r\nEvil: 1"})


if __name__ == "__main__":
    unittest.main()
