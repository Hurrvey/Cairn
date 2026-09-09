# Work Breakdown Structure

**Document:** `04-plan/02-work-breakdown.md`
**Status:** Baseline — the assignment source of truth
**Date:** 2026-08-28

Every task in the project, with ID, estimate, dependencies, requirement traceability, and a
completion condition. This is the document you hand to a developer or an Agent.

**2026-09-07 continuation:** see [verification and remaining work](06-continuation-verification.md)
and the [T-M07-01 checkpoint](05-parser-spike.md). T-M07-02's parser foundation and
T-M07-05's four text-format adapters are implemented; the PDF quality gate and
worker pipeline remain open. The baseline estimates below are not completion claims.

**2026-09-07 chunking increment:** T-M07-07/08/09 now have implementations and
verification described in [chunking continuation](08-chunking-implementation.md).
Fixed, recursive, Markdown and parent-child chunkers are library components;
semantic/custom strategies, language detection, PDF/Office/OCR and worker
parse → chunk → embed → index wiring remain open. FR-F-05 and the Phase 2
end-to-end gate are not closed by these changes.

**2026-09-08 continuation:** the [execution ledger](15-execution-ledger.md) is the current
restart/acceptance record. Office adapters and automatic language resolution have passed focused
independent checks; a real TEI/MiniLM initial-index/query/source-replacement test also passed.
The bounded pipeline safety/recovery repair package and advanced semantic/custom validation are
independently accepted. Semantic now uses the real model through the worker; custom execution requires
an explicitly injected scoped executor (the M12 production sandbox remains open). PDF/OCR and the
genuine 30-document PDF evaluation remain open; two full-KB rebuild regressions are still failing.
See the [lifecycle acceptance package](16-lifecycle-acceptance-plan.md) for unimplemented full-KB
rebuild, manual re-embedding and retirement. Neither this note nor a passing normal-path test
marks M07 or the knowledge-base end-to-end gate complete.

---

## How to use this document

**Assigning a task**

