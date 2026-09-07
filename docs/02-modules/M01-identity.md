# M01 — Identity

| | |
| --- | --- |
| **Package** | `cairn.identity` |
| **Layer** | L3 control plane |
| **Phase** | 0 (bootstrap + forced change) · 1 (user management) |
| **Owner** | Backend eng. A |
| **Depends on** | M00 core; M15 platform (audit, via facade) |
| **Depended on by** | M02 authz (facade), apps/api |
| **Tables owned** | `system_bootstrap`, `user`, `session` |
| **Requirements owned** | FR-A-01 … FR-A-14, FR-B-02, FR-P-02 (backend half) |

---

## 1. Purpose and scope

Who someone is, and whether they may hold a session at all.

**In scope:** first-boot bootstrap, password hashing and policy, login/logout/refresh, session
lifecycle, the forced credential-change state machine, user CRUD, account lockout.

**Out of scope:** what a principal may *do* (M02), API keys (M02), audit writing (M15 — this
module *emits* audit events through the facade).

---

## 2. Domain model

```python
@dataclass(frozen=True)
class UserView:               # DTO crossing the boundary
    id: UUID
    workspace_id: UUID
    username: str
    email: str | None
    display_name: str | None
    role: Literal["admin", "user"]
    must_change_password: bool
    is_active: bool
    credential_version: int
    perm_version: int
    last_login_at: datetime | None
    created_at: datetime

@dataclass(frozen=True)
class SessionView:
    id: UUID
    user_id: UUID
    scopes: frozenset[str]      # empty = full session; {"credential:bootstrap"} = restricted
    expires_at: datetime

@dataclass(frozen=True)
class LoginResult:
    status: Literal["ok", "password_change_required"]
    session_token: str | None      # set only when status == "ok"
    change_token: str | None       # set only when status == "password_change_required"
    user: UserView
    policy: PasswordPolicy | None
```

### Invariants

| # | Invariant | Enforced where |
| --- | --- | --- |
| I1 | Exactly one bootstrap admin is ever created | Advisory lock + `system_bootstrap` singleton |
| I2 | `username` unique within a workspace | DB unique constraint |
| I3 | `must_change_password=true` ⇒ no full session may exist for that user | Login logic + middleware |
| I4 | `must_change_password` clears **only** via a successful credential update | Single service method |
| I5 | Any credential change increments `credential_version` | Same transaction |
| I6 | Sessions with a stale `credential_version` are invalid | Validated on every lookup |
| I7 | `password_hash` is Argon2id, never plaintext, never logged | Hasher + redaction |
| I8 | The last active admin cannot be deleted or demoted | Service guard |

---

## 3. Public interface

```python
class IdentityService:
    # --- bootstrap ---
    async def bootstrap(self) -> BootstrapResult | None:
        """FR-A-02/03. Idempotent and race-safe. Returns None if already initialized."""

    # --- authentication ---
    async def login(self, username: str, password: str, *, ip, user_agent) -> LoginResult:
        """FR-A-04/11. Constant-time on unknown user. Applies lockout."""
    async def logout(self, session_token: str) -> None: ...
    async def resolve_session(self, token: str) -> SessionView | None:
        """Validates expiry, revocation, and credential_version."""

    # --- credential change ---
    async def complete_initial_setup(
        self, change_token: str, current_password: str,
        new_password: str, new_username: str | None,
    ) -> LoginResult:
        """FR-A-04..07/09/13. ONE transaction. Issues a real session only on success."""
    async def change_password(self, user_id: UUID, current: str, new: str) -> None: ...

    # --- user management (FR-B-02) ---
    async def create_user(self, actor: Principal, spec: CreateUserSpec) -> tuple[UserView, str]:
        """Returns the user and a generated initial password. must_change_password=True."""
    async def get_user(self, user_id: UUID) -> UserView: ...
    async def list_users(self, workspace_id: UUID, filters, page) -> CursorPage[UserView]: ...
    async def update_user(self, actor: Principal, user_id: UUID, spec: UpdateUserSpec) -> UserView: ...
    async def delete_user(self, actor: Principal, user_id: UUID) -> None: ...
    async def set_active(self, actor: Principal, user_id: UUID, active: bool) -> UserView: ...
    async def force_password_change(self, actor: Principal, user_id: UUID) -> None:
        """FR-A-12. Sets must_change_password and revokes all sessions."""
```

