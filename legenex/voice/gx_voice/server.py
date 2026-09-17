"""HTTP ingress (stdlib). The ONLY network surface of gx-voice.

    GET    /health                                    open: state, memory (D-038), queue
    GET    /v1/models                                 OpenAI model list (gx-voice)
    POST   /v1/audio/speech                           OpenAI-compatible text-to-speech (audio bytes)
    GET    /v1/voice/model                            identity, variants, speakers, limits, lifecycle
    POST   /v1/voice/load    {variant?}               explicit lifecycle
    POST   /v1/voice/unload  {if_idle?}
    POST   /v1/voice/jobs                             submit (tts | voice_design | voice_clone | dialogue)
    GET    /v1/voice/jobs?status=&limit=
    GET    /v1/voice/jobs/{vox-…}
    GET    /v1/voice/jobs/{vox-…}/content?take=&format=   (Range supported)
    POST   /v1/voice/jobs/{vox-…}/cancel
    DELETE /v1/voice/jobs/{vox-…}
    POST   /v1/voice/references                       raw audio body -> reference clip
    GET    /v1/voice/references/{ref-…}
    DELETE /v1/voice/references/{ref-…}
    GET    /v1/voice/voices                           saved-voice replica
    PUT    /v1/voice/voices/{vc_…}                    upsert (gx10-01 is the source of truth)
    DELETE /v1/voice/voices/{vc_…}
    GET    /v1/voice/events?limit=
"""

from __future__ import annotations

import hmac
import json
import logging
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__
from . import validation as v
from .errors import AuthError, NotFoundError, TooLargeError, ValidationError, VoiceError
from .service import VoiceService

log = logging.getLogger("gx_voice.http")
access = logging.getLogger("gx_voice.access")

JSON_LIMIT = 512 * 1024
JOB_RE = re.compile(r"^/v1/voice/jobs/(vox-[0-9a-f]{32})(/content|/cancel)?$")
REF_RE = re.compile(r"^/v1/voice/references/(ref-[0-9a-f]{32})$")
VOICE_RE = re.compile(r"^/v1/voice/voices/(vc_[0-9a-f]{24})$")


