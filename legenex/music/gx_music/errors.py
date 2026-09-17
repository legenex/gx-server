"""Typed errors. Every error has a stable machine code and a human message.

The message is safe to show to an end user (no paths, no tracebacks). Details
for admins go to the structured log, never to the response.
"""

from __future__ import annotations


class MusicError(Exception):
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


class ValidationError(MusicError):
    status = 400
    code = "invalid_request"


class AuthError(MusicError):
    status = 401
    code = "unauthorized"


class NotFoundError(MusicError):
    status = 404
    code = "not_found"


class ConflictError(MusicError):
    status = 409
    code = "conflict"


class TooLargeError(MusicError):
    status = 413
    code = "payload_too_large"


class UnavailableError(MusicError):
    """The service cannot take this right now (drained for gx-max, queue full)."""

    status = 503
    code = "unavailable"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message, code=code, retryable=True)


class EngineError(MusicError):
    """The engine failed or misbehaved. Message is already user-safe."""

    status = 502
    code = "generation_failed"


class ResourceWait(Exception):
    """Admission refused for now; the job should wait and retry."""

    def __init__(self, reason: str, *, code: str = "insufficient_memory") -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code