---

## 4. Behaviour — the critical flows

### 4.1 Bootstrap (`FR-A-02`, `FR-A-03`)

```python
async def bootstrap(self) -> BootstrapResult | None:
    async with transaction() as session:
        # Race-safe: with `docker compose up --scale api-control=3`, all three replicas
        # run this at once. Without the lock: three admins, or a unique-violation crash loop.
        await advisory_lock(session, BOOTSTRAP_LOCK_KEY)

        if await self.repo.admin_exists(session):
            return None

        workspace = await self.repo.ensure_default_workspace(session)

        env_password = os.environ.get("CAIRN_INITIAL_ADMIN_PASSWORD")
        password = env_password or secrets.token_urlsafe(18)     # ~144 bits

        user = await self.repo.create(session, User(
            workspace_id=workspace.id, username="admin", role="admin",
            password_hash=self.hasher.hash(password),
            must_change_password=True,                # ← durable. FR-A-06
        ))
        await self.repo.mark_initialized(session)
        await self.audit.record(session, action="system.bootstrap",
                                actor_type="system", outcome="success")

    if env_password is None:
        _print_banner(username="admin", password=password)   # once, stdout, never logged
    return BootstrapResult(user_id=user.id, password_printed=env_password is None)
```

Banner format (must be visually unmissable in a wall of Compose output):

```
╔══════════════════════════════════════════════════════════════════╗
║  Cairn — initial administrator account created                   ║
║                                                                  ║
║    username:  admin                                              ║
║    password:  7Kq2-mVx9RtL4pZsN3wY                               ║
║                                                                  ║
║  This password is displayed ONCE and is not recoverable.         ║
║  You will be required to change it at first login.               ║
╚══════════════════════════════════════════════════════════════════╝
```

**Runs from the `migrate` one-shot container**, after migrations, not from the API entrypoint —
so it happens exactly once regardless of replica count.

### 4.2 Login with forced change (`FR-A-04`) — the pivotal behaviour

```python
async def login(self, username, password, *, ip, user_agent) -> LoginResult:
    user = await self.repo.get_by_username(username)

    if user is None:
        self.hasher.hash(DUMMY_PASSWORD)          # constant-time: same work as a real check
        raise AuthenticationError("Invalid username or password.")
    if user.locked_until and user.locked_until > utcnow():
        raise AuthenticationError("Invalid username or password.")   # do not reveal lockout
    if not self.hasher.verify(user.password_hash, password):
        await self._record_failure(user)
        raise AuthenticationError("Invalid username or password.")
    if not user.is_active:
        raise AuthenticationError("Invalid username or password.")

    await self._record_success(user)

    if user.must_change_password:
        # NO SESSION IS ISSUED. There is nothing to abuse.
        change_token = await self._issue_token(
            user, scopes={"credential:bootstrap"}, ttl=timedelta(minutes=10))
        return LoginResult(status="password_change_required",
                           change_token=change_token, session_token=None,
                           user=user.to_view(), policy=self.policy)

    session_token = await self._issue_token(user, scopes=frozenset(), ttl=self.session_ttl)
    return LoginResult(status="ok", session_token=session_token,
                       change_token=None, user=user.to_view(), policy=None)
```

### 4.3 Middleware enforcement (`FR-A-05`) — the actual security control

```python
CREDENTIAL_SETUP_ALLOWLIST = frozenset({
    "/v1/auth/complete-initial-setup",
    "/v1/auth/logout",
    "/v1/me",
})

async def forced_change_middleware(request: Request, call_next):
    principal = request.state.principal
    if (principal
            and principal.type == "user"
            and principal.must_change_password
            and request.url.path not in CREDENTIAL_SETUP_ALLOWLIST):
        raise PasswordChangeRequiredError(
            "The credentials for this account must be changed before continuing.")
    return await call_next(request)
```

> **Why both the login gate and the middleware.** The login gate means no usable session is ever
> minted. The middleware means that even if some future code path — a refresh endpoint, an SSO
> callback, a bug — issues one anyway, it is inert for everything except the change flow.
> The UI dialog (`FR-P-02`) is a *consequence* of this state, never the enforcement.
> **`FR-A-06` is satisfied structurally:** the flag lives in Postgres, so browser close,
> session loss, container restart, and redeploy all leave it set.

