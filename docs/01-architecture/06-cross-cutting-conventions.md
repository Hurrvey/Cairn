# Cross-Cutting Conventions

**Document:** `01-architecture/06-cross-cutting-conventions.md`
**Status:** Normative — every contributor reads this before writing code
**Date:** 2026-08-28

---

## 1. Configuration

All configuration is environment-variable driven (`NFR-D-07`). One `Settings` object,
composed from per-module settings, validated at startup — the process **must refuse to start**
on invalid config rather than fail later at request time.

```python
# cairn/core/config.py
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CAIRN_", env_nested_delimiter="__")

    role: Literal["control", "data", "worker", "all"] = "all"
    environment: Literal["dev", "staging", "prod"] = "dev"
    log_level: str = "INFO"

    database_url: PostgresDsn
    redis_url: RedisDsn
    master_key: SecretStr                      # NFR-SEC-02

    db: DatabaseSettings = DatabaseSettings()
    auth: AuthSettings = AuthSettings()
    retrieval: RetrievalSettings = RetrievalSettings()
    tasks: TaskSettings = TaskSettings()
    telemetry: TelemetrySettings = TelemetrySettings()
```

Rules:
- Prefix `CAIRN_`; nested with `__` → `CAIRN_RETRIEVAL__CANDIDATE_K=200`.
- Every setting has a default **except** secrets and connection strings.
- Secrets are `SecretStr`; `repr()` must never reveal them.
- Full reference table maintained in [`../06-ops/01-deployment.md`](../06-ops/01-deployment.md) §6.

---

## 2. Identifiers

| Context | Format | Example |
| --- | --- | --- |
| Database primary key | `UUID` (v7 preferred, for index locality) | `018f3a…` |
| Public API identifier | `<prefix>_<ULID base32>` | `kb_01HQZX3N9K2M5P7R8T` |
| Task | `BIGSERIAL` — internal only, never exposed | `4821` |
| Request | `req_<ULID>` | `req_01HQZX…` |
| Idempotency key | client-supplied opaque string ≤ 255 chars | |

**Prefixes** (normative): `ws_` workspace · `usr_` user · `key_` api_key · `grant_` ·
`kb_` knowledge_base · `doc_` document · `chk_` chunk · `crawl_` crawl_job ·
`pipe_` pipeline · `fn_` function · `gs_` golden_set · `run_` eval_run · `mdl_` model ·
`prov_` model_provider · `bind_` storage_binding · `req_` request

```python
# cairn/core/ids.py
def encode_id(prefix: str, value: UUID) -> str: ...
def decode_id(prefix: str, value: str) -> UUID:
    """Raises InvalidIdError if the prefix does not match — this is a security control,
    not just validation: it prevents passing a doc_ id where a kb_ id is expected."""
```

Never expose raw UUIDs in the API. Never accept an unprefixed ID.

---

## 3. Errors

### 3.1 Hierarchy

```python
# cairn/core/errors.py
class CairnError(Exception):
    code: ClassVar[str]              # STABLE machine-readable. Never change after release.
    http_status: ClassVar[int]
    title: ClassVar[str]
    def __init__(self, detail: str | None = None, **context: Any) -> None: ...

class ValidationError(CairnError):    code="VALIDATION_FAILED";      http_status=400
class AuthenticationError(CairnError):code="AUTHENTICATION_FAILED";  http_status=401
class PermissionError_(CairnError):   code="PERMISSION_DENIED";      http_status=403
class NotFoundError(CairnError):      code="RESOURCE_NOT_FOUND";     http_status=404
class ConflictError(CairnError):      code="RESOURCE_CONFLICT";      http_status=409
class QuotaExceededError(CairnError): code="QUOTA_EXCEEDED";         http_status=429
class RateLimitError(CairnError):     code="RATE_LIMIT_EXCEEDED";    http_status=429
class UpstreamError(CairnError):      code="UPSTREAM_UNAVAILABLE";   http_status=502
class InternalError(CairnError):      code="INTERNAL_ERROR";         http_status=500
```

Module-specific errors subclass these with more specific codes:

```python
class PasswordChangeRequiredError(PermissionError_):
    code = "PASSWORD_CHANGE_REQUIRED"        # FR-A-05
class EmbeddingDimensionMismatchError(ValidationError):
    code = "EMBEDDING_DIMENSION_MISMATCH"    # FR-G-04
class KnowledgeBaseIndexingError(ConflictError):
    code = "KB_INDEX_IN_PROGRESS"
```

### 3.2 Wire format — RFC 9457 (`FR-I-04`)

```json
{
  "type": "https://docs.cairn.io/errors/permission-denied",
  "title": "Permission denied",
  "status": 403,
  "code": "PASSWORD_CHANGE_REQUIRED",
  "detail": "The credentials for this account must be changed before continuing.",
  "instance": "/v1/knowledge-bases",
  "request_id": "req_01HQZX3N9K2M5P7R8T",
  "errors": [
    { "field": "top_k", "code": "OUT_OF_RANGE", "detail": "must be between 1 and 100" }
  ]
}
```

