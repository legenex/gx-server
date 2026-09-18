"""The signed-in user's activity and error feed (Playground > Logs, Build V3).

Sources are small callables ``(user, since, limit) -> list[item]``. The
platform registers media jobs, music jobs, realtime sessions, the audit log
and metric lines; other workstreams add theirs with
``app.activity.register(name, fn)`` (plt.md section 6). Every string is
redacted and every item is normalised before it reaches the browser.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from typing import Any

from .redact import redact

log = logging.getLogger("gx.ui.activity")

STATUSES = ("ok", "failed", "running", "waiting", "cancelled")
KIND_RE = re.compile(r"^[a-z][a-z0-9_\-]{1,23}$")
MAX_LIMIT = 500
Source = Callable[[str, float, int], list[dict]]

_STATUS_MAP = {
    "ready": "ok", "completed": "ok", "complete": "ok", "succeeded": "ok", "success": "ok", "done": "ok",
    "ok": "ok", "saving": "running",
    "failed": "failed", "error": "failed", "refused": "failed", "timeout": "failed", "abandoned": "cancelled",
    "running": "running", "generating": "running", "loading": "running", "submitting": "running",
    "processing": "running", "active": "running", "rendering": "running", "connected": "running",
    "queued": "waiting", "waiting": "waiting", "pending": "waiting",
    "cancelled": "cancelled", "canceled": "cancelled",
}


def norm_status(value: Any) -> str:
    return _STATUS_MAP.get(str(value or "").lower(), "ok" if value is None else "waiting")


def _clean(value: Any, limit: int = 300) -> Any:
    if isinstance(value, str):
        return redact(value)[:limit]
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    return redact(str(value))[:limit]


def normalise(kind: str, item: dict) -> dict | None:
    try:
        at = float(item.get("at") or 0)
    except (TypeError, ValueError):
        return None
    status = item.get("status")
    status = status if status in STATUSES else norm_status(status)
    link = item.get("link")
    if not (isinstance(link, str) and re.fullmatch(r"#/[a-z\-]{2,24}(\?[A-Za-z0-9_=&.\-]{0,200})?", link)):
        link = None
    _detail_raw = item.get("detail")
    detail: dict = _detail_raw if isinstance(_detail_raw, dict) else {}
    duration = item.get("duration_ms")
    return {
        "id": _clean(str(item.get("id") or ""), 80),
        "kind": kind,
        "title": _clean(item.get("title") or kind, 160),
        "status": status,
        "at": at,
        "duration_ms": int(duration) if isinstance(duration, (int, float)) and duration >= 0 else None,
        "error": _clean(item["error"], 400) if item.get("error") else None,
        "link": link,
        "detail": {str(k)[:40]: _clean(v, 160) for k, v in list(detail.items())[:12]
                   if not re.search(r"(?i)prompt|lyrics|transcript|text|key|token|secret|password|cookie", str(k))},
    }


class ActivityFeed:
    def __init__(self) -> None:
        self._sources: dict[str, Source] = {}
        self._lock = threading.Lock()

    def register(self, kind: str, fn: Source) -> None:
        if not KIND_RE.match(kind):
            raise ValueError("activity kind must be a short lowercase name")
        with self._lock:
            self._sources[kind] = fn

    def kinds(self) -> list[str]:
        with self._lock:
            return sorted(self._sources)

    def query(self, user: str, *, kind: str | None = None, status: str | None = None, q: str = "",
              since: float = 0.0, limit: int = 200) -> dict:
        limit = max(1, min(MAX_LIMIT, int(limit)))
        with self._lock:
            sources = dict(self._sources)
        if kind and kind not in sources:
            raise ValueError("unknown activity kind")
        if status and status not in STATUSES:
            raise ValueError("unknown status filter")
        items: list[dict] = []
        unavailable: list[str] = []
        for name, fn in sources.items():
            if kind and name != kind:
                continue
            try:
                raw = fn(user, since, limit) or []
            except Exception as exc:  # noqa: BLE001 - one broken source must not hide the others
                log.warning("activity source %s failed: %s", name, exc)
                unavailable.append(name)
                continue
            for it in raw:
                if not isinstance(it, dict):
                    continue
                n = normalise(name, it)
                if n is None or n["at"] < since:
                    continue
                if status and n["status"] != status:
                    continue
                if q and q.lower() not in f"{n['title']} {n['error'] or ''} {n['id']}".lower():
                    continue
                items.append(n)
        items.sort(key=lambda i: i["at"], reverse=True)
        counts = {s: sum(1 for i in items if i["status"] == s) for s in STATUSES}
        return {"items": items[:limit], "total": len(items), "counts": counts, "kinds": sorted(sources),
                "unavailable": unavailable}


# ------------------------------------------------------------ built-in sources
def builtin_sources(app: Any) -> dict[str, Source]:
    """The platform's own sources: media jobs, music jobs, realtime sessions,
    this user's audit entries and metric lines. `app` is the Control Center App."""
    import json
    from pathlib import Path

    def media(user: str, since: float, limit: int) -> list[dict]:
        out = []
        with app.media._cv:  # noqa: SLF001 - same package; the public view has no owner
            owners = {jid: job.user for jid, job in app.media._jobs.items()}  # noqa: SLF001
        for j in app.media.list():
            if owners.get(j["id"]) != user:
                continue
            out.append({"id": j["id"], "title": j.get("label") or j.get("kind"), "status": j.get("phase"),
                        "at": j.get("ended") or j.get("started") or j.get("created"),
                        "duration_ms": int((j.get("elapsed_seconds") or 0) * 1000) or None,
                        "error": j.get("error"),
                        "link": "#/history",
                        "detail": {"alias": j.get("alias"), "kind": j.get("kind"),
                                   "waiting": (j.get("waiting") or {}).get("reason"),
                                   "assets": len(j.get("assets") or [])}})
        return out

    def music(user: str, since: float, limit: int) -> list[dict]:
        out = []
        for j in app.music.list(limit=min(200, limit), mine_only=False):
            local = app.music._jobs.get(j.get("id"), {})  # noqa: SLF001 - same package, owner bookkeeping
            if local.get("user") not in (None, user):
                continue
            err = j.get("error") if isinstance(j.get("error"), dict) else {}
            out.append({"id": j.get("id"), "title": f"Music {j.get('operation') or 'generate'}",
                        "status": j.get("status"),
                        "at": j.get("finished_at") or j.get("started_at") or j.get("created_at"),
                        "error": err.get("message"), "link": "#/music",
                        "detail": {"phase": j.get("phase"), "tracks": len(j.get("tracks") or []),
                                   "via": local.get("via")}})
        return out

    def realtime(user: str, since: float, limit: int) -> list[dict]:
        out = []
        for s in app.realtime.list(owner=f"user:{user}"):
            status = "running" if s["active"] else ("failed" if s["disposition"] == "failed" else "ok")
            out.append({"id": s["session_id"], "title": f"{'Call' if s['service'] == 'call' else 'Live'} session",
                        "status": status, "at": s["ended"] or s["last_connect"] or s["created"],
                        "duration_ms": int(((s["ended"] or time.time()) - s["created"]) * 1000),
                        "link": "#/call" if s["service"] == "call" else "#/live",
                        "detail": {"disposition": s["disposition"], "connects": s["connects"]}})
        return out

    def audit(user: str, since: float, limit: int) -> list[dict]:
        path = Path(app.actions.audit_path)
        try:
            size = path.stat().st_size
            with path.open("rb") as fh:
                fh.seek(max(0, size - 2 * 2**20))
                lines = fh.read().splitlines()[-4000:]
        except OSError:
            return []
        out = []
        for raw in lines:
            try:
                e = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(e, dict) or e.get("user") != user:
                continue
            action = str(e.get("action") or "")
            ts = _parse_ts(e.get("ts"))
            if ts < since:
                continue
            outcome = str(e.get("outcome") or "ok")
            out.append({"id": f"audit-{int(ts)}-{action}"[:80], "title": action, "at": ts,
                        "status": "ok" if outcome in ("ok", "queued", "saved") else
                        ("failed" if outcome in ("failed", "refused", "throttled") else outcome),
                        "error": e.get("reason") or e.get("detail") if outcome != "ok" else None,
                        "detail": {"outcome": outcome, "ip": e.get("ip")}})
        return out[-limit:]

    def metrics(user: str, since: float, limit: int) -> list[dict]:
        from .obs import metrics_files, read_metrics
        out = []
        for m in read_metrics(metrics_files(app.cfg.metrics_dir), user=f"user:{user}", since=since, limit=limit):
            ts = _parse_ts(m.get("ts"))
            title = f"{m.get('service')}: {m.get('event')}"
            out.append({"id": f"m-{ts:.3f}-{m.get('event')}"[:80], "title": title, "at": ts,
                        "status": m.get("outcome") or "ok", "duration_ms": m.get("duration_ms"),
                        "error": m.get("message") or m.get("error_code") if m.get("outcome") == "failed" else None,
                        "detail": {k: v for k, v in m.items()
                                   if k in ("alias", "operation", "session_id", "disposition", "error_code",
                                            "waiting_reason", "node", "flow_id", "run_id")}})
        return out

    return {"media": media, "music": music, "realtime": realtime, "account": audit, "metrics": metrics}


def _parse_ts(value: Any) -> float:
    from datetime import datetime
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return 0.0
    for candidate in (value, value[:-2] + ":" + value[-2:] if re.search(r"[+-]\d{4}$", value) else value):
        try:
            return datetime.fromisoformat(candidate).timestamp()
        except ValueError:
            continue
    return 0.0
