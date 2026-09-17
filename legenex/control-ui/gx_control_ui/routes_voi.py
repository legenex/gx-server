"""Build V3 VOI routes: Voice Studio (session) and the public voice API.

Registered by importing this module at the end of server.py. Session routes
(`/api/voice/*`) inherit authentication, CSRF and the same-origin check. The
public API (`/v1/voice/*`) is authenticated with a gateway (LiteLLM virtual)
key that allows `gx-voice`, never accepts a session cookie, and only ever
shows a key the jobs it created.
"""

from __future__ import annotations

import json
import re
import urllib.parse

from .media_library import LibraryError
from .redact import redact
from .server import MAX_BODY, Handler, _q, public_api, read_upload, route
from .voice import JOB_ID, VoiceError

VOICE_PATH = r"(?P<voice_id>vc_[0-9a-f]{24}|preset:[a-z_]{2,16})"
JOB_PATH = r"(?P<job_id>vj_[0-9a-f]{32})"
MAX_REFERENCE_UPLOAD = 32 * 1024 * 1024


def _via(h: Handler) -> str:
    return "playground" if h.via_playground() else "control-center"


def _user(h: Handler) -> str:
    assert h.session is not None  # noqa: S101 - guaranteed for session routes
    return h.session.username


# ================================================================ session
@route("GET", r"/api/voice/model")
def api_voice_model(h: Handler) -> None:
    info = h.app.voice.model()
    reg = (h.app.manager.registry().get("aliases") or {}).get("gx-voice") or {}
    last = h.app.results.get("gx-voice").get("inference") or {}
    h._json(200, {**info, "registry": reg, "last_result": last or None,
                  "gateway_url": h.app.cfg.public_gateway_url.rstrip("/"),
                  "playground_api": "/v1/voice"})


@route("POST", r"/api/voice/(?P<op>load|unload)")
def api_voice_lifecycle(h: Handler, op: str) -> None:
    if h.via_playground():
        raise VoiceError("runtime controls live in the Control Center (Resource Control)", 403, "forbidden")
    body = h._body(MAX_BODY)
    h._json(200, h.app.voice.lifecycle(op, user=_user(h), if_idle=body.get("if_idle") is True,
                                       variant=body.get("variant")))


@route("GET", r"/api/voice/voices")
def api_voice_voices(h: Handler) -> None:
    h._json(200, {"voices": h.app.voice.list_voices(include_presets=_q(h, "presets", "1") != "0")})


@route("POST", r"/api/voice/voices")
def api_voice_create(h: Handler) -> None:
    h._json(201, h.app.voice.create_voice(h._body(MAX_BODY), user=_user(h), ip=h._client_ip(), via=_via(h)))


@route("GET", rf"/api/voice/voices/{VOICE_PATH}")
def api_voice_get(h: Handler, voice_id: str) -> None:
    h._json(200, h.app.voice.get_voice(voice_id))


@route("POST", rf"/api/voice/voices/{VOICE_PATH}")
def api_voice_update(h: Handler, voice_id: str) -> None:
    h._json(200, h.app.voice.update_voice(voice_id, h._body(MAX_BODY), user=_user(h), ip=h._client_ip()))


@route("POST", rf"/api/voice/voices/{VOICE_PATH}/delete")
def api_voice_delete(h: Handler, voice_id: str) -> None:
    if h._body(MAX_BODY).get("confirm") is not True:
        raise ValueError("delete must be confirmed")
    h._json(200, h.app.voice.delete_voice(voice_id, user=_user(h), ip=h._client_ip()))


@route("GET", rf"/api/voice/voices/{VOICE_PATH}/versions")
def api_voice_versions(h: Handler, voice_id: str) -> None:
    h._json(200, {"versions": h.app.voice.voice_versions(voice_id)})


@route("GET", r"/api/voice/jobs")
def api_voice_jobs(h: Handler) -> None:
    status = _q(h, "status") or None
    if status and not re.fullmatch(r"[a-z_]{3,32}", status):
        raise ValueError("invalid status filter")
    voice_id = _q(h, "voice_id") or None
    if voice_id and not re.fullmatch(r"vc_[0-9a-f]{24}|preset:[a-z_]{2,16}", voice_id):
        raise ValueError("invalid voice id")
    h._json(200, {"jobs": h.app.voice.list_jobs(limit=int(_q(h, "limit", "50") or 50), status=status,
                                                voice_id=voice_id)})


@route("POST", r"/api/voice/jobs")
def api_voice_submit(h: Handler) -> None:
    h._json(202, h.app.voice.submit(h._body(MAX_BODY), user=_user(h), via=_via(h), ip=h._client_ip()))


@route("GET", rf"/api/voice/jobs/{JOB_PATH}")
def api_voice_job(h: Handler, job_id: str) -> None:
    h._json(200, h.app.voice.get(job_id))


