"""The Agents and Tasks pages: AgentOS snapshot + scheduler attribution.

Two honest sources, no fabrication:

* AgentOS Control Center (read-only adapter): coarse agent state only —
  gateways running/stopped, kanban boards/counts/cards, projects. Upstream
  does NOT support pause/resume/cancel or per-agent metrics, so none are shown.
* The orchestrator scheduler: per-agent and per-project request attribution
  (active generations, queue depth) — metadata only, never prompt bodies.

An agent appears here only when one of the two sources actually names it.
"""

from __future__ import annotations

import time
from typing import Any

from .agentos_adapter import SUPPORTED_CONTROLS, AgentOSAdapter
from .config import UIConfig


def _hermes_agents(agentos: dict) -> list[dict]:
    """Coarse agent rows from /api/hermes: profile, model, running/stopped."""
    hermes = agentos.get("hermes")
    if not isinstance(hermes, dict) or agentos.get("connected") is not True:
        return []
    out: list[dict] = []
    profiles = hermes.get("profiles") or []
    running = {g.get("profile") for g in (hermes.get("gateways") or []) if g.get("running")}
    stopped = {g.get("profile") for g in (hermes.get("gateways") or []) if not g.get("running")}
    for p in profiles:
        if not isinstance(p, dict):
            continue
        name = p.get("profile")
        if not name:
            continue
        out.append({
            "name": name,
            "source": "agentos",
            "state": "running" if name in running else ("stopped" if name in stopped else "unknown"),
            "state_detail": "coarse: running or stopped only (upstream has no finer state)",
            "model": p.get("model"), "alias": p.get("alias"), "gateway": p.get("gateway"),
        })
    return out


def _scheduler_by_agent(snap: dict) -> dict[str, dict]:
    """Per-agent request attribution from the scheduler status snapshot."""
    out: dict[str, dict] = {}
    if not snap.get("available"):
        return out
    records = []
    for key in ("queue", "requests", "records"):
        raw = snap.get(key)
        if isinstance(raw, list):
            records = raw
            break
    for rec in records:
        if not isinstance(rec, dict):
            continue
        agent = str(rec.get("agent") or "unknown")
        slot = out.setdefault(agent, {"active": 0, "queued": 0, "done": 0, "error": 0})
        state = str(rec.get("state") or "")
        if state == "active":
            slot["active"] += 1
        elif state == "queued":
            slot["queued"] += 1
        elif state in ("error", "timeout"):
            slot["error"] += 1
        elif state == "done":
            slot["done"] += 1
    return out


def agents_view(cfg: UIConfig, adapter: AgentOSAdapter, scheduler_snap: dict) -> dict:
    """The /api/agents shape (see WORKER-C-REPORT.md for the frontend contract)."""
    agentos = adapter.snapshot()
    sched = _scheduler_by_agent(scheduler_snap)
    rows: list[dict] = []
    seen: set[str] = set()
    for row in _hermes_agents(agentos):
        name = row["name"]
        seen.add(name)
        row["requests"] = sched.get(name)
        rows.append(row)
    for name, counts in sched.items():
        if name in seen:
            continue
        rows.append({"name": name, "source": "scheduler",
                     "state": "working" if counts.get("active") else "idle",
                     "state_detail": "derived from scheduler request attribution",
                     "requests": counts})
    return {
        "generated_at": time.time(),
        "agentos": agentos,
        "agents": rows,
        "scheduler_available": bool(scheduler_snap.get("available")),
        # pause/resume/cancel are NOT supported by the upstream API; the
        # frontend must not render control buttons for agents.
        "supported_controls": list(SUPPORTED_CONTROLS),
        "notes": ["AgentOS exposes coarse running/stopped state only",
                  "per-agent metrics are not available upstream and are not shown",
                  "pause/resume/cancel are not supported by AgentOS and are not offered"],
    }


def tasks_view(cfg: UIConfig, adapter: AgentOSAdapter, scheduler_snap: dict) -> dict:
    """The /api/agents/tasks shape: kanban cards + active/queued scheduler records."""
    agentos = adapter.snapshot()
    kanban = agentos.get("kanban") if agentos.get("connected") else None
    cards: list[dict] = []
    if isinstance(kanban, dict):
        raw = kanban.get("cards")
        if isinstance(raw, list):
            cards = [c for c in raw if isinstance(c, dict)][:200]
    records: list[dict] = []
    if scheduler_snap.get("available"):
        for key in ("queue", "requests", "records"):
            raw = scheduler_snap.get(key)
            if isinstance(raw, list):
                records = [r for r in raw if isinstance(r, dict)][:200]
                break
    return {
        "generated_at": time.time(),
        "agentos_connected": agentos.get("connected") is True,
        "kanban_cards": cards,
        "kanban_counts": (kanban or {}).get("counts") if isinstance(kanban, dict) else None,
        "scheduler_records": records,
        "note": "no dependency graph upstream; cards are flat kanban entries",
    }
