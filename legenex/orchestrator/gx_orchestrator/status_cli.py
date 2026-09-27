"""`gx status` -- one human- and machine-readable snapshot of the cluster.

Deliberately dependency-free (see D-003): reads `/proc/meminfo` directly,
shells out to `uname` / `docker` via `subprocess`, and talks to the
orchestrator over plain `urllib`. No pip package is required anywhere in
this module.

The V4.1 status shape (ARCHITECTURE-V41 §3): cluster state (lifecycle), the
current serving profile, queue depth / active count, node health
one-liners, and the model id + uncensored flag from the registry. There is
one model and two aliases; there are no per-tier probes left to re-implement
-- everything except the local host facts comes from the orchestrator's
`/text/status` endpoint so probing logic lives in exactly one place.

Usage:
    python3 -m gx_orchestrator.status_cli [--json]
    python3 -m gx_orchestrator.status_cli --orchestrator-base http://127.0.0.1:18900
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.parse
from typing import Any, Callable
from pathlib import Path

from .config import CONFIG, Config
from .profiles import RegistryError, load_registry
from .upstream import UpstreamError, get_json

#: ARCHITECTURE.md L-4 / CLAUDE.md L-4: pinned on BOTH nodes. Kernel 7.0
#: broke RDMA memory registration (see coordination/DECISIONS.md D-001).
#: Duplicated as a plain constant so this script has no dependency beyond
#: the stdlib -- if the pin ever changes, this line and the docs must move
#: together.
EXPECTED_KERNEL = "6.17.0-1032-nvidia"


def _run(cmd: list[str], timeout: float = 5.0) -> "str | None":
    """Run `cmd`, returning stripped stdout, or None on any failure.

    Never raises: every caller degrades to "unknown" rather than crashing the
    whole report over one missing tool or a timeout.
    """
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception:  # noqa: BLE001 - missing binary, timeout, whatever
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


# --------------------------------------------------------------------------
# Local (node 1) facts
# --------------------------------------------------------------------------


def read_meminfo(path: str = "/proc/meminfo") -> "dict[str, int] | None":
    """Parse a `/proc/meminfo`-shaped file into a dict of kibibytes.

    `path` is overridable (tests pass a fixture file); production code always
    uses the default. None if the file cannot be read.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None
    out: dict[str, int] = {}
    for line in text.splitlines():
        parts = line.split(":", 1)
        if len(parts) != 2:
            continue
        key = parts[0].strip()
        value = parts[1].strip().split()[0]
        try:
            out[key] = int(value)
        except ValueError:
            continue
    return out


def _docker_ps() -> list[dict[str, str]]:
    """`docker ps` as {name, status} pairs. Empty when docker is unreachable
    (a broken docker socket must not take down the whole report)."""
    out = _run(["docker", "ps", "--format", "{{.Names}}|{{.Status}}"])
    if out is None:
        return []
    containers = []
    for line in out.splitlines():
        if "|" not in line:
            continue
        name, status = line.split("|", 1)
        containers.append({"name": name, "status": status})
    return containers


def local_node1_facts(cfg: Config) -> dict[str, Any]:
    """Everything this script can learn about node 1 without leaving it."""
    kernel = _run(["uname", "-r"])
    meminfo = read_meminfo()
    containers = _docker_ps()
    names_running = {c["name"] for c in containers}
    workload = next(
        (n for n in (cfg.head_container,) if n in names_running), None
    )

    def gib(key: str) -> "float | None":
        if meminfo is None or key not in meminfo:
            return None
        return round(meminfo[key] / (1024 * 1024), 1)

    return {
        "online": True,  # this script always runs ON node 1
        "kernel": kernel,
        "kernel_pinned": kernel == EXPECTED_KERNEL if kernel else None,
        "expected_kernel": EXPECTED_KERNEL,
        "ram_total_gib": gib("MemTotal"),
        "ram_available_gib": gib("MemAvailable"),
        "swap_total_gib": gib("SwapTotal"),
        "swap_free_gib": gib("SwapFree"),
        "workload": workload,
        "containers": containers,
    }


# --------------------------------------------------------------------------
# Registry facts (model id + uncensored flag)
# --------------------------------------------------------------------------


def registry_model_facts(cfg: Config) -> dict[str, Any]:
    """Model id + uncensored flag straight from the registry, with a clear
    error instead of a guess when the registry is missing or stale."""
    try:
        registry = load_registry(cfg.registry_path)
    except (RegistryError, OSError) as exc:
        return {"error": str(exc)}
    model = registry.production_model()
    runtime = registry.runtime(registry.alias("gx-max").runtime)
    return {
        "model_id": runtime.served_model_id,
        "uncensored": model.uncensored,
        "quant": model.quant,
        "max_context": model.max_context,
        "vision": model.vision,
        "tools": model.tools,
    }


# --------------------------------------------------------------------------
# Orchestrator snapshot (reused, not re-implemented)
# --------------------------------------------------------------------------


def _orchestrator_key() -> str:
    """The orchestrator's bearer key (D-044): the environment, else the
    protected secrets store."""
    key = os.environ.get("GX_ORCHESTRATOR_API_KEY", "").strip()
    if not key or key == "not-required":
        try:
            with open(os.environ.get("GX_SECRETS_ENV", "/srv/projects/gx-cluster/secrets/gateway.env")) as fh:
                for line in fh:
                    if line.startswith("GX_ORCHESTRATOR_API_KEY="):
                        key = line.split("=", 1)[1].strip()
        except OSError:
            return ""
    return "" if key == "not-required" else key


