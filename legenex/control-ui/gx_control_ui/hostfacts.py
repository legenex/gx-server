"""Read-only host facts for ONE node, as a single JSON document.

This module is deliberately self-contained (stdlib only, no package imports)
because the control UI runs it in two ways:

  * imported on gx10-01 for the local node, and
  * streamed verbatim to gx10-02 as `ssh legenex-02@gx10-02 python3 - node2`
    so node 2 is described by exactly the same code, in one SSH round trip.

It reads /proc, /sys and a few fixed, read-only commands. It never changes
anything and never takes a lock for longer than a non-blocking probe.
"""

from __future__ import annotations

import fcntl
import glob
import json
import os
import socket
import subprocess
import sys
import time

EXPECTED_KERNEL = "6.17.0-1032-nvidia"

USER_UNITS = {
    "node1": [
        "gx-orchestrator.service", "gx-control-ui.service", "gx-hostwatch.timer",
        "gx-git-watch.service", "gx-git-autosync.timer", "gx-git-daily-audit.timer",
        "agentos-control-center.service", "agentos-supervisor.service",
    ],
    "node2": [
        "gx-hostwatch.timer", "gx-git-reconcile.timer", "gx-git-daily-audit.timer",
    ],
}

HOME = os.path.expanduser("~")

PATHS = {
    "node1": {
        "hostwatch_log": "/srv/logs/gx-hostwatch.log",
        "guard_lock": "/srv/projects/gx-cluster/state/guard/node1.lock",
        "watch_pid": "/srv/projects/gx-cluster/state/gx-max-rank0-watch.pid",
        "repo": os.path.join(HOME, "Documents/Projects/Server/gx-cluster"),
    },
    "node2": {
        "hostwatch_log": "/srv/logs/gx-hostwatch.log",
        "guard_lock": os.path.join(HOME, ".gx-guard/node2.lock"),
        "watch_pid": os.path.join(HOME, ".gx-guard/rank1-deadman.pid"),
        "repo": os.path.join(HOME, "Documents/Projects/Server/gx-cluster"),
    },
}


def _read(path: str, default: str = "") -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return default


def _cmd(args: list[str], timeout: float = 5.0) -> tuple[int, str]:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, str(exc)


def meminfo() -> dict:
    out = {}
    for line in _read("/proc/meminfo").splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts:
            try:
                out[key] = int(parts[0]) * 1024
            except ValueError:
                pass
    keys = ("MemTotal", "MemAvailable", "MemFree", "Cached", "SwapTotal", "SwapFree", "SwapCached",
            "Shmem", "Mlocked")
    return {k: out.get(k) for k in keys}


def psi() -> dict:
    res = {}
    for kind in ("memory", "cpu", "io"):
        entry = {}
        for line in _read(f"/proc/pressure/{kind}").splitlines():
            parts = line.split()
            if not parts:
                continue
            vals = dict(p.split("=", 1) for p in parts[1:] if "=" in p)
            entry[parts[0]] = {k: float(v) for k, v in vals.items() if k.startswith("avg")}
        res[kind] = entry
    return res


def swaps() -> list[dict]:
    rows = []
    for line in _read("/proc/swaps").splitlines()[1:]:
        p = line.split()
        if len(p) >= 4:
            rows.append({"name": p[0], "type": p[1], "size": int(p[2]) * 1024,
                         "used": int(p[3]) * 1024, "priority": p[4] if len(p) > 4 else ""})
    return rows


def loadavg() -> dict:
    p = _read("/proc/loadavg").split()
    return {"load1": float(p[0]), "load5": float(p[1]), "load15": float(p[2]),
            "nproc": os.cpu_count()} if len(p) >= 3 else {}


def uptime_seconds() -> float | None:
    p = _read("/proc/uptime").split()
    return float(p[0]) if p else None


def temperatures() -> dict:
    zones = []
    for z in sorted(glob.glob("/sys/class/thermal/thermal_zone*")):
        t = _read(os.path.join(z, "temp")).strip()
        if t.lstrip("-").isdigit():
            zones.append({"zone": os.path.basename(z), "type": _read(os.path.join(z, "type")).strip(),
                          "celsius": int(t) / 1000.0})
    gpu = {}
    rc, out = _cmd(["nvidia-smi", "--query-gpu=name,temperature.gpu,utilization.gpu,power.draw",
                    "--format=csv,noheader,nounits"], timeout=6)
    if rc == 0 and out.strip():
        f = [x.strip() for x in out.strip().splitlines()[0].split(",")]
        def num(v):
            try:
                return float(v)
            except ValueError:
                return None
        if len(f) >= 4:
            gpu = {"name": f[0], "celsius": num(f[1]), "util_pct": num(f[2]), "power_w": num(f[3])}
    return {"zones": zones, "cpu_max_c": max((z["celsius"] for z in zones), default=None), "gpu": gpu}


