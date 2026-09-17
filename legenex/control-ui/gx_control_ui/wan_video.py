"""Wan 2.2 text-to-video with LoRAs: library, pairing, presets, generation and
history (D-040, workstream WAN).

    browser -> /api/video/* (session + CSRF) -> WanVideo
        |- LoRA catalogue: media router GET /v1/loras (files on gx10-02, read-only)
        |- pairs, settings, presets, history: application DB (migration 020)
        '- generation: MediaJobs (queue, Resource Control gate, Library import)
              -> router POST /v1/videos {"loras": {"high": [...], "low": [...]}}

Rules enforced here (and again by the router):
* the browser selects library ENTRIES by id; file names come from the
  router's catalogue, never from the request;
* a high-noise file only ever goes to the high-noise branch and a low-noise
  file to the low-noise branch; a general/unknown-noise file needs an explicit
  branch choice, and "both" is an explicit opt-in that is never offered for a
  high- or low-noise file;
* unknown compatibility is refused unless the entry allows it explicitly;
* default strengths are configuration (``DEFAULTS``), not part of the
  workflow generator, and a user-chosen strength is never overwritten.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable
from typing import Any

from .media_jobs import MAX_PROMPT, VIDEO_SIZES, JobError, MediaJob, MediaJobs, RouterClient
from .media_library import LibraryError, MediaLibrary
from .redact import redact

log = logging.getLogger("gx.ui.wan")

T2V_WORKFLOW = "wan22-t2v-a14b-uncensored"
MODEL_ID = "wan22-t2v-a14b"
MODEL_LABEL = "Wan 2.2 T2V-A14B (fp8, two experts)"
ENTRY_ID = re.compile(r"^l_[0-9a-f]{16}$")
PRESET_ID = re.compile(r"^wp_[0-9a-f]{16}$")
GEN_ID = re.compile(r"^[0-9a-f]{16}$")
FLOW_REF = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
SAMPLERS = ("euler", "euler_ancestral", "dpmpp_2m", "dpmpp_2m_sde", "ddim", "uni_pc", "lcm", "res_multistep")
SCHEDULERS = ("simple", "normal", "karras", "exponential", "sgm_uniform", "beta", "ddim_uniform")
APPLY = ("pair", "high", "low", "both")
MAX_STACK = 8
MAX_TAGS = 12
MAX_PRESETS = 200
MAX_FRAMES = 161
ASPECT = {"640x640": "1:1", "704x704": "1:1", "512x512": "1:1", "832x480": "16:9 (approx.)",
          "480x832": "9:16 (approx.)"}

#: UI defaults and ranges (configuration, not workflow logic)
DEFAULTS: dict[str, Any] = {
    "strength_high": 0.8, "strength_low": 0.8, "strength_min": 0.0, "strength_max": 1.5, "strength_step": 0.05,
    "multi_lora_strength": 0.5, "max_stack": MAX_STACK,
    "size": "640x640", "seconds": 3.0, "fps": 16,
    "negative_prompt": "blurry, low quality, distorted, watermark, text, static, frozen frame",
    "advanced": {"shift": 5.0, "cfg": 1.0, "steps": 4, "boundary": 2, "sampler_name": "euler",
                 "scheduler": "simple"},
}
LIMITS: dict[str, tuple[float, float]] = {
    "seconds": (0.5, 10.0), "fps": (8, 24), "shift": (0.5, 20.0), "cfg": (1.0, 10.0), "steps": (2, 40),
}

#: human text for machine-readable failure codes (router and local)
ERROR_TEXT: dict[str, str] = {
    "lora_not_found": "A selected LoRA file is no longer on gx10-02. Rescan the LoRA library and choose it again.",
    "lora_high_missing": "The high-noise file of a paired LoRA is missing. Rescan, re-pair it or use it low-only.",
    "lora_low_missing": "The low-noise file of a paired LoRA is missing. Rescan, re-pair it or use it high-only.",
    "lora_invalid_file": "A selected LoRA is not a valid safetensors file and cannot be loaded.",
    "lora_unsupported_file": "Only .safetensors LoRA files are supported.",
    "lora_incompatible": "A selected LoRA is not compatible with Wan 2.2 T2V-A14B.",
    "lora_unknown_compatibility": "A selected LoRA's compatibility with Wan 2.2 could not be determined. Allow "
                                  "unknown compatibility for it in the LoRA library to use it anyway.",
    "lora_not_visible": "ComfyUI does not list a selected LoRA yet. Press Rescan and try again.",
    "lora_branch_mismatch": "A LoRA was placed on the wrong expert branch (high-noise files go to the high "
                            "branch, low-noise files to the low branch).",
    "lora_shared_not_allowed": "Applying one LoRA to both experts must be chosen explicitly.",
    "lora_apply_required": "Choose where a general LoRA applies: high noise, low noise or both.",
    "lora_disabled": "A selected LoRA is disabled in the LoRA library.",
    "lora_duplicate": "The same LoRA file is used twice on one expert branch.",
    "lora_too_many": f"At most {MAX_STACK} LoRAs per expert branch.",
    "lora_invalid_strength": "LoRA strengths must be between 0.0 and 1.5.",
    "lora_invalid_request": "The LoRA selection is not valid.",
    "lora_loader_unavailable": "ComfyUI's LoraLoaderModelOnly node is not available, so LoRAs cannot be applied.",
    "lora_unavailable": "The LoRA catalogue is not configured on the media router.",
    "lora_unsupported_workflow": "This video workflow does not accept LoRAs.",
    "comfy_unavailable": "ComfyUI on gx10-02 is not reachable right now. Try again in a moment.",
    "router_unavailable": "The media router on gx10-02 is not reachable right now. Try again in a moment.",
    "node_unavailable": "A workflow node is not installed in ComfyUI.",
    "model_unavailable": "A Wan 2.2 model file is not available in ComfyUI.",
    "workflow_rejected": "ComfyUI rejected the generated workflow (see details).",
    "workflow_invalid_graph": "The generated workflow graph failed its safety checks.",
    "out_of_memory": "gx10-02 ran out of memory during generation. Use fewer LoRAs, a shorter clip or a "
                     "smaller size.",
    "execution_error": "ComfyUI failed while generating (see details).",
    "cancelled": "The job was cancelled before it started.",
    "timeout": "The generation did not finish in time.",
    "timeout_error": "The generation did not finish in time.",
    "output_missing": "ComfyUI finished but produced no video file.",
    "insufficient_memory": "gx10-02 did not have enough free memory for this job, even after waiting.",
    "exceeds_node_reserve": "This job can never fit on gx10-02 while keeping its memory reserve.",
    "gx_max_active": "gx-max is using the cluster; video generation is paused until it is released.",
    "maintenance": "Maintenance mode is on; video generation is paused.",
}


class WanError(Exception):
    def __init__(self, message: str, status: int = 400, code: str = "invalid_request") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def human_error(code: str | None, message: str | None) -> str:
    """A readable sentence for a failure; never just 'Generation failed'."""
    if code and code in ERROR_TEXT:
        return ERROR_TEXT[code]
    text = (message or "").strip()
    if not text:
        return "The video could not be generated; the details are in the error log."
    return text[:300]


def entry_id(names: list[str]) -> str:
    return "l_" + hashlib.sha256("|".join(sorted(names)).encode()).hexdigest()[:16]


def snap_frames(seconds: float, fps: float) -> int:
    """The router's frame count: nearest 4k+1, 5..161 (validation.video_length)."""
    frames = max(5, min(int(round(seconds * fps)), MAX_FRAMES))
    remainder = (frames - 1) % 4
    lower = frames - remainder
    frames = lower if remainder <= 2 else lower + 4
    if frames > MAX_FRAMES:
        frames = lower
    return max(5, frames)


