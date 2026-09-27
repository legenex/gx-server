"""Hermetic V4.1 backend for the browser E2E suite (Worker F).

Runs the REAL post-rebuild control-UI server code (the retired media world of
e2e/fixture_server.py is gone with its modules) against a temp password store,
a schema-2 registry fixture, synthetic node facts and in-process stub
upstreams: orchestrator (lifecycle + scheduler), LiteLLM and AgentOS.

    GX_E2E_PASSWORD=... python3 e2e/fixture_server_v41.py 18091
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "tests"))

from support import StubUpstream, TempEnv  # noqa: E402

from gx_control_ui import auth  # noqa: E402
from gx_control_ui import server as srv  # noqa: E402

GIB = 2**30
NOW = time.time()


def node_facts(role: str, avail: float) -> dict:
    """Synthetic V4.1 node facts (no SSH, no docker, nothing real)."""
    return {
        "role": role, "reachable": True,
        "hostname": "gx10-01" if role == "node1" else "gx10-02",
        "kernel": "6.17.0-1032-nvidia", "kernel_ok": True, "collected_at": time.time(),
        "uptime_seconds": 120000, "ssh_ms": 12,
        "memory": {"MemTotal": 121.6 * GIB, "MemAvailable": avail * GIB, "MemFree": 80 * GIB,
                   "Cached": 20 * GIB, "SwapTotal": 48 * GIB, "SwapFree": 47 * GIB,
                   "SwapCached": 0, "Shmem": 0.5 * GIB, "Mlocked": 3 * GIB},
        "psi": {k: {"some": {"avg10": 0.0, "avg60": 0.0, "avg300": 0.0},
                    "full": {"avg10": 0.0, "avg60": 0.0, "avg300": 0.0}}
                for k in ("memory", "io", "cpu")},
        "load": {"load1": 0.4, "load5": 0.3, "load15": 0.3, "nproc": 20},
        "temperature": {"zones": [], "cpu_max_c": 47.5,
                        "gpu": {"name": "NVIDIA GB10", "celsius": 46, "util_pct": 5, "power_w": 14}},
        "rdma": [
            {"device": "rocep1s0f0", "netdev": "enp1s0f0np0", "state": "4: ACTIVE",
             "phys_state": "5: LinkUp", "rate": "200 Gb/sec (2X NDR)",
             "xmit_bytes": int(NOW * 1000) % (2**40), "rcv_bytes": 5 * GIB},
            {"device": "roceP2p1s0f0", "netdev": "enP2p1s0f0np0", "state": "4: ACTIVE",
             "phys_state": "5: LinkUp", "rate": "200 Gb/sec (2X NDR)",
             "xmit_bytes": 8 * GIB, "rcv_bytes": 6 * GIB},
        ],
        "tailscale": {"ok": True, "backend": "Running", "ips": ["100.105.214.61"],
                      "peers": [{"host": "gx10-02", "online": True, "direct": True}]},
        "docker": {"ok": True, "containers": []},
        "units": [{"unit": "gx-hostwatch.timer", "active": "active", "sub": "waiting", "enabled": "enabled"},
                  {"unit": "gx-git-watch.service" if role == "node1" else "gx-git-reconcile.timer",
                   "active": "active", "sub": "running" if role == "node1" else "waiting",
                   "enabled": "enabled"}],
        "git": {"ok": True, "head": "a" * 40, "branch": "main", "subject": "e2e fixture",
                "date": "2026-09-27T12:00:00+02:00", "dirty_files": 0,
                "push_url": "https://github.com/legenex/gx-server.git" if role == "node1"
                else "DISABLED-gx10-02-is-pull-only"},
        "hostwatch": {"ok": True, "status": "ok", "detail": "ok=5 warn=0 crit=0", "age_seconds": 20},
        "guard_lock": "free",
        "disk": {"path": "/", "total": 916 * 10**9, "used": 377 * 10**9, "free": 492 * 10**9,
                 "percent": 44.0},
    }


def scheduler_records() -> list[dict]:
    done = []
    for i in range(8):
        done.append({
            "id": f"req-done-{i}", "project": "gx-cluster", "agent": "architect", "task": f"task-{i}",
            "priority": 3, "profile": "balanced", "reasoning": "medium", "state": "done",
            "enqueue_ts": NOW - 600 + i * 60, "start_ts": NOW - 598 + i * 60,
            "done_ts": NOW - 540 + i * 60, "ttft_ms": 700 + i * 120, "tps": 38.0 + i,
            "prompt_tokens": 1200 + i * 10, "completion_tokens": 640, "cached_tokens": 100,
        })
    return [
        {"id": "req-queued-1", "project": "mia-app", "agent": "solver", "task": "draft",
         "priority": 0, "profile": "deep", "reasoning": "max", "state": "queued",
         "enqueue_ts": NOW - 30},
        {"id": "req-active-1", "project": "gx-cluster", "agent": "reviewer", "task": "review",
         "priority": 1, "profile": "balanced", "reasoning": "high", "state": "active",
         "enqueue_ts": NOW - 60, "start_ts": NOW - 55, "ttft_ms": 880, "tps": 41.0,
         "prompt_tokens": 900, "completion_tokens": 210},
        *done,
        {"id": "req-err-1", "project": "gx-cluster", "agent": "tester", "task": "run",
         "priority": 4, "profile": "fast", "reasoning": "low", "state": "error",
         "enqueue_ts": NOW - 3600, "start_ts": NOW - 3590, "done_ts": NOW - 3580,
         "error": "upstream connection closed"},
    ]


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18091
    password = os.environ["GX_E2E_PASSWORD"]
    records = scheduler_records()

    orch = StubUpstream({
        ("GET", "/health/detailed"): (200, {"ok": True, "nodes": {}}),
        ("GET", "/scheduler/status"): (200, {"queue": records}),
        ("GET", "/scheduler/history"): (200, {"records": records}),
        ("GET", "/lifecycle/gx-max/status"): (200, {"state": "down", "phase": "idle", "waiters": 1,
                                                    "profile": None, "detail": "released",
                                                    "last_error": ""}),
        ("GET", "/lifecycle/gx-max/events"): (200, {"seq": 2, "active_job": None, "history": [
            {"kind": "acquire", "started": NOW - 4000, "ended": NOW - 3450,
             "elapsed_seconds": 550, "startup_seconds": 480, "outcome": "ready",
             "phases": [{"phase": p} for p in ("preflight", "loading_rank1", "loading_rank0",
                                               "warming", "ready")]}],
            "events": [{"seq": 1, "ts": NOW - 3450, "source": "acquire",
                        "line": "=== gx-max READY after 480s ==="}]}),
        ("POST", "/lifecycle/gx-max/acquire"): (200, {"state": "ready", "last_startup_seconds": 480}),
        ("POST", "/lifecycle/gx-max/release"): (200, {"state": "down"}),
        ("POST", "/lifecycle/gx-max/drain"): (200, {"state": "draining"}),
        ("POST", "/scheduler/cancel"): (200, {"ok": True}),
        ("POST", "/scheduler/retry"): (200, {"ok": True}),
    })
    keys: dict = {"e" * 64: {"token": "e" * 64, "key_alias": "kilo-code", "key_name": "sk-...kilo",
                             "models": ["gx-auto"], "metadata": {}, "expires": None,
                             "created_at": "2026-09-27T00:00:00Z", "last_active": None}}

    def key_generate(handler, body):
        if any(k["key_alias"] == body["key_alias"] for k in keys.values()):
            return 400, {"error": {"message": f"Key with alias '{body['key_alias']}' already exists."}}
        token = f"{len(keys) + 100:064x}"
        keys[token] = {"token": token, "key_alias": body["key_alias"], "key_name": "sk-...new",
                       "models": body["models"], "metadata": body.get("metadata") or {},
                       "expires": None, "created_at": "2026-09-27T00:00:00Z", "last_active": None}
        return 200, {"key": "sk-e2e-" + "x" * 30, "token": token, "expires": None}

    litellm = StubUpstream({
        ("GET", "/health/liveliness"): (200, {"status": "healthy"}),
        ("GET", "/health/readiness"): (200, {"status": "ready"}),
        ("GET", "/key/list"): (200, {"keys": list(keys.values())}),
        ("POST", "/key/generate"): key_generate,
        ("POST", "/key/delete"): (200, {"deleted_keys": []}),
        ("POST", "/key/update"): (200, {"key": "updated"}),
        ("GET", "/v1/models"): (200, {"data": [{"id": "gx-max"}, {"id": "gx-auto"}]}),
        ("POST", "/v1/chat/completions"): (200, {
            "id": "e2e", "model": "gx-auto",
            "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
            "choices": [{"message": {"role": "assistant", "content": "pong"}}]}),
    })
    agentos = StubUpstream({
        ("GET", "/api/health"): (200, {"status": "ok"}),
        ("GET", "/api/hermes"): (200, {
            "profiles": [{"profile": "solver", "model": "gx-max", "alias": "gx-max",
                           "gateway": "http://127.0.0.1:4100"},
                          {"profile": "reviewer", "model": "gx-auto", "alias": "gx-auto",
                           "gateway": "http://127.0.0.1:4101"}],
            "gateways": [{"profile": "solver", "running": True},
                         {"profile": "reviewer", "running": False}]}),
        ("GET", "/api/kanban"): (200, {
            "counts": {"ready": 1, "todo": 1, "blocked": 0, "done": 1, "archived": 0},
            "cards": [
                {"id": "c1", "title": "Ship V4.1", "status": "ready", "assignee": "solver",
                 "priority": "high", "board": "main"},
                {"id": "c2", "title": "Wire scheduler caps", "status": "todo", "assignee": "reviewer",
                 "priority": "normal", "board": "main"},
                {"id": "c3", "title": "Registry schema 2", "status": "done", "assignee": "solver",
                 "priority": "normal", "board": "main"},
            ]}),
        ("GET", "/api/projects"): (200, []),
        ("GET", "/api/overview"): (200, {}),
        ("GET", "/api/buzz"): (200, {}),
    })

    env = TempEnv(offline=False, port=port, orchestrator_base=orch.url, litellm_base=litellm.url,
                  agentos_base=agentos.url)
    return run_server(env, orch, litellm, agentos, password, port)


def run_server(env, orch, litellm, agentos, password, port):
    cfg = env.cfg
    # Seed a project, a log line and a trash-free file world under the temp roots.
    projects = Path(cfg.file_roots[0])
    (projects / "alpha").mkdir(parents=True, exist_ok=True)
    (projects / "alpha" / "notes.md").write_text("# alpha\nhermetic e2e project\n", encoding="utf-8")
    (projects / "beta").mkdir(parents=True, exist_ok=True)
    (Path(str(cfg.srv_logs)) / "gx-orchestrator.log").write_text(
        "2026-09-27T12:00:00Z orchestrator e2e line one\n2026-09-27T12:00:01Z orchestrator e2e line two\n",
        encoding="utf-8")
    auth.PasswordStore(cfg.password_file).set_password("admin", password, n=2**12)

    app, servers = srv.build(cfg)
    cl = app.cluster
    # Synthetic node facts: nothing on this machine is measured.
    cl.node1._fn = lambda: node_facts("node1", 106.4)
    cl.node2._fn = lambda: node_facts("node2", 112.8)
    cl.remote_git._fn = lambda: {"ok": True, "head": "a" * 40, "checked_at": time.time()}
    # Fabric TCP probes would leave the machine; pin the probe cache instead.
    app._fabric_cache = {"at": time.time() + 10**9, "192.168.100.11": "open", "192.168.101.11": "open"}
    app.keys._master = lambda: "e2e-dummy-master-key"

    for s in servers:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    print(f"v41 e2e fixture server on http://127.0.0.1:{port}", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        orch.close()
        litellm.close()
        agentos.close()
        env.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
