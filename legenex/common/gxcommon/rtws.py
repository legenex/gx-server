"""Minimal RFC 6455 WebSocket endpoints (server and client), stdlib only.

Shared realtime building block for the node-2 supervisors (gx-live, gx-call)
and their tests. It deliberately supports only what the Build V3 realtime
tunnel carries (plt.md section 1): no extensions (the tunnel strips
``permessage-deflate``), no subprotocol negotiation beyond echoing one
offered value, text and binary messages, ping/pong and the close handshake.

Server side (inside a ``BaseHTTPRequestHandler`` that received an upgrade)::

    ws = accept(handler)                  # validates headers, sends the 101
    while True:
        msg = ws.recv()                   # -> Message(opcode, data) or None when closed
        ...
    ws.send_text("{}"); ws.send_binary(b"..."); ws.close(1000)

Client side (a supervisor talking to its engine on loopback)::

    ws = connect("127.0.0.1", 18851, "/session/abc", headers={"Authorization": "Bearer ..."})

Thread safety: ``send_*``/``ping``/``close`` may be called from several
threads; ``recv`` must be called from one thread only.
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import struct
import threading
import time
from dataclasses import dataclass
from typing import Any

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA
DEFAULT_MAX_MESSAGE = 8 * 1024 * 1024

#: Close codes used across the realtime services.
CLOSE_NORMAL = 1000
CLOSE_GOING_AWAY = 1001
CLOSE_PROTOCOL = 1002
CLOSE_UNSUPPORTED = 1003
CLOSE_INVALID_DATA = 1007
CLOSE_POLICY = 1008
CLOSE_TOO_BIG = 1009
CLOSE_INTERNAL = 1011


class WebSocketError(Exception):
    """Protocol violation or transport failure. ``code`` is the close code to send."""

    def __init__(self, message: str, code: int = CLOSE_PROTOCOL) -> None:
        super().__init__(message)
        self.code = code


class HandshakeError(Exception):
    """The upgrade request is not acceptable. ``status`` is the HTTP status to answer."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Message:
    opcode: int
    data: bytes

    @property
    def is_text(self) -> bool:
        return self.opcode == OP_TEXT

    def text(self) -> str:
        return self.data.decode("utf-8")


def accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + GUID).encode("ascii")).digest()).decode("ascii")  # noqa: S324


def check_upgrade(headers: Any) -> str:
    """Validate an upgrade request's headers; return the client key.

    ``headers`` is any mapping with case-insensitive ``get`` (``http.client.HTTPMessage``).
    """
    if "websocket" not in (headers.get("Upgrade") or "").lower():
        raise HandshakeError("expected a WebSocket upgrade", 426)
    if "upgrade" not in (headers.get("Connection") or "").lower():
        raise HandshakeError("expected Connection: Upgrade", 400)
    if (headers.get("Sec-WebSocket-Version") or "").strip() != "13":
        raise HandshakeError("only WebSocket version 13 is supported", 426)
    key = (headers.get("Sec-WebSocket-Key") or "").strip()
    try:
        raw = base64.b64decode(key, validate=True)
    except ValueError:
        raw = b""
    if len(raw) != 16:
        raise HandshakeError("invalid Sec-WebSocket-Key", 400)
    return key


def accept(handler: Any, *, subprotocol: str | None = None, max_message: int = DEFAULT_MAX_MESSAGE,
           extra_headers: dict[str, str] | None = None) -> WebSocket:
    """Complete the server side of the handshake on a ``BaseHTTPRequestHandler``.

    Raises ``HandshakeError`` before anything is written when the request is not
    a valid upgrade. The handler must not write another response afterwards.
    """
    key = check_upgrade(handler.headers)
    lines = ["HTTP/1.1 101 Switching Protocols", "Upgrade: websocket", "Connection: Upgrade",
             f"Sec-WebSocket-Accept: {accept_key(key)}"]
    if subprotocol:
        lines.append(f"Sec-WebSocket-Protocol: {subprotocol}")
    for k, v in (extra_headers or {}).items():
        lines.append(f"{k}: {v}")
    handler.wfile.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
    handler.wfile.flush()
    handler.close_connection = True
    return WebSocket(handler.connection, reader=handler.rfile, client=False, max_message=max_message)


