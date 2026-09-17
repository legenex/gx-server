"""The node-2 pending-memory contract (D-038, generalised for Build V3).

Every memory tenant on gx10-02 publishes an OPEN ``GET /health``::

    {"service": "gx-voice", "state": "unloaded|loading|ready|busy|unloading|failed",
     "busy": false, "pinned": false, "active_jobs": 0, "active_sessions": 0,
     "memory": {"pending_gib": 0.0, "resident_gib": 0.0, "estimate_gib": 14.0}}

``pending_gib`` is memory the tenant has been granted (or is loading) that
MemAvailable does not show yet. Before any tenant starts something heavy it
requires::

    MemAvailable - sum(peers' pending_gib) - own growth >= 30 GiB (reserve)

This module reads the peers. It never unloads anything and needs no key.
The media router keeps a self-contained copy of ``parse_health`` (its
container only ships its own package); ``tests/test_contract.py`` keeps the
two identical.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass

FABRIC_NODE2 = "192.168.100.11"
#: Default peers: name -> open health URL (fabric address only, L-3).
DEFAULT_PEERS: dict[str, str] = {
    "gx-media-router": f"http://{FABRIC_NODE2}:18800/health",
    "gx-music": f"http://{FABRIC_NODE2}:18820/health",
    "gx-voice": f"http://{FABRIC_NODE2}:18830/health",
    "gx-call": f"http://{FABRIC_NODE2}:18840/health",
    "gx-live": f"http://{FABRIC_NODE2}:18850/health",
}
#: States in which a tenant holds (or is taking) memory.
HOLDING_STATES = frozenset({"loading", "ready", "busy", "unloading", "generating", "running"})


@dataclass(frozen=True)
class PeerState:
    name: str
    reachable: bool
    state: str = "unknown"
    busy: bool = False
    pinned: bool = False
    active: int = 0
    pending_gib: float = 0.0
    resident_gib: float | None = None
    estimate_gib: float | None = None
    error: str | None = None

    @property
    def holds_memory(self) -> bool:
        return self.reachable and self.state in HOLDING_STATES

    def public(self) -> dict:
        d = asdict(self)
        d["holds_memory"] = self.holds_memory
        return d


def _num(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        out = None if value is None else float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if out is None or out != out or out in (float("inf"), float("-inf")):
        return None
    return out


def parse_health(name: str, body: object) -> PeerState:
    """Interpret one tenant's /health body (pure).

    A tenant that does not publish ``pending_gib`` while it is loading is
    assumed to hold none of its estimate in MemAvailable yet (the router's
    music rule). The media router publishes ``busy`` and ``memory.pending_gib``
    but no ``state``; it is "busy" while a job runs and "ready" otherwise.
    """
    if not isinstance(body, Mapping):
        return PeerState(name, False, error="unexpected /health body")
    raw_mem = body.get("memory")
    mem: Mapping = raw_mem if isinstance(raw_mem, Mapping) else {}
    state = body.get("state") if isinstance(body.get("state"), str) else body.get("engine")
    busy = bool(body.get("busy"))
    if not isinstance(state, str) or not state:
        state = "busy" if busy else ("ready" if body.get("resident_alias") else "unloaded")
    state = state.lower()[:24]
    estimate = _num(mem.get("estimate_gib"))
    pending_raw = _num(mem.get("pending_gib"))
    if pending_raw is not None:
        pending = max(0.0, pending_raw)
    else:
        pending = (estimate or 0.0) if state == "loading" else 0.0
    resident = _num(mem.get("resident_gib"))
    if resident is None:
        resident = _num(mem.get("loaded_gib"))
    active = 0
    for field in ("active_jobs", "active_sessions"):
        value = _num(body.get(field))
        active += int(value) if value and value > 0 else 0
    return PeerState(name=name, reachable=True, state=state, busy=busy, pinned=bool(body.get("pinned")),
                     active=active, pending_gib=round(pending, 3), resident_gib=resident, estimate_gib=estimate)


def parse_peer_map(raw: str) -> dict[str, str]:
    """``name=url,name=url`` -> dict. Only http URLs on the fabric or loopback are accepted."""
    out: dict[str, str] = {}
    for item in (p.strip() for p in raw.split(",")):
        if not item:
            continue
        name, sep, url = item.partition("=")
        name, url = name.strip(), url.strip().rstrip("/")
        if not sep or not name.startswith("gx-") or not url.startswith("http://"):
            raise ValueError(f"invalid peer entry: {item!r}")
        host = url[7:].split("/", 1)[0].split(":", 1)[0]
        if not (host.startswith("192.168.100.") or host.startswith("192.168.101.") or host in
                ("127.0.0.1", "localhost")):
            raise ValueError(f"peer {name} must be on the fabric or loopback (L-3), not {host}")
        out[name] = url if url.endswith("/health") else url + "/health"
    return out


Fetcher = Callable[[str, float], object]


def _http_fetch(url: str, timeout: float) -> object:
    with urllib.request.urlopen(url, timeout=timeout) as res:  # noqa: S310 - fixed fabric URLs
        return json.loads(res.read(65536))


class PeerTenants:
    """Reads the other node-2 tenants' /health (parallel, cached briefly)."""

    def __init__(self, peers: Mapping[str, str], *, exclude: str | None = None, timeout: float = 2.0,
                 cache_seconds: float = 2.0, fetch: Fetcher | None = None) -> None:
        self.peers = {k: v for k, v in peers.items() if k != exclude}
        self.timeout = timeout
        self.cache_seconds = cache_seconds
        self._fetch = fetch or _http_fetch
        self._lock = threading.Lock()
        self._cache: tuple[float, dict[str, PeerState]] = (0.0, {})

    @classmethod
    def from_env(cls, *, exclude: str | None = None, **kw) -> PeerTenants:
        raw = os.environ.get("GX_NODE2_PEERS", "")
        peers = parse_peer_map(raw) if raw.strip() else dict(DEFAULT_PEERS)
        return cls(peers, exclude=exclude, **kw)

    def _one(self, name: str, url: str) -> PeerState:
        try:
            return parse_health(name, self._fetch(url, self.timeout))
        except (OSError, ValueError) as exc:
            return PeerState(name, False, error=str(exc)[:160])

    def states(self, *, fresh: bool = False) -> dict[str, PeerState]:
        now = time.monotonic()
        with self._lock:
            if not fresh and self._cache[1] and now - self._cache[0] < self.cache_seconds:
                return dict(self._cache[1])
        if not self.peers:
            return {}
        with ThreadPoolExecutor(max_workers=min(8, len(self.peers))) as pool:
            futures = {n: pool.submit(self._one, n, u) for n, u in self.peers.items()}
            result = {n: f.result() for n, f in futures.items()}
        with self._lock:
            self._cache = (time.monotonic(), result)
        return result

    def pending_gib(self, *, fresh: bool = True) -> float:
        return round(sum(s.pending_gib for s in self.states(fresh=fresh).values() if s.reachable), 3)

    def snapshot(self, *, fresh: bool = False) -> dict[str, dict]:
        return {n: s.public() for n, s in self.states(fresh=fresh).items()}
