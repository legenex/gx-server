"""Reference analysis for the Music page (MUS, build V3).

Sources and what is really analysed:

* **Uploaded file / Library audio** -> gx10-02 measures the audio itself
  (``POST /v1/music/analyses``: tempo, key, time signature, energy, spectrum,
  structure; numpy/scipy in a network-less helper container) and, when asked,
  ACE-Step 1.5 listens to it (audio understanding: caption, genres, vocals,
  language). gx-auto then turns those facts into editable settings.
* **YouTube / Spotify URL** -> only the platform's public oEmbed metadata
  (title, channel/artist) is fetched, through ``netguard`` (SSRF-safe, https,
  size-capped). No audio is downloaded, no DRM or sign-in is bypassed, and the
  result says so: ``audio_analysed: false``.

Every value carries its origin: ``measured`` (signal processing),
``model`` (ACE-Step's own inference) or ``inferred`` (gx-auto's suggestion).
The reference's lyrics are never returned or reused (only whether vocals were
heard, and their language). Sessions live in memory for an hour.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import threading
import time
import urllib.parse
from collections.abc import Callable
from typing import Any

from . import netguard
from .music import JOB_ID, MusicError, MusicJobs
from .music_ai import LANGUAGES, MusicAI, MusicAIError

log = logging.getLogger("gx.ui.music_reference")

SESSION_TTL = 3600
MAX_SESSIONS = 64
URL_MAX = 512
HINT_MAX = 300
ANALYSIS_TIMEOUT = 45 * 60
YT_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
SP_ID = re.compile(r"^[A-Za-z0-9]{22}$")
YOUTUBE_HOSTS = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"})
SPOTIFY_HOSTS = frozenset({"open.spotify.com"})
SPOTIFY_KINDS = ("track", "album", "playlist", "episode", "show", "artist")
OEMBED = {"youtube": "https://www.youtube.com/oembed?format=json&url={}",
          "spotify": "https://open.spotify.com/oembed?url={}"}
LABELS = {
    "measured": "Measured from the audio (signal processing on gx10-02)",
    "model": "Heard by ACE-Step 1.5 (model inference, approximate)",
    "inferred": "Suggested by gx-auto from the facts above (not measured)",
    "metadata": "Public page metadata only (title and channel); no audio was analysed",
}


class ReferenceError_(Exception):  # noqa: N801 - mirrors the package's other *_ error names
    def __init__(self, message: str, status: int = 400, code: str = "invalid_request") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def classify_url(raw: Any) -> tuple[str, str]:
    """A user URL -> (platform, canonical URL rebuilt from the id). Raises ReferenceError_."""
    if not isinstance(raw, str) or not raw.strip():
        raise ReferenceError_("enter a YouTube or Spotify link")
    url = raw.strip()
    if len(url) > URL_MAX or any(c.isspace() for c in url):
        raise ReferenceError_("that link is not valid")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("https", "http") or parts.username or parts.password or parts.port not in (None, 443, 80):
        raise ReferenceError_("use a normal https:// YouTube or Spotify link")
    host = (parts.hostname or "").lower()
    if host in YOUTUBE_HOSTS:
        vid = None
        if host == "youtu.be":
            vid = parts.path.strip("/").split("/")[0]
        elif parts.path == "/watch":
            vid = (urllib.parse.parse_qs(parts.query).get("v") or [""])[0]
        else:
            m = re.fullmatch(r"/(?:shorts|embed|live)/([^/]+)/?", parts.path)
            vid = m.group(1) if m else None
        if not vid or not YT_ID.match(vid):
            raise ReferenceError_("that YouTube link does not name a video")
        return "youtube", f"https://www.youtube.com/watch?v={vid}"
    if host in SPOTIFY_HOSTS:
        m = re.fullmatch(r"(?:/intl-[a-z]{2}(?:-[a-z]{2})?)?/(" + "|".join(SPOTIFY_KINDS) + r")/([^/]+)/?",
                         parts.path)
        if not m or not SP_ID.match(m.group(2)):
            raise ReferenceError_("that Spotify link does not name a track, album or playlist")
        return "spotify", f"https://open.spotify.com/{m.group(1)}/{m.group(2)}"
    raise ReferenceError_("only YouTube and Spotify links can be used as a reference")


def fetch_oembed(platform: str, canonical: str, fetch: Callable[..., netguard.FetchResult]) -> dict:
    """Public oEmbed metadata through netguard. Never audio, never thumbnails."""
    target = OEMBED[platform].format(urllib.parse.quote(canonical, safe=""))
    try:
        res = fetch(target, timeout=10.0, max_bytes=256 * 1024, allow_http=False,
                    headers={"Accept": "application/json"})
    except netguard.BlockedURL as exc:
        raise ReferenceError_(f"the link was blocked: {exc}", 400, "blocked_url") from None
    except OSError:
        raise ReferenceError_(f"{platform.capitalize()} could not be reached; try again later", 503,
                              "metadata_unavailable") from None
    if res.status in (401, 403, 404):
        raise ReferenceError_(f"{platform.capitalize()} has no public information for that link "
                              "(private, removed or region-locked)", 404, "metadata_unavailable")
    if res.status != 200 or res.truncated:
        raise ReferenceError_(f"{platform.capitalize()} answered HTTP {res.status}", 502, "metadata_unavailable")
    try:
        data = json.loads(res.text())
    except ValueError:
        raise ReferenceError_(f"{platform.capitalize()} returned unreadable metadata", 502,
                              "metadata_unavailable") from None
    if not isinstance(data, dict):
        raise ReferenceError_("unreadable metadata", 502, "metadata_unavailable")

    def text(key: str, limit: int = 200) -> str | None:
        val = data.get(key)
        if not isinstance(val, str):
            return None
        return re.sub(r"[\x00-\x1f\x7f]", "", val)[:limit].strip() or None

    return {"platform": platform, "url": canonical, "title": text("title"),
            "author": text("author_name"), "provider": text("provider_name", 60) or platform.capitalize()}


def _measured_summary(m: dict) -> dict:
    """The facts gx-auto sees (numbers with their confidence)."""
    tempo, key, meter = m.get("tempo") or {}, m.get("key") or {}, m.get("time_signature") or {}
    return {
        "tempo_bpm": tempo.get("bpm"), "tempo_confidence": tempo.get("confidence"),
        "tempo_stability": tempo.get("stability"),
        "tempo_alternatives": [c.get("bpm") for c in (tempo.get("candidates") or [])[1:3]],
        "key": key.get("value"), "key_confidence": key.get("confidence"),
        "time_signature": meter.get("value"), "time_signature_confidence": meter.get("confidence"),
        "duration_s": m.get("duration_s"), "loudness": m.get("loudness"),
        "energy": {k: (m.get("energy") or {}).get(k) for k in ("level", "trend")},
        "spectrum": {k: (m.get("spectrum") or {}).get(k) for k in ("brightness", "bass_weight", "centroid_hz")},
        "texture": (m.get("texture") or {}).get("character"), "stereo": (m.get("stereo") or {}).get("label"),
        "structure": [f"{s.get('label')} {s.get('start'):.0f}-{s.get('end'):.0f}s {s.get('energy')}"
                      for s in ((m.get("structure") or {}).get("segments") or [])[:16]],
        "descriptors": m.get("descriptors"),
    }


def _understanding_public(u: dict | None) -> dict | None:
    if not u:
        return None
    lyrics = u.get("lyrics") or ""
    return {"method": u.get("method"), "caption": u.get("caption"), "genres": u.get("genres"),
            "vocals_detected": bool(u.get("vocals_detected")), "language": u.get("language"),
            "sung_lines_heard": sum(1 for ln in lyrics.splitlines() if ln.strip() and not ln.strip().startswith("[")),
            "bpm": u.get("bpm"), "key": u.get("key"), "time_signature": u.get("time_signature")}


class ReferenceAnalyzer:
    def __init__(self, music: MusicJobs, ai: MusicAI, *, fetch: Callable[..., netguard.FetchResult] = netguard.fetch,
                 poll: float = 2.0, audit: Callable[..., None] | None = None, start_threads: bool = True) -> None:
        self.music = music
        self.ai = ai
        self.fetch = fetch
        self.poll = poll
        self.audit = audit or (lambda **kw: None)
        self.start_threads = start_threads
        self._lock = threading.Lock()
        self._sessions: dict[str, dict] = {}

    # ------------------------------------------------------------ public
    def start(self, body: Any, *, user: str) -> dict:
        if not isinstance(body, dict):
            raise ReferenceError_("request body must be an object")
        unknown = sorted(set(body) - {"source", "understand", "suggest", "hint"})
        if unknown:
            raise ReferenceError_(f"unsupported field(s): {', '.join(unknown[:5])}")
        src = body.get("source")
        if not isinstance(src, dict) or src.get("kind") not in ("asset", "url"):
            raise ReferenceError_("source must be {kind: 'asset', asset_id} or {kind: 'url', url}")
        for flag in ("understand", "suggest"):
            if not isinstance(body.get(flag, True), bool):
                raise ReferenceError_(f"{flag} must be true or false")
        hint = body.get("hint") or ""
        if not isinstance(hint, str) or len(hint) > HINT_MAX:
            raise ReferenceError_(f"hint must be text up to {HINT_MAX} characters")
        session = {"id": secrets.token_hex(12), "user": user, "created_at": time.time(), "state": "starting",
                   "detail": "", "source": {}, "audio_analysed": False, "measured": None, "understanding": None,
                   "suggestions": None, "field_sources": {}, "labels": LABELS, "notice": "", "error": None,
                   "job_id": None, "understand": body.get("understand", True) is True,
                   "suggest": body.get("suggest", True) is True, "hint": hint.strip()}
        if src["kind"] == "url":
            platform, canonical = classify_url(src.get("url"))
            session["source"] = {"kind": "url", "platform": platform, "url": canonical}
            session["notice"] = (f"{platform.capitalize()} audio is not downloaded or analysed (it is protected). "
                                 "Only the public title and channel were read; the suggestions are inferred from "
                                 "them, not measured.")
        else:
            asset_id = src.get("asset_id")
            if not isinstance(asset_id, str) or not re.fullmatch(r"a_[0-9a-f]{24}", asset_id):
                raise ReferenceError_("choose an audio file from the Library or upload one")
            asset = self.music.library.get(asset_id)
            if asset.get("type") != "audio":
                raise ReferenceError_("the selected Library item is not audio")
            if (asset.get("duration") or 0) > 600:
                raise ReferenceError_("references can be at most 10 minutes long")
            session["source"] = {"kind": "asset", "asset_id": asset_id, "title": asset.get("title"),
                                 "duration_s": asset.get("duration")}
        with self._lock:
            self._gc()
            if sum(1 for s in self._sessions.values() if s["user"] == user and s["state"] not in
                   ("done", "failed")) >= 2:
                raise ReferenceError_("two analyses are already running for you; wait for one to finish", 429,
                                      "busy")
            self._sessions[session["id"]] = session
        self.audit(user=user, ip="", action="music.reference", outcome="started", kind=src["kind"],
                   platform=session["source"].get("platform"))
        if self.start_threads:
            threading.Thread(target=self.run, args=(session["id"],), name="music-reference", daemon=True).start()
        return self.view(session)

    def get(self, sid: str, *, user: str) -> dict:
        with self._lock:
            session = self._sessions.get(sid)
        if not session or session["user"] != user:
            raise ReferenceError_("no such analysis (it may have expired)", 404, "not_found")
        return self.view(session)

    @staticmethod
    def view(s: dict) -> dict:
        return {k: s[k] for k in ("id", "state", "detail", "source", "audio_analysed", "measured", "understanding",
                                  "suggestions", "field_sources", "labels", "notice", "error", "created_at")}

    # ------------------------------------------------------------ worker
    def _set(self, s: dict, **kw: Any) -> None:
        with self._lock:
            s.update(kw)

    def run(self, sid: str) -> None:
        with self._lock:
            s = self._sessions[sid]
        try:
            if s["source"]["kind"] == "url":
                self._run_url(s)
            else:
                self._run_audio(s)
            self._set(s, state="done", detail="")
        except (ReferenceError_, MusicError, MusicAIError) as exc:
            self._set(s, state="failed", detail="", error={"message": str(exc),
                                                            "code": getattr(exc, "code", "failed")})
        except Exception as exc:  # noqa: BLE001 - a session must always end
            log.exception("reference analysis %s crashed", sid)
            self._set(s, state="failed", detail="", error={"message": f"analysis failed ({type(exc).__name__})",
                                                            "code": "internal_error"})

    def _run_url(self, s: dict) -> None:
        self._set(s, state="metadata", detail="reading the public page information")
        meta = fetch_oembed(s["source"]["platform"], s["source"]["url"], self.fetch)
        self._set(s, source={**s["source"], **meta})
        if not s["suggest"]:
            return
        self._set(s, state="suggesting", detail="gx-auto is suggesting settings from the title and channel")
        facts = {"source": "public page metadata only; NO audio was analysed",
                 "title": meta.get("title"), "channel_or_artist": meta.get("author"),
                 "platform": meta.get("provider")}
        result = self.ai.suggest(facts, hint=s["hint"], user=s["user"])
        settings = result["settings"]
        sources = {k: "inferred" for k in settings if k not in ("seed", "notes")}
        self._set(s, suggestions={"settings": settings, "notes": settings.get("notes", ""), "model": result["model"]},
                  field_sources=sources)

    def _run_audio(self, s: dict) -> None:
        self._set(s, state="measuring", detail="measuring tempo, key, energy and structure on gx10-02")
        ref, _ = self.music._resolve_ref({"asset_id": s["source"]["asset_id"]}, "source")  # noqa: SLF001
        job = self.music.client.call("POST", "/v1/music/analyses",
                                     body={"source": ref, "understand": s["understand"],
                                           "title": f"Reference {s['source'].get('title') or ''}"[:120]},
                                     timeout=60)
        job_id = job.get("id")
        if not JOB_ID.match(str(job_id)):
            raise MusicError("the music service returned an invalid analysis job", 502, "upstream_error")
        self._set(s, job_id=job_id)
        deadline = time.time() + ANALYSIS_TIMEOUT
        announced_measure = False
        while True:
            job = self.music.client.call("GET", f"/v1/music/{job_id}")
            analysis = job.get("analysis") or {}
            if analysis.get("measured") and not announced_measure:
                announced_measure = True
                self._set(s, measured=analysis["measured"], audio_analysed=True)
            status = job.get("status")
            if status == "completed":
                break
            if status in ("failed", "cancelled"):
                err = job.get("error") or {}
                raise MusicError(err.get("message") or f"the analysis was {status}", 502,
                                 str(err.get("code") or "analysis_failed"))
            if time.time() > deadline:
                raise MusicError("the analysis took too long", 504, "timeout")
            if status in ("queued", "waiting_for_resource", "loading_model", "generating"):
                self._set(s, state="listening",
                          detail=job.get("detail") or "waiting for ACE-Step to listen to the track")
            time.sleep(self.poll)
        measured = analysis.get("measured")
        understanding = _understanding_public(analysis.get("understanding"))
        self._set(s, measured=measured, understanding=understanding, audio_analysed=bool(measured))
        if not s["suggest"]:
            return
        self._set(s, state="suggesting", detail="gx-auto is turning the measurements into settings")
        facts = {"measured_by_signal_processing": _measured_summary(measured or {})}
        if understanding:
            facts["heard_by_ace_step_model"] = {k: understanding.get(k) for k in
                                                ("caption", "genres", "vocals_detected", "language")}
        result = self.ai.suggest(facts, hint=s["hint"], user=s["user"])
        settings = result["settings"]
        sources = {k: "inferred" for k in settings if k not in ("seed", "notes")}
        tempo = (measured or {}).get("tempo") or {}
        if tempo.get("bpm") and (tempo.get("stability") or 0) >= 0.5:
            settings["bpm"] = int(round(tempo["bpm"]))
            sources["bpm"] = "measured"
        key = ((measured or {}).get("key") or {}).get("value")
        if key:
            settings["key"] = key
            sources["key"] = "measured"
        meter = ((measured or {}).get("time_signature") or {}).get("value")
        if meter in ("3/4", "4/4"):
            settings["time_signature"] = meter
            sources["time_signature"] = "measured"
        dur = (measured or {}).get("duration_s")
        if isinstance(dur, (int, float)) and 10 <= dur <= 600:
            settings["duration"] = round(float(dur))
            sources["duration"] = "measured"
        if understanding:
            if understanding["vocals_detected"]:
                sources["instrumental"] = "model"
                settings["instrumental"] = False
                if understanding.get("language") in LANGUAGES:
                    settings["vocal_language"] = understanding["language"]
                    sources["vocal_language"] = "model"
            else:
                settings["instrumental"] = True
                settings["vocal_intent"] = "auto"
                settings["vocal_language"] = ""
                sources["instrumental"] = "model"
        settings["lyrics"] = ""
        settings["seed"] = None
        self._set(s, suggestions={"settings": settings, "notes": settings.get("notes", ""), "model": result["model"]},
                  field_sources=sources)

    def _gc(self) -> None:
        now = time.time()
        for sid in [k for k, v in self._sessions.items() if now - v["created_at"] > SESSION_TTL]:
            del self._sessions[sid]
        while len(self._sessions) >= MAX_SESSIONS:
            oldest = min(self._sessions.values(), key=lambda v: v["created_at"])
            del self._sessions[oldest["id"]]
