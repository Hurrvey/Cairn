"""Request/response models for the auth endpoints.

``extra="forbid"`` on every write body (NFR-SEC-05): an unknown field is far
more likely to be a client bug or an attack probe than a harmless extra.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from cairn.core.ids import encode_id
from cairn.identity.dto import LoginResult, PasswordPolicyView, UserView

__all__ = [
    "ChangePasswordRequest",
    "CompleteSetupRequest",
    "LoginRequest",
    "LoginResponse",
    "MeResponse",
    "PolicyResponse",
    "UserResponse",
]

_Strict = ConfigDict(extra="forbid", str_strip_whitespace=True)


class LoginRequest(BaseModel):
    model_config = _Strict
    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=256)


class CompleteSetupRequest(BaseModel):
    model_config = _Strict
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)
    confirm_password: str | None = Field(default=None, max_length=256)
    #: Optional — the administrator may rename the account in the same
    #: transaction as the password change (FR-A-07).
    new_username: str | None = Field(default=None, min_length=3, max_length=255)


class ChangePasswordRequest(BaseModel):
    model_config = _Strict
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


class PolicyResponse(BaseModel):
    min_length: int
    max_length: int
    require_classes: int
    classes: list[str]
    username_editable: bool
    disallow_previous: bool

    @classmethod
    def from_dto(cls, policy: PasswordPolicyView) -> PolicyResponse:
        return cls(
            min_length=policy.min_length,
            max_length=policy.max_length,
            require_classes=policy.require_classes,
            classes=list(policy.classes),
            username_editable=policy.username_editable,
            disallow_previous=policy.disallow_previous,
        )


class UserResponse(BaseModel):
    id: str
    username: str
    email: str | None
    display_name: str | None
    role: Literal["admin", "user"]
    must_change_password: bool
    is_active: bool
    last_login_at: datetime | None
    created_at: datetime

    @classmethod
    def from_dto(cls, user: UserView) -> UserResponse:
        return cls(
            id=encode_id("usr", user.id),
            username=user.username,
            email=user.email,
            display_name=user.display_name,
            role=user.role,
            must_change_password=user.must_change_password,
            is_active=user.is_active,
            last_login_at=user.last_login_at,
            created_at=user.created_at,
        )


class LoginResponse(BaseModel):
    """Two shapes in one model, discriminated by ``status``.

    On ``password_change_required`` there is **no session cookie** on the
    response and no session token here — see FR-A-04.
    """

    status: Literal["ok", "password_change_required"]
    user: UserResponse
    reason: str | None = None
    change_token: str | None = None
    expires_in: int | None = None
    policy: PolicyResponse | None = None

    @classmethod
    def from_dto(cls, result: LoginResult) -> LoginResponse:
        return cls(
            status=result.status,
            user=UserResponse.from_dto(result.user),
            reason=result.reason,
            change_token=result.change_token,
            expires_in=result.expires_in,
            policy=PolicyResponse.from_dto(result.policy) if result.policy else None,
        )


class MeResponse(BaseModel):
    user: UserResponse
    workspace_id: str
    permissions: list[str]
    must_change_password: bool
