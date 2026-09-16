"""Minimal, dependency-free ComfyUI client.

Only four upstream calls are ever made: ``/system_stats`` (health), ``/prompt``
(submit), ``/history/<id>`` (poll) and ``/view`` (fetch a result). The ``/view``
arguments are taken from ComfyUI's own history output, never from a caller, so
the file-read primitive is not reachable from the network.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable

from .errors import TimeoutError_, UpstreamError

log = logging.getLogger("gx-media.comfy")

# ComfyUI history entries name their artefacts under one of these output keys.
_ARTEFACT_KEYS = ("images", "gifs", "videos", "audio")


@dataclass(frozen=True)
class Artefact:
    """One file produced by a completed prompt."""

    filename: str
    subfolder: str
    type: str
    kind: str  # "images" | "videos" | ...
    #: produced by the workflow's declared thumbnail node
    thumbnail: bool = False

    @property
    def media_type(self) -> str:
        lower = self.filename.lower()
        if lower.endswith(".png"):
            return "image/png"
        if lower.endswith((".jpg", ".jpeg")):
            return "image/jpeg"
        if lower.endswith(".webp"):
            return "image/webp"
        if lower.endswith(".mp4"):
            return "video/mp4"
        if lower.endswith(".webm"):
            return "video/webm"
        return "application/octet-stream"


@dataclass(frozen=True)
class Result:
    prompt_id: str
    artefacts: tuple[Artefact, ...]
    elapsed_seconds: float


class ComfyClient:
    def __init__(self, base_url: str, *, connect_timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.connect_timeout = connect_timeout

    # -- transport ---------------------------------------------------------
    def _request(self, path: str, *, data: bytes | None = None, timeout: float | None = None) -> bytes:
        url = f"{self.base_url}{path}"
        request = urllib.request.Request(url, data=data, method="POST" if data else "GET")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.connect_timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            body = exc.read()[:2000].decode("utf-8", "replace")
            raise UpstreamError(f"ComfyUI {path} returned HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise UpstreamError(f"ComfyUI {path} unreachable at {self.base_url}: {exc.reason}") from exc
        except TimeoutError as exc:
            raise UpstreamError(f"ComfyUI {path} timed out after {timeout or self.connect_timeout}s") from exc

    def _json(self, path: str, *, data: dict | None = None, timeout: float | None = None) -> dict:
        payload = json.dumps(data).encode("utf-8") if data is not None else None
        raw = self._request(path, data=payload, timeout=timeout)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise UpstreamError(f"ComfyUI {path} returned non-JSON: {raw[:200]!r}") from exc

    # -- operations --------------------------------------------------------
    def system_stats(self) -> dict:
        return self._json("/system_stats")

    def queue_depth(self) -> int:
        queue = self._json("/queue")
        return len(queue.get("queue_running", [])) + len(queue.get("queue_pending", []))

    def submit(self, graph: dict, client_id: str) -> str:
        response = self._json("/prompt", data={"prompt": graph, "client_id": client_id}, timeout=120.0)
        node_errors = response.get("node_errors") or {}
        if node_errors or "prompt_id" not in response:
            raise UpstreamError(
                "ComfyUI rejected the graph: "
                + json.dumps(node_errors or response)[:1500]
            )
        return str(response["prompt_id"])

    def wait(
        self,
        prompt_id: str,
        *,
        timeout: float,
        poll_interval: float = 1.0,
        cancelled: Callable[[], bool] | None = None,
        thumbnail_node: str | None = None,
    ) -> Result:
        """Block until ``prompt_id`` completes, fails, or ``timeout`` elapses."""
        started = time.monotonic()
        deadline = started + timeout
        while True:
            if cancelled is not None and cancelled():
                raise UpstreamError("generation cancelled")
            history = self._json(f"/history/{urllib.parse.quote(prompt_id)}", timeout=30.0)
            entry = history.get(prompt_id)
            if entry is not None:
                status = entry.get("status") or {}
                completed = bool(status.get("completed"))
                status_str = str(status.get("status_str", ""))
                if status_str == "error" or (status_str and status_str != "success" and not completed):
                    raise UpstreamError(
                        "ComfyUI execution failed: " + _summarise_failure(status)
                    )
                if completed or status_str == "success":
                    artefacts = _collect_artefacts(entry.get("outputs") or {}, thumbnail_node)
                    if not artefacts:
                        raise UpstreamError(
                            f"ComfyUI prompt {prompt_id} completed but produced no output file"
                        )
                    return Result(prompt_id, tuple(artefacts), time.monotonic() - started)
            if time.monotonic() >= deadline:
                raise TimeoutError_(
                    f"generation did not finish within {timeout:.0f}s (prompt_id={prompt_id})"
                )
            time.sleep(poll_interval)

    def fetch(self, artefact: Artefact, *, timeout: float = 120.0) -> bytes:
        query = urllib.parse.urlencode(
            {
                "filename": artefact.filename,
                "subfolder": artefact.subfolder,
                "type": artefact.type,
            }
        )
        return self._request(f"/view?{query}", timeout=timeout)

    def free(self, *, unload_models: bool = True, free_memory: bool = True) -> None:
        """Ask ComfyUI to release model memory. Best effort."""
        payload = {"unload_models": unload_models, "free_memory": free_memory}
        try:
            self._request("/free", data=json.dumps(payload).encode("utf-8"), timeout=120.0)
        except UpstreamError as exc:  # pragma: no cover - best effort
            log.warning("free() failed: %s", exc)


def _collect_artefacts(outputs: dict, thumbnail_node: str | None = None) -> list[Artefact]:
    artefacts: list[Artefact] = []
    for node_id, node_output in outputs.items():
        for key in _ARTEFACT_KEYS:
            for item in node_output.get(key) or []:
                filename = item.get("filename")
                if not filename:
                    continue
                artefacts.append(
                    Artefact(
                        filename=str(filename),
                        subfolder=str(item.get("subfolder", "")),
                        type=str(item.get("type", "output")),
                        kind=key,
                        thumbnail=thumbnail_node is not None and str(node_id) == str(thumbnail_node),
                    )
                )
    # ComfyUI writes intermediate previews with type "temp"; keep real outputs.
    final = [a for a in artefacts if a.type == "output"]
    return final or artefacts


def _summarise_failure(status: dict) -> str:
    messages = []
    for entry in status.get("messages") or []:
        if isinstance(entry, list) and len(entry) == 2 and entry[0] == "execution_error":
            detail = entry[1] or {}
            messages.append(
                f"node {detail.get('node_id')} ({detail.get('node_type')}): "
                f"{detail.get('exception_type')}: {detail.get('exception_message')}"
            )
    return "; ".join(messages) or json.dumps(status)[:1000]
