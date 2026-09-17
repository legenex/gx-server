"""Typed errors with stable machine codes and user-safe messages."""

from __future__ import annotations


class CallError(Exception):
    status = 500
    code = "internal_error"

    def __init__(self, message: str, *, code: str | None = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.retryable = retryable

    def payload(self) -> dict:
        return {"error": {"code": self.code, "message": self.message, "retryable": self.retryable}}


class ValidationError(CallError):
    status = 400
    code = "invalid_request"


class AuthError(CallError):
    status = 401
    code = "unauthorized"


class NotFoundError(CallError):
    status = 404
    code = "not_found"


class ConflictError(CallError):
    status = 409
    code = "conflict"


class TooLargeError(CallError):
    status = 413
    code = "payload_too_large"


class UnavailableError(CallError):
    status = 503
    code = "unavailable"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message, code=code, retryable=True)


class EngineError(CallError):
    status = 502
    code = "engine_failed"


class ResourceWait(Exception):
    """Admission refused for now; the load should wait and retry."""

    def __init__(self, reason: str, *, code: str = "insufficient_memory") -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code
