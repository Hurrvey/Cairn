# M00 — Core Foundation

| | |
| --- | --- |
| **Package** | `cairn.core` |
| **Layer** | L0 foundation — imports nothing internal |
| **Plane** | shared |
| **Phase** | 0 |
| **Owner** | Backend lead |
| **Depends on** | nothing |
| **Depended on by** | every module |
| **Requirements owned** | FR-I-04, FR-I-06, FR-I-08, FR-I-10, FR-I-11, NFR-O-01, NFR-O-04, NFR-D-07, NFR-M-04, NFR-SEC-03 |

---

## 1. Purpose and scope

Primitives every module needs, with **zero domain knowledge**. If `core` ever imports something
about knowledge bases, documents, or retrieval, the architecture has failed.

**In scope:** settings, errors, IDs, time, logging, tracing, metrics, DB session/transaction,
Redis client, HTTP client with SSRF protection, pagination, idempotency, health checks,
OpenAPI customization.

**Out of scope:** anything naming a domain entity; anything requiring a database table.

---

## 2. Public interface

```python
# cairn/core/config.py
class Settings(BaseSettings): ...
def get_settings() -> Settings: ...            # cached singleton

# cairn/core/errors.py
class CairnError(Exception):
    code: ClassVar[str]; http_status: ClassVar[int]; title: ClassVar[str]
    def __init__(self, detail: str | None = None, **context: Any) -> None: ...
    def to_problem(self, instance: str, request_id: str) -> ProblemDetail: ...
# + ValidationError, AuthenticationError, PermissionError_, NotFoundError,
#   ConflictError, QuotaExceededError, RateLimitError, UpstreamError, InternalError

# cairn/core/ids.py
def new_uuid() -> UUID: ...                     # UUIDv7 for index locality
def encode_id(prefix: str, value: UUID) -> str: ...
def decode_id(prefix: str, value: str) -> UUID: ...   # raises InvalidIdError on prefix mismatch

# cairn/core/time.py
def utcnow() -> datetime: ...                   # the ONLY clock. tests monkeypatch this.

# cairn/core/db.py
@asynccontextmanager
async def transaction() -> AsyncIterator[AsyncSession]: ...
async def get_session() -> AsyncIterator[AsyncSession]: ...
async def advisory_lock(session: AsyncSession, key: int) -> None: ...

# cairn/core/cache.py
class Cache(Protocol):
    async def get(self, key: str) -> bytes | None: ...
    async def set(self, key: str, value: bytes, ttl: int) -> None: ...
    async def delete(self, key: str) -> None: ...
    async def incr(self, key: str, ttl: int) -> int: ...
def get_cache() -> Cache: ...

# cairn/core/http.py
def safe_client() -> httpx.AsyncClient: ...     # SSRF-protected. NFR-SEC-09
def internal_client() -> httpx.AsyncClient: ... # for known internal services only

# cairn/core/logging.py
def get_logger(name: str) -> structlog.BoundLogger: ...
def bind_request_context(**kwargs: Any) -> None: ...

# cairn/core/pagination.py
class CursorPage(BaseModel, Generic[T]):
    items: list[T]; next_cursor: str | None; has_more: bool
def encode_cursor(**fields: Any) -> str: ...
def decode_cursor(cursor: str) -> dict[str, Any]: ...

# cairn/core/idempotency.py
async def idempotent(key: str | None, ttl: int = 86400) -> AsyncIterator[IdempotencyGuard]: ...
```

---

## 3. Behaviour

### 3.1 SSRF-protected HTTP client (`NFR-SEC-09`) — security-critical

The single most subtle security control in the system. An attacker who can supply a URL
(crawl seed, pipeline HTTP node, webhook target) will otherwise read cloud instance metadata.

```python
BLOCKED_NETWORKS = [
    ip_network("0.0.0.0/8"),     ip_network("10.0.0.0/8"),
    ip_network("127.0.0.0/8"),   ip_network("169.254.0.0/16"),  # ← cloud metadata
    ip_network("172.16.0.0/12"), ip_network("192.168.0.0/16"),
    ip_network("100.64.0.0/10"), ip_network("::1/128"),
    ip_network("fc00::/7"),      ip_network("fe80::/10"),
]

async def _guard(request: httpx.Request) -> None:
    """Resolve and validate EVERY hop. Pre-flight checks alone are defeated by DNS rebinding:
    the attacker's DNS returns a public IP for the check, then 127.0.0.1 for the real fetch."""
    infos = await resolve(request.url.host)          # our own resolution
    for addr in infos:
        if any(addr in net for net in BLOCKED_NETWORKS):
            raise BlockedAddressError(host=request.url.host, resolved=str(addr))
    request.extensions["pinned_ip"] = infos[0]       # connect to what we validated
```

