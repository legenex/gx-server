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


def get_json(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float = 4.0,
) -> UpstreamResponse:
    """GET `url` and buffer the response. Raises UpstreamError on a non-2xx.

    Used to probe a node's own service (e.g. llama-swap's `/v1/models`) for
    its REAL state, as opposed to `probe()`, which only answers true/false.
    """
    req = _request(url, None, headers or {}, timeout, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return UpstreamResponse(resp.status, dict(resp.headers), resp.read())
    except urllib.error.HTTPError as exc:
        raise UpstreamError(exc.code, exc.read().decode("utf-8", "replace")) from exc


def open_post(
    url: str,
    payload: Mapping[str, Any],
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float = 900.0,
):
    """POST JSON and return the open response once the status is known.

    The caller relays the body. An HTTP error is raised as UpstreamError
    BEFORE anything has been sent to the client, so a streamed request can
    still be answered with a proper error status (D-039: the old relay sent
    `200` first and then wrote the error into the chunked stream, which left
    clients waiting until their own timeout and retrying).
    """
    req = _request(url, payload, headers or {}, timeout)
    try:
        return urllib.request.urlopen(req, timeout=timeout)
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


def probe(
    url: str,
    timeout: float = 4.0,
    *,
    headers: Mapping[str, str] | None = None,
) -> bool:
    """True if `url` answers with a 2xx.

    `headers` matters: the LiteLLM gateway requires a bearer token, and an
    unauthenticated probe returns 401, which would make every tier look
    unavailable and push gx-auto into permanent fallback.
    """
    req = urllib.request.Request(url, method="GET")
    for k, v in (headers or {}).items():
        if v:
            req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False
