"""Minimal RFC 6455 WebSocket (stdlib only) for the gx-call supervisor.

Server side: the Playground tunnel forwards the caller's upgrade request to
gx-call (PLT section 1.2); we answer 101 on the same socket.
Client side: gx-call connects to the engine container on host loopback.

No extensions are negotiated (the tunnel strips them). Frames are limited to
``MAX_FRAME`` bytes; fragmented messages are reassembled up to the same
limit. Control frames (ping/pong/close) are handled inline.
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import struct
import threading
from dataclasses import dataclass

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_FRAME = 4 * 1024 * 1024
OP_CONT, OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class WSClosed(Exception):
    def __init__(self, code: int = 1006, reason: str = "") -> None:
        super().__init__(f"websocket closed ({code}) {reason}")
        self.code = code
        self.reason = reason


class WSProtocolError(Exception):
    pass


def accept_key(client_key: str) -> str:
    return base64.b64encode(hashlib.sha1((client_key + GUID).encode()).digest()).decode()


def valid_client_key(value: str) -> bool:
    try:
        return len(base64.b64decode(value, validate=True)) == 16
    except (ValueError, TypeError):
        return False


@dataclass
class Message:
    opcode: int
    data: bytes

    @property
    def is_text(self) -> bool:
        return self.opcode == OP_TEXT

    def text(self) -> str:
        return self.data.decode("utf-8")


class WebSocket:
    """A connected WebSocket over a blocking socket.

    ``mask_outgoing`` is True for the client role (RFC 6455 5.3) and
    ``require_masked`` for the server role (clients MUST mask).
    """

    def __init__(self, sock: socket.socket, rfile, *, mask_outgoing: bool, require_masked: bool,  # noqa: ANN001
                 max_frame: int = MAX_FRAME) -> None:
        self.sock = sock
        self.rfile = rfile
        self.mask_outgoing = mask_outgoing
        self.require_masked = require_masked
        self.max_frame = max_frame
        self._send_lock = threading.Lock()
        self.closed = False
        self.close_code: int | None = None
        self.bytes_in = 0
        self.bytes_out = 0

    # ------------------------------------------------------------- read
    def _read_exact(self, n: int) -> bytes:
        buf = self.rfile.read(n) if n else b""
        if len(buf) != n:
            raise WSClosed(1006, "connection lost")
        return buf

    def _read_frame(self) -> tuple[bool, int, bytes]:
        b1, b2 = self._read_exact(2)
        fin, opcode = bool(b1 & 0x80), b1 & 0x0F
        if b1 & 0x70:
            raise WSProtocolError("reserved bits set (no extensions negotiated)")
        masked, length = bool(b2 & 0x80), b2 & 0x7F
        if self.require_masked and not masked:
            raise WSProtocolError("client frames must be masked")
        if length == 126:
            (length,) = struct.unpack("!H", self._read_exact(2))
        elif length == 127:
            (length,) = struct.unpack("!Q", self._read_exact(8))
        if length > self.max_frame:
            raise WSProtocolError("frame too large")
        mask = self._read_exact(4) if masked else b""
        payload = self._read_exact(length)
        if masked and length:
            payload = _unmask(payload, mask)
        self.bytes_in += length
        return fin, opcode, payload

    def recv(self) -> Message:
        """Next data message. Answers pings; raises WSClosed on close."""
        buffer = bytearray()
        first_op: int | None = None
        while True:
            fin, opcode, payload = self._read_frame()
            if opcode == OP_PING:
                self._send_frame(OP_PONG, payload[:125])
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                code = struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else 1005
                reason = payload[2:].decode("utf-8", "replace")
                self.close_code = code
                if not self.closed:
                    try:
                        self._send_frame(OP_CLOSE, struct.pack("!H", code if code != 1005 else 1000))
                    except OSError:
                        pass
                self.closed = True
                raise WSClosed(code, reason)
            if opcode in (OP_TEXT, OP_BIN):
                if first_op is not None:
                    raise WSProtocolError("new message before the previous one finished")
                first_op = opcode
            elif opcode == OP_CONT:
                if first_op is None:
                    raise WSProtocolError("continuation without a message")
            else:
                raise WSProtocolError(f"unknown opcode {opcode}")
            buffer.extend(payload)
            if len(buffer) > self.max_frame:
                raise WSProtocolError("message too large")
            if fin:
                data = bytes(buffer)
                if first_op == OP_TEXT:
                    data.decode("utf-8")  # raises on invalid UTF-8
                return Message(first_op, data)

    # ------------------------------------------------------------ write
    def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytearray([0x80 | opcode])
        mask_bit = 0x80 if self.mask_outgoing else 0
        n = len(payload)
        if n < 126:
            header.append(mask_bit | n)
        elif n < 65536:
            header.append(mask_bit | 126)
            header += struct.pack("!H", n)
        else:
            header.append(mask_bit | 127)
            header += struct.pack("!Q", n)
        if self.mask_outgoing:
            mask = os.urandom(4)
            header += mask
            payload = _unmask(payload, mask)
        with self._send_lock:
            self.sock.sendall(bytes(header) + payload)
            self.bytes_out += n

    def send_text(self, text: str) -> None:
        if self.closed:
            raise WSClosed(self.close_code or 1006)
        self._send_frame(OP_TEXT, text.encode("utf-8"))

    def send_binary(self, data: bytes) -> None:
        if self.closed:
            raise WSClosed(self.close_code or 1006)
        self._send_frame(OP_BIN, data)

    def ping(self, data: bytes = b"gx") -> None:
        if not self.closed:
            self._send_frame(OP_PING, data)

    def close(self, code: int = 1000, reason: str = "") -> None:
        if self.closed:
            return
        self.closed = True
        self.close_code = code
        try:
            self._send_frame(OP_CLOSE, struct.pack("!H", code) + reason.encode("utf-8")[:120])
        except OSError:
            pass
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


def _unmask(data: bytes, mask: bytes) -> bytes:
    if not data:
        return data
    n = len(data)
    key = int.from_bytes((mask * (n // 4 + 1))[:n], "little")
    return (int.from_bytes(data, "little") ^ key).to_bytes(n, "little")


def connect(host: str, port: int, path: str, headers: dict[str, str], timeout: float = 10.0) -> WebSocket:
    """Client handshake (used for the engine on host loopback)."""
    sock = socket.create_connection((host, port), timeout=timeout)
    key = base64.b64encode(os.urandom(16)).decode()
    lines = [f"GET {path} HTTP/1.1", f"Host: {host}:{port}", "Upgrade: websocket", "Connection: Upgrade",
             f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13"]
    lines += [f"{k}: {v}" for k, v in headers.items()]
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
    rfile = sock.makefile("rb")
    status = rfile.readline(1024).decode("latin-1").strip()
    resp_headers: dict[str, str] = {}
    while True:
        line = rfile.readline(8192).decode("latin-1")
        if line in ("\r\n", "\n", ""):
            break
        k, _, v = line.partition(":")
        resp_headers[k.strip().lower()] = v.strip()
    parts = status.split(" ", 2)
    if len(parts) < 2 or parts[1] != "101":
        length = int(resp_headers.get("content-length", "0") or 0)
        body = rfile.read(min(length, 65536)) if length else b""
        sock.close()
        raise ConnectionRefusedError(f"engine refused the stream: {status} {body[:300]!r}")
    if resp_headers.get("sec-websocket-accept") != accept_key(key):
        sock.close()
        raise WSProtocolError("bad Sec-WebSocket-Accept from engine")
    sock.settimeout(None)
    return WebSocket(sock, rfile, mask_outgoing=True, require_masked=False)