def _stem(name: str) -> str:
    base = name.rsplit("/", 1)[-1]
    return base[:-len(".safetensors")] if base.lower().endswith(".safetensors") else base


def _derived_name(high: str | None, low: str | None) -> str:
    if high and low:
        tokens = re.split(r"[^A-Za-z0-9]+", _stem(high))
        kept = [t for t in tokens if t and t.lower() not in ("high", "noise", "hn", "highnoise")]
        return " ".join(kept) or _stem(high)
    return _stem(high or low or "LoRA")


def _worst(values: list[str]) -> str:
    for level in ("incompatible", "unknown"):
        if level in values:
            return level
    return "compatible"


# ============================================================ library model
def build_library(files: list[dict], pairs: list[dict], unpaired: set[str], settings: dict[str, dict],
                  seen: dict[str, dict]) -> dict:
    """Group catalogue files into library entries (pure; see module rules).

    files     router catalogue rows (lora_catalog.LoraFile.public())
    pairs     manual pairs [{id, high_name, low_name}]
    unpaired  names whose automatic pairing the user split
    settings  entry_id -> stored settings
    seen      name -> {first_seen, last_seen, missing}
    """
    visible = [f for f in files if not f.get("shadowed_by")]
    by_name = {f["name"]: f for f in visible}
    used: set[str] = set()
    raw: list[dict] = []

    for p in pairs:
        high, low = by_name.get(p["high_name"]), by_name.get(p["low_name"])
        if high is None and low is None:
            continue
        state = "paired" if high and low else ("broken_high" if high is None else "broken_low")
        raw.append({"kind": "pair", "high": p["high_name"], "low": p["low_name"], "source": "manual",
                    "pair_id": p["id"], "state": state})
        used.update((p["high_name"], p["low_name"]))

    groups: dict[str, dict[str, list[dict]]] = {}
    for f in visible:
        if f["name"] in used or f["name"] in unpaired or f.get("noise") not in ("high", "low"):
            continue
        groups.setdefault(f.get("pair_key") or "", {"high": [], "low": []})[f["noise"]].append(f)
    for key, group in sorted(groups.items()):
        if key and len(group["high"]) == 1 and len(group["low"]) == 1:
            h, lo = group["high"][0]["name"], group["low"][0]["name"]
            raw.append({"kind": "pair", "high": h, "low": lo, "source": "auto", "pair_id": None, "state": "paired"})
            used.update((h, lo))

    for f in visible:
        if f["name"] in used:
            continue
        noise = f.get("noise") or "unknown"
        kind = noise if noise in ("high", "low", "general") else "unknown"
        raw.append({"kind": kind, "high": f["name"] if kind == "high" else None,
                    "low": f["name"] if kind == "low" else None,
                    "file": f["name"], "source": None, "pair_id": None,
                    "state": {"high": "high_only", "low": "low_only", "general": "general"}.get(kind, "unresolved")})

    entries = []
    for r in raw:
        names = [n for n in (r["high"], r["low"], r.get("file")) if n]
        names = list(dict.fromkeys(names))
        eid = entry_id(names)
        st = settings.get(eid, {})
        efiles = [by_name[n] for n in names if n in by_name]
        problems: list[str] = []
        if r["state"] == "broken_high":
            problems.append(f"high-noise file missing: {r['high']}")
        if r["state"] == "broken_low":
            problems.append(f"low-noise file missing: {r['low']}")
        if r["state"] == "high_only":
            problems.append("no low-noise partner found: applies to the high-noise expert only")
        if r["state"] == "low_only":
            problems.append("no high-noise partner found: applies to the low-noise expert only")
        if r["state"] == "unresolved":
            problems.append(efiles[0].get("noise_reason") or "high/low noise class could not be determined")
        for f in efiles:
            if not f.get("valid"):
                problems.append(f"{f['name']}: {f.get('error') or 'invalid file'}")
            elif f.get("compatibility") != "compatible":
                problems.append(f"{f['name']}: {f.get('compatibility')} ({f.get('compatibility_reason')})")
            if f.get("comfy_visible") is False:
                problems.append(f"{f['name']}: not listed by ComfyUI yet (rescan)")
        compat = _worst([f.get("compatibility", "unknown") for f in efiles]) if efiles else "unknown"
        if any(not f.get("valid") for f in efiles):
            compat = "incompatible"
        allow_unknown = bool(st.get("allow_unknown"))
        broken = r["state"] in ("broken_high", "broken_low")
        usable = (bool(efiles) and not broken and all(f.get("usable") for f in efiles)
                  and (compat == "compatible" or (compat == "unknown" and allow_unknown)))
        first_seen = [seen[n]["first_seen"] for n in names if n in seen]
        entries.append({
            "id": eid, "kind": r["kind"], "pair_state": r["state"], "pair_source": r["source"],
            "pair_id": r["pair_id"],
            "display_name": st.get("display_name") or (_derived_name(r["high"], r["low"]) if r["kind"] == "pair"
                                                        else _stem(r.get("file") or r["high"] or r["low"] or "")),
            "description": st.get("description") or "", "tags": st.get("tags") or [],
            "high_file": r["high"], "low_file": r["low"], "file": r.get("file"),
            "files": efiles, "size": sum(int(f.get("size") or 0) for f in efiles),
            "discovered_at": min(first_seen) if first_seen else None,
            "default_high": st.get("default_high"), "default_low": st.get("default_low"),
            "enabled": bool(st.get("enabled", True)), "allow_unknown": allow_unknown,
            "position": st.get("position"), "compatibility": compat, "usable": usable,
            "unresolved": r["state"] in ("unresolved", "broken_high", "broken_low"),
            "problems": problems,
            "apply_options": ["pair"] if r["kind"] == "pair" and not broken else
            (["high"] if r["kind"] == "high" or r["state"] == "broken_low" else
             ["low"] if r["kind"] == "low" or r["state"] == "broken_high" else ["high", "low", "both"]),
        })
    entries.sort(key=lambda e: (e["position"] if e["position"] is not None else 10**6, e["display_name"].lower()))
    return {"entries": entries,
            "unpaired_files": [f for f in visible if f["name"] not in used and f.get("noise") in ("high", "low")],
            "shadowed_files": [f for f in files if f.get("shadowed_by")]}


def summarize_chains(graph: dict) -> dict:
    """High/low LoRA chains, as applied, read from a built ComfyUI graph."""
    out: dict[str, Any] = {}
    for branch, sampler in (("high", "12"), ("low", "13")):
        chain: list[dict] = []
        model = None
        ref = (graph.get(sampler) or {}).get("inputs", {}).get("model")
        hops = 0
        while isinstance(ref, list) and len(ref) == 2 and str(ref[0]) in graph and hops < 64:
            node_id = str(ref[0])
            node = graph[node_id]
            if node.get("class_type") == "LoraLoaderModelOnly":
                chain.append({"node": node_id, "lora_name": node["inputs"].get("lora_name"),
                              "strength": node["inputs"].get("strength_model"),
                              "base": not (node_id.isdigit() and int(node_id) >= 1000)})
            if node.get("class_type") == "UNETLoader":
                model = node["inputs"].get("unet_name")
            ref = node.get("inputs", {}).get("model")
            hops += 1
        out[branch] = list(reversed(chain))
        out[f"{branch}_model"] = model
    return out


