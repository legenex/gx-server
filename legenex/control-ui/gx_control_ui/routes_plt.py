"""Build V3 platform routes (PLT): realtime tunnel authorisation, realtime
sessions, the Playground activity feed (Logs), the creative model catalogue
(Models) and Playground preferences (Settings).

Registered by importing this module at the end of server.py. Session routes
inherit the server's authentication, CSRF and same-origin checks.

``POST /api/realtime/authorize`` is registered "public" so the session check
does not answer 401 before the route runs, but it answers 404 to anyone who is
not the local GX-Playground proxy (loopback peer + the shared proxy token). It
is not in the Playground's forwarding allow-list, so a browser can never reach
it.
"""

from __future__ import annotations

import re
import time
from typing import Any

from .realtime import RealtimeError
from .server import MAX_BODY, Handler, _q, route


# ============================================================ realtime
@route("POST", r"/api/realtime/authorize", "public")
def api_realtime_authorize(h: Handler) -> None:
    if not h.via_playground():
        h._error(404, "not found", "not_found")
        return
    body = h._body(4096)
    service = body.get("service")
    session_id = body.get("session_id")
    ticket = body.get("ticket")
    origin = body.get("origin")
    host = body.get("host")
    for name, value in (("service", service), ("session_id", session_id)):
        if not isinstance(value, str):
            raise ValueError(f"{name} is required")
    # re-bind as str so the checker carries the isinstance narrowing to the call
    service, session_id = str(service), str(session_id)
    for name, value in (("ticket", ticket), ("origin", origin), ("host", host)):
        if value is not None and (not isinstance(value, str) or len(value) > 512):
            raise ValueError(f"{name} must be text")
    user = None
    if not ticket:
        sess = h.app.sessions.get(h._cookie_token(), h.app.generation())
        user = sess.username if sess is not None else None
    ip = h._client_ip()
    try:
        grant = h.app.realtime.authorize(service=service, session_id=session_id, ticket=ticket or None,
                                         cookie_user=user, origin=origin, host=host)
    except RealtimeError as exc:
        h.app.actions.audit(user=user, ip=ip, action=f"realtime.authorize.{service}"[:64], outcome="refused",
                            session=str(session_id)[:40], code=exc.code)
        h._json(exc.status, {"error": {"message": str(exc), "code": exc.code}})
        return
    h.app.actions.audit(user=user or f"{grant['owner_label']}:{grant['owner']}", ip=ip,
                        action=f"realtime.authorize.{service}", outcome="ok", session=session_id, via=grant["via"])
    h._json(200, grant)


@route("GET", r"/api/realtime/sessions")
def api_realtime_sessions(h: Handler) -> None:
    assert h.session is not None  # noqa: S101
    active = _q(h, "active") in ("1", "true")
    h._json(200, {"sessions": h.app.realtime.list(owner=f"user:{h.session.username}", include_ended=not active),
                  "stats": h.app.realtime.stats()})


# ============================================================ activity
@route("GET", r"/api/activity")
def api_activity(h: Handler) -> None:
    assert h.session is not None  # noqa: S101
    kind = _q(h, "kind") or None
    status = _q(h, "status") or None
    q = _q(h, "q")[:100]
    try:
        since = float(_q(h, "since", "0") or 0)
        limit = int(_q(h, "limit", "200") or 200)
    except ValueError as exc:
        raise ValueError("since and limit must be numbers") from exc
    if kind and not re.fullmatch(r"[a-z][a-z0-9_\-]{1,23}", kind):
        raise ValueError("invalid kind")
    h._json(200, h.app.activity.query(h.session.username, kind=kind, status=status, q=q, since=since,
                                      limit=limit))


# ============================================================ catalogue
@route("GET", r"/api/catalog")
def api_catalog(h: Handler) -> None:
    from .catalog import build_catalog
    h._json(200, build_catalog(h.app))


# ========================================================== preferences
PREF_KEYS: dict[str, Any] = {
    "theme": ("dark", "light", "system"),
    "reduced_motion": ("system", "reduce"),
    "density": ("comfortable", "compact"),
    "default_image_size": re.compile(r"^[0-9]{3,4}x[0-9]{3,4}$"),
    "default_video_size": re.compile(r"^[0-9]{3,4}x[0-9]{3,4}$"),
    "default_image_model": re.compile(r"^[a-z0-9_\-.]{1,64}$"),
    "default_music_duration": re.compile(r"^[0-9]{2,3}$"),
}


def _prefs_get(h: Handler, user: str) -> dict:
    with h.app.library.connect() as con:
        rows = con.execute("SELECT key, value FROM plt_preferences WHERE username = ?", (user,)).fetchall()
    return {k: v for k, v in rows if k in PREF_KEYS}


@route("GET", r"/api/preferences")
def api_preferences(h: Handler) -> None:
    assert h.session is not None  # noqa: S101
    h._json(200, {"preferences": _prefs_get(h, h.session.username),
                  "allowed": {k: (list(v) if isinstance(v, tuple) else v.pattern) for k, v in PREF_KEYS.items()}})


@route("POST", r"/api/preferences")
def api_preferences_set(h: Handler) -> None:
    assert h.session is not None  # noqa: S101
    body = h._body(MAX_BODY)
    prefs = body.get("preferences")
    if not isinstance(prefs, dict) or not prefs or len(prefs) > len(PREF_KEYS):
        raise ValueError("preferences must be a non-empty object")
    clean: dict[str, str | None] = {}
    for key, value in prefs.items():
        rule = PREF_KEYS.get(key)
        if rule is None:
            raise ValueError(f"unknown preference: {str(key)[:40]}")
        if value is None:
            clean[key] = None
            continue
        if not isinstance(value, str):
            raise ValueError(f"{key} must be text")
        ok = value in rule if isinstance(rule, tuple) else bool(rule.fullmatch(value))
        if not ok:
            raise ValueError(f"invalid value for {key}")
        clean[key] = value
    now = time.time()
    user = h.session.username
    with h.app.library.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            for key, value in clean.items():
                if value is None:
                    con.execute("DELETE FROM plt_preferences WHERE username = ? AND key = ?", (user, key))
                else:
                    con.execute("INSERT INTO plt_preferences (username, key, value, updated_at) VALUES (?, ?, ?, ?) "
                                "ON CONFLICT(username, key) DO UPDATE SET value = excluded.value, "
                                "updated_at = excluded.updated_at", (user, key, value, now))
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    h.app.actions.audit(user=user, ip=h._client_ip(), action="preferences.update", outcome="ok",
                        keys=sorted(clean))
    h._json(200, {"preferences": _prefs_get(h, user)})