1. Pick a task whose `Depends on` are all `done`.
2. The assignee reads: the [module spec](../02-modules/), [cross-cutting conventions](../01-architecture/06-cross-cutting-conventions.md), and the requirements cited in `Req`.
3. The task is complete when `Done when` is objectively true and the
   [Definition of Done](03-team-and-workflow.md#4-definition-of-done) is satisfied.

**Columns**

| Column | Meaning |
| --- | --- |
| `Task` | Stable ID. Never reused. |
| `Est` | Ideal engineer-days for one competent implementer with the spec in hand |
| `Str` | Stream — see [team](03-team-and-workflow.md#1-streams) — `F`oundation, `A`ccess, `K`nowledge, `I`ngestion, `R`etrieval, `X`tensibility, `Q`uality, `W`eb, `O`ps |
| `Deps` | Prerequisite tasks. `—` = none. |
| `Req` | Requirement IDs satisfied |

**Legend:** 🔴 critical path · 🔒 security-gated · 🧪 spike/uncertain

---

## Phase 0 — Skeleton (W1–W2) · 26.0 days

| Task | Description | Est | Str | Deps | Req |
| --- | --- | --- | --- | --- | --- |
| T-OPS-01 | Repo, `uv`, ruff, mypy, import-linter config, Makefile, pre-commit | 1.0 | O | — | NFR-M-01 |
| T-OPS-02 | CI: lint, typecheck, boundary check, test, coverage gate, licence scan | 1.5 | O | T-OPS-01 | NFR-M-01, NFR-SEC-11 |
| T-OPS-03 | Dockerfiles: multi-stage, non-root, pinned digests, api + worker + web | 1.5 | O | T-OPS-01 | NFR-D-04, NFR-SEC-12 |
| T-OPS-04 | Compose: postgres+pgvector, redis, minio, migrate, api, nginx; profiles | 2.0 | O | T-OPS-03 | NFR-D-01/02 |
| T-OPS-05 | `install.sh`: generate `CAIRN_MASTER_KEY`, `.env`, first-run guidance | 0.5 | O | T-OPS-04 | NFR-D-01 |
| 🔴 T-M00-01 | Package scaffold, module skeletons, DI wiring | 1.5 | F | T-OPS-01 | — |
| T-M00-02 | `config.py` — Settings, validation, fail-fast startup | 0.5 | F | T-M00-01 | NFR-D-07 |
| T-M00-03 | `errors.py` — hierarchy, RFC 9457, FastAPI handler | 1.0 | F | T-M00-01 | FR-I-04 |
| T-M00-04 | `ids.py`, `time.py` | 0.5 | F | T-M00-01 | — |
| 🔴 T-M00-05 | `db.py` — engine, session, transaction, advisory lock | 1.0 | F | T-M00-02 | — |
| T-M00-06 | `cache.py` — Redis client + Protocol | 0.5 | F | T-M00-02 | — |
| 🔒 T-M00-07 | `http.py` — SSRF-protected client, IP pinning, redirect revalidation | 1.5 | F | T-M00-02 | NFR-SEC-09 |
| T-M00-08 | `logging.py` — structlog, context binding, redaction | 1.0 | F | T-M00-02 | NFR-O-01, NFR-SEC-03 |
| T-M00-09 | `telemetry.py` — OTel tracing + Prometheus metrics | 1.0 | F | T-M00-02 | NFR-O-02/03 |
| T-M00-10 | `pagination.py` — cursor encode/decode | 0.5 | F | T-M00-01 | FR-I-11 |
| T-M00-11 | `idempotency.py` | 0.5 | F | T-M00-06 | FR-I-10 |
| T-M00-12 | `health.py` — `/healthz`, `/readyz` | 0.5 | F | T-M00-05 | NFR-O-04 |
| T-M00-13 | Tests TC-M00-01..13 incl. SSRF suite | 1.5 | F | T-M00-07 | — |
| T-OPS-06 | Alembic baseline + migrate container with advisory lock | 1.0 | O | T-M00-05 | NFR-D-03 |
| T-OPS-07 | `apps/api` — `CAIRN_ROLE` router mounting, middleware chain | 1.0 | F | T-M00-03 | ADR-0002 |
| T-M01-01 | ORM + migration: `user`, `session`, `system_bootstrap` | 0.5 | A | T-OPS-06 | — |
| T-M01-02 | Argon2 hasher, password policy, common-password list | 1.0 | A | T-M00-01 | FR-A-08/09 |
| 🔴 T-M01-03 | **Bootstrap: advisory lock, generated password, banner** | 1.0 | A | T-M01-01 | FR-A-02/03 |
| T-M01-04 | Session issue/resolve/revoke, `credential_version` validation | 1.0 | A | T-M01-01 | FR-A-10/13 |
| 🔴 T-M01-05 | **Login with the forced-change branch** | 1.0 | A | T-M01-02,04 | FR-A-04 |
| 🔴 T-M01-06 | **`complete_initial_setup` single transaction** | 1.0 | A | T-M01-05 | FR-A-06/07 |
| 🔴 T-M01-07 | **Forced-change middleware + allowlist** | 0.5 | A | T-M01-05 | FR-A-05 |
| T-M01-10 | Auth router, CSRF, cookie handling | 1.0 | A | T-M01-06 | NFR-SEC-04 |
| T-M16-01 | Web scaffold: Vite, TS, Element Plus, Pinia, router, i18n | 1.5 | W | T-OPS-01 | — |
| T-M16-02 | OpenAPI client generation + CI drift check | 0.5 | W | T-OPS-07 | — |
| 🔴 T-M16-04 | **Login + forced credential dialog + router guard** | 2.0 | W | T-M16-02, T-M01-06 | FR-P-02 |
| T-M01-11a | Tests TC-M01-01..11 (bootstrap and forced change) | 1.5 | A | T-M01-07 | — |

**Phase 0 gate:** J1 green including all interrupt variants; `--scale api-control=3` creates one
admin; CI fully green.

---

## Phase 1 — Identity and access (W3–W4) · 42.0 days

| Task | Description | Est | Str | Deps | Req |
| --- | --- | --- | --- | --- | --- |
| 🔴 T-M06-01 | ORM + migration: `task`, `workspace_runtime`, partial indexes | 0.5 | F | T-OPS-06 | — |
| T-M06-02 | `TaskSpec` / `TaskContext` / `TaskResult` types | 0.5 | F | T-M06-01 | — |
| 🔴 T-M06-03 | **Transactional enqueue + dedupe** | 1.0 | F | T-M06-01 | NFR-R-03 |
| 🔴 T-M06-04 | **Claim query with fairness join** | 1.5 | F | T-M06-01 | — |
| T-M06-05 | Lease, heartbeat, reaper | 1.0 | F | T-M06-04 | NFR-R-04 |
| T-M06-06 | Retry classification, backoff, dead-letter | 1.0 | F | T-M06-04 | — |
| 🔴 T-M06-07 | Worker harness, LISTEN/NOTIFY, graceful shutdown | 1.5 | F | T-M06-04 | NFR-S-02 |
| T-M06-08 | Batch claiming for `embed` | 0.5 | F | T-M06-07 | — |
| T-M06-09 | Progress reporting + `document_progress` | 0.5 | F | T-M06-07 | FR-G-09 |
| T-M06-10 | Metrics + tracing integration | 0.5 | F | T-M06-07 | NFR-S-06 |
| T-M06-11 | Maintenance jobs: prune, partitions, sessions, namespaces | 1.0 | F | T-M06-07 | FR-O-03 |
| T-M06-12 | Cancellation | 0.5 | F | T-M06-03 | — |
| T-M06-13 | Tests TC-M06-01..22 + chaos + benchmark | 2.5 | F | T-M06-11 | — |
| T-M15-01 | ORM + migration: `workspace`, `audit_log`, `usage_record` (partitioned) | 1.0 | F | T-OPS-06 | — |
| T-M15-02 | Audit service: record, transactional mode, redaction | 1.5 | F | T-M15-01 | FR-O-01 |
| T-M15-03 | Audit query + streaming export | 1.0 | F | T-M15-02 | FR-O-02 |
| T-M15-04 | Partition create-ahead / drop-expired | 1.0 | F | T-M06-11 | FR-O-03 |
| T-M15-05 | Settings service + schema + caching | 1.0 | F | T-M15-01 | FR-O-06 |
| T-M15-11a | Platform routers (audit, settings) | 0.5 | F | T-M15-03 | — |
| T-M02-01 | ORM + migration: `api_key`, `resource_grant` | 0.5 | A | T-OPS-06 | — |
| 🔴 T-M02-02 | `model.py` — Permission, Principal, implication rules | 1.0 | A | T-M02-01 | FR-B-01/04 |
| T-M02-03 | Grant repository + resolution query | 1.0 | A | T-M02-02 | FR-B-06 |
| 🔴 T-M02-04 | **Effective permissions incl. key ∩ owner** | 1.5 | A | T-M02-03 | FR-B-08 |
| 🔴 T-M02-05 | **`dataplane.py` — cache-only auth path** | 1.0 | A | T-M02-04 | FR-B-11, NFR-P-06 |
| T-M02-06 | Version-bump invalidation + revocation pubsub | 1.0 | A | T-M02-05 | FR-B-12 |
| T-M02-07 | API key lifecycle: generate, hash, store, revoke | 1.0 | A | T-M02-01 | FR-B-13/14 |
| T-M02-08 | Rate limiter + RFC 9331 headers | 1.0 | A | T-M00-06 | FR-I-07 |
| T-M02-09 | Break-glass flow + notification hook | 1.0 | A | T-M02-03, T-M15-02 | FR-B-09/10 |
| T-M02-10 | `deps.py` FastAPI authorization dependencies | 0.5 | A | T-M02-04 | FR-B-05 |
| T-M02-11 | Grant and key routers | 1.0 | A | T-M02-07 | FR-B-03 |
| T-M02-12 | Tests TC-M02-01..20 + benchmark | 2.5 | A | T-M02-11 | — |
| T-M01-08 | Lockout + auth rate limiting | 0.5 | A | T-M01-05 | FR-A-11 |
| T-M01-09 | User CRUD service + endpoints | 1.5 | A | T-M02-10 | FR-B-02, FR-A-12 |
| T-M01-11b | Tests TC-M01-12..18 | 0.5 | A | T-M01-09 | — |
| T-M16-03 | Design tokens + shared components (DataTable, StatusBadge, …) | 2.0 | W | T-M16-01 | — |
| T-M16-05 | App shell, navigation, `usePermissions` | 1.5 | W | T-M16-03 | FR-P-01 |
| T-M16-06 | User management screens | 2.0 | W | T-M16-05, T-M01-09 | FR-P-03 |
| T-M16-07 | Grants and permission editor | 2.0 | W | T-M16-05, T-M02-11 | FR-P-03 |
| T-M16-08 | API key management | 1.5 | W | T-M16-05, T-M02-11 | FR-P-03 |
| T-M16-09 | Audit log viewer | 1.5 | W | T-M16-05, T-M15-03 | FR-P-03 |
| T-OPS-08 | Route-enumeration CI check: every endpoint has an authz dependency | 0.5 | O | T-M02-10 | FR-B-05 |

**Phase 1 gate:** J2 and J8 green; key auth p99 < 3 ms; task chaos test loses no work; every
route carries an authz dependency.

---

## Phase 2 — Knowledge core (W5–W9) · 108.0 days ★

### 2a — Drivers and spikes (W5)

| Task | Description | Est | Str | Deps | Req |
| --- | --- | --- | --- | --- | --- |
| 🧪🔴 T-M07-01 | **SPIKE: PDF parser evaluation + AGPL licence decision** | 3.0 | I | — | FR-F-02, RISK-11 |
| T-M04-01 | `ObjectStore` Protocol, DTOs, errors | 0.5 | K | T-M00-03 | — |
| T-M04-02 | `LocalObjectStore` + HMAC presign emulation | 1.0 | K | T-M04-01 | — |
| T-M04-03 | `S3ObjectStore`, multipart, presign | 2.0 | K | T-M04-01 | NFR-SEC-07 |
| T-M04-04 | `ObjectStoreRegistry` + connection caching | 0.5 | K | T-M04-03 | — |
| T-M04-05 | `FakeObjectStore` | 0.5 | K | T-M04-01 | — |
| T-M04-06 | Conformance suite TC-M04-01..12 | 1.5 | K | T-M04-05 | — |
| 🔴 T-M05-01 | `VectorStore` Protocol, `Namespace`, `Capabilities` | 1.0 | K | T-M00-03 | FR-G-07 |
| 🔴 T-M05-02 | **Filter AST + normative semantics** | 1.0 | K | T-M05-01 | FR-H-05 |
| T-M05-03 | `FakeVectorStore` (brute force, exact) | 1.0 | K | T-M05-02 | — |
| 🔴 T-M05-04 | **Conformance suite skeleton** | 1.5 | K | T-M05-03 | — |

### 2b — Storage and catalog (W6)

| Task | Description | Est | Str | Deps | Req |
| --- | --- | --- | --- | --- | --- |
| 🔴 T-M05-05 | pgvector: DDL, namespaces, upsert, dimension validation | 2.0 | K | T-M05-02 | FR-G-04 |
| 🔴 T-M05-06 | pgvector: dense search, HNSW tuning, `ef_search` | 1.5 | K | T-M05-05 | FR-H-01 |
| T-M05-07 | pgvector: FTS sparse + `zhparser` + score normalization | 1.5 | K | T-M05-05 | FR-G-05, FR-F-04 |
| T-M05-08 | pgvector: filter translation | 1.0 | K | T-M05-02 | FR-H-05 |
| T-M05-09 | Registry, pooling, health | 0.5 | K | T-M05-05 | — |
| T-M05-14a | Conformance tests TC-M05-01..16 on pgvector + fake | 2.0 | K | T-M05-08 | — |
| 🔴 T-M03-01 | ORM + migration: `knowledge_base`, `kb_index_version`, `document`, `chunk` (16 partitions), `storage_binding` | 1.5 | K | T-OPS-06 | — |
| 🔴 T-M03-02 | **`guard_kb_embedding_immutable` trigger** | 0.5 | K | T-M03-01 | FR-C-04 |
| T-M03-03 | `ChunkConfig` / `RetrievalConfig` schemas + defaults | 0.5 | K | T-M03-01 | FR-C-05/06 |
| 🔴 T-M03-04 | KB CRUD service | 2.0 | K | T-M03-02 | FR-C-01..07 |
| T-M03-05 | Storage binding CRUD + health test | 1.0 | K | T-M04-04, T-M05-09 | FR-C-03 |
| 🔴 T-M03-06 | **`publish_runtime` + `KnowledgeBaseRuntime` DTO** | 1.0 | K | T-M03-04 | ADR-0002 |
| T-M08-01 | `EmbeddingService` interface, `Vector`, `ModelRef` | 0.5 | I | T-M00-03 | — |
| T-M08-02 | Tokenizer registry, `count_tokens`, truncation | 1.0 | I | T-M08-01 | FR-F-11 |
| 🔴 T-M08-03 | **Embedding cache: keying, normalization, binary encoding** | 1.0 | I | T-M00-06 | FR-G-02 |
| T-M08-04 | `embed_query` hot path | 0.5 | I | T-M08-03 | FR-G-01 |
| T-M08-05 | `embed_documents` batching, order preservation | 1.5 | I | T-M08-03 | FR-G-01 |
| T-M08-06 | Local server client (TEI / Infinity) | 1.0 | I | T-M08-01 | FR-G-03 |

### 2c — Ingestion (W6–W8)

| Task | Description | Est | Str | Deps | Req |
| --- | --- | --- | --- | --- | --- |
| T-M07-02 | Parser registry, `ParsedDocument`, `Block` model | 1.0 | I | T-M07-01 | FR-F-01 |
| 🔴 T-M07-03 | PDF parser per spike outcome | 3.0 | I | T-M07-02 | FR-F-02 |
| T-M07-04 | DOCX / PPTX / XLSX / CSV parsers | 2.0 | I | T-M07-02 | FR-D-03 |
| T-M07-05 | HTML / Markdown / TXT / JSON parsers | 1.5 | I | T-M07-02 | FR-D-03 |
| T-M07-06 | Language detection + tokenizer selection | 1.0 | I | T-M08-02 | FR-F-04 |
| T-M07-07 | Chunkers: fixed, recursive, markdown | 2.0 | I | T-M07-02 | FR-F-05 |
| 🔴 T-M07-08 | **`parent_child` chunker** | 1.5 | I | T-M07-07 | FR-F-06 |
| T-M07-09 | Chunk rules: tables, headers, min-merge, CJK | 1.5 | I | T-M07-07 | FR-F-07 |
| 🔴 T-M07-10 | **Stage handlers: parse, chunk, index + resumability** | 2.5 | I | T-M06-07, T-M03-04 | NFR-R-05 |
| T-M07-11 | Incremental reprocessing by content hash | 1.5 | I | T-M07-10 | FR-G-06 |
| 🔴 T-M07-12 | Index writer → M05 with verification | 1.0 | I | T-M05-05, T-M07-10 | FR-G-06 |
| T-M07-13 | Error taxonomy + contributor-facing messages | 1.0 | I | T-M07-10 | FR-P-05 |
| T-M07-14 | Reference corpus (30 docs) + snapshot tests | 2.0 | I | T-M07-03..05 | FR-F-02 |
| T-M03-07 | Document registration, dedup, MIME sniffing, upload | 1.5 | K | T-M04-04 | FR-D-01/05/07 |
| T-M03-08 | Bulk upload with per-file results | 1.0 | K | T-M03-07 | FR-D-02 |
| T-M03-09 | Document state reporting API for workers | 0.5 | K | T-M03-07 | NFR-O-05 |
| T-M03-10 | Chunk storage, bulk replace, edit/split/merge | 1.5 | K | T-M03-01 | FR-F-09 |
| 🔴 T-M03-11 | **Index version lifecycle: start / activate / fail / progress** | 1.5 | K | T-M03-06 | FR-G-08 |
| T-M03-12 | KB deletion purge task | 1.0 | K | T-M03-11 | FR-C-08 |
| T-M03-13 | Icon upload + signed URL | 0.5 | K | T-M04-03 | FR-C-10 |
| T-M03-14 | Routers: KB, documents, chunks, bindings | 2.0 | K | T-M03-11 | — |
| T-M03-15 | Tests TC-M03-01..17 | 2.5 | K | T-M03-14 | — |
| T-M08-07 | Sparse: model-produced + BM25 with per-KB statistics | 2.0 | I | T-M08-02 | FR-G-05 |
| T-M08-08 | Normalization + metric consistency assertions | 0.5 | I | T-M08-04 | — |
| T-M08-09 | Retries, circuit breaker, semaphores | 1.0 | I | T-M08-04 | — |
| T-M08-10 | Metrics | 0.5 | I | T-M08-04 | — |
| T-M08-11 | Tests TC-M08-01..17 + benchmarks | 2.0 | I | T-M08-09 | — |
| T-M07-23a | Tests TC-M07-01..16 (parse, chunk, resumability) | 2.0 | I | T-M07-14 | — |

### 2d — Retrieval (W7–W9) ★

| Task | Description | Est | Str | Deps | Req |
| --- | --- | --- | --- | --- | --- |
| 🔴 T-M09-01 | Request/response schemas + validation | 1.0 | R | T-M00-03 | FR-I-02/03 |
| 🔴 T-M09-02 | `QueryPlan` resolution (precedence merge) | 1.0 | R | T-M09-01 | — |
| T-M09-03 | Auth + authorization via `M02.dataplane` | 0.5 | R | T-M02-05 | FR-B-07 |
| 🔴 T-M09-04 | KB runtime config load, cache, invalidation subscribe | 1.0 | R | T-M03-06 | — |
| T-M09-05 | Filter spec → `FilterNode` translation | 1.0 | R | T-M05-02 | FR-H-05/06 |
| 🔴 T-M09-06 | **Concurrent dense + sparse execution** | 1.5 | R | T-M05-06/07 | FR-H-01 |
| 🔴 T-M09-07 | **RRF + weighted fusion** | 1.0 | R | T-M09-06 | FR-H-02 |
| T-M09-08 | Reranker client + timeout degradation | 1.5 | R | T-M08-06 | FR-H-03/16 |
| T-M09-09 | Threshold, dedupe, MMR | 1.0 | R | T-M09-07 | FR-H-07/11/12 |
| T-M09-10 | Parent expansion (batched fetch) | 1.0 | R | T-M09-07 | FR-H-10 |
| T-M09-11 | Token budgeting | 0.5 | R | T-M09-10 | FR-H-09 |
| 🔴 T-M09-12 | **Explain mode** | 1.5 | R | T-M09-07 | FR-H-08 |
| T-M09-13 | Federated multi-KB + partial failure | 1.5 | R | T-M09-06 | FR-H-04/17 |
| T-M09-14 | Batch query endpoint | 1.0 | R | T-M09-06 | FR-H-14 |
| T-M09-15 | Discovery endpoint | 0.5 | R | T-M09-03 | FR-I-12 |
| T-M09-16 | Load monitor + shedding | 1.0 | R | T-M09-02 | NFR-P-03 |
| T-M09-17 | Per-stage metrics + tracing | 1.0 | R | T-M00-09 | NFR-O-03 |
| 🔴 T-M09-18 | **Router, error mapping, rate limit headers** | 1.0 | R | T-M09-01 | FR-I-01/04/07 |
| T-M09-20 | Golden-set fixture + quality tests | 2.0 | R | T-M09-08 | — |
| 🔴 T-M09-21 | Load-test harness + TC-M09-29/30 | 2.0 | R | T-M09-18 | NFR-P-01/02/03 |
| T-M09-22 | Tests TC-M09-01..32 | 3.0 | R | T-M09-21 | — |
| T-M16-10 | KB list + creation wizard | 2.5 | W | T-M03-14 | FR-P-04 |
| T-M16-11 | KB settings | 2.0 | W | T-M03-14 | FR-P-04 |
| 🔴 T-M16-12 | **Document list with live status + upload** | 3.0 | W | T-M03-14, T-M07-13 | FR-P-05 |
| T-M16-13 | Chunk browser + editor | 2.5 | W | T-M03-14 | FR-P-04 |
| 🔴 T-M16-14 | **Retrieval console + explain viewer** | 3.0 | W | T-M09-18 | FR-P-06 |
| T-M16-23a | E2E: J3, J4 | 1.5 | W | T-M16-14 | — |
| T-OPS-09 | Data-plane isolation CI check (no PG connection on happy path) | 0.5 | O | T-M09-18 | ADR-0002 |

**Phase 2 gate — the release gate.** See [development plan §2](01-development-plan.md).

---

## Phase 3 — Distribution (W10–W13) · 78.0 days

| Task | Description | Est | Str | Deps | Req |
| --- | --- | --- | --- | --- | --- |
| T-M10-01 | ORM + migration: `model_provider`, `model`, `secret` | 0.5 | X | T-OPS-06 | — |
| 🔒 T-M10-02 | **Envelope encryption + KEK rotation** | 2.0 | X | T-M10-01 | NFR-SEC-02 |
| T-M10-03 | `ModelRef` + provider/model CRUD | 1.5 | X | T-M10-02 | FR-K-01/03 |
| T-M10-04 | LiteLLM adapter layer (chat, embed, rerank) | 2.0 | X | T-M10-03 | FR-K-02 |
| T-M10-05 | Native adapters (Anthropic, DashScope) | 1.5 | X | T-M10-04 | FR-K-02 |
| T-M10-06 | Local server adapters (TEI, Infinity, Ollama, vLLM) | 1.0 | X | T-M10-04 | FR-K-02, NFR-C-04 |
| T-M10-07 | Semaphores, timeouts, retries, circuit breaker | 1.5 | X | T-M10-04 | FR-K-07 |
| T-M10-08 | Fallback chains | 1.0 | X | T-M10-07 | FR-K-08 |
| T-M10-09 | Model discovery + connectivity test | 1.0 | X | T-M10-04 | FR-K-05 |
| T-M10-10 | Usage accounting with buffered flush | 1.0 | X | T-M15-08 | FR-K-06 |
| T-M10-11 | Streaming + SSE + cancellation | 1.0 | X | T-M10-04 | FR-K-09 |
| T-M10-12 | Routers | 1.0 | X | T-M10-09 | — |
| T-M10-13 | Tests TC-M10-01..17 | 2.0 | X | T-M10-12 | — |
| 🔴 T-M13-01 | MCP protocol layer: JSON-RPC, streamable HTTP, initialize | 2.0 | R | T-M09-18 | FR-J-01, NFR-C-03 |
| T-M13-02 | Auth integration via `M02.dataplane` | 0.5 | R | T-M13-01 | FR-J-03 |
| 🔴 T-M13-03 | **Dynamic tool description generation + cache** | 1.5 | R | T-M13-02 | FR-J-04 |
| T-M13-04 | `search_knowledge_base` tool | 1.0 | R | T-M13-03 | FR-J-02 |
| T-M13-05 | `list_knowledge_bases` tool | 0.5 | R | T-M13-03 | FR-J-02 |
| T-M13-06 | `get_document` tool | 0.5 | R | T-M13-03 | FR-J-02 |
| T-M13-07 | Content formatting, citations, `structuredContent` | 1.0 | R | T-M13-04 | FR-J-05 |
| T-M13-08 | Error mapping | 0.5 | R | T-M13-04 | — |
| T-M13-09 | Session state in Redis | 0.5 | R | T-M13-01 | — |
| T-M13-11 | Client-compatibility testing + setup docs | 1.5 | R | T-M13-07 | NFR-C-03 |
| T-M13-12 | Tests TC-M13-01..15 | 1.5 | R | T-M13-11 | — |
| T-M09-19 | Dify compatibility endpoint | 0.5 | R | T-M09-18 | NFR-C-01 |
| T-OPS-10 | SDK generation (Python + TS) + publish pipeline | 2.0 | O | T-M09-18 | FR-I-09 |
| T-OPS-11 | LangChain + LlamaIndex retriever packages | 1.5 | O | T-OPS-10 | NFR-C-02 |
| T-OPS-12 | Nightly compatibility test matrix against real clients | 1.5 | O | T-OPS-11 | NFR-C-01..03 |
| 🔒 T-M07-16 | Crawler: frontier, scope, robots, sitemap | 3.0 | I | T-M00-07 | FR-E-01..03/10, NFR-SEC-09 |
| T-M07-17 | Playwright rendering + main-content extraction | 2.0 | I | T-M07-16 | FR-E-04/05 |
| T-M07-18 | Incremental recrawl, removal detection, report | 2.0 | I | T-M07-16 | FR-E-07/08/11 |
| T-M07-19 | Crawl scheduling + politeness | 1.0 | I | T-M07-16 | FR-E-06/09 |
| T-M07-15 | OCR: detection, RapidOCR, page caching | 2.0 | I | T-M07-03 | FR-F-03 |
| 🔒 T-M07-20 | Archive parser + safety controls | 1.5 | I | T-M07-02 | FR-D-04, NFR-SEC-08 |
| T-M07-21 | Semantic chunker | 1.5 | I | T-M08-04 | FR-F-05 |
| T-M07-23b | Tests TC-M07-17..27 (crawl, SSRF, archive, benchmark) | 1.5 | I | T-M07-20 | — |
| T-M15-06 | Quota service: definitions, checks, Redis counters, reconcile | 2.0 | F | T-M15-05 | FR-O-04 |
| T-M15-07 | Ingestion pause/resume on quota breach | 1.0 | F | T-M15-06 | FR-O-05 |
| T-M15-08 | Usage service with buffered flush | 1.0 | F | T-M15-01 | FR-O-07 |
| T-M15-09 | Usage aggregation queries | 1.0 | F | T-M15-08 | FR-O-07 |
| T-M15-11b | Quota + usage routers | 0.5 | F | T-M15-09 | — |
| T-M15-12 | Tests TC-M15-01..15 | 1.5 | F | T-M15-11b | — |
| T-M16-15 | Crawl job management screens | 2.0 | W | T-M07-19 | FR-P-04 |
| T-M16-16 | Model provider/model screens + chat test console | 2.5 | W | T-M10-12 | FR-N-09 |
| T-M16-17 | i18n zh-CN completion | 1.5 | W | T-M16-16 | FR-P-08 |
| T-M09-23 | Query rewriting hooks (prep for FR-H-15) | 0.5 | R | T-M09-02 | FR-H-15 |

---

## Phase 4 — Composition (W14–W19) · 76.0 days

| Task | Description | Est | Str | Deps | Req |
| --- | --- | --- | --- | --- | --- |
| T-M11-01 | `PipelineGraph` schema + port type system | 2.0 | X | T-M00-03 | FR-L-01/02 |
| T-M11-02 | Validator: cycles, types, ports, references | 2.0 | X | T-M11-01 | FR-L-06 |
| 🔴 T-M11-03 | **Pure executor + `ExecutionContext`** | 2.5 | X | T-M11-01 | NFR-M-02 |
| T-M11-04 | Node registry + built-in ingest nodes | 2.5 | X | T-M11-03 | FR-L-01/05 |
| T-M11-05 | Built-in retrieval nodes | 2.0 | X | T-M11-03, T-M09-07 | FR-L-02 |
| T-M11-06 | LLM nodes: classify, extract, summarize, rewrite, compress | 2.0 | X | T-M10-04 | FR-L-05, FR-H-15 |
| T-M11-07 | Control-flow nodes: branch, map, merge | 1.5 | X | T-M11-03 | FR-L-05 |
| T-M11-08 | Function node → sandbox bridge | 1.0 | X | T-M12-06 | FR-L-05 |
| T-M11-09 | MCP tool node (client side) | 1.0 | X | T-M11-04 | FR-J-06 |
| T-M11-10 | Checkpointing + resume | 1.5 | X | T-M11-03 | FR-L-08 |
| 🔴 T-M11-11 | **Deadline enforcement + node degradation** | 1.0 | X | T-M11-03 | FR-L-09 |
| T-M11-12 | Versioning, publish, pin semantics | 1.5 | X | T-M11-02 | FR-L-03/04 |
| T-M11-13 | Test run with SSE trace | 1.5 | X | T-M11-04 | FR-L-07 |
| T-M11-14 | Import / export | 0.5 | X | T-M11-12 | FR-L-11 |
| T-M11-15 | Default pipeline definitions | 1.0 | X | T-M11-05 | FR-L-04 |
| T-M11-16 | Routers | 1.5 | X | T-M11-13 | — |
| T-M11-17 | Tests TC-M11-01..19 | 2.5 | X | T-M11-16 | — |
| T-M12-01 | ORM + migration: `function`, `function_version` | 0.5 | X | T-OPS-06 | — |
| T-M12-02 | Slot signatures + Pydantic I/O schemas | 1.0 | X | T-M12-01 | FR-M-02 |
| T-M12-03 | Static validation: parse, signature, imports | 1.5 | X | T-M12-02 | FR-M-08 |
| 🔒 T-M12-04 | **Sandbox image: base, allowlisted deps, non-root** | 1.5 | X | T-M12-02 | FR-M-05/08 |
| 🔒🔴 T-M12-05 | **`sandbox-runner`: dispatcher, runsc spawn, limits** | 3.0 | X | T-M12-04 | FR-M-03/04/05 |
| T-M12-06 | `SandboxClient` protocol, retries, health | 1.0 | X | T-M12-05 | — |
| T-M12-07 | Output validation against slot schemas | 0.5 | X | T-M12-02 | — |
| T-M12-08 | Error taxonomy + traceback sanitization | 1.0 | X | T-M12-06 | FR-M-09 |
| T-M12-09 | Versioning, publish, pin semantics | 1.0 | X | T-M12-03 | FR-M-06 |
| T-M12-10 | Test endpoint with resource reporting | 1.0 | X | T-M12-06 | FR-M-07 |
| 🔒 T-M12-11 | Egress allowlist + proxy + approval flow | 2.0 | X | T-M12-05 | FR-M-04 |
| T-M12-12 | Routers | 1.0 | X | T-M12-10 | — |
| 🔒🔴 T-M12-13 | **Escape-attempt suite TC-M12-04..15 + security review gate** | 3.0 | X | T-M12-11 | NFR-SEC-06 |
| T-M12-14 | Remaining tests | 1.5 | X | T-M12-13 | — |
| T-M07-22 | Enrichers: summary, keywords, questions | 2.0 | I | T-M10-04 | FR-F-10 |
| T-M16-18 | **Pipeline editor (Vue Flow)** | 5.0 | W | T-M11-16 | FR-P-07, FR-L-10 |
| T-M16-19 | Function editor (Monaco) + test panel | 3.0 | W | T-M12-12 | FR-M-07 |

**Phase 4 gate:** sandbox escape suite 100% green **and** security review signed off, or the
Function Library does not ship.

---

## Phase 5 — Quality and scale (W20–W26) · 58.0 days

| Task | Description | Est | Str | Deps | Req |
| --- | --- | --- | --- | --- | --- |
| T-M14-01 | ORM + migration: golden sets, eval runs, results | 0.5 | Q | T-OPS-06 | — |
| T-M14-02 | Metric implementations + reference validation | 2.0 | Q | T-M14-01 | FR-N-03 |
| T-M14-03 | Golden set CRUD, import, review gate | 1.5 | Q | T-M14-01 | FR-N-01/02 |
| T-M14-04 | LLM-assisted item generation | 1.5 | Q | T-M10-04 | FR-N-02 |
| T-M14-05 | Run orchestration (query-time path) | 1.5 | Q | T-M09-18 | FR-N-05 |
| T-M14-06 | **Temp index build + guaranteed cleanup** | 2.0 | Q | T-M03-11 | FR-N-06 |
| T-M14-07 | Comparison + paired bootstrap significance | 1.5 | Q | T-M14-05 | FR-N-04 |
| T-M14-08 | Export (CSV / JSONL) | 0.5 | Q | T-M14-07 | FR-N-07 |
| T-M14-09 | LLM-as-judge mode | 1.5 | Q | T-M14-05 | FR-N-08 |
| T-M14-10 | Routers | 1.0 | Q | T-M14-08 | — |
| T-M14-11 | Tests TC-M14-01..13 | 1.5 | Q | T-M14-10 | — |
| T-M05-10 | Qdrant driver: collections, named vectors, tenant payload index | 2.0 | K | T-M05-04 | FR-G-07, NFR-S-07 |
| T-M05-11 | Qdrant: dense + sparse search, filter translation | 1.5 | K | T-M05-10 | FR-G-05 |
| T-M05-12 | Qdrant: quantization + layout promotion | 1.0 | K | T-M05-10 | FR-G-10 |
| T-M05-13 | Benchmark harness + TC-M05-17..20 | 1.5 | K | T-M05-12 | NFR-S-03 |
| T-M05-14b | Conformance suite green on Qdrant | 0.5 | K | T-M05-12 | — |
| T-M08-12 | Bring-your-own-vectors path | 0.5 | I | T-M08-05 | FR-G-11 |
| T-M15-10 | Notification channels + delivery task | 2.0 | F | T-M06-07 | FR-O-08 |
| T-M01-12 | OIDC / SSO (stretch) | 3.0 | A | T-M01-10 | FR-A-14 |
| T-OPS-13 | Helm chart: deployments, services, ingress, secrets | 3.0 | O | T-OPS-03 | NFR-D-05 |
| T-OPS-14 | HPA on `api-data`; KEDA Postgres scaler on queue depth | 1.5 | O | T-OPS-13 | NFR-S-06 |
| T-OPS-15 | Grafana dashboards (provisioned JSON) + alert rules | 2.0 | O | T-M09-17 | NFR-O-06 |
| T-OPS-16 | Backup scripts + **rehearsed restore drill** | 2.0 | O | T-OPS-13 | NFR-R-08 |
| T-OPS-17 | Sizing presets small/medium/large, documented and measured | 1.5 | O | T-M09-21 | NFR-D-06 |
| T-OPS-18 | 10M-vector scale demonstration | 2.0 | O | T-M05-13 | NFR-S-03 |
| T-OPS-19 | Rolling-upgrade verification (migration forward-compat) | 1.0 | O | T-OPS-13 | NFR-R-09 |
| T-OPS-20 | Public documentation site | 3.0 | O | — | — |
| T-M16-20 | Evaluation dashboards + comparison charts | 3.0 | W | T-M14-10 | FR-N-04 |
| T-M16-21 | Usage and quota screens | 1.5 | W | T-M15-09 | FR-O-07 |
| T-M16-22 | Accessibility pass | 1.5 | W | T-M16-20 | FR-P-10 |
| T-M16-23b | E2E: J7, J8 | 1.5 | W | T-M16-20 | — |
| T-M13-10 | MCP resources (P2) | 1.5 | R | T-M13-07 | FR-J-07 |
| T-OPS-21 | OpenAI-compatible embeddings endpoint (P2) | 1.0 | O | T-M10-04 | — |

---

## Summary

| Phase | Weeks | Tasks | Days |
| --- | --- | --- | --- |
| 0 Skeleton | W1–W2 | 32 | 26.0 |
| 1 Identity | W3–W4 | 41 | 42.0 |
| 2 Knowledge core ★ | W5–W9 | 71 | 108.0 |
| 3 Distribution | W10–W13 | 47 | 78.0 |
| 4 Composition | W14–W19 | 34 | 76.0 |
| 5 Quality & scale | W20–W26 | 36 | 58.0 |
| **Total** | **26** | **261** | **388.0** |

### Critical-path tasks (🔴) — 34 tasks

Delay in any of these delays the product. They receive priority for the strongest engineers,
the earliest scheduling, and the closest review.

### Security-gated tasks (🔒) — 8 tasks

`T-M00-07`, `T-M10-02`, `T-M07-16`, `T-M07-20`, `T-M12-04`, `T-M12-05`, `T-M12-11`, `T-M12-13`.

These require a security review before merge, and their test suites are release gates. See
[`../05-quality/03-security-baseline.md`](../05-quality/03-security-baseline.md).