### 4.4 Completing the change (`FR-A-07`) — one transaction

```python
async def complete_initial_setup(self, change_token, current_password,
                                 new_password, new_username) -> LoginResult:
    async with transaction() as session:
        sess = await self.repo.get_session_by_token(session, change_token, for_update=True)
        if sess is None or "credential:bootstrap" not in sess.scopes or sess.is_expired():
            raise AuthenticationError("Invalid or expired setup token.")

        user = await self.repo.get(session, sess.user_id, for_update=True)
        if not self.hasher.verify(user.password_hash, current_password):
            raise AuthenticationError("Current password is incorrect.")

        self.policy.validate(new_password, username=new_username or user.username)
        if self.hasher.verify(user.password_hash, new_password):
            raise ValidationError("New password must differ from the current password.")

        before = user.snapshot()
        if new_username and new_username != user.username:
            if await self.repo.username_taken(session, user.workspace_id, new_username):
                raise ConflictError("That username is already in use.")
            user.username = new_username                      # FR-A-07, same transaction

        user.password_hash        = self.hasher.hash(new_password)
        user.must_change_password = False                     # FR-A-06 — cleared only here
        user.password_changed_at  = utcnow()
        user.credential_version  += 1                         # FR-A-13 — kills old tokens

        await self.repo.revoke_all_sessions(session, user.id)
        await self.audit.record(session, action="user.credentials.initial_setup",
                                actor_id=user.id, before=before, after=user.snapshot())
        # COMMIT — only now does a real session become possible
    return await self._issue_full_session(user)
```

### 4.5 Password policy (`FR-A-09`)

| Rule | Default | Configurable |
| --- | --- | --- |
| Minimum length | 12 | yes |
| Character classes required | 3 of {lower, upper, digit, symbol} | yes |
| Not equal to previous | always | no |
| Not in the common-password list | 100k-entry bundled list | list swappable |
| Must not contain the username | always | no |
| Maximum length | 256 | no (Argon2 DoS guard) |

Policy is returned to the client so the UI renders live validation, but **the server is the
authority** — client-side checks are cosmetic.

### 4.6 Lockout (`FR-A-11`)

`failed_login_count` increments per failure. Thresholds: 5 → 1 min, 7 → 5 min, 10 → 30 min,
15 → 24 h. Reset on success. Independently, a Redis token bucket limits attempts per IP
(20/min). Lockout is never revealed in the response — same message, comparable timing.

---

## 5. API endpoints owned

| Method | Path | Auth | Requirement |
| --- | --- | --- | --- |
| POST | `/v1/auth/login` | none | FR-A-04 |
| POST | `/v1/auth/complete-initial-setup` | change token | FR-A-07 |
| POST | `/v1/auth/logout` | session | FR-A-10 |
| POST | `/v1/auth/change-password` | session | FR-A-09 |
| GET | `/v1/me` | session | FR-A-10 |
| GET | `/v1/users` | `platform:users` | FR-B-02 |
| POST | `/v1/users` | `platform:users` | FR-B-02 |
| GET | `/v1/users/{id}` | `platform:users` | FR-B-02 |
| PATCH | `/v1/users/{id}` | `platform:users` | FR-B-02 |
| DELETE | `/v1/users/{id}` | `platform:users` | FR-B-02 |
| POST | `/v1/users/{id}/force-password-change` | `platform:users` | FR-A-12 |

Full schemas in [`../03-api/03-control-plane-api.md`](../03-api/03-control-plane-api.md) §2.

---

## 6. Errors owned

| Code | HTTP | Meaning |
| --- | --- | --- |
| `AUTHENTICATION_FAILED` | 401 | Bad credentials, locked, or inactive — deliberately indistinguishable |
| `PASSWORD_CHANGE_REQUIRED` | 403 | Forced change pending |
| `PASSWORD_POLICY_VIOLATION` | 400 | With `errors[]` detailing each failed rule |
| `PASSWORD_REUSED` | 400 | New password equals current |
| `USERNAME_TAKEN` | 409 | |
| `SETUP_TOKEN_INVALID` | 401 | Missing, wrong scope, or expired |
| `LAST_ADMIN_PROTECTED` | 409 | Cannot delete or demote the last active admin |
| `SESSION_EXPIRED` | 401 | |

---

## 7. Security notes

