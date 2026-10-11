"""Typed errors for the Smartware HTTP binding.

Every error the server sends has the shape ``{"error": {"code", "message", "details"}}``. The client turns each
code into its own exception class, so callers can catch exactly what they mean to handle. Messages come from the
server and never contain tokens; the client never adds request bodies to them.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Type


class SmartwareError(Exception):
    """Base class for every error raised by the client.

    ``code`` is the protocol error code, ``status`` the HTTP status (``None`` when no response arrived), and
    ``details`` the server's structured details (for example ``{"parameters": [...]}`` or ``{"current": 3}``).
    """

    code: str = "error"

    def __init__(self, code: Optional[str] = None, message: str = "", status: Optional[int] = None,
                 details: Optional[Dict[str, Any]] = None, request_id: Optional[str] = None) -> None:
        self.code = code or type(self).code
        self.message = message
        self.status = status
        self.details: Dict[str, Any] = dict(details or {})
        self.request_id = request_id
        super().__init__(f"{self.code}: {message}" if message else self.code)


class UnsupportedParameterError(SmartwareError):
    """A field, parameter or enum value this server does not support. Never silently ignored."""
    code = "unsupported_parameter"


class InvalidParameterError(SmartwareError):
    code = "invalid_parameter"


class VersionConflictError(SmartwareError):
    """The record changed since you read it (``details["current"]`` holds the version now stored)."""
    code = "version_conflict"


class ForbiddenError(SmartwareError):
    code = "forbidden"


class NotFoundError(SmartwareError):
    """Absent, or present but not readable by you: the server makes the two look the same."""
    code = "not_found"


class BudgetTooSmallError(SmartwareError):
    code = "budget_too_small"


class RateLimitedError(SmartwareError):
    code = "rate_limited"


class UnauthorizedError(SmartwareError):
    code = "unauthorized"


class PayloadTooLargeError(SmartwareError):
    code = "payload_too_large"


class DegradedError(SmartwareError):
    code = "degraded"


class UnavailableError(SmartwareError):
    """The server is shutting down or overloaded (HTTP 503)."""
    code = "unavailable"


class InternalError(SmartwareError):
    code = "internal"


class TransportError(SmartwareError):
    """No usable response arrived (connection refused, reset, timeout, or a body that is not JSON).

    For a write without an idempotency key, the write may or may not have been applied. The client does not retry
    such writes, so it cannot have applied them twice.
    """
    code = "transport"


ERRORS: Dict[str, Type[SmartwareError]] = {c.code: c for c in (
    UnsupportedParameterError, InvalidParameterError, VersionConflictError, ForbiddenError, NotFoundError,
    BudgetTooSmallError, RateLimitedError, UnauthorizedError, PayloadTooLargeError, DegradedError,
    UnavailableError, InternalError, TransportError)}

_BY_STATUS = {400: "invalid_parameter", 401: "unauthorized", 403: "forbidden", 404: "not_found",
              409: "version_conflict", 413: "payload_too_large", 429: "rate_limited", 503: "unavailable"}


def error_from_response(status: int, body: Any, request_id: Optional[str] = None) -> SmartwareError:
    """Build the typed error for an HTTP error response. Unknown codes become a plain ``SmartwareError``."""
    code, message, details = None, "", {}
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        e = body["error"]
        code = e.get("code") if isinstance(e.get("code"), str) else None
        message = e.get("message") if isinstance(e.get("message"), str) else ""
        details = e.get("details") if isinstance(e.get("details"), dict) else {}
    if code is None:
        code = _BY_STATUS.get(status, "internal" if status >= 500 else "invalid_parameter")
        message = message or f"HTTP {status}"
    cls = ERRORS.get(code, SmartwareError)
    return cls(code, message, status, details, request_id)
