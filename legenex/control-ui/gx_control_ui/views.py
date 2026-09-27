"""Page-level JSON views built from the cached cluster readings.

V4.1 world: one model (DeepSeek V4.1 Flash EXL3) behind the gx-max lifecycle;
the scheduler owns the request queue; gx10-02 carries only the rank-1 mirror.

Status vocabulary: ok (green), warn (amber), crit (red), unknown (grey).
A status is only ever "ok" because a probe said so -- never because a
container merely exists.
"""

from __future__ import annotations

import json
import os
import time
from typing import TYPE_CHECKING, Any

from . import __version__
from .logs import tail_file
from .models import read_registry
from .redact import redact
from .util import run

if TYPE_CHECKING:
    from .server import App

GIB = 2**30

_ORDER = {"ok": 0, "unknown": 1, "warn": 2, "crit": 3}


def worst(*levels: str) -> str:
    return max(levels or ("ok",), key=lambda s: _ORDER.get(s, 1))


def _gxmax(app: App) -> dict:
    lc = app.cluster.lifecycle.get() or {}
    st = (lc.get("status") or {})
    body = st.get("body")
    return body if st.get("ok") and isinstance(body, dict) else {"state": "unknown"}


def _rail_state(facts: dict, rail: dict) -> dict:
    for r in (facts or {}).get("rdma") or []:
        if r.get("device") == rail["rdma"] or r.get("netdev") == rail["netdev"]:
            active = "ACTIVE" in (r.get("state") or "")
            linkup = "LinkUp" in (r.get("phys_state") or "")
            return {"device": r.get("device"), "netdev": r.get("netdev"), "state": r.get("state"),
                    "phys_state": r.get("phys_state"), "rate": r.get("rate"),
                    "xmit_bytes": r.get("xmit_bytes"), "rcv_bytes": r.get("rcv_bytes"),
                    "ok": active and linkup}
    return {"ok": False, "state": "absent"}


def _rails(app: App) -> list[dict]:
    """Rail rows: registry IPs (schema 2) with the historical netdev probe keys."""
    from .netview import NetView
    reg = read_registry(app.cfg.registry_path)
    nodes = reg.get("nodes") or {}
    head = next((n for n, s in nodes.items() if isinstance(s, dict) and s.get("role") == "head"), "gx10-01")
    worker = next((n for n, s in nodes.items() if isinstance(s, dict) and s.get("role") == "worker"),
                  "gx10-02")
    hcas_head = (nodes.get(head) or {}).get("hcas") or ["rocep1s0f0", "roceP2p1s0f0"]
    hcas_worker = (nodes.get(worker) or {}).get("hcas") or ["rocep1s0f0", "roceP2p1s0f0"]
    rows = []
    for i, key in enumerate(("rail1", "rail2")):
        rows.append({
            "name": f"Rail {i + 1}",
            "node1_ip": ((nodes.get(head) or {}).get("fabric") or {}).get(key) or app.cfg.fabric_local[i],
            "node2_ip": ((nodes.get(worker) or {}).get("fabric") or {}).get(key) or app.cfg.fabric_peers[i],
            "netdev": hcas_head[i] if i < len(hcas_head) else "",
            "rdma": hcas_worker[i] if i < len(hcas_worker) else "",
        })
    return rows


def fabric_probe(app: App) -> dict:
    """TCP reachability of node 2's fabric addresses from node 1. 'refused'
    means the far kernel answered -- the link is alive."""
    from .util import tcp_state
    cache = getattr(app, "_fabric_cache", None)
    if cache and time.time() - cache["at"] < 10:
        return cache
    res: dict[str, Any] = {"at": time.time()}
    for rail in _rails(app):
        if app.cfg.offline:
            res[rail["node2_ip"]] = "unknown"
        else:
            res[rail["node2_ip"]] = tcp_state(rail["node2_ip"], 22, 1.5)
    setattr(app, "_fabric_cache", res)  # noqa: B010 - per-process probe cache
    return res