@route("POST", rf"/api/voice/jobs/{JOB_PATH}/cancel")
def api_voice_cancel(h: Handler, job_id: str) -> None:
    h._json(200, h.app.voice.cancel(job_id, user=_user(h)))


@route("POST", rf"/api/voice/jobs/{JOB_PATH}/delete")
def api_voice_job_delete(h: Handler, job_id: str) -> None:
    if h._body(MAX_BODY).get("confirm") is not True:
        raise ValueError("delete must be confirmed")
    h._json(200, h.app.voice.delete_job(job_id, user=_user(h)))


@route("GET", rf"/api/voice/jobs/{JOB_PATH}/takes/(?P<take>[0-3])/audio")
def api_voice_take_audio(h: Handler, job_id: str, take: str) -> None:
    fmt = _q(h, "format", "wav")
    path = h.app.voice.take_file(job_id, int(take), fmt)
    extra = {"Cache-Control": "private, max-age=3600"}
    if _q(h, "download") == "1":
        extra["Content-Disposition"] = f'attachment; filename="{job_id}-take{int(take) + 1}.{fmt}"'
    h._send_file(path, "audio/wav" if fmt == "wav" else "audio/mpeg", extra)


@route("POST", rf"/api/voice/jobs/{JOB_PATH}/takes/(?P<take>[0-3])/save")
def api_voice_take_save(h: Handler, job_id: str, take: str) -> None:
    body = h._body(MAX_BODY)
    title = body.get("title")
    if title is not None and (not isinstance(title, str) or len(title) > 200):
        raise ValueError("title must be text (at most 200 characters)")
    h._json(200, h.app.voice.save_take(job_id, int(take), user=_user(h), title=(title or "").strip() or None))


