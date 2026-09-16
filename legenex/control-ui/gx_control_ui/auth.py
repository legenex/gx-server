"""Authentication for the control UI: one local admin account, server-side
sessions, CSRF tokens and login throttling.

Password hashing uses `hashlib.scrypt` -- the memory-hard KDF from the
standard library (OpenSSL) -- with a per-password random salt and a
constant-time comparison. Nothing here invents a hash construction.

The password store lives OUTSIDE the Git checkout (`secret_dir/auth.json`,
mode 0600 in a 0700 directory) and is only ever written by the admin helper
(`python3 -m gx_control_ui.passwd`). The running server re-reads it when it
changes and drops every session if the password generation moved, so a reset
logs everyone out.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SCRYPT_MAXMEM = 128 * 1024 * 1024

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 1024
USERNAME_RE_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789._-")

COOKIE_NAME = "gxui_session"


class AuthError(Exception):
    pass


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"), validate=True)


def validate_username(username: str) -> str:
    name = (username or "").strip().lower()
    if not 1 <= len(name) <= 32 or not set(name) <= USERNAME_RE_CHARS:
        raise AuthError("username must be 1-32 characters of a-z, 0-9, '.', '_' or '-'")
    return name


def validate_new_password(password: str) -> None:
    if not isinstance(password, str):
        raise AuthError("password must be a string")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AuthError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise AuthError("password is too long")
    if password.strip() != password:
        raise AuthError("password must not start or end with whitespace")
    if len(set(password)) < 5:
        raise AuthError("password is too repetitive")


def hash_password(password: str, *, n: int = SCRYPT_N) -> dict:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=SCRYPT_R, p=SCRYPT_P,
        maxmem=SCRYPT_MAXMEM, dklen=SCRYPT_DKLEN,
    )
    return {"algo": "scrypt", "n": n, "r": SCRYPT_R, "p": SCRYPT_P,
            "salt": _b64(salt), "hash": _b64(digest)}


def verify_password(password: str, record: dict) -> bool:
    try:
        if record.get("algo") != "scrypt":
            return False
        salt = _unb64(record["salt"])
        expected = _unb64(record["hash"])
        digest = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=int(record["n"]), r=int(record["r"]),
            p=int(record["p"]), maxmem=SCRYPT_MAXMEM, dklen=len(expected),
        )
    except (KeyError, ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected)


# A fixed record used to spend the same time on unknown usernames.
_DUMMY_RECORD = None


def _dummy_record() -> dict:
    global _DUMMY_RECORD
    if _DUMMY_RECORD is None:
        _DUMMY_RECORD = hash_password(secrets.token_urlsafe(24))
    return _DUMMY_RECORD


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def write_private_file(path: Path, text: str) -> None:
    """Atomically write `text` with mode 0600 (never world/group readable,
    not even for an instant)."""
    ensure_private_dir(path.parent)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.chmod(path, 0o600)


class PasswordStore:
    """The single admin credential, on disk."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._cache: dict | None = None
        self._mtime: float | None = None

    def load(self) -> dict | None:
        with self._lock:
            try:
                st = self.path.stat()
            except FileNotFoundError:
                self._cache, self._mtime = None, None
                return None
            if self._cache is not None and st.st_mtime == self._mtime:
                return self._cache
            if st.st_mode & 0o077:
                # Refuse a store other users could read or write.
                raise AuthError(f"{self.path} must be mode 0600 (is {oct(st.st_mode & 0o777)})")
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or "username" not in data or "password" not in data:
                raise AuthError(f"{self.path} is malformed")
            self._cache, self._mtime = data, st.st_mtime
            return data

    def configured(self) -> bool:
        try:
            return self.load() is not None
        except (AuthError, ValueError, OSError):
            return False

    def generation(self) -> int:
        data = self.load()
        return int(data.get("generation", 0)) if data else 0

    def set_password(self, username: str, password: str, *, n: int = SCRYPT_N) -> dict:
        username = validate_username(username)
        validate_new_password(password)
        try:
            previous = self.load()
        except (AuthError, ValueError):
            previous = None
        record = {
            "version": 1,
            "username": username,
            "password": hash_password(password, n=n),
            "generation": int(previous.get("generation", 0)) + 1 if previous else 1,
            "updated": int(time.time()),
        }
        write_private_file(self.path, json.dumps(record, indent=1) + "\n")
        with self._lock:
            self._cache, self._mtime = None, None
        return record

    def check(self, username: str, password: str) -> bool:
        data = self.load()
        if data is None:
            verify_password(password or "", _dummy_record())
            return False
        name_ok = hmac.compare_digest(
            (username or "").strip().lower().encode("utf-8"), data["username"].encode("utf-8")
        )
        pw_ok = verify_password(password or "", data["password"] if name_ok else _dummy_record())
        return name_ok and pw_ok