def _node_meta(app: App, key: str) -> dict:
    reg = read_registry(app.cfg.registry_path)
    nodes = reg.get("nodes") or {}
    head = next((n for n, s in nodes.items() if isinstance(s, dict) and s.get("role") == "head"), None)
    worker = next((n for n, s in nodes.items() if isinstance(s, dict) and s.get("role") == "worker"),
                  None)
    if key == "node1" and head:
        spec = nodes[head]
        return {"name": head, "role": "head: orchestrator, gateway, control UI, gx-max rank 0",
                "tailscale_ip": spec.get("tailscale_ip"), "user": spec.get("user")}
    if key == "node2" and worker:
        spec = nodes[worker]
        return {"name": worker, "role": "worker: gx-max rank 1 (mirror only; no management stack)",
                "tailscale_ip": spec.get("tailscale_ip"), "user": spec.get("user")}
    return {"name": "gx10-0" + ("1" if key == "node1" else "2"), "role": "", "tailscale_ip": None,
            "user": None}


def node_summary(app: App, key: str, facts: dict, gxmax_state: str) -> dict:
    meta = _node_meta(app, key)
    if not facts or not facts.get("reachable", facts.get("role") == "node1"):
        return {"key": key, **meta, "reachable": False, "level": "crit",
                "problems": [facts.get("error") or "not reachable over SSH"] if facts else ["no data"],
                "fabric_probe": (facts or {}).get("fabric_probe")}
    if facts.get("offline"):
        return {"key": key, **meta, "reachable": False, "level": "unknown", "problems": ["offline mode"]}
    mem = facts.get("memory") or {}
    avail = (mem.get("MemAvailable") or 0) / GIB
    swap_total = (mem.get("SwapTotal") or 0) / GIB
    swap_used = swap_total - (mem.get("SwapFree") or 0) / GIB
    psi = ((facts.get("psi") or {}).get("memory") or {})
    psi_full = (psi.get("full") or {}).get("avg10", 0.0)
    psi_some = (psi.get("some") or {}).get("avg10", 0.0)
    load = facts.get("load") or {}
    hw = facts.get("hostwatch") or {}
    problems, level = [], "ok"
    gx_busy = gxmax_state in ("acquiring", "ready", "releasing")
    if not facts.get("kernel_ok"):
        problems.append(f"kernel {facts.get('kernel')} is not the pinned 6.17.0-1032-nvidia")
        level = worst(level, "crit")
    if avail < 4 and gxmax_state != "acquiring":
        problems.append(f"MemAvailable {avail:.1f} GiB is critically low")
        level = worst(level, "crit")
    elif avail < 30 and not gx_busy:
        problems.append(f"MemAvailable {avail:.1f} GiB is below the 30 GiB reserve")
        level = worst(level, "warn")
    if psi_full >= 40:
        problems.append(f"memory PSI full {psi_full:.1f}% (thrashing)")
        level = worst(level, "crit")
    elif psi_full >= 5:
        problems.append(f"memory PSI full {psi_full:.1f}%")
        level = worst(level, "warn")
    if hw.get("ok"):
        detail = hw.get("detail") or ""
        if "crit=0" not in detail:
            problems.append(f"hostwatch: {detail}")
            level = worst(level, "crit")
        elif "warn=0" not in detail:
            problems.append(f"hostwatch: {detail}")
            level = worst(level, "warn")
        if (hw.get("age_seconds") or 0) > 300:
            problems.append("hostwatch has not written for over 5 minutes")
            level = worst(level, "warn")
    else:
        problems.append("hostwatch status unavailable")
        level = worst(level, "warn")
    ts = facts.get("tailscale") or {}
    if not ts.get("ok"):
        problems.append("Tailscale is not running")
        level = worst(level, "warn")
    failed_units = [
        u["unit"] for u in facts.get("units") or []
        if u.get("active") == "failed"
        or (u.get("unit", "").endswith(".timer") and u.get("active") != "active")
    ]
    if failed_units:
        problems.append(f"units not healthy: {', '.join(failed_units)}")
        level = worst(level, "warn")
    temp = facts.get("temperature") or {}
    return {
        "key": key, **meta, "reachable": True, "level": level, "problems": problems,
        "hostname": facts.get("hostname"), "kernel": facts.get("kernel"), "kernel_ok": facts.get("kernel_ok"),
        "uptime_seconds": facts.get("uptime_seconds"),
        "mem_total_gib": round((mem.get("MemTotal") or 0) / GIB, 2),
        "mem_available_gib": round(avail, 2),
        "swap_total_gib": round(swap_total, 2), "swap_used_gib": round(swap_used, 2),
        "psi_some_avg10": psi_some, "psi_full_avg10": psi_full,
        "load1": load.get("load1"), "nproc": load.get("nproc"),
        "gpu": temp.get("gpu"), "cpu_max_c": temp.get("cpu_max_c"),
        "hostwatch": hw, "tailscale_ok": ts.get("ok"), "guard_lock": facts.get("guard_lock"),
        "git_head": (facts.get("git") or {}).get("head"),
        "collected_at": facts.get("collected_at"), "ssh_ms": facts.get("ssh_ms"),
    }