| Concern | Control |
| --- | --- |
| Password storage | Argon2id, m=65536 KiB, t=3, p=4 (`FR-A-08`) |
| Timing attacks | Dummy hash on unknown user; `secrets.compare_digest` for tokens |
| Session tokens | 32 random bytes, base64url; **only the SHA-256 is stored** |
| Cookies | `HttpOnly; Secure; SameSite=Lax; Path=/` |
| CSRF | Double-submit token + `Origin` check on state-changing requests (`NFR-SEC-04`) |
| Enumeration | Identical error and comparable timing for unknown user vs. bad password |
| Change-token blast radius | 10-minute TTL, single scope, single-use, revoked on completion |
| Logging | Never log passwords, hashes, or tokens (`NFR-SEC-03`) |

---

## 8. Test requirements

| ID | Test | Req |
| --- | --- | --- |
| TC-M01-01 | Bootstrap creates exactly one admin with `must_change_password=true` | FR-A-02 |
| TC-M01-02 | **Bootstrap under 5 concurrent processes creates exactly one admin** | FR-A-02 |
| TC-M01-03 | Bootstrap is a no-op when an admin exists | FR-A-02 |
| TC-M01-04 | `CAIRN_INITIAL_ADMIN_PASSWORD` is honoured and nothing is printed | FR-A-03 |
| TC-M01-05 | Generated password has ≥ 128 bits entropy and is never persisted in clear | FR-A-03 |
| TC-M01-06 | Login by a flagged user returns `password_change_required` and **no session cookie** | FR-A-04 |
| TC-M01-07 | The change token cannot access any non-allowlisted route | FR-A-05 |
| TC-M01-08 | **Restart between login and change → next login still requires the change** | FR-A-06 |
| TC-M01-09 | **A leaked full session for a flagged user is rejected by middleware** | FR-A-05 |
| TC-M01-10 | Completing setup with a new username updates both atomically | FR-A-07 |
| TC-M01-11 | Setup rolls back entirely if the username is taken (password unchanged) | FR-A-07 |
| TC-M01-12 | Each policy rule is enforced with a specific `errors[]` entry | FR-A-09 |
| TC-M01-13 | New password equal to old is rejected | FR-A-09 |
| TC-M01-14 | `credential_version` bump invalidates all existing sessions | FR-A-13 |
| TC-M01-15 | Lockout after 5 failures; unknown-user timing within 20% of known-user | FR-A-11 |
| TC-M01-16 | Admin-forced change reuses the identical flow | FR-A-12 |
| TC-M01-17 | Deleting the last admin is refused | I8 |
| TC-M01-18 | Created users start with `must_change_password=true` | FR-B-02 |

Coverage target: **90%**.

---

## 9. Acceptance criteria

- [ ] Journey J1 passes end to end including every interrupt variant
- [ ] TC-M01-02 passes repeatedly under `--scale api-control=3`
- [ ] No password, hash, or token appears anywhere in logs during a full integration run
- [ ] `POST /v1/auth/login` p95 < 250 ms (Argon2 dominates and that is correct)
- [ ] Every state change emits an audit event
- [ ] OpenAPI documents all endpoints with examples

---

## 10. Task breakdown

| Task | Description | Est (d) | Deps |
| --- | --- | --- | --- |
| T-M01-01 | ORM models + Alembic migration for `user`, `session`, `system_bootstrap` | 0.5 | T-M00-05 |
| T-M01-02 | Argon2 hasher + password policy + common-password list | 1.0 | |
| T-M01-03 | **Bootstrap with advisory lock + banner** | 1.0 | T-M01-01 |
| T-M01-04 | Session issue / resolve / revoke, credential_version validation | 1.0 | |
| T-M01-05 | **Login with the forced-change branch** | 1.0 | T-M01-02,04 |
| T-M01-06 | **`complete_initial_setup` transaction** | 1.0 | T-M01-05 |
| T-M01-07 | **Forced-change middleware + allowlist** | 0.5 | T-M01-05 |
| T-M01-08 | Lockout + rate limiting | 0.5 | |
| T-M01-09 | User CRUD service + endpoints | 1.5 | T-M02-* |
| T-M01-10 | Auth router, CSRF, cookie handling | 1.0 | |
| T-M01-11 | Tests TC-M01-01..18 | 2.0 | all |
| | **Total** | **11.0** | |
