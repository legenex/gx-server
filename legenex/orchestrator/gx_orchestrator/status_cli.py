"""`gx status` -- one human- and machine-readable snapshot of the two-node
gx-cluster.

Deliberately dependency-free (see D-003 in coordination/DECISIONS.md): reads
`/proc/meminfo` directly, shells out to `uname` / `docker` / `ping` via
`subprocess`, and talks to the orchestrator over plain `urllib`. No pip
package is required anywhere in this module.

Design note -- this does NOT re-implement upstream probing. Per-alias state
for gx-mini / gx-fast / gx-reason / gx-max comes straight from the running
orchestrator's own `/health/detailed` endpoint (backed by `TierHealth`, see
health.py), so there is exactly one place that probing logic lives. This
module adds only what the orchestrator does not already know:

  * local host facts (kernel, RAM, swap, which containers are running)
  * node 2's KERNEL-level liveness -- a single ICMP probe. BLOCKERS.md B-012
    documents why this specific, separate signal matters: node 2 can be
    "alive but userspace-starved" (kernel answers ICMP, llama-swap never
    answers TCP at all), so kernel-liveness and llama-swap-reachability are
    two different facts, not one.

Usage:
    python3 -m gx_orchestrator.status_cli [--json]
    python3 -m gx_orchestrator.status_cli --orchestrator-base http://127.0.0.1:18900
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Mapping

from .config import CONFIG, Config
from .tiers import Tier
from .upstream import get_json, probe

#: ARCHITECTURE.md L-4 / CLAUDE.md L-4: pinned on BOTH nodes. Kernel 7.0 broke
#: RDMA memory registration for gx-max (see coordination/DECISIONS.md D-001).
#: Duplicated here as a plain constant (not imported from the docs) so this
#: script has no dependency beyond the stdlib -- if the pin ever changes, this
#: line and the docs must be updated together.
EXPECTED_KERNEL = "6.17.0-1032-nvidia"

#: Node 1 container names this script recognises as "the current workload".
#: Anything else running is not this stack's concern (CLAUDE.md explicitly
#: lists unrelated pre-existing services on this shared host).
_NODE1_WORKLOAD_CONTAINERS = ("gx-mini", "gx-fast", "gx-max-rank0")

#: A single ICMP probe's deadline, in whole seconds (ping's -W wants an int
#: on the iputils build shipped here). Short and NOT retried -- see the
#: module docstring and BLOCKERS.md B-012.
_NODE2_PING_DEADLINE_S = 2


def _run(cmd: list[str], timeout: float = 5.0) -> str | None:
    """Run `cmd`, returning stripped stdout, or None on any failure.

    Never raises: every caller in this module must degrade to "unknown"
    rather than crash the whole report over one missing tool or a timeout.
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


def read_meminfo(path: str = "/proc/meminfo") -> dict[str, int] | None:
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
    """`docker ps` as a list of {name, status}. Empty list if docker is
    unreachable -- this must never raise (a broken docker socket must not
    take down the whole status report).
    """
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
    workload = [n for n in _NODE1_WORKLOAD_CONTAINERS if n in names_running] or None

    litellm_root = cfg.gateway_base.rstrip("/").removesuffix("/v1")
    litellm_up = probe(f"{litellm_root}/health/liveliness", timeout=cfg.node1_probe_timeout)
    swap_up = probe(f"{cfg.node1_swap_base.rstrip('/')}/health", timeout=cfg.node1_probe_timeout)

    def gib(kib_key: str) -> float | None:
        if meminfo is None or kib_key not in meminfo:
            return None
        return round(meminfo[kib_key] / (1024 * 1024), 1)

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
        "gateway": {"litellm": litellm_up, "llama_swap": swap_up},
    }


# --------------------------------------------------------------------------
# Node 2 -- a single, fast, non-retrying kernel-liveness probe
# --------------------------------------------------------------------------


