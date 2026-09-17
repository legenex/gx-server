"""gx-voice on gx10-01 (D-040 VOI): saved voices, voice jobs and takes.

    browser / API client / Creative Flows
      -> gx10-01 (session, gateway key or in-process)      this module (App.voice)
      -> 192.168.100.11:18830 (RoCE fabric, bearer key from secrets/gx-voice/api-key)
      -> Qwen3-TTS 12Hz 1.7B on gx10-02

The Control Center owns the voices (application database, migration 040):
a preset voice is a speaker plus style defaults; a designed or cloned voice
is a reference clip in the Media Library plus its transcript. Every change is
pushed to gx10-02's replica (so the OpenAI speech endpoint can resolve saved
voices by name) and re-pushed by a reconcile loop if gx10-02 lost it.

Cloning needs an explicit permission statement. It is stored in
``voice_consents`` with who confirmed it, from where, and the SHA-256 of the
exact recording.

A worker follows every submitted job; when gx10-02 reports it complete it
downloads each take (WAV and MP3), verifies the SHA-256 the supervisor
reported, and records the take. Takes are saved to the Library on request
(or automatically with ``auto_save``). Nothing here returns a node-2 path,
the node-2 key or an engine URL.
"""

from __future__ import annotations

import builtins
import hashlib
import json
import logging
import re
import secrets
import shutil
import threading
import time
import urllib.parse
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .media_library import LibraryError, MediaLibrary, NewAsset
from .redact import redact
from .util import HTTPError, http

log = logging.getLogger("gx.ui.voice")

ALIAS = "gx-voice"
VOICE_ID = re.compile(r"^vc_[0-9a-f]{24}$")
PRESET_ID = re.compile(r"^preset:([a-z_]{2,16})$")
JOB_ID = re.compile(r"^vj_[0-9a-f]{32}$")
NODE_JOB_ID = re.compile(r"^vox-[0-9a-f]{32}$")
NODE_REF_ID = re.compile(r"^ref-[0-9a-f]{32}$")
ASSET_ID = re.compile(r"^a_[0-9a-f]{24}$")
FLOW_ID = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
OPERATIONS = ("tts", "voice_design", "voice_clone", "dialogue")
LIBRARY_OPERATION = {"tts": "tts", "dialogue": "tts", "voice_design": "voice_design", "voice_clone": "voice_clone"}
#: Human labels for the Logs feed when a job has no title.
OPERATION_LABEL = {"tts": "Speech", "dialogue": "Dialogue", "voice_design": "Voice design",
                   "voice_clone": "Voice clone"}
TERMINAL = ("completed", "failed", "cancelled")
LANGUAGES = ("auto", "english", "chinese", "german", "french", "spanish", "italian", "portuguese",
             "russian", "japanese", "korean")
#: The nine CustomVoice speakers (model card of Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice).
PRESETS = {
    "aiden": ("Aiden", "Sunny American male voice with a clear midrange.", "english"),
    "ryan": ("Ryan", "Dynamic male voice with strong rhythmic drive.", "english"),
    "vivian": ("Vivian", "Bright, slightly edgy young female voice.", "chinese"),
    "serena": ("Serena", "Warm, gentle young female voice.", "chinese"),
    "uncle_fu": ("Uncle Fu", "Seasoned male voice with a low, mellow timbre.", "chinese"),
    "dylan": ("Dylan", "Youthful Beijing male voice with a clear, natural timbre.", "chinese (Beijing dialect)"),
    "eric": ("Eric", "Lively Chengdu male voice with a slightly husky brightness.", "chinese (Sichuan dialect)"),
    "ono_anna": ("Ono Anna", "Playful Japanese female voice with a light, nimble timbre.", "japanese"),
    "sohee": ("Sohee", "Warm Korean female voice with rich emotion.", "korean"),
}
VARIANT_MODEL = {
    "custom": ("Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice", "0c0e3051f131929182e2c023b9537f8b1c68adfe"),
    "design": ("Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign", "5ecdb67327fd37bb2e042aab12ff7391903235d3"),
    "base": ("Qwen/Qwen3-TTS-12Hz-1.7B-Base", "fd4b254389122332181a7c3db7f27e918eec64e3"),
}
STYLE_KEYS = {"speed": (0.5, 2.0, float), "pause_ms": (0, 5000, int), "temperature": (0.1, 2.0, float),
              "top_p": (0.05, 1.0, float), "top_k": (1, 200, int), "repetition_penalty": (1.0, 2.0, float)}
SAMPLING_KEYS = ("temperature", "top_p", "top_k", "repetition_penalty")
AUDIO_TYPES = {"audio/wav": "wav", "audio/x-wav": "wav", "audio/wave": "wav", "audio/flac": "flac",
               "audio/x-flac": "flac", "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/ogg": "ogg",
               "audio/mp4": "m4a", "audio/x-m4a": "m4a", "audio/webm": "ogg"}
TAKE_FORMATS = ("wav", "mp3")
MAX_TEXT = 10000
MAX_SEGMENTS = 60
DEFAULT_CONSENT = ("I confirm that I own this recording or have the speaker's permission to clone "
                   "their voice, and that I will not use the clone to deceive or impersonate anyone.")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class VoiceError(Exception):
    def __init__(self, message: str, status: int = 400, code: str = "voice_error") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


# ------------------------------------------------------------------ client
class VoiceClient:
    """Fabric client for the node-2 supervisor. The key is read per call."""

    def __init__(self, base: str, key_file: Path) -> None:
        self.base = base.rstrip("/")
        self.key_file = Path(key_file)

    def _key(self) -> str:
        try:
            key = self.key_file.read_text(encoding="utf-8").strip()
        except OSError:
            raise VoiceError("gx-voice is not configured on gx10-01 (API key file missing)", 503,
                             "not_configured") from None
        if len(key) < 32:
            raise VoiceError("gx-voice API key on gx10-01 is invalid", 503, "not_configured")
        return key

    def call(self, method: str, path: str, *, body: Any = None, raw: bytes | None = None,
             headers: dict[str, str] | None = None, timeout: float = 30) -> Any:
        hdrs = {"Authorization": f"Bearer {self._key()}", **(headers or {})}
        try:
            res = http(method, self.base + path, body=body, raw_body=raw, headers=hdrs, timeout=timeout)
        except HTTPError:
            raise VoiceError("the voice service on gx10-02 is not reachable", 503, "node_unavailable") from None
        try:
            data = res.json()
        except ValueError:
            data = None
        if 200 <= res.status < 300:
            return data
        err = (data or {}).get("error") if isinstance(data, dict) else None
        message = err.get("message") if isinstance(err, dict) else f"HTTP {res.status}"
        code = err.get("code") if isinstance(err, dict) else "upstream_error"
        status = res.status if res.status in (400, 404, 409, 413, 422, 503) else 502
        raise VoiceError(redact(str(message))[:400], status, str(code)[:40])

    def download(self, path: str, dest: Path, timeout: float = 300) -> int:
        import urllib.request

        req = urllib.request.Request(self.base + path, headers={"Authorization": f"Bearer {self._key()}"})
        total = 0
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp, dest.open("wb") as fh:
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    fh.write(chunk)
                    total += len(chunk)
        except OSError as exc:
            dest.unlink(missing_ok=True)
            raise VoiceError(f"downloading the audio from gx10-02 failed: {type(exc).__name__}", 502,
                             "download_failed") from None
        return total

    def health(self) -> dict:
        try:
            res = http("GET", self.base + "/health", timeout=4)
            return {"ok": res.status == 200, **(res.json() if res.status == 200 else {})}
        except (HTTPError, ValueError):
            return {"ok": False}


