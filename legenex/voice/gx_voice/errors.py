"""Typed errors. Every error has a stable machine code and a user-safe message.

Messages never contain paths, tracebacks or secrets; details go to the log.
"""

from __future__ import annotations


class VoiceError(Exception):
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


class ValidationError(VoiceError):
    status = 400
    code = "invalid_request"


class AuthError(VoiceError):
    status = 401
    code = "unauthorized"


class NotFoundError(VoiceError):
    status = 404
    code = "not_found"


class ConflictError(VoiceError):
    status = 409
    code = "conflict"


class TooLargeError(VoiceError):
    status = 413
    code = "payload_too_large"


class UnavailableError(VoiceError):
    """Not now: drained for gx-max, Maintenance, queue full, timed out waiting."""

    status = 503
    code = "unavailable"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message, code=code, retryable=True)


class EngineError(VoiceError):
    """The engine failed or misbehaved. The message is already user-safe."""

    status = 502
    code = "generation_failed"


class ResourceWait(Exception):
    """Admission refused for now; the job waits and retries."""

    def __init__(self, reason: str, *, code: str = "insufficient_memory") -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code