def fetch_orchestrator_snapshot(base_url: str, *, timeout: float = 5.0) -> dict[str, Any]:
    """GET `<base_url>/text/status` once. Returns the parsed body with
    `reachable` / `error` filled in. Never raises."""
    try:
        key = _orchestrator_key()
        headers = {"Authorization": f"Bearer {key}"} if key else None
        resp = get_json(f"{base_url.rstrip('/')}/text/status", headers=headers, timeout=timeout)
        body = resp.json()
    except (UpstreamError, Exception) as exc:  # noqa: BLE001
        return {"reachable": False, "error": repr(exc)}
    if not isinstance(body, dict):
        return {"reachable": False, "error": f"unrecognised orchestrator response: {body!r}"}
    body["reachable"] = True
    body["error"] = ""
    return body


# --------------------------------------------------------------------------
# Report assembly + rendering
# --------------------------------------------------------------------------


def build_report(cfg: Config, orchestrator_base: str) -> dict[str, Any]:
    node1 = local_node1_facts(cfg)
    snap = fetch_orchestrator_snapshot(orchestrator_base)
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "node1": node1,
        "model": registry_model_facts(cfg),
        "orchestrator": {
            "reachable": bool(snap.get("reachable")),
            "base": orchestrator_base,
            "error": snap.get("error", ""),
            # The V4.1 shape, passed through unchanged when reachable.
            "cluster": snap.get("model", {}),
            "queue": snap.get("queue", {}),
        },
    }


def _one_line_nodes(cluster: dict[str, Any]) -> list[str]:
    """Node health one-liners from the orchestrator's snapshot."""
    nodes = cluster.get("nodes") or {}
    head = nodes.get("head") or {}
    worker = nodes.get("worker") or {}
    mem = nodes.get("mem") or {}
    lines = []
    if head.get("healthy") and head.get("serves_model"):
        lines.append("head  : healthy (model id verified)")
    else:
        lines.append(f"head  : DOWN ({head.get('detail') or 'unreachable'})")
    fabric = worker.get("fabric") or {}
    if worker.get("ssh_reachable") and worker.get("container_running"):
        rails = ", ".join(f"{ip}:{'up' if ok else 'DOWN'}" for ip, ok in fabric.items()) or "-"
        lines.append(f"worker: container running (fabric {rails})")
    else:
        lines.append(f"worker: PROBLEM ({worker.get('detail') or 'ssh/container unreachable'})")
    lines.append(
        "mem   : node1={n1} GiB  node2={n2} GiB MemAvailable".format(
            n1=mem.get("node1_gib"), n2=mem.get("node2_gib"),
        )
    )
    return lines


def render_human(report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"gx-cluster status -- {report['generated_at']}")
    lines.append("")

    model = report["model"]
    if "error" in model:
        lines.append(f"MODEL  *** registry problem: {model['error']} ***")
    else:
        unc = "uncensored" if model.get("uncensored") else "STOCK (not uncensored!)"
        lines.append(f"MODEL  {model.get('model_id')}  ({unc}, {model.get('quant', '?')})")

    orch = report["orchestrator"]
    if not orch["reachable"]:
        lines.append(f"*** orchestrator at {orch['base']} is UNREACHABLE: {orch['error']} ***")
        lines.append("(cluster state, queue and node health are unknown, not assumed)")
    else:
        cluster = orch["cluster"]
        lifecycle = cluster.get("lifecycle") or {}
        state = lifecycle.get("state", "unknown")
        profile = cluster.get("profile") or "(none)"
        lines.append(f"CLUSTER  {state}  profile={profile}  model uptime={lifecycle.get('seconds_in_state')}s")
        lines.extend(f"  {line}" for line in _one_line_nodes(cluster))
        queue = orch["queue"]
        lines.append(
            f"QUEUE  active={queue.get('active')}/{queue.get('capacity')}  "
            f"queued={queue.get('queued')}  oldest_wait={queue.get('oldest_wait_seconds')}s"
        )
    lines.append("")

    n1 = report["node1"]
    pin_note = "" if n1["kernel_pinned"] else "  *** NOT the pinned kernel -- see BLOCKERS.md B-001 ***"
    lines.append("NODE 1 (gx10-01, head)  ONLINE")
    lines.append(f"  kernel      {n1['kernel']}{pin_note}")
    lines.append(f"  ram         {n1['ram_available_gib']} GiB available / {n1['ram_total_gib']} GiB total")
    lines.append(f"  swap        {n1['swap_free_gib']} GiB free / {n1['swap_total_gib']} GiB total")
    lines.append(f"  workload    {n1['workload'] or '(model not resident)'}")

    return "\n".join(lines) + "\n"


def render_json(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=False) + "\n"


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(prog="gx-status", description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON instead of text")
    parser.add_argument(
        "--orchestrator-base",
        default=f"http://127.0.0.1:{CONFIG.port}",
        help=f"orchestrator base URL (default: http://127.0.0.1:{CONFIG.port})",
    )
    args = parser.parse_args(argv)

    report = build_report(CONFIG, args.orchestrator_base)
    sys.stdout.write(render_json(report) if args.json else render_human(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
