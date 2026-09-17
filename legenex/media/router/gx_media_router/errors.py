"""Structured errors that map cleanly onto HTTP status codes."""

from __future__ import annotations


class RouterError(Exception):
    """Base class. ``status`` is the HTTP status to return to the caller."""

    status = 500
    code = "internal_error"

    def __init__(self, message: str, *, param: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.param = param

    def payload(self) -> dict:
        return {
            "error": {
                "message": self.message,
                "type": self.code,
                "param": self.param,
                "code": self.code,
            }
        }


class ValidationError(RouterError):
    status = 400
    code = "invalid_request_error"


class AuthError(RouterError):
    status = 401
    code = "authentication_error"


class NotFoundError(RouterError):
    status = 404
    code = "not_found_error"


class BusyError(RouterError):
    """The single global generation slot could not be acquired in time."""

    status = 503
    code = "service_busy"


class InsufficientMemoryError(RouterError):
    """gx10-02 does not have the memory this job needs (usually gx-reason is loaded)."""

    status = 503
    code = "insufficient_memory"


class UpstreamError(RouterError):
    """ComfyUI rejected the graph, failed to execute it, or is unreachable."""

    status = 502
    code = "upstream_error"


class TimeoutError_(RouterError):
    status = 504
    code = "timeout_error"
