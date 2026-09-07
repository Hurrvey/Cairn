"""Authorization-specific errors."""

from __future__ import annotations

from cairn.core.errors import AuthenticationFailed, PermissionDenied, ValidationFailed

__all__ = [
    "ApiKeyInvalid",
    "ApiKeyIpNotAllowed",
    "BreakGlassReasonRequired",
    "ContentAccessRequiresGrant",
    "GrantExceedsOwner",
    "PlatformCapabilityNotGrantable",
]


class ApiKeyInvalid(AuthenticationFailed):
    code = "API_KEY_INVALID"
    title = "Invalid API key"


class ApiKeyIpNotAllowed(PermissionDenied):
    code = "API_KEY_IP_NOT_ALLOWED"
    title = "Source address not permitted for this key"


class ContentAccessRequiresGrant(PermissionDenied):
    """Raised when an administrator hits the break-glass boundary (FR-B-09)."""

    code = "CONTENT_ACCESS_REQUIRES_GRANT"
    title = "Content access requires an explicit grant"


class GrantExceedsOwner(ValidationFailed):
    code = "GRANT_EXCEEDS_OWNER"
    title = "Requested scope exceeds the owner's own permissions"


class PlatformCapabilityNotGrantable(ValidationFailed):
    code = "PLATFORM_CAPABILITY_NOT_GRANTABLE"
    title = "Platform capabilities are role-based and cannot be granted"


class BreakGlassReasonRequired(ValidationFailed):
    code = "BREAK_GLASS_REASON_REQUIRED"
    title = "A substantive reason is required to open break-glass access"