Rules:
- `code` is the contract. Clients branch on it. **It never changes after release.**
- `detail` is human-facing and may change; it must never contain secrets, SQL, or stack traces.
- `errors[]` appears only for field-level validation failures.
- Every registered code is listed in [`../03-api/01-api-conventions.md`](../03-api/01-api-conventions.md) §7.

### 3.3 Rules

- Never `except Exception: pass`. Never swallow without logging.
- Never let a driver exception escape a module — wrap it in a module error.
- 5xx responses never expose internal detail; log the detail with the `request_id` and return
  a generic message carrying that ID.
- Errors are raised, not returned as sentinel values.

---

## 4. Logging

Structured JSON via `structlog` to stdout (`NFR-O-01`).

```python
log = get_logger(__name__)
log.info("retrieval.completed",
         kb_id=kb_id, top_k=top_k, latency_ms=94, rerank=True, hit_count=5)
```

**Mandatory context**, injected by middleware and worker harness:
`request_id`, `trace_id`, `span_id`, `workspace_id`, `principal_id`, `principal_type`,
`role` (`data`/`control`/`worker`).

Event naming: `<module>.<action>[.<outcome>]` — `auth.login.failed`,
`ingestion.parse.completed`, `task.claimed`.

| Level | Use for |
| --- | --- |
| DEBUG | Development detail. Off in production. |
| INFO | Normal lifecycle events. One per request, one per task transition. |
| WARNING | Degraded but handled — provider retry, cache miss storm, rerank skipped |
| ERROR | Operation failed, user-visible |
| CRITICAL | System-level failure needing a page |

**Never log** (`NFR-SEC-03`): passwords, password hashes, API keys or their plaintext,
session tokens, provider credentials, decrypted secrets, full document content,
full chunk content (log `chunk_id` and a ≤ 80-char preview only), PII beyond identifiers.

`cairn.core.logging` installs a redaction processor keyed on field names
(`password`, `token`, `api_key`, `secret`, `authorization`, `credential`) as a safety net —
but the safety net is not permission to be careless.

---

## 5. Tracing and metrics

### Tracing (`NFR-O-02`)

OpenTelemetry, OTLP export. Span naming `<module>.<operation>`.

Mandatory spans on the retrieval path: `retrieval.query`, `authz.resolve`,
`embedding.encode`, `vectorstore.search.dense`, `vectorstore.search.sparse`,
`retrieval.fuse`, `retrieval.rerank`, `retrieval.assemble`.

Mandatory span attributes: `cairn.workspace_id`, `cairn.kb_id`, `cairn.principal_type`,
`cairn.index_version`. **Never** put query text or content in span attributes.

Trace context propagates into `task.payload` so worker spans link to the originating request.

### Metrics (`NFR-O-03`)

Naming: `cairn_<subsystem>_<measure>_<unit>`.

```
cairn_retrieval_latency_seconds{stage,kb_id,mode}          histogram
cairn_retrieval_requests_total{status,mode}                counter
cairn_retrieval_results_returned                           histogram
cairn_task_queue_depth{queue}                              gauge     ← KEDA scales on this
cairn_task_duration_seconds{queue,kind,outcome}            histogram
cairn_task_lease_expired_total{queue}                      counter
cairn_ingestion_documents_total{stage,outcome}             counter
cairn_embedding_batch_size                                 histogram
cairn_embedding_cache_hits_total / _misses_total           counter
cairn_authz_cache_hits_total / _misses_total               counter
cairn_provider_requests_total{provider,model,status}       counter
cairn_provider_latency_seconds{provider,model}             histogram
cairn_vectorstore_operation_seconds{driver,op}             histogram
```

**Cardinality rule:** never label by `principal_id`, `document_id`, `chunk_id`, or query text.
`kb_id` is permitted only where the deployment has < 1000 KBs; above that, aggregate.

---

## 6. Time

- All timestamps `TIMESTAMPTZ`, stored and computed in **UTC**.
- Never `datetime.now()` — always `cairn.core.time.utcnow()`, which tests monkeypatch.
- API serializes RFC 3339 with `Z`: `2026-08-28T14:31:07Z`.
- Durations in code are `timedelta`; in the API, integer seconds or milliseconds with the unit
  in the field name (`latency_ms`, `ttl_seconds`).
- Cron expressions are interpreted in the workspace timezone, stored with an explicit `tz`.

---

## 7. Database access

```python
# cairn/core/db.py
@asynccontextmanager
async def transaction() -> AsyncIterator[AsyncSession]: ...
async def get_session() -> AsyncIterator[AsyncSession]: ...   # FastAPI dependency
```

Rules:
1. **One transaction per request** on the control plane, opened by middleware, committed on
   success, rolled back on any exception.
2. **The data plane opens no write transaction.** Read-only, and on the happy path no Postgres
   connection at all.
3. `SELECT … FOR UPDATE` requires a comment explaining the lock ordering. Locks are always
   acquired in table-name alphabetical order to prevent deadlock.
4. Repositories return ORM objects; **services return DTOs**. ORM objects never cross a module
   boundary or reach a router.
