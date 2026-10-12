# M02 — Authorization

| | |
| --- | --- |
| **Package** | `cairn.authz` |
| **Layer** | L2 domain service — spans **both planes** |
| **Phase** | 1 |
| **Owner** | Backend eng. A |
| **Depends on** | M00 core; M01 identity (facade); M15 platform (audit, facade) |
| **Depended on by** | every module that guards an operation |
| **Tables owned** | `api_key`, `resource_grant` |
| **Requirements owned** | FR-B-01 … FR-B-14, FR-I-07, NFR-P-06, NFR-SEC-13 |

---

## 1. Purpose and scope

What an authenticated principal is allowed to do, resolved in O(1) on the data plane.

**In scope:** the permission model, grant CRUD, effective-permission resolution and caching,
API key lifecycle and authentication, rate limiting, break-glass, FastAPI authorization
dependencies.

**Out of scope:** authentication of humans (M01), audit persistence (M15), quota enforcement (M15).

## 1.1 The two-subpackage split — read this first

```
cairn/authz/
├── dataplane.py     ← importable by M09/M13. Cache-only. NO ORM, NO control-plane imports.
├── service.py       ← control plane. Grant/key CRUD. Imports ORM.
├── model.py         ← Permission/Principal value objects. Importable by anyone.
├── deps.py          ← FastAPI dependencies (both planes)
└── ...
```

`cairn.authz.dataplane` is the **only** part `M09` and `M13` may import (`NFR-M-02`). It reads
Redis and, on a miss, calls a narrow read-only repository — it never imports
`cairn.identity`, `cairn.catalog`, or any control-plane module.

---

## 2. Permission model

### Roles — platform capability only (`FR-B-01`, `FR-B-05`)

| Role | Implicit platform capabilities |
| --- | --- |
| `admin` | `platform:users`, `platform:models`, `platform:storage`, `platform:settings`, `platform:audit`, plus `kb:manage` on every KB in the workspace |
| `user` | none implicit — everything comes from grants |

Platform capabilities are **not grantable** to `user` accounts. There is no "power user".

### Resource permissions — grant-based (`FR-B-04`)

| Resource | Permissions |
| --- | --- |
| `workspace` | `kb:create` |
| `knowledge_base` | `kb:read`, `kb:query`, `kb:write`, `kb:manage` |
| `pipeline` | `pipeline:read`, `pipeline:edit`, `pipeline:run` |
| `function` | `function:use`, `function:edit` |
| `model` | `model:use`, `model:manage` |
| `golden_set` | `eval:read`, `eval:run` |

**`kb:query` is deliberately separate from `kb:read`** (`FR-B-07`). An Agent's key should be
able to *retrieve* from a KB without being able to *enumerate and export* its documents. This
distinction is the difference between granting a search capability and granting a data dump.

| Permission | Grants |
| --- | --- |
| `kb:query` | `POST /v1/retrieval/query`, MCP search. Returns matched chunks only. |
| `kb:read` | List documents, browse all chunks, read KB settings, export |
| `kb:write` | Upload, delete, edit documents and chunks |
| `kb:manage` | Change settings, reindex, manage grants, delete the KB |

`kb:manage` ⊃ `kb:write` ⊃ `kb:read` ⊃ `kb:query` for **implication** purposes, but each is
granted explicitly and stored explicitly. Implication is resolved at check time, never expanded
at write time (so revoking `kb:manage` does not silently leave `kb:read` behind).

### Admin content access (`FR-B-09`, `FR-B-10`)

| Mode | Admin can manage a KB | Admin can read chunk content |
| --- | --- | --- |
| `always` | ✅ | ✅ |
| `on_grant` | ✅ | only with an explicit grant from the owner |
| `break_glass` **(default)** | ✅ | only via a time-boxed, reasoned, audited self-grant |

> This is the difference between "we can host your team's documents" and "we can't". In an
> internal tool, admin-sees-all is expected; in infrastructure holding other teams' contracts,
> it is a compliance blocker. The cost of building it is one extra branch in one function.

---

## 3. Public interface