def node2_kernel_reachable(cfg: Config) -> bool:
    """One ICMP echo, short deadline, no retry.

    This is deliberately NOT a userspace/HTTP probe: BLOCKERS.md B-012 records
    that node 2 can answer ICMP while llama-swap never answers TCP at all
    ("alive but userspace-starved"). Distinguishing those two is the entire
    point of this separate check -- llama-swap's own reachability is reported
    by the orchestrator's TierHealth (see gx-reason's entry in `aliases`).
    """
    host = urllib.parse.urlparse(cfg.node2_swap_base).hostname
    if not host:
        return False
    return (
        _run(["ping", "-c", "1", "-W", str(_NODE2_PING_DEADLINE_S), host], timeout=_NODE2_PING_DEADLINE_S + 1)
        is not None
    )


# --------------------------------------------------------------------------
# Orchestrator snapshot (reused, not re-implemented)
# --------------------------------------------------------------------------


@dataclass
class OrchestratorSnapshot:
    reachable: bool
    tiers: dict[str, dict[str, Any]] = field(default_factory=dict)
    gx_max: dict[str, Any] = field(default_factory=dict)
    error: str = ""


def fetch_orchestrator_snapshot(base_url: str, *, timeout: float = 5.0) -> OrchestratorSnapshot:
    """GET `<base_url>/health/detailed` once. Never raises."""
    try:
        resp = get_json(f"{base_url.rstrip('/')}/health/detailed", timeout=timeout)
        body = resp.json()
    except Exception as exc:  # noqa: BLE001
        return OrchestratorSnapshot(reachable=False, error=repr(exc))
    return OrchestratorSnapshot(
        reachable=True,
        tiers=body.get("tiers", {}),
        gx_max=body.get("gx_max", {}),
    )


# --------------------------------------------------------------------------
# Alias table (mini/fast/reason/max come from the orchestrator; auto/image/
# video are derived here because the orchestrator does not own them)
# --------------------------------------------------------------------------


def _normalize_tier_status(value: Any) -> dict[str, Any]:
    """Coerce whatever the orchestrator returned for one tier into the
    expected `{state, usable, reason}` shape.

    Defensive on purpose: this script and the orchestrator can drift out of
    sync (an old orchestrator process still running a previous version of
    `/health/detailed`, for instance -- exactly what happened the first time
    this was tested live, when the running service still had the OLD
    boolean-only response cached in its process memory). Never crash the
    whole report over one tier's shape, and never silently treat an
    unrecognised shape as healthy.
    """
    if isinstance(value, Mapping) and "state" in value:
        return dict(value)
    if value is None:
        return {"state": "unavailable", "usable": False, "reason": "not_reported"}
    return {
        "state": "unavailable",
        "usable": False,
        "reason": f"unrecognised_orchestrator_response:{value!r}",
    }


def build_alias_table(snap: OrchestratorSnapshot, node2_online: bool) -> dict[str, dict[str, Any]]:
    aliases: dict[str, dict[str, Any]] = {}

    if not snap.reachable:
        # Never guess: if the orchestrator itself cannot be reached, every
        # alias it would otherwise speak for is unknown, not "probably fine".
        reason = f"orchestrator_unreachable: {snap.error}" if snap.error else "orchestrator_unreachable"
        for alias in (Tier.MINI, Tier.FAST, Tier.REASON, Tier.MAX, Tier.AUTO):
            aliases[alias.value] = {"state": "unavailable", "usable": False, "reason": reason}
    else:
        for alias in (Tier.MINI, Tier.FAST, Tier.REASON, Tier.MAX):
            aliases[alias.value] = _normalize_tier_status(snap.tiers.get(alias.value))
        # gx-auto is pure routing logic inside the orchestrator process itself
        # -- no upstream of its own. If we can reach the orchestrator at all,
        # gx-auto is, by construction, able to make a routing decision.
        aliases[Tier.AUTO.value] = {
            "state": "ready",
            "usable": True,
            "reason": "orchestrator routing (picks among mini/fast/reason/max per request)",
        }

    # gx-image / gx-video: NOT owned by the orchestrator (media router lives
    # on node 2, outside this project's scope tonight -- see CURRENT_STATE.md
    # "built, unproven via API"). Report honestly rather than inferring
    # readiness: at most we can say whether their only possible host is even
    # reachable.
    if not node2_online:
        media = {"state": "unavailable", "usable": False, "reason": "node2_offline"}
    else:
        media = {
            "state": "unavailable",
            "usable": False,
            "reason": "media_router_not_probed_by_gx_status (out of orchestrator scope; see CURRENT_STATE.md)",
        }
    aliases[Tier.IMAGE.value] = dict(media)
    aliases[Tier.VIDEO.value] = dict(media)
    return aliases


