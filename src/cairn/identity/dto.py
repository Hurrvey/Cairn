"""Identity DTOs — the only shapes that cross this module's boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal
from uuid import UUID

__all__ = [
    "BootstrapResult",
    "CreateUserSpec",
    "LoginResult",
    "PasswordPolicyView",
    "SessionView",
    "UpdateUserSpec",
    "UserView",
]


@dataclass(frozen=True, slots=True)
class UserView:
    id: UUID
    workspace_id: UUID
    username: str
    email: str | None
    display_name: str | None
    role: Literal["admin", "user"]
    must_change_password: bool
    is_active: bool
    credential_version: int
    last_login_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class SessionView:
    id: UUID
    user_id: UUID
    workspace_id: UUID
    scopes: frozenset[str]
    expires_at: datetime
    credential_version: int
    #: SHA-256 of the CSRF token issued alongside a cookie session. ``None`` for
    #: bearer tokens, which are not ambient credentials and need no CSRF pairing.
    csrf_token_hash: str | None = None


@dataclass(frozen=True, slots=True)
class PasswordPolicyView:
    min_length: int
    max_length: int
    require_classes: int
    classes: tuple[str, ...] = ("lowercase", "uppercase", "digit", "symbol")
    username_editable: bool = True
    disallow_previous: bool = True


@dataclass(frozen=True, slots=True)
class LoginResult:
    """The pivotal return type (FR-A-04).

    ``session_token`` and ``change_token`` are mutually exclusive. When a
    credential change is required, **no session exists** — there is nothing to
    abuse, not merely something the UI declines to use.
    """

    status: Literal["ok", "password_change_required"]
    user: UserView
    session_token: str | None = None
    csrf_token: str | None = None
    change_token: str | None = None
    reason: str | None = None
    expires_in: int | None = None
    policy: PasswordPolicyView | None = None

    def __post_init__(self) -> None:
        if self.status == "password_change_required" and self.session_token is not None:
            raise AssertionError(  # pragma: no cover — structural invariant
                "a session token must never accompany password_change_required"
            )


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    user_id: UUID
    workspace_id: UUID
    username: str
    password: str | None
    password_printed: bool
    created: bool = True


@dataclass(frozen=True, slots=True)
class UpdateUserSpec:
    email: str | None = None
    display_name: str | None = None
    role: Literal["admin", "user"] | None = None
    is_active: bool | None = None


@dataclass(frozen=True, slots=True)
class CreateUserSpec:
    username: str
    role: Literal["admin", "user"] = "user"
    email: str | None = None
    display_name: str | None = None
    password: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)