def _service_levels(app: App, svc: dict, gxmax_state: str) -> list[dict]:
    """Cluster-managed rows + user apps, honestly labelled."""
    rows: list[dict] = []

    def row(name, probe_key, when_down="crit", managed=True, note=""):
        p = svc.get(probe_key) or {}
        level = "ok" if p.get("ok") else ("unknown" if (app.cfg.offline or p.get("status") is None)
                                          else when_down)
        return {"name": name, "level": level, "ok": bool(p.get("ok")), "status": p.get("status"),
                "ms": p.get("ms"), "error": p.get("error"), "note": note,
                "managed": managed}

    rows += [
        row("LiteLLM gateway", "litellm_live"),
        row("LiteLLM readiness (DB)", "litellm_ready", "warn"),
        row("gx-orchestrator", "orchestrator"),
        row("Scheduler (orchestrator)", "scheduler", "warn"),
        row("Mia runtime :8888 (only while READY)", "mia", "warn",
            note=f"gx-max is {gxmax_state}; :8888 is not serving while down" if gxmax_state != "ready"
            else ""),
        row("hostwatch", None, "warn", note="checked via node facts (hostwatch status file)"),
        row("ts-proxy (docker)", None, "warn",
            note="presence checked via node facts docker containers"),
    ]
    rows += [
        row("OpenWebUI (user app)", "openwebui", "warn", managed=False),
        row("AgentOS Control Center (user app)", "agentos", "warn", managed=False),
    ]
    return rows


def git_view(app: App, n1: dict, n2: dict) -> dict:
    remote = app.cluster.remote_git.get() or {}
    h1 = (n1.get("git") or {}).get("head")
    h2 = (n2.get("git") or {}).get("head")
    hr = remote.get("head")
    match = bool(h1 and h2 and hr and h1 == h2 == hr)
    units1 = {u["unit"]: u for u in n1.get("units") or []}
    units2 = {u["unit"]: u for u in n2.get("units") or []}
    level = "ok" if match else "warn"
    if not remote.get("ok"):
        level = "warn"
    dirty = (n1.get("git") or {}).get("dirty_files") or 0
    note = ""
    if not match and h1 and hr and h1 != hr:
        note = "gx10-01 has changes not yet on GitHub (autosync pushes after 45 quiet seconds)"
    elif not match and h2 and hr and h2 != hr:
        note = "gx10-02 has not reconciled yet (it reconciles right after each push and every minute)"
    return {
        "level": level, "match": match, "note": note,
        "node1_head": h1, "origin_main": hr, "node2_head": h2,
        "remote_ok": remote.get("ok"), "remote_error": remote.get("error"),
        "remote_checked_at": remote.get("checked_at"),
        "node1_branch": (n1.get("git") or {}).get("branch"),
        "node1_subject": (n1.get("git") or {}).get("subject"),
        "node1_date": (n1.get("git") or {}).get("date"),
        "node1_dirty_files": dirty,
        "node2_push_url": (n2.get("git") or {}).get("push_url"),
        "node2_push_disabled": str((n2.get("git") or {}).get("push_url", "")).startswith("DISABLED"),
        "units": {
            "node1_watcher": units1.get("gx-git-watch.service"),
            "node1_timer": units1.get("gx-git-autosync.timer"),
            "node1_daily_audit": units1.get("gx-git-daily-audit.timer"),
            "node2_reconcile": units2.get("gx-git-reconcile.timer"),
            "node2_daily_audit": units2.get("gx-git-daily-audit.timer"),
        },
    }