Requirements:
1. Resolve DNS ourselves and **connect to the validated IP**, not to the hostname.
2. Re-validate on **every redirect hop**; cap redirects at 5.
3. Cap response size (default 50 MB) and total time (default 30 s).
4. Only `http`/`https` schemes.
5. Optional per-caller allowlist that bypasses the block (admin-configured, audited).

### 3.2 Transaction management

```python
@asynccontextmanager
async def transaction() -> AsyncIterator[AsyncSession]:
    """One transaction per request on the control plane. Commits on clean exit,
    rolls back on any exception. Nesting reuses the outer transaction (savepoint)."""
```

Control-plane middleware opens one per request. The data plane never opens a write transaction.

### 3.3 Idempotency (`FR-I-10`)

```
1. Client sends Idempotency-Key: <opaque>
2. Guard computes fingerprint = sha256(method + path + principal_id + body)
3. Redis SET NX idem:{key} = {fingerprint, state:"in_progress"} TTL 24h
     - not set, same fingerprint, state=completed → replay the stored response
     - not set, same fingerprint, state=in_progress → 409 REQUEST_IN_PROGRESS
     - not set, DIFFERENT fingerprint             → 422 IDEMPOTENCY_KEY_REUSED
     - set → proceed; store response on success
```

The fingerprint check matters: reusing a key with a different body is a client bug that
silently returns the wrong resource if unchecked.

### 3.4 Cursor pagination (`FR-I-11`)

Opaque base64url of `{"k": [<sort key values>], "v": 1}`, sorted by `(sort_field, id)` for
stability. Offset pagination is forbidden — it drifts under concurrent writes and degrades on
deep pages.

### 3.5 Health checks (`NFR-O-04`)

| Endpoint | Checks | Semantics |
| --- | --- | --- |
| `GET /healthz` | process alive | Liveness. Never touches a dependency — a slow DB must not trigger a restart loop. |
| `GET /readyz` | Postgres, Redis, and (data plane) the vector store | Readiness. 200 with per-check detail, or 503. |

### 3.6 Structured logging

`structlog` → JSON → stdout. Contextvars carry `request_id`, `trace_id`, `workspace_id`,
`principal_id`, `role` automatically into every line.

Redaction processor drops values whose key matches
`password|token|api_key|secret|authorization|credential|passwd|private_key` (`NFR-SEC-03`).
A safety net, not a licence for carelessness.

---

## 4. Configuration

| Variable | Default | Description |
| --- | --- | --- |
| `CAIRN_ROLE` | `all` | `control` \| `data` \| `worker` \| `all` |
| `CAIRN_ENVIRONMENT` | `dev` | `dev` \| `staging` \| `prod` |
| `CAIRN_LOG_LEVEL` | `INFO` | |
| `CAIRN_LOG_FORMAT` | `json` | `json` \| `console` (dev) |
| `CAIRN_DATABASE_URL` | — | **required** |
| `CAIRN_REDIS_URL` | — | **required** |
| `CAIRN_MASTER_KEY` | — | **required**; 32 bytes base64 |
| `CAIRN_DB__POOL_SIZE` | `20` | |
| `CAIRN_DB__MAX_OVERFLOW` | `10` | |
| `CAIRN_DB__STATEMENT_TIMEOUT_MS` | `30000` | `2000` on the data plane |
| `CAIRN_HTTP__TIMEOUT_S` | `30` | |
| `CAIRN_HTTP__MAX_RESPONSE_BYTES` | `52428800` | |
| `CAIRN_TELEMETRY__OTLP_ENDPOINT` | none | Tracing disabled if unset |
| `CAIRN_TELEMETRY__METRICS_ENABLED` | `true` | |

Startup validation MUST fail fast: an invalid `DATABASE_URL` or a missing `MASTER_KEY` exits
non-zero with a clear message rather than failing on the first request.

---

## 5. Errors owned

