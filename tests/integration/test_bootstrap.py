"""Bootstrap — TC-M01-01 … TC-M01-05, TC-M01-18.

FR-A-02: exactly one administrator, race-safe.
FR-A-03: a strong generated password, printed once, never persisted in clear.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator

import pytest
from sqlalchemy import func, select, text

from cairn.core.db import session_scope
from cairn.identity.bootstrap import print_bootstrap_banner
from cairn.identity.models import SystemBootstrap, User
from cairn.identity.service import IdentityService


@pytest.fixture
def clear_admin_password_env() -> Iterator[None]:
    saved = os.environ.pop("CAIRN_INITIAL_ADMIN_PASSWORD", None)
    yield
    if saved is not None:
        os.environ["CAIRN_INITIAL_ADMIN_PASSWORD"] = saved


async def _count_admins() -> int:
    async with session_scope() as session:
        return int(
            await session.scalar(select(func.count(User.id)).where(User.role == "admin")) or 0
        )


async def test_bootstrap_creates_one_admin_requiring_a_credential_change(
    identity: IdentityService, clear_admin_password_env: None
) -> None:
    """TC-M01-01 / FR-A-02 / FR-A-06."""
    result = await identity.bootstrap()

    assert result is not None
    assert result.username == "admin"
    assert result.password_printed is True
    assert result.password is not None

    async with session_scope() as session:
        user = await session.scalar(select(User).where(User.username == "admin"))

    assert user is not None
    assert user.role == "admin"
    assert user.must_change_password is True  # durable, survives restart
    assert user.credential_version == 1
    assert user.is_active is True


async def test_bootstrap_is_idempotent(
    identity: IdentityService, clear_admin_password_env: None
) -> None:
    """TC-M01-03: a second run must be a no-op, not a second admin."""
    first = await identity.bootstrap()
    second = await identity.bootstrap()

    assert first is not None
    assert second is None
    assert await _count_admins() == 1


async def test_concurrent_bootstrap_creates_exactly_one_admin(
    identity: IdentityService, clear_admin_password_env: None
) -> None:
    """TC-M01-02 / FR-A-02 — the test this whole design exists for.

    With ``docker compose up --scale api-control=3`` every replica reaches
    bootstrap simultaneously. Without the advisory lock the outcomes are three
    administrators, or a unique-violation crash loop on first boot. Both are
    terrible first impressions and neither is reproducible on a developer laptop.
    """
    from cairn.identity.service import IdentityService as Service

    services = [Service() for _ in range(5)]
    results = await asyncio.gather(
        *(service.bootstrap() for service in services), return_exceptions=True
    )

    for outcome in results:
        assert not isinstance(outcome, Exception), f"bootstrap raised: {outcome!r}"

    created = [r for r in results if r is not None]
    assert len(created) == 1, f"expected exactly one creation, got {len(created)}"
    assert await _count_admins() == 1

    async with session_scope() as session:
        rows = int(await session.scalar(select(func.count(SystemBootstrap.id))) or 0)
    assert rows == 1


async def test_generated_password_has_sufficient_entropy(
    identity: IdentityService, clear_admin_password_env: None
) -> None:
    """TC-M01-05 / FR-A-03: at least 128 bits."""
    result = await identity.bootstrap()
    assert result is not None and result.password is not None
    # token_urlsafe(18) -> 24 base64url characters ~= 144 bits.
    assert len(result.password) >= 22
    assert len(set(result.password)) >= 10


async def test_generated_password_is_never_stored_in_clear(
    identity: IdentityService, clear_admin_password_env: None
) -> None:
    """TC-M01-05: the hash is stored; the plaintext must appear nowhere."""
    result = await identity.bootstrap()
    assert result is not None and result.password is not None
    password = result.password

    async with session_scope() as session:
        user = await session.scalar(select(User).where(User.username == "admin"))
        assert user is not None
        assert user.password_hash.startswith("$argon2id$")
        assert password not in user.password_hash

        # Nor anywhere in the audit trail.
        audit_dump = await session.execute(text("SELECT detail::text FROM audit_log"))
        for (detail,) in audit_dump:
            assert password not in detail


async def test_env_supplied_password_is_used_and_not_printed(
    identity: IdentityService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TC-M01-04 / FR-A-03: automated deployment path.

    The password is not printed, but the forced change still applies — an
    unattended deployment must not silently opt out of it.
    """
    monkeypatch.setenv("CAIRN_INITIAL_ADMIN_PASSWORD", "Bootstrap-From-Env-9xK!")

    result = await identity.bootstrap()

    assert result is not None
    assert result.password_printed is False
    assert result.password is None

    async with session_scope() as session:
        user = await session.scalar(select(User).where(User.username == "admin"))
    assert user is not None
    assert user.must_change_password is True

    login = await identity.login("admin", "Bootstrap-From-Env-9xK!")
    assert login.status == "password_change_required"


async def test_banner_prints_only_when_a_password_was_generated(
    identity: IdentityService,
    clear_admin_password_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The banner goes to stdout via print(), deliberately not through the
    structured logger — log lines are shipped to aggregation and retained."""
    result = await identity.bootstrap()
    assert result is not None

    print_bootstrap_banner(result)
    captured = capsys.readouterr().out

    assert "Cairn — initial administrator account created" in captured
    assert "username:  admin" in captured
    assert result.password is not None and result.password in captured
    assert "displayed ONCE" in captured


async def test_bootstrap_records_an_audit_entry(
    identity: IdentityService, clear_admin_password_env: None
) -> None:
    """FR-O-01: the very first state change is auditable."""
    await identity.bootstrap()

    async with session_scope() as session:
        rows = (
            await session.execute(
                text("SELECT action, actor_type, outcome FROM audit_log ORDER BY at")
            )
        ).all()

    assert ("system.bootstrap", "system", "success") in [tuple(r) for r in rows]