@dataclass
class Session:
    username: str
    csrf: str
    created: float
    last_seen: float
    generation: int
    ip: str = ""
    data: dict = field(default_factory=dict)


class SessionManager:
    """Opaque random session tokens; only their SHA-256 is kept in memory."""

    MAX_SESSIONS = 32

    def __init__(self, idle_seconds: int, max_seconds: int) -> None:
        self.idle = idle_seconds
        self.max = max_seconds
        self._lock = threading.Lock()
        self._sessions: dict[str, Session] = {}

    @staticmethod
    def _key(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def create(self, username: str, generation: int, ip: str = "") -> tuple[str, Session]:
        token = secrets.token_urlsafe(32)
        now = time.time()
        sess = Session(username, secrets.token_urlsafe(32), now, now, generation, ip)
        with self._lock:
            self._prune(now)
            if len(self._sessions) >= self.MAX_SESSIONS:
                oldest = min(self._sessions, key=lambda k: self._sessions[k].last_seen)
                del self._sessions[oldest]
            self._sessions[self._key(token)] = sess
        return token, sess

    def get(self, token: str | None, generation: int) -> Session | None:
        if not token or len(token) > 128:
            return None
        now = time.time()
        with self._lock:
            key = self._key(token)
            sess = self._sessions.get(key)
            if sess is None:
                return None
            if (
                now - sess.last_seen > self.idle
                or now - sess.created > self.max
                or sess.generation != generation
            ):
                del self._sessions[key]
                return None
            sess.last_seen = now
            return sess

    def destroy(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            self._sessions.pop(self._key(token), None)

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()

    def count(self) -> int:
        with self._lock:
            self._prune(time.time())
            return len(self._sessions)

    def _prune(self, now: float) -> None:
        dead = [
            k for k, s in self._sessions.items()
            if now - s.last_seen > self.idle or now - s.created > self.max
        ]
        for k in dead:
            del self._sessions[k]


def csrf_ok(session: Session, supplied: str | None) -> bool:
    if not supplied:
        return False
    return hmac.compare_digest(session.csrf.encode(), supplied.encode())


class LoginThrottle:
    """Per-client and global failure limits. A locked-out client is refused
    before any password is checked."""

    def __init__(self, per_ip: int = 5, global_limit: int = 30, window: float = 900.0,
                 lockout: float = 900.0) -> None:
        self.per_ip = per_ip
        self.global_limit = global_limit
        self.window = window
        self.lockout = lockout
        self._lock = threading.Lock()
        self._fails: dict[str, list[float]] = {}
        self._locked: dict[str, float] = {}
        self._global: list[float] = []

    def blocked(self, ip: str) -> float:
        """Seconds until `ip` may try again (0 = allowed)."""
        now = time.time()
        with self._lock:
            until = self._locked.get(ip, 0.0)
            if until > now:
                return until - now
            self._global = [t for t in self._global if now - t < self.window]
            if len(self._global) >= self.global_limit:
                return self.window - (now - self._global[0])
        return 0.0

    def failure(self, ip: str) -> None:
        now = time.time()
        with self._lock:
            fails = [t for t in self._fails.get(ip, []) if now - t < self.window]
            fails.append(now)
            self._fails[ip] = fails
            self._global.append(now)
            if len(fails) >= self.per_ip:
                self._locked[ip] = now + self.lockout
                self._fails[ip] = []

    def success(self, ip: str) -> None:
        with self._lock:
            self._fails.pop(ip, None)
            self._locked.pop(ip, None)