def rdma() -> list[dict]:
    rows = []
    for dev in sorted(glob.glob("/sys/class/infiniband/*")):
        name = os.path.basename(dev)
        port = os.path.join(dev, "ports/1")
        nets = sorted(os.listdir(os.path.join(dev, "device/net"))) if os.path.isdir(
            os.path.join(dev, "device/net")) else []
        def cnt(n):
            v = _read(os.path.join(port, "counters", n)).strip()
            return int(v) if v.isdigit() else None
        xmit, rcv = cnt("port_xmit_data"), cnt("port_rcv_data")
        rows.append({
            "device": name,
            "netdev": nets[0] if nets else "",
            "state": _read(os.path.join(port, "state")).strip(),
            "phys_state": _read(os.path.join(port, "phys_state")).strip(),
            "rate": _read(os.path.join(port, "rate")).strip(),
            "link_layer": _read(os.path.join(port, "link_layer")).strip(),
            # port_*_data counts in units of 4 octets
            "xmit_bytes": xmit * 4 if xmit is not None else None,
            "rcv_bytes": rcv * 4 if rcv is not None else None,
            "xmit_packets": cnt("port_xmit_packets"),
            "rcv_packets": cnt("port_rcv_packets"),
        })
    return rows


def interfaces() -> list[dict]:
    rc, out = _cmd(["ip", "-j", "addr"])
    rows = []
    if rc != 0:
        return rows
    try:
        data = json.loads(out)
    except ValueError:
        return rows
    for itf in data:
        name = itf.get("ifname", "")
        if name == "lo" or name.startswith(("veth", "br-", "docker")):
            continue
        addrs = [f"{a['local']}/{a['prefixlen']}" for a in itf.get("addr_info", []) if a.get("family") == "inet"]
        if not addrs:
            continue
        stats = {}
        base = f"/sys/class/net/{name}"
        for k in ("rx_bytes", "tx_bytes"):
            v = _read(f"{base}/statistics/{k}").strip()
            stats[k] = int(v) if v.isdigit() else None
        rows.append({"name": name, "state": itf.get("operstate", ""), "mtu": itf.get("mtu"),
                     "addrs": addrs, "speed_mbps": (_read(f"{base}/speed").strip() or None), **stats})
    return rows


def tailscale() -> dict:
    rc, out = _cmd(["tailscale", "status", "--json"], timeout=6)
    if rc != 0:
        return {"ok": False, "error": "tailscale status failed"}
    try:
        d = json.loads(out)
    except ValueError:
        return {"ok": False, "error": "bad tailscale json"}
    me = d.get("Self") or {}
    peers = []
    for p in (d.get("Peer") or {}).values():
        if str(p.get("HostName", "")).startswith("gx10"):
            peers.append({"host": p.get("HostName"), "ips": p.get("TailscaleIPs", []),
                          "online": bool(p.get("Online")), "direct": bool(p.get("CurAddr")),
                          "rx": p.get("RxBytes"), "tx": p.get("TxBytes")})
    return {"ok": d.get("BackendState") == "Running", "backend": d.get("BackendState"),
            "host": me.get("HostName"), "ips": me.get("TailscaleIPs", []),
            "dns": (me.get("DNSName") or "").rstrip("."), "online": bool(me.get("Online")),
            "peers": peers}


def docker_ps() -> dict:
    rc, out = _cmd(["docker", "ps", "-a", "--no-trunc", "--format", "{{json .}}"], timeout=8)
    if rc != 0:
        return {"ok": False, "containers": []}
    rows = []
    for line in out.splitlines():
        try:
            c = json.loads(line)
        except ValueError:
            continue
        rows.append({"name": c.get("Names"), "image": c.get("Image"), "state": c.get("State"),
                     "status": c.get("Status"), "ports": c.get("Ports"),
                     "created": c.get("CreatedAt"), "running_for": c.get("RunningFor")})
    return {"ok": True, "containers": rows}


def docker_stats() -> dict:
    rc, out = _cmd(["docker", "stats", "--no-stream", "--format", "{{json .}}"], timeout=10)
    res = {}
    if rc == 0:
        for line in out.splitlines():
            try:
                s = json.loads(line)
            except ValueError:
                continue
            res[s.get("Name")] = {"cpu": s.get("CPUPerc"), "mem": s.get("MemUsage"),
                                  "mem_pct": s.get("MemPerc"), "pids": s.get("PIDs")}
    return res


