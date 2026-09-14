"""Minimal stdlib HTTP client for proxying OpenAI-compatible traffic.

Deliberately dependency-free: the orchestrator gates gx-max, so it must start
even on a node where pip has never been run.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterator, Mapping


@dataclass
class UpstreamResponse:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


class UpstreamError(RuntimeError):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"upstream returned {status}: {body[:500]}")
        self.status = status
        self.body = body


def _request(
    url: str,
    payload: Mapping[str, Any] | None,
    headers: Mapping[str, str],
    timeout: float,
    method: str = "POST",
) -> urllib.request.Request:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in headers.items():
        if v:
            req.add_header(k, v)
    return req


def post_json(
    url: str,
    payload: Mapping[str, Any],
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float = 900.0,
) -> UpstreamResponse:
    """POST JSON and buffer the whole response."""
    req = _request(url, payload, headers or {}, timeout)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return UpstreamResponse(resp.status, dict(resp.headers), resp.read())
    except urllib.error.HTTPError as exc:
        raise UpstreamError(exc.code, exc.read().decode("utf-8", "replace")) from exc


def stream_post(
    url: str,
    payload: Mapping[str, Any],
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float = 900.0,
    chunk_size: int = 8192,
) -> Iterator[bytes]:
    """POST JSON and yield the response body as it arrives (for SSE streaming)."""
    req = _request(url, payload, headers or {}, timeout)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        raise UpstreamError(exc.code, exc.read().decode("utf-8", "replace")) from exc
    with resp:
        while True:
            chunk = resp.read(chunk_size)
            if not chunk:
                return
            yield chunk


def probe(url: str, timeout: float = 4.0) -> bool:
    """True if `url` answers with a 2xx."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False
