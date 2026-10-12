"""Forced credential change — TC-M01-06 … TC-M01-11, TC-M01-14, TC-M01-16.

These run through the real ASGI stack, not the service directly: the guarantee
under test *is* middleware behaviour (FR-A-05), so a service-level test would
verify nothing about the property that actually protects the system.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select

from cairn.core.config import get_settings
from cairn.core.db import session_scope
from cairn.identity.models import Session, User
from cairn.identity.service import IdentityService

STRONG = "Correct-Horse-Battery-9"
ANOTHER = "Another-Strong-Passphrase-7"


@pytest.fixture
async def bootstrapped(identity: IdentityService, monkeypatch: pytest.MonkeyPatch) -> str:
    """A freshly bootstrapped admin. Returns the initial password."""
    monkeypatch.delenv("CAIRN_INITIAL_ADMIN_PASSWORD", raising=False)
    result = await identity.bootstrap()
    assert result is not None and result.password is not None
    return result.password


def _cookie_name() -> str:
    return get_settings().auth.cookie_name


# --- TC-M01-06: login issues a change token and NO session -------------------


async def test_login_returns_change_token_and_no_session_cookie(
    client: httpx.AsyncClient, bootstrapped: str
) -> None:
    """TC-M01-06 / FR-A-04.

    The critical assertion is the *absence* of a session cookie. If one were
    issued and merely ignored by the UI, `curl` would walk straight past the
    requirement.
    """
    response = await client.post(
        "/v1/auth/login", json={"username": "admin", "password": bootstrapped}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "password_change_required"
    assert body["change_token"]
    assert body["reason"] == "initial_admin_setup"
    assert body["expires_in"] == 600
    assert body["policy"]["min_length"] >= 12
    assert body["user"]["username"] == "admin"

    assert _cookie_name() not in response.cookies
    assert "session_token" not in body


# --- TC-M01-07: the change token reaches nothing else ------------------------


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/v1/users"),
        ("POST", "/v1/auth/change-password"),
        ("GET", "/v1/knowledge-bases"),
        ("GET", "/v1/audit-log"),
    ],
)
async def test_change_token_is_rejected_everywhere_except_the_allowlist(
    client: httpx.AsyncClient, bootstrapped: str, method: str, path: str
) -> None:
    """TC-M01-07 / FR-A-05."""
    login = await client.post(
        "/v1/auth/login", json={"username": "admin", "password": bootstrapped}
    )
    token = login.json()["change_token"]

    response = await client.request(
        method, path, headers={"Authorization": f"Bearer {token}"}, json={}
    )

    assert response.status_code in (403, 404)
    if response.status_code == 403:
        assert response.json()["code"] == "PASSWORD_CHANGE_REQUIRED"
        assert response.headers["content-type"].startswith("application/problem+json")


async def test_me_is_allowlisted_so_the_dialog_can_name_the_account(
    client: httpx.AsyncClient, bootstrapped: str
) -> None:
    """FR-A-05: the allowlist is exactly complete-setup, logout, and /me."""
    login = await client.post(
        "/v1/auth/login", json={"username": "admin", "password": bootstrapped}
    )
    token = login.json()["change_token"]

    response = await client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json()["must_change_password"] is True
    assert response.json()["user"]["username"] == "admin"


# --- TC-M01-08: the requirement survives a restart ---------------------------


async def test_requirement_persists_across_a_process_restart(
    client: httpx.AsyncClient, bootstrapped: str
) -> None:
    """TC-M01-08 / FR-A-06.

    Simulates the user closing the browser — or the container being recreated —
    between login and completing the change. The flag lives in PostgreSQL, so
    nothing about the client or the process holds the state.
    """
    first = await client.post(
        "/v1/auth/login", json={"username": "admin", "password": bootstrapped}
    )
    assert first.json()["status"] == "password_change_required"

    # Tear the process state down: new app instance, new client, fresh caches.
    from apps.api.main import create_app
    from cairn.core.db import dispose_engine
    from cairn.identity.service import reset_identity_service

    await dispose_engine()
    reset_identity_service()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app()), base_url="http://testserver"
    ) as fresh:
        second = await fresh.post(
            "/v1/auth/login", json={"username": "admin", "password": bootstrapped}
        )

    assert second.status_code == 200
    assert second.json()["status"] == "password_change_required"
    assert _cookie_name() not in second.cookies


# --- TC-M01-09: a leaked full session is inert -------------------------------


async def test_a_full_session_for_a_flagged_user_is_blocked_by_middleware(
    client: httpx.AsyncClient, bootstrapped: str, identity: IdentityService
) -> None:
    """TC-M01-09 / FR-A-05 — the belt-and-braces layer.

    Fabricates exactly what a future bug might produce: an unscoped, valid
    session for a user who still owes a credential change. The middleware must
    make it useless for anything outside the allowlist.
    """
    from datetime import timedelta

    from cairn.core.time import utcnow
    from cairn.identity.hashing import hash_token, new_token

    async with session_scope() as session:
        user = await session.scalar(select(User).where(User.username == "admin"))
        assert user is not None and user.must_change_password is True

        leaked = new_token()
        session.add(
            Session(
                workspace_id=user.workspace_id,
                user_id=user.id,
                token_hash=hash_token(leaked),
                credential_version=user.credential_version,
                scopes=[],  # a FULL session, not a change token
                expires_at=utcnow() + timedelta(hours=8),
            )
        )
        await session.commit()

    blocked = await client.get("/v1/users", headers={"Authorization": f"Bearer {leaked}"})
    assert blocked.status_code in (403, 404)
    if blocked.status_code == 403:
        assert blocked.json()["code"] == "PASSWORD_CHANGE_REQUIRED"

    # ...but the change flow itself remains reachable.
    allowed = await client.get("/v1/me", headers={"Authorization": f"Bearer {leaked}"})
    assert allowed.status_code == 200


# --- TC-M01-10 / TC-M01-11: completing the change ----------------------------


async def test_completing_setup_changes_username_and_password_atomically(
    client: httpx.AsyncClient, bootstrapped: str
) -> None:
    """TC-M01-10 / FR-A-07 / FR-A-13."""
    login = await client.post(
        "/v1/auth/login", json={"username": "admin", "password": bootstrapped}
    )
    token = login.json()["change_token"]

    response = await client.post(
        "/v1/auth/complete-initial-setup",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "current_password": bootstrapped,
            "new_password": STRONG,
            "confirm_password": STRONG,
            "new_username": "dana.ops",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["user"]["username"] == "dana.ops"
    assert body["user"]["must_change_password"] is False
    assert _cookie_name() in response.cookies  # a real session, finally

    async with session_scope() as session:
        user = await session.scalar(select(User).where(User.username == "dana.ops"))
    assert user is not None
    assert user.must_change_password is False
    assert user.credential_version == 2  # FR-A-13
    assert user.password_changed_at is not None


async def test_taken_username_rolls_back_the_password_change(
    client: httpx.AsyncClient, bootstrapped: str, identity: IdentityService
) -> None:
    """TC-M01-11 / FR-A-07 — the atomicity that makes this one transaction.

    A partial success here would be the worst outcome: the password silently
    changed to something the user believes was rejected, leaving them locked out
    with a credential they never wrote down.
    """
    async with session_scope() as session:
        admin = await session.scalar(select(User).where(User.username == "admin"))
        assert admin is not None
        session.add(
            User(
                workspace_id=admin.workspace_id,
                username="taken.name",
                role="user",
                password_hash=admin.password_hash,
            )
        )
        await session.commit()

    login = await client.post(
        "/v1/auth/login", json={"username": "admin", "password": bootstrapped}
    )
    token = login.json()["change_token"]

    response = await client.post(
        "/v1/auth/complete-initial-setup",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "current_password": bootstrapped,
            "new_password": STRONG,
            "new_username": "taken.name",
        },
    )

    assert response.status_code == 409
    assert response.json()["code"] == "USERNAME_TAKEN"

    # Nothing changed: the original password still works and the flag stands.
    async with session_scope() as session:
        user = await session.scalar(select(User).where(User.username == "admin"))
    assert user is not None
    assert user.must_change_password is True
    assert user.credential_version == 1

    retry = await client.post(
        "/v1/auth/login", json={"username": "admin", "password": bootstrapped}
    )
    assert retry.json()["status"] == "password_change_required"


async def test_username_is_optional(client: httpx.AsyncClient, bootstrapped: str) -> None:
    """FR-A-07: renaming is offered, not required."""
    login = await client.post(
        "/v1/auth/login", json={"username": "admin", "password": bootstrapped}
    )
    response = await client.post(
        "/v1/auth/complete-initial-setup",
        headers={"Authorization": f"Bearer {login.json()['change_token']}"},
        json={"current_password": bootstrapped, "new_password": STRONG},
    )
    assert response.status_code == 200
    assert response.json()["user"]["username"] == "admin"


async def test_wrong_current_password_is_rejected(
    client: httpx.AsyncClient, bootstrapped: str
) -> None:
    login = await client.post(
        "/v1/auth/login", json={"username": "admin", "password": bootstrapped}
    )
    response = await client.post(
        "/v1/auth/complete-initial-setup",
        headers={"Authorization": f"Bearer {login.json()['change_token']}"},
        json={"current_password": "not-it", "new_password": STRONG},
    )
    assert response.status_code == 401
    assert response.json()["code"] == "AUTHENTICATION_FAILED"


async def test_reusing_the_bootstrap_password_is_rejected(
    client: httpx.AsyncClient, bootstrapped: str
) -> None:
    """TC-M01-13 / FR-A-09: 'change' must mean change."""
    login = await client.post(
        "/v1/auth/login", json={"username": "admin", "password": bootstrapped}
    )
    response = await client.post(
        "/v1/auth/complete-initial-setup",
        headers={"Authorization": f"Bearer {login.json()['change_token']}"},
        json={"current_password": bootstrapped, "new_password": bootstrapped},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "PASSWORD_REUSED"


async def test_policy_violations_are_reported_per_rule(
    client: httpx.AsyncClient, bootstrapped: str
) -> None:
    """TC-M01-12 / FR-A-09: every failure at once, each with a machine code."""
    login = await client.post(
        "/v1/auth/login", json={"username": "admin", "password": bootstrapped}
    )
    response = await client.post(
        "/v1/auth/complete-initial-setup",
        headers={"Authorization": f"Bearer {login.json()['change_token']}"},
        json={"current_password": bootstrapped, "new_password": "short"},
    )
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "PASSWORD_POLICY_VIOLATION"
    codes = {e["code"] for e in body["errors"]}
    assert "TOO_SHORT" in codes


async def test_mismatched_confirmation_is_rejected(
    client: httpx.AsyncClient, bootstrapped: str
) -> None:
    login = await client.post(
        "/v1/auth/login", json={"username": "admin", "password": bootstrapped}
    )
    response = await client.post(
        "/v1/auth/complete-initial-setup",
        headers={"Authorization": f"Bearer {login.json()['change_token']}"},
        json={
            "current_password": bootstrapped,
            "new_password": STRONG,
            "confirm_password": ANOTHER,
        },
    )
    assert response.status_code == 400
    assert response.json()["code"] == "VALIDATION_FAILED"


# --- TC-M01-14: credential_version invalidation ------------------------------


async def test_change_token_is_single_use(client: httpx.AsyncClient, bootstrapped: str) -> None:
    """TC-M01-14 / FR-A-13: bumping credential_version kills every prior token."""
    login = await client.post(
        "/v1/auth/login", json={"username": "admin", "password": bootstrapped}
    )
    token = login.json()["change_token"]

    first = await client.post(
        "/v1/auth/complete-initial-setup",
        headers={"Authorization": f"Bearer {token}"},
        json={"current_password": bootstrapped, "new_password": STRONG},
    )
    assert first.status_code == 200

    replay = await client.post(
        "/v1/auth/complete-initial-setup",
        headers={"Authorization": f"Bearer {token}"},
        json={"current_password": STRONG, "new_password": ANOTHER},
    )
    assert replay.status_code == 401
    assert replay.json()["code"] == "SETUP_TOKEN_INVALID"


async def test_expired_change_token_is_rejected(
    client: httpx.AsyncClient, bootstrapped: str
) -> None:
    from datetime import timedelta

    from cairn.core.time import utcnow

    login = await client.post(
        "/v1/auth/login", json={"username": "admin", "password": bootstrapped}
    )
    token = login.json()["change_token"]

    from cairn.identity.hashing import hash_token

    async with session_scope() as session:
        record = await session.scalar(
            select(Session).where(Session.token_hash == hash_token(token))
        )
        assert record is not None
        record.expires_at = utcnow() - timedelta(seconds=1)
        await session.commit()

    response = await client.post(
        "/v1/auth/complete-initial-setup",
        headers={"Authorization": f"Bearer {token}"},
        json={"current_password": bootstrapped, "new_password": STRONG},
    )
    assert response.status_code == 401
    assert response.json()["code"] == "SETUP_TOKEN_INVALID"


# --- after the change: normal operation --------------------------------------


async def test_after_completion_login_returns_a_normal_session(
    client: httpx.AsyncClient, bootstrapped: str
) -> None:
    login = await client.post(
        "/v1/auth/login", json={"username": "admin", "password": bootstrapped}
    )
    await client.post(
        "/v1/auth/complete-initial-setup",
        headers={"Authorization": f"Bearer {login.json()['change_token']}"},
        json={"current_password": bootstrapped, "new_password": STRONG, "new_username": "dana.ops"},
    )

    async with httpx.AsyncClient(
        transport=client._transport, base_url="http://testserver"
    ) as fresh:
        again = await fresh.post(
            "/v1/auth/login", json={"username": "dana.ops", "password": STRONG}
        )

    assert again.status_code == 200
    assert again.json()["status"] == "ok"
    assert again.json()["change_token"] is None
    assert _cookie_name() in again.cookies


async def test_admin_forced_change_reuses_the_identical_flow(
    client: httpx.AsyncClient, bootstrapped: str, identity: IdentityService
) -> None:
    """TC-M01-16 / FR-A-12: one state machine, two entry points."""
    login = await client.post(
        "/v1/auth/login", json={"username": "admin", "password": bootstrapped}
    )
    await client.post(
        "/v1/auth/complete-initial-setup",
        headers={"Authorization": f"Bearer {login.json()['change_token']}"},
        json={"current_password": bootstrapped, "new_password": STRONG},
    )

    async with session_scope() as session:
        user = await session.scalar(select(User).where(User.username == "admin"))
        assert user is not None
        principal_id, workspace_id = user.id, user.workspace_id

    from cairn.authz.model import Principal

    actor = Principal(
        type="user",
        id=principal_id,
        workspace_id=workspace_id,
        role="admin",
        username="admin",
    )
    await identity.force_password_change(actor, principal_id)

    again = await client.post("/v1/auth/login", json={"username": "admin", "password": STRONG})
    assert again.json()["status"] == "password_change_required"
    assert again.json()["reason"] == "initial_admin_setup"
    assert _cookie_name() not in again.cookies