# --------------------------------------------------------------- helpers
def _text(value: Any, field: str, *, max_len: int, required: bool = True, single_line: bool = False) -> str:
    if value is None or value == "":
        if required:
            raise VoiceError(f"{field} is required")
        return ""
    if not isinstance(value, str):
        raise VoiceError(f"{field} must be text")
    text = _CTRL.sub("", value.replace("\r\n", "\n"))
    if single_line:
        text = " ".join(text.split())
    text = text.strip()
    if required and not text:
        raise VoiceError(f"{field} is empty")
    if len(text) > max_len:
        raise VoiceError(f"{field} is longer than {max_len} characters")
    return text


def _num(value: Any, field: str, lo: float, hi: float, typ: type) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        raise VoiceError(f"{field} must be a number")
    if typ is int and not float(value).is_integer():
        raise VoiceError(f"{field} must be a whole number")
    if not lo <= value <= hi:
        raise VoiceError(f"{field} must be between {lo} and {hi}")
    return typ(value)


def _language(value: Any) -> str:
    if value in (None, ""):
        return "auto"
    if not isinstance(value, str) or value.lower() not in LANGUAGES:
        raise VoiceError(f"language must be one of: {', '.join(LANGUAGES)}")
    return value.lower()


def _style(value: Any) -> dict:
    if value in (None, {}):
        return {}
    if not isinstance(value, dict):
        raise VoiceError("style must be an object")
    bad = sorted(set(value) - set(STYLE_KEYS))
    if bad:
        raise VoiceError(f"unsupported style field(s): {', '.join(bad)}")
    return {k: _num(value[k], f"style.{k}", *STYLE_KEYS[k]) for k in value if value[k] is not None}


