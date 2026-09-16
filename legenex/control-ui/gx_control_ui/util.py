"""Small shared helpers: an HTTP client, a TTL cache and a command runner.

`run()` only ever receives a fixed argument list assembled in this package;
there is no code path that turns browser input into a shell command.
"""

from __future__ import annotations

import json
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from .redact import redact


@dataclass
class HTTPResult:
    status: int
    body: bytes
    headers: dict[str, str]
    elapsed_ms: float

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8") or "null")

    def text(self, limit: int = 4000) -> str:
        return redact(self.body[:limit].decode("utf-8", "replace"))


class HTTPError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = redact(message)


def http(
    method: str,
    url: str,
    *,
    body: Any = None,
    headers: dict[str, str] | None = None,
    timeout: float = 5.0,
    raw_body: bytes | None = None,
) -> HTTPResult:
    """One HTTP request. Returns non-2xx responses (does not raise for them);
    raises HTTPError(0, ...) when the upstream could not be reached."""
    data = raw_body
    hdrs = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read()
            return HTTPResult(resp.status, payload, dict(resp.headers.items()),
                              (time.monotonic() - t0) * 1000)
    except urllib.error.HTTPError as exc:
        payload = exc.read() if exc.fp else b""
        return HTTPResult(exc.code, payload, dict(exc.headers.items()) if exc.headers else {},
                          (time.monotonic() - t0) * 1000)
    except (urllib.error.URLError, OSError, socket.timeout, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        raise HTTPError(0, f"{method} {url.split('?')[0]} unreachable: {reason}") from None


def http_json(method: str, url: str, **kw: Any) -> tuple[int, Any]:
    res = http(method, url, **kw)
    try:
        return res.status, res.json()
    except ValueError:
        return res.status, {"raw": res.text(500)}


def bearer(token: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


def tcp_state(host: str, port: int, timeout: float = 2.0) -> str:
    """'open', 'refused' (kernel answered, nothing listening) or 'timeout'."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return "open"
    except ConnectionRefusedError:
        return "refused"
    except (socket.timeout, TimeoutError):
        return "timeout"
    except OSError:
        return "unreachable"


@dataclass
class CmdResult:
    rc: int
    out: str
    elapsed_ms: float

    @property
    def ok(self) -> bool:
        return self.rc == 0


def run(args: list[str], timeout: float = 10.0, input_text: str | None = None,
        merge_stderr: bool = True) -> CmdResult:
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            args,
            input=input_text,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if merge_stderr else subprocess.DEVNULL,
            stdin=None if input_text is not None else subprocess.DEVNULL,
            text=True,
            timeout=timeout,
        )
        return CmdResult(proc.returncode, proc.stdout or "", (time.monotonic() - t0) * 1000)
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", "replace")
        return CmdResult(124, (out or "") + f"\n[timed out after {timeout:.0f}s]",
                         (time.monotonic() - t0) * 1000)
    except OSError as exc:
        return CmdResult(127, f"cannot run {args[0]}: {exc}", (time.monotonic() - t0) * 1000)


def ssh_args(target: str, connect_timeout: int = 5) -> list[str]:
    return [
        "ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={connect_timeout}",
        "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2", target,
    ]


class TTLCache:
    """Stale-while-revalidate cache for one value.

    A fresh value is returned immediately. A stale value is returned
    immediately too, while one background thread refreshes it, so a slow
    upstream (node 2 over SSH) never stalls a page load. Only the very first
    call waits, bounded by `first_wait`.
    """

    def __init__(self, fn: Callable[[], Any], ttl: float, first_wait: float = 15.0) -> None:
        self._fn = fn
        self._ttl = ttl
        self._first_wait = first_wait
        self._lock = threading.Lock()
        self._value: Any = None
        self._stamp = 0.0
        self._refreshing = False
        self._done = threading.Event()

    def _refresh(self) -> None:
        try:
            value = self._fn()
        except Exception as exc:  # noqa: BLE001 - surfaced as data, never raised
            value = {"error": redact(f"{type(exc).__name__}: {exc}")}
        with self._lock:
            self._value, self._stamp, self._refreshing = value, time.time(), False
        self._done.set()

    def get(self, max_age: float | None = None) -> Any:
        ttl = self._ttl if max_age is None else max_age
        with self._lock:
            fresh = self._stamp and time.time() - self._stamp < ttl
            if fresh:
                return self._value
            start = not self._refreshing
            if start:
                self._refreshing = True
                self._done.clear()
            have_value = bool(self._stamp)
        if start:
            threading.Thread(target=self._refresh, daemon=True, name="ttlcache").start()
        if not have_value:
            self._done.wait(self._first_wait)
        with self._lock:
            return self._value

    def invalidate(self) -> None:
        with self._lock:
            self._stamp = 0.0

    @property
    def age(self) -> float | None:
        with self._lock:
            return time.time() - self._stamp if self._stamp else None
