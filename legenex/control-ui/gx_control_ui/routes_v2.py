"""D-037 routes: Resource Control, Storage & Cleanup, client Setup, gx-music,
the GX-Playground summary, and the public music API.

Registered by importing this module at the end of server.py. Session routes
inherit the server's authentication, CSRF and same-origin checks. The public
`/v1/music/*` API is authenticated with a gateway (LiteLLM virtual) key that
allows `gx-music`; it never accepts a session cookie and never exposes the
node-2 key, node-2 paths or the engine.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.parse
from typing import TYPE_CHECKING, Any

from . import setup as client_setup
from .music import JOB_ID, OPERATIONS, MusicError
from .redact import redact
from .resources import GENERATIVE, POLICIES, PROFILE_IDS, ResourceError
from .server import MAX_BODY, Handler, _q, read_upload, route
from .storage import StorageError
from .util import HTTPError, bearer, http_json

if TYPE_CHECKING:  # pragma: no cover
    pass

ALIAS_RE = "gx-(?:mini|fast|reason|image|video|music|max)"


# ============================================================ resources
@route("GET", r"/api/resources")
def api_resources(h: Handler) -> None:
    snap = h.app.resources.snapshot()
    snap["admission"] = {a: h.app.resources.admission(a, snap=snap) for a in GENERATIVE}
    h._json(200, snap)


@route("GET", r"/api/resources/compatibility")
def api_resources_compat(h: Handler) -> None:
    h._json(200, h.app.resources.compatibility())


@route("GET", rf"/api/resources/admission/(?P<alias>{ALIAS_RE})")
def api_resources_admission(h: Handler, alias: str) -> None:
    variant = _q(h, "variant") or None
    if variant not in (None, "edit", "keyframe_edit"):
        raise ValueError("unknown variant")
    h._json(200, h.app.resources.admission(alias, variant=variant))


@route("GET", rf"/api/resources/explain/(?P<alias>{ALIAS_RE})")
def api_resources_explain(h: Handler, alias: str) -> None:
    h._json(200, h.app.resources.explain(alias))


@route("GET", r"/api/resources/profile/plan")
def api_profile_plan(h: Handler) -> None:
    target = _q(h, "to")
    if target not in PROFILE_IDS:
        raise ValueError("unknown profile")
    h._json(200, h.app.resources.plan_profile(target))


@route("POST", r"/api/resources/profile")
def api_profile_set(h: Handler) -> None:
    assert h.session is not None  # noqa: S101
    body = h._body(MAX_BODY)
    target = body.get("profile")
    if target not in PROFILE_IDS:
        raise ValueError("unknown profile")
    source = "playground" if h.via_playground() else "control-center"
    h._json(200, h.app.resources.set_profile(target, user=h.session.username, ip=h._client_ip(),
                                             confirm=body.get("confirm"), source=source))


@route("POST", rf"/api/resources/(?P<alias>{ALIAS_RE})/(?P<op>load|unload|drain|pin|unpin)")
def api_resource_control(h: Handler, alias: str, op: str) -> None:
    assert h.session is not None  # noqa: S101
    if h.via_playground():
        raise ResourceError("runtime controls live in the Control Center (Resource Control)", 403)
    body = h._body(MAX_BODY)
    h._json(202, h.app.resources.control(alias, op, user=h.session.username, ip=h._client_ip(),
                                         confirm=body.get("confirm")))


@route("GET", r"/api/resources/ops/(?P<op_id>[0-9a-f]{16})")
def api_resource_op(h: Handler, op_id: str) -> None:
    h._json(200, h.app.resources.background_job(op_id))


@route("GET", r"/api/resources/summary")
def api_resources_summary(h: Handler) -> None:
    """The simplified widget GX-Playground shows (no infrastructure detail)."""
    h._json(200, resource_summary(h))


def resource_summary(h: Handler) -> dict:
    snap = h.app.resources.snapshot()
    rt = snap["runtimes"]

    def word(alias: str) -> str:
        st = rt[alias]["state"]
        return {"READY": "Ready", "UNLOADED": "Ready", "LOADING": "Loading", "GENERATING": "Working",
                "WAITING": "Waiting", "DRAINING": "Unloading", "BLOCKED": "Paused", "ERROR": "Unavailable"}[st]

    text_states = [rt[a]["state"] for a in ("gx-mini", "gx-fast")]
    text = "Ready" if all(s in ("READY", "GENERATING") for s in text_states) else \
        ("Paused" if "BLOCKED" in text_states else "Loading")
    max_state = {"UNLOADED": "Idle", "LOADING": "Starting", "READY": "Running", "DRAINING": "Releasing"}.get(
        rt["gx-max"]["state"], "Unavailable")
    creative = h.app.media.snapshot()
    music_queue = rt["gx-music"].get("queue") or 0
    return {
        "profile": snap["profile"]["profile"],
        "profile_label": snap["profile"]["label"],
        "profiles": [{"id": p["id"], "label": p["label"], "summary": p["summary"]}
                     for p in snap["profiles"] if p["user_selectable"]],
        "maintenance": snap["maintenance"],
        "rows": [
            {"key": "text", "label": "Text", "status": text, "detail": "gx-mini, gx-fast, gx-reason"},
            {"key": "image", "label": "Image", "status": word("gx-image"), "detail": rt["gx-image"]["detail"]},
            {"key": "video", "label": "Video", "status": word("gx-video"), "detail": rt["gx-video"]["detail"]},
            {"key": "music", "label": "Music", "status": word("gx-music"), "detail": rt["gx-music"]["detail"]},
            {"key": "max", "label": "Max", "status": max_state, "detail": "takes over both nodes"},
        ],
        "queued": sum(v for k, v in creative["counts"].items() if k in ("queued", "waiting")) + int(music_queue),
        "control_center_url": h.app.cfg.public_control_url.rstrip("/") + "/#/resources",
        "generated_at": snap["generated_at"],
    }


# ============================================================== storage
@route("GET", r"/api/storage")
def api_storage(h: Handler) -> None:
    status = h.app.storage.status()
    h._json(200, {"overview": h.app.storage.overview(), "scan": status,
                  "library": h.app.library.usage(),
                  "thresholds": {"critical_gib": 30, "low_gib": 75, "watch_gib": 150}})


@route("POST", r"/api/storage/scan")
def api_storage_scan(h: Handler) -> None:
    assert h.session is not None  # noqa: S101
    if h.via_playground():
        raise StorageError("storage cleanup lives in the Control Center", 403)
    h._json(202, h.app.storage.start_scan(user=h.session.username))


@route("POST", r"/api/storage/plan")
def api_storage_plan(h: Handler) -> None:
    ids = h._body(MAX_BODY).get("ids")
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise ValueError("ids must be a list of candidate ids")
    h._json(200, h.app.storage.plan(ids))


@route("POST", r"/api/storage/clean")
def api_storage_clean(h: Handler) -> None:
    assert h.session is not None  # noqa: S101
    if h.via_playground():
        raise StorageError("storage cleanup lives in the Control Center", 403)
    body = h._body(MAX_BODY)
    ids = body.get("ids")
    if not isinstance(ids, list) or not all(isinstance(i, str) and re.fullmatch(r"c_[0-9a-f]{24}", i)
                                            for i in ids):
        raise ValueError("ids must be candidate ids from the latest scan")
    h._json(200, h.app.storage.clean(ids, user=h.session.username, ip=h._client_ip(),
                                     allow_review=bool(body.get("allow_review")), confirm=body.get("confirm")))


# ================================================================ setup
@route("GET", r"/api/setup")
def api_setup(h: Handler) -> None:
    h._json(200, client_setup.setup_info(h.app.cfg))


@route("POST", r"/api/setup/test")
def api_setup_test(h: Handler) -> None:
    assert h.session is not None  # noqa: S101
    body = h._body(4096)
    client = body.get("client")
    result = client_setup.test_connection(h.app.cfg, str(client), body.get("secret"))
    h.app.actions.audit(user=h.session.username, ip=h._client_ip(), action=f"setup.test.{client}",
                        outcome="ok" if result.get("connected") else "failed")
    h._json(200, result)


# ================================================================ music
@route("GET", r"/api/music/model")
def api_music_model(h: Handler) -> None:
    info = h.app.music.model()
    reg = (h.app.manager.registry().get("aliases") or {}).get("gx-music") or {}
    last = h.app.results.get("gx-music").get("inference") or {}
    h._json(200, {**info, "registry": reg, "last_success": last if last.get("ok") else None,
                  "last_result": last})


@route("GET", r"/api/music/tags")
def api_music_tags(h: Handler) -> None:
    h._json(200, h.app.music.tags(_q(h, "q"), int(_q(h, "limit", "20") or 20)))


@route("GET", r"/api/music/jobs")
def api_music_jobs(h: Handler) -> None:
    status = _q(h, "status") or None
    if status and not re.fullmatch(r"[a-z_]{3,32}", status):
        raise ValueError("invalid status filter")
    h._json(200, {"jobs": h.app.music.list(status, int(_q(h, "limit", "50") or 50))})


@route("POST", r"/api/music/jobs")
def api_music_submit(h: Handler) -> None:
    assert h.session is not None  # noqa: S101
    body = h._body(MAX_BODY)
    operation = body.pop("operation", "generate")
    if operation not in OPERATIONS:
        raise ValueError("operation must be generate, remix, edit or extend")
    h._json(202, h.app.music.submit(str(operation), body, user=h.session.username, via="playground"
                                    if h.via_playground() else "control-center", ip=h._client_ip()))


@route("GET", r"/api/music/jobs/(?P<job_id>mus-[0-9a-f]{32})")
def api_music_job(h: Handler, job_id: str) -> None:
    h._json(200, h.app.music.get(job_id))


@route("GET", r"/api/music/jobs/(?P<job_id>mus-[0-9a-f]{32})/lineage")
def api_music_lineage(h: Handler, job_id: str) -> None:
    h._json(200, h.app.music.lineage(job_id))


@route("POST", r"/api/music/jobs/(?P<job_id>mus-[0-9a-f]{32})/cancel")
def api_music_cancel(h: Handler, job_id: str) -> None:
    assert h.session is not None  # noqa: S101
    h._json(200, h.app.music.cancel(job_id, user=h.session.username))


@route("POST", r"/api/music/upload")
def api_music_upload(h: Handler) -> None:
    assert h.session is not None  # noqa: S101
    ctype = (h.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if not ctype.startswith("audio/"):
        raise ValueError("upload an audio file (WAV, FLAC, MP3, OGG or M4A)")
    filename = urllib.parse.unquote(h.headers.get("X-Filename") or "")[:120]
    title = urllib.parse.unquote(h.headers.get("X-Title") or "")[:200] or None
    tmp = read_upload(h, 64 * 1024 * 1024)
    try:
        asset = h.app.music.upload(tmp.read_bytes(), filename or "upload", ctype, title=title,
                                   user=h.session.username)
    finally:
        tmp.unlink(missing_ok=True)
    h.app.actions.audit(user=h.session.username, ip=h._client_ip(), action="music.upload", outcome="ok",
                        asset=asset["id"])
    h._json(200, asset)


# ================================================== creative dashboard
@route("GET", r"/api/creative/overview")
def api_creative_overview(h: Handler) -> None:
    """GX-Playground dashboard: status, activity and recent creations."""
    lib = h.app.library
    recent = lib.search(limit=12, include_tests=True)
    media_jobs = h.app.media.list()[:30]
    try:
        music_jobs = h.app.music.list(limit=30)
    except MusicError as exc:
        music_jobs = []
        music_error = str(exc)
    else:
        music_error = None
    active = [j for j in media_jobs if j["phase"] not in ("ready", "failed", "cancelled")]
    active += [j for j in music_jobs if j.get("phase") not in ("completed", "failed", "cancelled")]
    errors = [{"kind": j["label"], "message": j["error"], "at": j["ended"]} for j in media_jobs
              if j["phase"] == "failed"][:5]
    errors += [{"kind": f"Music {j.get('operation')}", "message": (j.get("error") or {}).get("message"),
                "at": j.get("finished_at")} for j in music_jobs if j.get("status") == "failed"][:5]
    errors.sort(key=lambda e: e.get("at") or 0, reverse=True)
    summary = resource_summary(h)
    h._json(200, {"resources": summary, "active": active, "recent": recent["items"],
                  "counts": recent["counts"], "errors": errors[:6], "music_error": music_error,
                  "capacity": h.app.library.usage(), "generated_at": time.time()})


# ========================================================== public API
_KEY_RE = re.compile(r"^sk-[A-Za-z0-9_\-]{8,200}$")
_PUBLIC_JOB = re.compile(r"^/v1/music/(mus-[a-f0-9]{32})(/lineage|/content|/cancel)?$")


def _key_identity(h: Handler) -> dict | None:
    """Validate a gateway key with LiteLLM (cached 60 s; negative 10 s)."""
    auth = h.headers.get("Authorization") or ""
    secret = auth[7:].strip() if auth.startswith("Bearer ") else ""
    if not _KEY_RE.match(secret):
        return None
    digest = hashlib.sha256(secret.encode()).hexdigest()
    now = time.time()
    cached = h.app.api_keys_cache.get(digest)
    if cached and cached[0] > now:
        return cached[1]
    ident = None
    try:
        status, data = http_json("GET", f"{h.app.cfg.litellm_base}/key/info", headers=bearer(secret), timeout=8)
        info = (data or {}).get("info") if isinstance(data, dict) else None
        if status == 200 and isinstance(info, dict):
            models = info.get("models") or []
            expired = False
            if info.get("expires"):
                from datetime import UTC, datetime
                try:
                    expired = datetime.fromisoformat(str(info["expires"]).replace("Z", "+00:00")) < datetime.now(UTC)
                except ValueError:
                    expired = False
            if not info.get("blocked") and not expired:
                ident = {"key": digest[:16], "name": info.get("key_alias") or "key",
                         "music": not models or "gx-music" in models or "all-proxy-models" in models}
    except HTTPError:
        ident = None
    if len(h.app.api_keys_cache) > 500:
        h.app.api_keys_cache.clear()
    h.app.api_keys_cache[digest] = (now + (60 if ident else 10), ident)
    return ident


def _rate_ok(h: Handler, key: str, limit: int = 120) -> bool:
    now = time.time()
    hits = [t for t in h.app.api_rate.get(key, []) if now - t < 60]
    if len(hits) >= limit:
        h.app.api_rate[key] = hits
        return False
    hits.append(now)
    h.app.api_rate[key] = hits
    return True


def _api_error(h: Handler, status: int, message: str, code: str) -> None:
    h._json(status, {"error": {"message": redact(message), "code": code, "retryable": status in (429, 503)}})


def _public_job(job: dict) -> dict:
    out = dict(job)
    jid = out.get("id")
    for t in out.get("tracks") or []:
        t["files"] = {fmt: {**meta, "url": f"/v1/music/{jid}/content?index={t['index']}&format={fmt}"}
                      for fmt, meta in (t.get("files") or {}).items()}
    out["links"] = {"self": f"/v1/music/{jid}", "lineage": f"/v1/music/{jid}/lineage",
                    "content": f"/v1/music/{jid}/content"}
    for k in ("submitted_via", "import_error"):
        out.pop(k, None)
    return out


def public_music(h: Handler, method: str, path: str) -> None:  # noqa: C901 - flat routing
    """`/v1/music/*` for API clients (gateway key with gx-music allowed)."""
    if h._body_error is not None and path != "/v1/music/uploads":
        raise h._body_error
    ident = _key_identity(h)
    if ident is None:
        _api_error(h, 401, "missing or invalid API key (create one in the Control Center > API Keys)",
                   "unauthorized")
        return
    if not ident["music"]:
        _api_error(h, 403, "this key does not allow gx-music", "forbidden")
        return
    if not _rate_ok(h, ident["key"]):
        _api_error(h, 429, "rate limit: at most 120 music API requests per minute per key", "rate_limited")
        return
    user = f"key:{ident['name']}"
    music = h.app.music
    owner = f"key:{ident['key']}"

    def own(job_id: str) -> bool:
        with music._lock:  # noqa: SLF001 - same package
            return music._jobs.get(job_id, {}).get("owner") == owner  # noqa: SLF001

    try:
        if method == "GET" and path == "/v1/music/model":
            info = music.model()
            h._json(200, {k: info.get(k) for k in ("alias", "task", "node", "identity", "runtime",
                                                      "capabilities")})
            return
        if method == "GET" and path == "/v1/music/tags":
            h._json(200, music.tags(_q(h, "q"), int(_q(h, "limit", "20") or 20)))
            return
        if method == "GET" and path == "/v1/music/jobs":
            jobs = [j for j in music.list(_q(h, "status") or None, int(_q(h, "limit", "50") or 50))
                    if own(j.get("id", ""))]
            h._json(200, {"data": [_public_job(j) for j in jobs]})
            return
        if method == "POST" and path in ("/v1/music/load", "/v1/music/unload"):
            _api_error(h, 403, "model lifecycle is managed by the cluster (Control Center > Resource Control)",
                       "forbidden")
            return
        if method == "POST" and path == "/v1/music/uploads":
            if h._body_error is not None:
                raise h._body_error
            tmp = read_upload(h, 64 * 1024 * 1024)
            try:
                up = music.client.call("POST", "/v1/music/uploads", raw=tmp.read_bytes(), timeout=180,
                                       headers={"Content-Type": "application/octet-stream",
                                                "X-Filename": h.headers.get("X-Filename") or "upload"})
            finally:
                tmp.unlink(missing_ok=True)
            h._json(201, up)
            return
        op = {"/v1/music/generations": "generate", "/v1/music/remix": "remix", "/v1/music/edits": "edit",
              "/v1/music/extend": "extend"}.get(path)
        if method == "POST" and op:
            if not _rate_ok(h, "submit:" + ident["key"], 20):
                _api_error(h, 429, "at most 20 music jobs per minute per key", "rate_limited")
                return
            job = music.submit(op, h._body(MAX_BODY), user=user, via="api", ip=h._client_ip())
            with music._lock:  # noqa: SLF001
                music._jobs[job["id"]]["owner"] = owner  # noqa: SLF001
            music._save()  # noqa: SLF001
            h._json(202, _public_job(job), {"Location": f"/v1/music/{job['id']}"})
            return
        m = _PUBLIC_JOB.match(path)
        if not m:
            _api_error(h, 404, f"no route for {method} {path}", "not_found")
            return
        job_id, sub = m.group(1), m.group(2)
        if not own(job_id):
            _api_error(h, 404, "no such music job for this key", "not_found")
            return
        if sub is None and method == "GET":
            h._json(200, _public_job(music.get(job_id)))
        elif sub == "/lineage" and method == "GET":
            h._json(200, music.lineage(job_id))
        elif sub == "/cancel" and method == "POST":
            h._json(200, _public_job(music.cancel(job_id, user=user)))
        elif sub == "/content" and method == "GET":
            fmt = _q(h, "format", "mp3")
            if fmt not in ("wav", "flac", "mp3"):
                _api_error(h, 400, "format must be wav, flac or mp3", "invalid_request")
                return
            index = int(_q(h, "index", "0") or 0)
            asset = next((a for a in h.app.library.find_by_job(job_id)
                          if (a.get("settings") or {}).get("track_index") == index), None)
            if asset is None:
                _api_error(h, 409, "the track is not ready yet (generating or being saved); retry shortly",
                           "not_ready")
                return
            file_path = h.app.library.file_path(asset, fmt if fmt != asset["ext"] else None)
            if not file_path.is_file():
                _api_error(h, 404, f"no {fmt} version of this track", "not_found")
                return
            ctype = {"wav": "audio/wav", "flac": "audio/flac", "mp3": "audio/mpeg"}[fmt]
            h._send_file(file_path, ctype, {"Content-Disposition":
                                            f'attachment; filename="{job_id}-{index}.{fmt}"',
                                            "Cache-Control": "private, max-age=3600"})
        elif sub is None and method == "DELETE":
            _api_error(h, 405, "delete tracks in GX-Playground > Library", "method_not_allowed")
        else:
            _api_error(h, 405, "method not allowed", "method_not_allowed")
    except MusicError as exc:
        _api_error(h, exc.status, str(exc), exc.code)
    except (ValueError, json.JSONDecodeError) as exc:
        _api_error(h, 400, str(exc), "invalid_request")
    except OverflowError as exc:
        _api_error(h, 413, str(exc), "too_large")


def aliases_with_policy() -> dict[str, Any]:
    return {a: p.public() for a, p in POLICIES.items()}
