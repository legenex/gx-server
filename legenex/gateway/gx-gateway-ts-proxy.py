#!/usr/bin/env python3
"""Public Tailscale front door for LiteLLM.

Docker keeps the gateway on loopback (boot-safe). This process binds the
host Tailscale IPv4:4000 and forwards to 127.0.0.1:4000.

GET /v1/models and GET /models are filtered to the four public logical
modes. Internal worker aliases remain callable (gx-max dual-worker).
"""
from __future__ import annotations

import json
import os
import select
import socket
import subprocess
import sys
import time

UPSTREAM = ("127.0.0.1", 4000)
PUBLIC_MODELS = ("gx-mini", "gx-code", "gx-auto", "gx-max")


def tailscale_ipv4(timeout: float = 120.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            out = subprocess.run(
                ["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=5
            )
            ip = (out.stdout or "").strip().splitlines()[0] if out.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            ip = ""
        if ip.startswith("100."):
            try:
                addrs = subprocess.run(
                    ["ip", "-4", "addr", "show"], capture_output=True, text=True, timeout=5
                )
                if f"inet {ip}/" in (addrs.stdout or ""):
                    return ip
            except (OSError, subprocess.SubprocessError):
                pass
        time.sleep(2)
    raise SystemExit("gx-gateway-ts-proxy: no Tailscale IPv4 after wait")


def _recv_headers(sock: socket.socket, limit: int = 65536) -> bytes:
    buf = b""
    sock.settimeout(30)
    while b"\r\n\r\n" not in buf and len(buf) < limit:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    return buf


def _is_models_get(head: bytes) -> bool:
    line = head.split(b"\r\n", 1)[0].decode("latin1", "replace")
    parts = line.split(" ")
    if len(parts) < 2 or parts[0].upper() != "GET":
        return False
    path = parts[1].split("?", 1)[0]
    return path in ("/v1/models", "/models", "/v1/models/", "/models/")


def _filter_models_body(body: bytes) -> bytes:
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return body
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        return body
    data["data"] = [
        m for m in data["data"]
        if isinstance(m, dict) and m.get("id") in PUBLIC_MODELS
    ]
    return json.dumps(data, separators=(",", ":")).encode("utf-8")


def _splice(a: socket.socket, b: socket.socket) -> None:
    a.setblocking(False)
    b.setblocking(False)
    sockets = [a, b]
    try:
        while sockets:
            r, _, x = select.select(sockets, [], sockets, 300)
            if x:
                break
            if not r:
                break
            for src in r:
                dst = b if src is a else a
                try:
                    data = src.recv(65536)
                except BlockingIOError:
                    continue
                if not data:
                    sockets = []
                    break
                dst.setblocking(True)
                dst.sendall(data)
                dst.setblocking(False)
    finally:
        for s in (a, b):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            s.close()


def _handle(client: socket.socket) -> None:
    up = None
    try:
        head = _recv_headers(client)
        if not head:
            return
        up = socket.create_connection(UPSTREAM, timeout=10)
        up.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if not _is_models_get(head):
            up.sendall(head)
            _splice(client, up)
            up = None
            return
        up.sendall(head)
        resp = _recv_headers(up)
        sep = resp.find(b"\r\n\r\n")
        if sep < 0:
            client.sendall(resp)
            return
        headers, rest = resp[:sep], resp[sep + 4:]
        header_lines = headers.split(b"\r\n")
        clen = None
        new_headers = [header_lines[0]]
        chunked = False
        for line in header_lines[1:]:
            low = line.lower()
            if low.startswith(b"content-length:"):
                try:
                    clen = int(line.split(b":", 1)[1].strip())
                except ValueError:
                    clen = None
                continue
            if low.startswith(b"transfer-encoding:") and b"chunked" in low:
                chunked = True
                continue
            if low.startswith(b"connection:"):
                continue
            new_headers.append(line)
        body = rest
        if clen is not None:
            while len(body) < clen:
                body += up.recv(65536)
            body = body[:clen]
        elif chunked:
            # LiteLLM models listing is small; read remaining until close/idle.
            up.settimeout(5)
            while True:
                try:
                    chunk = up.recv(65536)
                except socket.timeout:
                    break
                if not chunk:
                    break
                body += chunk
            # de-chunk if needed
            try:
                raw = b""
                src = body
                while src:
                    line, _, src = src.partition(b"\r\n")
                    n = int(line.split(b";", 1)[0], 16)
                    if n == 0:
                        break
                    raw += src[:n]
                    src = src[n:]
                    if src.startswith(b"\r\n"):
                        src = src[2:]
                body = raw
            except ValueError:
                pass
        else:
            up.settimeout(2)
            while True:
                try:
                    chunk = up.recv(65536)
                except socket.timeout:
                    break
                if not chunk:
                    break
                body += chunk
        filtered = _filter_models_body(body)
        out_headers = new_headers + [
            f"Content-Length: {len(filtered)}".encode(),
            b"Connection: close",
        ]
        client.sendall(b"\r\n".join(out_headers) + b"\r\n\r\n" + filtered)
    finally:
        client.close()
        if up is not None:
            try:
                up.close()
            except OSError:
                pass


def main() -> int:
    ip = os.environ.get("GX_TS_BIND") or tailscale_ipv4()
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((ip, 4000))
    srv.listen(128)
    print(f"gx-gateway-ts-proxy: {ip}:4000 -> 127.0.0.1:4000 (public models filtered)", flush=True)

    def _reap(_signum=None, _frame=None) -> None:
        while True:
            try:
                pid, _ = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                break
            if pid == 0:
                break

    import signal
    signal.signal(signal.SIGCHLD, _reap)

    while True:
        client, _ = srv.accept()
        client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        try:
            pid = os.fork()
            if pid == 0:
                srv.close()
                try:
                    _handle(client)
                finally:
                    os._exit(0)
            client.close()
        except OSError:
            _handle(client)
    return 0


if __name__ == "__main__":
    sys.exit(main())