class Handler(BaseHTTPRequestHandler):
    server_version = f"gx-voice/{__version__}"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    service: VoiceService
    api_key: str

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: A003
        return

    # ----------------------------------------------------------- output --
    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, val in (extra or {}).items():
            self.send_header(k, val)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self._status = status

    def _json(self, status: int, payload: object, extra: dict | None = None) -> None:
        self._send(status, json.dumps(payload, separators=(",", ":")).encode(), "application/json", extra)

    def _error(self, exc: VoiceError) -> None:
        extra = {"Retry-After": "30"} if exc.status == 503 else None
        self._json(exc.status, exc.payload(), extra)

    # ------------------------------------------------------------ input --
    def _auth(self) -> None:
        header = self.headers.get("Authorization", "")
        token = header[7:] if header.startswith("Bearer ") else ""
        if not token or not hmac.compare_digest(token.encode(), self.api_key.encode()):
            raise AuthError("missing or invalid API key")

    def _read(self, limit: int) -> bytes:
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            raise ValidationError("chunked bodies are not supported; send Content-Length")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValidationError("invalid Content-Length") from exc
        if length < 0:
            raise ValidationError("invalid Content-Length")
        if length > limit:
            self.close_connection = True
            if length <= limit + (64 << 20):
                remaining = length  # drain, so the client reads the 413 instead of a reset
                while remaining > 0:
                    chunk = self.rfile.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
            raise TooLargeError(f"request body larger than {max(1, limit // 1048576)} MB")
        return self.rfile.read(length) if length else b""

    def _body(self, optional: bool = False) -> dict:
        raw = self._read(JSON_LIMIT)
        if optional and not raw:
            return {}
        ctype = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise ValidationError("Content-Type must be application/json")
        try:
            body = json.loads(raw or b"{}")
        except ValueError as exc:
            raise ValidationError("body is not valid JSON") from exc
        if not isinstance(body, dict):
            raise ValidationError("body must be a JSON object")
        return body

    def _query(self) -> dict[str, str]:
        return {k: val[0] for k, val in urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).items()}

    @staticmethod
    def _qint(q: dict, key: str, default: int, lo: int, hi: int) -> int:
        try:
            return max(lo, min(hi, int(q.get(key, default))))
        except ValueError as exc:
            raise ValidationError(f"'{key}' must be an integer") from exc

    # ---------------------------------------------------------- routing --
    def _dispatch(self) -> None:
        self._t0 = time.time()
        self._status = 0
        path = urllib.parse.urlsplit(self.path).path
        try:
            if self.command in ("GET", "HEAD") and path == "/health":
                return self._json(200, self.service.health())
            self._auth()
            handler = self._route(path)
            if handler is None:
                raise NotFoundError(f"no route for {self.command} {path}")
            handler()
        except VoiceError as exc:
            self.close_connection = True
            if exc.status >= 500:
                log.error("%s %s -> %s %s", self.command, path, exc.code, exc.message)
            self._error(exc)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:  # noqa: BLE001
            self.close_connection = True
            log.exception("unhandled error on %s %s", self.command, path)
            if not self._status:
                self._error(VoiceError("internal error"))
        finally:
            access.info(json.dumps({"method": self.command, "path": path, "status": self._status,
                                    "ms": round((time.time() - self._t0) * 1000, 1),
                                    "client": self.client_address[0]}))

    def _route(self, path: str):  # noqa: C901 - flat routing table
        s, m = self.service, self.command
        if m == "HEAD":
            m = "GET"
        simple = {
            ("GET", "/v1/models"): lambda: self._json(200, {"object": "list", "data": [
                {"id": "gx-voice", "object": "model", "created": 0, "owned_by": "gx-cluster"}]}),
            ("POST", "/v1/audio/speech"): self._speech,
            ("GET", "/v1/voice/model"): lambda: self._json(200, s.model_info()),
            ("POST", "/v1/voice/load"): lambda: self._json(200, s.load(self._body(optional=True).get("variant"))),
            ("POST", "/v1/voice/unload"): lambda: self._json(
                200, s.unload(if_idle=self._body(optional=True).get("if_idle") is True)),
            ("POST", "/v1/voice/jobs"): self._submit,
            ("GET", "/v1/voice/jobs"): self._jobs,
            ("POST", "/v1/voice/references"): self._reference,
            ("GET", "/v1/voice/voices"): lambda: self._json(200, {"data": s.store.list_voices()}),
            ("GET", "/v1/voice/events"): lambda: self._json(
                200, {"data": s.store.events(self._qint(self._query(), "limit", 100, 1, 1000))}),
        }
        if (m, path) in simple:
            return simple[(m, path)]
        if rm := REF_RE.match(path):
            ref_id = rm.group(1)
            if m == "GET":
                return lambda: self._json(200, s.get_reference(ref_id))
            if m == "DELETE":
                return lambda: self._json(200, s.delete_reference(ref_id))
            return None
        if vm := VOICE_RE.match(path):
            voice_id = vm.group(1)
            if m == "PUT":
                return lambda: self._json(200, s.put_voice(voice_id, self._body()))
            if m == "DELETE":
                return lambda: self._json(200, s.delete_voice(voice_id))
            if m == "GET":
                return lambda: self._json(200, s.store.get_voice(voice_id) or _missing("voice"))
            return None
        jm = JOB_RE.match(path)
        if not jm:
            return None
        job_id, sub = jm.group(1), jm.group(2)
        if sub is None and m == "GET":
            return lambda: self._json(200, s.job_view(job_id))
        if sub is None and m == "DELETE":
            return lambda: (s.delete(job_id), self._json(200, {"id": job_id, "deleted": True}))
        if sub == "/cancel" and m == "POST":
            return lambda: self._json(200, s.cancel(job_id))
        if sub == "/content" and m == "GET":
            return lambda: self._content(job_id)
        return None

    # --------------------------------------------------------- handlers --
    def _submit(self) -> None:
        job = self.service.submit(self._body())
        self._json(202, job, {"Location": job["links"]["self"]})

    def _jobs(self) -> None:
        q = self._query()
        status = q.get("status")
        if status and not re.fullmatch(r"[a-z_]{3,32}", status):
            raise ValidationError("invalid status filter")
        self._json(200, {"data": self.service.list_jobs(status, self._qint(q, "limit", 50, 1, 500))})

    def _reference(self) -> None:
        data = self._read(self.service.cfg.max_upload_bytes)
        name = urllib.parse.unquote(self.headers.get("X-Filename", ""))[:200]
        self._json(201, self.service.add_reference(data, name))

    def _speech(self) -> None:
        path, ctype, job_id, timings = self.service.speech(self._body())
        data = path.read_bytes()
        if ctype.startswith("audio/L16"):
            from .audio import read_pcm16
            data = read_pcm16(path)[1]
        self.service.discard_audio(job_id)
        self._send(200, data, ctype, {"X-GX-Job": job_id, "X-GX-Generate-Seconds": str(timings.get("generate_s", "")),
                                      "X-GX-Audio-Seconds": str(timings.get("audio_s", ""))})

    def _content(self, job_id: str) -> None:
        q = self._query()
        fmt = q.get("format", "wav")
        take = self._qint(q, "take", 0, 0, v.MAX_TAKES - 1)
        path, ctype = self.service.content_path(job_id, take, fmt)
        if fmt == "pcm":
            from .audio import read_pcm16
            data = read_pcm16(path)[1]
            return self._send(200, data, ctype, {"Content-Disposition": f'attachment; filename="{job_id}-{take}.pcm"'})
        size = path.stat().st_size
        start, end, status = 0, size - 1, 200
        rng = self.headers.get("Range", "")
        mm = re.fullmatch(r"bytes=(\d*)-(\d*)", rng.strip()) if rng else None
        if mm and (mm.group(1) or mm.group(2)):
            if mm.group(1):
                start = int(mm.group(1))
                end = min(int(mm.group(2)), size - 1) if mm.group(2) else size - 1
            else:
                start = max(0, size - int(mm.group(2)))
            if start > end or start >= size:
                return self._send(416, b"", ctype, {"Content-Range": f"bytes */{size}"})
            status = 206
        length = end - start + 1
        ext = path.suffix.lstrip(".")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Disposition", f'attachment; filename="{job_id}-{take}.{ext}"')
        self.send_header("Cache-Control", "private, max-age=3600")
        self.send_header("X-Content-Type-Options", "nosniff")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        self._status = status
        if self.command == "HEAD":
            return
        with path.open("rb") as fh:
            fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fh.read(min(1 << 20, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = _dispatch  # noqa: N815


def _missing(what: str) -> dict:
    raise NotFoundError(f"no such {what}")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def build_servers(service: VoiceService, api_key: str, binds: tuple[str, ...], port: int) -> list[Server]:
    handler = type("BoundHandler", (Handler,), {"service": service, "api_key": api_key})
    return [Server((b, port), handler) for b in binds]


def serve_forever(servers: list[Server]) -> None:
    threads = [threading.Thread(target=srv.serve_forever, daemon=True, name=f"http-{srv.server_address[0]}")
               for srv in servers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
