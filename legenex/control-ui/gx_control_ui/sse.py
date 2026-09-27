"""A small SSE channel: /api/stream (session-authenticated).

Multiplexes three event kinds, pushed as JSON SSE events:

* ``queue``      — scheduler snapshot digest (depth, active, per-profile),
                   emitted whenever the digest changes,
* ``lifecycle``  — gx-max lifecycle state, emitted on change,
* ``telemetry``  — a light head-node tick (memory, load, swap), every 2 s.

One background thread, one queue per client, stdlib only. Digests are
computed from the same cached Cluster reads the pages use, so the stream is
cheap and consistent; when the orchestrator is down the queue event carries
{"available": false, ...} — never fabricated numbers.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from typing import Any

from . import hostfacts
from .config import UIConfig

TICK_SECONDS = 2.0
CLIENT_BUFFER = 200


def _telemetry() -> dict:
    try:
        with open("/proc/meminfo", encoding="ascii") as fh:
            mem = {}
            for line in fh:
                key, _, value = line.partition(":")
                mem[key] = int(value.strip().split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        return {"error": "meminfo unreadable"}
    try:
        load1, _load5, _load15 = (float(x) for x in
                                  open("/proc/loadavg", encoding="ascii").read().split()[:3])
    except (OSError, ValueError):
        load1 = None
    return {"mem_available_gib": round(mem.get("MemAvailable", 0) / 2**30, 2),
            "swap_used_gib": round((mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)) / 2**30, 2),
            "load1": load1}


class SSEHub:
    """Fan-out hub. `serve(handler_stream)` in server.py writes the events."""

    def __init__(self, cfg: UIConfig, cluster) -> None:
        self.cfg = cfg
        self.cluster = cluster
        self._clients: list[queue.Queue] = []
        self._lock = threading.Lock()
        self._last: dict[str, Any] = {}
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------- clients
    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=CLIENT_BUFFER)
        with self._lock:
            self._clients.append(q)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, daemon=True, name="sse-hub")
                self._thread.start()
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._clients:
                self._clients.remove(q)

    def _push(self, kind: str, data: Any) -> None:
        event = {"type": kind, "ts": time.time(), "data": data}
        with self._lock:
            clients = list(self._clients)
        for q in clients:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass

    # ------------------------------------------------------------ the loop
    def _loop(self) -> None:
        while True:
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - the hub must never die
                pass
            time.sleep(TICK_SECONDS)

    def tick(self) -> None:
        """One fan-out pass. Also the unit-test entry point."""
        tel = _telemetry()
        if tel != self._last.get("telemetry"):
            self._last["telemetry"] = tel
            self._push("telemetry", tel)
        if not self.cfg.offline:
            gx = self.cluster.gxmax_state()
            if gx != self._last.get("lifecycle"):
                self._last["lifecycle"] = gx
                self._push("lifecycle", {"state": gx})
            snap = self.cluster.scheduler_snapshot(max_age=5)
            digest = {"available": snap.get("available"),
                      "queued": _count_state(snap, "queued"),
                      "active": _count_state(snap, "active")}
            if digest != self._last.get("queue"):
                self._last["queue"] = digest
                self._push("queue", digest)
        else:
            gx = "down" if self._last.get("lifecycle") != "down" else self._last.get("lifecycle")
            self._last["lifecycle"] = gx

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)


def _count_state(snap: dict, state: str) -> int | None:
    """Count requests in `state` from a scheduler snapshot, or None unknown."""
    if not snap.get("available"):
        return None
    for key in ("queue", "requests", "records"):
        raw = snap.get(key)
        if isinstance(raw, list):
            return sum(1 for r in raw if isinstance(r, dict) and r.get("state") == state)
    counts = snap.get("counts")
    if isinstance(counts, dict):
        value = counts.get(state)
        return int(value) if isinstance(value, (int, float)) else None
    return None
