"""Identity-specific errors.

``AuthenticationFailed`` is reused verbatim for unknown user, wrong password,
locked account, and inactive account. That is deliberate: distinguishing them
enables user enumeration (FR-A-11).
"""

from __future__ import annotations

from cairn.core.errors import AuthenticationFailed, CairnError, Conflict, ValidationFailed

__all__ = [
    "INVALID_CREDENTIALS_MESSAGE",
    "LastAdminProtected",
    "PasswordChangeRequired",
    "PasswordPolicyViolation",
    "PasswordReused",
    "SelfDeletionRefused",
    "SessionExpired",
    "SetupTokenInvalid",
    "UsernameTaken",
]

#: One message for every authentication failure mode.
INVALID_CREDENTIALS_MESSAGE = "Invalid username or password."


class PasswordChangeRequired(CairnError):
    """FR-A-05. Raised by middleware for every non-allowlisted route."""

    code = "PASSWORD_CHANGE_REQUIRED"
    http_status = 403
    title = "Credential change required"


class PasswordPolicyViolation(ValidationFailed):
    code = "PASSWORD_POLICY_VIOLATION"
    title = "Password does not meet the policy"


class PasswordReused(ValidationFailed):
    code = "PASSWORD_REUSED"
    title = "Password reused"


class UsernameTaken(Conflict):
    code = "USERNAME_TAKEN"
    title = "Username already in use"


class SetupTokenInvalid(AuthenticationFailed):
    code = "SETUP_TOKEN_INVALID"
    title = "Invalid or expired setup token"


class LastAdminProtected(Conflict):
    code = "LAST_ADMIN_PROTECTED"
    title = "The last administrator cannot be removed or demoted"


class SelfDeletionRefused(Conflict):
    code = "SELF_DELETION_REFUSED"
    title = "You cannot delete your own account"


class SessionExpired(AuthenticationFailed):
    code = "SESSION_EXPIRED"
    title = "Session expired"
