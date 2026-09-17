"""gx-live sessions on the Control Center: the authoritative side of every live call.

    browser --HTTP--> Control Center (this module)
        | POST gx-live /v1/live/sessions (fabric, bearer key) -> join token
        | register the realtime session (the tunnel upstream path carries it)
        v
    microphone + camera --WS--> GX-Playground /rt/live/<sid> --fabric--> gx-live
                                                                   |  (gx10-02)
                                                                   v
                                                        MiniCPM-o 4.5 engine
        ^                                                          |
        '---- assistant audio, captions, tool events --------------'

The media path never touches gx10-01: audio, camera frames and the model
context live in memory on gx10-02 for the current turn only. What this module
owns is the session record, the turn timings, the transcript the owner chose
to save, and the **tools**: gx-live asks for a tool call, the Control Center
executes it here (never the model, never the browser) and posts the result
back over the fabric.

``delegate_to_gx`` can never reach gx-max: the alias set is closed both in
``legenex/live/gx_live/tools.py`` and again here before the gateway is called.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .netguard import BlockedURL
from .netguard import fetch as netguard_fetch
from .realtime import owner_hash

log = logging.getLogger("gx.live")

SESSION_RE = re.compile(r"^live_[0-9a-f]{32}$")
CALL_ID_RE = re.compile(r"^call_[0-9a-f]{16}$")
#: Text aliases a live session may delegate to. gx-max is deliberately absent.
DELEGATE_MODELS = ("gx-auto", "gx-fast", "gx-reason")
LIBRARY_TYPES = ("image", "video", "audio", "any")
TOOL_NAMES = ("get_time", "delegate_to_gx", "search_library", "fetch_url")
LANGUAGES = ("en", "zh")
TRIGGERS = ("speech", "text", "tool")
TURN_STATUS = ("completed", "interrupted", "failed")
SPEAKERS = ("user", "assistant")
END_REASONS = ("completed", "abandoned", "failed", "timeout")
#: Dispositions the realtime registry understands, keyed by our end reason.
DISPOSITION = {"completed": "completed", "abandoned": "abandoned", "failed": "failed",
               "timeout": "timeout", "gx_max": "failed", "maintenance": "failed", "replaced": "replaced",
               "shutdown": "failed", "lost": "failed"}
MAX_TURNS_PER_POST = 50
MAX_TRANSCRIPT_ENTRIES = 2000
MAX_TRANSCRIPT_CHARS = 4000
DEFAULT_RETENTION_DAYS = 30
#: How much of a fetched page or delegated answer the model is given back.
MAX_TOOL_CONTENT = 4000
MAX_FETCH_BYTES = 2 * 1024 * 1024


class LiveError(Exception):
    def __init__(self, message: str, status: int = 400, code: str = "invalid_request") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def _int(value: Any, lo: int, hi: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise LiveError(f"{name} must be a whole number between {lo} and {hi}")
    if not lo <= value <= hi:
        raise LiveError(f"{name} must be between {lo} and {hi}")
    return value


def _opt_int(value: Any, lo: int, hi: int, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, float) and not isinstance(value, bool):
        value = int(value)
    return _int(value, lo, hi, name)


def _clean_text(value: Any, limit: int, name: str) -> str:
    if not isinstance(value, str):
        raise LiveError(f"{name} must be text")
    text = value.replace("\r\n", "\n").strip()
    if len(text) > limit:
        raise LiveError(f"{name} must be at most {limit} characters")
    if any(ord(c) < 32 and c not in "\n\t" for c in text):
        raise LiveError(f"{name} contains control characters")
    return text


def normalise_config(body: Any) -> dict:
    """Validate the session options a browser or API client may choose.

    The same rules live in ``gx_live/protocol.py`` on gx10-02; both sides
    validate, because neither trusts the other's caller.
    """
    if body is None:
        body = {}
    if not isinstance(body, dict):
        raise LiveError("config must be an object")
    unknown = sorted(set(body) - {"instructions", "language", "tools", "output_audio", "vad",
                                  "max_response_tokens"})
    if unknown:
        raise LiveError(f"unsupported option(s): {', '.join(unknown[:6])}")
    language = body.get("language", "en")
    if language not in LANGUAGES:
        raise LiveError("language must be en or zh")
    for key in ("tools", "output_audio"):
        if key in body and not isinstance(body[key], bool):
            raise LiveError(f"{key} must be true or false")
    vad = body.get("vad") or {}
    if not isinstance(vad, dict) or set(vad) - {"threshold", "silence_ms"}:
        raise LiveError("vad accepts threshold and silence_ms")
    threshold = vad.get("threshold", 0.5)
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not 0.3 <= threshold <= 0.9:
        raise LiveError("vad.threshold must be between 0.3 and 0.9")
    return {
        "instructions": _clean_text(body.get("instructions", ""), 2000, "instructions"),
        "language": language,
        "tools": bool(body.get("tools", True)),
        "output_audio": bool(body.get("output_audio", True)),
        "vad": {"threshold": float(threshold),
                "silence_ms": _int(vad.get("silence_ms", 700), 300, 2000, "vad.silence_ms")},
        "max_response_tokens": _int(body.get("max_response_tokens", 256), 32, 1024, "max_response_tokens"),
    }


# ------------------------------------------------------------ node-2 client --
class LiveClient:
    """The gx-live supervisor on gx10-02 (fabric address, bearer key, JSON)."""

    def __init__(self, base: str, key_file: Path, timeout: float = 20.0) -> None:
        self.base = base.rstrip("/")
        self.key_file = key_file
        self.timeout = timeout

    def _key(self) -> str:
        try:
            key = self.key_file.read_text(encoding="utf-8").strip()
        except OSError:
            key = ""
        if len(key) < 32:
            raise LiveError("gx-live is not configured on this Control Center (no service key)", 503,
                            "not_configured")
        return key

    def request(self, method: str, path: str, body: dict | None = None, *, timeout: float | None = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={
            "Authorization": f"Bearer {self._key()}", "Accept": "application/json",
            **({"Content-Type": "application/json"} if data is not None else {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as res:  # noqa: S310 - fixed URL
                payload = res.read()
        except urllib.error.HTTPError as exc:
            try:
                err = json.loads(exc.read() or b"{}").get("error") or {}
            except ValueError:
                err = {}
            raise LiveError(err.get("message") or f"gx-live answered HTTP {exc.code}", exc.code,
                            err.get("code") or "gx_live_error") from None
        except (OSError, ValueError) as exc:
            raise LiveError("gx-live on gx10-02 is not reachable", 502, "gx_live_unreachable") from exc
        return json.loads(payload or b"{}")

    def health(self) -> dict:
        try:
            with urllib.request.urlopen(self.base + "/health", timeout=3) as res:  # noqa: S310 - fixed URL
                return json.load(res)
        except (OSError, ValueError):
            return {"state": "unreachable", "service": "gx-live"}


# ------------------------------------------------------------------ manager --
class LiveManager:
    """Live sessions, their records and the server-side tool executor."""

    def __init__(self, *, connect: Callable, client: LiveClient, realtime: Any, library: Any,
                 gateway_base: str, gateway_headers: Callable[[], dict], audit: Callable | None = None,
                 metric: Callable | None = None, explain: Callable[[str], dict] | None = None,
                 fetch: Callable = netguard_fetch, timezone: str = "Africa/Johannesburg",
                 start_threads: bool = True, clock: Callable[[], float] = time.time) -> None:
        self.connect = connect
        self.client = client
        self.realtime = realtime
        self.library = library
        self.gateway_base = gateway_base.rstrip("/")
        self.gateway_headers = gateway_headers
        self._audit = audit
        self._metric = metric
        self._explain = explain
        self.fetch = fetch
        self.timezone = timezone
        self.start_threads = start_threads
        self.clock = clock
        self._lock = threading.RLock()
        self._controllers: dict[str, threading.Thread] = {}
        self._stop = threading.Event()
        #: how long the delegation gateway call may take (gx-reason may load first)
        self.delegate_timeout = 600.0
        self.poll_wait = 25
        if start_threads:
            self._resume()

    # ------------------------------------------------------------- plumbing
    def audit(self, **kw: Any) -> None:
        if self._audit is not None:
            try:
                self._audit(**kw)
            except Exception:  # noqa: BLE001 - auditing must never break a session
                log.exception("audit failed")

    def metric(self, event: str, **fields: Any) -> None:
        if self._metric is not None:
            try:
                self._metric(event, **fields)
            except Exception:  # noqa: BLE001
                log.exception("metric emit failed")

    def stop(self) -> None:
        self._stop.set()

    def _row(self, sid: str) -> dict:
        if not SESSION_RE.match(sid or ""):
            raise LiveError("no such live session", 404, "not_found")
        with self.connect() as con:
            row = con.execute("SELECT * FROM live_sessions WHERE session_id = ?", (sid,)).fetchone()
        if row is None:
            raise LiveError("no such live session", 404, "not_found")
        return dict(row)

    #: Columns :meth:`_update` may write. Nothing else ever reaches the SQL.
    COLUMNS = frozenset({
        "owner", "user_label", "via", "state", "end_reason", "ready_at", "ended_at", "duration_s",
        "language", "tools_enabled", "output_audio", "camera_used", "has_instructions", "model_repo",
        "model_revision", "load_ms", "wait_ms", "turns", "responses", "interrupted", "text_inputs",
        "metrics", "error", "transcript_saved", "content_purged", "retain_until"})

    def _update(self, sid: str, **fields: Any) -> None:
        if not fields:
            return
        unknown = sorted(set(fields) - self.COLUMNS)
        if unknown:
            raise LiveError(f"cannot update live_sessions.{unknown[0]}", 500, "internal")
        cols = ", ".join(f"{k} = ?" for k in fields)
        values = [json.dumps(v) if isinstance(v, (dict, list)) else v for v in fields.values()]
        with self.connect() as con:
            con.execute(f"UPDATE live_sessions SET {cols} WHERE session_id = ?",  # noqa: S608 - fixed names
                        (*values, sid))

    def owner_of(self, sid: str) -> str:
        return self._row(sid)["owner"]

    def _event(self, sid: str, type_: str, data: dict | None = None, source: str = "server") -> None:
        payload = json.dumps(_meta_only(data or {}))[:2000]
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                seq = int(con.execute("SELECT event_seq FROM live_sessions WHERE session_id = ?",
                                      (sid,)).fetchone()[0]) + 1
                con.execute("INSERT OR REPLACE INTO live_events (session_id, seq, type, at, source, data) "
                            "VALUES (?, ?, ?, ?, ?, ?)", (sid, seq, type_[:40], self.clock(), source, payload))
                con.execute("UPDATE live_sessions SET event_seq = ? WHERE session_id = ?", (seq, sid))
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise

    # ---------------------------------------------------------------- model
    def model(self) -> dict:
        """What the Live page shows before a session exists."""
        health = self.client.health()
        info: dict = {}
        try:
            info = self.client.request("GET", "/v1/live/model", timeout=6)
        except LiveError as exc:
            info = {"error": {"code": exc.code, "message": str(exc)}}
        return {
            "alias": "gx-live", "task": info.get("task", "realtime-omni"), "node": info.get("node", "gx10-02"),
            "protocol": info.get("protocol", "gx-live.v1"), "identity": info.get("identity") or {},
            "capabilities": info.get("capabilities") or {}, "limits": info.get("limits") or {},
            "policy": info.get("policy") or {}, "session": info.get("session") or {},
            "tools": _tool_help(), "delegation_models": list(DELEGATE_MODELS),
            "health": {k: health.get(k) for k in ("state", "busy", "pinned", "active_sessions", "memory",
                                                  "waiting", "idle_seconds")},
            "reachable": health.get("state") != "unreachable",
            "error": info.get("error"),
        }

    # ------------------------------------------------------------- sessions
    def create_session(self, *, owner: str, via: str, user_label: str, config: Any = None,
                       ttl_s: int = 3600) -> dict:
        cfg = normalise_config(config)
        ttl = _int(ttl_s, 60, 4 * 3600, "ttl_s")
        from .realtime import new_session_id  # noqa: PLC0415 - avoids an import cycle at module load

        sid = new_session_id("live")
        now = self.clock()
        with self.connect() as con:
            con.execute(
                "INSERT INTO live_sessions (session_id, owner, user_label, via, state, created_at, language, "
                "tools_enabled, output_audio, has_instructions, retain_until) "
                "VALUES (?, ?, ?, ?, 'creating', ?, ?, ?, ?, ?, ?)",
                (sid, owner, user_label[:60], via, now, cfg["language"], int(cfg["tools"]),
                 int(cfg["output_audio"]), int(bool(cfg["instructions"])),
                 now + DEFAULT_RETENTION_DAYS * 86400))
        body = {"session_id": sid, "owner": owner_hash(owner), "config": cfg, "ttl_s": ttl}
        try:
            created = self.client.request("POST", "/v1/live/sessions", body, timeout=15)
        except LiveError as exc:
            self._update(sid, state="ended", end_reason=exc.code, ended_at=self.clock(), duration_s=0.0,
                         error=json.dumps({"code": exc.code, "message": str(exc)}))
            self._event(sid, "session.failed", {"code": exc.code})
            raise
        try:
            self.realtime.register("live", sid, owner=owner,
                                   upstream_path=created["upstream_path"], ttl_s=ttl,
                                   meta={"alias": "gx-live", "language": cfg["language"],
                                         "tools": cfg["tools"]})
        except Exception:
            # The session exists on gx10-02 but can never be connected to: end it.
            with _quiet():
                self.client.request("POST", f"/v1/live/sessions/{sid}/end", {"reason": "failed"}, timeout=10)
            self._update(sid, state="ended", end_reason="register_failed", ended_at=self.clock())
            raise
        model = (self.client_model_identity() or {})
        self._update(sid, state="created", model_repo=model.get("repository"),
                     model_revision=model.get("revision"))
        self._event(sid, "session.created", {"model_state": created.get("model_state"), "via": via})
        self.audit(user=user_label, action="live.session.create", outcome="ok", session=sid, via=via,
                   language=cfg["language"], tools=cfg["tools"])
        self.metric("realtime.session", service="gx-live", session_id=sid, disposition="created",
                    outcome="ok", user=owner)
        self._start_controller(sid)
        return {"session_id": sid, "protocol": created.get("protocol", "gx-live.v1"),
                "ws_path": f"/rt/live/{sid}", "expires_at": created.get("expires_at"),
                "model_state": created.get("model_state"), "waiting": created.get("waiting"),
                "config": {k: v for k, v in cfg.items() if k != "instructions"},
                "has_instructions": bool(cfg["instructions"]), "created_at": now,
                "tools": _tool_help() if cfg["tools"] else []}

    def client_model_identity(self) -> dict:
        try:
            return (self.client.request("GET", "/v1/live/model", timeout=6) or {}).get("identity") or {}
        except LiveError:
            return {}

    def end_session(self, sid: str, reason: str = "completed", *, user_label: str = "") -> dict:
        row = self._row(sid)
        reason = reason if reason in END_REASONS else "completed"
        if row["state"] != "ended":
            try:
                self.client.request("POST", f"/v1/live/sessions/{sid}/end", {"reason": reason}, timeout=15)
            except LiveError as exc:
                if exc.status != 404:
                    raise
            self._finalize(sid, reason)
        if user_label:
            self.audit(user=user_label, action="live.session.end", outcome="ok", session=sid, reason=reason)
        return self.session_view(sid)

    def _finalize(self, sid: str, reason: str) -> dict:
        """Pull the final (metadata-only) summary from gx10-02 into the record."""
        summary: dict = {}
        with _quiet():
            summary = self.client.request("GET", f"/v1/live/sessions/{sid}", timeout=10) or {}
        row = self._row(sid)
        if row["state"] == "ended":
            return row
        end_reason = summary.get("end_reason") or reason
        now = self.clock()
        duration = summary.get("duration_s")
        if not isinstance(duration, (int, float)):
            duration = round(now - float(row["created_at"]), 1)
        stats = {
            "state": "ended", "end_reason": str(end_reason)[:40], "ended_at": now, "duration_s": float(duration),
            "turns": int(summary.get("turns") or row["turns"]),
            "responses": int(summary.get("responses") or row["responses"]),
            "interrupted": int(summary.get("interrupted") or row["interrupted"]),
            "text_inputs": int(summary.get("text_inputs") or row["text_inputs"]),
            "load_ms": summary.get("model_load_ms") or row["load_ms"],
            "wait_ms": summary.get("model_wait_ms") or row["wait_ms"],
            "metrics": json.dumps(_summary_metrics(summary))[:4000],
        }
        media = summary.get("media") or {}
        if media.get("camera_frames"):
            stats["camera_used"] = 1
        self._update(sid, **stats)
        self._event(sid, "session.ended", {"reason": end_reason, "duration_s": duration,
                                           "turns": stats["turns"]})
        with _quiet():
            self.realtime.end(sid, DISPOSITION.get(str(end_reason), "failed"))
        self.metric("realtime.session", service="gx-live", session_id=sid, disposition=str(end_reason)[:40],
                    outcome="ok" if end_reason in ("completed", "abandoned", "timeout") else "failed",
                    duration_ms=int(float(duration) * 1000), user=row["owner"], turns=stats["turns"])
        return self._row(sid)

    def refresh(self, sid: str) -> dict:
        """Update the record from gx10-02 while the session is running."""
        row = self._row(sid)
        if row["state"] == "ended":
            return row
        try:
            summary = self.client.request("GET", f"/v1/live/sessions/{sid}", timeout=8) or {}
        except LiveError as exc:
            if exc.status == 404:
                return self._finalize(sid, "lost")
            return row
        if summary.get("state") == "ended":
            return self._finalize(sid, summary.get("end_reason") or "completed")
        fields: dict[str, Any] = {
            "state": "live" if summary.get("first_attach_at") else "created",
            "turns": int(summary.get("turns") or 0), "responses": int(summary.get("responses") or 0),
            "interrupted": int(summary.get("interrupted") or 0),
            "text_inputs": int(summary.get("text_inputs") or 0),
            "metrics": json.dumps(_summary_metrics(summary))[:4000],
        }
        if summary.get("model_load_ms"):
            fields["load_ms"] = summary["model_load_ms"]
            fields["ready_at"] = fields.get("ready_at") or row["ready_at"] or self.clock()
        if (summary.get("media") or {}).get("camera_frames"):
            fields["camera_used"] = 1
        self._update(sid, **{k: v for k, v in fields.items() if v is not None})
        return self._row(sid)

    # --------------------------------------------------------------- views
    def session_view(self, sid: str, *, include_content: bool = True) -> dict:
        row = self._row(sid)
        out = {
            "session_id": row["session_id"], "state": row["state"], "end_reason": row["end_reason"],
            "owner_label": row["user_label"], "via": row["via"], "created_at": row["created_at"],
            "ready_at": row["ready_at"], "ended_at": row["ended_at"], "duration_s": row["duration_s"],
            "language": row["language"], "tools_enabled": bool(row["tools_enabled"]),
            "output_audio": bool(row["output_audio"]), "camera_used": bool(row["camera_used"]),
            "has_instructions": bool(row["has_instructions"]),
            "model": {"alias": row["model_alias"], "repository": row["model_repo"],
                      "revision": row["model_revision"]},
            "load_ms": row["load_ms"], "wait_ms": row["wait_ms"], "turns": row["turns"],
            "responses": row["responses"], "interrupted": row["interrupted"],
            "text_inputs": row["text_inputs"], "tool_calls": row["tool_calls"],
            "metrics": _loads(row["metrics"]), "error": _loads(row["error"]) or None,
            "transcript_saved": bool(row["transcript_saved"]),
            "content_purged": bool(row["content_purged"]), "ws_path": f"/rt/live/{sid}",
        }
        with self.connect() as con:
            out["tools"] = [
                {"call_id": r["call_id"], "name": r["name"], "ok": None if r["ok"] is None else bool(r["ok"]),
                 "model": r["model"], "routed_to": r["routed_to"], "latency_ms": r["latency_ms"],
                 "error_code": r["error_code"], "at": r["at"], "arguments": _loads(r["arg_keys"]) or []}
                for r in con.execute("SELECT * FROM live_tool_calls WHERE session_id = ? ORDER BY at", (sid,))]
            out["assets"] = [
                {"asset_id": r["asset_id"], "relation": r["relation"], "at": r["at"]}
                for r in con.execute("SELECT * FROM live_session_assets WHERE session_id = ? ORDER BY at",
                                     (sid,))]
            if include_content:
                out["turn_log"] = [dict(r) for r in con.execute(
                    "SELECT * FROM live_turns WHERE session_id = ? ORDER BY response", (sid,))]
        return out

    def list_sessions(self, *, owner: str | None = None, limit: int = 30) -> list[dict]:
        limit = max(1, min(200, int(limit)))
        sql = "SELECT * FROM live_sessions"
        args: list[Any] = []
        if owner is not None:
            sql += " WHERE owner = ?"
            args.append(owner)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        with self.connect() as con:
            rows = [dict(r) for r in con.execute(sql, args)]
        return [{"session_id": r["session_id"], "state": r["state"], "end_reason": r["end_reason"],
                 "created_at": r["created_at"], "ended_at": r["ended_at"], "duration_s": r["duration_s"],
                 "turns": r["turns"], "responses": r["responses"], "interrupted": r["interrupted"],
                 "tool_calls": r["tool_calls"], "language": r["language"],
                 "camera_used": bool(r["camera_used"]), "transcript_saved": bool(r["transcript_saved"]),
                 "via": r["via"], "metrics": _loads(r["metrics"])} for r in rows]

    def events(self, sid: str, after: int = 0, limit: int = 500) -> dict:
        row = self._row(sid)
        after = max(0, int(after))
        limit = max(1, min(500, int(limit)))
        with self.connect() as con:
            items = [{"seq": r["seq"], "type": r["type"], "at": r["at"], "source": r["source"],
                      "data": _loads(r["data"])}
                     for r in con.execute("SELECT * FROM live_events WHERE session_id = ? AND seq > ? "
                                          "ORDER BY seq LIMIT ?", (sid, after, limit))]
        return {"session_id": sid, "state": row["state"], "events": items,
                "cursor": items[-1]["seq"] if items else after}

    # ------------------------------------------------------- client reports
    def record_turns(self, sid: str, turns: Any, *, owner: str) -> dict:
        """Per-turn timings reported by the session's own client.

        The client is the only party that sees a whole turn (the media path
        does not pass through gx10-01). Everything here is validated at the
        boundary and stored without any text.
        """
        row = self._row(sid)
        if row["owner"] != owner:
            raise LiveError("no such live session", 404, "not_found")
        if not isinstance(turns, list):
            raise LiveError("turns must be a list")
        if len(turns) > MAX_TURNS_PER_POST:
            raise LiveError(f"at most {MAX_TURNS_PER_POST} turns per request")
        cleaned = []
        for item in turns:
            if not isinstance(item, dict):
                raise LiveError("each turn must be an object")
            trigger = item.get("trigger")
            status = item.get("status")
            reason = item.get("interrupt_reason")
            cleaned.append((
                sid,
                _int(item.get("response"), 1, 100000, "response"),
                _opt_int(item.get("turn"), 0, 100000, "turn"),
                trigger if trigger in TRIGGERS else None,
                status if status in TURN_STATUS else None,
                self.clock(),
                _opt_int(item.get("first_audio_ms"), 0, 3600000, "first_audio_ms"),
                _opt_int(item.get("first_text_ms"), 0, 3600000, "first_text_ms"),
                _opt_int(item.get("turn_ms"), 0, 3600000, "turn_ms"),
                _opt_int(item.get("audio_ms"), 0, 3600000, "audio_ms"),
                _opt_int(item.get("interrupt_ms"), 0, 3600000, "interrupt_ms"),
                reason if reason in ("barge_in", "client_cancel", "new_input") else None,
                _opt_int(item.get("user_chars"), 0, 100000, "user_chars"),
                _opt_int(item.get("assistant_chars"), 0, 100000, "assistant_chars"),
                int(bool(item.get("camera"))),
            ))
        with self.connect() as con:
            con.executemany(
                "INSERT INTO live_turns (session_id, response, turn, trigger, status, at, first_audio_ms, "
                "first_text_ms, turn_ms, audio_ms, interrupt_ms, interrupt_reason, user_chars, "
                "assistant_chars, camera) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(session_id, response) DO UPDATE SET "
                "turn=excluded.turn, trigger=excluded.trigger, status=excluded.status, "
                "first_audio_ms=excluded.first_audio_ms, first_text_ms=excluded.first_text_ms, "
                "turn_ms=excluded.turn_ms, audio_ms=excluded.audio_ms, interrupt_ms=excluded.interrupt_ms, "
                "interrupt_reason=excluded.interrupt_reason, user_chars=excluded.user_chars, "
                "assistant_chars=excluded.assistant_chars, camera=excluded.camera", cleaned)
            count = con.execute("SELECT count(*) FROM live_turns WHERE session_id = ?", (sid,)).fetchone()[0]
        if any(t[14] for t in cleaned):
            self._update(sid, camera_used=1)
        for t in cleaned:
            if t[10] is not None:
                self.metric("realtime.latency", service="gx-live", session_id=sid, stage="interrupt", ms=t[10])
            elif t[6] is not None:
                self.metric("realtime.latency", service="gx-live", session_id=sid, stage="first_audio", ms=t[6])
        return {"session_id": sid, "stored": len(cleaned), "turns": count}

    def save_transcript(self, sid: str, entries: Any, *, owner: str, user_label: str = "") -> dict:
        """Keep the owner's copy of a conversation (PROTOCOL.md section 6)."""
        row = self._row(sid)
        if row["owner"] != owner:
            raise LiveError("no such live session", 404, "not_found")
        if row["content_purged"]:
            raise LiveError("this session's content was deleted", 409, "content_purged")
        if not isinstance(entries, list) or not entries:
            raise LiveError("transcript must be a non-empty list")
        if len(entries) > MAX_TRANSCRIPT_ENTRIES:
            raise LiveError(f"a transcript may hold at most {MAX_TRANSCRIPT_ENTRIES} entries")
        rows = []
        for seq, item in enumerate(entries, start=1):
            if not isinstance(item, dict):
                raise LiveError("each transcript entry must be an object")
            speaker = item.get("speaker")
            if speaker not in SPEAKERS:
                raise LiveError("speaker must be user or assistant")
            text = _clean_text(item.get("text"), MAX_TRANSCRIPT_CHARS, "text")
            if not text:
                continue
            rows.append((sid, seq, speaker, _opt_int(item.get("turn"), 0, 100000, "turn"), text,
                         self.clock(), int(bool(item.get("interrupted")))))
        if not rows:
            raise LiveError("the transcript is empty")
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                con.execute("DELETE FROM live_transcripts WHERE session_id = ?", (sid,))
                con.executemany("INSERT INTO live_transcripts (session_id, seq, speaker, turn, text, at, "
                                "interrupted) VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
                con.execute("UPDATE live_sessions SET transcript_saved = 1 WHERE session_id = ?", (sid,))
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
        self._event(sid, "transcript.saved", {"entries": len(rows)})
        self.audit(user=user_label or row["user_label"] or "", action="live.transcript.save", outcome="ok",
                   session=sid, entries=len(rows))
        return {"session_id": sid, "entries": len(rows), "saved": True}

    def transcript(self, sid: str) -> dict:
        row = self._row(sid)
        with self.connect() as con:
            items = [{"seq": r["seq"], "speaker": r["speaker"], "turn": r["turn"], "text": r["text"],
                      "at": r["at"], "interrupted": bool(r["interrupted"])}
                     for r in con.execute("SELECT * FROM live_transcripts WHERE session_id = ? ORDER BY seq",
                                          (sid,))]
        return {"session_id": sid, "saved": bool(row["transcript_saved"]), "entries": items,
                "content_purged": bool(row["content_purged"])}

    def delete_session_content(self, sid: str, *, user_label: str = "") -> dict:
        self._row(sid)
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                con.execute("DELETE FROM live_transcripts WHERE session_id = ?", (sid,))
                con.execute("UPDATE live_sessions SET transcript_saved = 0, content_purged = 1 "
                            "WHERE session_id = ?", (sid,))
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
        self._event(sid, "content.deleted", {})
        self.audit(user=user_label, action="live.session.delete-content", outcome="ok", session=sid)
        return {"session_id": sid, "content_purged": True}

    def purge_expired(self) -> int:
        """Delete the transcripts of sessions whose retention has passed."""
        now = self.clock()
        with self.connect() as con:
            stale = [r[0] for r in con.execute(
                "SELECT session_id FROM live_sessions WHERE content_purged = 0 AND retain_until IS NOT NULL "
                "AND retain_until < ? AND state = 'ended'", (now,))]
            for sid in stale:
                con.execute("DELETE FROM live_transcripts WHERE session_id = ?", (sid,))
                con.execute("UPDATE live_sessions SET transcript_saved = 0, content_purged = 1 "
                            "WHERE session_id = ?", (sid,))
        return len(stale)

    # ------------------------------------------------------------- activity
    def activity(self, user: str, since: float, limit: int) -> list[dict]:
        """Rows for the Playground Logs page (plt.md section 6)."""
        with self.connect() as con:
            rows = [dict(r) for r in con.execute(
                "SELECT * FROM live_sessions WHERE owner = ? AND created_at >= ? "
                "ORDER BY created_at DESC LIMIT ?", (f"user:{user}", since, max(1, min(200, int(limit)))))]
        out = []
        for r in rows:
            status = {"ended": "ok", "creating": "waiting", "created": "waiting", "live": "running"}.get(
                r["state"], "running")
            if r["state"] == "ended" and r["end_reason"] not in ("completed", "abandoned", None):
                status = "failed" if r["end_reason"] != "timeout" else "cancelled"
            title = f"Live session · {r['turns']} turn" + ("s" if r["turns"] != 1 else "")
            if r["tool_calls"]:
                title += f" · {r['tool_calls']} tool call" + ("s" if r["tool_calls"] != 1 else "")
            out.append({
                "id": r["session_id"], "kind": "live", "title": title, "status": status,
                "at": r["created_at"],
                "duration_ms": int((r["duration_s"] or 0) * 1000) or None,
                "error": r["end_reason"] if status == "failed" else None,
                "link": f"#/live?session={r['session_id']}",
                "detail": {"turns": r["turns"], "responses": r["responses"],
                           "interrupted": r["interrupted"], "language": r["language"],
                           "camera": bool(r["camera_used"]), "via": r["via"]},
            })
        return out

    # ------------------------------------------------------- tool execution
    def _resume(self) -> None:
        """After a Control Center restart no live session can survive (the tunnel
        registry is in memory), so unfinished rows are closed."""
        try:
            with self.connect() as con:
                stale = [r[0] for r in con.execute(
                    "SELECT session_id FROM live_sessions WHERE state != 'ended'")]
        except Exception:  # noqa: BLE001 - the database may not be migrated yet
            log.exception("could not read live sessions at start-up")
            return
        for sid in stale:
            with _quiet():
                self._update(sid, state="ended", end_reason="restarted", ended_at=self.clock())

    def _start_controller(self, sid: str) -> None:
        if not self.start_threads:
            return
        with self._lock:
            t = self._controllers.get(sid)
            if t and t.is_alive():
                return
            t = threading.Thread(target=self._controller, args=(sid,), name=f"live-ctl-{sid[-6:]}",
                                 daemon=True)
            self._controllers[sid] = t
        t.start()

    def _controller(self, sid: str) -> None:
        """Long-poll gx-live for tool calls until the session ends."""
        failures = 0
        while not self._stop.is_set():
            try:
                page = self.client.request(
                    "GET", f"/v1/live/sessions/{sid}/tool-calls?wait={self.poll_wait}",
                    timeout=self.poll_wait + 15)
                failures = 0
            except LiveError as exc:
                failures += 1
                if exc.status == 404 or failures > 20:
                    with _quiet():
                        self._finalize(sid, "lost")
                    return
                self._stop.wait(min(10.0, 0.5 * failures))
                continue
            for call in page.get("calls") or []:
                self._dispatch_tool(sid, call)
            if page.get("session_state") == "ended":
                with _quiet():
                    self._finalize(sid, page.get("end_reason") or "completed")
                return
            with _quiet():
                self.refresh(sid)
            if self._row(sid)["state"] == "ended":
                return

    def _dispatch_tool(self, sid: str, call: dict) -> None:
        call_id = str(call.get("call_id") or "")
        name = str(call.get("name") or "")
        raw = call.get("arguments")
        args: dict = raw if isinstance(raw, dict) else {}
        if not CALL_ID_RE.match(call_id) or name not in TOOL_NAMES:
            log.warning("session %s: ignoring malformed tool call", sid)
            return
        with self.connect() as con:
            cur = con.execute(
                "INSERT OR IGNORE INTO live_tool_calls (session_id, call_id, name, arg_keys, arg_bytes, at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (sid, call_id, name, json.dumps(sorted(args)[:8]), len(json.dumps(args)), self.clock()))
            fresh = cur.rowcount == 1
        if not fresh:
            return
        with self.connect() as con:
            con.execute("UPDATE live_sessions SET tool_calls = tool_calls + 1 WHERE session_id = ?", (sid,))
        self._event(sid, "tool.call", {"name": name, "call_id": call_id})
        threading.Thread(target=self._run_tool, args=(sid, call_id, name, args), name="gx-live-tool",
                         daemon=True).start()

    def _run_tool(self, sid: str, call_id: str, name: str, args: dict) -> None:
        t0 = self.clock()
        model = None
        try:
            ok, content, summary, extra = self.execute_tool(sid, call_id, name, args)
            model = extra.get("model")
            error = None if ok else {"code": extra.get("error_code", "tool_failed"),
                                     "message": summary[:300] or "the tool failed"}
        except LiveError as exc:
            ok, content, summary, extra = False, "", str(exc), {}
            error = {"code": exc.code, "message": str(exc)[:300]}
        except Exception as exc:  # noqa: BLE001 - one bad tool must never kill a session
            log.exception("live tool %s failed", name)
            ok, content, summary, extra = False, "", "the tool failed", {}
            error = {"code": type(exc).__name__[:40], "message": "the tool failed"}
        latency = int((self.clock() - t0) * 1000)
        body = {"kind": "result", "ok": ok, "content": content[:8000], "summary": summary[:600],
                "model": model, "routed_to": extra.get("routed_to"), "error": error}
        with _quiet():
            self.client.request("POST", f"/v1/live/sessions/{sid}/tool-calls/{call_id}", body, timeout=15)
        with self.connect() as con:
            con.execute("UPDATE live_tool_calls SET ok = ?, latency_ms = ?, error_code = ?, model = ?, "
                        "routed_to = ?, result_chars = ?, finished_at = ? "
                        "WHERE session_id = ? AND call_id = ?",
                        (int(ok), latency, (error or {}).get("code"), model, extra.get("routed_to"),
                         len(content), self.clock(), sid, call_id))
        self._event(sid, "tool.result", {"name": name, "call_id": call_id, "ok": ok,
                                         "latency_ms": latency, "model": model,
                                         "error_code": (error or {}).get("code")})
        self.metric("realtime.latency", service="gx-live", session_id=sid,
                    stage="delegation" if name == "delegate_to_gx" else "tool", ms=latency,
                    outcome="ok" if ok else "failed", alias=model)

    def _progress(self, sid: str, call_id: str, state: str, detail: str, model: str | None = None) -> None:
        with _quiet():
            self.client.request("POST", f"/v1/live/sessions/{sid}/tool-calls/{call_id}",
                                {"kind": "progress", "state": state, "detail": detail[:300], "model": model},
                                timeout=8)

    def execute_tool(self, sid: str, call_id: str, name: str, args: dict) -> tuple[bool, str, str, dict]:
        """Run one tool. Returns (ok, content for the model, short summary, extra)."""
        row = self._row(sid)
        if not row["tools_enabled"]:
            return False, "", "tools are switched off for this session", {"error_code": "tools_disabled"}
        if name == "get_time":
            return True, *self._tool_time()
        if name == "delegate_to_gx":
            return self._tool_delegate(sid, call_id, row, args)
        if name == "search_library":
            return self._tool_search(sid, call_id, args)
        if name == "fetch_url":
            return self._tool_fetch(args)
        return False, "", f"unknown tool {name[:40]}", {"error_code": "unknown_tool"}

    def _tool_time(self) -> tuple[str, str, dict]:
        now = None
        try:
            from zoneinfo import ZoneInfo  # noqa: PLC0415

            from datetime import datetime  # noqa: PLC0415
            now = datetime.now(ZoneInfo(self.timezone))
        except Exception:  # noqa: BLE001 - fall back to the host's local time
            from datetime import datetime  # noqa: PLC0415
            now = datetime.now().astimezone()
        text = now.strftime("%A %d %B %Y, %H:%M") + f" ({now.tzname()})"
        return text, text, {}

    def _tool_delegate(self, sid: str, call_id: str, row: dict, args: dict) -> tuple[bool, str, str, dict]:
        alias = args.get("model")
        if alias not in DELEGATE_MODELS:
            return False, "", "that model cannot be used for delegation", {"error_code": "bad_model"}
        task = _clean_text(args.get("task"), 4000, "task")
        if not task:
            return False, "", "the task was empty", {"error_code": "invalid_tool_arguments"}
        if self._explain:
            with _quiet():
                why = self._explain(alias) or {}
                if why.get("code") not in (None, "starting"):
                    self._progress(sid, call_id, "loading", str(why.get("reason") or "")[:300], alias)
        self._progress(sid, call_id, "running", f"asking {alias}", alias)
        body = {"model": alias, "messages": [
            {"role": "system", "content": "You are answering a question that was asked out loud during a live "
                                          "voice conversation. Answer directly, in plain spoken language, "
                                          "without markdown, lists or headings."},
            {"role": "user", "content": task}],
            "max_tokens": 700, "temperature": 0.7, "user": row["owner"]}
        req = urllib.request.Request(f"{self.gateway_base}/v1/chat/completions",
                                     data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", **self.gateway_headers()})
        try:
            with urllib.request.urlopen(req, timeout=self.delegate_timeout) as res:  # noqa: S310 - fixed URL
                data = json.loads(res.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return False, "", f"{alias} refused the request (HTTP {exc.code})", {
                "error_code": f"gateway_{exc.code}", "model": alias}
        except (OSError, ValueError, TimeoutError):
            return False, "", f"{alias} did not answer in time", {"error_code": "gateway_timeout",
                                                                  "model": alias}
        choice = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        text = str(choice).strip()
        if not text:
            return False, "", f"{alias} returned an empty answer", {"error_code": "empty_answer",
                                                                    "model": alias}
        routed = str(data.get("model") or alias)[:40]
        return True, text[:MAX_TOOL_CONTENT], text[:600], {"model": alias, "routed_to": routed}

    def _tool_search(self, sid: str, call_id: str, args: dict) -> tuple[bool, str, str, dict]:
        query = _clean_text(args.get("query"), 200, "query")
        kind = args.get("type", "any")
        if kind not in LIBRARY_TYPES:
            return False, "", "type must be image, video, audio or any", {"error_code":
                                                                          "invalid_tool_arguments"}
        try:
            found = self.library.search(q=query, type_="" if kind == "any" else kind, limit=5,
                                        include_tests=False)
        except Exception as exc:  # noqa: BLE001 - a library error must not break the session
            log.exception("library search failed")
            return False, "", "the library search failed", {"error_code": type(exc).__name__[:40]}
        items = found.get("items") or []
        if not items:
            return True, f"Nothing in the library matches {query!r}.", "no matches", {}
        now = self.clock()
        with self.connect() as con:
            con.executemany("INSERT OR IGNORE INTO live_session_assets (session_id, asset_id, relation, "
                            "call_id, at) VALUES (?, ?, 'referenced', ?, ?)",
                            [(sid, a["id"], call_id, now) for a in items])
        lines = []
        for a in items:
            title = (a.get("title") or a.get("prompt") or "untitled").strip()[:80]
            lines.append(f"- {a.get('type')}: {title} ({_ago_words(now - float(a.get('created_at') or now))})")
        text = f"{len(items)} match(es) for {query!r}:\n" + "\n".join(lines)
        return True, text[:MAX_TOOL_CONTENT], f"{len(items)} library match(es)", {}

    def _tool_fetch(self, args: dict) -> tuple[bool, str, str, dict]:
        url = _clean_text(args.get("url"), 2000, "url")
        if not re.match(r"^https?://", url):
            return False, "", "the address must start with http:// or https://", {
                "error_code": "invalid_tool_arguments"}
        try:
            res = self.fetch(url, timeout=12.0, max_bytes=MAX_FETCH_BYTES,
                             headers={"Accept": "text/html,text/plain;q=0.9"})
        except BlockedURL as exc:
            return False, "", f"that address cannot be fetched: {exc}", {"error_code": "blocked_url"}
        except OSError:
            return False, "", "that page could not be reached", {"error_code": "fetch_failed"}
        if res.status >= 400:
            return False, "", f"the page answered HTTP {res.status}", {"error_code": f"http_{res.status}"}
        ctype = (res.headers.get("content-type") or "").split(";")[0].strip().lower()
        if ctype and not (ctype.startswith("text/") or ctype in ("application/json", "application/xhtml+xml")):
            return False, "", f"that address is {ctype}, not a text page", {"error_code": "not_text"}
        text = _html_to_text(res.body.decode("utf-8", "replace"))
        if not text:
            return False, "", "that page has no readable text", {"error_code": "empty_page"}
        return True, text[:MAX_TOOL_CONTENT], text[:600], {}


# ------------------------------------------------------------------ helpers --
class _quiet:
    """Swallow expected failures of best-effort side calls (logged, never raised)."""

    def __enter__(self) -> _quiet:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:  # noqa: ANN001
        if exc_type is None:
            return False
        if issubclass(exc_type, (LiveError, OSError, ValueError, KeyError)):
            log.info("live: best-effort call failed: %s", exc)
            return True
        return False


def _loads(raw: Any) -> Any:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {}


#: Keys that may never be written to an event row.
_CONTENT_KEYS = frozenset({"text", "prompt", "transcript", "content", "audio", "image", "messages", "body",
                           "arguments", "instructions", "task", "query", "url", "summary", "join_token",
                           "ticket", "authorization", "key", "token"})


def _meta_only(data: dict) -> dict:
    out: dict[str, Any] = {}
    for k, v in data.items():
        if k in _CONTENT_KEYS or "api_key" in k:
            continue
        if isinstance(v, (int, float, bool)) or v is None:
            out[k] = v
        elif isinstance(v, str):
            out[k] = v[:120]
    return out


def _summary_metrics(summary: dict) -> dict:
    latency = summary.get("latency") or {}
    media = summary.get("media") or {}
    transport = summary.get("transport") or {}
    return {"latency": {k: latency.get(k) for k in ("first_audio_ms", "first_text_ms", "turn_ms",
                                                    "interrupt_ms")},
            "media": {k: media.get(k) for k in ("mic_seconds", "camera_frames", "camera_frames_dropped",
                                                "assistant_audio_seconds")},
            "transport": {k: transport.get(k) for k in ("bytes_in", "bytes_out", "connects")},
            "speech_segments": summary.get("speech_segments"),
            "failed_responses": summary.get("failed_responses")}


def _ago_words(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds < 3600:
        return f"{int(seconds // 60)} minutes ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} hours ago"
    return f"{int(seconds // 86400)} days ago"


_TAG = re.compile(r"<[^>]+>")
_DROP = re.compile(r"<(script|style|noscript|template)\b[^>]*>.*?</\1>", re.I | re.S)
_ENTITIES = {"&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'", "&nbsp;": " "}


def _html_to_text(raw: str) -> str:
    text = _DROP.sub(" ", raw)
    text = re.sub(r"<(br|/p|/div|/li|/h[1-6])\s*/?>", "\n", text, flags=re.I)
    text = _TAG.sub(" ", text)
    for entity, char in _ENTITIES.items():
        text = text.replace(entity, char)
    text = re.sub(r"&#(\d{1,6});", lambda m: chr(int(m.group(1))) if int(m.group(1)) < 0x110000 else " ", text)
    text = re.sub(r"[ \t\f\v]+", " ", text)
    return re.sub(r"\n\s*\n\s*", "\n\n", text).strip()


def _tool_help() -> list[dict]:
    return [
        {"name": "get_time", "label": "Time and date", "summary": "Reads the clock on gx10-01."},
        {"name": "delegate_to_gx", "label": "Ask a bigger model",
         "summary": "Hands a hard question to gx-auto, gx-fast or gx-reason and speaks the answer. "
                    "Never gx-max."},
        {"name": "search_library", "label": "Search the Library",
         "summary": "Finds up to five of your own images, videos or tracks by words."},
        {"name": "fetch_url", "label": "Read a web page",
         "summary": "Fetches a public page through netguard and reads its text."},
    ]
