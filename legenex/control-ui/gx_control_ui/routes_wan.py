"""Build V3 WAN routes: the Wan 2.2 LoRA library, presets, workflow preview,
generation and video history (D-040).

Registered by importing this module at the end of server.py. Every route is a
browser route: it inherits session authentication, CSRF and the same-origin
check from the server. State changes happen only through POST.

    GET  /api/video/config                      defaults, ranges, sizes, model
    GET  /api/video/loras[?refresh=1]           library entries + files
    POST /api/video/loras/rescan                rescan gx10-02's LoRA roots
    POST /api/video/loras/order                 {"ids": [...]} library order
    GET  /api/video/loras/{entry}               one entry with file details
    POST /api/video/loras/{entry}               settings (name, tags, defaults, enabled, allow_unknown)
    POST /api/video/pairs                       {"high_file", "low_file"} manual pair
    POST /api/video/pairs/remove                {"entry_id"} unpair
    POST /api/video/pairs/restore               {"name"} undo an unpair of an automatic pair
    GET  /api/video/presets                     list
    POST /api/video/presets                     create {"name", "description", "data"}
    GET  /api/video/presets/{id}                one
    POST /api/video/presets/{id}                update / rename
    POST /api/video/presets/{id}/duplicate      {"name"?}
    POST /api/video/presets/{id}/delete         {"confirm": true}
    POST /api/video/presets/{id}/resolve        {"overrides": {...}} -> generate body (Creative Flows)
    POST /api/video/workflow                    build (not run) the ComfyUI graph for a request
    POST /api/video/generate                    submit (a media job; poll /api/media/jobs/{id})
    POST /api/video/jobs/{id}/cancel            cancel while queued or waiting
    GET  /api/video/generations                 history (?q, status, limit, offset)
    GET  /api/video/generations/{id}            details incl. workflow, chains, asset
    GET  /api/video/generations/{id}/workflow   download the workflow JSON
    GET  /api/video/errors                      recent failures with diagnostics
"""

from __future__ import annotations

import functools
from collections.abc import Callable

from .redact import redact
from .server import MAX_BODY, Handler, _q, route
from .wan_video import WanError, human_error

_ENTRY = r"(?P<eid>l_[0-9a-f]{16})"
_PRESET = r"(?P<pid>wp_[0-9a-f]{16})"
_GEN = r"(?P<gid>[0-9a-f]{16})"


