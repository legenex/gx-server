"""gx — the one command a human needs on the GX Cluster (V4.1 rebuild).

Implements ARCHITECTURE-V41.md §8: a single-file, dependency-free CLI
(stdlib only, matching the repo's D-003 discipline) that wraps the
orchestrator API on the head node. No Docker or systemd knowledge is
required to operate the cluster through it.

Usage:
    gx status                      cluster + queue on one screen
    gx doctor                      PASS/FAIL/WARN health battery
    gx start [--profile P] [--reasoning R]
    gx stop | gx restart [--profile P] | gx drain
    gx max                         interactive chat quick-poke
    gx auto                        one-shot gx-auto test call
    gx profile [list|show P]
    gx queue [--watch]             live scheduler status
    gx requests [--last N]         scheduler history
    gx logs [--follow]
    gx benchmark [--suite quick|full|matrix]
    gx models | gx nodes | gx storage
    gx backup | gx update

Entry points:
    ~/.local/bin/gx                the normal way (the symlink itself is
                                   created by the install step -- NOT by
                                   this file; this repo deliberately does
                                   not touch ~/.local/bin)
    python3 -m gx_cli              from legenex/cli/ (or with that dir on
                                   PYTHONPATH) -- thin wrapper module
    python3 -m legenex.cli         from the repo root

All remote calls carry short timeouts and print a clear one-line error
when the cluster is down -- the CLI degrades, it never hangs. Exit codes:
0 ok, 1 operational error, 2 usage error.

This repo is PUBLIC: no secrets here. The orchestrator bearer key comes
from the GX_ORCHESTRATOR_API_KEY environment variable at call time.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Endpoints + pinned facts (ARCHITECTURE-V41.md §1-§8, MIA-RUNTIME.md)
# ---------------------------------------------------------------------------

#: Orchestrator (head, loopback + docker bridge). Auth: Bearer key from the
#: environment on every route except /health (which is unauthenticated).
ORCHESTRATOR_BASE = os.environ.get("GX_ORCH_BASE", "http://127.0.0.1:18900").rstrip("/")

#: The Mia/OpenAI model endpoint. Loopback-only on the head node.
MODEL_BASE = os.environ.get("GX_MODEL_BASE", "http://127.0.0.1:8888").rstrip("/")

#: The LiteLLM gateway (gx-max / gx-auto aliases, key required).
GATEWAY_BASE = os.environ.get("GX_GATEWAY_BASE", "http://127.0.0.1:4000").rstrip("/")

#: Served model id pinned by the Mia EXL3 kit (MIA-RUNTIME.md).
SERVED_MODEL_ID = "DeepSeek-v4.1-Flash-EXL3"

#: Runtime state root (outside the checkout, D-026). Also used by the
#: benchmark suite and the uncensoring-verification reports.
STATE_ROOT = Path(os.environ.get("GX_STATE_ROOT", "/srv/projects/gx-cluster/state"))

#: Logs the `logs` subcommand tails.
LOG_ROOT = Path(os.environ.get("GX_LOG_ROOT", "/srv/logs"))

#: Bench suite lives in the same checkout as this package.
_REPO_ROOT = Path(__file__).resolve().parents[2]
BENCH_SCRIPT = _REPO_ROOT / "ops" / "bench" / "run_bench.py"
REGISTRY_PATH = _REPO_ROOT / "legenex" / "models" / "registry.json"

#: Reasoning ladder (ARCHITECTURE-V41.md §2 "reasoning.mapping").
#: none/minimal disable thinking entirely; the rest map to effort 1-100.
REASONING_LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
_DEFAULT_REASONING = "medium"
_REASONING_EFFORT = {"low": 50, "medium": 62, "high": 75, "xhigh": 90, "max": 100}

#: Public aliases (everything else is internal).
PUBLIC_ALIASES = ("gx-max", "gx-auto")

#: Short default timeout for read/status calls. Lifecycle calls that boot
#: the model are the deliberate exception (a Mia boot is ~25 min).
_TIMEOUT_S = float(os.environ.get("GX_TIMEOUT_S", "6"))
_LIFECYCLE_TIMEOUT_S = float(os.environ.get("GX_LIFECYCLE_TIMEOUT_S", "1900"))
_DR_TIMEOUT_S = 60.0

#: Kernel pin (CLAUDE.md L-4) surfaced by `doctor` for context.
EXPECTED_KERNEL = "6.17.0-1032-nvidia"


class GxError(Exception):
    """An operational error with a human-readable, action-ready message."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def orchestrator_key() -> str:
    """Bearer key for the orchestrator API, environment only. Empty string
    when unset -- /health still works then, everything else will 401."""
    return os.environ.get("GX_ORCHESTRATOR_API_KEY", "").strip()


def gateway_key() -> str:
    """Key for the LiteLLM gateway (gx-max / gx-auto), environment only."""
    return (
        os.environ.get("GX_GATEWAY_KEY")
        or os.environ.get("LITELLM_MASTER_KEY")
        or ""
    ).strip()


def _friendly_error(exc: Exception, what: str) -> GxError:
    """Turn any urllib failure into one clear line, never a traceback."""
    if isinstance(exc, urllib.error.HTTPError):
        try:
            body = exc.read(400).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            body = ""
        hint = f" :: {body.strip()[:200]}" if body.strip() else ""
        return GxError(f"{what}: HTTP {exc.code} from the server{hint}")
    if isinstance(exc, (urllib.error.URLError, ConnectionError, TimeoutError, OSError)):
        return GxError(f"{what}: cannot reach the server ({exc})")
    return GxError(f"{what}: {exc}")