_ABS_PATH = re.compile(r"(?<![A-Za-z0-9_.-])/(?:srv|home|opt|tmp|var|etc|root|run|mnt|data|usr)/[^\s\"']*")
_IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b")
_HOST = re.compile(r"\bgx10-0\d\b", re.I)


def _scrub(value: Any) -> Any:
    if isinstance(value, str):
        text = _ABS_PATH.sub(lambda m: m.group(0).rsplit("/", 1)[-1], value)
        text = _IPV4.sub("[address]", text)
        text = _HOST.sub("[node]", text)
        return redact(text)
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _scrub(v) for k, v in value.items()}
    return value


def sanitize_graph(graph: dict) -> dict:
    """An exportable copy: model file names only, no host paths, addresses,
    node names or credentials."""
    return _scrub(copy.deepcopy(graph))


# ================================================================= service
class WanVideo:
    def __init__(self, library: MediaLibrary, media: MediaJobs, router: RouterClient, *,
                 audit: Callable[..., None] | None = None, catalogue_ttl: float = 15.0) -> None:
        self.library = library
        self.media = media
        self.router = router
        self.audit = audit or (lambda **kw: None)
        self.catalogue_ttl = catalogue_ttl
        self._lock = threading.RLock()
        self._catalogue: tuple[float, dict] | None = None
        self._workflows: tuple[float, dict] | None = None
        media.observers.append(self.observe)

    # ------------------------------------------------------------ database
    def _db(self) -> Any:
        return self.library.connect()

    @staticmethod
    def _rows(con: sqlite3.Connection, sql: str, args: tuple = ()) -> list[dict]:
        return [dict(r) for r in con.execute(sql, args).fetchall()]

    # ------------------------------------------------------------ catalogue
    def _router_get(self, path: str, timeout: float = 30) -> dict:
        try:
            return self.router.get_json(path, timeout=timeout)
        except JobError as exc:
            raise WanError(f"the media router refused {path}: {exc}", 502, exc.code or "router_error") from None
        except Exception as exc:  # noqa: BLE001 - util.HTTPError and socket errors
            raise WanError(ERROR_TEXT["router_unavailable"], 502, "router_unavailable") from exc

    def catalogue(self, *, refresh: bool = False) -> dict:
        with self._lock:
            cached = self._catalogue
            if cached and not refresh and time.time() - cached[0] < self.catalogue_ttl:
                return cached[1]
        data = self._router_get("/v1/loras")
        self._remember(data.get("data") or [])
        with self._lock:
            self._catalogue = (time.time(), data)
        return data

    def rescan(self, *, user: str) -> dict:
        try:
            data = self.router.post_json("/v1/loras/rescan", {}, timeout=300)
        except JobError as exc:
            raise WanError(f"rescan failed: {exc}", 502, exc.code or "router_error") from None
        except Exception as exc:  # noqa: BLE001
            raise WanError(ERROR_TEXT["router_unavailable"], 502, "router_unavailable") from exc
        self._remember(data.get("data") or [])
        with self._lock:
            self._catalogue = (time.time(), data)
        self.audit(user=user, ip="", action="video.loras.rescan", outcome="ok", files=len(data.get("data") or []))
        return self.library_view()

    def _remember(self, files: list[dict]) -> None:
        now = time.time()
        names = {f["name"] for f in files if isinstance(f.get("name"), str)}
        with self._db() as con:
            con.execute("BEGIN IMMEDIATE")
            for f in files:
                if f.get("shadowed_by") or not isinstance(f.get("name"), str):
                    continue
                con.execute(
                    "INSERT INTO wan_lora_files (name, root, size, first_seen, last_seen, missing) "
                    "VALUES (?, ?, ?, ?, ?, 0) ON CONFLICT(name) DO UPDATE SET root=excluded.root, "
                    "size=excluded.size, last_seen=excluded.last_seen, missing=0",
                    (f["name"], str(f.get("root") or ""), int(f.get("size") or 0), now, now))
            for row in con.execute("SELECT name FROM wan_lora_files WHERE missing=0").fetchall():
                if row["name"] not in names:
                    con.execute("UPDATE wan_lora_files SET missing=1 WHERE name=?", (row["name"],))
            con.execute("COMMIT")

    def _state(self) -> tuple[list[dict], set[str], dict[str, dict], dict[str, dict]]:
        with self._db() as con:
            pairs = self._rows(con, "SELECT id, high_name, low_name FROM wan_lora_pairs ORDER BY created_at")
            unpaired = {r["name"] for r in con.execute("SELECT name FROM wan_lora_unpaired")}
            settings = {}
            for r in self._rows(con, "SELECT * FROM wan_lora_settings"):
                r["tags"] = json.loads(r.get("tags") or "[]")
                settings[r["entry_id"]] = r
            seen = {r["name"]: r for r in self._rows(con, "SELECT * FROM wan_lora_files")}
        return pairs, unpaired, settings, seen

    def library_view(self, *, refresh: bool = False) -> dict:
        cat = self.catalogue(refresh=refresh)
        pairs, unpaired, settings, seen = self._state()
        lib = build_library(cat.get("data") or [], pairs, unpaired, settings, seen)
        missing = [{"name": n, "first_seen": r["first_seen"], "last_seen": r["last_seen"]}
                   for n, r in sorted(seen.items()) if r["missing"]]
        return {**lib, "missing_files": missing, "roots": cat.get("roots") or [],
                "scanned_at": cat.get("scanned_at"), "comfy": cat.get("comfy") or {},
                "problems": cat.get("problems") or [], "defaults": self.defaults_public()}

    def entry(self, eid: str) -> dict:
        if not ENTRY_ID.match(eid):
            raise WanError("invalid LoRA entry id")
        for e in self.library_view()["entries"]:
            if e["id"] == eid:
                return e
        raise WanError("no such LoRA entry (the library may have changed; rescan)", 404, "lora_not_found")

    def update_entry(self, eid: str, body: dict, *, user: str) -> dict:
        current = self.entry(eid)
        values: dict[str, Any] = {}
        if "display_name" in body:
            values["display_name"] = _short_text(body["display_name"], "display_name", 120) or None
        if "description" in body:
            values["description"] = _short_text(body["description"], "description", 1000) or None
        if "tags" in body:
            tags = body["tags"]
            if not isinstance(tags, list) or len(tags) > MAX_TAGS:
                raise WanError(f"tags must be a list of at most {MAX_TAGS} words")
            clean = []
            for t in tags:
                t = _short_text(t, "tag", 40)
                if t and t not in clean:
                    clean.append(t)
            values["tags"] = json.dumps(clean)
        for key in ("default_high", "default_low"):
            if key in body:
                values[key] = None if body[key] is None else _strength(body[key], key)
        for key in ("enabled", "allow_unknown"):
            if key in body:
                if not isinstance(body[key], bool):
                    raise WanError(f"{key} must be true or false")
                values[key] = int(body[key])
        if not values:
            raise WanError("nothing to update")
        self._save_settings(eid, values)
        self.audit(user=user, ip="", action="video.loras.update", outcome="ok", entry=eid,
                   fields=sorted(values), name=current["display_name"])
        return self.entry(eid)

    def _save_settings(self, eid: str, values: dict) -> None:
        cols = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        updates = ", ".join(f"{k}=excluded.{k}" for k in values)
        with self._db() as con:
            con.execute(f"INSERT INTO wan_lora_settings (entry_id, {cols}, updated_at) VALUES (?, {marks}, ?) "  # noqa: S608
                        f"ON CONFLICT(entry_id) DO UPDATE SET {updates}, updated_at=excluded.updated_at",
                        (eid, *values.values(), time.time()))

    def reorder(self, ids: object, *, user: str) -> dict:
        if not isinstance(ids, list) or not ids or len(ids) > 2000 \
                or not all(isinstance(i, str) and ENTRY_ID.match(i) for i in ids) or len(set(ids)) != len(ids):
            raise WanError("ids must be a list of distinct LoRA entry ids")
        with self._db() as con:
            con.execute("BEGIN IMMEDIATE")
            now = time.time()
            for pos, eid in enumerate(ids):
                con.execute("INSERT INTO wan_lora_settings (entry_id, position, updated_at) VALUES (?, ?, ?) "
                            "ON CONFLICT(entry_id) DO UPDATE SET position=excluded.position, "
                            "updated_at=excluded.updated_at", (eid, pos, now))
            con.execute("COMMIT")
        self.audit(user=user, ip="", action="video.loras.reorder", outcome="ok", count=len(ids))
        return self.library_view()

    def pair(self, body: dict, *, user: str) -> dict:
        high, low = body.get("high_file"), body.get("low_file")
        if not isinstance(high, str) or not isinstance(low, str) or high == low:
            raise WanError("choose one high-noise file and a different low-noise file")
        files = {f["name"]: f for f in self.catalogue(refresh=True).get("data") or [] if not f.get("shadowed_by")}
        for name, role, other in ((high, "high", "low"), (low, "low", "high")):
            f = files.get(name)
            if f is None:
                raise WanError(f"{name} is not in the LoRA catalogue", 404, "lora_not_found")
            if f.get("noise") == other:
                raise WanError(f"{name} is a {other}-noise file; it cannot be the {role}-noise half of a pair",
                               code="lora_branch_mismatch")
            if f.get("noise") == "general":
                raise WanError(f"{name} is a general LoRA; general LoRAs are applied on their own, not paired",
                               code="lora_branch_mismatch")
        pid = "p_" + secrets.token_hex(8)
        try:
            with self._db() as con:
                con.execute("BEGIN IMMEDIATE")
                con.execute("DELETE FROM wan_lora_unpaired WHERE name IN (?, ?)", (high, low))
                con.execute("INSERT INTO wan_lora_pairs (id, high_name, low_name, created_at, created_by) "
                            "VALUES (?, ?, ?, ?, ?)", (pid, high, low, time.time(), user))
                con.execute("COMMIT")
        except sqlite3.IntegrityError:
            raise WanError("one of these files is already in a manual pair; unpair it first", 409,
                           "pair_conflict") from None
        self.audit(user=user, ip="", action="video.loras.pair", outcome="ok", high=high, low=low)
        return self.entry(entry_id([high, low]))

    def unpair(self, eid: str, *, user: str) -> dict:
        entry = self.entry(eid)
        if entry["kind"] != "pair":
            raise WanError("this LoRA is not paired", 409, "not_paired")
        with self._db() as con:
            con.execute("BEGIN IMMEDIATE")
            if entry["pair_source"] == "manual":
                con.execute("DELETE FROM wan_lora_pairs WHERE id=?", (entry["pair_id"],))
            else:
                for name in (entry["high_file"], entry["low_file"]):
                    con.execute("INSERT OR IGNORE INTO wan_lora_unpaired (name, created_at, created_by) "
                                "VALUES (?, ?, ?)", (name, time.time(), user))
            con.execute("COMMIT")
        self.audit(user=user, ip="", action="video.loras.unpair", outcome="ok", high=entry["high_file"],
                   low=entry["low_file"], source=entry["pair_source"])
        return self.library_view()

    def restore_auto_pairing(self, name: object, *, user: str) -> dict:
        if not isinstance(name, str) or len(name) > 512:
            raise WanError("name must be a LoRA file name")
        with self._db() as con:
            con.execute("DELETE FROM wan_lora_unpaired WHERE name=?", (name,))
        self.audit(user=user, ip="", action="video.loras.autopair", outcome="ok", name=name)
        return self.library_view()

    # ------------------------------------------------------------ config
    def defaults_public(self) -> dict:
        return {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}

    def model_info(self) -> dict:
        with self._lock:
            cached = self._workflows
        if not cached or time.time() - cached[0] > 300:
            try:
                data = self.router.get_json("/v1/workflows", timeout=10)
            except Exception:  # noqa: BLE001 - shown as "unknown" in the UI
                data = {}
            cached = (time.time(), data)
            with self._lock:
                self._workflows = cached
        wf = next((w for w in cached[1].get("data") or [] if w.get("name") == T2V_WORKFLOW), None)
        models = list((wf or {}).get("models") or [])
        return {"id": MODEL_ID, "label": MODEL_LABEL, "workflow": T2V_WORKFLOW,
                "available": wf is not None, "lora_support": bool((wf or {}).get("loras")),
                "high_model": next((m for m in models if "high_noise_14B" in m), None),
                "low_model": next((m for m in models if "low_noise_14B" in m), None),
                "base_loras": [m for m in models if "14B" not in m]}

    def config(self) -> dict:
        return {"defaults": self.defaults_public(),
                "limits": {k: list(v) for k, v in LIMITS.items()},
                "sizes": [{"value": s, "aspect": ASPECT.get(s, "")} for s in VIDEO_SIZES],
                "samplers": list(SAMPLERS), "schedulers": list(SCHEDULERS), "apply": list(APPLY),
                "model": self.model_info(), "max_frames": MAX_FRAMES}

    # ------------------------------------------------------------ presets
    def presets(self) -> list[dict]:
        with self._db() as con:
            rows = self._rows(con, "SELECT * FROM wan_presets ORDER BY builtin DESC, name COLLATE NOCASE")
        return [self._preset_public(r) for r in rows]

    @staticmethod
    def _preset_public(row: dict) -> dict:
        data = json.loads(row["data"])
        return {"id": row["id"], "name": row["name"], "description": row["description"],
                "builtin": bool(row["builtin"]), "created_at": row["created_at"] or None,
                "updated_at": row["updated_at"] or None, "created_by": row["created_by"], "data": data}

    def preset(self, pid: str) -> dict:
        if not PRESET_ID.match(pid or ""):
            raise WanError("invalid preset id")
        with self._db() as con:
            row = con.execute("SELECT * FROM wan_presets WHERE id=?", (pid,)).fetchone()
        if row is None:
            raise WanError("no such preset", 404, "preset_not_found")
        return self._preset_public(dict(row))

    def _preset_data(self, raw: object) -> dict:
        if not isinstance(raw, dict):
            raise WanError("preset data must be an object")
        data: dict[str, Any] = {"version": 1}
        data["loras"] = [self._stack_item(item, i, for_preset=True)
                         for i, item in enumerate(_list(raw.get("loras"), "loras", MAX_STACK * 2))]
        data["size"] = _choice(raw.get("size", DEFAULTS["size"]), VIDEO_SIZES, "size")
        data["seconds"] = _number(raw.get("seconds", DEFAULTS["seconds"]), "seconds", *LIMITS["seconds"])
        data["fps"] = int(_number(raw.get("fps", DEFAULTS["fps"]), "fps", *LIMITS["fps"], integer=True))
        data["frames"] = snap_frames(data["seconds"], data["fps"])
        data["aspect_ratio"] = ASPECT.get(data["size"], "")
        data["seed_mode"] = _choice(raw.get("seed_mode", "random"), ("random", "fixed"), "seed_mode")
        seed = raw.get("seed")
        data["seed"] = None if seed is None else int(_number(seed, "seed", 0, 2**63 - 1, integer=True))
        if data["seed_mode"] == "fixed" and data["seed"] is None:
            raise WanError("a fixed-seed preset needs a seed")
        data["prompt"] = _short_text(raw.get("prompt", ""), "prompt", MAX_PROMPT)
        data["prompt_suffix"] = _short_text(raw.get("prompt_suffix", ""), "prompt_suffix", 1000)
        data["negative_prompt"] = _short_text(raw.get("negative_prompt", DEFAULTS["negative_prompt"]),
                                              "negative_prompt", MAX_PROMPT)
        data["negative_mode"] = _choice(raw.get("negative_mode", "replace"), ("replace", "append"), "negative_mode")
        data["advanced"] = self._advanced(raw.get("advanced"))
        data["model"] = MODEL_ID
        data["workflow_version"] = _short_text(raw.get("workflow_version") or "", "workflow_version", 120) or None
        return data

    def create_preset(self, body: dict, *, user: str) -> dict:
        name = _short_text(body.get("name"), "name", 80)
        if not name:
            raise WanError("a preset needs a name")
        data = self._preset_data(body.get("data"))
        pid = "wp_" + secrets.token_hex(8)
        now = time.time()
        try:
            with self._db() as con:
                con.execute("BEGIN IMMEDIATE")
                if con.execute("SELECT COUNT(*) FROM wan_presets").fetchone()[0] >= MAX_PRESETS:
                    con.execute("ROLLBACK")
                    raise WanError(f"at most {MAX_PRESETS} presets", 409, "too_many_presets")
                con.execute("INSERT INTO wan_presets (id, name, description, builtin, data, created_at, "
                            "updated_at, created_by) VALUES (?, ?, ?, 0, ?, ?, ?, ?)",
                            (pid, name, _short_text(body.get("description", ""), "description", 500),
                             json.dumps(data, sort_keys=True), now, now, user))
                con.execute("COMMIT")
        except sqlite3.IntegrityError:
            raise WanError(f"a preset named {name!r} already exists", 409, "preset_exists") from None
        self.audit(user=user, ip="", action="video.presets.create", outcome="ok", preset=pid, name=name)
        return self.preset(pid)

    def update_preset(self, pid: str, body: dict, *, user: str) -> dict:
        current = self.preset(pid)
        sets: dict[str, Any] = {}
        if "name" in body:
            name = _short_text(body.get("name"), "name", 80)
            if not name:
                raise WanError("a preset needs a name")
            sets["name"] = name
        if "description" in body:
            sets["description"] = _short_text(body.get("description"), "description", 500)
        if "data" in body:
            sets["data"] = json.dumps(self._preset_data(body.get("data")), sort_keys=True)
        if not sets:
            raise WanError("nothing to update")
        sets["updated_at"] = time.time()
        try:
            with self._db() as con:
                con.execute(f"UPDATE wan_presets SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?",  # noqa: S608
                            (*sets.values(), pid))
        except sqlite3.IntegrityError:
            raise WanError(f"a preset named {sets.get('name')!r} already exists", 409, "preset_exists") from None
        self.audit(user=user, ip="", action="video.presets.update", outcome="ok", preset=pid,
                   fields=sorted(k for k in sets if k != "updated_at"), name=current["name"])
        return self.preset(pid)

    def duplicate_preset(self, pid: str, body: dict, *, user: str) -> dict:
        src = self.preset(pid)
        name = _short_text(body.get("name") or f"{src['name']} copy", "name", 80)
        return self.create_preset({"name": name, "description": src["description"], "data": src["data"]},
                                  user=user)

    def delete_preset(self, pid: str, *, user: str) -> dict:
        src = self.preset(pid)
        with self._db() as con:
            con.execute("DELETE FROM wan_presets WHERE id=?", (pid,))
        self.audit(user=user, ip="", action="video.presets.delete", outcome="ok", preset=pid, name=src["name"])
        return {"deleted": pid}

    def resolve_preset(self, pid: str, overrides: dict | None = None) -> dict:
        """A preset plus overrides -> the exact body POST /api/video/generate
        accepts (the Creative Flows contract; see coordination/build-v3/wan.md).

        Overrides may set prompt, negative_prompt, seed, size, seconds, fps,
        title, loras, advanced and flow ids. The prompt is the override (or the
        preset's own prompt) followed by the preset's prompt suffix.
        """
        preset = self.preset(pid)
        data = preset["data"]
        over = overrides or {}
        if not isinstance(over, dict):
            raise WanError("overrides must be an object")
        allowed = {"prompt", "negative_prompt", "seed", "size", "seconds", "fps", "title", "loras", "advanced",
                   "flow_id", "flow_run_id", "flow_node_id"}
        unknown = set(over) - allowed
        if unknown:
            raise WanError(f"unknown override(s): {', '.join(sorted(unknown))}")
        base_prompt = over.get("prompt") if over.get("prompt") not in (None, "") else data.get("prompt", "")
        prompt = ", ".join(p for p in (str(base_prompt or "").strip(), data.get("prompt_suffix", "")) if p)
        negative = data.get("negative_prompt", "")
        if over.get("negative_prompt") not in (None, ""):
            neg = str(over["negative_prompt"])
            negative = f"{negative}, {neg}" if data.get("negative_mode") == "append" and negative else neg
        seed = over.get("seed")
        if seed is None and data.get("seed_mode") == "fixed":
            seed = data.get("seed")
        body: dict[str, Any] = {
            "prompt": prompt, "negative_prompt": negative, "seed": seed,
            "size": over.get("size", data["size"]), "seconds": over.get("seconds", data["seconds"]),
            "fps": over.get("fps", data["fps"]), "loras": over.get("loras", data["loras"]),
            "advanced": {**data.get("advanced", {}), **(over.get("advanced") or {})},
            "preset_id": pid,
        }
        for key in ("title", "flow_id", "flow_run_id", "flow_node_id"):
            if over.get(key) is not None:
                body[key] = over[key]
        warnings: list[str] = []
        if not str(base_prompt or "").strip():
            warnings.append("no prompt: supply overrides.prompt (this preset only adds style wording)")
        try:
            self.prepare(body)
        except WanError as exc:
            warnings.append(f"{exc} ({exc.code})")
        return {"preset": {"id": pid, "name": preset["name"]}, "body": body, "valid": not warnings,
                "warnings": warnings}

    # ------------------------------------------------------------ requests
    def _advanced(self, raw: object) -> dict:
        raw = raw or {}
        if not isinstance(raw, dict) or not set(raw) <= set(DEFAULTS["advanced"]):
            raise WanError("advanced accepts shift, cfg, steps, boundary, sampler_name and scheduler")
        d = DEFAULTS["advanced"]
        steps = int(_number(raw.get("steps", d["steps"]), "steps", *LIMITS["steps"], integer=True))
        boundary_raw = raw.get("boundary")
        boundary = steps // 2 if boundary_raw is None else int(_number(boundary_raw, "boundary", 1, 39, integer=True))
        if boundary >= steps:
            raise WanError("the expert switch step (boundary) must be lower than the step count")
        return {"shift": _number(raw.get("shift", d["shift"]), "shift", *LIMITS["shift"]),
                "cfg": _number(raw.get("cfg", d["cfg"]), "cfg", *LIMITS["cfg"]),
                "steps": steps, "boundary": boundary,
                "sampler_name": _choice(raw.get("sampler_name", d["sampler_name"]), SAMPLERS, "sampler_name"),
                "scheduler": _choice(raw.get("scheduler", d["scheduler"]), SCHEDULERS, "scheduler")}

    def _stack_item(self, item: object, index: int, *, for_preset: bool = False) -> dict:
        where = f"loras[{index}]"
        if not isinstance(item, dict):
            raise WanError(f"{where} must be an object")
        extra = set(item) - {"entry_id", "enabled", "strength_high", "strength_low", "apply", "high_file",
                             "low_file", "file", "display_name"}
        if extra:
            raise WanError(f"{where}: unknown field(s) {', '.join(sorted(extra))}")
        eid = item.get("entry_id")
        if not isinstance(eid, str) or not ENTRY_ID.match(eid):
            raise WanError(f"{where}: entry_id must be a LoRA library id", code="lora_not_found")
        enabled = item.get("enabled", True)
        if not isinstance(enabled, bool):
            raise WanError(f"{where}: enabled must be true or false")
        apply = item.get("apply")
        if apply is not None and apply not in APPLY:
            raise WanError(f"{where}: apply must be one of {', '.join(APPLY)}")
        out = {"entry_id": eid, "enabled": enabled, "apply": apply,
               "strength_high": None if item.get("strength_high") is None
               else _strength(item["strength_high"], f"{where}.strength_high"),
               "strength_low": None if item.get("strength_low") is None
               else _strength(item["strength_low"], f"{where}.strength_low")}
        if for_preset:
            for key in ("high_file", "low_file", "file", "display_name"):
                if item.get(key) is not None:
                    out[key] = _short_text(item[key], key, 512)
        return out

    def _resolve_entry(self, item: dict, entries: dict[str, dict]) -> dict:
        entry = entries.get(item["entry_id"])
        if entry is None and (item.get("high_file") or item.get("low_file") or item.get("file")):
            # a preset saved before the library changed: find the same files
            for e in entries.values():
                if (e["high_file"], e["low_file"], e["file"]) == (item.get("high_file"), item.get("low_file"),
                                                                  item.get("file")):
                    return e
        if entry is None:
            name = item.get("display_name") or item["entry_id"]
            raise WanError(f"LoRA {name} is no longer in the library (rescan or remove it)", code="lora_not_found")
        return entry

    def prepare(self, body: dict) -> tuple[dict, dict]:
        """Validate a generate request -> (MediaJobs body, Wan extras)."""
        if not isinstance(body, dict):
            raise WanError("request body must be an object")
        prompt = _short_text(body.get("prompt"), "prompt", MAX_PROMPT)
        if not prompt:
            raise WanError("write a prompt first", code="prompt_required")
        negative = _short_text(body.get("negative_prompt", DEFAULTS["negative_prompt"]), "negative_prompt",
                               MAX_PROMPT)
        size = _choice(body.get("size", DEFAULTS["size"]), VIDEO_SIZES, "size")
        seconds = _number(body.get("seconds", DEFAULTS["seconds"]), "seconds", *LIMITS["seconds"])
        fps = int(_number(body.get("fps", DEFAULTS["fps"]), "fps", *LIMITS["fps"], integer=True))
        seed_raw = body.get("seed")
        seed = secrets.randbelow(2**31 - 1) if seed_raw in (None, "") \
            else int(_number(seed_raw, "seed", 0, 2**63 - 1, integer=True))
        title = _short_text(body.get("title", ""), "title", 200) or None
        preset_id = body.get("preset_id")
        if preset_id is not None and (not isinstance(preset_id, str) or not PRESET_ID.match(preset_id)):
            raise WanError("preset_id is not a preset id")
        flow = {}
        for key in ("flow_id", "flow_run_id", "flow_node_id"):
            value = body.get(key)
            if value is not None:
                if not isinstance(value, str) or not FLOW_REF.match(value):
                    raise WanError(f"{key} is not a valid id")
                flow[key] = value
        advanced = self._advanced(body.get("advanced"))
        items = [self._stack_item(it, i, for_preset=True)
                 for i, it in enumerate(_list(body.get("loras"), "loras", MAX_STACK * 2))]
        router_loras, record = self._chains(items)
        width, _, height = size.partition("x")
        request = {"prompt": prompt, "negative_prompt": negative, "size": size, "seconds": seconds, "fps": fps,
                   "seed": seed, "title": title, "preset_id": preset_id, "advanced": advanced,
                   "loras": [{k: v for k, v in r.items() if k in ("entry_id", "enabled", "strength_high",
                                                                 "strength_low", "apply", "high_file",
                                                                 "low_file", "file", "display_name")}
                             for r in record], **flow}
        media_body = {"kind": "t2v", "prompt": prompt, "negative_prompt": negative, "size": size,
                      "seconds": seconds, "fps": fps, "seed": seed, "title": title}
        wan = {"workflow": T2V_WORKFLOW, "model": MODEL_ID, "preset_id": preset_id, "loras": record,
               "advanced": advanced, "flow": flow, "request": request,
               "frames": snap_frames(seconds, fps), "width": int(width), "height": int(height),
               "router": {"loras": router_loras, **advanced}}
        return media_body, wan

    def _chains(self, items: list[dict]) -> tuple[dict, list[dict]]:
        """Stack items -> router chains {high, low} and the history record."""
        if not items:
            return {"high": [], "low": []}, []
        entries = {e["id"]: e for e in self.library_view()["entries"]}
        high: list[dict] = []
        low: list[dict] = []
        record: list[dict] = []
        for order, item in enumerate(items):
            try:
                entry = self._resolve_entry(item, entries)
            except WanError:
                if item["enabled"]:
                    raise
                # a disabled item whose files are gone is kept in the record, never applied
                record.append({"order": order, "entry_id": item["entry_id"],
                               "display_name": item.get("display_name") or item["entry_id"], "kind": None,
                               "high_file": item.get("high_file"), "low_file": item.get("low_file"),
                               "file": item.get("file"), "apply": item["apply"], "enabled": False,
                               "strength_high": item["strength_high"], "strength_low": item["strength_low"],
                               "compatibility": None, "missing": True})
                continue
            apply = item["apply"] or (entry["apply_options"][0] if len(entry["apply_options"]) == 1 else None)
            sh = item["strength_high"] if item["strength_high"] is not None else (
                entry["default_high"] if entry["default_high"] is not None else DEFAULTS["strength_high"])
            sl = item["strength_low"] if item["strength_low"] is not None else (
                entry["default_low"] if entry["default_low"] is not None else DEFAULTS["strength_low"])
            rec = {"order": order, "entry_id": entry["id"], "display_name": entry["display_name"],
                   "kind": entry["kind"], "high_file": entry["high_file"], "low_file": entry["low_file"],
                   "file": entry["file"], "apply": apply, "enabled": item["enabled"],
                   "strength_high": sh if apply in ("pair", "high", "both") else None,
                   "strength_low": sl if apply in ("pair", "low", "both") else None,
                   "compatibility": entry["compatibility"]}
            record.append(rec)
            if not item["enabled"]:
                continue
            self._check_entry(entry, apply)
            flags = {"allow_unknown": True} if entry["compatibility"] == "unknown" else {}
            if apply == "pair":
                high.append({"name": entry["high_file"], "strength": sh, **flags})
                low.append({"name": entry["low_file"], "strength": sl, **flags})
            elif apply == "high":
                high.append({"name": entry["high_file"] or entry["file"], "strength": sh, **flags})
            elif apply == "low":
                low.append({"name": entry["low_file"] or entry["file"], "strength": sl, **flags})
            else:
                high.append({"name": entry["file"], "strength": sh, "shared": True, **flags})
                low.append({"name": entry["file"], "strength": sl, "shared": True, **flags})
        for branch, chain in (("high", high), ("low", low)):
            names = [c["name"] for c in chain]
            if len(set(names)) != len(names):
                raise WanError(f"the {branch}-noise branch uses the same LoRA twice", code="lora_duplicate")
            if len(chain) > MAX_STACK:
                raise WanError(f"at most {MAX_STACK} LoRAs on the {branch}-noise branch", code="lora_too_many")
        return {"high": high, "low": low}, record

    @staticmethod
    def _check_entry(entry: dict, apply: str | None) -> None:
        name = entry["display_name"]
        if not entry["enabled"]:
            raise WanError(f"LoRA {name} is disabled in the library", code="lora_disabled")
        if entry["pair_state"] == "broken_high" and apply in ("pair", "high"):
            raise WanError(f"LoRA {name}: the high-noise file {entry['high_file']} is missing",
                           code="lora_high_missing")
        if entry["pair_state"] == "broken_low" and apply in ("pair", "low"):
            raise WanError(f"LoRA {name}: the low-noise file {entry['low_file']} is missing",
                           code="lora_low_missing")
        if apply is None:
            raise WanError(f"LoRA {name}: choose whether it applies to high noise, low noise or both",
                           code="lora_apply_required")
        if apply not in entry["apply_options"]:
            raise WanError(f"LoRA {name} cannot be applied as '{apply}' (allowed: "
                           f"{', '.join(entry['apply_options'])})", code="lora_branch_mismatch")
        for f in entry["files"]:
            if not f.get("valid"):
                raise WanError(f"LoRA {name}: {f['name']} is not a valid safetensors file ({f.get('error')})",
                               code="lora_invalid_file")
            if f.get("comfy_visible") is False:
                raise WanError(f"LoRA {name}: ComfyUI does not list {f['name']} yet; rescan",
                               code="lora_not_visible")
        if entry["compatibility"] == "incompatible":
            reason = "; ".join(p for p in entry["problems"] if "incompatible" in p) or "incompatible"
            raise WanError(f"LoRA {name} is not compatible with Wan 2.2 T2V-A14B: {reason}",
                           code="lora_incompatible")
        if entry["compatibility"] == "unknown" and not entry["allow_unknown"]:
            raise WanError(f"LoRA {name} has unknown compatibility; allow it in the LoRA library to use it",
                           code="lora_unknown_compatibility")

    def preview(self, body: dict) -> dict:
        media_body, wan = self.prepare(body)
        payload = {k: v for k, v in media_body.items() if k not in ("kind", "title") and v is not None}
        payload.update(wan["router"])
        try:
            built = self.router.post_json("/v1/videos/workflow", payload, timeout=60)
        except JobError as exc:
            raise WanError(human_error(exc.code, str(exc)), exc.status if exc.status < 500 else 502,
                           exc.code or "router_error") from None
        except Exception as exc:  # noqa: BLE001
            raise WanError(ERROR_TEXT["router_unavailable"], 502, "router_unavailable") from exc
        graph = sanitize_graph(built.get("graph") or {})
        return {"workflow": T2V_WORKFLOW, "workflow_version": built.get("workflow_version"),
                "seed": media_body["seed"], "loras": wan["loras"], "chains": summarize_chains(graph),
                "frames": wan["frames"], "graph": graph}

    def generate(self, body: dict, *, user: str, ip: str = "") -> dict:
        media_body, wan = self.prepare(body)
        job = self.media.submit(media_body, user=user, ip=ip, wan=wan)
        self.audit(user=user, ip=ip, action="video.generate", outcome="queued", job=job["id"],
                   loras=[r["display_name"] for r in wan["loras"] if r["enabled"]], preset=wan["preset_id"])
        return job

    # ------------------------------------------------------------ history
    @staticmethod
    def _plain_video(job: MediaJob) -> dict:
        """History fields for a video job submitted without the Wan extras
        (image to video, video edit, or text to video from another page)."""
        p = job.params
        width, _, height = str(p.get("size") or "x").partition("x")
        seconds = float(p.get("seconds") or 0) or None
        fps = p.get("fps")
        frames = snap_frames(seconds, fps) if seconds and fps else None
        model = {"t2v": MODEL_ID, "i2v": "wan22-i2v-a14b", "v2v": "wan22-v2v-a14b"}[job.kind]
        request = {k: v for k, v in p.items() if k != "wan"}
        return {"workflow": None, "model": model, "preset_id": None, "loras": [], "advanced": {}, "flow": {},
                "request": request, "frames": frames, "width": int(width) if width.isdigit() else None,
                "height": int(height) if height.isdigit() else None}

    def observe(self, job: MediaJob, event: str) -> None:
        if job.kind not in ("t2v", "i2v", "v2v"):
            return
        wan = job.params.get("wan")
        if not isinstance(wan, dict):
            wan = self._plain_video(job)
        now = time.time()
        if event in ("failed", "cancelled") and job.error_hint is None:
            code = job.error_code or ("cancelled" if event == "cancelled" else None)
            if code in ERROR_TEXT:
                job.error_hint = ERROR_TEXT[code]
        if event == "submitted":
            p = job.params
            with self._db() as con:
                con.execute(
                    "INSERT OR IGNORE INTO wan_generations (id, created_at, updated_at, user, status, prompt, "
                    "negative_prompt, seed, model, loras, width, height, frames, fps, seconds, settings, request, "
                    "preset_id, title, flow_id, flow_run_id, flow_node_id) "
                    "VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (job.id, job.created, now, job.user, p.get("prompt") or "", p.get("negative_prompt"),
                     p.get("seed"), wan["model"], json.dumps(wan["loras"]), wan["width"], wan["height"],
                     wan["frames"], p.get("fps"), p.get("seconds"), json.dumps(wan["advanced"]),
                     json.dumps(wan["request"]), wan.get("preset_id"), p.get("title"),
                     wan["flow"].get("flow_id"), wan["flow"].get("flow_run_id"), wan["flow"].get("flow_node_id")))
            return
        sets: dict[str, Any] = {"status": job.phase, "updated_at": now, "router_job": job.router_job,
                                "started_at": job.started}
        if event in ("ready", "failed", "cancelled"):
            sets.update(self._final_fields(job, event))
        with self._db() as con:
            con.execute(f"UPDATE wan_generations SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?",  # noqa: S608
                        (*sets.values(), job.id))

    def _final_fields(self, job: MediaJob, event: str) -> dict:
        out: dict[str, Any] = {"finished_at": job.ended or time.time(),
                               "duration_seconds": job.elapsed_generation}
        if job.router_job:
            try:
                wf = self.router.get_json(f"/v1/videos/{job.router_job}/workflow", timeout=15)
            except Exception as exc:  # noqa: BLE001 - the job itself is not affected
                log.info("no workflow for %s: %s", job.id, redact(str(exc))[:200])
            else:
                graph = sanitize_graph(wf.get("graph") or {})
                chains = summarize_chains(graph)
                out.update(workflow_json=json.dumps(graph, sort_keys=True), workflow_version=wf.get("workflow_version"),
                           comfy_prompt_id=wf.get("comfy_prompt_id"), chains=json.dumps(chains),
                           high_model=chains.get("high_model"), low_model=chains.get("low_model"))
        if event == "ready" and job.assets:
            asset_id = job.assets[0]
            out["asset_id"] = asset_id
            try:
                asset = self.library.get(asset_id)
                out["output_path"] = f"videos/{asset['filename']}"
                wan = job.params.get("wan") or {}
                self.library.update_settings(asset_id, wan={
                    "generation_id": job.id, "loras": wan.get("loras"), "advanced": wan.get("advanced"),
                    "preset_id": wan.get("preset_id"), "workflow_version": out.get("workflow_version"),
                    "comfy_prompt_id": out.get("comfy_prompt_id"), "chains": json.loads(out.get("chains") or "{}"),
                    "flow": wan.get("flow") or None})
            except LibraryError as exc:
                log.warning("asset %s for %s: %s", asset_id, job.id, exc)
        if event != "ready":
            code = job.error_code or ("cancelled" if event == "cancelled" else None)
            out["error_code"] = code
            out["error_message"] = human_error(code, job.error)
            out["error_detail"] = redact(job.error or job.detail or "")[:4000]
        return out

    def _gen_public(self, row: dict, *, full: bool = False) -> dict:
        d = dict(row)
        for key, default in (("loras", []), ("chains", {}), ("settings", {}), ("request", {})):
            try:
                d[key] = json.loads(d.get(key) or "null") or default
            except ValueError:
                d[key] = default
        wf = d.pop("workflow_json", None)
        d["has_workflow"] = bool(wf)
        if full and wf:
            d["workflow"] = json.loads(wf)
        d["size"] = f"{d['width']}x{d['height']}" if d.get("width") else None
        if d.get("asset_id"):
            d["asset_url"] = f"/api/media/assets/{d['asset_id']}/file"
            d["has_thumbnail"] = self.library.thumb_path(d["asset_id"]).is_file()
            d["thumbnail_url"] = f"/api/media/assets/{d['asset_id']}/thumbnail" if d["has_thumbnail"] else None
            d["flows_url"] = f"#/flows?asset={d['asset_id']}"
        d["workflow_url"] = f"/api/video/generations/{d['id']}/workflow" if wf else None
        live = None
        try:
            live = self.media.get(d["id"])
        except JobError:
            pass
        if live is not None and d["status"] not in ("ready", "failed", "cancelled"):
            d["live"] = {"phase": live.get("phase"), "detail": live.get("detail"), "waiting": live.get("waiting"),
                         "queue_position": live.get("queue_position")}
        return d

    def generations(self, *, q: str = "", status: str = "", limit: int = 30, offset: int = 0) -> dict:
        where, args = [], []
        if q:
            if len(q) > 200:
                raise WanError("search text is too long")
            like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            where.append("(prompt LIKE ? ESCAPE '\\' OR title LIKE ? ESCAPE '\\' OR loras LIKE ? ESCAPE '\\')")
            args += [like, like, like]
        if status:
            if status not in ("queued", "waiting", "generating", "saving", "ready", "failed", "cancelled", "active"):
                raise WanError("unknown status filter")
            if status == "active":
                where.append("status NOT IN ('ready', 'failed', 'cancelled')")
            else:
                where.append("status=?")
                args.append(status)
        limit = max(1, min(100, int(limit)))
        offset = max(0, int(offset))
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        with self._db() as con:
            total = con.execute(f"SELECT COUNT(*) FROM wan_generations {clause}", args).fetchone()[0]  # noqa: S608
            rows = self._rows(con, f"SELECT * FROM wan_generations {clause} ORDER BY created_at DESC "  # noqa: S608
                                   "LIMIT ? OFFSET ?", (*args, limit, offset))
        return {"total": total, "items": [self._gen_public(r) for r in rows], "limit": limit, "offset": offset}

    def generation(self, gid: str) -> dict:
        if not GEN_ID.match(gid or ""):
            raise WanError("invalid generation id")
        with self._db() as con:
            row = con.execute("SELECT * FROM wan_generations WHERE id=?", (gid,)).fetchone()
        if row is None:
            raise WanError("no such video generation", 404, "not_found")
        out = self._gen_public(dict(row), full=True)
        if out.get("asset_id"):
            try:
                out["asset"] = self.library.get(out["asset_id"])
            except LibraryError:
                out["asset"] = None
        return out

    def workflow_export(self, gid: str) -> tuple[bytes, str]:
        gen = self.generation(gid)
        if not gen.get("workflow"):
            raise WanError("this generation has no stored workflow (it never reached the router)", 404,
                           "not_found")
        doc = {"format": "comfyui-api-prompt", "generator": "GX-Playground Wan 2.2 LoRA workflow",
               "workflow_version": gen.get("workflow_version"), "generation_id": gid,
               "comfy_prompt_id": gen.get("comfy_prompt_id"), "prompt": sanitize_graph(gen["workflow"])}
        name = f"gx-wan22-{gid}-workflow.json"
        return (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode(), name

    def errors(self, limit: int = 50) -> dict:
        limit = max(1, min(200, int(limit)))
        with self._db() as con:
            rows = self._rows(con, "SELECT id, created_at, finished_at, prompt, status, error_code, error_message, "
                                   "error_detail, loras, router_job, comfy_prompt_id FROM wan_generations "
                                   "WHERE status IN ('failed', 'cancelled') ORDER BY created_at DESC LIMIT ?",
                              (limit,))
        for r in rows:
            r["loras"] = json.loads(r.get("loras") or "[]")
            r["prompt"] = (r.get("prompt") or "")[:200]
        return {"items": rows}


# ================================================================ helpers
def _short_text(value: object, field: str, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or "\x00" in value:
        raise WanError(f"{field} must be text")
    value = value.strip()
    if len(value) > limit:
        raise WanError(f"{field} is longer than {limit} characters")
    return value


def _number(value: object, field: str, lo: float, hi: float, *, integer: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WanError(f"{field} must be a number")
    if integer and isinstance(value, float) and not value.is_integer():
        raise WanError(f"{field} must be a whole number")
    number = float(value)
    if not lo <= number <= hi:
        raise WanError(f"{field} must be between {lo:g} and {hi:g}")
    return int(number) if integer else number


def _strength(value: object, field: str) -> float:
    return round(float(_number(value, field, DEFAULTS["strength_min"], DEFAULTS["strength_max"])), 4)


def _choice(value: object, allowed: tuple[str, ...], field: str) -> str:
    if value not in allowed:
        raise WanError(f"{field} must be one of {', '.join(allowed)}")
    return str(value)


def _list(value: object, field: str, limit: int) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        raise WanError(f"{field} must be a list")
    if len(value) > limit:
        raise WanError(f"{field} has more than {limit} items", code="lora_too_many")
    return value