def _flow(value: Any) -> dict | None:
    if value in (None, {}):
        return None
    if not isinstance(value, dict) or set(value) - {"flow_id", "flow_run_id", "flow_node_id"}:
        raise VoiceError("flow must be {flow_id, flow_run_id, flow_node_id}")
    out = {}
    for k in ("flow_id", "flow_run_id", "flow_node_id"):
        if value.get(k) is not None:
            if not isinstance(value[k], str) or not FLOW_ID.match(value[k]):
                raise VoiceError(f"flow.{k} is invalid")
            out[k] = value[k]
    return out or None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(4 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def preset_view(speaker: str) -> dict:
    label, desc, lang = PRESETS[speaker]
    return {"id": f"preset:{speaker}", "name": label, "kind": "preset", "builtin": True, "speaker": speaker,
            "description": desc, "native_language": lang, "instructions": "", "language": "auto",
            "style": {}, "reference_asset_id": None, "version": 1, "model_repo": VARIANT_MODEL["custom"][0],
            "model_revision": VARIANT_MODEL["custom"][1], "created_at": None}


# ---------------------------------------------------------------- studio
class VoiceStudio:
    def __init__(self, client: VoiceClient, library: MediaLibrary, takes_root: Path, *,
                 audit: Callable[..., None] | None = None, results=None,
                 explain: Callable[[str], dict | None] | None = None,
                 poll_interval: float = 1.5, start_worker: bool = True) -> None:
        self.client = client
        self.library = library
        self.takes_root = Path(takes_root)
        self.audit = audit if audit is not None else (lambda **kw: None)
        self.results = results
        self.explain = explain if explain is not None else (lambda alias: None)
        self.poll_interval = poll_interval
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._cache: dict[str, tuple[float, Any]] = {}
        self._last_reconcile = 0.0
        self.last_error: str | None = None
        self.takes_root.mkdir(parents=True, exist_ok=True, mode=0o750)
        if start_worker:
            threading.Thread(target=self._loop, name="voice-jobs", daemon=True).start()

    # ------------------------------------------------------------ model
    def model(self) -> dict:
        now = time.time()
        hit = self._cache.get("model")
        if hit and now - hit[0] < 5:
            return hit[1]
        info = self.client.call("GET", "/v1/voice/model")
        health = self.client.health()
        info["state"] = health.get("state", "unknown")
        info["health"] = {k: health.get(k) for k in ("state", "busy", "queue", "waiting", "blocked_by",
                                                     "variants_loaded", "memory", "idle_seconds",
                                                     "idle_unload_after_s", "pinned")}
        self._cache["model"] = (now, info)
        return info

    def lifecycle(self, op: str, *, user: str, if_idle: bool = False, variant: str | None = None) -> dict:
        if op not in ("load", "unload"):
            raise VoiceError("unknown operation", 404, "not_found")
        if variant is not None and variant not in VARIANT_MODEL:
            raise VoiceError("variant must be custom, design or base")
        body: dict = {}
        if op == "unload" and if_idle:
            body["if_idle"] = True
        if op == "load" and variant:
            body["variant"] = variant
        result = self.client.call("POST", f"/v1/voice/{op}", body=body, timeout=900 if op == "load" else 120)
        self._cache.pop("model", None)
        self.audit(user=user, ip="", action=f"voice.{op}", outcome="ok", if_idle=if_idle, variant=variant)
        return result

    # ----------------------------------------------------------- voices
    def _row_voice(self, r: Any) -> dict:
        d = dict(r)
        style = json.loads(d.pop("style_json") or "{}")
        meta = json.loads(d.pop("metadata_json") or "{}")
        out = {"id": d["id"], "name": d["name"], "kind": d["kind"], "builtin": False,
               "description": d["description"], "speaker": d["speaker"], "instructions": d["instructions"],
               "language": d["language"], "style": style, "metadata": meta,
               "reference_asset_id": d["reference_asset_id"], "reference_text": d["reference_text"],
               "x_vector_only": bool(d["x_vector_only"]), "source_job_id": d["source_job_id"],
               "source_take": d["source_take"], "model_repo": d["model_repo"], "model_revision": d["model_revision"],
               "consent_id": d["consent_id"], "version": d["version"], "created_by": d["created_by"],
               "created_at": d["created_at"], "updated_at": d["updated_at"],
               "synced": d["synced_version"] == d["version"]}
        if d["kind"] == "preset" and d["speaker"] in PRESETS:
            out["native_language"] = PRESETS[d["speaker"]][2]
        if d["reference_asset_id"]:
            out["reference_url"] = f"/api/media/assets/{d['reference_asset_id']}/file"
        return out

    def list_voices(self, *, include_presets: bool = True) -> builtins.list[dict]:
        with self.library.connect() as con:
            rows = con.execute("SELECT * FROM voice_voices WHERE deleted_at IS NULL "
                               "ORDER BY updated_at DESC").fetchall()
        saved = [self._row_voice(r) for r in rows]
        return saved + ([preset_view(s) for s in PRESETS] if include_presets else [])

    def get_voice(self, voice_id: str) -> dict:
        m = PRESET_ID.match(voice_id or "")
        if m:
            if m.group(1) not in PRESETS:
                raise VoiceError("no such preset voice", 404, "not_found")
            return preset_view(m.group(1))
        if not VOICE_ID.match(voice_id or ""):
            raise VoiceError("invalid voice id", 404, "not_found")
        with self.library.connect() as con:
            row = con.execute("SELECT * FROM voice_voices WHERE id=? AND deleted_at IS NULL", (voice_id,)).fetchone()
        if not row:
            raise VoiceError("no such voice", 404, "not_found")
        return self._row_voice(row)

    def voice_versions(self, voice_id: str) -> builtins.list[dict]:
        self.get_voice(voice_id)
        with self.library.connect() as con:
            rows = con.execute("SELECT version, snapshot_json, changed_by, created_at FROM voice_versions "
                               "WHERE voice_id=? ORDER BY version DESC", (voice_id,)).fetchall()
        return [{"version": r["version"], "snapshot": json.loads(r["snapshot_json"]),
                 "changed_by": r["changed_by"], "created_at": r["created_at"]} for r in rows]

    def _name_free(self, con: Any, name: str, exclude: str | None = None) -> None:
        key = " ".join(name.lower().split())
        if key.replace(" ", "_") in PRESETS or key in {p[0].lower() for p in PRESETS.values()}:
            raise VoiceError("that name belongs to a preset voice; choose another", 409, "name_taken")
        for r in con.execute("SELECT id, name FROM voice_voices WHERE deleted_at IS NULL").fetchall():
            if r["id"] != exclude and " ".join(r["name"].lower().split()) == key:
                raise VoiceError("a saved voice with that name already exists", 409, "name_taken")

    def _consent(self, consent: Any, *, asset: dict, user: str, via: str, ip: str,
                 voice_id: str | None = None, job_id: str | None = None) -> str:
        if not isinstance(consent, dict) or consent.get("confirmed") is not True:
            raise VoiceError("confirm that you have permission to clone this voice (consent.confirmed = true)",
                             403, "consent_required")
        statement = _text(consent.get("statement") or DEFAULT_CONSENT, "consent.statement", max_len=1000)
        consent_id = "vcs_" + secrets.token_hex(12)
        with self.library.connect() as con:
            con.execute("INSERT INTO voice_consents (id, voice_id, job_id, reference_asset_id, reference_sha256, "
                        "statement, confirmed_by, via, ip, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (consent_id, voice_id, job_id, asset["id"], asset.get("sha256") or "", statement, user,
                         via, ip[:64], time.time()))
        self.audit(user=user, ip=ip, action="voice.consent", outcome="recorded", consent=consent_id,
                   asset=asset["id"], voice=voice_id, job=job_id, via=via)
        return consent_id

    def _reference_asset(self, asset_id: Any) -> dict:
        if not isinstance(asset_id, str) or not ASSET_ID.match(asset_id):
            raise VoiceError("reference_asset_id must be a Library asset id")
        asset = self.library.get(asset_id)
        if asset["type"] != "audio":
            raise VoiceError("the reference must be an audio item from the Library")
        return asset

    def _node_reference(self, asset: dict) -> str:
        """Upload a Library audio asset to gx10-02 once; re-upload if gx10-02 lost it."""
        with self.library.connect() as con:
            row = con.execute("SELECT settings FROM assets WHERE id=?", (asset["id"],)).fetchone()
        cached = json.loads(row["settings"] or "{}").get("node2_voice_ref") if row else None
        if cached and NODE_REF_ID.match(str(cached)):
            try:
                self.client.call("GET", f"/v1/voice/references/{cached}")
                return str(cached)
            except VoiceError as exc:
                if exc.status != 404:
                    raise
        fmt = "wav" if "wav" in (asset.get("variants") or {}) or asset["ext"] == "wav" else asset["ext"]
        path = self.library.file_path(asset, fmt if fmt != asset["ext"] else None)
        if not path.is_file():
            raise VoiceError("the Library file of the reference is missing", 404, "not_found")
        if path.stat().st_size > 32 * 1024 * 1024:
            raise VoiceError("the reference clip is larger than 32 MB; use a shorter clip (2-60 s)", 413, "too_large")
        name = re.sub(r"[^A-Za-z0-9._-]+", "-", asset.get("title") or asset["id"])[:80] + "." + fmt
        ref = self.client.call("POST", "/v1/voice/references", raw=path.read_bytes(), timeout=180,
                               headers={"Content-Type": "application/octet-stream",
                                        "X-Filename": urllib.parse.quote(name)})
        self.library.update_settings(asset["id"], node2_voice_ref=ref["id"])
        return str(ref["id"])

    def _node_spec(self, voice: dict) -> dict:
        if voice["kind"] == "preset":
            return {"kind": "preset", "speaker": voice["speaker"]}
        with self.library.connect() as con:
            row = con.execute("SELECT node_reference_id FROM voice_voices WHERE id=?", (voice["id"],)).fetchone()
        ref_id = row["node_reference_id"] if row else None
        spec = {"kind": "reference", "reference_id": ref_id, "x_vector_only": voice["x_vector_only"]}
        if voice.get("reference_text"):
            spec["transcript"] = voice["reference_text"]
        return spec

    def _sync_voice(self, voice_id: str) -> None:
        """Push one voice to gx10-02 (idempotent upsert, or delete)."""
        with self.library.connect() as con:
            row = con.execute("SELECT * FROM voice_voices WHERE id=?", (voice_id,)).fetchone()
        if not row:
            return
        if row["deleted_at"] is not None:
            self.client.call("DELETE", f"/v1/voice/voices/{voice_id}")
            with self.library.connect() as con:
                con.execute("UPDATE voice_voices SET synced_version=-1 WHERE id=?", (voice_id,))
            return
        voice = self._row_voice(row)
        if voice["kind"] != "preset":
            asset = self.library.get(voice["reference_asset_id"])
            ref_id = self._node_reference(asset)
            if ref_id != row["node_reference_id"]:
                with self.library.connect() as con:
                    con.execute("UPDATE voice_voices SET node_reference_id=? WHERE id=?", (ref_id, voice_id))
        body = {"name": voice["name"], "voice": self._node_spec(voice), "instructions": voice["instructions"],
                "language": voice["language"], "version": voice["version"]}
        self.client.call("PUT", f"/v1/voice/voices/{voice_id}", body=body)
        with self.library.connect() as con:
            con.execute("UPDATE voice_voices SET synced_version=? WHERE id=? AND version=?",
                        (voice["version"], voice_id, voice["version"]))

    def _snapshot(self, con: Any, voice_id: str, user: str) -> None:
        row = con.execute("SELECT * FROM voice_voices WHERE id=?", (voice_id,)).fetchone()
        snap = self._row_voice(row)
        snap.pop("synced", None)
        con.execute("INSERT INTO voice_versions (voice_id, version, snapshot_json, changed_by, created_at) "
                    "VALUES (?,?,?,?,?)", (voice_id, row["version"], json.dumps(snap, sort_keys=True), user,
                                           time.time()))

    def create_voice(self, body: Any, *, user: str, ip: str = "", via: str = "ui") -> dict:
        if not isinstance(body, dict):
            raise VoiceError("request body must be a JSON object")
        allowed = {"kind", "name", "description", "speaker", "instructions", "language", "style", "metadata",
                   "job_id", "take", "reference_asset_id", "transcript", "consent", "x_vector_only"}
        unknown = sorted(set(body) - allowed)
        if unknown:
            raise VoiceError(f"unsupported field(s): {', '.join(unknown[:8])}")
        kind = body.get("kind")
        if kind not in ("preset", "designed", "cloned"):
            raise VoiceError("kind must be preset, designed or cloned")
        name = _text(body.get("name"), "name", max_len=80, single_line=True)
        description = _text(body.get("description"), "description", max_len=1000, required=False)
        instructions = _text(body.get("instructions"), "instructions", max_len=500, required=False,
                             single_line=True)
        language = _language(body.get("language"))
        style = _style(body.get("style"))
        metadata = body.get("metadata") or {}
        if not isinstance(metadata, dict) or len(json.dumps(metadata)) > 4000:
            raise VoiceError("metadata must be an object (at most 4 kB)")
        voice_id = "vc_" + secrets.token_hex(12)
        now = time.time()
        row: dict[str, Any] = {"speaker": None, "reference_asset_id": None, "reference_text": None,
                               "reference_sha256": None, "x_vector_only": 0, "source_job_id": None,
                               "source_take": None, "consent_id": None}
        if kind == "preset":
            speaker = str(body.get("speaker") or "").lower().replace(" ", "_")
            if speaker not in PRESETS:
                raise VoiceError(f"speaker must be one of: {', '.join(PRESETS)}")
            row["speaker"] = speaker
            repo, rev = VARIANT_MODEL["custom"]
            description = description or PRESETS[speaker][1]
        elif kind == "designed":
            job_id = body.get("job_id")
            take = body.get("take", 0)
            if not isinstance(job_id, str) or not JOB_ID.match(job_id):
                raise VoiceError("job_id must be a completed voice design job")
            if isinstance(take, bool) or not isinstance(take, int):
                raise VoiceError("take must be a take number")
            job = self.get(job_id)
            if job["operation"] != "voice_design" or job["status"] != "completed":
                raise VoiceError("pick a take from a completed voice design", 409, "not_ready")
            if not 0 <= take < len(job["takes"]):
                raise VoiceError("no such take", 404, "not_found")
            asset = self.save_take(job_id, take, user=user, title=f"{name} (voice reference)")
            req = job["request"]
            row.update(reference_asset_id=asset["id"], reference_sha256=asset["sha256"],
                       reference_text=req.get("text"), source_job_id=job_id, source_take=take)
            description = description or req.get("description") or ""
            instructions = instructions  # style defaults are metadata for designed voices
            repo, rev = VARIANT_MODEL["base"]
            metadata = {**metadata, "designed_from": req.get("description"),
                        "design_model": VARIANT_MODEL["design"][0], "design_revision": VARIANT_MODEL["design"][1]}
        else:
            asset = self._reference_asset(body.get("reference_asset_id"))
            transcript = _text(body.get("transcript"), "transcript", max_len=2000, required=False)
            xvec = body.get("x_vector_only", not transcript)
            if not isinstance(xvec, bool):
                raise VoiceError("x_vector_only must be true or false")
            row.update(reference_asset_id=asset["id"], reference_sha256=asset["sha256"],
                       reference_text=transcript or None, x_vector_only=int(xvec or not transcript))
            repo, rev = VARIANT_MODEL["base"]
            row["consent_id"] = self._consent(body.get("consent"), asset=asset, user=user, via=via, ip=ip,
                                              voice_id=voice_id)
            self._node_reference(asset)  # validates the clip on gx10-02 (2-60 s, decodable) before saving
        with self._lock, self.library.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                self._name_free(con, name)
                con.execute(
                    "INSERT INTO voice_voices (id, name, kind, description, speaker, instructions, language, "
                    "reference_asset_id, reference_text, reference_sha256, x_vector_only, source_job_id, "
                    "source_take, model_repo, model_revision, style_json, metadata_json, consent_id, version, "
                    "created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?)",
                    (voice_id, name, kind, description, row["speaker"], instructions, language,
                     row["reference_asset_id"], row["reference_text"], row["reference_sha256"],
                     row["x_vector_only"], row["source_job_id"], row["source_take"], repo, rev,
                     json.dumps(style, sort_keys=True), json.dumps(metadata, sort_keys=True), row["consent_id"],
                     user, now, now))
                self._snapshot(con, voice_id, user)
                con.execute("COMMIT")
            except BaseException:
                con.execute("ROLLBACK")
                raise
        try:
            self._sync_voice(voice_id)
        except VoiceError as exc:
            log.warning("voice %s saved but not yet on gx10-02: %s", voice_id, exc)
        self.audit(user=user, ip=ip, action="voice.create", outcome="ok", voice=voice_id, kind=kind, via=via)
        return self.get_voice(voice_id)

    def update_voice(self, voice_id: str, body: Any, *, user: str, ip: str = "") -> dict:
        current = self.get_voice(voice_id)
        if current["builtin"]:
            raise VoiceError("preset voices cannot be changed; save a copy with your defaults", 409, "builtin")
        if not isinstance(body, dict):
            raise VoiceError("request body must be a JSON object")
        allowed = {"name", "description", "instructions", "language", "style", "metadata", "transcript"}
        unknown = sorted(set(body) - allowed)
        if unknown:
            raise VoiceError(f"unsupported field(s): {', '.join(unknown[:8])}")
        changes: dict[str, Any] = {}
        if "name" in body:
            changes["name"] = _text(body["name"], "name", max_len=80, single_line=True)
        if "description" in body:
            changes["description"] = _text(body["description"], "description", max_len=1000, required=False)
        if "instructions" in body:
            changes["instructions"] = _text(body["instructions"], "instructions", max_len=500, required=False,
                                            single_line=True)
        if "language" in body:
            changes["language"] = _language(body["language"])
        if "style" in body:
            changes["style_json"] = json.dumps(_style(body["style"]), sort_keys=True)
        if "metadata" in body:
            if not isinstance(body["metadata"], dict) or len(json.dumps(body["metadata"])) > 4000:
                raise VoiceError("metadata must be an object (at most 4 kB)")
            changes["metadata_json"] = json.dumps(body["metadata"], sort_keys=True)
        if "transcript" in body:
            if current["kind"] == "preset":
                raise VoiceError("preset voices have no reference transcript")
            t = _text(body["transcript"], "transcript", max_len=2000, required=False)
            changes["reference_text"] = t or None
            changes["x_vector_only"] = int(not t)
        if not changes:
            return current
        with self._lock, self.library.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                if "name" in changes:
                    self._name_free(con, changes["name"], exclude=voice_id)
                sets = ", ".join(f"{k}=?" for k in changes)
                con.execute(f"UPDATE voice_voices SET {sets}, version=version+1, updated_at=? WHERE id=?",  # noqa: S608
                            (*changes.values(), time.time(), voice_id))
                self._snapshot(con, voice_id, user)
                con.execute("COMMIT")
            except BaseException:
                con.execute("ROLLBACK")
                raise
        try:
            self._sync_voice(voice_id)
        except VoiceError as exc:
            log.warning("voice %s updated but not yet on gx10-02: %s", voice_id, exc)
        self.audit(user=user, ip=ip, action="voice.update", outcome="ok", voice=voice_id, fields=sorted(changes))
        return self.get_voice(voice_id)

    def delete_voice(self, voice_id: str, *, user: str, ip: str = "") -> dict:
        voice = self.get_voice(voice_id)
        if voice["builtin"]:
            raise VoiceError("preset voices cannot be deleted", 409, "builtin")
        with self.library.connect() as con:
            con.execute("UPDATE voice_voices SET deleted_at=? WHERE id=?", (time.time(), voice_id))
        try:
            self._sync_voice(voice_id)
        except VoiceError as exc:
            log.warning("voice %s deleted here; gx10-02 is updated later: %s", voice_id, exc)
        self.audit(user=user, ip=ip, action="voice.delete", outcome="ok", voice=voice_id)
        return {"id": voice_id, "deleted": True}

    # ------------------------------------------------------------ submit
    def _resolve_voice(self, voice_id: Any, field: str) -> tuple[dict, dict]:
        if not isinstance(voice_id, str):
            raise VoiceError(f"{field} is required (a saved voice id or preset:<name>)")
        voice = self.get_voice(voice_id)
        if voice["builtin"]:
            return voice, {"kind": "preset", "speaker": voice["speaker"]}
        with self.library.connect() as con:
            row = con.execute("SELECT synced_version, version FROM voice_voices WHERE id=?", (voice_id,)).fetchone()
        if row["synced_version"] != row["version"]:
            self._sync_voice(voice_id)
        return voice, {"kind": "saved", "voice_id": voice_id}

    def submit(self, body: Any, *, user: str, via: str = "ui", ip: str = "", owner: str | None = None) -> dict:
        if via not in ("ui", "playground", "control-center", "api", "flow"):
            raise VoiceError("invalid submission source")
        if not isinstance(body, dict):
            raise VoiceError("request body must be a JSON object")
        allowed = {"operation", "text", "voice_id", "description", "reference", "segments", "instructions",
                   "language", "takes", "seed", "speed", "pause_ms", "title", "auto_save", "flow",
                   *SAMPLING_KEYS}
        unknown = sorted(set(body) - allowed)
        if unknown:
            raise VoiceError(f"unsupported field(s): {', '.join(unknown[:8])}")
        op = body.get("operation", "tts")
        if op not in OPERATIONS:
            raise VoiceError(f"operation must be one of: {', '.join(OPERATIONS)}")
        instructions = _text(body.get("instructions"), "instructions", max_len=500, required=False,
                             single_line=True)
        voice: dict | None = None
        consent_asset: dict | None = None
        segments: list[dict] = []
        if op == "dialogue":
            segs = body.get("segments")
            if not isinstance(segs, list) or not 1 <= len(segs) <= MAX_SEGMENTS:
                raise VoiceError(f"segments must be a list of 1-{MAX_SEGMENTS} lines")
            for i, seg in enumerate(segs):
                if not isinstance(seg, dict) or set(seg) - {"voice_id", "text", "instructions", "pause_ms"}:
                    raise VoiceError(f"line {i + 1}: use voice_id, text, instructions and pause_ms")
                _, spec = self._resolve_voice(seg.get("voice_id"), f"line {i + 1} voice")
                entry: dict[str, Any] = {"text": _text(seg.get("text"), f"line {i + 1} text", max_len=MAX_TEXT),
                                         "voice": spec}
                seg_instr = _text(seg.get("instructions"), f"line {i + 1} instructions", max_len=500,
                                  required=False, single_line=True)
                if seg_instr:
                    entry["instructions"] = seg_instr
                if seg.get("pause_ms") is not None:
                    entry["pause_ms"] = _num(seg["pause_ms"], f"line {i + 1} pause", 0, 5000, int)
                segments.append(entry)
        else:
            text = _text(body.get("text"), "text", max_len=MAX_TEXT)
            if op == "tts":
                voice, spec = self._resolve_voice(body.get("voice_id"), "voice_id")
            elif op == "voice_design":
                spec = {"kind": "design",
                        "description": _text(body.get("description"), "description (the voice to design)",
                                             max_len=1000, single_line=True)}
            else:
                ref = body.get("reference")
                if not isinstance(ref, dict) or set(ref) - {"asset_id", "transcript", "consent"}:
                    raise VoiceError("reference must be {asset_id, transcript, consent}")
                consent_asset = self._reference_asset(ref.get("asset_id"))
                if not isinstance(ref.get("consent"), dict) or ref["consent"].get("confirmed") is not True:
                    raise VoiceError("confirm that you have permission to clone this voice", 403,
                                     "consent_required")
                transcript = _text(ref.get("transcript"), "reference.transcript", max_len=2000, required=False)
                spec = {"kind": "reference", "reference_id": self._node_reference(consent_asset),
                        "x_vector_only": not transcript}
                if transcript:
                    spec["transcript"] = transcript
            entry = {"text": text, "voice": spec}
            if instructions:
                entry["instructions"] = instructions
            segments.append(entry)
        style = dict((voice or {}).get("style") or {})
        node: dict[str, Any] = {"operation": op, "language": _language(body.get("language") or
                                                                       (voice or {}).get("language")),
                                "segments": segments}
        for key, lo, hi, typ in (("takes", 1, 4, int), ("seed", 0, 2_147_483_643, int),
                                 ("speed", 0.5, 2.0, float), ("pause_ms", 0, 5000, int)):
            val = body.get(key, style.get(key))
            if val is not None:
                node[key] = _num(val, key, lo, hi, typ)
        sampling = {}
        for key in SAMPLING_KEYS:
            val = body.get(key, style.get(key))
            if val is not None:
                sampling[key] = _num(val, key, *STYLE_KEYS[key])
        if sampling:
            node["sampling"] = sampling
        title = _text(body.get("title"), "title", max_len=200, required=False, single_line=True)
        if title:
            node["title"] = title
        auto_save = body.get("auto_save", False)
        if not isinstance(auto_save, bool):
            raise VoiceError("auto_save must be true or false")
        flow = _flow(body.get("flow"))
        job_id = "vj_" + secrets.token_hex(16)
        node["client_ref"] = job_id
        consent_id = None
        if consent_asset is not None:
            consent_id = self._consent(body["reference"]["consent"], asset=consent_asset, user=user, via=via,
                                       ip=ip, job_id=job_id)
        try:
            remote = self.client.call("POST", "/v1/voice/jobs", body=node, timeout=60)
        except VoiceError as exc:
            saved = sorted({seg["voice"]["voice_id"] for seg in segments if seg["voice"]["kind"] == "saved"})
            if exc.status != 404 or not saved:
                raise
            for vid in saved:  # gx10-02 lost a replica or its clip: push again, then retry once
                self._sync_voice(vid)
            remote = self.client.call("POST", "/v1/voice/jobs", body=node, timeout=60)
        node_id = remote.get("id") if isinstance(remote, dict) else None
        if not NODE_JOB_ID.match(str(node_id)):
            raise VoiceError("the voice service returned an invalid job", 502, "upstream_error")
        public_req = {k: v for k, v in body.items() if k not in ("flow",)}
        if isinstance(public_req.get("reference"), dict):
            public_req["reference"] = {k: v for k, v in public_req["reference"].items() if k != "consent"}
            public_req["reference"]["consent_id"] = consent_id
        now = time.time()
        with self.library.connect() as con:
            con.execute(
                "INSERT INTO voice_jobs (id, node_job_id, operation, status, title, voice_id, request_json, "
                "node_request_json, user, via, owner, auto_save, consent_id, flow_id, flow_run_id, flow_node_id, "
                "detail, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, node_id, op, remote.get("status", "queued"), remote.get("title") or title,
                 (voice or {}).get("id"), json.dumps(public_req), json.dumps(node), user, via, owner,
                 int(auto_save), consent_id, (flow or {}).get("flow_id"), (flow or {}).get("flow_run_id"),
                 (flow or {}).get("flow_node_id"), remote.get("detail") or "", now, now))
        self.audit(user=user, ip=ip, action=f"voice.{op}", outcome="queued", job=job_id, via=via)
        self._wake.set()
        return self.get(job_id)

    # -------------------------------------------------------------- read
    def _job_row(self, job_id: str) -> Any:
        if not isinstance(job_id, str) or not JOB_ID.match(job_id):
            raise VoiceError("invalid job id", 404, "not_found")
        with self.library.connect() as con:
            row = con.execute("SELECT * FROM voice_jobs WHERE id=? AND deleted_at IS NULL", (job_id,)).fetchone()
        if not row:
            raise VoiceError("no such voice job", 404, "not_found")
        return row

    def _view(self, row: Any) -> dict:
        with self.library.connect() as con:
            takes = con.execute("SELECT * FROM voice_takes WHERE job_id=? ORDER BY take_index",
                                (row["id"],)).fetchall()
        status = row["status"]
        if status == "completed" and not row["imported"]:
            status = "saving"
        timings = json.loads(row["timings_json"] or "{}")
        view = {
            "id": row["id"], "operation": row["operation"], "status": status, "detail": row["detail"],
            "progress": row["progress"], "title": row["title"], "voice_id": row["voice_id"],
            "created_at": row["created_at"], "started_at": timings.get("started_at"),
            "finished_at": row["finished_at"], "request": json.loads(row["request_json"]),
            "takes": [{"index": t["take_index"], "seed": t["seed"], "duration_s": t["duration_s"],
                       "sample_rate": t["sample_rate"], "rms_dbfs": t["rms_dbfs"], "peak": t["peak"],
                       "waveform": json.loads(t["waveform_json"] or "[]"),
                       "formats": sorted(json.loads(t["files_json"] or "{}")), "asset_id": t["asset_id"],
                       "audio_url": f"/api/voice/jobs/{row['id']}/takes/{t['take_index']}/audio"}
                      for t in takes],
            "timings": {k: val for k, val in timings.items() if k != "started_at"},
            "notes": json.loads(row["notes_json"] or "[]"),
            "error": json.loads(row["error_json"]) if row["error_json"] else None,
            "waiting": None, "via": row["via"], "user": row["user"], "auto_save": bool(row["auto_save"]),
            "imported": bool(row["imported"]), "consent_id": row["consent_id"],
            "flow": ({"flow_id": row["flow_id"], "flow_run_id": row["flow_run_id"],
                      "flow_node_id": row["flow_node_id"]} if row["flow_id"] else None),
        }
        if status == "waiting_for_resource":
            waiting = self._cache.get(f"waiting:{row['id']}", (0, None))[1]
            try:
                cluster = self.explain(ALIAS)
            except Exception:  # noqa: BLE001 - the resource view is advisory
                cluster = None
            view["waiting"] = {**(waiting or {}), **({"cluster": cluster} if cluster else {})} or None
        return view

    def get(self, job_id: str) -> dict:
        return self._view(self._job_row(job_id))

    def list_jobs(self, *, limit: int = 50, status: str | None = None, voice_id: str | None = None,
                  owner: str | None = None, flow_run_id: str | None = None) -> builtins.list[dict]:
        q = "SELECT * FROM voice_jobs WHERE deleted_at IS NULL"
        args: list[Any] = []
        if status == "active":
            q += " AND status NOT IN ('completed','failed','cancelled') OR (status='completed' AND imported=0 " \
                 "AND deleted_at IS NULL)"
        elif status:
            q += " AND status=?"
            args.append(status)
        if voice_id:
            q += " AND voice_id=?"
            args.append(voice_id)
        if owner:
            q += " AND owner=?"
            args.append(owner)
        if flow_run_id:
            q += " AND flow_run_id=?"
            args.append(flow_run_id)
        q += " ORDER BY created_at DESC LIMIT ?"
        args.append(max(1, min(200, int(limit))))
        with self.library.connect() as con:
            rows = con.execute(q, args).fetchall()
        return [self._view(r) for r in rows]

    def activity(self, user: str, since: float, limit: int) -> builtins.list[dict]:
        """This user's voice jobs for the Playground Logs feed (plt.md section 6).

        Only small, non-sensitive fields: never the script, the voice
        description or a consent statement."""
        with self.library.connect() as con:
            rows = con.execute(
                "SELECT id, operation, status, title, detail, voice_id, created_at, finished_at, error_json, "
                "timings_json FROM voice_jobs WHERE deleted_at IS NULL AND user=? AND "
                "COALESCE(finished_at, created_at) >= ? ORDER BY created_at DESC LIMIT ?",
                (user, float(since or 0), max(1, min(200, int(limit))))).fetchall()
        out = []
        for r in rows:
            error = json.loads(r["error_json"]) if r["error_json"] else None
            timings = json.loads(r["timings_json"] or "{}")
            out.append({
                "id": r["id"], "kind": "voice",
                "title": r["title"] or OPERATION_LABEL.get(r["operation"], "Voice"),
                "status": r["status"],
                "at": r["finished_at"] or r["created_at"],
                "duration_ms": (int((r["finished_at"] - r["created_at"]) * 1000)
                                if r["finished_at"] and r["created_at"] else None),
                "error": (error or {}).get("message"),
                "link": f"#/voice?job={r['id']}",
                "detail": {"operation": r["operation"], "voice_id": r["voice_id"],
                           "audio_seconds": timings.get("audio_s"), "rtf": timings.get("rtf"),
                           "variant": (timings.get("variants") or [None])[0]},
            })
        return out

    def cancel(self, job_id: str, *, user: str) -> dict:
        row = self._job_row(job_id)
        if row["status"] in TERMINAL:
            raise VoiceError(f"the job is already {row['status']}", 409, "conflict")
        remote = self.client.call("POST", f"/v1/voice/jobs/{row['node_job_id']}/cancel")
        self._apply(row, remote)
        self.audit(user=user, ip="", action="voice.cancel", outcome="ok", job=job_id)
        return self.get(job_id)

    def delete_job(self, job_id: str, *, user: str) -> dict:
        row = self._job_row(job_id)
        if row["status"] not in TERMINAL:
            raise VoiceError("cancel the job before deleting it", 409, "conflict")
        with self.library.connect() as con:
            con.execute("UPDATE voice_jobs SET deleted_at=? WHERE id=?", (time.time(), job_id))
        shutil.rmtree(self.takes_root / job_id, ignore_errors=True)
        try:
            self.client.call("DELETE", f"/v1/voice/jobs/{row['node_job_id']}")
        except VoiceError as exc:
            if exc.status != 404:
                log.warning("could not delete %s on gx10-02: %s", row["node_job_id"], exc)
        self.audit(user=user, ip="", action="voice.delete_job", outcome="ok", job=job_id)
        return {"id": job_id, "deleted": True}

    def wait(self, job_id: str, *, timeout: float = 900, poll: float = 1.0) -> dict:
        deadline = time.time() + timeout
        while True:
            job = self.get(job_id)
            if job["status"] in TERMINAL or time.time() >= deadline:
                return job
            self._wake.set()
            time.sleep(max(0.2, min(poll, deadline - time.time())))

    def take_file(self, job_id: str, take: int, fmt: str = "wav") -> Path:
        self._job_row(job_id)
        if fmt not in TAKE_FORMATS:
            raise VoiceError("format must be wav or mp3")
        if isinstance(take, bool) or not isinstance(take, int) or not 0 <= take < 4:
            raise VoiceError("no such take", 404, "not_found")
        path = self.takes_root / job_id / f"take-{take}.{fmt}"
        if not path.is_file():
            raise VoiceError("this take is not available", 404, "not_found")
        return path

    def save_take(self, job_id: str, take: int, *, user: str, title: str | None = None,
                  flow: dict | None = None) -> dict:
        row = self._job_row(job_id)
        flow = _flow(flow) or ({"flow_id": row["flow_id"], "flow_run_id": row["flow_run_id"],
                                "flow_node_id": row["flow_node_id"]} if row["flow_id"] else None)
        with self._lock:
            with self.library.connect() as con:
                t = con.execute("SELECT * FROM voice_takes WHERE job_id=? AND take_index=?",
                                (job_id, take)).fetchone()
            if not t:
                raise VoiceError("this take is not ready", 404, "not_found")
            if t["asset_id"]:
                try:
                    return self.library.get(t["asset_id"])
                except LibraryError:
                    pass  # deleted from the Library: save it again
            wav = self.take_file(job_id, take, "wav")
            tmp_wav = self.library.tmp_file(".wav")
            shutil.copyfile(wav, tmp_wav)
            variants: dict[str, Path] = {}
            mp3 = self.takes_root / job_id / f"take-{take}.mp3"
            if mp3.is_file():
                variants["mp3"] = self.library.tmp_file(".mp3")
                shutil.copyfile(mp3, variants["mp3"])
            req = json.loads(row["request_json"])
            node_req = json.loads(row["node_request_json"])
            timings = json.loads(row["timings_json"] or "{}")
            variant = (timings.get("variants") or ["custom"])[0]
            repo, rev = VARIANT_MODEL.get(variant, VARIANT_MODEL["custom"])
            parent = None
            voice_name = None
            if row["voice_id"] and VOICE_ID.match(row["voice_id"]):
                try:
                    voice = self.get_voice(row["voice_id"])
                    voice_name = voice["name"]
                    parent = voice.get("reference_asset_id")
                except VoiceError:
                    parent = None
            if row["operation"] == "voice_clone":
                parent = (req.get("reference") or {}).get("asset_id")
            if parent:
                try:
                    self.library.get(parent)
                except LibraryError:
                    parent = None
            text = req.get("text") or "\n".join(s.get("text", "") for s in req.get("segments") or [])
            base_title = title or row["title"] or text[:60]
            if not title and len(json.loads(row["node_request_json"]).get("segments", [])) and \
                    node_req.get("takes", 1) > 1:
                base_title = f"{base_title} (take {take + 1})"
            try:
                asset = self.library.add(NewAsset(
                    type="audio", ext="wav", operation=LIBRARY_OPERATION[row["operation"]], data_path=tmp_wav,
                    variant_paths=variants, title=base_title[:200], model_alias=ALIAS, model_repo=repo,
                    model_revision=rev, workflow=f"qwen3-tts:{variant}", prompt=text[:4000], seed=t["seed"],
                    duration=t["duration_s"], sample_rate=t["sample_rate"], channels=1,
                    waveform=json.loads(t["waveform_json"] or "[]"), parent_id=parent, job_id=job_id,
                    tags=[x for x in (row["operation"], voice_name) if x],
                    settings={"voice_job_id": job_id, "take_index": take, "operation": row["operation"],
                              "request": req, "timings": timings, "voice_id": row["voice_id"],
                              "voice_name": voice_name, "notes": json.loads(row["notes_json"] or "[]"),
                              "variants": timings.get("variants"), "rms_dbfs": t["rms_dbfs"],
                              "consent_id": row["consent_id"],
                              "description": req.get("description"), "instructions": req.get("instructions")},
                    source_kind="voice_take", source_ref=f"{job_id}#{take}",
                    flow_id=(flow or {}).get("flow_id"), flow_run_id=(flow or {}).get("flow_run_id"),
                    flow_node_id=(flow or {}).get("flow_node_id")))
            except BaseException:
                tmp_wav.unlink(missing_ok=True)
                for p in variants.values():
                    p.unlink(missing_ok=True)
                raise
            with self.library.connect() as con:
                con.execute("UPDATE voice_takes SET asset_id=? WHERE job_id=? AND take_index=?",
                            (asset["id"], job_id, take))
        self.audit(user=user, ip="", action="voice.save_take", outcome="ok", job=job_id, take=take, asset=asset["id"])
        return asset

    def upload_reference(self, data: bytes, filename: str, content_type: str, *, user: str,
                         title: str | None = None) -> dict:
        """A reference clip: validated on gx10-02 first, then kept in the Library (operation upload)."""
        ext = AUDIO_TYPES.get(content_type.split(";")[0].strip().lower())
        if ext is None:
            raise VoiceError("upload WAV, FLAC, MP3, OGG, WebM or M4A audio")
        if not data:
            raise VoiceError("the upload is empty")
        ref = self.client.call("POST", "/v1/voice/references", raw=data, timeout=180,
                               headers={"Content-Type": "application/octet-stream",
                                        "X-Filename": urllib.parse.quote(filename[:120] or f"reference.{ext}")})
        container = ref.get("container")
        if container in ("wav", "flac", "mp3", "ogg", "m4a"):
            ext = container
        tmp = self.library.tmp_file("." + ext)
        tmp.write_bytes(data)
        asset = self.library.add(NewAsset(
            type="audio", ext=ext, operation="upload", data_path=tmp, title=title or filename or None,
            duration=ref.get("duration_s"), channels=None,
            settings={"uploaded_by": user, "original_filename": filename[:120], "purpose": "voice_reference"},
            source_kind="voice_reference"))
        self.library.update_settings(asset["id"], node2_voice_ref=ref["id"])
        self.audit(user=user, ip="", action="voice.upload_reference", outcome="ok", asset=asset["id"])
        return self.library.get(asset["id"])

    # ------------------------------------------------------------ worker
    def _loop(self) -> None:
        while True:
            busy = False
            try:
                busy = self.sweep()
                if time.time() - self._last_reconcile > 600:
                    self._last_reconcile = time.time()
                    self.reconcile_voices()
                self.last_error = None
            except Exception as exc:  # noqa: BLE001 - the worker must never die
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("voice sweep failed: %s", self.last_error)
            self._wake.wait(self.poll_interval if busy else 10)
            self._wake.clear()

    def reconcile_voices(self) -> int:
        """Re-push voices gx10-02 does not have (it may have lost its database)."""
        remote = self.client.call("GET", "/v1/voice/voices")
        have = {v["id"]: v.get("version") for v in (remote or {}).get("data", [])}
        pushed = 0
        with self.library.connect() as con:
            rows = con.execute("SELECT id, version, deleted_at, synced_version FROM voice_voices").fetchall()
        for r in rows:
            try:
                stale = have.get(r["id"]) != r["version"] if r["deleted_at"] is None else r["id"] in have
                if stale:
                    self._sync_voice(r["id"])
                    pushed += 1
            except (VoiceError, LibraryError) as exc:
                log.warning("voice %s not synced to gx10-02: %s", r["id"], exc)
        return pushed

    def open_jobs(self) -> builtins.list[Any]:
        with self.library.connect() as con:
            return con.execute("SELECT * FROM voice_jobs WHERE deleted_at IS NULL AND imported=0 "
                               "AND status NOT IN ('failed','cancelled') ORDER BY created_at").fetchall()

    def sweep(self) -> bool:
        rows = self.open_jobs()
        for row in rows:
            try:
                remote = self.client.call("GET", f"/v1/voice/jobs/{row['node_job_id']}")
            except VoiceError as exc:
                if exc.status == 404:
                    self._apply(row, {"status": "failed", "detail": "", "error": {
                        "code": "lost", "message": "the job no longer exists on gx10-02", "retryable": True}})
                    continue
                raise
            self._apply(row, remote)
            if remote.get("status") == "completed":
                self.import_job(self._job_row(row["id"]), remote)
        return bool(rows)

    def _apply(self, row: Any, remote: dict) -> None:
        status = remote.get("status") or row["status"]
        timings = dict(remote.get("timings") or {})
        if remote.get("started_at"):
            timings["started_at"] = remote["started_at"]
        variants = sorted((remote.get("model") or {}).get("variants") or {})
        if variants:
            timings["variants"] = variants
        if remote.get("waiting"):
            self._cache[f"waiting:{row['id']}"] = (time.time(), remote["waiting"])
        with self.library.connect() as con:
            con.execute("UPDATE voice_jobs SET status=?, detail=?, progress=?, timings_json=?, notes_json=?, "
                        "error_json=?, finished_at=?, updated_at=? WHERE id=?",
                        (status, str(remote.get("detail") or "")[:400], remote.get("progress"),
                         json.dumps(timings), json.dumps(remote.get("notes") or []),
                         json.dumps(remote["error"]) if remote.get("error") else None,
                         remote.get("finished_at"), time.time(), row["id"]))
        if status == "failed" and row["status"] != "failed" and self.results:
            self.results.record(ALIAS, "inference", False, str((remote.get("error") or {}).get("message", "")))

    def import_job(self, row: Any, remote: dict) -> builtins.list[int]:
        """Download and record every take of a completed job (idempotent)."""
        job_id = row["id"]
        out_dir = self.takes_root / job_id
        out_dir.mkdir(parents=True, exist_ok=True)
        with self.library.connect() as con:
            have = {r["take_index"] for r in con.execute("SELECT take_index FROM voice_takes WHERE job_id=?",
                                                          (job_id,)).fetchall()}
        done = []
        for take in remote.get("takes") or []:
            index = int(take["index"])
            if index in have:
                done.append(index)
                continue
            files = {}
            for fmt in TAKE_FORMATS:
                meta = (take.get("files") or {}).get(fmt)
                if not meta:
                    continue
                dest = out_dir / f"take-{index}.{fmt}"
                part = dest.with_name(dest.name + ".part")
                size = self.client.download(
                    f"/v1/voice/jobs/{row['node_job_id']}/content?take={index}&format={fmt}", part)
                if (meta.get("sha256") and _sha256(part) != meta["sha256"]) or \
                        (meta.get("bytes") and size != meta["bytes"]):
                    part.unlink(missing_ok=True)
                    raise VoiceError(f"take {index + 1} ({fmt}) failed its checksum", 502, "checksum")
                part.replace(dest)
                files[fmt] = {"bytes": size, "sha256": meta.get("sha256")}
            if "wav" not in files:
                raise VoiceError(f"take {index + 1} has no downloadable audio", 502, "no_audio")
            with self.library.connect() as con:
                con.execute("INSERT OR IGNORE INTO voice_takes (job_id, take_index, seed, duration_s, sample_rate, "
                            "rms_dbfs, peak, waveform_json, files_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (job_id, index, take.get("seed"), float(take.get("duration_s") or 0),
                             int(take.get("sample_rate") or 24000), take.get("rms_dbfs"), take.get("peak"),
                             json.dumps(take.get("waveform") or []), json.dumps(files), time.time()))
            done.append(index)
        if row["auto_save"]:
            for index in done:
                self.save_take(job_id, index, user=row["user"])
        with self.library.connect() as con:
            con.execute("UPDATE voice_jobs SET imported=1, updated_at=? WHERE id=?", (time.time(), job_id))
        if self.results:
            timings = remote.get("timings") or {}
            self.results.record(ALIAS, "inference", True, f"{row['operation']} ok",
                                seconds=timings.get("generate_s"))
        self.audit(user=row["user"], ip="", action=f"voice.{row['operation']}", outcome="completed", job=job_id,
                   takes=len(done))
        return done