def http_json(
    method: str,
    url: str,
    *,
    body: dict[str, Any] | None = None,
    key: str = "",
    timeout: float = _TIMEOUT_S,
) -> Any:
    """One JSON HTTP call. Raises GxError with a clear message, never a
    raw urllib exception. `key` adds the Bearer header when non-empty."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read()
    except Exception as exc:  # noqa: BLE001 - one clear message for any failure
        raise _friendly_error(exc, f"{method} {url}") from None
    if not payload:
        return None
    try:
        return json.loads(payload.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise GxError(f"{method} {url}: server returned invalid JSON ({exc})") from None


def orch_get(path: str, *, auth: bool = True, timeout: float = _TIMEOUT_S) -> Any:
    """GET on the orchestrator. `auth=False` only for the unauthenticated
    /health route."""
    return http_json(
        "GET",
        f"{ORCHESTRATOR_BASE}{path}",
        key=orchestrator_key() if auth else "",
        timeout=timeout,
    )


def orch_post(path: str, body: dict[str, Any] | None = None, *, timeout: float = _TIMEOUT_S) -> Any:
    return http_json(
        "POST", f"{ORCHESTRATOR_BASE}{path}", body=body or {}, key=orchestrator_key(), timeout=timeout
    )


def run_cmd(cmd: list[str], *, timeout: float = 5.0) -> str | None:
    """Run a command, return stripped stdout or None on ANY failure.
    Never raises -- callers degrade to 'unknown'."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception:  # noqa: BLE001
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def read_meminfo(path: str = "/proc/meminfo") -> dict[str, int] | None:
    """Parse /proc/meminfo into {key: kibibytes}. None if unreadable."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    out: dict[str, int] = {}
    for line in text.splitlines():
        parts = line.split(":", 1)
        if len(parts) != 2:
            continue
        try:
            out[parts[0].strip()] = int(parts[1].strip().split()[0])
        except (ValueError, IndexError):
            continue
    return out


def mem_available_gib() -> float | None:
    info = read_meminfo()
    if info is None or "MemAvailable" not in info:
        return None
    return round(info["MemAvailable"] / (1024 * 1024), 1)


def reasoning_kwargs(level: str = _DEFAULT_REASONING) -> dict[str, Any]:
    """chat_template_kwargs for a reasoning ladder level (§2)."""
    if level not in REASONING_LEVELS:
        raise GxError(f"unknown reasoning level {level!r} (choose from {', '.join(REASONING_LEVELS)})")
    if level in ("none", "minimal"):
        return {"enable_thinking": False}
    return {"enable_thinking": True, "reasoning_effort": _REASONING_EFFORT[level]}


def chat(
    *,
    messages: list[dict[str, str]],
    model: str = SERVED_MODEL_ID,
    base: str = MODEL_BASE,
    key: str = "",
    max_tokens: int = 512,
    reasoning: str = _DEFAULT_REASONING,
    timeout: float = 300.0,
) -> dict[str, Any]:
    """One OpenAI chat completion. Returns {content, model, usage, raw}."""
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 1.0,
        "top_p": 0.95,
        "chat_template_kwargs": reasoning_kwargs(reasoning),
    }
    payload = http_json("POST", f"{base}/v1/chat/completions", body=body, key=key, timeout=timeout)
    if not isinstance(payload, dict) or "choices" not in payload:
        raise GxError(f"chat: unexpected response shape from {base}")
    content = ""
    choice = payload["choices"][0] if payload["choices"] else {}
    msg = choice.get("message") or {}
    content = msg.get("content") or ""
    if not content:
        # thinking-only answer: surface the reasoning text rather than nothing
        content = msg.get("reasoning_content") or ""
    return {
        "content": content,
        "model": payload.get("model", model),
        "usage": payload.get("usage") or {},
        "raw": payload,
    }


def load_registry() -> dict[str, Any]:
    """Load legenex/models/registry.json. Raises GxError when invalid."""
    try:
        return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise GxError(f"registry not found at {REGISTRY_PATH}") from None
    except json.JSONDecodeError as exc:
        raise GxError(f"registry is not valid JSON ({exc})") from None


def registry_model_cards(reg: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalized model cards from either registry schema.

    Schema 2 (the V4.1 target) has models.{id} with source/revision/path/
    uncensored bool. Schema 1 (the pre-rebuild registry still on disk) has
    aliases.{name} with repository/revision/path/uncensored string. Both
    render the same card; schema 1 entries keep their alias name as id.
    """
    cards: list[dict[str, Any]] = []
    models = reg.get("models")
    if isinstance(models, dict):
        for mid, m in models.items():
            if not isinstance(m, dict):
                continue
            cards.append(
                {
                    "id": mid,
                    "source": m.get("source") or m.get("repository") or "?",
                    "revision": m.get("revision") or "?",
                    "path": m.get("path") or "",
                    "uncensored": bool(m.get("uncensored", False)),
                    "uncensored_raw": m.get("uncensored"),
                    "quant": m.get("quant") or m.get("quantization") or "",
                    "context": m.get("max_context") or m.get("context") or "",
                    "engram_dir": m.get("engram_dir") or "",
                }
            )
        return cards
    aliases = reg.get("aliases")
    if isinstance(aliases, dict):
        for name, a in aliases.items():
            if not isinstance(a, dict):
                continue
            raw = a.get("uncensored")
            unc = isinstance(raw, bool) and raw or (isinstance(raw, str) and raw.strip().casefold().startswith("yes"))
            cards.append(
                {
                    "id": name,
                    "source": a.get("repository") or "?",
                    "revision": a.get("revision") or "?",
                    "path": a.get("path") or "",
                    "uncensored": bool(unc),
                    "uncensored_raw": raw,
                    "quant": a.get("quantization") or "",
                    "context": a.get("context") or "",
                    "engram_dir": "",
                }
            )
    return cards


