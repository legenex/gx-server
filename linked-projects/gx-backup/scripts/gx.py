#!/usr/bin/env python3
"""gx — operator command for the dual ASUS GX10 cluster."""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

ROOT = Path(os.environ.get("GX_BACKUP_ROOT", Path.home() / "Documents/Projects/gx-backup"))
CONF = Path(os.environ.get("GX_BACKUP_CONF", Path.home() / ".config/gx-backup"))
NODE2 = os.environ.get("GX_NODE2_HOST", "gx10-02")
GATEWAY = os.environ.get("GX_GATEWAY", "http://127.0.0.1:4000/v1")
ORCH = os.environ.get("GX_ORCH", "http://127.0.0.1:18900")
OWUI = os.environ.get("GX_OWUI", "http://127.0.0.1:3000")
UI = os.environ.get("GX_UI", "http://127.0.0.1:8088")
CODE_01 = os.environ.get("GX_CODE_01", "http://127.0.0.1:19011/v1")
CODE_02 = os.environ.get("GX_CODE_02", "http://192.168.100.11:19011/v1")
MINI = os.environ.get("GX_MINI", "http://127.0.0.1:19001/v1")
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", NODE2]


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(cmd: list[str], timeout: int = 30, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True, timeout=timeout, check=check)


def http_json(url: str, payload: dict[str, Any] | None = None, headers: dict[str, str] | None = None, timeout: int = 20) -> tuple[int, Any]:
    data = None if payload is None else json.dumps(payload).encode()
    hdrs = {"User-Agent": "gx/1.0", "Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    req = Request(url, data=data, headers=hdrs, method="POST" if payload is not None else "GET")
    try:
        with urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(body) if body else {}
            except json.JSONDecodeError:
                return resp.status, body
    except URLError as exc:
        return 0, str(exc)
    except Exception as exc:  # noqa: BLE001
        code = getattr(getattr(exc, "code", None), "real", None) or getattr(exc, "code", 0) or 0
        return int(code or 0), str(exc)


def http_code(url: str, timeout: int = 5) -> int:
    req = Request(url, headers={"User-Agent": "gx/1.0"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return int(resp.status)
    except Exception as exc:  # noqa: BLE001
        return int(getattr(exc, "code", 0) or 0)


def banner(title: str) -> None:
    print(f"\n{title}")
    print("=" * max(12, len(title)))


def ok(flag: bool) -> str:
    return "PASS" if flag else "FAIL"


def load_gateway_key() -> str:
    env = Path.home() / "Documents/Projects/Server/gx-cluster/legenex/gateway/.env"
    if not env.exists():
        return os.environ.get("LITELLM_MASTER_KEY", "")
    for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("LITELLM_MASTER_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return os.environ.get("LITELLM_MASTER_KEY", "")


def ssh_ok() -> bool:
    try:
        r = run(SSH + ["hostname"], timeout=12)
        return r.returncode == 0 and "gx10-02" in (r.stdout + r.stderr)
    except Exception:
        return False


def node_facts(which: str) -> dict[str, Any]:
    if which == "gx10-01":
        host, mem, gpu = "localhost", None, None
        uname = run(["uname", "-r"]).stdout.strip()
        hostname = socket.gethostname()
        mem = run(["free", "-h"]).stdout
        try:
            gpu = run(["nvidia-smi", "--query-gpu=name,temperature.gpu,utilization.gpu", "--format=csv,noheader"]).stdout.strip()
        except Exception:
            gpu = "unavailable"
        df = run(["df", "-h", "/"]).stdout
        return {"hostname": hostname, "kernel": uname, "mem": mem, "gpu": gpu, "df": df}
    r = run(SSH + ["bash", "-lc", "hostname; uname -r; free -h | head -2; nvidia-smi --query-gpu=name,temperature.gpu,utilization.gpu --format=csv,noheader; df -h / | tail -1"], timeout=15)
    return {"raw": r.stdout, "ok": r.returncode == 0}


def cmd_status(_args: list[str]) -> int:
    banner("GX Cluster")
    n2 = ssh_ok()
    print(f"GX10-01       {ok(True):<6}  {socket.gethostname()}")
    print(f"GX10-02       {ok(n2):<6}  ssh {NODE2}")
    print(f"SSH           {ok(n2)}")
    ts = run(["tailscale", "status", "--json"], timeout=8)
    ts_ok = ts.returncode == 0
    print(f"Tailscale     {ok(ts_ok)}")
    ping = run(["ping", "-c", "1", "-W", "1", "192.168.100.11"])
    print(f"200Gbps A     {ok(ping.returncode == 0)}  192.168.100.10 ↔ .11")
    pingb = run(["ping", "-c", "1", "-W", "1", "192.168.101.11"])
    print(f"200Gbps B     {ok(pingb.returncode == 0)}  192.168.101.10 ↔ .11")
    print(f"Gateway       {ok(http_code(GATEWAY.replace('/v1','/health/liveliness') if True else GATEWAY) in (200, 401, 403) or http_code('http://127.0.0.1:4000/health/liveliness') in (200,401,403))}  {GATEWAY}")
    print(f"OpenWebUI     {ok(http_code(OWUI) == 200)}  {OWUI}")
    print(f"GX-Cluster    {ok(http_code(UI) == 200)}  {UI}")
    print(f"Orchestrator  {ok(http_code(ORCH) in (200, 404))}  {ORCH}")
    latest = Path.home() / "Documents/Backups/LATEST"
    print(f"Backup        {'PASS' if latest.exists() else 'NONE'}  {latest.read_text().strip() if latest.exists() else 'no snapshot yet'}")
    print()
    print("Modes: gx-mini  gx-code  gx-auto  gx-max")
    print("Commands: gx status | backup | doctor | models | logs | benchmark")
    return 0 if n2 else 1


def cmd_doctor(_args: list[str]) -> int:
    banner("gx doctor")
    checks = []

    def add(name: str, good: bool, detail: str = "") -> None:
        checks.append(good)
        print(f"{ok(good):<6} {name:24} {detail}")

    add("gx10-01 hostname", socket.gethostname() == "gx10-01", socket.gethostname())
    add("ssh gx10-02", ssh_ok())
    add("restic binary", shutil.which("restic") is not None or (Path.home() / ".local/bin/restic").exists())
    add("age binary", (Path.home() / ".local/bin/age").exists())
    add("restic password", (CONF / "restic-password").exists())
    add("docker", shutil.which("docker") is not None)
    add("gateway :4000", http_code("http://127.0.0.1:4000/health/liveliness") in (200, 401, 403) or http_code("http://127.0.0.1:4000/") in (200, 401, 403, 404))
    add("openwebui :3000", http_code("http://127.0.0.1:3000/") == 200)
    add("control-ui :8088", http_code("http://127.0.0.1:8088/") == 200)
    add("orchestrator :18900", http_code("http://127.0.0.1:18900/") in (200, 404))
    add("roce A", run(["ping", "-c", "1", "-W", "1", "192.168.100.11"]).returncode == 0)
    add("roce B", run(["ping", "-c", "1", "-W", "1", "192.168.101.11"]).returncode == 0)
    add("gx-backup repo", (ROOT / "models.lock").exists())
    print()
    failed = checks.count(False)
    print(f"{len(checks) - failed}/{len(checks)} checks passed")
    return 0 if failed == 0 else 1


def cmd_models(_args: list[str]) -> int:
    banner("gx models")
    lock = json.loads((ROOT / "models.lock").read_text())
    for name, spec in lock.get("models", {}).items():
        print(f"\n{name}")
        if name in ("gx-auto", "gx-max"):
            print(f"  role        {spec.get('role')}")
            print(f"  runtime     {spec.get('runtime')}")
            continue
        print(f"  repository  {spec.get('repository')}")
        print(f"  revision    {spec.get('revision')}")
        print(f"  quant       {spec.get('quantization') or spec.get('quantization_selected')}")
        print(f"  runtime     {spec.get('runtime')}")
        print(f"  nodes       {', '.join(spec.get('nodes') or [])}")
        path = spec.get("path")
        if path:
            p = Path(path)
            print(f"  on disk     {'yes' if p.exists() else 'NO'}  {path}")
    print("\nLive containers:")
    r = run(["docker", "ps", "--format", "{{.Names}}\t{{.Status}}"])
    print(r.stdout or r.stderr)
    return 0


def cmd_backup(args: list[str]) -> int:
    banner("GX Backup")
    script = ROOT / "backup" / "restic.sh"
    if not script.exists():
        print("FAIL missing backup/restic.sh")
        return 1
    env = os.environ.copy()
    env["PATH"] = str(Path.home() / ".local/bin") + ":" + env.get("PATH", "")
    proc = subprocess.run(["bash", str(script), "backup"], env=env)
    latest = Path.home() / "Documents/Backups/LATEST"
    ident = latest.read_text().strip() if latest.exists() else "unknown"
    print()
    print(f"GX10-01       PASS")
    print(f"GX10-02       {'PASS' if ssh_ok() else 'FAIL'}")
    print(f"Snapshot:     {ident}")
    print(f"Latest recovery point: {ident}")
    return proc.returncode


def cmd_restore(args: list[str]) -> int:
    banner("gx restore")
    if args and args[0] == "latest":
        dest = Path("/tmp/gx-restore-validate") / time.strftime("%Y%m%dT%H%M%SZ")
        dest.mkdir(parents=True, exist_ok=True)
        os.chmod(dest, 0o700)
        env = os.environ.copy()
        env["PATH"] = str(Path.home() / ".local/bin") + ":" + env.get("PATH", "")
        env["RESTIC_PASSWORD_FILE"] = str(CONF / "restic-password")
        repo = str(Path.home() / "Documents/Backups/restic/local")
        include = str(ROOT / "models.lock")
        r = subprocess.run(
            ["restic", "-r", repo, "restore", "latest", "--target", str(dest), "--include", include],
            env=env,
        )
        print(f"validation restore -> {dest}  rc={r.returncode}")
        return r.returncode
    print("Usage:")
    print("  gx restore latest          non-destructive validation restore")
    print("  gx restore --help          full restore is documented in docs/FULL-RESTORE.md")
    print("Destructive machine restore is never the default.")
    return 0


def cmd_logs(args: list[str]) -> int:
    banner("gx logs")
    src = args[0] if args else "all"
    paths = {
        "gateway": Path("/srv/logs/gx-text/gateway-text.jsonl"),
        "backup": Path.home() / "Documents/Backups",
        "cluster": Path("/srv/logs"),
    }
    if src in ("mini", "code", "all"):
        r = run(["docker", "logs", "--tail", "40", "gx-mini"], timeout=20)
        print("--- gx-mini ---")
        print((r.stdout or r.stderr)[-4000:])
    if src in ("gateway", "all"):
        p = paths["gateway"]
        if p.exists():
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()[-20:]
            print("--- gateway ---")
            print("\n".join(lines))
    if src in ("cluster", "all"):
        r = run(["journalctl", "--user", "-u", "gx-control-ui", "-n", "20", "--no-pager"], timeout=10)
        print("--- gx-control-ui ---")
        print(r.stdout[-3000:] if r.stdout else r.stderr)
    return 0


def cmd_benchmark(_args: list[str]) -> int:
    banner("gx benchmark")
    key = load_gateway_key()
    if not key:
        print("No gateway key available from gateway .env; skipping live tokens.")
        return 1
    payload = {
        "model": "gx-mini",
        "messages": [{"role": "user", "content": "Reply with the single word pong."}],
        "max_tokens": 16,
        "temperature": 0,
    }
    t0 = time.time()
    code, body = http_json(f"{GATEWAY}/chat/completions", payload, {"Authorization": f"Bearer {key}"}, timeout=60)
    dt = time.time() - t0
    print(f"gx-mini via gateway  http={code}  {dt:.2f}s")
    if isinstance(body, dict):
        choice = (body.get("choices") or [{}])[0]
        text = ((choice.get("message") or {}).get("content") or "")[:200]
        usage = body.get("usage") or {}
        print(f"  content={text!r}")
        print(f"  usage={usage}")
    else:
        print(str(body)[:300])
    return 0 if code == 200 else 1


def _chat(model: str, prompt: str, timeout: int = 180) -> int:
    key = load_gateway_key()
    if not key:
        print("FAIL no gateway key")
        return 1
    payload = {"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": 256, "temperature": 0.2}
    t0 = time.time()
    code, body = http_json(f"{GATEWAY}/chat/completions", payload, {"Authorization": f"Bearer {key}"}, timeout=timeout)
    dt = time.time() - t0
    print(f"{model}  http={code}  {dt:.2f}s")
    if isinstance(body, dict):
        text = (((body.get("choices") or [{}])[0].get("message") or {}).get("content") or "")[:800]
        print(text or json.dumps(body)[:800])
        return 0 if code == 200 else 1
    print(str(body)[:400])
    return 1


def cmd_mini(args: list[str]) -> int:
    prompt = " ".join(args) or "In one sentence, what is gx-mini for?"
    return _chat("gx-mini", prompt, timeout=60)


def cmd_code(args: list[str]) -> int:
    prompt = " ".join(args) or "Write a Python function is_palindrome(s) with a doctest."
    return _chat("gx-code", prompt, timeout=180)


def cmd_auto(args: list[str]) -> int:
    prompt = " ".join(args) or "Classify this as mini or code: summarise this log line: INFO backup ok."
    return _chat("gx-auto", prompt, timeout=180)


def cmd_max(args: list[str]) -> int:
    prompt = " ".join(args) or "Review this function for bugs: def add(a,b): return a-b"
    return _chat("gx-max", prompt, timeout=300)


def cmd_update(_args: list[str]) -> int:
    banner("gx update")
    r = run(["git", "-C", str(ROOT), "status", "-sb"])
    print(r.stdout)
    print("Recipe updates are git commits in gx-backup. Model weights are hf downloads via models.lock.")
    return 0


COMMANDS = {
    "status": cmd_status,
    "backup": cmd_backup,
    "restore": cmd_restore,
    "doctor": cmd_doctor,
    "models": cmd_models,
    "logs": cmd_logs,
    "benchmark": cmd_benchmark,
    "mini": cmd_mini,
    "code": cmd_code,
    "auto": cmd_auto,
    "max": cmd_max,
    "update": cmd_update,
}


def usage() -> None:
    print("gx — dual GX10 operator command")
    print()
    print("  gx status       cluster health")
    print("  gx backup       encrypted incremental backup")
    print("  gx restore latest")
    print("  gx doctor       aggregate health checks")
    print("  gx models       logical models and pins")
    print("  gx mini|code|auto|max [prompt]")
    print("  gx logs [source]")
    print("  gx benchmark")
    print("  gx update")


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        usage()
        return 0
    cmd = args[0]
    fn = COMMANDS.get(cmd)
    if not fn:
        print(f"unknown command: {cmd}")
        usage()
        return 2
    try:
        return int(fn(args[1:]))
    except subprocess.TimeoutExpired:
        print("FAIL command timed out")
        return 1


if __name__ == "__main__":
    sys.exit(main())
