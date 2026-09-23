"""Error hierarchy and RFC 9457 (problem+json) serialization.

``code`` is the contract. Clients branch on it, so it MUST NOT change after
release. ``detail`` is human-facing and may change freely — but must never
contain secrets, SQL, stack traces, or internal hostnames.
"""

from __future__ import annotations

from typing import Any, ClassVar

__all__ = [
    "AuthenticationFailed",
    "BlockedAddress",
    "CairnError",
    "Conflict",
    "FieldError",
    "IdempotencyKeyReused",
    "InternalError",
    "InvalidId",
    "NotFound",
    "PayloadTooLarge",
    "PermissionDenied",
    "QuotaExceeded",
    "RateLimitExceeded",
    "RequestInProgress",
    "UpstreamUnavailable",
    "ValidationFailed",
]

_DOCS_BASE = "https://docs.cairn.io/errors/"


class FieldError(dict[str, Any]):
    """One entry in the ``errors[]`` array of a validation problem document."""

    def __init__(self, field: str, code: str, detail: str, value: Any = None) -> None:
        payload: dict[str, Any] = {"field": field, "code": code, "detail": detail}
        if value is not None:
            payload["value"] = value
        super().__init__(payload)


class CairnError(Exception):
    """Base for every error Cairn raises deliberately.

    Subclasses set ``code``/``http_status``/``title`` as class variables. Never
    catch this to swallow — catch it to translate.
    """

    code: ClassVar[str] = "INTERNAL_ERROR"
    http_status: ClassVar[int] = 500
    title: ClassVar[str] = "Internal error"

    def __init__(
        self,
        detail: str | None = None,
        *,
        errors: list[FieldError] | None = None,
        **context: Any,
    ) -> None:
        self.detail = detail or self.title
        self.errors = errors or []
        self.context = context
        super().__init__(self.detail)

    def to_problem(self, *, instance: str, request_id: str) -> dict[str, Any]:
        """Serialize to an RFC 9457 problem document."""
        problem: dict[str, Any] = {
            "type": f"{_DOCS_BASE}{self.code.lower().replace('_', '-')}",
            "title": self.title,
            "status": self.http_status,
            "code": self.code,
            "detail": self.detail,
            "instance": instance,
            "request_id": request_id,
        }
        if self.errors:
            problem["errors"] = list(self.errors)
        return problem


# --- 4xx ---------------------------------------------------------------------


class ValidationFailed(CairnError):
    code = "VALIDATION_FAILED"
    http_status = 400
    title = "Validation failed"


class InvalidId(ValidationFailed):
    code = "INVALID_ID"
    title = "Invalid identifier"


class BlockedAddress(ValidationFailed):
    code = "BLOCKED_ADDRESS"
    title = "Address not permitted"


class AuthenticationFailed(CairnError):
    code = "AUTHENTICATION_FAILED"
    http_status = 401
    title = "Authentication failed"


class PermissionDenied(CairnError):
    code = "PERMISSION_DENIED"
    http_status = 403
    title = "Permission denied"


class NotFound(CairnError):
    code = "RESOURCE_NOT_FOUND"
    http_status = 404
    title = "Resource not found"


class Conflict(CairnError):
    code = "RESOURCE_CONFLICT"
    http_status = 409
    title = "Resource conflict"


class RequestInProgress(Conflict):
    code = "REQUEST_IN_PROGRESS"
    title = "Request already in progress"


class IdempotencyKeyReused(CairnError):
    code = "IDEMPOTENCY_KEY_REUSED"
    http_status = 422
    title = "Idempotency key reused with a different payload"


class QuotaExceeded(CairnError):
    code = "QUOTA_EXCEEDED"
    http_status = 429
    title = "Quota exceeded"


class PayloadTooLarge(CairnError):
    code = "PAYLOAD_TOO_LARGE"
    http_status = 413
    title = "Payload too large"


class RateLimitExceeded(CairnError):
    code = "RATE_LIMIT_EXCEEDED"
    http_status = 429
    title = "Rate limit exceeded"

    def __init__(self, detail: str | None = None, *, retry_after: int = 60, **ctx: Any) -> None:
        super().__init__(detail, **ctx)
        self.retry_after = retry_after


# --- 5xx ---------------------------------------------------------------------


class UpstreamUnavailable(CairnError):
    code = "UPSTREAM_UNAVAILABLE"
    http_status = 502
    title = "Upstream service unavailable"


class InternalError(CairnError):
    code = "INTERNAL_ERROR"
    http_status = 500
    title = "Internal error"