def registry_profiles(reg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    profiles = reg.get("profiles")
    return profiles if isinstance(profiles, dict) else {}


def registry_nodes(reg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    nodes = reg.get("nodes")
    return nodes if isinstance(nodes, dict) else {}


def fmt_bytes(n: int | None) -> str:
    if n is None:
        return "?"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024  # type: ignore[operator]
    return f"{n} TiB"


def disk_free_bytes(path: str) -> int | None:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def dir_size_bytes(path: Path, *, max_depth: int = 2) -> int:
    """Best-effort recursive size with a depth cap (never raises, never
    follows symlinks). Used by `storage` only -- it is a summary, not an
    audit."""
    total = 0
    try:
        stack = [(path, 0)]
        while stack:
            current, depth = stack.pop()
            try:
                with os.scandir(current) as it:
                    for entry in it:
                        try:
                            if entry.is_symlink():
                                continue
                            if entry.is_dir(follow_symlinks=False):
                                if depth < max_depth:
                                    stack.append((entry.path, depth + 1))
                            else:
                                total += entry.stat(follow_symlinks=False).st_size
                        except OSError:
                            continue
            except OSError:
                continue
    except OSError:
        return 0
    return total


# ---------------------------------------------------------------------------
# status -- cluster + queue on one screen
# ---------------------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> int:
    lines: list[str] = [f"gx-cluster status -- {time.strftime('%Y-%m-%d %H:%M:%S')}", ""]

    # Orchestrator /health is unauthenticated by contract.
    health = None
    try:
        health = orch_get("/health", auth=False)
    except GxError as exc:
        lines.append(f"orchestrator   DOWN ({exc})")
    if health is not None:
        lines.append(f"orchestrator   up at {ORCHESTRATOR_BASE}  {json.dumps(health)[:120]}")

    # Lifecycle state machine
    try:
        life = orch_get("/lifecycle/gx-max/status")
        state = life.get("state", "?") if isinstance(life, dict) else "?"
        profile = life.get("profile", "?") if isinstance(life, dict) else "?"
        since = life.get("since_ts") or life.get("since") or "" if isinstance(life, dict) else ""
        lines.append(f"gx-max         state={state}  profile={profile}  {since}")
    except GxError as exc:
        lines.append(f"gx-max         unknown ({exc})")

    # Scheduler queue snapshot
    try:
        sched = orch_get("/scheduler/status")
        if isinstance(sched, dict):
            q = sched.get("queued", sched.get("queue_depth", "?"))
            a = sched.get("active", "?")
            lines.append(f"queue          queued={q} active={a}")
        else:
            lines.append("queue          (unexpected scheduler status shape)")
    except GxError as exc:
        lines.append(f"queue          unknown ({exc})")

    # Model endpoint (head loopback)
    try:
        started = time.monotonic()
        http_json("GET", f"{MODEL_BASE}/health", timeout=_TIMEOUT_S)
        ms = int((time.monotonic() - started) * 1000)
        try:
            models = http_json("GET", f"{MODEL_BASE}/v1/models", timeout=_TIMEOUT_S)
            ids = [m.get("id") for m in models.get("data", [])] if isinstance(models, dict) else []
            served = ids[0] if ids else "?"
        except GxError:
            served = "?"
        lines.append(f"model API      up on {MODEL_BASE} ({ms} ms)  served={served}")
    except GxError as exc:
        lines.append(f"model API      down ({exc})")

    # Gateway
    try:
        http_json("GET", f"{GATEWAY_BASE}/health/liveliness", timeout=_TIMEOUT_S)
        lines.append(f"gateway        up on {GATEWAY_BASE}  aliases={', '.join(PUBLIC_ALIASES)}")
    except GxError:
        try:
            http_json("GET", f"{GATEWAY_BASE}/health", timeout=_TIMEOUT_S)
            lines.append(f"gateway        up on {GATEWAY_BASE}  aliases={', '.join(PUBLIC_ALIASES)}")
        except GxError as exc:
            lines.append(f"gateway        down ({exc})")

    # One local fact that matters most for long prompts
    mem = mem_available_gib()
    lines.append(f"MemAvailable   {mem if mem is not None else '?'} GiB" if mem is not None else "MemAvailable   ?")
    disk = disk_free_bytes("/srv")
    lines.append(f"/srv free      {fmt_bytes(disk) if disk is not None else '?'}")

    print("\n".join(lines))
    return 0


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------

#: doctor check result triples.
PASS, WARN, FAIL = "PASS", "WARN", "FAIL"

#: MemAvailable thresholds (GiB). The loaded head sits around 5.9 GiB after
#: a 34k replay (MIA-RUNTIME.md); below 6 is worth a warning, below 2 the
#: next long prompt is in danger.
_MEM_WARN_GIB = 6.0
_MEM_FAIL_GIB = 2.0

#: /srv free thresholds. EXL3 packs are ~200 GiB scale.
_DISK_WARN_BYTES = 100 * 1024**3
_DISK_FAIL_BYTES = 20 * 1024**3

#: The proven NCCL GID index for this fabric (ARCHITECTURE-V41.md §1).
REQUIRED_GID_INDEX = 3


def _gid_table_ok(hca_dir: Path, gid_index: int) -> tuple[bool, str]:
    """Check /sys/class/infiniband/<hca>/ports/<n>/gids/<index> is a real,
    non-zero GID (the all-zero entry at the wrong index is exactly the
    ibv_modify_qp errno 61 failure mode Mia documented)."""
    ports_dir = hca_dir / "ports"
    try:
        ports = sorted(p for p in ports_dir.iterdir() if p.name.isdigit())
    except OSError:
        return False, "no ports directory"
    if not ports:
        return False, "no ports found"
    for port in ports:
        gid_file = port / "gids" / str(gid_index)
        try:
            text = gid_file.read_text(encoding="utf-8")
        except OSError:
            continue
        first = text.split("\n", 1)[0]
        hexpart = first.split()[0] if first.split() else ""
        # The all-zero GID (the one NCCL dies on) is 32 '0's (+ colons).
        if hexpart and set(hexpart.replace(":", "")) != {"0"}:
            return True, f"{hca_dir.name}/{port.name} gid{gid_index} ok"
    return False, f"no valid non-zero GID at index {gid_index} on any port"


def doctor_checks(*, registry_path: str | None = None) -> list[tuple[str, str, str, str]]:
    """Run every doctor check, returning (name, status, detail, hint) tuples.

    Broken out as a pure-ish function so tests can feed fixtures (meminfo
    text, a fake /sys tree, a registry path) without any network.
    """
    results: list[tuple[str, str, str, str]] = []

    def add(name: str, status: str, detail: str, hint: str = "") -> None:
        results.append((name, status, detail, hint))

    # 1. orchestrator health (unauthenticated by contract)
    try:
        orch_get("/health", auth=False, timeout=3.0)
        add("orchestrator", PASS, f"answers /health on {ORCHESTRATOR_BASE}")
    except GxError as exc:
        add("orchestrator", FAIL, str(exc), "the orchestrator service on the head node is not answering; gx status shows what else is up")

    # 2. gateway /health
    gw_ok = False
    for path in ("/health/liveliness", "/health"):
        try:
            http_json("GET", f"{GATEWAY_BASE}{path}", timeout=3.0)
            gw_ok = True
            add("gateway", PASS, f"answers {path} on {GATEWAY_BASE}")
            break
        except GxError:
            continue
    if not gw_ok:
        add("gateway", FAIL, f"no answer on {GATEWAY_BASE}/health", "gx-auto and gx-max aliases go through the gateway; check its container on the head node")

    # 3. registry valid
    reg: dict[str, Any] = {}
    reg_path = Path(registry_path) if registry_path else REGISTRY_PATH
    try:
        reg = json.loads(reg_path.read_text(encoding="utf-8"))
        schema = reg.get("schema")
        add("registry", PASS, f"valid JSON, schema {schema} at {reg_path}")
    except GxError:
        raise
    except FileNotFoundError:
        add("registry", FAIL, f"missing: {reg_path}")
    except json.JSONDecodeError as exc:
        add("registry", FAIL, f"invalid JSON: {exc}", "fix or restore legenex/models/registry.json before touching the cluster")

    # 4. model files present at registry paths
    cards = registry_model_cards(reg) if reg else []
    with_path = [c for c in cards if c["path"]]
    if not with_path:
        add("model files", WARN, "no model paths found in the registry", "registry schema drift -- no paths to verify")
    else:
        missing = [c["id"] for c in with_path if not Path(c["path"]).is_dir()]
        if missing:
            add("model files", FAIL, f"missing on this node: {', '.join(missing)}", "weights not downloaded/synced to the paths the registry names")
        else:
            add("model files", PASS, f"{len(with_path)} registry path(s) present")

    # 5. docker reachable
    ver = run_cmd(["docker", "version", "--format", "{{.Server.Version}}"], timeout=5.0)
    if ver:
        add("docker", PASS, f"server {ver}")
    else:
        add("docker", FAIL, "docker CLI present but no server answer", "the docker daemon on this node is not reachable")

    # 6. fabric interfaces up + GID 3 present (NCCL_IB_GID_INDEX pin)
    ib_root = Path("/sys/class/infiniband")
    hcas = []
    if ib_root.is_dir():
        hcas = sorted(p.name for p in ib_root.iterdir() if p.is_dir())
    nodes = registry_nodes(reg)
    expected = None
    if nodes:
        head = next((nodes[n] for n, ninfo in nodes.items() if isinstance(ninfo, dict) and ninfo.get("role") == "head"), None)
        expected = head.get("hcas") if isinstance(head, dict) else None
    if not hcas:
        add("fabric", FAIL, "no InfiniBand/RoCE devices in /sys/class/infiniband", "RoCE NICs not visible to the kernel; gx-max cannot run TP=2")
    else:
        problems = []
        details = []
        for hca in hcas:
            hca_dir = ib_root / hca
            state_files = sorted((hca_dir / "ports").glob("*/state")) if (hca_dir / "ports").is_dir() else []
            active = any("ACTIVE" in sf.read_text(encoding="utf-8", errors="replace") for sf in state_files)
            if expected is not None and hca not in (expected or []):
                # Present but not one the registry names -- informational only.
                details.append(f"{hca}: not in registry head hcas")
                continue
            if not active:
                problems.append(f"{hca} port not ACTIVE")
                continue
            gid_ok, gid_detail = _gid_table_ok(hca_dir, REQUIRED_GID_INDEX)
            if gid_ok:
                details.append(gid_detail)
            else:
                problems.append(f"{hca}: {gid_detail}")
        if problems:
            add("fabric", FAIL, "; ".join(problems), f"NCCL needs a live port and a non-zero GID at index {REQUIRED_GID_INDEX} (NCCL_IB_GID_INDEX=3 is the proven pin)")
        elif details:
            add("fabric", PASS, "; ".join(details))
        else:
            add("fabric", WARN, "HCAs present but none matching the registry", "check the hcas list in the registry nodes section")

    # 7. MemAvailable
    mem = mem_available_gib()
    if mem is None:
        add("memory", FAIL, "cannot read /proc/meminfo")
    elif mem < _MEM_FAIL_GIB:
        add("memory", FAIL, f"{mem} GiB MemAvailable", "too low for a long prompt; run `gx stop` or `gx drain` to release memory first")
    elif mem < _MEM_WARN_GIB:
        add("memory", WARN, f"{mem} GiB MemAvailable", "tight for long prompts (the loaded head runs near this level; MIA-RUNTIME.md envelope)")
    else:
        add("memory", PASS, f"{mem} GiB MemAvailable")

    # 8. disk free
    for mount in ("/srv", "/"):
        free = disk_free_bytes(mount)
        if free is None:
            add(f"disk {mount}", WARN, "cannot stat", "mount not accessible from here")
        elif free < _DISK_FAIL_BYTES:
            add(f"disk {mount}", FAIL, fmt_bytes(free) + " free", "critically low; clean up via the Control Center Storage page (never by hand)")
        elif free < _DISK_WARN_BYTES:
            add(f"disk {mount}", WARN, fmt_bytes(free) + " free", "model packs are ~200 GiB scale; plan cleanup before a new download")
        else:
            add(f"disk {mount}", PASS, fmt_bytes(free) + " free")

    # 9. scheduler state file
    sched_file = STATE_ROOT / "scheduler" / "queue.json"
    if not sched_file.exists():
        add("scheduler state", WARN, f"no state file at {sched_file}", "the scheduler has not persisted yet (fresh install) or is not running")
    else:
        try:
            json.loads(sched_file.read_text(encoding="utf-8"))
            add("scheduler state", PASS, f"valid JSON at {sched_file}")
        except (OSError, json.JSONDecodeError) as exc:
            add("scheduler state", FAIL, f"corrupt state file: {exc}", "stop the orchestrator and let it rebuild state, or restore from backups")

    return results


def cmd_doctor(args: argparse.Namespace) -> int:
    results = doctor_checks(registry_path=args.registry)
    width = max(len(name) for name, *_ in results) + 2
    fails = warns = passes = 0
    for name, status, detail, hint in results:
        mark = {"PASS": "PASS", "WARN": "WARN", "FAIL": "FAIL"}[status]
        line = f"{mark:<4} {name:<{width}} {detail}"
        if hint:
            line += f"\n     {'':<{width}} hint: {hint}"
        print(line)
        passes += status == PASS
        warns += status == WARN
        fails += status == FAIL
    kernel = run_cmd(["uname", "-r"])
    if kernel:
        pin = " (pinned kernel)" if kernel == EXPECTED_KERNEL else " *** NOT the pinned kernel -- see CLAUDE.md L-4 ***"
        print(f"kernel {kernel}{pin}")
    print(f"\n{passes} pass, {warns} warn, {fails} fail")
    if fails:
        print("fix the FAIL lines above (each has a hint); rerun `gx doctor`.")
        return 1
    return 0


# ---------------------------------------------------------------------------
# lifecycle: start / stop / restart / drain
# ---------------------------------------------------------------------------


def _lifecycle_action(path: str, body: dict[str, Any] | None, what: str, timeout: float) -> dict[str, Any]:
    try:
        out = orch_post(path, body, timeout=timeout)
    except GxError as exc:
        print(f"error: {exc}")
        raise SystemExit(1)
    if not isinstance(out, dict):
        print(f"error: unexpected response to {path}: {out!r}")
        raise SystemExit(1)
    return out


def _validate_profile(profile: str) -> str:
    reg = load_registry()
    profiles = registry_profiles(reg)
    if profiles and profile not in profiles:
        raise SystemExit(f"error: unknown profile {profile!r}; registry has: {', '.join(sorted(profiles))}")
    return profile


def cmd_start(args: argparse.Namespace) -> int:
    if args.reasoning not in REASONING_LEVELS:
        raise SystemExit(f"error: unknown reasoning level {args.reasoning!r} (choose from {', '.join(REASONING_LEVELS)})")
    profile = args.profile or "balanced"
    try:
        _validate_profile(profile)
    except GxError as exc:
        print(f"warn: {exc} (continuing with the orchestrator's own validation)")
    except SystemExit as exc:
        print(str(exc))
        return 1
    if args.reasoning:
        print(f"note: reasoning level {args.reasoning!r} is applied per request (chat_template_kwargs);")
        print("      the profile's reasoning_default stays as configured.")
    print(f"starting gx-max with profile {profile!r} -- a cold boot takes about 25 minutes (MIA-RUNTIME.md)...")
    body: dict[str, Any] = {"profile": profile}
    out = _lifecycle_action("/lifecycle/gx-max/acquire", body, "start", _LIFECYCLE_TIMEOUT_S)
    state = out.get("state", "?")
    print(f"acquire accepted: state={state} profile={out.get('profile', '?')}")
    if state in ("READY", "ready", "ok"):
        print("gx-max is READY.")
        return 0
    print(f"state is {state}; watch progress with `gx queue` or `gx logs --follow`.")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    out = _lifecycle_action("/lifecycle/gx-max/release", {}, "stop", _DR_TIMEOUT_S)
    print(f"release accepted: {json.dumps(out)[:200]}")
    print("memory returns to the host as the model unloads; `gx doctor` verifies it.")
    return 0


def cmd_restart(args: argparse.Namespace) -> int:
    profile = args.profile or "balanced"
    print(f"restarting gx-max with profile {profile!r} -- expect a full ~25 min boot...")
    out = _lifecycle_action("/lifecycle/gx-max/restart", {"profile": profile}, "restart", _LIFECYCLE_TIMEOUT_S)
    print(f"restart accepted: state={out.get('state', '?')} profile={out.get('profile', '?')}")
    return 0


def cmd_drain(args: argparse.Namespace) -> int:
    out = _lifecycle_action("/lifecycle/gx-max/drain", {}, "drain", _DR_TIMEOUT_S)
    print(f"drain accepted: {json.dumps(out)[:200]}")
    print("in-flight requests finish; no new ones are admitted. `gx stop` releases after.")
    return 0


# ---------------------------------------------------------------------------
# max (interactive quick-poke) + auto (one-shot gx-auto test call)
# ---------------------------------------------------------------------------


def cmd_max(args: argparse.Namespace) -> int:
    """Interactive chat quick-poke against gx-max.

    Prefers the direct head-loopback endpoint (fast, no key); falls back to
    the gx-max alias through the LiteLLM gateway when :8888 is not answering.
    """
    reasoning = args.reasoning or _DEFAULT_REASONING
    base, key, model = MODEL_BASE, "", SERVED_MODEL_ID
    try:
        http_json("GET", f"{MODEL_BASE}/health", timeout=2.0)
    except GxError:
        gw_key = gateway_key()
        if not gw_key:
            print("error: the model endpoint is down and no gateway key (GX_GATEWAY_KEY) is set for the fallback.")
            return 1
        base, key, model = GATEWAY_BASE + "/v1", gw_key, "gx-max"
    print(f"gx max -- talking to {model} on {base}")
    print("commands:  :q quit  :reasoning <level> switch the reasoning ladder")
    print(f"reasoning: {reasoning}\n")
    history: list[dict[str, str]] = []
    while True:
        try:
            line = input("you> ").strip()
        except EOFError:
            print()
            return 0
        if not line:
            continue
        if line == ":q":
            return 0
        if line.startswith(":reasoning"):
            parts = line.split()
            if len(parts) == 2 and parts[1] in REASONING_LEVELS:
                reasoning = parts[1]
                print(f"reasoning now {reasoning}")
            else:
                print(f"usage: :reasoning <{'|'.join(REASONING_LEVELS)}>")
            continue
        history.append({"role": "user", "content": line})
        try:
            reply = chat(messages=history, model=model, base=base, key=key, reasoning=reasoning)
        except GxError as exc:
            print(f"error: {exc}")
            continue
        print(f"model> {reply['content']}")
        history.append({"role": "assistant", "content": reply["content"]})


def cmd_auto(args: argparse.Namespace) -> int:
    """One-shot gx-auto test call through the gateway (auto profile/reasoning
    selection is the orchestrator's job -- this proves the whole path)."""
    key = gateway_key()
    if not key:
        print("error: no gateway key set (export GX_GATEWAY_KEY or LITELLM_MASTER_KEY).")
        return 1
    prompt = args.prompt or "What is 17 * 19? Answer with just the number."
    try:
        reply = chat(
            messages=[{"role": "user", "content": prompt}],
            model="gx-auto",
            base=GATEWAY_BASE + "/v1",
            key=key,
            max_tokens=2048,
            timeout=300.0,
        )
    except GxError as exc:
        print(f"error: {exc}")
        return 1
    usage = reply["usage"]
    print(f"model id : {reply['model']}")
    print(f"content  : {reply['content']}")
    print(f"tokens   : prompt={usage.get('prompt_tokens', '?')} completion={usage.get('completion_tokens', '?')}")
    if "323" in reply["content"]:
        print("smoke check: PASS (17*19=323 present)")
        return 0
    print("smoke check: answer did not contain 323 -- inspect the content above.")
    return 1


# ---------------------------------------------------------------------------
# profile / queue / requests
# ---------------------------------------------------------------------------


def cmd_profile(args: argparse.Namespace) -> int:
    reg = load_registry()
    profiles = registry_profiles(reg)
    live = ""
    try:
        status = orch_get("/text/status")
        if isinstance(status, dict):
            live = str(status.get("profile", "?"))
    except GxError:
        pass
    if args.cmd == "list" or (not profiles and not args.cmd):
        if not profiles:
            print("registry has no profiles section (schema < 2).")
            return 1
        print(f"{'profile':<10} {'seqs':<5} {'spec':<8} {'max_len':<8} {'reasoning':<10} target")
        for name, p in profiles.items():
            print(
                f"{name:<10} {str(p.get('max_num_seqs', '?')):<5} "
                f"{str(p.get('spec_method', '?')):<8} {str(p.get('max_model_len', '?')):<8} "
                f"{str(p.get('reasoning_default', '?')):<10} {p.get('target', '')}"
            )
        if live:
            print(f"\nlive profile: {live}")
        return 0
    # show P
    name = args.profile_name
    if name not in profiles:
        print(f"error: unknown profile {name!r}; registry has: {', '.join(sorted(profiles))}")
        return 1
    p = profiles[name]
    print(f"profile {name}")
    for key in ("max_num_seqs", "spec_method", "dspark_tokens", "max_model_len", "reasoning_default", "target"):
        if key in p:
            print(f"  {key:<18} {p[key]}")
    if live:
        print(f"  {'live now':<18} {'YES' if live == name else f'no (live is {live})'}")
    return 0


def cmd_queue(args: argparse.Namespace) -> int:
    def snapshot() -> bool:
        try:
            sched = orch_get("/scheduler/status")
        except GxError as exc:
            print(f"error: {exc}")
            return False
        if not isinstance(sched, dict):
            print(f"error: unexpected scheduler status: {sched!r}")
            return False
        print(f"scheduler -- {time.strftime('%H:%M:%S')}")
        for key in ("queued", "active", "done", "error", "cancelled", "timeout"):
            if key in sched:
                print(f"  {key:<10} {sched[key]}")
        for key in ("queue", "requests", "items"):
            value = sched.get(key)
            if isinstance(value, list):
                print(f"  {key}:")
                for r in value[:20]:
                    if isinstance(r, dict):
                        print(
                            f"    {str(r.get('id', '?'))[:14]:<14} {str(r.get('state', '?')):<10} "
                            f"p={r.get('priority', '?')} {r.get('project', '?')}/{r.get('agent', '?')} "
                            f"{r.get('profile', '')}"
                        )
        return True

    if not snapshot():
        return 1
    if args.watch:
        print("(refreshing every 2 s -- Ctrl-C to stop)")
        try:
            while True:
                time.sleep(2)
                print("\x1b[2J\x1b[H", end="")
                snapshot()
        except KeyboardInterrupt:
            return 0
    return 0


def cmd_requests(args: argparse.Namespace) -> int:
    try:
        hist = orch_get(f"/scheduler/history?limit={args.last}")
    except GxError as exc:
        print(f"error: {exc}")
        return 1
    if isinstance(hist, dict):
        hist = hist.get("events") or hist.get("history") or []
    if not isinstance(hist, list):
        print(f"error: unexpected history shape: {hist!r}")
        return 1
    if not hist:
        print("no scheduler history yet.")
        return 0
    print(f"{'id':<14} {'state':<10} {'prio':<5} {'proj/agent':<24} {'profile':<8} {'tokens':>10} {'tps':>6}")
    for r in hist:
        if not isinstance(r, dict):
            continue
        tokens = (r.get("prompt_tokens") or 0) + (r.get("completion_tokens") or 0)
        print(
            f"{str(r.get('id', '?'))[:14]:<14} {str(r.get('state', '?')):<10} {str(r.get('priority', '?')):<5} "
            f"{str(r.get('project', '?'))[:12]}/{str(r.get('agent', '?'))[:10]:<10} "
            f"{str(r.get('profile', '')):<8} {tokens:>10} {str(r.get('tps', '')):>6}"
        )
    return 0


# ---------------------------------------------------------------------------
# logs
# ---------------------------------------------------------------------------


def _collect_log_files() -> list[Path]:
    """The gx-text logs plus the orchestrator log, newest first."""
    files: list[Path] = []
    for candidate in LOG_ROOT.glob("gx-text*"):
        if candidate.is_dir():
            inner = sorted(candidate.glob("*.log"), key=lambda p: p.stat().st_mtime if p.exists() else 0)
            files.extend(inner[-2:])
        else:
            files.append(candidate)
    files.extend(p for p in LOG_ROOT.glob("*orchestrator*.log") if p.is_file())
    orch_log_dir = LOG_ROOT / "orchestrator"
    if orch_log_dir.is_dir():
        files.extend(p for p in orch_log_dir.glob("*.log") if p.is_file())
    # de-dup, newest first
    seen = set()
    unique = []
    for f in sorted(files, key=lambda p: p.stat().st_mtime, reverse=True):
        if f not in seen:
            seen.add(f)
            unique.append(f)
    return unique


def cmd_logs(args: argparse.Namespace) -> int:
    files = _collect_log_files()
    if not files:
        print(f"no gx-text or orchestrator logs found under {LOG_ROOT}.")
        return 1
    if args.follow:
        cmd = ["tail", "-n", str(args.lines), "-F", *[str(f) for f in files]]
        print(f"following: {', '.join(str(f) for f in files)} (Ctrl-C to stop)")
        try:
            os.execvp("tail", cmd)
        except OSError as exc:
            print(f"error: cannot run tail: {exc}")
            return 1
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            print(f"{f}: unreadable ({exc})")
            continue
        print(f"--- {f} (last {args.lines}) " + "-" * 20)
        for line in text[-args.lines:]:
            print(line)
    return 0


# ---------------------------------------------------------------------------
# benchmark / models / nodes / storage / backup / update
# ---------------------------------------------------------------------------


def cmd_benchmark(args: argparse.Namespace) -> int:
    if not BENCH_SCRIPT.is_file():
        print(f"error: benchmark suite not found at {BENCH_SCRIPT}")
        return 1
    suite = args.suite
    if suite == "matrix":
        # The PHASE-35 matrix: quick suite per profile, with an orchestrator
        # restart between profiles (procedure documented in ops/bench/README.md).
        reg = load_registry()
        profiles = registry_profiles(reg)
        order = [p for p in ("fast", "balanced", "swarm", "deep", "long") if p in profiles]
        if not order:
            print("error: registry has no profiles to run a matrix over.")
            return 1
        print(f"matrix: quick suite per profile with a restart between profiles: {', '.join(order)}")
        for profile in order:
            print(f"\n=== profile {profile}: restarting ===")
            rc = cmd_restart(argparse.Namespace(profile=profile))
            if rc != 0:
                return rc
            label = f"matrix-{profile}"
            rc = _run_bench(["--suite", "quick", "--label", label])
            if rc != 0:
                print(f"warn: quick suite for profile {profile} exited {rc}; continuing")
        return 0
    return _run_bench(["--suite", suite])


def _run_bench(extra: list[str]) -> int:
    cmd = [sys.executable, str(BENCH_SCRIPT), *extra]
    try:
        proc = subprocess.run(cmd)
    except OSError as exc:
        print(f"error: cannot run {BENCH_SCRIPT}: {exc}")
        return 1
    return proc.returncode


def cmd_models(args: argparse.Namespace) -> int:
    reg = load_registry()
    cards = registry_model_cards(reg)
    if not cards:
        print("registry has no models or aliases cards.")
        return 1
    live_ids: list[str] = []
    try:
        models = orch_get("/v1/models")
        if isinstance(models, dict):
            live_ids = [m.get("id") for m in models.get("data", []) if isinstance(m, dict)]
    except GxError:
        pass
    print(f"registry: {REGISTRY_PATH}")
    for c in cards:
        print()
        print(f"{c['id']}")
        print(f"  source      {c['source']}")
        print(f"  revision    {c['revision'][:16]}{'...' if len(str(c['revision'])) > 16 else ''}")
        print(f"  path        {c['path'] or '-'}")
        if c["engram_dir"]:
            print(f"  engram dir  {c['engram_dir']}")
        if c["quant"]:
            print(f"  quant       {c['quant']}")
        if c["context"]:
            print(f"  context     {c['context']}")
        unc = "UNCENSORED" if c["uncensored"] else "stock"
        print(f"  status      {unc}{'' if not isinstance(c['uncensored_raw'], str) or c['uncensored'] else f' (raw: {c['uncensored_raw']})'}")
        on_disk = Path(c["path"]).is_dir() if c["path"] else None
        print(f"  on disk     {'yes' if on_disk else ('no' if on_disk is False else '?')}")
    if live_ids:
        print(f"\nlive model ids from the orchestrator: {', '.join(str(i) for i in live_ids)}")
    else:
        print("\nlive model ids: orchestrator unreachable or /v1/models empty")
    return 0


def cmd_nodes(args: argparse.Namespace) -> int:
    reg = load_registry()
    nodes = registry_nodes(reg)
    cluster = reg.get("cluster", {})
    if nodes:
        print(f"cluster: {cluster.get('name', '?')}  head={cluster.get('head', '?')}  worker={cluster.get('worker', '?')}")
        for name, n in nodes.items():
            if not isinstance(n, dict):
                continue
            print(f"\n{name} ({n.get('role', '?')})")
            print(f"  user         {n.get('user', '?')}")
            print(f"  lan ip       {n.get('lan_ip', '?')}")
            print(f"  tailscale    {n.get('tailscale_ip', '?')}")
            fabric = n.get("fabric") or {}
            for rail, ip in fabric.items():
                print(f"  fabric {rail:<6} {ip}")
            if n.get("hcas"):
                print(f"  hcas         {', '.join(n['hcas'])}")
            print(f"  ssh          {n.get('ssh', '?')}")
        return 0
    # schema-1 registry has no nodes section: report the local node honestly.
    kernel = run_cmd(["uname", "-r"]) or "?"
    print("registry has no nodes section (schema < 2); local facts only:")
    print(f"  this node    kernel {kernel}, MemAvailable {mem_available_gib()} GiB")
    print("  note         fabric/worker facts come from the schema-2 registry (V4.1 rebuild)")
    return 0


def cmd_storage(args: argparse.Namespace) -> int:
    print(f"storage summary (head node) -- {time.strftime('%Y-%m-%d %H:%M')}")
    for mount in ("/", "/srv", "/srv/models", "/srv/cache", "/srv/logs"):
        try:
            usage = shutil.disk_usage(mount)
        except OSError:
            print(f"  {mount:<14} (not accessible)")
            continue
        pct = usage.used * 100 // usage.total if usage.total else 0
        print(f"  {mount:<14} total {fmt_bytes(usage.total):>9}  used {fmt_bytes(usage.used):>9}  free {fmt_bytes(usage.free):>9} ({pct}%)")
    models_dir = Path("/srv/models")
    if models_dir.is_dir():
        print("\n  /srv/models children (shallow size estimate):")
        entries = sorted(models_dir.iterdir(), key=lambda p: (p.name,))
        for entry in entries:
            size = dir_size_bytes(entry) if entry.is_dir() else entry.stat().st_size
            print(f"    {entry.name:<40} {fmt_bytes(size):>10}")
    mem = mem_available_gib()
    print(f"\n  MemAvailable  {mem} GiB" if mem is not None else "\n  MemAvailable  ?")
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    """Invoke the gx-backup CLI when it is installed; otherwise say so.

    The backup tooling itself lives outside this package (RECOVERY.md);
    this subcommand is only the operator's single entry point.
    """
    candidates = [Path.home() / ".local" / "bin" / "gx-backup", Path.home() / ".local" / "bin" / "gx"]
    for cand in candidates:
        if cand.is_file() and os.access(cand, os.X_OK):
            cmd = [str(cand)]
            if cand.name == "gx":
                cmd.append("backup")
            print(f"running: {' '.join(cmd)}")
            try:
                proc = subprocess.run(cmd)
            except OSError as exc:
                print(f"error: {exc}")
                return 1
            return proc.returncode
    on_path = shutil.which("gx-backup")
    if on_path:
        print(f"running: gx-backup")
        try:
            proc = subprocess.run(["gx-backup"])
        except OSError as exc:
            print(f"error: {exc}")
            return 1
        return proc.returncode
    print("hint: the gx-backup CLI is not installed.")
    print("      expected at ~/.local/bin/gx-backup (or on PATH); see RECOVERY.md for the")
    print("      backup recipe. Until it is installed, run the documented backup steps by hand.")
    return 1


def cmd_update(args: argparse.Namespace) -> int:
    """Orchestrator update check via its API. The route name is not yet
    pinned across orchestrator versions, so a small candidate list is
    probed and the first answering one wins; absence is reported plainly."""
    candidates = ("/update", "/updates", "/update/check", "/updates/check", "/version")
    for path in candidates:
        try:
            out = orch_get(path, timeout=3.0)
        except GxError as exc:
            if "HTTP 404" in str(exc):
                continue
            print(f"error: {exc}")
            return 1
        print(f"update check ({path}):")
        print(json.dumps(out, indent=2)[:2000])
        return 0
    print("the orchestrator does not expose an update check yet (tried: " + ", ".join(candidates) + ").")
    print("check the repo's CURRENT_STATE.md / CHANGELOG.md for the running version instead.")
    return 1


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gx",
        description="GX Cluster control CLI (V4.1) -- no Docker or systemd knowledge needed.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="cluster + queue on one screen").set_defaults(func=cmd_status)

    doctor = sub.add_parser("doctor", help="PASS/FAIL/WARN health battery")
    doctor.add_argument("--registry", help="override the registry.json path (tests)", default=None)
    doctor.set_defaults(func=cmd_doctor)

    start = sub.add_parser("start", help="boot gx-max (acquire both nodes)")
    start.add_argument("--profile", default=None, help="serving profile (default: balanced)")
    start.add_argument("--reasoning", default=_DEFAULT_REASONING, choices=REASONING_LEVELS, help="default reasoning ladder level (applied per request)")
    start.set_defaults(func=cmd_start)

    sub.add_parser("stop", help="release gx-max (unload the model)").set_defaults(func=cmd_stop)

    restart = sub.add_parser("restart", help="restart gx-max with a profile")
    restart.add_argument("--profile", default=None, help="serving profile (default: balanced)")
    restart.set_defaults(func=cmd_restart)

    sub.add_parser("drain", help="stop admitting new requests; in-flight ones finish").set_defaults(func=cmd_drain)

    maxp = sub.add_parser("max", help="interactive chat quick-poke against gx-max")
    maxp.add_argument("--reasoning", default=_DEFAULT_REASONING, choices=REASONING_LEVELS)
    maxp.set_defaults(func=cmd_max)

    auto = sub.add_parser("auto", help="one-shot gx-auto test call")
    auto.add_argument("--prompt", default=None, help="override the smoke prompt")
    auto.set_defaults(func=cmd_auto)

    profile = sub.add_parser("profile", help="serving profiles")
    profile.add_argument("cmd", nargs="?", default=None, help="list | show")
    profile.add_argument("profile_name", nargs="?", default=None)
    profile.set_defaults(func=cmd_profile)

    queue = sub.add_parser("queue", help="live scheduler status")
    queue.add_argument("--watch", action="store_true", help="refresh every 2 s")
    queue.set_defaults(func=cmd_queue)

    requests = sub.add_parser("requests", help="scheduler history")
    requests.add_argument("--last", type=int, default=20, help="how many entries (default 20)")
    requests.set_defaults(func=cmd_requests)

    logs = sub.add_parser("logs", help="tail gx-text + orchestrator logs")
    logs.add_argument("--follow", action="store_true", help="follow (tail -F)")
    logs.add_argument("--lines", type=int, default=40, help="lines shown per file (default 40)")
    logs.set_defaults(func=cmd_logs)

    bench = sub.add_parser("benchmark", help="run the ops/bench suite")
    bench.add_argument("--suite", choices=("quick", "full", "coding", "matrix"), default="quick")
    bench.set_defaults(func=cmd_benchmark)

    sub.add_parser("models", help="registry model cards (uncensored status, revisions)").set_defaults(func=cmd_models)
    sub.add_parser("nodes", help="node facts from the registry").set_defaults(func=cmd_nodes)
    sub.add_parser("storage", help="head-node storage summary").set_defaults(func=cmd_storage)
    sub.add_parser("backup", help="invoke the gx-backup CLI").set_defaults(func=cmd_backup)
    sub.add_parser("update", help="orchestrator update check").set_defaults(func=cmd_update)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "profile":
        args.cmd = args.cmd or "list"
        if args.cmd == "show" and not args.profile_name:
            parser.error("profile show needs a profile name")
        if args.cmd not in ("list", "show"):
            parser.error(f"unknown profile subcommand {args.cmd!r} (list | show)")
    try:
        return args.func(args)
    except GxError as exc:
        print(f"error: {exc}")
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
