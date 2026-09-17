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


class ConflictError(RouterError):
    """The request conflicts with the job's state (e.g. cancelling a running job)."""

    status = 409
    code = "conflict"


class BusyError(RouterError):
    """The single global generation slot could not be acquired in time."""

    status = 503
    code = "service_busy"


class InsufficientMemoryError(RouterError):
    """Starting this job now would take gx10-02 below the 30 GiB reserve (D-038).

    ``details`` says why, with numbers: required / available / reserve GiB,
    the blocking tenant and what happens next. A video job WAITS on this
    error (``retryable``); a synchronous image request returns it as 503.
    """

    status = 503
    code = "insufficient_memory"

    def __init__(self, message: str, *, details: dict | None = None, retryable: bool = True) -> None:
        super().__init__(message)
        self.details = details or {}
        self.retryable = retryable

    def payload(self) -> dict:
        body = super().payload()
        body["error"]["details"] = self.details
        return body


class ExceedsNodeError(InsufficientMemoryError):
    """The job can never run on gx10-02 while keeping the reserve (measured
    growth + reserve is more than the node ever has available)."""

    status = 422
    code = "exceeds_node_reserve"

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message, details=details, retryable=False)


class UpstreamError(RouterError):
    """ComfyUI rejected the graph, failed to execute it, or is unreachable."""

    status = 502
    code = "upstream_error"


class TimeoutError_(RouterError):
    status = 504
    code = "timeout_error"


class PolicyBlockedError(RouterError):
    """Cluster policy (gx-max hold, Maintenance mode) forbids starting a new job now."""

    status = 503

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code