| Code | HTTP | Meaning |
| --- | --- | --- |
| `VALIDATION_FAILED` | 400 | Schema or bounds violation |
| `INVALID_ID` | 400 | Malformed or wrong-prefix public ID |
| `AUTHENTICATION_FAILED` | 401 | No or bad credentials |
| `PERMISSION_DENIED` | 403 | Authenticated but not allowed |
| `RESOURCE_NOT_FOUND` | 404 | |
| `RESOURCE_CONFLICT` | 409 | Uniqueness or state conflict |
| `REQUEST_IN_PROGRESS` | 409 | Idempotent request still running |
| `IDEMPOTENCY_KEY_REUSED` | 422 | Same key, different payload |
| `QUOTA_EXCEEDED` | 429 | |
| `RATE_LIMIT_EXCEEDED` | 429 | With `Retry-After` |
| `BLOCKED_ADDRESS` | 400 | SSRF guard rejected the target |
| `UPSTREAM_UNAVAILABLE` | 502 | Provider or backend failed |
| `INTERNAL_ERROR` | 500 | Detail logged, never returned |

---

## 6. Performance requirements

| Operation | Budget |
| --- | --- |
| `utcnow()`, `encode_id()` | < 1 µs |
| Cache get/set | < 2 ms p99 |
| Transaction open/close overhead | < 1 ms |
| Logging call | < 50 µs |
| `/healthz` | < 5 ms, no dependency I/O |

---

## 7. Test requirements

| ID | Test |
| --- | --- |
| TC-M00-01 | `decode_id` rejects a `doc_` ID where `kb_` is expected |
| TC-M00-02 | Settings validation fails fast on a bad `DATABASE_URL` |
| TC-M00-03 | Errors serialize to RFC 9457 with a stable `code` |
| TC-M00-04 | Log redaction removes every configured sensitive key, including nested |
| TC-M00-05 | **SSRF: `http://169.254.169.254/` is blocked** |
| TC-M00-06 | **SSRF: a redirect from a public host to `127.0.0.1` is blocked on the redirect hop** |
| TC-M00-07 | **SSRF: DNS rebinding — a host resolving to a public IP then a private one is blocked (IP pinning)** |
| TC-M00-08 | SSRF: response exceeding `MAX_RESPONSE_BYTES` is aborted mid-stream |
| TC-M00-09 | Idempotency: replay of the same key + body returns the stored response |
| TC-M00-10 | Idempotency: same key + different body returns `IDEMPOTENCY_KEY_REUSED` |
| TC-M00-11 | Cursor pagination is stable when rows are inserted between pages |
| TC-M00-12 | `transaction()` rolls back on exception; nested reuses the outer |
| TC-M00-13 | `/healthz` returns 200 with Postgres stopped; `/readyz` returns 503 |

Coverage target: **90%** (foundation code, everything depends on it).

---

## 8. Acceptance criteria

- [ ] `mypy --strict` passes on `cairn/core`
- [ ] `import-linter` confirms `core` imports no internal module
- [ ] All 13 test cases pass
- [ ] SSRF suite passes including the rebinding case
- [ ] A misconfigured process exits non-zero within 5 s with an actionable message
- [ ] Structured logs contain full request context in every line during an integration run
- [ ] `/v1/openapi.json` validates against the OpenAPI 3.1 schema

---

## 9. Task breakdown

| Task | Description | Est (d) |
| --- | --- | --- |
| T-M00-01 | Project scaffold, `uv`, ruff, mypy, import-linter, Makefile, CI | 1.5 |
| T-M00-02 | `config.py` — Settings, validation, fail-fast startup | 0.5 |
| T-M00-03 | `errors.py` — hierarchy, RFC 9457 serialization, FastAPI handler | 1.0 |
| T-M00-04 | `ids.py`, `time.py` | 0.5 |
| T-M00-05 | `db.py` — engine, session, transaction, advisory lock | 1.0 |
| T-M00-06 | `cache.py` — Redis client + Protocol | 0.5 |
| T-M00-07 | **`http.py` — SSRF-protected client** | 1.5 |
| T-M00-08 | `logging.py` — structlog, context binding, redaction | 1.0 |
| T-M00-09 | `telemetry.py` — OTel tracing + Prometheus metrics | 1.0 |
| T-M00-10 | `pagination.py` — cursor encode/decode | 0.5 |
| T-M00-11 | `idempotency.py` | 0.5 |
| T-M00-12 | `health.py` — `/healthz`, `/readyz` | 0.5 |
| T-M00-13 | Test suite TC-M00-01..13 | 1.5 |
| | **Total** | **11.5** |
