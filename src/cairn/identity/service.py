"""Identity service.

Transaction discipline note: a failed login must still *commit* its failure
bookkeeping (attempt counter, lockout). Raising inside ``transaction()`` would
roll that back, so failures are evaluated inside the transaction, recorded, and
raised only after it commits.
"""

from __future__ import annotations

import os
from datetime import timedelta
from uuid import UUID

from cairn.authz.model import Principal
from cairn.core.config import Settings, get_settings
from cairn.core.db import transaction
from cairn.core.errors import AuthenticationFailed, NotFound
from cairn.core.logging import get_logger
from cairn.core.telemetry import auth_login_total, bootstrap_total
from cairn.core.time import utcnow
from cairn.identity.dto import (
    BootstrapResult,
    CreateUserSpec,
    LoginResult,
    SessionView,
    UpdateUserSpec,
    UserView,
)
from cairn.identity.errors import (
    INVALID_CREDENTIALS_MESSAGE,
    LastAdminProtected,
    PasswordReused,
    SelfDeletionRefused,
    SetupTokenInvalid,
    UsernameTaken,
)
from cairn.identity.hashing import Hasher, hash_token, new_token
from cairn.identity.models import Session, User
from cairn.identity.policy import PasswordPolicy
from cairn.identity.repository import IdentityRepository
from cairn.platform.audit import AuditService, get_audit_service

__all__ = ["BOOTSTRAP_LOCK_KEY", "SCOPE_BOOTSTRAP", "IdentityService", "get_identity_service"]

log = get_logger(__name__)

#: Arbitrary but fixed. Any two processes taking this lock serialise, which is
#: what makes concurrent bootstrap safe (FR-A-02).
BOOTSTRAP_LOCK_KEY = 0x0CA1_2001

#: The only scope a credential-change token carries.
SCOPE_BOOTSTRAP = "credential:bootstrap"

_INITIAL_PASSWORD_ENV = "CAIRN_INITIAL_ADMIN_PASSWORD"  # noqa: S105 — a variable name
_BOOTSTRAP_PASSWORD_BYTES = 18  # ~144 bits, comfortably above the 128 required


