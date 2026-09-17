"""Typed errors with a stable code and a user-safe message (no paths, no secrets)."""

from __future__ import annotations


class LiveError(Exception):
    status = 500
    code = "internal_error"

    def __init__(self, message: str, *, code: str | None = None, retryable: bool = False,
                 extra: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.retryable = retryable
        self.extra = extra or {}

    def payload(self) -> dict:
        body = {"code": self.code, "message": self.message, "retryable": self.retryable}
        body.update(self.extra)
        return {"error": body}


class ValidationError(LiveError):
    status = 400
    code = "invalid_request"


class AuthError(LiveError):
    status = 401
    code = "unauthorized"


class ForbiddenError(LiveError):
    status = 403
    code = "forbidden"


class NotFoundError(LiveError):
    status = 404
    code = "not_found"


class ConflictError(LiveError):
    status = 409
    code = "conflict"


class GoneError(LiveError):
    status = 410
    code = "session_ended"


class TooLargeError(LiveError):
    status = 413
    code = "payload_too_large"


class UnavailableError(LiveError):
    status = 503
    code = "unavailable"

    def __init__(self, message: str, *, code: str | None = None, extra: dict | None = None) -> None:
        super().__init__(message, code=code, retryable=True, extra=extra)


class EngineError(LiveError):
    status = 502
    code = "engine_failed"


class ResourceWait(Exception):
    """Admission refused for now; the caller waits and retries."""

    def __init__(self, reason: str, *, code: str = "insufficient_memory", details: dict | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code
        self.details = details or {}

    def view(self) -> dict:
        return {"code": self.code, "reason": self.reason, **self.details}