def user_units(role: str) -> list[dict]:
    units = USER_UNITS.get(role, [])
    if not units:
        return []
    rc, out = _cmd(["systemctl", "--user", "show", *units, "-p",
                    "Id,ActiveState,SubState,UnitFileState,Result,ActiveEnterTimestamp,"
                    "NextElapseUSecRealtime,LastTriggerUSec,ExecMainStatus"], timeout=6)
    rows, cur = [], {}
    for line in out.splitlines() + [""]:
        if not line.strip():
            if cur:
                rows.append(cur)
                cur = {}
            continue
        k, _, v = line.partition("=")
        cur[k] = v
    return [{"unit": r.get("Id"), "active": r.get("ActiveState"), "sub": r.get("SubState"),
             "enabled": r.get("UnitFileState"), "result": r.get("Result"),
             "since": r.get("ActiveEnterTimestamp"), "next": r.get("NextElapseUSecRealtime"),
             "last_trigger": r.get("LastTriggerUSec"), "exit_status": r.get("ExecMainStatus")}
            for r in rows]


def git_state(repo: str) -> dict:
    def g(*a):
        rc, out = _cmd(["git", "-C", repo, *a], timeout=6)
        return out.strip() if rc == 0 else None
    head = g("rev-parse", "HEAD")
    if head is None:
        return {"ok": False, "repo": repo}
    dirty = g("status", "--porcelain", "--untracked-files=no")
    return {"ok": True, "repo": repo, "head": head, "branch": g("rev-parse", "--abbrev-ref", "HEAD"),
            "subject": g("log", "-1", "--format=%s"), "date": g("log", "-1", "--format=%cI"),
            "dirty_files": len(dirty.splitlines()) if dirty else 0,
            "origin_main": g("rev-parse", "origin/main"),
            "push_url": g("remote", "get-url", "--push", "origin")}


def hostwatch(path: str) -> dict:
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            fh.seek(max(0, size - 8192))
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return {"ok": False, "error": "no hostwatch log"}
    last_ts, summary, checks = None, None, {}
    for line in reversed(lines):
        fields = {}
        for tok in line.replace("\\ ", "\x00").split(" "):
            k, _, v = tok.partition("=")
            fields[k] = v.replace("\x00", " ")
        if "check" not in fields:
            continue
        if fields["check"] == "summary":
            if summary is None:
                summary, last_ts = fields, fields.get("ts")
                continue
            break
        if summary is not None and fields.get("ts") == last_ts:
            checks.setdefault(fields["check"], {"level": fields.get("level"),
                                                "status": fields.get("status"),
                                                "detail": fields.get("detail")})
    if summary is None:
        return {"ok": False, "error": "no summary line yet"}
    return {"ok": True, "ts": last_ts, "status": summary.get("status"),
            "detail": summary.get("detail"), "checks": checks,
            "age_seconds": time.time() - os.path.getmtime(path)}


def lock_state(path: str) -> str:
    """'free', 'held' or 'absent'. Non-blocking shared probe; never waits."""
    if not os.path.exists(path):
        return "absent"
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return "unknown"
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return "free"
    except BlockingIOError:
        return "held"
    finally:
        os.close(fd)


def pid_alive(pidfile: str) -> dict:
    raw = _read(pidfile).strip()
    if not raw.isdigit():
        return {"pidfile": pidfile, "present": False, "alive": False}
    pid = int(raw)
    try:
        os.kill(pid, 0)
        alive = True
    except ProcessLookupError:
        alive = False
    except PermissionError:
        alive = True
    return {"pidfile": pidfile, "present": True, "pid": pid, "alive": alive}


def collect(role: str) -> dict:
    paths = PATHS.get(role, PATHS["node1"])
    uname = os.uname()
    return {
        "role": role,
        "hostname": socket.gethostname(),
        "kernel": uname.release,
        "kernel_ok": uname.release == EXPECTED_KERNEL,
        "arch": uname.machine,
        "collected_at": time.time(),
        "uptime_seconds": uptime_seconds(),
        "memory": meminfo(),
        "swaps": swaps(),
        "psi": psi(),
        "load": loadavg(),
        "temperature": temperatures(),
        "rdma": rdma(),
        "interfaces": interfaces(),
        "tailscale": tailscale(),
        "docker": docker_ps(),
        "docker_stats": docker_stats(),
        "units": user_units(role),
        "git": git_state(paths["repo"]),
        "hostwatch": hostwatch(paths["hostwatch_log"]),
        "guard_lock": lock_state(paths["guard_lock"]),
        "gxmax_watcher": pid_alive(paths["watch_pid"]),
    }


if __name__ == "__main__":
    json.dump(collect(sys.argv[1] if len(sys.argv) > 1 else "node1"), sys.stdout)
