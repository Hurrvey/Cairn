"""Authentication hardening and health — TC-M01-15, TC-M00-13."""

from __future__ import annotations

import time

import httpx
import pytest
from sqlalchemy import select

from cairn.core.db import session_scope
from cairn.identity.models import User
from cairn.identity.service import IdentityService

STRONG = "Correct-Horse-Battery-9"


@pytest.fixture
async def active_admin(identity: IdentityService, monkeypatch: pytest.MonkeyPatch) -> str:
    """A bootstrapped admin that has completed its credential change."""
    monkeypatch.delenv("CAIRN_INITIAL_ADMIN_PASSWORD", raising=False)
    result = await identity.bootstrap()
    assert result is not None and result.password is not None
    login = await identity.login("admin", result.password)
    assert login.change_token is not None
    await identity.complete_initial_setup(
        login.change_token, current_password=result.password, new_password=STRONG
    )
    return STRONG


# --- enumeration and lockout -------------------------------------------------


async def test_unknown_user_and_wrong_password_are_indistinguishable(
    client: httpx.AsyncClient, active_admin: str
) -> None:
    """TC-M01-15 / FR-A-11: identical message, comparable timing."""
    unknown = await client.post(
        "/v1/auth/login", json={"username": "nobody-here", "password": "whatever"}
    )
    wrong = await client.post(
        "/v1/auth/login", json={"username": "admin", "password": "wrong-password"}
    )

    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json()["code"] == wrong.json()["code"] == "AUTHENTICATION_FAILED"
    assert unknown.json()["detail"] == wrong.json()["detail"]


@pytest.mark.slow
async def test_unknown_user_timing_is_comparable(
    client: httpx.AsyncClient, active_admin: str
) -> None:
    """FR-A-11: without the dummy-hash burn, an unknown username returns in ~1 ms
    against ~100 ms for a known one, which enumerates the user list."""

    async def timed(username: str) -> float:
        start = time.perf_counter()
        await client.post(
            "/v1/auth/login", json={"username": username, "password": "wrong-password"}
        )
        return time.perf_counter() - start

    known = min([await timed("admin") for _ in range(3)])
    unknown = min([await timed("nobody-here") for _ in range(3)])

    ratio = max(known, unknown) / max(min(known, unknown), 1e-6)
    assert ratio < 3.0, f"timing differs by {ratio:.1f}x — enumeration is possible"


async def test_repeated_failures_lock_the_account(
    client: httpx.AsyncClient, active_admin: str
) -> None:
    """TC-M01-15 / FR-A-11."""
    for _ in range(5):
        await client.post("/v1/auth/login", json={"username": "admin", "password": "nope"})

    async with session_scope() as session:
        user = await session.scalar(select(User).where(User.username == "admin"))
    assert user is not None
    assert user.failed_login_count >= 5
    assert user.locked_until is not None

    # Even the correct password is refused while locked — and the response does
    # not reveal that lockout is the reason.
    response = await client.post(
        "/v1/auth/login", json={"username": "admin", "password": active_admin}
    )
    assert response.status_code == 401
    assert "lock" not in response.json()["detail"].lower()


async def test_successful_login_clears_the_failure_counter(
    client: httpx.AsyncClient, active_admin: str
) -> None:
    for _ in range(3):
        await client.post("/v1/auth/login", json={"username": "admin", "password": "nope"})

    ok = await client.post("/v1/auth/login", json={"username": "admin", "password": active_admin})
    assert ok.json()["status"] == "ok"

    async with session_scope() as session:
        user = await session.scalar(select(User).where(User.username == "admin"))
    assert user is not None
    assert user.failed_login_count == 0
    assert user.locked_until is None


# --- sessions ----------------------------------------------------------------


async def test_session_cookie_flags(client: httpx.AsyncClient, active_admin: str) -> None:
    """HttpOnly stops JavaScript reading it; SameSite=Lax stops cross-site sends."""
    response = await client.post(
        "/v1/auth/login", json={"username": "admin", "password": active_admin}
    )
    header = response.headers.get("set-cookie", "")
    assert "cairn_session=" in header
    assert "HttpOnly" in header
    assert "SameSite=lax" in header.replace("Lax", "lax")


async def test_logout_revokes_the_session(client: httpx.AsyncClient, active_admin: str) -> None:
    await client.post("/v1/auth/login", json={"username": "admin", "password": active_admin})
    assert (await client.get("/v1/me")).status_code == 200

    logout = await client.post(
        "/v1/auth/logout", headers={"X-CSRF-Token": client.cookies["cairn_csrf"]}
    )
    assert logout.status_code == 204

    assert (await client.get("/v1/me")).status_code == 401


async def test_changing_the_password_invalidates_existing_sessions(
    client: httpx.AsyncClient, active_admin: str
) -> None:
    """FR-A-13."""
    login = await client.post(
        "/v1/auth/login", json={"username": "admin", "password": active_admin}
    )
    session_cookie = login.cookies["cairn_session"]

    change = await client.post(
        "/v1/auth/change-password",
        json={"current_password": active_admin, "new_password": "Yet-Another-Strong-11"},
        headers={"X-CSRF-Token": login.cookies["cairn_csrf"]},
    )
    assert change.status_code == 204

    async with httpx.AsyncClient(
        transport=client._transport,
        base_url="http://testserver",
        cookies={"cairn_session": session_cookie},
    ) as stale:
        assert (await stale.get("/v1/me")).status_code == 401


async def test_unauthenticated_access_is_refused(client: httpx.AsyncClient) -> None:
    """FR-A-01."""
    assert (await client.get("/v1/me")).status_code == 401


# --- health and meta ---------------------------------------------------------


async def test_healthz_is_dependency_free(client: httpx.AsyncClient) -> None:
    """TC-M00-13: liveness must not probe the database, or a slow query becomes
    a restart loop."""
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readyz_reports_each_dependency(client: httpx.AsyncClient) -> None:
    response = await client.get("/readyz")
    assert response.status_code in (200, 503)
    checks = response.json()["checks"]
    assert set(checks) == {"database", "cache"}


async def test_meta_is_public_and_declares_capabilities(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/meta")
    assert response.status_code == 200
    body = response.json()
    assert body["product"] == "Cairn"
    assert body["api_version"] == "v1"
    # Phase 0 honestly reports what it does not yet have, so a client can adapt
    # rather than discovering a missing feature via a 404.
    assert body["capabilities"]["mcp"] is False


async def test_request_id_is_echoed(client: httpx.AsyncClient) -> None:
    """FR-I-08."""
    response = await client.get("/healthz", headers={"X-Request-Id": "req_client_supplied"})
    assert response.headers["X-Request-Id"] == "req_client_supplied"

    generated = await client.get("/healthz")
    assert generated.headers["X-Request-Id"].startswith("req_")


async def test_errors_use_problem_json(client: httpx.AsyncClient) -> None:
    """FR-I-04."""
    response = await client.post("/v1/auth/login", json={"username": "x"})
    assert response.status_code == 422 or response.status_code == 400
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["code"] == "VALIDATION_FAILED"
    assert "request_id" in body


async def test_unknown_fields_are_rejected_on_write(client: httpx.AsyncClient) -> None:
    """NFR-SEC-05: extra='forbid'."""
    response = await client.post(
        "/v1/auth/login",
        json={"username": "admin", "password": "x", "role": "admin"},
    )
    assert response.status_code in (400, 422)