def connect(host: str, port: int, path: str, *, headers: dict[str, str] | None = None, timeout: float = 10.0,
            max_message: int = DEFAULT_MAX_MESSAGE) -> WebSocket:
    """Open a client WebSocket (masks its frames). Raises ``HandshakeError`` with the
    upstream HTTP status when the server refuses the upgrade."""
    sock = socket.create_connection((host, port), timeout=timeout)
    try:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = [f"GET {path} HTTP/1.1", f"Host: {host}:{port}", "Upgrade: websocket", "Connection: Upgrade",
               f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13"]
        for k, v in (headers or {}).items():
            if "\r" in k or "\n" in k or "\r" in v or "\n" in v:
                raise ValueError("header injection refused")
            req.append(f"{k}: {v}")
        sock.sendall(("\r\n".join(req) + "\r\n\r\n").encode("latin-1"))
        reader = sock.makefile("rb")
        status_line = reader.readline(4096).decode("latin-1").strip()
        parts = status_line.split(" ", 2)
        status = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 0
        resp_headers: dict[str, str] = {}
        body_len = 0
        while True:
            line = reader.readline(8192)
            if line in (b"\r\n", b"\n", b""):
                break
            name, _, value = line.decode("latin-1").partition(":")
            resp_headers[name.strip().lower()] = value.strip()
        if status != 101:
            try:
                body_len = min(int(resp_headers.get("content-length", "0")), 65536)
            except ValueError:
                body_len = 0
            body = reader.read(body_len) if body_len else b""
            err = HandshakeError(f"upgrade refused with HTTP {status}", status or 502)
            err.body = body  # type: ignore[attr-defined]
            raise err
        if resp_headers.get("sec-websocket-accept") != accept_key(key):
            raise HandshakeError("bad Sec-WebSocket-Accept from server", 502)
        sock.settimeout(None)
        return WebSocket(sock, reader=reader, client=True, max_message=max_message)
    except BaseException:
        sock.close()
        raise


class WebSocket:
    def __init__(self, sock: socket.socket, *, reader: Any = None, client: bool = False,
                 max_message: int = DEFAULT_MAX_MESSAGE) -> None:
        self.sock = sock
        self._rfile = reader if reader is not None else sock.makefile("rb")
        self.client = client
        self.max_message = max_message
        self._send_lock = threading.Lock()
        self.closed = False
        self.close_code: int | None = None
        self.close_reason = ""
        self._close_sent = False
        self.bytes_in = 0
        self.bytes_out = 0
        self.last_activity = time.monotonic()

    # ------------------------------------------------------------ sending --
    def _send_frame(self, opcode: int, payload: bytes, fin: bool = True) -> None:
        head = bytearray([(0x80 if fin else 0) | opcode])
        mask_bit = 0x80 if self.client else 0
        n = len(payload)
        if n < 126:
            head.append(mask_bit | n)
        elif n < 65536:
            head.append(mask_bit | 126)
            head += struct.pack("!H", n)
        else:
            head.append(mask_bit | 127)
            head += struct.pack("!Q", n)
        if self.client:
            mask = os.urandom(4)
            head += mask
            payload = _mask(payload, mask)
        with self._send_lock:
            if self._close_sent:
                raise WebSocketError("connection is closing", CLOSE_GOING_AWAY)
            try:
                self.sock.sendall(bytes(head) + payload)
            except OSError as exc:
                self.closed = True
                raise WebSocketError(f"send failed: {type(exc).__name__}", CLOSE_GOING_AWAY) from exc
            self.bytes_out += len(head) + n
            if opcode == OP_CLOSE:
                self._close_sent = True

    def send_text(self, text: str) -> None:
        self._send_frame(OP_TEXT, text.encode("utf-8"))

    def send_binary(self, data: bytes) -> None:
        self._send_frame(OP_BINARY, bytes(data))

    def ping(self, data: bytes = b"") -> None:
        self._send_frame(OP_PING, data[:125])

    def close(self, code: int = CLOSE_NORMAL, reason: str = "") -> None:
        """Send a close frame (once) and shut the socket down for writing."""
        payload = struct.pack("!H", code) + reason.encode("utf-8")[:120]
        try:
            self._send_frame(OP_CLOSE, payload)
        except WebSocketError:
            pass
        self.closed = True
        self.close_code = self.close_code or code
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        for closer in (self._rfile.close, self.sock.close):
            try:
                closer()
            except (OSError, ValueError):
                pass

    # ---------------------------------------------------------- receiving --
    def _read_exact(self, n: int) -> bytes:
        data = self._rfile.read(n) if n else b""
        if len(data) != n:
            raise WebSocketError("connection closed by peer", CLOSE_GOING_AWAY)
        self.bytes_in += n
        return data

    def _read_frame(self) -> tuple[bool, int, bytes]:
        b1, b2 = self._read_exact(2)
        fin, rsv, opcode = bool(b1 & 0x80), b1 & 0x70, b1 & 0x0F
        masked, n = bool(b2 & 0x80), b2 & 0x7F
        if rsv:
            raise WebSocketError("reserved bits set (no extensions negotiated)", CLOSE_PROTOCOL)
        if masked == self.client:
            # clients must mask, servers must not (RFC 6455 5.1)
            raise WebSocketError("frame masking violates RFC 6455", CLOSE_POLICY)
        if n == 126:
            n = struct.unpack("!H", self._read_exact(2))[0]
        elif n == 127:
            n = struct.unpack("!Q", self._read_exact(8))[0]
        if opcode >= 0x8 and (n > 125 or not fin):
            raise WebSocketError("invalid control frame", CLOSE_PROTOCOL)
        if n > self.max_message:
            raise WebSocketError("frame too big", CLOSE_TOO_BIG)
        mask = self._read_exact(4) if masked else b""
        payload = self._read_exact(n)
        if masked:
            payload = _mask(payload, mask)
        return fin, opcode, payload

    def recv(self) -> Message | None:
        """Next complete data message, or None once the connection is closed.

        Pings are answered, pongs ignored, a close frame is echoed. Protocol
        errors close the connection with the matching code and return None.
        """
        buf = bytearray()
        msg_op: int | None = None
        while not self.closed:
            try:
                fin, opcode, payload = self._read_frame()
            except WebSocketError as exc:
                if not self._close_sent and exc.code != CLOSE_GOING_AWAY:
                    self.close(exc.code, str(exc)[:100])
                self.closed = True
                self.close_code = self.close_code or exc.code
                return None
            except (OSError, ValueError):
                self.closed = True
                self.close_code = self.close_code or CLOSE_GOING_AWAY
                return None
            self.last_activity = time.monotonic()
            if opcode == OP_PING:
                try:
                    self._send_frame(OP_PONG, payload)
                except WebSocketError:
                    pass
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                code = struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else 1005
                self.close_code = code
                self.close_reason = payload[2:].decode("utf-8", "replace")
                if not self._close_sent:
                    self.close(code if code not in (1005, 1006, 1015) else CLOSE_NORMAL)
                self.closed = True
                return None
            if opcode in (OP_TEXT, OP_BINARY):
                if msg_op is not None:
                    self.close(CLOSE_PROTOCOL, "new message before the previous one finished")
                    return None
                msg_op = opcode
            elif opcode == OP_CONT:
                if msg_op is None:
                    self.close(CLOSE_PROTOCOL, "continuation without a message")
                    return None
            else:
                self.close(CLOSE_PROTOCOL, "unknown opcode")
                return None
            buf += payload
            if len(buf) > self.max_message:
                self.close(CLOSE_TOO_BIG, "message too big")
                return None
            if fin:
                if msg_op == OP_TEXT:
                    try:
                        bytes(buf).decode("utf-8")
                    except UnicodeDecodeError:
                        self.close(CLOSE_INVALID_DATA, "text frame is not UTF-8")
                        return None
                assert msg_op is not None  # noqa: S101
                return Message(msg_op, bytes(buf))
        return None


def _mask(data: bytes, mask: bytes) -> bytes:
    if not data:
        return b""
    # XOR through int arithmetic: fast enough for audio-sized frames, stdlib only.
    n = len(data)
    key = (mask * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "little") ^ int.from_bytes(key, "little")).to_bytes(n, "little")