class IdentityService:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        repository: IdentityRepository | None = None,
        audit: AuditService | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._repo = repository or IdentityRepository()
        self._audit = audit or get_audit_service()
        self._hasher = Hasher(self._settings.auth)
        self._policy = PasswordPolicy(self._settings.auth)

    @property
    def policy(self) -> PasswordPolicy:
        return self._policy

    # ------------------------------------------------------------------ bootstrap

    async def bootstrap(self, *, schema_version: str = "0001") -> BootstrapResult | None:
        """Create the initial administrator. Idempotent and race-safe (FR-A-02/03).

        Returns ``None`` if an administrator already exists.
        """
        env_password = os.environ.get(_INITIAL_PASSWORD_ENV) or (
            self._settings.initial_admin_password.get_secret_value()
            if self._settings.initial_admin_password
            else None
        )

        async with transaction() as session:
            # With `docker compose up --scale api-control=3`, all three replicas
            # reach this line at once. Without the lock: three admins, or a
            # unique-violation crash loop on startup.
            from cairn.core.db import advisory_xact_lock

            await advisory_xact_lock(session, BOOTSTRAP_LOCK_KEY)

            if await self._repo.admin_exists(session):
                bootstrap_total.labels(outcome="already_initialized").inc()
                log.info("bootstrap.skipped", reason="administrator already exists")
                return None

            workspace = await self._repo.ensure_default_workspace(session)
            password = env_password or new_token(_BOOTSTRAP_PASSWORD_BYTES)

            user = await self._repo.add(
                session,
                User(
                    workspace_id=workspace.id,
                    username="admin",
                    role="admin",
                    display_name="Administrator",
                    password_hash=self._hasher.hash(password),
                    must_change_password=True,  # FR-A-06 — durable from birth
                ),
            )

            if not await self._repo.is_initialized(session):
                await self._repo.mark_initialized(session, schema_version=schema_version)

            await self._audit.record(
                session,
                workspace_id=workspace.id,
                action="system.bootstrap",
                actor_type="system",
                actor_label="system",
                resource_type="user",
                resource_id=user.id,
                detail={
                    "username": "admin",
                    "password_source": "env" if env_password else "generated",
                },
            )

            result = BootstrapResult(
                user_id=user.id,
                workspace_id=workspace.id,
                username="admin",
                password=None if env_password else password,
                password_printed=env_password is None,
            )

        bootstrap_total.labels(outcome="created").inc()
        log.info("bootstrap.created", username="admin", password_printed=result.password_printed)
        return result

    # ---------------------------------------------------------------------- login

    async def login(
        self, username: str, password: str, *, ip: str | None = None, user_agent: str | None = None
    ) -> LoginResult:
        """Authenticate. Returns a change token — and NO session — when the
        principal must change its credentials first (FR-A-04)."""
        result: LoginResult | None = None
        outcome = "failed"

        async with transaction() as session:
            user = await self._repo.get_by_username(session, username, for_update=True)

            if user is None:
                # Equalise timing so an unknown username is indistinguishable
                # from a wrong password (FR-A-11).
                self._hasher.consume_dummy()
            else:
                now = utcnow()
                locked = user.locked_until is not None and user.locked_until > now
                password_ok = self._hasher.verify(user.password_hash, password)

                if password_ok and not locked and user.is_active:
                    user.failed_login_count = 0
                    user.locked_until = None
                    user.last_login_at = now
                    result = await self._issue_login_result(
                        session, user, ip=ip, user_agent=user_agent
                    )
                    outcome = result.status
                else:
                    if not password_ok:
                        self._apply_failure(user)
                    outcome = "locked" if locked else "failed"
                    await self._audit.record(
                        session,
                        workspace_id=user.workspace_id,
                        action="auth.login.failed",
                        outcome="failure",
                        actor_id=user.id,
                        actor_label=user.username,
                        ip=ip,
                        user_agent=user_agent,
                        detail={"reason": "locked" if locked else "bad_credentials"},
                    )

        auth_login_total.labels(outcome=outcome).inc()

        if result is None:
            log.info("auth.login.failed", username=username, outcome=outcome, ip=ip)
            raise AuthenticationFailed(INVALID_CREDENTIALS_MESSAGE)

        log.info("auth.login", outcome=outcome, user_id=str(result.user.id), ip=ip)
        return result

    def _apply_failure(self, user: User) -> None:
        """Exponential lockout (FR-A-11). Never revealed to the caller."""
        user.failed_login_count += 1
        thresholds = self._settings.auth.lockout_thresholds
        applicable = [
            seconds for count, seconds in thresholds.items() if user.failed_login_count >= count
        ]
        if applicable:
            user.locked_until = utcnow() + timedelta(seconds=max(applicable))

    async def _issue_login_result(
        self, session: object, user: User, *, ip: str | None, user_agent: str | None
    ) -> LoginResult:
        from sqlalchemy.ext.asyncio import AsyncSession

        assert isinstance(session, AsyncSession)

        if user.must_change_password:
            token = await self._create_token(
                session,
                user,
                scopes=[SCOPE_BOOTSTRAP],
                ttl=timedelta(minutes=self._settings.auth.change_token_ttl_minutes),
                ip=ip,
                user_agent=user_agent,
            )
            return LoginResult(
                status="password_change_required",
                user=_to_view(user),
                change_token=token,
                reason="initial_admin_setup" if user.role == "admin" else "admin_forced",
                expires_in=self._settings.auth.change_token_ttl_minutes * 60,
                policy=self._policy.to_view(),
            )

        token, csrf = await self._create_session(session, user, ip=ip, user_agent=user_agent)
        return LoginResult(status="ok", user=_to_view(user), session_token=token, csrf_token=csrf)

    # --------------------------------------------------------- credential change

    async def complete_initial_setup(
        self,
        change_token: str,
        *,
        current_password: str,
        new_password: str,
        new_username: str | None = None,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> LoginResult:
        """Complete the forced credential change (FR-A-06/07/09/13).

        Username and password change atomically: if the username turns out to be
        taken, the whole transaction rolls back and the password is unchanged.
        """
        user_id: UUID

        async with transaction() as session:
            record = await self._repo.get_session_by_token_hash(
                session, hash_token(change_token), for_update=True
            )
            if record is None or not self._token_usable(record, SCOPE_BOOTSTRAP):
                raise SetupTokenInvalid("The setup token is invalid or has expired.")

            user = await self._repo.get(session, record.user_id, for_update=True)
            if user is None or record.credential_version != user.credential_version:
                raise SetupTokenInvalid("The setup token is invalid or has expired.")

            if not self._hasher.verify(user.password_hash, current_password):
                raise AuthenticationFailed("The current password is incorrect.")

            candidate_username = (new_username or user.username).strip()
            self._policy.validate(new_password, username=candidate_username)

            if self._hasher.verify(user.password_hash, new_password):
                raise PasswordReused("The new password must differ from the current password.")

            before = user.snapshot()

            if new_username and candidate_username.lower() != user.username.lower():
                if await self._repo.username_taken(
                    session, user.workspace_id, candidate_username, exclude=user.id
                ):
                    # Rolls back the entire unit of work — FR-A-07.
                    raise UsernameTaken("That username is already in use.")
                user.username = candidate_username

            user.password_hash = self._hasher.hash(new_password)
            user.must_change_password = False  # cleared ONLY here (FR-A-06)
            user.password_changed_at = utcnow()
            user.credential_version += 1  # FR-A-13 — invalidates every old token

            await self._repo.revoke_all_sessions(session, user.id)

            await self._audit.record(
                session,
                workspace_id=user.workspace_id,
                action="user.credentials.initial_setup",
                actor_id=user.id,
                actor_label=user.username,
                resource_type="user",
                resource_id=user.id,
                ip=ip,
                user_agent=user_agent,
                before=before,
                after=user.snapshot(),
                detail={"username_changed": bool(new_username)},
            )
            user_id = user.id

        # Fresh transaction: the credential version just changed, so the new
        # session must be minted against the committed value.
        return await self._issue_session_for(user_id, ip=ip, user_agent=user_agent)

    async def change_password(
        self,
        user_id: UUID,
        *,
        current_password: str,
        new_password: str,
        ip: str | None = None,
    ) -> None:
        """Voluntary change. Also bumps ``credential_version`` (FR-A-13)."""
        async with transaction() as session:
            user = await self._repo.get(session, user_id, for_update=True)
            if user is None:
                raise NotFound("User not found.")
            if not self._hasher.verify(user.password_hash, current_password):
                raise AuthenticationFailed("The current password is incorrect.")
            self._policy.validate(new_password, username=user.username)
            if self._hasher.verify(user.password_hash, new_password):
                raise PasswordReused("The new password must differ from the current password.")

            before = user.snapshot()
            user.password_hash = self._hasher.hash(new_password)
            user.password_changed_at = utcnow()
            user.credential_version += 1
            user.must_change_password = False
            await self._repo.revoke_all_sessions(session, user.id)
            await self._audit.record(
                session,
                workspace_id=user.workspace_id,
                action="user.credentials.change",
                actor_id=user.id,
                actor_label=user.username,
                resource_type="user",
                resource_id=user.id,
                ip=ip,
                before=before,
                after=user.snapshot(),
            )

    async def force_password_change(self, actor: Principal, user_id: UUID) -> None:
        """Admin-forced reset (FR-A-12). Reuses the identical flow."""
        async with transaction() as session:
            user = await self._repo.get(session, user_id, for_update=True)
            if user is None:
                raise NotFound("User not found.")
            before = user.snapshot()
            user.must_change_password = True
            user.credential_version += 1
            await self._repo.revoke_all_sessions(session, user.id)
            await self._audit.record(
                session,
                workspace_id=user.workspace_id,
                action="user.credentials.force_change",
                actor_id=actor.id,
                actor_label=actor.username,
                resource_type="user",
                resource_id=user.id,
                before=before,
                after=user.snapshot(),
            )

    # ---------------------------------------------------------------- user CRUD

    async def create_user(
        self, actor: Principal, spec: CreateUserSpec
    ) -> tuple[UserView, str | None]:
        """Create an account (FR-B-02).

        The account starts with ``must_change_password = True``, so the
        identical forced flow applies. An administrator handing out an initial
        password therefore never creates an account whose long-term credentials
        both of them know.
        """
        password = spec.password or self._policy.generate(username=spec.username)
        async with transaction() as session:
            if await self._repo.username_taken(session, actor.workspace_id, spec.username):
                raise UsernameTaken("That username is already in use.")
            self._policy.validate(password, username=spec.username)

            user = await self._repo.add(
                session,
                User(
                    workspace_id=actor.workspace_id,
                    username=spec.username,
                    email=spec.email,
                    display_name=spec.display_name,
                    role=spec.role,
                    password_hash=self._hasher.hash(password),
                    must_change_password=True,
                ),
            )
            await self._audit.record(
                session,
                workspace_id=actor.workspace_id,
                action="user.create",
                actor_id=actor.id,
                actor_label=actor.username,
                resource_type="user",
                resource_id=user.id,
                after=user.snapshot(),
            )
            view = _to_view(user)
        return view, (None if spec.password else password)

    async def list_users(
        self, workspace_id: UUID, *, limit: int = 50, cursor_id: UUID | None = None
    ) -> list[UserView]:
        from cairn.core.db import session_scope

        async with session_scope() as session:
            users = await self._repo.list_users(
                session, workspace_id, limit=limit, cursor_id=cursor_id
            )
        return [_to_view(u) for u in users]

    async def update_user(self, actor: Principal, user_id: UUID, spec: UpdateUserSpec) -> UserView:
        async with transaction() as session:
            user = await self._repo.get(session, user_id, for_update=True)
            if user is None or user.workspace_id != actor.workspace_id:
                raise NotFound("User not found.")

            before = user.snapshot()

            demoting = spec.role is not None and spec.role != user.role and user.role == "admin"
            deactivating = spec.is_active is False and user.role == "admin"
            if demoting or deactivating:
                await self._assert_not_last_admin_in(session, user)

            if spec.email is not None:
                user.email = spec.email
            if spec.display_name is not None:
                user.display_name = spec.display_name
            if spec.role is not None:
                user.role = spec.role
                # A role change alters platform capability, so every cached
                # principal for this user must become unreachable.
                user.perm_version += 1
            if spec.is_active is not None:
                user.is_active = spec.is_active
                if not spec.is_active:
                    await self._repo.revoke_all_sessions(session, user.id)

            await self._audit.record(
                session,
                workspace_id=actor.workspace_id,
                action="user.update",
                actor_id=actor.id,
                actor_label=actor.username,
                resource_type="user",
                resource_id=user.id,
                before=before,
                after=user.snapshot(),
            )
            return _to_view(user)

    async def delete_user(self, actor: Principal, user_id: UUID) -> None:
        if user_id == actor.id:
            raise SelfDeletionRefused("You cannot delete your own account.")
        async with transaction() as session:
            user = await self._repo.get(session, user_id, for_update=True)
            if user is None or user.workspace_id != actor.workspace_id:
                raise NotFound("User not found.")
            if user.role == "admin":
                await self._assert_not_last_admin_in(session, user)

            before = user.snapshot()
            user.deleted_at = utcnow()
            user.is_active = False
            user.credential_version += 1
            await self._repo.revoke_all_sessions(session, user.id)
            await self._audit.record(
                session,
                workspace_id=actor.workspace_id,
                action="user.delete",
                actor_id=actor.id,
                actor_label=actor.username,
                resource_type="user",
                resource_id=user.id,
                before=before,
            )

        # Not optional cleanup: a key that outlives its owner is a credential
        # nobody is accountable for.
        from cairn.authz.service import get_authz_service

        await get_authz_service().revoke_all_for_user(user_id)

    async def _assert_not_last_admin_in(self, session: object, user: User) -> None:
        from sqlalchemy.ext.asyncio import AsyncSession

        assert isinstance(session, AsyncSession)
        if await self._repo.count_active_admins(session, user.workspace_id) <= 1:
            raise LastAdminProtected(
                "This is the only active administrator; promote another account first."
            )

    # -------------------------------------------------------------------- sessions

    async def resolve_session(self, token: str) -> tuple[Principal, SessionView] | None:
        """Validate a session or change token and build its principal.

        Returns ``None`` for anything unusable — expired, revoked, or minted
        against a superseded ``credential_version``.
        """
        from cairn.core.db import session_scope

        async with session_scope() as session:
            record = await self._repo.get_session_by_token_hash(session, hash_token(token))
            if record is None:
                return None
            user = await self._repo.get(session, record.user_id)
            if user is None or not user.is_active:
                return None
            if not self._token_usable(record, None):
                return None
            if record.credential_version != user.credential_version:
                # FR-A-13: credentials changed since this token was issued.
                return None

            view = SessionView(
                id=record.id,
                user_id=record.user_id,
                workspace_id=record.workspace_id,
                scopes=frozenset(record.scopes),
                expires_at=record.expires_at,
                credential_version=record.credential_version,
                csrf_token_hash=record.csrf_token_hash,
            )
            user_id = user.id
            session_id = record.id
            scopes = frozenset(record.scopes)
        from cairn.authz.service import AuthzService

        principal = await AuthzService().principal_for_user(
            user_id, session_id=session_id, scopes=scopes
        )
        return (principal, view) if principal is not None else None

    async def logout(self, token: str) -> None:
        async with transaction() as session:
            record = await self._repo.get_session_by_token_hash(session, hash_token(token))
            if record is not None:
                await self._repo.revoke_session(session, record.id)

    async def get_user(self, user_id: UUID) -> UserView:
        from cairn.core.db import session_scope

        async with session_scope() as session:
            user = await self._repo.get(session, user_id)
            if user is None:
                raise NotFound("User not found.")
            return _to_view(user)

    async def assert_not_last_admin(self, workspace_id: UUID, user_id: UUID) -> None:
        from cairn.core.db import session_scope

        async with session_scope() as session:
            user = await self._repo.get(session, user_id)
            if user is None or user.role != "admin":
                return
            if await self._repo.count_active_admins(session, workspace_id) <= 1:
                raise LastAdminProtected(
                    "This is the only active administrator; promote another account first."
                )

    # --------------------------------------------------------------------- helpers

    def _token_usable(self, record: Session, required_scope: str | None) -> bool:
        if record.revoked_at is not None:
            return False
        if record.expires_at <= utcnow():
            return False
        return required_scope is None or required_scope in record.scopes

    async def _create_token(
        self,
        session: object,
        user: User,
        *,
        scopes: list[str],
        ttl: timedelta,
        ip: str | None,
        user_agent: str | None,
        csrf_hash: str | None = None,
    ) -> str:
        from sqlalchemy.ext.asyncio import AsyncSession

        assert isinstance(session, AsyncSession)
        token = new_token()
        await self._repo.add_session(
            session,
            Session(
                workspace_id=user.workspace_id,
                user_id=user.id,
                token_hash=hash_token(token),
                credential_version=user.credential_version,
                scopes=scopes,
                csrf_token_hash=csrf_hash,
                ip=ip,
                user_agent=user_agent,
                expires_at=utcnow() + ttl,
            ),
        )
        return token

    async def _create_session(
        self, session: object, user: User, *, ip: str | None, user_agent: str | None
    ) -> tuple[str, str]:
        csrf = new_token(16)
        token = await self._create_token(
            session,
            user,
            scopes=[],
            ttl=timedelta(minutes=self._settings.auth.session_ttl_minutes),
            ip=ip,
            user_agent=user_agent,
            csrf_hash=hash_token(csrf),
        )
        return token, csrf

    async def _issue_session_for(
        self, user_id: UUID, *, ip: str | None, user_agent: str | None
    ) -> LoginResult:
        async with transaction() as session:
            user = await self._repo.get(session, user_id, for_update=True)
            if user is None:  # pragma: no cover — the caller just updated this row
                raise NotFound("User not found.")
            token, csrf = await self._create_session(session, user, ip=ip, user_agent=user_agent)
            return LoginResult(
                status="ok", user=_to_view(user), session_token=token, csrf_token=csrf
            )


def _to_view(user: User) -> UserView:
    return UserView(
        id=user.id,
        workspace_id=user.workspace_id,
        username=user.username,
        email=user.email,
        display_name=user.display_name,
        role="admin" if user.role == "admin" else "user",
        must_change_password=user.must_change_password,
        is_active=user.is_active,
        credential_version=user.credential_version,
        last_login_at=user.last_login_at,
        created_at=user.created_at,
    )


_service: IdentityService | None = None


def get_identity_service() -> IdentityService:
    global _service
    if _service is None:
        _service = IdentityService()
    return _service


def reset_identity_service() -> None:
    """Test hook — drops the cached instance so new settings take effect."""
    global _service
    _service = None