```python
# ---- cairn/authz/model.py — importable by all layers ----
@dataclass(frozen=True)
class Principal:
    type: Literal["user", "api_key"]
    id: UUID
    workspace_id: UUID
    role: Literal["admin", "user"]
    owner_user_id: UUID | None                 # api_key only
    must_change_password: bool
    permissions: frozenset[str]                # workspace-wide, e.g. {"kb:create"}
    resource_permissions: Mapping[UUID, frozenset[str]]   # resource_id -> perms
    accessible_kb_ids: frozenset[UUID]         # precomputed for the data plane
    rate_limit_rpm: int | None
    perm_version: int

    def can(self, permission: str, resource_id: UUID | None = None) -> bool: ...

# ---- cairn/authz/dataplane.py — the ONLY authz surface M09/M13 may import ----
class DataPlaneAuthz:
    async def authenticate_api_key(self, raw_key: str, *, ip: str) -> Principal:
        """O(1): one Redis GET on the happy path. NFR-P-06 budget < 5 ms p99."""
    async def check_rate_limit(self, principal: Principal) -> RateLimitState: ...

# ---- cairn/authz/service.py — control plane ----
class AuthzService:
    async def resolve_principal_for_user(self, user_id: UUID) -> Principal: ...
    async def grant(self, actor: Principal, spec: GrantSpec) -> GrantView: ...
    async def revoke(self, actor: Principal, grant_id: UUID) -> None: ...
    async def list_grants(self, subject_type, subject_id) -> list[GrantView]: ...
    async def list_resource_grants(self, resource_type, resource_id) -> list[GrantView]: ...
    async def open_break_glass(self, actor: Principal, kb_id: UUID, reason: str) -> GrantView: ...

    async def create_api_key(self, actor: Principal, spec: ApiKeySpec) -> tuple[ApiKeyView, str]:
        """Returns the view and the plaintext key. Plaintext is returned ONCE. FR-B-13"""
    async def list_api_keys(self, workspace_id, owner_user_id=None) -> list[ApiKeyView]: ...
    async def revoke_api_key(self, actor: Principal, key_id: UUID) -> None: ...

# ---- cairn/authz/deps.py — FastAPI dependencies ----
def require_permission(permission: str, resource_param: str | None = None) -> Callable: ...
def require_role(role: str) -> Callable: ...
def current_principal() -> Callable: ...
```

---

## 4. Behaviour

### 4.1 API key format and authentication (`FR-B-13`)

```
cairn_sk_live_7Kq2mVx9RtL4pZsN3wYbG5hJ8dF6cA1e
│     │  │    └── 32 random bytes, base62 (~190 bits of entropy)
│     │  └─────── environment: live | test
│     └────────── secret-key marker
└──────────────── brand
```

- `key_prefix` = first 18 chars, indexed, and shown in the UI as `cairn_sk_live_7Kq2…A1e`.
- `key_hash` = **SHA-256** of the full key. Not Argon2 — the key is 190 bits of true randomness,
  so there is nothing to brute-force, and the hot path needs an O(1) lookup, not a 100 ms KDF.
- Plaintext is shown exactly once and is unrecoverable.

```python
async def authenticate_api_key(self, raw_key: str, *, ip: str) -> Principal:
    if not raw_key.startswith("cairn_sk_"):
        raise AuthenticationError("Invalid API key.")
    key_hash = sha256(raw_key.encode()).hexdigest()

    cached = await self.cache.get(f"authz:key:{key_hash}")
    if cached:                                   # ← the happy path. one Redis GET.
        principal = Principal.model_validate_json(cached)
    else:
        principal = await self._resolve_from_db(key_hash)     # narrow read-only repo
        await self.cache.set(f"authz:key:{key_hash}",
                             principal.model_dump_json(), ttl=60)

    if principal.ip_allowlist and not ip_in(ip, principal.ip_allowlist):
        raise PermissionError_("Source address not permitted for this key.")
    return principal
```

### 4.2 Effective permission resolution (`FR-B-06`, `FR-B-08`)

```
For a USER:
    grants = SELECT … WHERE subject=(user,id) AND revoked_at IS NULL
                        AND (expires_at IS NULL OR expires_at > now())
    if role == 'admin':
        grants += implicit platform capabilities
        grants += kb:manage on every KB in the workspace
        if admin_content_access == 'always':
            grants += kb:read, kb:query on every KB
    effective = union(grants)

For an API KEY:
    key_grants   = key.scopes × (key.kb_ids or owner's accessible KBs)
    owner_perms  = effective permissions of owner_user_id      ← recursive
    effective    = key_grants ∩ owner_perms                    ← FR-B-08. THE INTERSECTION.
```

> **The intersection is the important part.** A key can never exceed its owner's permissions,
> and it is computed at authentication time rather than stored. So revoking a user's access to
> a KB instantly narrows every key they ever issued — with no key bookkeeping, no cascade
> update, and no possibility of an orphaned over-privileged key.

### 4.3 Cache invalidation by version bump (`FR-B-12`)

Cache keys embed a version counter:

```
authz:key:{key_hash}                  → Principal JSON, TTL 60 s
authz:user:{user_id}:{perm_version}   → Principal JSON, TTL 300 s
```