def wan_route(method: str, pattern: str) -> Callable:
    def deco(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(h: Handler, **kw: str) -> None:
            try:
                fn(h, **kw)
            except WanError as exc:
                message = redact(str(exc))
                h._json(exc.status, {"error": {"message": message, "code": exc.code,
                                               "hint": human_error(exc.code, None) if exc.code in
                                               _HINTED else None}})
        route(method, pattern)(wrapper)
        return wrapper
    return deco


_HINTED = frozenset({"lora_not_found", "lora_unknown_compatibility", "lora_not_visible", "router_unavailable",
                     "lora_high_missing", "lora_low_missing"})


def _user(h: Handler) -> str:
    assert h.session is not None  # noqa: S101 - guaranteed by the dispatcher for session routes
    return h.session.username


# ----------------------------------------------------------------- library
@wan_route("GET", r"/api/video/config")
def api_video_config(h: Handler) -> None:
    h._json(200, h.app.wan.config())


@wan_route("GET", r"/api/video/loras")
def api_video_loras(h: Handler) -> None:
    h._json(200, h.app.wan.library_view(refresh=_q(h, "refresh") == "1"))


@wan_route("POST", r"/api/video/loras/rescan")
def api_video_rescan(h: Handler) -> None:
    h._body(MAX_BODY)
    h._json(200, h.app.wan.rescan(user=_user(h)))


@wan_route("POST", r"/api/video/loras/order")
def api_video_order(h: Handler) -> None:
    h._json(200, h.app.wan.reorder(h._body(MAX_BODY).get("ids"), user=_user(h)))


@wan_route("GET", rf"/api/video/loras/{_ENTRY}")
def api_video_entry(h: Handler, eid: str) -> None:
    h._json(200, h.app.wan.entry(eid))


@wan_route("POST", rf"/api/video/loras/{_ENTRY}")
def api_video_entry_update(h: Handler, eid: str) -> None:
    h._json(200, h.app.wan.update_entry(eid, h._body(MAX_BODY), user=_user(h)))


@wan_route("POST", r"/api/video/pairs")
def api_video_pair(h: Handler) -> None:
    h._json(200, h.app.wan.pair(h._body(MAX_BODY), user=_user(h)))


@wan_route("POST", r"/api/video/pairs/remove")
def api_video_unpair(h: Handler) -> None:
    eid = h._body(MAX_BODY).get("entry_id")
    if not isinstance(eid, str):
        raise WanError("entry_id is required")
    h._json(200, h.app.wan.unpair(eid, user=_user(h)))


@wan_route("POST", r"/api/video/pairs/restore")
def api_video_autopair(h: Handler) -> None:
    h._json(200, h.app.wan.restore_auto_pairing(h._body(MAX_BODY).get("name"), user=_user(h)))


# ----------------------------------------------------------------- presets
@wan_route("GET", r"/api/video/presets")
def api_video_presets(h: Handler) -> None:
    h._json(200, {"presets": h.app.wan.presets()})


@wan_route("POST", r"/api/video/presets")
def api_video_preset_create(h: Handler) -> None:
    h._json(200, h.app.wan.create_preset(h._body(MAX_BODY), user=_user(h)))


@wan_route("GET", rf"/api/video/presets/{_PRESET}")
def api_video_preset(h: Handler, pid: str) -> None:
    h._json(200, h.app.wan.preset(pid))


@wan_route("POST", rf"/api/video/presets/{_PRESET}")
def api_video_preset_update(h: Handler, pid: str) -> None:
    h._json(200, h.app.wan.update_preset(pid, h._body(MAX_BODY), user=_user(h)))


@wan_route("POST", rf"/api/video/presets/{_PRESET}/duplicate")
def api_video_preset_duplicate(h: Handler, pid: str) -> None:
    h._json(200, h.app.wan.duplicate_preset(pid, h._body(MAX_BODY), user=_user(h)))


@wan_route("POST", rf"/api/video/presets/{_PRESET}/delete")
def api_video_preset_delete(h: Handler, pid: str) -> None:
    if h._body(MAX_BODY).get("confirm") is not True:
        raise WanError("deleting a preset must be confirmed")
    h._json(200, h.app.wan.delete_preset(pid, user=_user(h)))


@wan_route("POST", rf"/api/video/presets/{_PRESET}/resolve")
def api_video_preset_resolve(h: Handler, pid: str) -> None:
    h._json(200, h.app.wan.resolve_preset(pid, h._body(MAX_BODY).get("overrides")))


# -------------------------------------------------------------- generation
@wan_route("POST", r"/api/video/workflow")
def api_video_workflow(h: Handler) -> None:
    h._json(200, h.app.wan.preview(h._body(MAX_BODY)))


@wan_route("POST", r"/api/video/generate")
def api_video_generate(h: Handler) -> None:
    h._json(202, h.app.wan.generate(h._body(MAX_BODY), user=_user(h), ip=h._client_ip()))


@wan_route("POST", rf"/api/video/jobs/{_GEN}/cancel")
def api_video_cancel(h: Handler, gid: str) -> None:
    h._body(MAX_BODY)
    h._json(200, h.app.media.cancel(gid, user=_user(h)))


# ----------------------------------------------------------------- history
@wan_route("GET", r"/api/video/generations")
def api_video_generations(h: Handler) -> None:
    try:
        limit, offset = int(_q(h, "limit", "30") or 30), int(_q(h, "offset", "0") or 0)
    except ValueError:
        raise WanError("limit and offset must be numbers") from None
    h._json(200, h.app.wan.generations(q=_q(h, "q"), status=_q(h, "status"), limit=limit, offset=offset))


@wan_route("GET", rf"/api/video/generations/{_GEN}")
def api_video_generation(h: Handler, gid: str) -> None:
    h._json(200, h.app.wan.generation(gid))


@wan_route("GET", rf"/api/video/generations/{_GEN}/workflow")
def api_video_generation_workflow(h: Handler, gid: str) -> None:
    data, name = h.app.wan.workflow_export(gid)
    disposition = "inline" if _q(h, "inline") == "1" else "attachment"
    h._send(200, data, "application/json; charset=utf-8",
            {"Content-Disposition": f'{disposition}; filename="{name}"', "Cache-Control": "no-store"})


@wan_route("GET", r"/api/video/errors")
def api_video_errors(h: Handler) -> None:
    try:
        limit = int(_q(h, "limit", "50") or 50)
    except ValueError:
        raise WanError("limit must be a number") from None
    h._json(200, h.app.wan.errors(limit))
