"""The other memory tenant on gx10-02 that the router must account for (D-038).

gx-music's supervisor runs on the same node. Its open ``/health`` says whether
the ACE-Step engine is loaded, loading or rendering, and how much of its
memory is not visible in MemAvailable yet (``pending_gib``). With the
supervisor's key the router may ask it to unload an IDLE engine through the
supervisor's own lifecycle path (``POST /v1/music/unload`` with
``if_idle``), exactly like the Control Center does. The router never touches
the engine container itself.

gx-reason is not a client of this module: its llama-swap start command frees
ComfyUI first, and while it is loaded the router simply sees less
MemAvailable and waits.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger("gx-media.tenants")

#: Lower bound of the measured ACE-Step resident size (24-28 GiB, 2026-09-17),
#: used only to decide whether evicting an idle engine could make room.
MUSIC_LOADED_FLOOR_GIB = 24.0


@dataclass(frozen=True)
class MusicState:
    reachable: bool
    engine: str = "unknown"          # unloaded | loading | ready | unloading | failed
    busy: bool = False               # a job holds the engine (rendering or loading for it)
    active_jobs: int = 0             # queued + running music jobs
    pending_gib: float = 0.0         # growth that MemAvailable does not show yet
    loaded_gib: float | None = None  # measured when the engine became ready
    estimate_gib: float = 32.0
    pinned: bool = False
    idle_seconds: float | None = None
    error: str | None = None

    @property
    def holds_memory(self) -> bool:
        return self.reachable and self.engine in ("ready", "loading", "unloading")

    @property
    def signature(self) -> tuple:
        """Changes whenever the engine's memory changes (for held-memory measurements)."""
        return (self.reachable, self.engine)

    def public(self) -> dict:
        return asdict(self)


class MusicTenant:
    """Client for the gx-music supervisor. Disabled when ``url`` is empty."""

    def __init__(self, url: str, key_file: str = "", *, timeout: float = 3.0, cache_seconds: float = 2.0) -> None:
        self.url = url.rstrip("/")
        self.key_file = key_file
        self.timeout = timeout
        self.cache_seconds = cache_seconds
        self._cached: tuple[float, MusicState | None] = (0.0, None)

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def _key(self) -> str:
        if not self.key_file:
            return ""
        try:
            return Path(self.key_file).read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    @property
    def can_unload(self) -> bool:
        return self.enabled and bool(self._key())

    def state(self, *, fresh: bool = True) -> MusicState | None:
        if not self.enabled:
            return None
        now = time.monotonic()
        if not fresh and self._cached[1] is not None and now - self._cached[0] < self.cache_seconds:
            return self._cached[1]
        try:
            with urllib.request.urlopen(f"{self.url}/health", timeout=self.timeout) as res:
                body = json.load(res)
        except (OSError, ValueError) as exc:
            state = MusicState(reachable=False, error=str(exc)[:200])
        else:
            state = _parse(body)
        self._cached = (now, state)
        return state

    def unload_if_idle(self) -> tuple[bool, dict]:
        """Ask the supervisor to unload its engine only if nothing is using it.

        Returns (unloaded, supervisor response). A 409 means the engine is busy,
        has queued work or is pinned; nothing is interrupted.
        """
        key = self._key()
        if not key:
            return False, {"reason": "no gx-music key configured"}
        req = urllib.request.Request(
            f"{self.url}/v1/music/unload", data=json.dumps({"if_idle": True}).encode(), method="POST",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as res:
                body = json.load(res)
        except urllib.error.HTTPError as exc:
            try:
                err = json.load(exc)
            except (OSError, ValueError):
                err = {}
            message = ((err.get("error") or {}).get("message") if isinstance(err, dict) else None) or f"HTTP {exc.code}"
            return False, {"reason": message, "status": exc.code}
        except (OSError, ValueError) as exc:
            return False, {"reason": str(exc)[:200]}
        finally:
            self._cached = (0.0, None)
        return True, body if isinstance(body, dict) else {}


def _num(value: object, default: float | None = None) -> float | None:
    try:
        return None if value is None else float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _parse(body: object) -> MusicState:
    if not isinstance(body, dict):
        return MusicState(reachable=False, error="unexpected /health body")
    mem = body.get("memory") if isinstance(body.get("memory"), dict) else {}
    engine = str(body.get("engine") or "unknown")
    estimate = _num(mem.get("estimate_gib"), 32.0) or 32.0
    if "pending_gib" in mem:
        pending = max(0.0, _num(mem.get("pending_gib"), 0.0) or 0.0)
    else:
        # A supervisor that does not publish its pending memory: while it loads,
        # assume none of the engine is in MemAvailable yet.
        pending = estimate if engine == "loading" else 0.0
    return MusicState(
        reachable=True,
        engine=engine,
        busy=bool(body.get("busy")),
        active_jobs=int(_num(body.get("active_jobs"), 0) or 0),
        pending_gib=pending,
        loaded_gib=_num(mem.get("loaded_gib")),
        estimate_gib=estimate,
        pinned=bool(body.get("pinned")),
        idle_seconds=_num(body.get("idle_seconds")),
    )