A grant change runs `UPDATE "user" SET perm_version = perm_version + 1` for every affected
subject **in the same transaction as the grant write**. The old cache key becomes unreachable
and expires naturally.

**Never enumerate keys to invalidate.** `SCAN`+`DEL` is O(n) on a hot Redis and races with
concurrent writes. Version bumping is O(1) and race-free.

For API keys (whose cache key has no version component) the 60 s TTL bounds staleness, and
**revocation additionally publishes an explicit invalidation** on a Redis channel that all
`api-data` replicas subscribe to — revocation must be immediate, not eventually consistent.

### 4.4 Authorization dependency

```python
def require_permission(permission: str, resource_param: str | None = None):
    async def dependency(request: Request,
                         principal: Principal = Depends(current_principal)) -> Principal:
        resource_id = None
        if resource_param:
            resource_id = decode_id_from_path(request, resource_param)
        if not principal.can(permission, resource_id):
            await audit.record(action="authz.denied", outcome="denied",
                               detail={"permission": permission, "resource_id": str(resource_id)})
            raise PermissionError_(f"This action requires {permission}.")
        return principal
    return dependency
```

**Every endpoint carries one.** A route without an authorization dependency fails code review.
Deny by default; there is no implicit-allow path.

### 4.5 Break-glass (`FR-B-10`)

```python
async def open_break_glass(self, actor, kb_id, reason) -> GrantView:
    if actor.role != "admin":
        raise PermissionError_("Only administrators may open break-glass access.")
    if len(reason.strip()) < 20:
        raise ValidationError("A substantive reason is required (min 20 characters).")

    grant = await self.repo.create_grant(GrantSpec(
        subject_type="user", subject_id=actor.id,
        resource_type="knowledge_base", resource_id=kb_id,
        permissions=["kb:read", "kb:query"],
        is_break_glass=True, reason=reason,
        expires_at=utcnow() + self.settings.break_glass_ttl,
    ))
    await self.audit.record(action="break_glass.open", outcome="success",
                            resource_id=kb_id, detail={"reason": reason,
                                                       "expires_at": grant.expires_at})
    await self.notify.kb_owner(kb_id, BreakGlassNotification(actor, reason, grant.expires_at))
    await self.bump_perm_version(actor.id)
    return grant
```

Expiry is passive — expired grants are excluded by the resolution query, so no cleanup job is
required for correctness.

### 4.6 Rate limiting (`FR-I-07`, `NFR-SEC-13`)

Sliding-window counter in Redis, per key and per IP.

```python
async def check_rate_limit(self, principal) -> RateLimitState:
    limit = principal.rate_limit_rpm or self.settings.default_rpm
    window = int(utcnow().timestamp()) // 60
    count = await self.cache.incr(f"rl:{principal.id}:{window}", ttl=120)
    state = RateLimitState(limit=limit, remaining=max(0, limit - count),
                           reset=(window + 1) * 60)
    if count > limit:
        raise RateLimitError("Rate limit exceeded.", retry_after=state.reset - now)
    return state
```

Headers on **every** response, success or failure (RFC 9331):

```
RateLimit-Limit: 600
RateLimit-Remaining: 417
RateLimit-Reset: 23
```

A machine client that can see its own budget throttles itself. One that cannot, retries into a
wall.

---

## 5. API endpoints owned

| Method | Path | Auth | Req |
| --- | --- | --- | --- |
| GET | `/v1/grants` | `platform:users` or resource `*:manage` | FR-B-03 |
| POST | `/v1/grants` | `platform:users` or resource `*:manage` | FR-B-03 |
| DELETE | `/v1/grants/{id}` | same | FR-B-03 |
| GET | `/v1/users/{id}/permissions` | `platform:users` or self | FR-B-03 |
| POST | `/v1/knowledge-bases/{id}/break-glass` | admin | FR-B-10 |
| GET | `/v1/api-keys` | self, or `platform:users` for all | FR-B-14 |
| POST | `/v1/api-keys` | authenticated | FR-B-08 |
| DELETE | `/v1/api-keys/{id}` | owner or `platform:users` | FR-B-14 |
| GET | `/v1/api-keys/{id}/usage` | owner or `platform:users` | FR-B-14 |

---

## 6. Errors owned

| Code | HTTP | Meaning |
| --- | --- | --- |
| `PERMISSION_DENIED` | 403 | Generic; detail names the required permission |
| `CONTENT_ACCESS_REQUIRES_GRANT` | 403 | Admin blocked by `break_glass` / `on_grant` |
| `API_KEY_INVALID` | 401 | Unknown, revoked, or expired |
| `API_KEY_IP_NOT_ALLOWED` | 403 | Source outside the CIDR allowlist |
| `GRANT_EXCEEDS_OWNER` | 400 | Key scope exceeds the owner's permissions |
| `RATE_LIMIT_EXCEEDED` | 429 | With `Retry-After` |
| `BREAK_GLASS_REASON_REQUIRED` | 400 | |

