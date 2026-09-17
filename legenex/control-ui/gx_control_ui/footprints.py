"""Measured footprints of the Build V3 services (plt.md section 7).

The specialists publish lines like::

    FOOTPRINT gx-voice node=gx10-02 cold_gib=14.2 resident_gib=11.8 startup_s=38 measured=2026-09-17 evidence=/srv/logs/acceptance/build-v3/voi/x.log

in ``coordination/build-v3/{voi,cal,liv}.md``. ``python3 -m
gx_control_ui.footprints sync`` validates them and records them in
``legenex/models/registry.json`` as ``aliases.<alias>.measured_footprint``.
Resource Control and the catalogue read ONLY the registry: a number that was
not measured is shown as "not measured yet", never guessed.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]
SOURCES = {"gx-voice": "voi.md", "gx-call": "cal.md", "gx-live": "liv.md"}
LINE_RE = re.compile(
    r"^FOOTPRINT\s+(?P<alias>gx-(?:voice|call|live))\s+node=(?P<node>gx10-0[12])\s+"
    r"cold_gib=(?P<cold>[0-9]{1,3}(?:\.[0-9]{1,2})?)\s+resident_gib=(?P<resident>[0-9]{1,3}(?:\.[0-9]{1,2})?)\s+"
    r"startup_s=(?P<startup>[0-9]{1,5}(?:\.[0-9])?)\s+measured=(?P<date>20[0-9]{2}-[01][0-9]-[0-3][0-9])"
    r"(?:\s+evidence=(?P<evidence>/srv/logs/acceptance/[A-Za-z0-9._/\-]{1,200}))?\s*$")


def parse(text: str) -> dict[str, dict]:
    """The LAST valid FOOTPRINT line per alias (pure)."""
    out: dict[str, dict] = {}
    for raw in text.splitlines():
        line = raw.strip().strip("`")
        m = LINE_RE.match(line)
        if not m:
            continue
        cold, resident = float(m["cold"]), float(m["resident"])
        if not 0 < resident <= cold <= 121:
            continue
        out[m["alias"]] = {"node": m["node"], "cold_gib": cold, "resident_gib": resident,
                           "startup_s": float(m["startup"]), "measured": m["date"],
                           "evidence": m["evidence"]}
    return out


def measured_footprint(alias: str, spec: dict | None) -> dict:
    """What the UI shows for an alias's memory (never invented)."""
    fp = (spec or {}).get("measured_footprint")
    if isinstance(fp, dict) and isinstance(fp.get("cold_gib"), (int, float)):
        return {"measured": True, **{k: fp.get(k) for k in ("node", "cold_gib", "resident_gib", "startup_s",
                                                              "measured", "evidence")}}
    if alias in SOURCES:
        return {"measured": False, "label": "not measured yet"}
    return {"measured": True, "summary": (spec or {}).get("memory"), "startup": (spec or {}).get("startup")}


def sync(repo: Path = REPO) -> dict:
    registry_path = repo / "legenex" / "models" / "registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    aliases = registry.setdefault("aliases", {})
    changed: dict[str, Any] = {}
    for alias, name in SOURCES.items():
        path = repo / "coordination" / "build-v3" / name
        try:
            found = parse(path.read_text(encoding="utf-8")).get(alias)
        except OSError:
            continue
        if not found:
            continue
        spec = aliases.get(alias)
        if not isinstance(spec, dict):
            # the owning workstream registers the alias; PLT only adds measurements
            continue
        if spec.get("measured_footprint") != found:
            spec["measured_footprint"] = found
            changed[alias] = found
    if changed:
        text = json.dumps(registry, indent=2, ensure_ascii=False) + "\n"
        tmp = registry_path.with_name(".registry.json.plt.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, registry_path)
    return {"changed": changed}


if __name__ == "__main__":
    if sys.argv[1:] != ["sync"]:
        print("usage: python3 -m gx_control_ui.footprints sync", file=sys.stderr)
        raise SystemExit(2)
    print(json.dumps(sync()))