def _recent_problems(app: App, nodes: list[dict], services: list[dict], gx: dict) -> list[dict]:
    items = []
    for n in nodes:
        for p in n.get("problems", []):
            items.append({"level": n["level"] if n["level"] != "ok" else "warn", "source": n["name"],
                          "message": p})
    for s in services:
        if s["level"] not in ("ok", "unknown"):
            items.append({"level": s["level"], "source": s["name"],
                          "message": s.get("error") or f"HTTP {s.get('status')}"})
    if gx.get("last_error"):
        items.append({"level": "warn", "source": "gx-max", "message": redact(gx["last_error"])[:400]})
    for job in app.actions.jobs()[:10]:
        if job["state"] == "failed" and time.time() - job["started"] < 6 * 3600:
            items.append({"level": "warn", "source": "control UI", "message": f"operation failed: {job['label']}"})
    try:
        pf = tail_file("/srv/logs/gx-git-sync/push-failures.log", 3)
        mtime = os.path.getmtime("/srv/logs/gx-git-sync/push-failures.log")
        if pf and time.time() - mtime < 3600:
            items.append({"level": "warn", "source": "git push", "message": redact(pf[-1])[:300]})
    except OSError:
        pass
    for s in app.cluster.secret_hygiene():
        if s["state"] in ("placeholder", "weak") and s["name"] != "GX_ORCHESTRATOR_API_KEY":
            items.append({"level": "warn", "source": "secrets",
                          "message": f"{s['name']} is {s['state']} (value not shown)"})
    return items


def _queue(app: App) -> dict:
    snap = app.cluster.scheduler_snapshot(max_age=10)
    if not snap.get("available"):
        return {"available": False, "reason": snap.get("reason"), "queued": None, "active": None}
    from .sse import _count_state
    return {"available": True, "queued": _count_state(snap, "queued"),
            "active": _count_state(snap, "active")}


