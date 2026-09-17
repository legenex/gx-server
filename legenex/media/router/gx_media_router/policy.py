"""Cluster resource policy as seen from the router (D-036).

gx10-02's guard directory (/srv/projects/gx-cluster/state/guard, mounted
read-only at /srv/guard) carries the cluster-wide signals the Control Center
and the gx-max lifecycle write:

    node2.gxmax-hold        gx-max is draining/owning node 2 (fresh = < 20 min)
    node2.maintenance-hold  Maintenance mode: no new heavy jobs
    pins.json               {"gx-image": {...}, "gx-video": {...}} keep-resident preferences

The router only READS them. A missing directory disables every rule, so the
router behaves exactly like 2.2 when the mount is absent (tests, dev).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

GXMAX_HOLD_TTL = 1200.0


@dataclass(frozen=True)
class Block:
    code: str
    message: str


class Policy:
    def __init__(self, guard_dir: str) -> None:
        self.dir = Path(guard_dir) if guard_dir else None

    def _age(self, name: str) -> float | None:
        if self.dir is None:
            return None
        try:
            return time.time() - (self.dir / name).stat().st_mtime
        except OSError:
            return None

    def maintenance(self) -> bool:
        return self._age("node2.maintenance-hold") is not None

    def gxmax_hold(self) -> bool:
        age = self._age("node2.gxmax-hold")
        return age is not None and age <= GXMAX_HOLD_TTL

    def block(self) -> Block | None:
        """Why a NEW job must not start now, or None."""
        if self.gxmax_hold():
            return Block("gx_max_active", "gx-max owns the cluster right now; media generation resumes when "
                                          "it is released")
        if self.maintenance():
            return Block("maintenance", "the cluster is in Maintenance mode; media generation resumes when "
                                        "Maintenance ends")
        return None

    def pinned(self) -> list[str]:
        if self.dir is None:
            return []
        try:
            data = json.loads((self.dir / "pins.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(data, dict):
            return []
        return sorted(a for a in ("gx-image", "gx-video") if isinstance(data.get(a), dict))

    def state(self) -> dict:
        return {"guard_dir_mounted": bool(self.dir and self.dir.is_dir()),
                "maintenance": self.maintenance(), "gxmax_hold": self.gxmax_hold(),
                "pinned": self.pinned()}