---

## 7. Performance requirements

| Operation | Budget | Notes |
| --- | --- | --- |
| `authenticate_api_key` cache hit | **< 3 ms p99** | One Redis GET (`NFR-P-06`) |
| `authenticate_api_key` cache miss | < 25 ms p99 | 2 queries; measured separately |
| `Principal.can()` | < 10 µs | Pure in-memory set lookup |
| Rate limit check | < 2 ms p99 | One Redis INCR |
| Cache hit rate | > 99% steady state | Alert below 95% |

---

## 8. Test requirements

| ID | Test | Req |
| --- | --- | --- |
| TC-M02-01 | Grant union across multiple grants resolves correctly | FR-B-06 |
| TC-M02-02 | Expired grants are excluded | FR-B-06 |
| TC-M02-03 | Revoked grants are excluded | FR-B-06 |
| TC-M02-04 | **Key permissions are the intersection with the owner's** | FR-B-08 |
| TC-M02-05 | **Revoking the owner's KB access immediately narrows their key** | FR-B-08 |
| TC-M02-06 | `kb:query` alone permits retrieval but not document listing | FR-B-07 |
| TC-M02-07 | `admin` implicitly holds platform capabilities | FR-B-01 |
| TC-M02-08 | A `user` cannot be granted a platform capability | FR-B-05 |
| TC-M02-09 | Under `break_glass`, admin content read is denied | FR-B-09 |
| TC-M02-10 | Break-glass grants access, expires, and audits | FR-B-10 |
| TC-M02-11 | Break-glass without a reason is rejected | FR-B-10 |
| TC-M02-12 | Key plaintext is returned once and never again | FR-B-13 |
| TC-M02-13 | Only the SHA-256 is stored; plaintext appears nowhere | FR-B-13 |
| TC-M02-14 | IP allowlist enforcement | FR-B-08 |
| TC-M02-15 | **Grant change bumps `perm_version` and invalidates within 60 s** | FR-B-12 |
| TC-M02-16 | **Key revocation takes effect immediately across replicas (pubsub)** | FR-B-14 |
| TC-M02-17 | Rate limit headers present on success and on 429 | FR-I-07 |
| TC-M02-18 | Rate limit enforced per key, independently across keys | NFR-SEC-13 |
| TC-M02-19 | **`import-linter`: `dataplane.py` imports no ORM and no control-plane module** | NFR-M-02 |
| TC-M02-20 | Cache-hit auth p99 < 3 ms over 10k iterations | NFR-P-06 |

Coverage target: **90%**.

---

## 9. Acceptance criteria

- [ ] All 20 test cases pass
- [ ] `import-linter` confirms data-plane isolation of `dataplane.py`
- [ ] Benchmark: 10k authentications, p99 < 3 ms on cache hit
- [ ] Every endpoint in the codebase has an authorization dependency (CI check enumerating routes)
- [ ] Grant and key changes emit audit events with before/after
- [ ] Journey J2 and J8 pass end to end

---

## 10. Task breakdown

| Task | Description | Est (d) | Deps |
| --- | --- | --- | --- |
| T-M02-01 | ORM + migration for `api_key`, `resource_grant` | 0.5 | T-M00-05 |
| T-M02-02 | `model.py` — Permission, Principal, implication rules | 1.0 | |
| T-M02-03 | Grant repository + resolution query | 1.0 | T-M02-01 |
| T-M02-04 | **Effective-permission resolution incl. key ∩ owner** | 1.5 | T-M02-03 |
| T-M02-05 | **`dataplane.py` — cache-only auth path** | 1.0 | T-M02-04 |
| T-M02-06 | Version-bump cache invalidation + revocation pubsub | 1.0 | T-M02-05 |
| T-M02-07 | API key lifecycle: generate, hash, store, revoke | 1.0 | T-M02-01 |
| T-M02-08 | Rate limiter + RFC 9331 headers | 1.0 | |
| T-M02-09 | Break-glass flow + notification hook | 1.0 | T-M02-03 |
| T-M02-10 | `deps.py` FastAPI dependencies | 0.5 | T-M02-04 |
| T-M02-11 | Grant and key routers | 1.0 | T-M02-07 |
| T-M02-12 | Tests TC-M02-01..20 + benchmark | 2.5 | all |
| | **Total** | **13.0** | |
