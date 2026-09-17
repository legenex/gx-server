"""Build V3 MUS routes: Music conditioning preview, Build with AI, Improve My
Prompt and reference analysis (D-040, coordination/build-v3/mus.md).

Registered by importing this module at the end of server.py. Every route is a
browser route: session authentication, CSRF and the same-origin check come
from the server; gx-auto and gx10-02 are only ever called server-side.

    POST /api/music/preview               exact caption / lyrics / vocal mode for a Create request
    POST /api/music/ai/build              {prompt, current, locked, write_lyrics} -> settings + changes
    POST /api/music/ai/improve            {current, locked, improve_lyrics, instruction} -> merged + diff
    POST /api/music/reference/analyze     {source: {kind: asset|url, ...}, understand, suggest, hint}
    GET  /api/music/reference/{id}        progress and results (measured / model / inferred)
"""

from __future__ import annotations

import functools
from collections.abc import Callable

from .music_ai import MusicAIError
from .music_reference import ReferenceError_
from .redact import redact
from .server import MAX_BODY, Handler, route

AI_BODY = 32 * 1024


def mus_route(method: str, pattern: str) -> Callable:
    def deco(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(h: Handler, **kw: str) -> None:
            try:
                fn(h, **kw)
            except (MusicAIError, ReferenceError_) as exc:
                h._json(exc.status, {"error": {"message": redact(str(exc)), "code": exc.code}})
        route(method, pattern)(wrapper)
        return wrapper
    return deco


def _user(h: Handler) -> str:
    assert h.session is not None  # noqa: S101 - guaranteed by the dispatcher for session routes
    return h.session.username


@mus_route("POST", r"/api/music/preview")
def api_music_preview(h: Handler) -> None:
    h._json(200, h.app.music.preview(h._body(MAX_BODY)))


@mus_route("POST", r"/api/music/ai/build")
def api_music_ai_build(h: Handler) -> None:
    body = h._body(AI_BODY)
    write_lyrics = body.get("write_lyrics", True)
    if not isinstance(write_lyrics, bool):
        raise ValueError("write_lyrics must be true or false")
    h._json(200, h.app.music_ai.build(body.get("prompt"), body.get("current"), body.get("locked"),
                                      write_lyrics=write_lyrics, user=_user(h)))


@mus_route("POST", r"/api/music/ai/improve")
def api_music_ai_improve(h: Handler) -> None:
    body = h._body(AI_BODY)
    improve_lyrics = body.get("improve_lyrics", False)
    if not isinstance(improve_lyrics, bool):
        raise ValueError("improve_lyrics must be true or false")
    h._json(200, h.app.music_ai.improve(body.get("current"), body.get("locked"), improve_lyrics=improve_lyrics,
                                        instruction=body.get("instruction"), user=_user(h)))


@mus_route("POST", r"/api/music/reference/analyze")
def api_music_reference_start(h: Handler) -> None:
    h._json(202, h.app.music_reference.start(h._body(MAX_BODY), user=_user(h)))


@mus_route("GET", r"/api/music/reference/(?P<sid>[0-9a-f]{24})")
def api_music_reference_get(h: Handler, sid: str) -> None:
    h._json(200, h.app.music_reference.get(sid, user=_user(h)))
