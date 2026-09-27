"""The Network page: both ConnectX rails, registry-pinned, live-verified.

READ-ONLY by construction: `ip`, `ethtool`, `show_gids`, `ping`, /sys counters
and `docker ps --filter`. Nothing here can reconfigure the network (L-7).

The rails come from the registry (schema 2 ``nodes.<name>.fabric`` + ``hcas``);
when the registry is not readable the configured fallback fabric addresses
are used and the view says so.

Parsers are pure functions over text fixtures so they are unit-testable
without touching a real HCA.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from .config import UIConfig
from .models import fabric_rails, read_registry
from .util import run

_COUNTERS = ("rx_errors", "tx_errors", "rx_dropped", "tx_dropped", "rx_bytes", "tx_bytes")


# ------------------------------------------------------------ parsers
def parse_ip_br(text: str) -> dict[str, dict]:
    """`ip -br addr` output -> {iface: {state, ips[]}}."""
    out: dict[str, dict] = {}
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[0] in ("lo",):
            continue
        iface, state = parts[0], parts[1]
        ips = [p for p in parts[2:] if "/" in p]
        out[iface] = {"state": state, "ips": ips}
    return out


def parse_ethtool(text: str) -> dict:
    """`ethtool <iface>` output -> {speed_mbps, link, port}."""
    speed = link = port = None
    for line in (text or "").splitlines():
        m = re.match(r"^\s*Speed:\s*(\d+)", line)
        if m:
            speed = int(m.group(1))
        m = re.match(r"^\s*Link detected:\s*(\w+)", line)
        if m:
            link = m.group(1).lower() == "yes"
        m = re.match(r"^\s*Port:\s*(\w+)", line)
        if m:
            port = m.group(1).lower()
    return {"speed_mbps": speed, "link": link, "port": port}


def parse_show_gids(text: str) -> list[dict]:
    """`show_gids` output -> [{dev, port, gid_index, gid, gid_type}], low indexes first."""
    rows: list[dict] = []
    for line in (text or "").splitlines():
        m = re.match(r"^(\S+)\s+(\d+)\s+(\d+)[0x]*\s+([0-9A-Fa-f:]+)\s+(\S+.*)$", line)
        if m:
            rows.append({"dev": m.group(1), "port": int(m.group(2)), "gid_index": int(m.group(3)),
                         "gid": m.group(4), "gid_type": m.group(5).strip()})
    return rows


# ------------------------------------------------------------ live probes
def _iface_stats(iface: str) -> dict[str, int]:
    out: dict[str, int] = {}
    base = Path("/sys/class/net") / iface / "statistics"
    for name in _COUNTERS:
        try:
            out[name] = int((base / name).read_text().strip())
        except (OSError, ValueError):
            out[name] = -1
    return out


def _ping(host: str) -> dict:
    if host in ("", None):
        return {"ok": False, "reason": "no address configured"}
    res = run(["ping", "-c", "1", "-W", "2", str(host)], timeout=6)
    m = re.search(r"time[=<]([\d.]+)\s*ms", res.out)
    return {"ok": res.ok, "ms": float(m.group(1)) if m else None}


def _ibv_devinfo_present() -> dict:
    res = run(["which", "ibv_devinfo"], timeout=5)
    present = res.ok and bool(res.out.strip())
    info: dict[str, Any] = {"present": present}
    if present:
        dres = run(["ibv_devinfo"], timeout=10)
        devices = re.findall(r"hca_id:\s*(\S+)", dres.out or "")
        info["devices"] = sorted(set(devices))
        info["command_ok"] = dres.ok
    return info


def _rank_containers() -> list[dict]:
    res = run(["docker", "ps", "--format", "{{json .}}", "--filter", "name=rank"], timeout=15)
    rows = []
    for line in (res.out or "").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass
    return rows


class NetView:
    def __init__(self, cfg: UIConfig) -> None:
        self.cfg = cfg
        self._cache: tuple[float, dict] = (0.0, {})
        self._ttl = 15.0

    def rails(self) -> list[dict[str, Any]]:
        reg = read_registry(self.cfg.registry_path)
        rails = fabric_rails(reg)
        if rails:
            return rails
        # Registry not readable: the configured fallback (same fabric).
        return [{"name": f"Rail {i + 1}",
                 "head": {"node": "gx10-01", "ip": self.cfg.fabric_local[i], "hca": None},
                 "worker": {"node": "gx10-02", "ip": self.cfg.fabric_peers[i], "hca": None}}
                for i in range(len(self.cfg.fabric_peers))]

    def view(self, *, fresh: bool = False) -> dict:
        now = time.time()
        if not fresh and self._cache[0] and now - self._cache[0] < self._ttl:
            return self._cache[1]
        offline = self.cfg.offline
        ip_br = {} if offline else parse_ip_br(run(["ip", "-br", "addr"], timeout=8).out)
        rails_out: list[dict] = []
        for rail in self.rails():
            hca = (rail.get("head") or {}).get("hca")
            entry: dict[str, Any] = {"name": rail["name"],
                                     "head_ip": (rail.get("head") or {}).get("ip"),
                                     "worker_ip": (rail.get("worker") or {}).get("ip")}
            if hca:
                entry["hca"] = hca
                entry["interface"] = ip_br.get(hca) or ip_br.get(hca.replace("roce", "en"))
                if not offline:
                    et = run(["ethtool", hca], timeout=8)
                    entry["ethtool"] = parse_ethtool(et.out) if et.ok else \
                        {"speed_mbps": None, "link": None, "error": "ethtool failed or not installed"}
                    entry["counters"] = _iface_stats(hca)
                    gids = run(["show_gids", "-d", hca], timeout=10)
                    parsed = parse_show_gids(gids.out) if gids.ok else []
                    entry["gids"] = parsed
                    # NCCL_IB_GID_INDEX pin (registry fabric section): show the
                    # entry the pin selects so drift is visible at a glance.
                    reg = read_registry(self.cfg.registry_path)
                    pin = ((reg.get("fabric") or {}).get("nccl") or {}).get("NCCL_IB_GID_INDEX")
                    entry["nccl_gid_index_pin"] = pin
                    entry["gids_pinned"] = next((g for g in parsed
                                                 if str(g["gid_index"]) == str(pin)), None)
                else:
                    entry["ethtool"] = {"speed_mbps": None, "link": None}
                    entry["counters"] = {k: -1 for k in _COUNTERS}
                    entry["gids"] = []
                    entry["nccl_gid_index_pin"] = None
            entry["ping_worker"] = {"skipped": "offline mode"} if offline else \
                _ping(entry.get("worker_ip") or "")
            entry["ok"] = bool(entry.get("ethtool", {}).get("link")) and bool(entry["ping_worker"].get("ok"))
            entry["level"] = "ok" if entry["ok"] else "unknown" if offline else "crit"
            rails_out.append(entry)
        out = {
            "generated_at": now,
            "rails": rails_out,
            "rank_containers": [] if offline else _rank_containers(),
            "diagnostics_note": "read-only; no network reconfiguration from the dashboard (L-7)",
            "offline": offline,
        }
        self._cache = (now, out)
        return out

    def diagnostics(self) -> dict:
        """Ping every head<->worker fabric pair, and ibv_devinfo presence."""
        if self.cfg.offline:
            return {"available": False, "reason": "offline mode", "pairs": []}
        pairs = []
        for rail in self.rails():
            head_ip = (rail.get("head") or {}).get("ip")
            worker_ip = (rail.get("worker") or {}).get("ip")
            pairs.append({"rail": rail["name"], "from": "gx10-01", "to": "gx10-02",
                          "target": worker_ip, "result": _ping(worker_ip)})
        # The reverse direction runs on gx10-02 (SSH, management plane, L-3 is
        # about model traffic; a diagnostic ping is management).
        for rail in self.rails():
            head_ip = (rail.get("head") or {}).get("ip")
            res = run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                       self.cfg.node2_ssh, "ping", "-c", "1", "-W", "2", str(head_ip)], timeout=15)
            pairs.append({"rail": rail["name"], "from": "gx10-02", "to": "gx10-01",
                          "target": head_ip, "result": {"ok": res.ok, "ms": None}})
        return {"available": True, "generated_at": time.time(), "pairs": pairs,
                "ibv_devinfo": _ibv_devinfo_present()}