def overview(app: App) -> dict:
    n1 = app.cluster.node1.get() or {}
    n2 = app.cluster.node2.get() or {}
    svc = app.cluster.services.get() or {}
    gx = _gxmax(app)
    state = gx.get("state", "unknown")
    nodes = [node_summary(app, "node1", {**n1, "reachable": True}, state),
             node_summary(app, "node2", n2, state)]
    services = _service_levels(app, svc, state)
    probe = fabric_probe(app)
    rails = []
    for rail in _rails(app):
        s1, s2 = _rail_state(n1, rail), _rail_state(n2, rail)
        tcp = probe.get(rail["node2_ip"], "unknown")
        ok = s1.get("ok") and (s2.get("ok") or not n2.get("reachable")) and tcp in ("open", "refused")
        rails.append({**rail, "node1": s1, "node2": s2, "tcp_probe": tcp, "ok": bool(ok),
                      "level": "ok" if ok else "crit"})
    ts1, ts2 = (n1.get("tailscale") or {}), (n2.get("tailscale") or {})
    ts_level = "ok" if ts1.get("ok") and (ts2.get("ok") or n2.get("reachable")) else "warn"
    git = git_view(app, n1, n2)
    reg = read_registry(app.cfg.registry_path)
    queue = _queue(app)
    overall = worst(*(n["level"] for n in nodes), *(s["level"] for s in services),
                    *(r["level"] for r in rails), ts_level,
                    "warn" if gx.get("last_error") else "ok")
    return {
        "generated_at": time.time(),
        "overall": overall,
        "nodes": nodes,
        "services": services,
        "rails": rails,
        "tailscale": {"level": ts_level, "node1": ts1, "node2": ts2},
        "rdma_ok": all(r["ok"] for r in rails),
        "gxmax": {"state": state, "phase": gx.get("phase"), "detail": gx.get("detail"),
                  "profile": gx.get("profile") or (app.resources.profile().get("serving")),
                  "last_error": gx.get("last_error"), "waiters": gx.get("waiters")},
        "queue": {**queue, "ui_running": app.actions.running()},
        "registry_ok": reg.get("schema") == 2,
        "locks": {"node1": n1.get("guard_lock"), "node2": n2.get("guard_lock")},
        "ledger": {k: v for k, v in (app.cluster.guard.get() or {}).items() if k in ("node1", "node2")},
        "git": git,
        "problems": _recent_problems(app, nodes, services, gx),
        "cache_age": {"node1": app.cluster.node1.age, "node2": app.cluster.node2.age,
                      "services": app.cluster.services.age},
    }


def nodes(app: App) -> dict:
    n1 = app.cluster.node1.get() or {}
    n2 = app.cluster.node2.get() or {}
    gx = _gxmax(app)
    svc = app.cluster.services.get() or {}

    def detail(key: str, facts: dict) -> dict:
        s = node_summary(app, key, {**facts, "reachable": facts.get("reachable", key == "node1")},
                         gx.get("state", ""))
        s["psi"] = facts.get("psi")
        s["load"] = facts.get("load")
        s["temperature"] = facts.get("temperature")
        s["containers"] = (facts.get("docker") or {}).get("containers", [])
        s["docker_stats"] = facts.get("docker_stats")
        s["units"] = facts.get("units")
        s["memory"] = facts.get("memory")
        return s

    return {
        "generated_at": time.time(),
        "node1": detail("node1", n1),
        "node2": detail("node2", n2),
        "services": _service_levels(app, svc, gx.get("state", "")),
        "orchestrator": (svc.get("orchestrator") or {}).get("body"),
        "scheduler": (svc.get("scheduler") or {}).get("body"),
        "gxmax": gx,
        "ledger": app.cluster.guard.get(),
    }


def cluster(app: App) -> dict:
    ov = overview(app)
    n1 = app.cluster.node1.get() or {}
    n2 = app.cluster.node2.get() or {}
    return {
        "generated_at": ov["generated_at"],
        "explanation": ("Two independent 128 GB unified-memory systems (about 121 GiB usable each). "
                        "They do NOT share memory: there is no 256 GB pool. One model — DeepSeek "
                        "V4.1 Flash EXL3 — runs as two tensor-parallel ranks (gx10-01 rank 0, "
                        "gx10-02 rank 1) exchanging activations over the ConnectX-7 RoCE rails."),
        "nodes": ov["nodes"],
        "rails": ov["rails"],
        "tailscale": ov["tailscale"],
        "management": {
            "ssh_node2": {"ok": bool(n2.get("reachable")), "ms": n2.get("ssh_ms"),
                          "target": app.cfg.node2_ssh, "path": "Tailscale"},
            "note": "Tailscale carries management only (SSH, this UI, client access). "
                    "Model and NCCL traffic use only the RoCE rails.",
        },
        "gxmax": ov["gxmax"],
        "queue": ov["queue"],
    }