5. No lazy loading across an `await` boundary — use explicit `selectinload`.
6. Every query filters on `workspace_id`. A query without it is a code-review rejection.
7. Bulk operations use `execute_values` / `COPY`, never per-row loops.

---

## 8. Async discipline

The data plane's latency budget is spent by any blocking call.

| Rule | Detail |
| --- | --- |
| Never block the loop | No `requests`, no `time.sleep`, no sync DB drivers, no CPU-bound work in a handler |
| CPU-bound work goes to a task | If it takes > 50 ms of CPU, it belongs on a worker queue |
| Unavoidable blocking calls | `await anyio.to_thread.run_sync(...)` |
| Concurrency | `asyncio.gather` for independent I/O — dense and sparse search MUST be gathered |
| Every external call has a timeout | No exceptions. Default 10 s, retrieval path 2 s. |
| Bound concurrency per dependency | `asyncio.Semaphore` per provider (`FR-K-07`) |
| One shared `httpx.AsyncClient` | Created at startup, closed at shutdown. Never per-request. |

---

## 9. Validation

- Every request body is a Pydantic model with `model_config = ConfigDict(extra="forbid")` on
  write endpoints (`NFR-SEC-05`).
- Validate at the boundary; services may then trust their inputs.
- Numeric bounds are declared, not asserted: `top_k: int = Field(5, ge=1, le=100)`.
- Response models are declared explicitly on every route — never return a bare `dict`.
- User-supplied strings that reach a URL, path, or shell are validated against an allowlist.

---

## 10. Testing conventions

Full strategy in [`../05-quality/01-test-strategy.md`](../05-quality/01-test-strategy.md).

| Convention | Rule |
| --- | --- |
| Location | `tests/unit/<module>/`, `tests/integration/<module>/`, `tests/e2e/` |
| Naming | `test_<unit>__<condition>__<expected>` |
| Requirement link | Docstring cites the requirement ID: `"""FR-A-05: middleware blocks non-allowlisted routes."""` |
| Fixtures | Factories in `tests/factories/`; never hand-built dicts |
| Fakes | `tests/fakes/<module>.py`, **maintained by the module owner** |
| Integration deps | testcontainers — real Postgres, Redis, Qdrant. No mocking of the database. |
| Time | `freezegun` or the injected clock. Never `sleep` to wait for time. |
| Async | `pytest-asyncio` in `auto` mode |
| Flakiness | A flaky test is a broken test. Fix or delete — never `@pytest.mark.flaky`. |

---

## 11. API handler shape

Every route follows this shape. Deviation needs a reason in review.

```python
@router.post(
    "/knowledge-bases",
    response_model=KnowledgeBaseResponse,
    status_code=201,
    responses={403: PROBLEM, 409: PROBLEM},
    summary="Create a knowledge base",
)
async def create_knowledge_base(
    body: CreateKnowledgeBaseRequest,
    principal: Principal = Depends(require_permission("kb:create")),
    svc: CatalogService = Depends(get_catalog_service),
) -> KnowledgeBaseResponse:
    kb = await svc.create(principal=principal, spec=body.to_spec())
    return KnowledgeBaseResponse.from_dto(kb)
```

- Authorization is a **dependency**, never an `if` inside the body.
- Handlers contain no business logic — they translate HTTP ↔ service calls.
- No handler exceeds ~20 lines.
- `summary` and `description` are written for the generated OpenAPI, because that is the SDK
  documentation.

---

## 12. Code style

- `ruff` with the repository config; `ruff format`. No debate.
- `mypy --strict` passes on `cairn/*` (`NFR-M-04`). `Any` requires a `# reason:` comment.
- Line length 100.
- Public functions have docstrings; private ones only if non-obvious.
- Comments explain **why**, never **what**. `# increment counter` is noise; `# bump before
  cache read so a concurrent grant change cannot be missed` is valuable.
- No commented-out code. Git remembers.
- `TODO(name): description` — untagged TODOs fail lint.

---

## 13. Internationalization

- Backend messages are English; user-facing text is keyed for the frontend to translate.
- Error `code` is the translation key; `detail` is the English fallback.
- The UI ships `en-US` and `zh-CN` (`FR-P-08`).
- Content processing must be language-aware: tokenizer, sentence splitter, and BM25 analyzer
  are selected by detected document language (`FR-F-04`).
- Never assume whitespace word boundaries — CJK text has none. This affects chunking, token
  counting, highlighting, and BM25.

---

## 14. Security defaults

Checklist in [`../05-quality/03-security-baseline.md`](../05-quality/03-security-baseline.md).

| Default | Rule |
| --- | --- |
| Deny by default | Every endpoint requires an explicit permission dependency. A route without one fails review. |
| Fail closed | On authorization system error, deny. Never "allow on error". |
| Constant-time comparison | `secrets.compare_digest` for every token, hash, and signature check |
| Secret handling | `SecretStr` in config; decrypted only at point of use; never in logs, traces, or responses |
| Outbound requests | Only through `cairn.core.http.safe_client()`, which enforces SSRF protection (`NFR-SEC-09`) |
| User content to LLMs | Delimited and marked untrusted (`NFR-SEC-10`) |
| Randomness | `secrets`, never `random` |
