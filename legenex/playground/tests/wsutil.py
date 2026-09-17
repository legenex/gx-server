"""Minimal RFC 6455 client and stub server for the tunnel tests (stdlib only)."""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import ssl
import struct
import threading

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def accept(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()  # noqa: S324


def encode(opcode: int, payload: bytes, *, masked: bool, fin: bool = True, length_override: int | None = None) -> bytes:
    head = bytes([(0x80 if fin else 0) | opcode])
    n = len(payload) if length_override is None else length_override
    mbit = 0x80 if masked else 0
    if n < 126:
        head += bytes([mbit | n])
    elif n < 65536:
        head += bytes([mbit | 126]) + struct.pack("!H", n)
    else:
        head += bytes([mbit | 127]) + struct.pack("!Q", n)
    if masked:
        mask = os.urandom(4)
        return head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return head + payload


class Reader:
    """Exact reads from a socket, starting with bytes already received."""

    def __init__(self, sock: socket.socket, initial: bytes = b"") -> None:
        self.sock = sock
        self.buf = bytearray(initial)

    def exact(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("closed")
            self.buf += chunk
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out


def read_frame(src: socket.socket | Reader) -> tuple[int, bytes]:
    r = src if isinstance(src, Reader) else Reader(src)
    b0, b1 = r.exact(2)
    n = b1 & 0x7F
    if n == 126:
        n = struct.unpack("!H", r.exact(2))[0]
    elif n == 127:
        n = struct.unpack("!Q", r.exact(8))[0]
    mask = r.exact(4) if b1 & 0x80 else None
    data = r.exact(n) if n else b""
    if mask:
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    return b0 & 0x0F, data


def read_head(sock: socket.socket) -> tuple[int, dict[str, str], bytes]:
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("closed before the response head")
        buf += chunk
    head, _, rest = buf.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split(" ")[1])
    headers = {}
    for line in lines[1:]:
        k, _, v = line.partition(":")
        headers[k.strip().lower()] = v.strip()
    return status, headers, rest


class Client:
    """Opens a WebSocket through the Playground; exposes the raw socket."""

    def __init__(self, port: int, path: str, *, cookie: str | None = None, origin: str | None = "same",
                 protocols: list[str] | None = None, tls_context: ssl.SSLContext | None = None,
                 host: str = "127.0.0.1", extra: dict[str, str] | None = None, timeout: float = 10) -> None:
        raw = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        self.sock: socket.socket = tls_context.wrap_socket(raw, server_hostname=host) if tls_context else raw
        self.key = base64.b64encode(os.urandom(16)).decode()
        hostport = f"{host}:{port}"
        scheme = "https" if tls_context else "http"
        lines = [f"GET {path} HTTP/1.1", f"Host: {hostport}", "Upgrade: websocket", "Connection: Upgrade",
                 f"Sec-WebSocket-Key: {self.key}", "Sec-WebSocket-Version: 13",
                 "Sec-WebSocket-Extensions: permessage-deflate; client_max_window_bits"]
        if origin == "same":
            lines.append(f"Origin: {scheme}://{hostport}")
        elif origin:
            lines.append(f"Origin: {origin}")
        if cookie:
            lines.append(f"Cookie: {cookie}")
        if protocols:
            lines.append(f"Sec-WebSocket-Protocol: {', '.join(protocols)}")
        for k, v in (extra or {}).items():
            lines.append(f"{k}: {v}")
        self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        self.status, self.headers, rest = read_head(self.sock)
        self.body = rest
        if self.status != 101:
            length = int(self.headers.get("content-length") or 0)
            while len(self.body) < length:
                chunk = self.sock.recv(4096)
                if not chunk:
                    break
                self.body += chunk
        self.reader = Reader(self.sock, rest if self.status == 101 else b"")

    def send(self, opcode: int, payload: bytes, **kw) -> None:
        self.sock.sendall(encode(opcode, payload, masked=kw.pop("masked", True), **kw))

    def recv(self) -> tuple[int, bytes]:
        return read_frame(self.reader)

    def close_code(self) -> int | None:
        """Read frames until a close frame (or EOF); return its code."""
        try:
            while True:
                op, data = self.recv()
                if op == 0x8:
                    return struct.unpack("!H", data[:2])[0] if len(data) >= 2 else 1005
        except (ConnectionError, OSError):
            return None

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class StubService:
    """A node-2 style WS service: bearer key, echo, and a few behaviours by path.

    /v1/call/sessions/<sid>/ws   echo (text/binary), answers ping with pong, close with close
    .../silent                    accepts and never sends anything
    .../refuse404                 answers 404 before the upgrade
    .../hello                     sends one text frame right after the 101
    """

    def __init__(self, key: str) -> None:
        self.key = key
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(32)
        self.port = self.sock.getsockname()[1]
        self.requests: list[dict] = []
        self.closes: list[int] = []
        self._stop = False
        threading.Thread(target=self._accept, daemon=True).start()

    def stop(self) -> None:
        self._stop = True
        self.sock.close()

    def _accept(self) -> None:
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        try:
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buf += chunk
            head = buf.split(b"\r\n\r\n")[0].decode("latin-1").split("\r\n")
            path = head[0].split(" ")[1]
            headers = {}
            for line in head[1:]:
                k, _, v = line.partition(":")
                headers[k.strip().lower()] = v.strip()
            self.requests.append({"path": path, "headers": headers})
            if headers.get("authorization") != f"Bearer {self.key}":
                conn.sendall(b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\n\r\n")
                return
            if path.endswith("/refuse404"):
                conn.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
                return
            resp = ["HTTP/1.1 101 Switching Protocols", "Upgrade: websocket", "Connection: Upgrade",
                    f"Sec-WebSocket-Accept: {accept(headers['sec-websocket-key'])}"]
            if "sec-websocket-protocol" in headers:
                resp.append(f"Sec-WebSocket-Protocol: {headers['sec-websocket-protocol'].split(',')[0].strip()}")
            first = encode(0x1, b"hello from node 2", masked=False) if path.endswith("/hello") else b""
            conn.sendall(("\r\n".join(resp) + "\r\n\r\n").encode() + first)
            if path.endswith("/silent"):
                while conn.recv(4096):
                    pass
                return
            reader = Reader(conn, buf.split(b"\r\n\r\n", 1)[1])
            while True:
                op, data = read_frame(reader)
                if op == 0x8:
                    self.closes.append(struct.unpack("!H", data[:2])[0] if len(data) >= 2 else 1005)
                    conn.sendall(encode(0x8, data[:2], masked=False))
                    return
                if op == 0x9:
                    conn.sendall(encode(0xA, data, masked=False))
                elif op in (0x1, 0x2):
                    conn.sendall(encode(op, data, masked=False))
        except (ConnectionError, OSError, IndexError, KeyError):
            return
        finally:
            conn.close()