def jobs(app: App) -> dict:
    gx = _gxmax(app)
    lc = app.cluster.lifecycle.get() or {}
    ev = (lc.get("events") or {})
    body = ev.get("body") if ev.get("ok") else {}
    ev = body if isinstance(body, dict) else {}
    return {
        "generated_at": time.time(),
        "gxmax": gx,
        "gxmax_active_job": ev.get("active_job"),
        "gxmax_history": list(reversed(ev.get("history") or [])),
        "gxmax_events": [{**e, "line": redact(e.get("line", ""))} for e in (ev.get("events") or [])][-300:],
        "queue": _queue(app),
        "ui_jobs": app.actions.jobs(),
    }


def system(app: App) -> dict:
    cfg = app.cfg
    n1 = app.cluster.node1.get() or {}
    n2 = app.cluster.node2.get() or {}
    version = "unknown"
    try:
        version = (cfg.repo_root / "VERSION").read_text().strip()
    except OSError:
        pass
    hide_units = ("gx-playground", "gx-call", "gx-live", "gx-music", "gx-voice", "gx-comfyui")

    def _keep_unit(u: dict) -> bool:
        name = u.get("unit") or ""
        return not any(h in name for h in hide_units)

    units = {
        "gx10-01": [u for u in (n1.get("units") or []) if _keep_unit(u)],
        "gx10-02": [u for u in (n2.get("units") or []) if _keep_unit(u)],
    }
    timers = run(["systemctl", "--user", "list-timers", "--all", "--no-pager", "--output=json"],
                 timeout=5)
    try:
        timer_rows = [t for t in json.loads(timers.out) if "gx" in (t.get("unit") or "")] if timers.ok else []
    except ValueError:
        timer_rows = []
    return {
        "generated_at": time.time(),
        "project_version": version,
        "ui_version": __version__,
        "repo": {"path": str(cfg.repo_root), "remote": cfg.github_url,
                 "branch": (n1.get("git") or {}).get("branch")},
        "git": git_view(app, n1, n2),
        "kernel_pin": "6.17.0-1032-nvidia",
        "kernels": {"gx10-01": n1.get("kernel"), "gx10-02": n2.get("kernel")},
        "endpoints": [
            {"name": "LiteLLM gateway (clients)", "url": cfg.public_gateway_url, "scope": "Tailscale + loopback"},
            {"name": "LiteLLM (internal)", "url": cfg.litellm_base, "scope": "loopback"},
            {"name": "gx-orchestrator + scheduler", "url": cfg.orchestrator_base, "scope": "loopback + docker bridge"},
            {"name": "Mia runtime (raw, loopback ONLY)", "url": cfg.mia_base, "scope": "loopback; never public"},
            {"name": "Control UI", "url": f"http://{_node_meta(app, 'node1')['tailscale_ip']}:{cfg.port}/",
             "scope": "Tailscale + loopback"},
            {"name": "OpenWebUI (user app)", "url": "http://127.0.0.1:3000", "scope": "loopback + Tailscale"},
            {"name": "AgentOS Control Center (user app)", "url": cfg.agentos_base, "scope": "loopback"},
        ],
        "units": units,
        "timers": timer_rows,
        "runtime_dirs": [
            {"path": str(cfg.state_dir), "purpose": "control UI state"},
            {"path": str(cfg.secret_dir), "purpose": "control UI password store (0700/0600)"},
            {"path": str(cfg.log_dir), "purpose": "control UI service + audit logs"},
            {"path": str(cfg.guard_dir), "purpose": "guard protocol (profile, pins, holds)"},
            {"path": str(cfg.gx_state_root / "scheduler"), "purpose": "scheduler queue + history"},
            {"path": str(cfg.gx_state_root / "watchdog"), "purpose": "watchdog incidents JSONL"},
            {"path": "/srv/logs", "purpose": "all service logs"},
            {"path": "/srv/models", "purpose": "model packs (per node, not shared)"},
            {"path": str(cfg.trash_root), "purpose": "file manager trash (restore / purge)"},
        ],
        "secrets": app.cluster.secret_hygiene(),
        "sessions": app.sessions.count(),
        "actions": [s.public() for s in app.actions.registry.values()],
        "jobs": app.actions.jobs()[:20],
    }
