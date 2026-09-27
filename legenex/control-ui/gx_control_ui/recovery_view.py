"""The Recovery page: incidents, restart attempts, backoff and memory events.

Sources (all read-only):
  * orchestrator lifecycle events (gx-max transitions),
  * the gx-hostwatch log tail,
  * the kernel/OOM journal (journalctl --user, /var/log fallback, read-only),
  * the DeepSeek watchdog incidents JSONL at
    /srv/projects/gx-cluster/state/watchdog/incidents.jsonl — written by a
    SEPARATE watchdog process (schema owner). This module only READS it and
    passes records through; when the file does not exist yet the page says so
    honestly instead of inventing incidents.

Incident record schema (as written by the watchdog): one JSON object per line
with at least {ts, kind, detail}; restart attempts carry {attempt, backoff_s}
when present. Unknown fields are passed through untouched.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .config import UIConfig
from .logs import tail_file
from .redact import redact
from .util import run

MAX_INCIDENTS = 200


class RecoveryView:
    def __init__(self, cfg: UIConfig) -> None:
        self.cfg = cfg

    # ---------------------------------------------------------- incidents
    def incidents(self) -> dict:
        path = Path(self.cfg.watchdog_incidents)
        if not path.is_file():
            return {"available": False,
                    "reason": "no incidents file yet — the watchdog writer is not deployed; "
                              "this page fills in as soon as it writes " + str(path),
                    "incidents": []}
        records: list[dict] = []
        try:
            lines = tail_file(str(path), MAX_INCIDENTS)
        except OSError as exc:
            return {"available": False, "reason": f"cannot read: {exc.strerror}", "incidents": []}
        for line in lines:
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                records.append({"kind": "unparsable", "raw": redact(line[:300])})
                continue
            if isinstance(rec, dict):
                records.append(rec)
        records.sort(key=lambda r: -float(r.get("ts") or 0))
        last = records[0] if records else None
        backoff = None
        if isinstance(last, dict) and "backoff_s" in last:
            backoff = last.get("backoff_s")
        return {"available": True, "path": str(path), "incidents": records,
                "restart_attempts": [r for r in records
                                      if isinstance(r, dict) and r.get("kind") == "restart"],
                "backoff_state": {"backoff_s": backoff, "last": last}}

    # ------------------------------------------------------------ journal
    def _journal(self) -> dict:
        """Kernel/OOM lines, read-only, best-effort with honest fallbacks."""
        out: dict[str, Any] = {"journal": [], "varlog": [], "note": ""}
        res = run(["journalctl", "--user", "-n", "400", "--no-pager", "-o", "short-iso"],
                  timeout=15)
        if res.ok:
            out["journal"] = [redact(line) for line in res.out.splitlines()
                             if "oom" in line.lower() or "out of memory" in line.lower()
                             or "killed process" in line.lower()][-50:]
        else:
            out["note"] = "journalctl not readable from this user session"
        try:
            for name in ("kern.log", "syslog"):
                p = Path("/var/log") / name
                if p.is_file():
                    lines = tail_file(str(p), 400)
                    out["varlog"] = [redact(line) for line in lines
                                     if "out of memory" in line.lower()
                                     or "oom-kill" in line.lower()][-50:]
                    if out["varlog"]:
                        break
        except OSError:
            pass
        return out

    # -------------------------------------------------------------- view
    def view(self, cluster) -> dict:
        lc = cluster.lifecycle.get() or {}
        ev = (lc.get("events") or {})
        body = ev.get("body") if ev.get("ok") else {}
        events = body if isinstance(body, dict) else {}
        hostwatch: list[str] = []
        hw_path = self.cfg.srv_logs / "gx-hostwatch.log"
        try:
            hostwatch = [redact(line) for line in tail_file(str(hw_path), 40)][-40:]
        except OSError:
            hostwatch = []
        return {
            "generated_at": time.time(),
            "gxmax": cluster.gxmax_state(),
            "lifecycle_events": [{"line": redact(e.get("line", "")), **{k: e.get(k) for k in ("ts", "state")
                                                                       if k in e}}
                                for e in (events.get("events") or [])][-200:],
            "lifecycle_history": list(reversed(events.get("history") or []))[-50:],
            "hostwatch_tail": hostwatch,
            "memory_events": self._journal(),
            "watchdog": self.incidents(),
        }
