"""Realtime sessions and WebSocket tunnel authorisation (Build V3, Part 11).

    browser/API --WS--> gx-playground /rt/<svc>/<sid>
                           | POST /api/realtime/authorize (loopback + proxy token)
                           v
                        gx-control-ui (this module): who is it, may they open <sid>?
                           | {target, path, authorization}
    gx-playground --WS (fabric)--> gx-call :18840 / gx-live :18850 on gx10-02

The workstreams that own the services (CAL, LIV) create sessions and call
:meth:`RealtimeRegistry.register`; API clients get a one-time ticket from
:meth:`RealtimeRegistry.issue_ticket`. The Playground asks :meth:`authorize`
before it opens anything. The contract is published in
``coordination/build-v3/plt.md`` section 1.

Tickets: ``v1.<sid>.<exp>.<nonce>.<mac>`` where
``mac = HMAC-SHA256(process key, "v1|sid|owner|exp|nonce")``. The owner is not
in the ticket (it is bound through the MAC), the nonce is remembered until the
ticket expires, and the key lives only in this process: a restart invalidates
every ticket, which is fine for a 60-second credential.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import urllib.parse
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

SERVICES = ("call", "live")
SESSION_RE = re.compile(r"^(call|live)_[0-9a-f]{32}$")
UPSTREAM_PATH_RE = re.compile(r"^/[A-Za-z0-9._~/\-]{1,200}(\?[A-Za-z0-9._~=&\-]{0,200})?$")
OWNER_RE = re.compile(r"^(user:[a-z0-9._\-]{1,32}|key:[0-9a-f]{16})$")
TICKET_RE = re.compile(r"^v1\.((?:call|live)_[0-9a-f]{32})\.([0-9]{10})\.([A-Za-z0-9_\-]{16})\.([A-Za-z0-9_\-]{43})$")
TICKET_TTL_S = 60
MAX_SESSION_TTL_S = 4 * 3600
MAX_SESSIONS = 256
MAX_META_BYTES = 2048
DISPOSITIONS = ("completed", "abandoned", "failed", "timeout", "transferred", "expired", "replaced")


class RealtimeError(Exception):
    def __init__(self, message: str, status: int = 400, code: str = "invalid_request") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def new_session_id(service: str) -> str:
    if service not in SERVICES:
        raise RealtimeError("unknown realtime service")
    return f"{service}_{secrets.token_hex(16)}"


def owner_hash(owner: str) -> str:
    return hashlib.sha256(owner.encode()).hexdigest()[:16]


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


@dataclass
class Target:
    host: str
    port: int
    key_file: Path

    @classmethod
    def parse(cls, raw: str, key_file: Path) -> Target:
        host, _, port = raw.rpartition(":")
        if not re.fullmatch(r"(192\.168\.10[01]\.[0-9]{1,3}|127\.0\.0\.1)", host) or not port.isdigit():
            raise ValueError(f"realtime target must be fabric or loopback host:port, not {raw!r}")
        return cls(host, int(port), key_file)


@dataclass
class Session:
    service: str
    session_id: str
    owner: str
    upstream_path: str
    created: float
    expires: float
    meta: dict = field(default_factory=dict)
    ended: float | None = None
    disposition: str | None = None
    connects: int = 0
    last_connect: float | None = None

    def is_active(self, now: float) -> bool:
        return self.ended is None and now < self.expires

    def public(self, now: float) -> dict:
        active = self.is_active(now)
        return {"service": self.service, "session_id": self.session_id, "owner": owner_hash(self.owner),
                "created": self.created, "expires": self.expires, "ended": self.ended,
                "disposition": self.disposition or (None if active else "expired"),
                "connects": self.connects, "last_connect": self.last_connect, "active": active,
                "meta": dict(self.meta)}


class RealtimeRegistry:
    """In-memory realtime sessions, tickets and the tunnel authorisation rule."""

    def __init__(self, targets: dict[str, Target], *, clock: Callable[[], float] = time.time,
                 on_event: Callable[..., None] | None = None) -> None:
        self.targets = targets
        self._clock = clock
        self._key = secrets.token_bytes(32)
        self._lock = threading.Lock()
        self._sessions: dict[str, Session] = {}
        self._used: dict[str, float] = {}
        self._on_event = on_event

    # ---------------------------------------------------------- sessions
    def register(self, service: str, session_id: str, *, owner: str, upstream_path: str,
                 ttl_s: int = 3600, meta: dict | None = None) -> Session:
        if service not in SERVICES or service not in self.targets:
            raise RealtimeError("unknown realtime service")
        if not SESSION_RE.match(session_id) or not session_id.startswith(service + "_"):
            raise RealtimeError("invalid session id")
        if not OWNER_RE.match(owner):
            raise RealtimeError("invalid session owner")
        if not UPSTREAM_PATH_RE.match(upstream_path) or ".." in upstream_path or "//" in upstream_path:
            raise RealtimeError("invalid upstream path")
        if not isinstance(ttl_s, int) or not 30 <= ttl_s <= MAX_SESSION_TTL_S:
            raise RealtimeError(f"ttl_s must be 30..{MAX_SESSION_TTL_S}")
        meta = dict(meta or {})
        if len(json.dumps(meta, default=str)) > MAX_META_BYTES:
            raise RealtimeError("session meta is too large")
        now = self._clock()
        with self._lock:
            self._prune(now)
            existing = self._sessions.get(session_id)
            if existing is not None and existing.owner != owner:
                raise RealtimeError("session id already in use", 409, "conflict")
            active = [s for s in self._sessions.values() if s.is_active(now)]
            if existing is None and len(active) >= MAX_SESSIONS:
                raise RealtimeError("too many realtime sessions", 429, "too_many_sessions")
            sess = Session(service, session_id, owner, upstream_path, now, now + ttl_s, meta)
            self._sessions[session_id] = sess
        return sess

    def get(self, session_id: str) -> Session | None:
        with self._lock:
            return self._sessions.get(session_id)

    def end(self, session_id: str, disposition: str = "completed") -> Session | None:
        if disposition not in DISPOSITIONS:
            raise RealtimeError("unknown disposition")
        ended_now = False
        with self._lock:
            sess = self._sessions.get(session_id)
            if sess is not None and sess.ended is None:
                sess.ended = self._clock()
                sess.disposition = disposition
                ended_now = True
        if sess is not None and ended_now and self._on_event:
            self._on_event("realtime.session", service=sess.service, session_id=session_id,
                           disposition=disposition, duration_ms=int(((sess.ended or 0) - sess.created) * 1000),
                           connects=sess.connects, user=sess.owner)
        return sess

    def list(self, *, owner: str | None = None, include_ended: bool = True) -> list[dict]:
        now = self._clock()
        with self._lock:
            items = [s for s in self._sessions.values()
                     if (owner is None or s.owner == owner) and (include_ended or s.is_active(now))]
        return [s.public(now) for s in sorted(items, key=lambda s: s.created, reverse=True)]

    def stats(self) -> dict:
        now = self._clock()
        with self._lock:
            active = [s for s in self._sessions.values() if s.is_active(now)]
        return {svc: sum(1 for s in active if s.service == svc) for svc in SERVICES}

    def _prune(self, now: float) -> None:
        # Keep ended/expired sessions for an hour (the Logs page shows them), bounded.
        for sid, s in list(self._sessions.items()):
            if (s.ended or s.expires) < now - 3600:
                del self._sessions[sid]
        if len(self._sessions) > 4 * MAX_SESSIONS:
            for sid, _ in sorted(self._sessions.items(), key=lambda kv: kv[1].created)[:MAX_SESSIONS]:
                del self._sessions[sid]
        for nonce, exp in list(self._used.items()):
            if exp < now:
                del self._used[nonce]

    # ----------------------------------------------------------- tickets
    def _mac(self, sid: str, owner: str, exp: int, nonce: str) -> str:
        msg = f"v1|{sid}|{owner}|{exp}|{nonce}".encode()
        return _b64(hmac.new(self._key, msg, hashlib.sha256).digest())

    def issue_ticket(self, session_id: str, *, owner: str) -> str:
        sess = self.get(session_id)
        if sess is None or not sess.is_active(self._clock()):
            raise RealtimeError("no such active session", 404, "not_found")
        if sess.owner != owner:
            raise RealtimeError("no such active session", 404, "not_found")
        exp = int(self._clock()) + TICKET_TTL_S
        nonce = _b64(secrets.token_bytes(12))
        return f"v1.{session_id}.{exp}.{nonce}.{self._mac(session_id, owner, exp, nonce)}"

    def redeem_ticket(self, ticket: str, session_id: str) -> str:
        """Verify and consume a ticket for `session_id`; returns the owner."""
        m = TICKET_RE.match(ticket or "")
        if not m:
            raise RealtimeError("invalid ticket", 401, "bad_ticket")
        sid, exp_s, nonce, mac = m.groups()
        if sid != session_id:
            raise RealtimeError("the ticket is for another session", 401, "bad_ticket")
        sess = self.get(session_id)
        if sess is None:
            raise RealtimeError("no such session", 404, "not_found")
        if not hmac.compare_digest(mac, self._mac(sid, sess.owner, int(exp_s), nonce)):
            raise RealtimeError("invalid ticket", 401, "bad_ticket")
        now = self._clock()
        if int(exp_s) < now:
            raise RealtimeError("the ticket has expired; request a new one", 401, "ticket_expired")
        with self._lock:
            if nonce in self._used:
                raise RealtimeError("the ticket was already used; request a new one", 401, "ticket_used")
            self._used[nonce] = int(exp_s) + 1
        return sess.owner

    # --------------------------------------------------------- authorise
    def authorize(self, *, service: str, session_id: str, ticket: str | None, cookie_user: str | None,
                  origin: str | None, host: str | None) -> dict:
        """The tunnel rule. Raises RealtimeError; returns what the Playground needs."""
        if service not in SERVICES or not SESSION_RE.match(session_id or "") \
                or not session_id.startswith(service + "_"):
            raise RealtimeError("not found", 404, "not_found")
        target = self.targets.get(service)
        if target is None:
            raise RealtimeError("realtime service is not configured", 503, "realtime_disabled")
        if ticket:
            owner = self.redeem_ticket(ticket, session_id)
            via = "ticket"
        else:
            if cookie_user is None:
                raise RealtimeError("authentication required", 401, "unauthenticated")
            if not origin or not host or _netloc(origin) != host.lower():
                raise RealtimeError("cross-origin WebSocket refused", 403, "bad_origin")
            owner = f"user:{cookie_user}"
            via = "session"
        sess = self.get(session_id)
        if sess is None or sess.owner != owner:
            raise RealtimeError("no such realtime session", 404, "not_found")
        if not sess.is_active(self._clock()):
            raise RealtimeError("this realtime session has ended", 410, "session_ended")
        try:
            key = target.key_file.read_text(encoding="utf-8").strip()
        except OSError:
            key = ""
        if len(key) < 16:
            raise RealtimeError(f"gx-{service} is not configured on this Control Center (no service key)", 503,
                                "realtime_disabled")
        now = self._clock()
        with self._lock:
            sess.connects += 1
            sess.last_connect = now
        return {"ok": True, "service": service, "session_id": session_id, "via": via,
                "owner": owner_hash(owner), "owner_label": owner.split(":", 1)[0], "user": owner,
                "target": {"host": target.host, "port": target.port},
                "path": sess.upstream_path, "authorization": f"Bearer {key}",
                "max_seconds": max(1, int(sess.expires - now))}


def _netloc(url: str) -> str:
    try:
        return urllib.parse.urlsplit(url).netloc.lower()
    except ValueError:
        return ""
