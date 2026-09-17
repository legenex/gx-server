"""Build V3 IMG routes: gx-image generation history, provenance and lineage.

Registered by importing this module at the end of server.py. Every route is a
browser route: it inherits session authentication, CSRF and the same-origin
check from the server, and it is read-only (the Images page creates work
through the shared ``POST /api/media/jobs``).

    GET /api/images/generations                 history (?kind, image_model, source, limit, offset)
    GET /api/images/generations/{id}            one generation with its outputs
    GET /api/images/assets/{id}/lineage         the recorded edit chain, oldest first
    GET /api/images/models                      the catalogue plus what actually ran

Data comes from ``migrations/070_images.sql`` through
``image_catalog.ImageHistory``, which observes every image media job.
"""

from __future__ import annotations

from .image_catalog import CatalogError, ImageHistory
from .server import Handler, _q, route

_JOB = r"(?P<gid>[0-9a-f]{16})"
_ASSET = r"(?P<aid>[A-Za-z0-9_-]{1,64})"


def _history(h: Handler) -> ImageHistory:
    """The app's history store, with a clear error if App.__init__ lacks it."""
    store = getattr(h.app, "image_history", None)
    if not isinstance(store, ImageHistory):  # pragma: no cover - a wiring mistake
        raise RuntimeError("gx-image history is not wired up: build ImageHistory in App.__init__")
    return store


def _int(h: Handler, key: str, default: int) -> int:
    raw = _q(h, key, str(default)) or str(default)
    try:
        return int(raw)
    except ValueError:
        raise CatalogError(f"{key} must be a whole number") from None


@route("GET", r"/api/images/generations")
def api_image_generations(h: Handler) -> None:
    try:
        payload = _history(h).generations(
            limit=_int(h, "limit", 30), offset=_int(h, "offset", 0),
            kind=_q(h, "kind") or None, image_model=_q(h, "image_model") or None,
            source_id=_q(h, "source") or None)
    except CatalogError as exc:
        h._json(400, {"error": {"message": str(exc)}})
        return
    h._json(200, payload)


@route("GET", rf"/api/images/generations/{_JOB}")
def api_image_generation(h: Handler, gid: str) -> None:
    try:
        h._json(200, _history(h).generation(gid))
    except CatalogError as exc:
        h._json(404, {"error": {"message": str(exc)}})


@route("GET", rf"/api/images/assets/{_ASSET}/lineage")
def api_image_lineage(h: Handler, aid: str) -> None:
    h._json(200, {"object": "list", "asset_id": aid, "data": _history(h).lineage(aid)})


@route("GET", r"/api/images/models")
def api_image_models(h: Handler) -> None:
    catalog = h.app.image_catalog.options()
    h._json(200, {"models": catalog["models"], "edit_modes": catalog["edit_modes"],
                  "default_generate": catalog["default_generate"], "default_edit": catalog["default_edit"],
                  "usage": _history(h).model_usage()})
