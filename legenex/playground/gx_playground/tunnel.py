"""Realtime WebSocket tunnel (Build V3, Part 11; contract: coordination/build-v3/plt.md section 1).

    client --WS--> Playground  GET /rt/<call|live>/<session_id>[?ticket=...]
                     1. strict path/header validation
                     2. POST /api/realtime/authorize on the Control Center (loopback + proxy token)
                     3. TCP to the FIXED node-2 target from the grant (fabric)
                     4. upgrade request rewritten: service path, bearer key, no client cookies/auth
                     5. relay the 101, then splice bytes both ways (one selector loop)

The splice parses WebSocket frame HEADERS only (never payloads) in both
directions: client frames must be masked and below the size limit, and the
tunnel can inject a close frame only on a frame boundary. One thread per
tunnel (the request thread) drives both sockets, so a TLS socket is never used
by two threads at once.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import logging
import os
import re
import secrets
import selectors
import socket
import ssl
import struct
import threading
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("gx.playground.tunnel")

RT_PATH = re.compile(r"^/rt/(call|live)/((?:call|live)_[0-9a-f]{32})$")
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
KEY_RE = re.compile(r"^[A-Za-z0-9+/]{22}==$")
PROTOCOL_RE = re.compile(r"^[A-Za-z0-9._\-]{1,64}$")
TICKET_RE = re.compile(r"^[A-Za-z0-9._\-]{20,200}$")
MAX_HEAD = 16 * 1024
BUF_HIGH_WATER = 4 * 1024 * 1024
OPCODES = frozenset({0x0, 0x1, 0x2, 0x8, 0x9, 0xA})


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        value = int(os.environ.get(name, "") or default)
    except ValueError:
        return default
    return max(lo, min(hi, value))


@dataclass(frozen=True)
class Limits:
    enabled: bool = True
    idle_s: int = 120
    max_s: int = 14400
    max_frame: int = 4 * 1024 * 1024
    max_bytes: int = 4 * 1024 ** 3
    per_owner: int = 4
    total: int = 32
    connect_timeout_s: float = 5.0
    handshake_timeout_s: float = 10.0

    @classmethod
    def from_env(cls) -> Limits:
        return cls(
            enabled=os.environ.get("GX_PG_RT_ENABLED", "1") != "0",
            idle_s=_env_int("GX_PG_RT_IDLE_S", 120, 5, 3600),
            max_s=_env_int("GX_PG_RT_MAX_S", 14400, 10, 86400),
            max_frame=_env_int("GX_PG_RT_MAX_FRAME", 4 * 1024 * 1024, 1024, 64 * 1024 * 1024),
            max_bytes=_env_int("GX_PG_RT_MAX_BYTES", 4 * 1024 ** 3, 1024, 64 * 1024 ** 3),
            per_owner=_env_int("GX_PG_RT_PER_OWNER", 4, 1, 64),
            total=_env_int("GX_PG_RT_TOTAL", 32, 1, 512),
        )


class TunnelError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class FrameError(Exception):
    def __init__(self, close_code: int, reason: str) -> None:
        super().__init__(reason)
        self.close_code = close_code
        self.reason = reason


class FrameScanner:
    """Incremental RFC 6455 frame-header scanner (payload bytes are skipped, not kept)."""

    def __init__(self, *, require_mask: bool, max_payload: int) -> None:
        self.require_mask = require_mask
        self.max_payload = max_payload
        self._hdr = bytearray()
        self._remaining = 0
        self.frames = 0

    @property
    def at_boundary(self) -> bool:
        return self._remaining == 0 and not self._hdr

    def feed(self, data: bytes | memoryview) -> None:
        view = memoryview(data)
        i, n = 0, len(view)
        while i < n:
            if self._remaining:
                take = min(self._remaining, n - i)
                self._remaining -= take
                i += take
                continue
            need = self._need()
            take = min(need - len(self._hdr), n - i)
            self._hdr += view[i:i + take]
            i += take
            if len(self._hdr) == self._need():
                self._complete()

    def _need(self) -> int:
        if len(self._hdr) < 2:
            return 2
        b1 = self._hdr[1]
        ln = b1 & 0x7F
        return 2 + (2 if ln == 126 else 8 if ln == 127 else 0) + (4 if b1 & 0x80 else 0)

    def _complete(self) -> None:
        b0, b1 = self._hdr[0], self._hdr[1]
        fin, rsv, opcode = b0 & 0x80, b0 & 0x70, b0 & 0x0F
        masked = bool(b1 & 0x80)
        ln = b1 & 0x7F
        if rsv:
            raise FrameError(1002, "reserved bits set (no extensions are negotiated)")
        if opcode not in OPCODES:
            raise FrameError(1002, "unknown opcode")
        if self.require_mask and not masked:
            raise FrameError(1002, "client frames must be masked")
        if ln == 126:
            length = struct.unpack("!H", bytes(self._hdr[2:4]))[0]
        elif ln == 127:
            length = struct.unpack("!Q", bytes(self._hdr[2:10]))[0]
            if length >> 63:
                raise FrameError(1002, "invalid frame length")
        else:
            length = ln
        if opcode >= 0x8 and (not fin or length > 125):
            raise FrameError(1002, "invalid control frame")
        if length > self.max_payload:
            raise FrameError(1009, "frame too big")
        self._hdr.clear()
        self._remaining = length
        self.frames += 1


def close_frame(code: int, reason: str, *, masked: bool) -> bytes:
    payload = struct.pack("!H", code) + reason.encode()[:120]
    if not masked:
        return bytes([0x88, len(payload)]) + payload
    mask = secrets.token_bytes(4)
    body = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return bytes([0x88, 0x80 | len(payload)]) + mask + body


def accept_for(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()  # noqa: S324 - RFC 6455


# ------------------------------------------------------------ registry
class Tunnel:
    def __init__(self, service: str, session_id: str, owner: str) -> None:
        self.service = service
        self.session_id = session_id
        self.owner = owner
        self.stop = threading.Event()
        self.stop_reason = ""
        self.started = time.time()

    def kill(self, reason: str) -> None:
        self.stop_reason = self.stop_reason or reason
        self.stop.set()


class TunnelRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_session: dict[str, Tunnel] = {}

    def acquire(self, service: str, session_id: str, owner: str, limits: Limits) -> Tunnel:
        with self._lock:
            old = self._by_session.get(session_id)
            live = [t for t in self._by_session.values() if t is not old]
            if len(live) >= limits.total:
                raise TunnelError(429, "too_many_connections", "the realtime tunnel is at capacity; try again")
            if sum(1 for t in live if t.owner == owner) >= limits.per_owner:
                raise TunnelError(429, "too_many_connections",
                                  f"at most {limits.per_owner} realtime connections at a time")
            if old is not None:
                old.kill("replaced")
            tunnel = Tunnel(service, session_id, owner)
            self._by_session[session_id] = tunnel
            return tunnel

    def release(self, tunnel: Tunnel) -> None:
        with self._lock:
            if self._by_session.get(tunnel.session_id) is tunnel:
                del self._by_session[tunnel.session_id]

    def count(self) -> int:
        with self._lock:
            return len(self._by_session)

    def shutdown(self) -> None:
        with self._lock:
            for t in self._by_session.values():
                t.kill("shutdown")


# ------------------------------------------------------------ handshake helpers
def validate_client_upgrade(headers: Any) -> tuple[str, list[str]]:
    """Returns (Sec-WebSocket-Key, requested protocols) or raises TunnelError(400)."""
    if (headers.get("Upgrade") or "").strip().lower() != "websocket":
        raise TunnelError(426, "upgrade_required", "this endpoint only accepts WebSocket upgrades")
    tokens = {t.strip().lower() for t in (headers.get("Connection") or "").split(",")}
    if "upgrade" not in tokens:
        raise TunnelError(400, "invalid_request", "Connection: Upgrade is required")
    if (headers.get("Sec-WebSocket-Version") or "").strip() != "13":
        raise TunnelError(426, "upgrade_required", "WebSocket version 13 is required")
    key = (headers.get("Sec-WebSocket-Key") or "").strip()
    if not KEY_RE.match(key) or len(base64.b64decode(key)) != 16:
        raise TunnelError(400, "invalid_request", "invalid Sec-WebSocket-Key")
    if headers.get("Content-Length") not in (None, "0") or headers.get("Transfer-Encoding"):
        raise TunnelError(400, "invalid_request", "an upgrade request has no body")
    protocols = []
    for raw in headers.get_all("Sec-WebSocket-Protocol") or []:
        for p in raw.split(","):
            p = p.strip()
            if p:
                if not PROTOCOL_RE.match(p):
                    raise TunnelError(400, "invalid_request", "invalid Sec-WebSocket-Protocol")
                protocols.append(p)
    if len(protocols) > 8:
        raise TunnelError(400, "invalid_request", "too many subprotocols")
    return key, protocols


def ticket_from_query(query: str) -> str | None:
    if not query:
        return None
    try:
        params = urllib.parse.parse_qs(query, max_num_fields=8, strict_parsing=True)
    except ValueError as exc:
        raise TunnelError(400, "invalid_request", "invalid query string") from exc
    values = params.get("ticket") or []
    if len(values) > 1:
        raise TunnelError(400, "invalid_request", "one ticket only")
    if values and not TICKET_RE.match(values[0]):
        raise TunnelError(401, "bad_ticket", "invalid ticket")
    return values[0] if values else None


def read_head(sock: socket.socket, deadline: float) -> tuple[bytes, bytes]:
    """Read an HTTP response head (bounded); returns (head, bytes after it)."""
    buf = bytearray()
    while b"\r\n\r\n" not in buf:
        left = deadline - time.monotonic()
        if left <= 0:
            raise TunnelError(504, "upstream_timeout", "the realtime service did not answer the upgrade")
        sock.settimeout(left)
        chunk = sock.recv(4096)
        if not chunk:
            raise TunnelError(502, "upstream_unavailable", "the realtime service closed the connection")
        buf += chunk
        if len(buf) > MAX_HEAD:
            raise TunnelError(502, "upstream_unavailable", "oversized upgrade response")
    head, _, rest = bytes(buf).partition(b"\r\n\r\n")
    return head, rest


def parse_head(head: bytes) -> tuple[int, dict[str, str]]:
    lines = head.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ", 2)
    if len(parts) < 2 or not parts[0].startswith("HTTP/1.") or not parts[1].isdigit():
        raise TunnelError(502, "upstream_unavailable", "invalid upgrade response")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip().lower()] = value.strip()
    return int(parts[1]), headers


# ------------------------------------------------------------ the tunnel
class Pump:
    """Moves bytes between the client and the upstream socket (single thread)."""

    def __init__(self, client: socket.socket, upstream: socket.socket, tunnel: Tunnel, limits: Limits,
                 deadline: float) -> None:
        self.client = client
        self.upstream = upstream
        self.tunnel = tunnel
        self.limits = limits
        self.deadline = deadline
        self.to_upstream = bytearray()
        self.to_client = bytearray()
        # scanners run over the bytes as they are QUEUED for a destination
        self.client_frames = FrameScanner(require_mask=True, max_payload=limits.max_frame)
        self.server_frames = FrameScanner(require_mask=False, max_payload=1 << 62)
        self.bytes_in = 0      # client -> upstream
        self.bytes_out = 0     # upstream -> client
        self.last = time.monotonic()
        self.reason = ""
        self.client_closed = False
        self.upstream_closed = False

    def queue_to_upstream(self, data: bytes) -> None:
        self.client_frames.feed(data)
        self.to_upstream += data
        self.bytes_in += len(data)
        if self.bytes_in > self.limits.max_bytes:
            raise FrameError(1009, "connection byte limit reached")

    def queue_to_client(self, data: bytes) -> None:
        try:
            self.server_frames.feed(data)
        except FrameError:
            # the service speaks for itself; a bad server frame ends the tunnel
            raise FrameError(1011, "invalid frame from the realtime service") from None
        self.to_client += data
        self.bytes_out += len(data)
        if self.bytes_out > self.limits.max_bytes:
            raise FrameError(1009, "connection byte limit reached")

    @staticmethod
    def _recv(sock: socket.socket) -> bytes | None:
        """bytes, b'' at EOF, None when nothing is ready."""
        try:
            return sock.recv(65536)
        except (BlockingIOError, InterruptedError, ssl.SSLWantReadError, ssl.SSLWantWriteError):
            return None

    @staticmethod
    def _send(sock: socket.socket, buf: bytearray) -> None:
        while buf:
            try:
                n = sock.send(buf[:65536])
            except (BlockingIOError, InterruptedError, ssl.SSLWantReadError, ssl.SSLWantWriteError):
                return
            if n <= 0:
                return
            del buf[:n]

    def run(self, *, first_to_client: bytes = b"", first_to_upstream: bytes = b"") -> str:
        """Splice until one side closes or a limit is hit; returns the close reason.

        ``first_to_client`` are bytes the service sent right behind its 101,
        ``first_to_upstream`` bytes the client sent right behind its upgrade.
        """
        sel = selectors.DefaultSelector()
        self.client.setblocking(False)
        self.upstream.setblocking(False)
        try:
            if first_to_client:
                self.queue_to_client(first_to_client)
            if first_to_upstream:
                self.queue_to_upstream(first_to_upstream)
            while True:
                if self.tunnel.stop.is_set():
                    return self._finish(1001, self.tunnel.stop_reason or "closed")
                now = time.monotonic()
                if now - self.last > self.limits.idle_s:
                    return self._finish(1001, "idle timeout")
                if now > self.deadline:
                    return self._finish(1001, "maximum session duration reached")
                self._send(self.upstream, self.to_upstream)
                self._send(self.client, self.to_client)
                if (self.client_closed and not self.to_upstream) or (self.upstream_closed and not self.to_client):
                    return "client closed" if self.client_closed else "service closed"
                ev_client = selectors.EVENT_READ if len(self.to_upstream) < BUF_HIGH_WATER else 0
                ev_up = selectors.EVENT_READ if len(self.to_client) < BUF_HIGH_WATER else 0
                if self.to_client:
                    ev_client |= selectors.EVENT_WRITE
                if self.to_upstream:
                    ev_up |= selectors.EVENT_WRITE
                for sock, ev in ((self.client, ev_client), (self.upstream, ev_up)):
                    try:
                        sel.unregister(sock)
                    except KeyError:
                        pass
                    if ev:
                        sel.register(sock, ev)
                pending_tls = isinstance(self.client, ssl.SSLSocket) and self.client.pending() > 0 \
                    and len(self.to_upstream) < BUF_HIGH_WATER
                ready = sel.select(0 if pending_tls else 1.0)
                readable = {key.fileobj for key, mask in ready if mask & selectors.EVENT_READ}
                if pending_tls:
                    readable.add(self.client)
                if self.client in readable and not self.client_closed:
                    data = self._recv(self.client)
                    if data == b"":
                        self.client_closed = True
                    elif data:
                        self.last = time.monotonic()
                        self.queue_to_upstream(data)
                if self.upstream in readable and not self.upstream_closed:
                    data = self._recv(self.upstream)
                    if data == b"":
                        self.upstream_closed = True
                    elif data:
                        self.last = time.monotonic()
                        self.queue_to_client(data)
        except FrameError as exc:
            return self._finish(exc.close_code, exc.reason)
        except OSError as exc:
            return f"socket error: {type(exc).__name__}"
        finally:
            sel.close()

    def _finish(self, code: int, reason: str) -> str:
        """Close both sides with a close frame where the stream is on a frame boundary."""
        deadline = time.monotonic() + 2.0
        if self.server_frames.at_boundary and not self.client_closed:
            self.to_client += close_frame(code, reason, masked=False)
        if self.client_frames.at_boundary and not self.upstream_closed:
            self.to_upstream += close_frame(1001 if code != 1011 else 1011, "tunnel closed", masked=True)
        for sock, buf in ((self.client, self.to_client), (self.upstream, self.to_upstream)):
            while buf and time.monotonic() < deadline:
                before = len(buf)
                self._send(sock, buf)
                if len(buf) == before:
                    time.sleep(0.02)
        self._linger(deadline + 1.0)
        return f"{code} {reason}"

    def _linger(self, deadline: float) -> None:
        """Read (and drop) what the client still sends before closing: closing a
        socket with unread input makes the kernel send RST, which would discard
        the close frame the client has not read yet."""
        if self.client_closed:
            return
        if not isinstance(self.client, ssl.SSLSocket):
            try:
                self.client.shutdown(socket.SHUT_WR)
            except OSError:
                return
        while time.monotonic() < deadline:
            try:
                data = self.client.recv(65536)
            except (BlockingIOError, InterruptedError, ssl.SSLWantReadError, ssl.SSLWantWriteError):
                time.sleep(0.02)
                continue
            except OSError:
                return
            if not data:
                return


class Authorizer:
    """Asks the Control Center whether a tunnel may open (loopback + proxy token)."""

    def __init__(self, host: str, port: int, token_reader) -> None:
        self.host = host
        self.port = port
        self.token_reader = token_reader

    def __call__(self, *, service: str, session_id: str, ticket: str | None, cookie: str | None,
                 origin: str | None, host: str | None, scheme: str, client_ip: str) -> dict:
        body = json.dumps({"service": service, "session_id": session_id, "ticket": ticket,
                           "origin": origin, "host": host, "scheme": scheme}).encode()
        headers = {"Content-Type": "application/json", "Content-Length": str(len(body)),
                   "X-GX-Proxy-Token": self.token_reader(), "X-GX-Forwarded-For": client_ip,
                   "X-GX-Forwarded-Proto": scheme, "Host": f"{self.host}:{self.port}"}
        if cookie and not ticket:
            headers["Cookie"] = cookie
        conn = http.client.HTTPConnection(self.host, self.port, timeout=10)
        try:
            conn.request("POST", "/api/realtime/authorize", body=body, headers=headers)
            res = conn.getresponse()
            raw = res.read(65536)
        except OSError as exc:
            raise TunnelError(502, "bad_gateway", "the Control Center backend is not reachable") from exc
        finally:
            conn.close()
        try:
            data = json.loads(raw or b"{}")
        except ValueError as exc:
            raise TunnelError(502, "bad_gateway", "invalid authorisation answer") from exc
        if res.status != 200 or not isinstance(data, dict) or data.get("ok") is not True:
            err = (data.get("error") or {}) if isinstance(data, dict) else {}
            status = res.status if res.status in (400, 401, 403, 404, 409, 410, 429, 503) else 502
            raise TunnelError(status, str(err.get("code") or "forbidden"),
                              str(err.get("message") or "the realtime connection was refused"))
        target = data.get("target") or {}
        if not (isinstance(target.get("host"), str) and isinstance(target.get("port"), int)
                and re.fullmatch(r"(192\.168\.10[01]\.[0-9]{1,3}|127\.0\.0\.1)", target["host"])
                and isinstance(data.get("path"), str) and data["path"].startswith("/")
                and isinstance(data.get("authorization"), str) and data["authorization"].startswith("Bearer ")):
            raise TunnelError(502, "bad_gateway", "invalid authorisation answer")
        return data


def upstream_request(grant: dict, key: str, protocols: list[str], session_id: str, request_id: str) -> bytes:
    target = grant["target"]
    lines = [
        f"GET {grant['path']} HTTP/1.1",
        f"Host: {target['host']}:{target['port']}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}",
        "Sec-WebSocket-Version: 13",
        f"Authorization: {grant['authorization']}",
        f"X-GX-Session: {session_id}",
        f"X-GX-Owner: {grant.get('owner', '')}",
        f"X-GX-Request-Id: {request_id}",
    ]
    if protocols:
        lines.append(f"Sec-WebSocket-Protocol: {', '.join(protocols)}")
    for line in lines:
        if "\r" in line or "\n" in line:
            raise TunnelError(502, "bad_gateway", "invalid header value")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")
