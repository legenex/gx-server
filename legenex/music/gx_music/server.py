"""HTTP ingress (stdlib). The ONLY network surface of gx-music.

    GET    /health                                  unauthenticated liveness
    GET    /v1/music/model                          identity, capabilities, lifecycle, memory
    GET    /v1/music/tags?q=&limit=                 style-tag suggestions (model vocabulary)
    POST   /v1/music/load | /v1/music/unload        explicit lifecycle
    POST   /v1/music/generations                    text2music (+ optional reference audio)
    POST   /v1/music/remix                          cover of an earlier track or an upload
    POST   /v1/music/edits                          repaint a time range
    POST   /v1/music/extend                         outpaint before/after a track
    POST   /v1/music/uploads                        raw audio body -> upload id
    GET    /v1/music/uploads/{id}
    GET    /v1/music/jobs?status=&limit=
    GET    /v1/music/events?limit=
    GET    /v1/music/{job_id}
    GET    /v1/music/{job_id}/lineage
    GET    /v1/music/{job_id}/content?index=&format=wav|flac|mp3   (Range supported)
    POST   /v1/music/{job_id}/cancel
    DELETE /v1/music/{job_id}
"""

from __future__ import annotations

import bisect
import hmac
import json
import logging
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__
from .errors import AuthError, MusicError, NotFoundError, TooLargeError, ValidationError
from .service import MusicService

log = logging.getLogger("gx_music.http")
access = logging.getLogger("gx_music.access")

JSON_LIMIT = 256 * 1024
JOB_RE = re.compile(r"^/v1/music/(mus-[a-f0-9]{32})(/lineage|/content|/cancel)?$")
UPLOAD_RE = re.compile(r"^/v1/music/uploads/(upl-[a-f0-9]{32})$")

# Curated, model-legitimate descriptor groups for the tag picker. Every entry
# is a plain descriptor the ACE-Step caption format accepts; the full model
# vocabulary is searchable through ?q=.
TAG_GROUPS = {
    "genre": ["pop", "rock", "hip hop", "rap", "r&b", "soul", "funk", "jazz", "blues", "country", "folk",
              "electronic", "house", "techno", "trance", "drum and bass", "dubstep", "ambient", "lo-fi",
              "synthwave", "metal", "punk", "indie", "classical", "orchestral", "cinematic", "reggae",
              "latin", "k-pop", "j-pop", "afrobeats", "gospel", "trap", "edm", "disco"],
    "mood": ["happy", "uplifting", "energetic", "chill", "relaxing", "melancholic", "sad", "dark",
             "romantic", "epic", "dreamy", "nostalgic", "aggressive", "peaceful", "mysterious", "playful"],
    "instrument": ["piano", "acoustic guitar", "electric guitar", "bass guitar", "drums", "808",
                   "synthesizer", "strings", "violin", "cello", "brass", "trumpet", "saxophone", "flute",
                   "organ", "harp", "choir", "pads", "percussion"],
    "vocal": ["female vocals", "male vocals", "duet", "choir vocals", "rap vocals", "falsetto",
              "breathy vocals", "powerful vocals", "whispered vocals", "harmonies", "instrumental"],
    "production": ["lo-fi", "hi-fi", "analog", "warm", "punchy", "reverb", "distorted", "minimal",
                   "layered", "live recording", "studio quality", "vinyl crackle", "sidechain"],
    "tempo": ["slow tempo", "mid tempo", "fast tempo", "half-time", "driving beat", "groovy"],
    "era": ["60s", "70s", "80s", "90s", "2000s", "retro", "vintage", "modern", "futuristic"],
}


class TagIndex:
    def __init__(self, path: Path) -> None:
        try:
            words = {w.strip() for w in path.read_text(encoding="utf-8").splitlines() if w.strip()}
        except OSError:
            words = set()
        for group in TAG_GROUPS.values():
            words.update(group)
        self.sorted = sorted(words, key=str.lower)
        self.lower = [w.lower() for w in self.sorted]

    def search(self, q: str, limit: int) -> list[str]:
        q = q.strip().lower()
        if not q:
            return []
        i = bisect.bisect_left(self.lower, q)
        out = []
        while i < len(self.lower) and self.lower[i].startswith(q) and len(out) < limit:
            out.append(self.sorted[i])
            i += 1
        if len(out) < limit:
            for w, lw in zip(self.sorted, self.lower):
                if len(out) >= limit:
                    break
                if q in lw and not lw.startswith(q):
                    out.append(w)
        return out


