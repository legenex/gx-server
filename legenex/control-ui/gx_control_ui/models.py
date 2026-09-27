"""Model cards for the V4.1 single-model world, read from registry.json.

The registry (``legenex/models/registry.json``, schema 2) is the single source
of truth: models (stock + uncensored DeepSeek V4.1 Flash EXL3 packs, revisions,
runtime pins), aliases (gx-max, gx-auto ONLY), profiles (fast / balanced /
swarm / deep / long / custom), the reasoning ladder and the NCCL fabric.

There is no per-tier load/unload any more: the unit of control is the gx-max
lifecycle (DOWN -> ACQUIRING -> READY -> RELEASING), owned by the orchestrator.
This module only READS the registry and shapes live facts; it never mutates
anything.

If the registry is not at schema 2 yet (the rebuild is in progress), every
view degrades honestly: cards say "registry not at schema 2" and no fact is
fabricated.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from .util import TTLCache

#: The only public aliases (ARCHITECTURE-V41.md section 1).
ALIASES = ("gx-max", "gx-auto")

#: Registry schema this module understands.
SCHEMA = 2

_lock = threading.Lock()
_cache: dict[tuple[str, int, int], dict] = {}


def read_registry(path: Path | None = None, max_age: float = 5.0) -> dict:
    """The parsed registry, cached briefly (autosync rewrites the file often)."""
    p = Path(path) if path is not None else None
    if p is None:
        from .config import UIConfig
        p = UIConfig.registry_path  # type: ignore[attr-defined]
    try:
        sig = (str(p), p.stat().st_mtime_ns, p.stat().st_size)
    except OSError:
        return {}
    with _lock:
        hit = _cache.get(sig)
    if hit is not None and time.time() - hit["_loaded_at"] < max_age:
        return hit
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    data = dict(data)
    data["_loaded_at"] = time.time()
    data["_path"] = str(p)
    with _lock:
        if len(_cache) > 8:
            _cache.clear()
        _cache[sig] = data
    return data


def registry_ok(reg: dict) -> bool:
    return isinstance(reg, dict) and reg.get("schema") == SCHEMA


def cluster_nodes(reg: dict) -> dict[str, dict]:
    nodes = reg.get("nodes")
    return nodes if isinstance(nodes, dict) else {}


def production_model_id(reg: dict) -> str | None:
    """The model id the gx-max alias points at (the production pack)."""
    spec = (reg.get("aliases") or {}).get("gx-max") or {}
    mid = spec.get("model")
    if isinstance(mid, str) and mid and not mid.startswith("<"):
        return mid
    return None


def cards(reg: dict) -> dict[str, dict[str, Any]]:
    """One card per model pack in the registry ``models`` section.

    Each card carries: id, source, revision, path, quant, vision, tools,
    max_context, engram_dir, serving_notes, uncensored badge and whether it is
    the production/active pack (what gx-max serves).
    """
    if not registry_ok(reg):
        return {}
    models = reg.get("models") or {}
    prod = production_model_id(reg)
    out: dict[str, dict[str, Any]] = {}
    for mid, spec in models.items():
        if not isinstance(spec, dict):
            continue
        out[mid] = {
            "id": mid,
            "source": spec.get("source"),
            "revision": spec.get("revision"),
            "path": spec.get("path"),
            "engram_dir": spec.get("engram_dir"),
            "quant": spec.get("quant"),
            "vision": bool(spec.get("vision")),
            "tools": bool(spec.get("tools")),
            "max_context": spec.get("max_context"),
            "serving_notes": spec.get("serving_notes"),
            "uncensored": bool(spec.get("uncensored")),
            "production": mid == prod,
            "active": mid == prod,
        }
    return out


def profiles(reg: dict) -> dict[str, dict[str, Any]]:
    """The serving profiles (fast/balanced/swarm/deep/long/custom) with their
    effective values, straight from the registry."""
    if not registry_ok(reg):
        return {}
    return {k: dict(v) for k, v in (reg.get("profiles") or {}).items() if isinstance(v, dict)}


def reasoning(reg: dict) -> dict[str, Any]:
    if not registry_ok(reg):
        return {"levels": [], "mapping": {}, "numeric_range": None}
    r = reg.get("reasoning") or {}
    return {"levels": list(r.get("levels") or []), "mapping": r.get("mapping") or {},
            "numeric_range": r.get("numeric_range")}


def runtimes(reg: dict) -> dict[str, dict[str, Any]]:
    if not registry_ok(reg):
        return {}
    return {k: dict(v) for k, v in (reg.get("runtimes") or {}).items() if isinstance(v, dict)}


def fabric_rails(reg: dict) -> list[dict[str, Any]]:
    """The ConnectX rails from the registry (node names, rail IPs, HCAs)."""
    nodes = cluster_nodes(reg)
    out: list[dict[str, Any]] = []
    heads = [n for n, s in nodes.items() if isinstance(s, dict) and s.get("role") == "head"]
    workers = [n for n, s in nodes.items() if isinstance(s, dict) and s.get("role") == "worker"]
    if not heads or not workers:
        return out
    head, worker = heads[0], workers[0]
    for rail_key in ("rail1", "rail2"):
        fab_h = (nodes[head].get("fabric") or {}).get(rail_key)
        fab_w = (nodes[worker].get("fabric") or {}).get(rail_key)
        if not fab_h or not fab_w:
            continue
        out.append({
            "name": rail_key.replace("rail", "Rail "),
            "head": {"node": head, "ip": fab_h,
                     "hca": ((nodes[head].get("hcas") or [None, None])[0] if rail_key == "rail1"
                             else (nodes[head].get("hcas") or [None, None])[1])},
            "worker": {"node": worker, "ip": fab_w,
                       "hca": ((nodes[worker].get("hcas") or [None, None])[0] if rail_key == "rail1"
                               else (nodes[worker].get("hcas") or [None, None])[1])},
        })
    return out


#: gx-max lifecycle state -> (card state, detail).
_GX_MAP = {
    "down": ("unavailable", "down; start it with a profile (Actions page)"),
    "acquiring": ("loading", "acquiring: rank1 then rank0, then health"),
    "ready": ("loaded", "serving DeepSeek V4.1 Flash (gx-max)"),
    "releasing": ("unloading", "releasing: stopping ranks, returning memory"),
}


def live_state(cluster, reg: dict) -> list[dict]:
    """The Models page: one card per model pack, plus gx-max / gx-auto.

    `cluster` is the services.Cluster; `reg` the parsed registry. Live facts
    come from the orchestrator lifecycle + scheduler; nothing is guessed.
    """
    packs = cards(reg)
    gx_state = cluster.gxmax_state()
    state, detail = _GX_MAP.get(gx_state, ("error", f"lifecycle state {gx_state!r}"))
    sched_ok = cluster.scheduler_ok()
    out: list[dict] = []
    for mid, card in packs.items():
        c = dict(card)
        if gx_state == "ready":
            c["state"] = "loaded" if card["production"] else "available"
            c["state_detail"] = ("serving as gx-max" if card["production"]
                                 else "on disk; not served (gx-max never silently falls back)")
        else:
            c["state"] = "available"
            c["state_detail"] = f"on disk; gx-max is {gx_state}"
        c["kind"] = "model"
        out.append(c)
    for alias in ALIASES:
        spec = (reg.get("aliases") or {}).get(alias) or {}
        if alias == "gx-max":
            astate, adetail = state, detail
        else:
            astate = "ready" if sched_ok else "unavailable"
            adetail = ("auto-selects profile and reasoning per request" if sched_ok
                       else "orchestrator scheduler unreachable")
        out.append({
            "id": alias, "alias": alias, "kind": "alias", "state": astate, "state_detail": adetail,
            "mode": spec.get("mode"), "description": spec.get("description"),
            "model": spec.get("model") or production_model_id(reg),
            "runtime": spec.get("runtime"),
            "uncensored": True,
        })
    return out


class ResultLog:
    """Last health / inference / lifecycle result per alias, persisted outside Git."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        try:
            self._data: dict = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._data = {}

    def record(self, alias: str, kind: str, ok: bool, detail: str = "", **extra: Any) -> None:
        with self._lock:
            entry = {"ok": ok, "at": time.time(), "detail": detail[:300], **extra}
            self._data.setdefault(alias, {})[kind] = entry
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self._data, indent=1), encoding="utf-8")
                tmp.replace(self.path)
            except OSError:
                pass

    def get(self, alias: str) -> dict:
        with self._lock:
            return dict(self._data.get(alias, {}))


def registry_view(cluster, cfg) -> dict:
    """Everything the Model page needs, shaped once (see WORKER-C-REPORT.md)."""
    reg = read_registry(cfg.registry_path)
    ok = registry_ok(reg)
    view: dict[str, Any] = {
        "schema": reg.get("schema"),
        "registry_ok": ok,
        "cluster": reg.get("cluster") or {},
        # Only the known aliases are ever echoed (a pre-schema-2 registry may
        # still name retired aliases; they must not leak back to a page).
        "aliases": {a: dict(s) for a, s in (reg.get("aliases") or {}).items()
                    if isinstance(s, dict) and a in ALIASES},
        "capabilities": reg.get("capabilities") or {},
        "runtimes": runtimes(reg),
        "fabric": fabric_rails(reg),
        "nodes": cluster_nodes(reg),
    }
    if ok:
        view["models"] = cards(reg)
        view["profiles"] = profiles(reg)
        view["reasoning"] = reasoning(reg)
    else:
        view["models"] = {}
        view["profiles"] = {}
        view["reasoning"] = {"levels": [], "mapping": {}, "numeric_range": None}
        view["note"] = ("registry.json is not at schema 2 yet (rebuild in progress); "
                        "no model facts are shown until it is")
    return view
