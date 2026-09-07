"""User management endpoints (FR-B-02)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field

from cairn.authz.deps import require_permission
from cairn.authz.model import Principal
from cairn.core.ids import decode_id
from cairn.identity.dto import CreateUserSpec, UpdateUserSpec
from cairn.identity.schemas import UserResponse
from cairn.identity.service import IdentityService, get_identity_service

__all__ = ["router"]

router = APIRouter(prefix="/v1/users", tags=["users"])
PROBLEM: dict[int | str, dict[str, Any]] = {
    403: {"content": {"application/problem+json": {}}},
    409: {"content": {"application/problem+json": {}}},
}

_Strict = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CreateUserRequest(BaseModel):
    model_config = _Strict
    username: str = Field(min_length=3, max_length=255, pattern=r"^[A-Za-z0-9._@-]+$")
    email: EmailStr | None = None
    display_name: str | None = Field(default=None, max_length=255)
    role: Literal["admin", "user"] = "user"


class UpdateUserRequest(BaseModel):
    model_config = _Strict
    email: EmailStr | None = None
    display_name: str | None = Field(default=None, max_length=255)
    role: Literal["admin", "user"] | None = None
    is_active: bool | None = None


class CreatedUserResponse(BaseModel):
    user: UserResponse
    #: Shown once. The account must change it at first login, so this is a
    #: handover credential rather than a password the administrator retains.
    initial_password: str | None
    warning: str = "This password is displayed once and cannot be retrieved again."


class ForcedChangeResponse(BaseModel):
    user_id: str
    must_change_password: Literal[True] = True
    effective_at: datetime


def _service() -> IdentityService:
    return get_identity_service()


@router.get(
    "",
    response_model=list[UserResponse],
    responses=PROBLEM,
    summary="List users",
)
async def list_users(
    actor: Annotated[Principal, Depends(require_permission("platform:users"))],
    service: Annotated[IdentityService, Depends(_service)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[str | None, Query()] = None,
) -> list[UserResponse]:
    users = await service.list_users(
        actor.workspace_id,
        limit=limit,
        cursor_id=decode_id("usr", cursor) if cursor else None,
    )
    return [UserResponse.from_dto(u) for u in users]


@router.post(
    "",
    response_model=CreatedUserResponse,
    status_code=status.HTTP_201_CREATED,
    responses=PROBLEM,
    summary="Create a user",
    description=(
        "The new account starts with a mandatory credential change, so the "
        "generated password is a one-time handover rather than a shared secret."
    ),
)
async def create_user(
    body: CreateUserRequest,
    actor: Annotated[Principal, Depends(require_permission("platform:users"))],
    service: Annotated[IdentityService, Depends(_service)],
) -> CreatedUserResponse:
    user, password = await service.create_user(
        actor,
        CreateUserSpec(
            username=body.username,
            email=str(body.email) if body.email else None,
            display_name=body.display_name,
            role=body.role,
        ),
    )
    return CreatedUserResponse(user=UserResponse.from_dto(user), initial_password=password)


@router.get(
    "/{user_id}",
    response_model=UserResponse,
    responses=PROBLEM,
    summary="Get a user",
)
async def get_user(
    user_id: str,
    _: Annotated[Principal, Depends(require_permission("platform:users"))],
    service: Annotated[IdentityService, Depends(_service)],
) -> UserResponse:
    return UserResponse.from_dto(await service.get_user(decode_id("usr", user_id)))


@router.patch(
    "/{user_id}",
    response_model=UserResponse,
    responses=PROBLEM,
    summary="Update a user",
)
async def update_user(
    user_id: str,
    body: UpdateUserRequest,
    actor: Annotated[Principal, Depends(require_permission("platform:users"))],
    service: Annotated[IdentityService, Depends(_service)],
) -> UserResponse:
    user = await service.update_user(
        actor,
        decode_id("usr", user_id),
        UpdateUserSpec(
            email=str(body.email) if body.email else None,
            display_name=body.display_name,
            role=body.role,
            is_active=body.is_active,
        ),
    )
    return UserResponse.from_dto(user)


@router.delete(
    "/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=PROBLEM,
    summary="Delete a user",
    description=(
        "Soft delete. Revokes every session, grant, and API key the account "
        "held — a key that outlives its owner is a credential nobody is "
        "accountable for."
    ),
)
async def delete_user(
    user_id: str,
    actor: Annotated[Principal, Depends(require_permission("platform:users"))],
    service: Annotated[IdentityService, Depends(_service)],
) -> None:
    await service.delete_user(actor, decode_id("usr", user_id))


@router.post(
    "/{user_id}/force-password-change",
    response_model=ForcedChangeResponse,
    responses=PROBLEM,
    summary="Require a credential change at next login",
    description="Reuses the identical forced flow as first-boot bootstrap (FR-A-12).",
)
async def force_password_change(
    user_id: str,
    actor: Annotated[Principal, Depends(require_permission("platform:users"))],
    service: Annotated[IdentityService, Depends(_service)],
) -> ForcedChangeResponse:
    from cairn.core.time import utcnow

    target = decode_id("usr", user_id)
    await service.force_password_change(actor, target)
    return ForcedChangeResponse(user_id=user_id, effective_at=utcnow())