class Handler(BaseHTTPRequestHandler):
    server_version = f"gx-music/{__version__}"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    service: MusicService
    api_key: str
    tags: TagIndex

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: A003
        return  # replaced by structured access logging

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

    def _error(self, exc: MusicError) -> None:
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
            raise ValidationError("chunked uploads are not supported; send Content-Length")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValidationError("invalid Content-Length") from exc
        if length < 0:
            raise ValidationError("invalid Content-Length")
        if length > limit:
            self.close_connection = True
            if length <= limit + (32 << 20):
                # Drain so the client sees the 413 instead of a reset.
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
            raise TooLargeError(f"request body larger than {limit // 1048576 or 1} MB")
        return self.rfile.read(length) if length else b""

    def _body(self) -> dict:
        ctype = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        raw = self._read(JSON_LIMIT)
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
        return {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).items()}

    @staticmethod
    def _int(q: dict, key: str, default: int, lo: int, hi: int) -> int:
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
                return self._json(200, {**self.service.health(), "version": __version__})
            self._auth()
            handler = self._route(path)
            if handler is None:
                raise NotFoundError(f"no route for {self.command} {path}")
            handler()
        except MusicError as exc:
            self.close_connection = True  # the body may be unread
            if exc.status >= 500:
                log.error("%s %s -> %s %s", self.command, path, exc.code, exc.message)
            self._error(exc)
        except Exception:  # noqa: BLE001
            self.close_connection = True
            log.exception("unhandled error on %s %s", self.command, path)
            self._error(MusicError("internal error"))
        finally:
            access.info(json.dumps({
                "method": self.command, "path": path, "status": self._status,
                "ms": round((time.time() - self._t0) * 1000, 1), "client": self.client_address[0]}))

    def _route(self, path: str):  # noqa: C901 - flat routing table
        s, m = self.service, self.command
        simple = {
            ("GET", "/v1/music/model"): lambda: self._json(200, s.model_info()),
            ("GET", "/v1/music/tags"): self._tags,
            ("POST", "/v1/music/load"): lambda: self._json(200, s.load()),
            ("POST", "/v1/music/unload"): lambda: self._json(200, s.unload()),
            ("POST", "/v1/music/generations"): lambda: self._submit("generate"),
            ("POST", "/v1/music/remix"): lambda: self._submit("remix"),
            ("POST", "/v1/music/edits"): lambda: self._submit("edit"),
            ("POST", "/v1/music/extend"): lambda: self._submit("extend"),
            ("POST", "/v1/music/uploads"): self._upload,
            ("GET", "/v1/music/jobs"): self._jobs,
            ("GET", "/v1/music/events"): lambda: self._json(
                200, {"data": s.store.events(self._int(self._query(), "limit", 100, 1, 1000))}),
        }
        if m == "HEAD" and (m, path) not in simple:
            m = "GET"
        if (m, path) in simple:
            return simple[(m, path)]
        um = UPLOAD_RE.match(path)
        if um and m == "GET":
            return lambda: self._json(200, s.get_upload(um.group(1)))
        jm = JOB_RE.match(path)
        if not jm:
            return None
        job_id, sub = jm.group(1), jm.group(2)
        if sub is None and m == "GET":
            return lambda: self._json(200, s.job_view(job_id))
        if sub is None and m == "DELETE":
            return lambda: (s.delete(job_id), self._json(200, {"id": job_id, "deleted": True}))
        if sub == "/lineage" and m == "GET":
            return lambda: self._json(200, s.lineage(job_id))
        if sub == "/cancel" and m == "POST":
            return lambda: self._json(200, s.cancel(job_id))
        if sub == "/content" and m in ("GET", "HEAD"):
            return lambda: self._content(job_id)
        return None

    # --------------------------------------------------------- handlers --
    def _submit(self, operation: str) -> None:
        job = self.service.submit(operation, self._body())
        self._json(202, job, {"Location": job["links"]["self"]})

    def _jobs(self) -> None:
        q = self._query()
        status = q.get("status")
        if status and not re.fullmatch(r"[a-z_]{3,32}", status):
            raise ValidationError("invalid status filter")
        self._json(200, {"data": self.service.list_jobs(status, self._int(q, "limit", 50, 1, 500))})

    def _tags(self) -> None:
        q = self._query()
        limit = self._int(q, "limit", 20, 1, 100)
        query = q.get("q", "")[:48]
        self._json(200, {"groups": TAG_GROUPS, "suggestions": self.tags.search(query, limit) if query else []})

    def _upload(self) -> None:
        data = self._read(self.service.cfg.max_upload_bytes)
        name = urllib.parse.unquote(self.headers.get("X-Filename", ""))[:200]
        self._json(201, self.service.add_upload(data, name))

    def _content(self, job_id: str) -> None:
        q = self._query()
        fmt = q.get("format", "mp3")
        index = self._int(q, "index", 0, 0, 7)
        path, ctype = self.service.content_path(job_id, index, fmt)
        size = path.stat().st_size
        start, end, status = 0, size - 1, 200
        rng = self.headers.get("Range", "")
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng.strip()) if rng else None
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            if start > end or start >= size:
                self._send(416, b"", ctype, {"Content-Range": f"bytes */{size}"})
                return
            status = 206
        length = end - start + 1
        fname = f"{job_id}-{index}.{fmt}"
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
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

    do_GET = do_POST = do_DELETE = do_HEAD = _dispatch  # noqa: N815


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def build_servers(service: MusicService, api_key: str, binds: tuple[str, ...], port: int) -> list[Server]:
    tags = TagIndex(Path(__file__).with_name("genres_vocab.txt"))
    handler = type("BoundHandler", (Handler,), {"service": service, "api_key": api_key, "tags": tags})
    return [Server((b, port), handler) for b in binds]


def serve_forever(servers: list[Server]) -> None:
    threads = [threading.Thread(target=s.serve_forever, daemon=True, name=f"http-{s.server_address[0]}")
               for s in servers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