@route("POST", r"/api/voice/upload")
def api_voice_upload(h: Handler) -> None:
    ctype = (h.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if not ctype.startswith("audio/"):
        raise ValueError("upload an audio file (WAV, FLAC, MP3, OGG, WebM or M4A)")
    filename = urllib.parse.unquote(h.headers.get("X-Filename") or "")[:120]
    title = urllib.parse.unquote(h.headers.get("X-Title") or "")[:200] or None
    tmp = read_upload(h, MAX_REFERENCE_UPLOAD)
    try:
        asset = h.app.voice.upload_reference(tmp.read_bytes(), filename or "reference", ctype, title=title,
                                             user=_user(h))
    finally:
        tmp.unlink(missing_ok=True)
    h._json(200, asset)


# ============================================================= public API
def _api_error(h: Handler, status: int, message: str, code: str) -> None:
    h._json(status, {"error": {"message": redact(message), "code": code, "retryable": status in (429, 503)}})


def _public_job(job: dict) -> dict:
    out = {k: val for k, val in job.items() if k not in ("user", "via", "imported", "waiting")}
    out["waiting"] = {k: job["waiting"].get(k) for k in ("code", "reason", "since")} if job.get("waiting") else None
    jid = out["id"]
    for t in out.get("takes") or []:
        t["files"] = {fmt: f"/v1/voice/jobs/{jid}/takes/{t['index']}/content?format={fmt}"
                      for fmt in t.pop("formats", [])}
        t.pop("audio_url", None)
    out["links"] = {"self": f"/v1/voice/jobs/{jid}"}
    return out


_PUBLIC_JOB = re.compile(r"^/v1/voice/jobs/(vj_[0-9a-f]{32})(/cancel|/takes/([0-3])/(content|save))?$")
_PUBLIC_VOICE = re.compile(r"^/v1/voice/voices/(vc_[0-9a-f]{24}|preset:[a-z_]{2,16})$")
_PUBLIC_OPS = {"/v1/voice/speech": "tts", "/v1/voice/design": "voice_design", "/v1/voice/clone": "voice_clone",
               "/v1/voice/dialogue": "dialogue"}


def _public_voice(v: dict) -> dict:
    return {k: v.get(k) for k in ("id", "name", "kind", "builtin", "description", "speaker", "instructions",
                                  "language", "style", "reference_asset_id", "version", "created_at",
                                  "native_language")}


@public_api("/v1/voice")
def public_voice(h: Handler, method: str, path: str) -> None:  # noqa: C901 - flat routing
    """`/v1/voice/*` for API clients (gateway key with gx-voice allowed)."""
    from .routes_v2 import _key_identity, _rate_ok

    if h._body_error is not None and path != "/v1/voice/uploads":
        raise h._body_error
    ident = _key_identity(h)
    if ident is None:
        _api_error(h, 401, "missing or invalid API key (create one in the Control Center > API Keys)",
                   "unauthorized")
        return
    models = ident.get("models")
    if models is None:  # an older identity cache entry
        models = []
    if models and "gx-voice" not in models and "all-proxy-models" not in models:
        _api_error(h, 403, "this key does not allow gx-voice", "forbidden")
        return
    if not _rate_ok(h, "voice:" + ident["key"]):
        _api_error(h, 429, "rate limit: at most 120 voice API requests per minute per key", "rate_limited")
        return
    user = f"key:{ident['name']}"
    owner = f"key:{ident['key']}"
    voice = h.app.voice

    def own(job_id: str) -> dict:
        row = voice._job_row(job_id)  # noqa: SLF001 - same package
        if row["owner"] != owner:
            raise VoiceError("no such voice job for this key", 404, "not_found")
        return row

    try:
        if method == "GET" and path == "/v1/voice/model":
            info = voice.model()
            h._json(200, {k: info.get(k) for k in ("alias", "task", "node", "family", "licence", "variants",
                                                     "tokenizer", "speakers", "languages", "limits", "output",
                                                     "state")})
        elif method == "GET" and path == "/v1/voice/voices":
            h._json(200, {"data": [_public_voice(v) for v in voice.list_voices()]})
        elif method == "POST" and path == "/v1/voice/voices":
            body = h._body(MAX_BODY)
            created = voice.create_voice(body, user=user, ip=h._client_ip(), via="api")
            h._json(201, _public_voice(created))
        elif (m := _PUBLIC_VOICE.match(path)) and method == "GET":
            h._json(200, _public_voice(voice.get_voice(m.group(1))))
        elif method == "GET" and path == "/v1/voice/jobs":
            limit = int(_q(h, "limit", "50") or 50)
            h._json(200, {"data": [_public_job(j) for j in voice.list_jobs(limit=limit, owner=owner)]})
        elif method == "POST" and path == "/v1/voice/uploads":
            if h._body_error is not None:
                raise h._body_error
            ctype = (h.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            tmp = read_upload(h, MAX_REFERENCE_UPLOAD)
            try:
                asset = voice.upload_reference(tmp.read_bytes(),
                                               urllib.parse.unquote(h.headers.get("X-Filename") or "")[:120],
                                               ctype, user=user)
            finally:
                tmp.unlink(missing_ok=True)
            h._json(201, {"asset_id": asset["id"], "duration_s": asset.get("duration"), "type": asset["type"]})
        elif method == "POST" and path in _PUBLIC_OPS:
            if not _rate_ok(h, "voice-submit:" + ident["key"], 30):
                _api_error(h, 429, "at most 30 voice jobs per minute per key", "rate_limited")
                return
            body = h._body(MAX_BODY)
            if "operation" in body and body["operation"] != _PUBLIC_OPS[path]:
                raise VoiceError(f"{path} runs {_PUBLIC_OPS[path]}; leave out 'operation'")
            body["operation"] = _PUBLIC_OPS[path]
            job = voice.submit(body, user=user, via="api", ip=h._client_ip(), owner=owner)
            h._json(202, _public_job(job), {"Location": f"/v1/voice/jobs/{job['id']}"})
        elif m := _PUBLIC_JOB.match(path):
            job_id, sub, take, action = m.group(1), m.group(2), m.group(3), m.group(4)
            if not JOB_ID.match(job_id):
                raise VoiceError("no such voice job", 404, "not_found")
            own(job_id)
            if sub is None and method == "GET":
                h._json(200, _public_job(voice.get(job_id)))
            elif sub == "/cancel" and method == "POST":
                h._json(200, _public_job(voice.cancel(job_id, user=user)))
            elif action == "content" and method == "GET":
                fmt = _q(h, "format", "wav")
                job = voice.get(job_id)
                if job["status"] != "completed":
                    _api_error(h, 409, "the audio is not ready yet; poll the job", "not_ready")
                    return
                path_ = voice.take_file(job_id, int(take), fmt)
                h._send_file(path_, "audio/wav" if fmt == "wav" else "audio/mpeg",
                             {"Content-Disposition": f'attachment; filename="{job_id}-{take}.{fmt}"',
                              "Cache-Control": "private, max-age=3600"})
            elif action == "save" and method == "POST":
                body = h._body(MAX_BODY)
                asset = voice.save_take(job_id, int(take), user=user, title=body.get("title"))
                h._json(200, {"asset_id": asset["id"], "title": asset.get("title")})
            else:
                _api_error(h, 405, "method not allowed", "method_not_allowed")
        elif method == "POST" and path in ("/v1/voice/load", "/v1/voice/unload"):
            _api_error(h, 403, "model lifecycle is managed by the cluster (Control Center > Resource Control)",
                       "forbidden")
        else:
            _api_error(h, 404, f"no route for {method} {path}", "not_found")
    except VoiceError as exc:
        _api_error(h, exc.status, str(exc), exc.code)
    except LibraryError as exc:
        _api_error(h, exc.status, str(exc), "library")
    except (ValueError, json.JSONDecodeError) as exc:
        _api_error(h, 400, str(exc), "invalid_request")
    except OverflowError as exc:
        _api_error(h, 413, str(exc), "too_large")