# --------------------------------------------------------------------------
# Report assembly + rendering
# --------------------------------------------------------------------------


def build_report(cfg: Config, orchestrator_base: str) -> dict[str, Any]:
    node1 = local_node1_facts(cfg)
    node2_online = node2_kernel_reachable(cfg)
    snap = fetch_orchestrator_snapshot(orchestrator_base, timeout=cfg.node1_probe_timeout)
    aliases = build_alias_table(snap, node2_online)

    node2 = {
        "online": node2_online,
        "note": (
            "kernel answers ICMP" if node2_online else "no ICMP reply within "
            f"{_NODE2_PING_DEADLINE_S}s (single probe, not retried)"
        ),
        "llama_swap": aliases.get(Tier.REASON.value, {}),
        "workload": "gx-reason" if aliases.get(Tier.REASON.value, {}).get("state") == "ready" else None,
    }

    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "node1": node1,
        "node2": node2,
        "aliases": aliases,
        "orchestrator": {"reachable": snap.reachable, "base": orchestrator_base, "error": snap.error},
    }


def render_human(report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(f"gx-cluster status -- {report['generated_at']}")
    lines.append("")

    n1 = report["node1"]
    pin_note = "" if n1["kernel_pinned"] else "  *** NOT the pinned kernel -- see BLOCKERS.md B-001 ***"
    lines.append("NODE 1 (gx10-01, control)  ONLINE")
    lines.append(f"  kernel      {n1['kernel']}{pin_note}")
    lines.append(f"  ram         {n1['ram_available_gib']} GiB available / {n1['ram_total_gib']} GiB total")
    lines.append(f"  swap        {n1['swap_free_gib']} GiB free / {n1['swap_total_gib']} GiB total")
    lines.append(f"  workload    {', '.join(n1['workload']) if n1['workload'] else '(idle)'}")
    lines.append(
        f"  gateway     litellm={'healthy' if n1['gateway']['litellm'] else 'DOWN'}  "
        f"llama-swap={'healthy' if n1['gateway']['llama_swap'] else 'DOWN'}"
    )
    lines.append("")

    n2 = report["node2"]
    lswap = n2["llama_swap"]
    if not n2["online"]:
        headline = "OFFLINE"
    elif not lswap.get("usable"):
        # The exact BLOCKERS.md B-012 signature: kernel answers ICMP, but
        # llama-swap (userspace) never answers at all. These are two
        # different facts -- do not collapse them into one "ONLINE"/"OFFLINE"
        # that would either hide the outage or contradict the kernel probe.
        headline = "KERNEL ONLINE / USERSPACE UNREACHABLE"
    else:
        headline = "ONLINE"
    lines.append(f"NODE 2 (gx10-02, compute)  {headline}")
    lines.append(f"  {n2['note']}")
    lines.append(f"  llama-swap  {lswap.get('state', 'unknown')}  ({lswap.get('reason', '-')})")
    lines.append(f"  workload    {n2['workload'] or 'unknown'}")
    lines.append("")

    orch = report["orchestrator"]
    if not orch["reachable"]:
        lines.append(f"*** orchestrator at {orch['base']} is UNREACHABLE: {orch['error']} ***")
        lines.append("")

    lines.append("ALIASES")
    for alias, status in report["aliases"].items():
        lines.append(f"  {alias:<10} {status.get('state', 'unknown'):<12} {status.get('reason', '')}")

    return "\n".join(lines) + "\n"


def render_json(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=False) + "\n"


def main(argv: list[str] | None = None) -> int:
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
