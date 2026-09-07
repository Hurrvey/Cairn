# Requirements Catalog

**Document:** `00-overview/04-requirements.md`
**Status:** Normative baseline
**Date:** 2026-08-28

Every requirement has a stable ID. **Every WBS task cites the IDs it satisfies; every module
spec lists the IDs it owns; every acceptance test cites an ID.** IDs are never reused or
renumbered — deprecated requirements are marked `WITHDRAWN`, not deleted.

**Priority:** `P0` = required for the phase to exit · `P1` = required for GA · `P2` = desirable
**Phase:** the phase in which the requirement must be satisfied.

---

# Functional Requirements

## FR-A — Authentication & Accounts

| ID | Requirement | Pri | Phase | Module |
| --- | --- | --- | --- | --- |
| FR-A-01 | The system MUST require authentication before any functional endpoint may be used. Only `/healthz`, `/readyz`, `/v1/meta`, and login are unauthenticated. | P0 | 0 | M01 |
| FR-A-02 | On first startup with no existing administrator, the system MUST create exactly one account with username `admin` and role `admin`. Creation MUST be idempotent and race-safe across concurrent API replicas. | P0 | 0 | M01 |
| FR-A-03 | The system MUST generate a cryptographically strong random password (≥ 128 bits entropy) for the bootstrap admin and print it once to stdout in a visually distinct banner. It MUST NOT be logged again, stored in plaintext, or retrievable via API. An env override `CAIRN_INITIAL_ADMIN_PASSWORD` MUST be supported for automated deployment; when set, no password is printed. | P0 | 0 | M01 |
| FR-A-04 | When a principal with `must_change_password = true` authenticates successfully, the system MUST NOT issue a session. It MUST return `status: "password_change_required"` with a scope-limited `change_token` (TTL ≤ 10 min, scope `credential:bootstrap` only). | P0 | 0 | M01 |
| FR-A-05 | The forced credential change MUST be enforced server-side by middleware rejecting all requests from such a principal with `403 PASSWORD_CHANGE_REQUIRED`, except a fixed allowlist: complete-setup, logout, and `/v1/me`. UI enforcement alone is insufficient. | P0 | 0 | M01 |
| FR-A-06 | The `must_change_password` state MUST persist in durable storage, surviving browser close, session loss, container restart, and redeploy. It MUST clear only on a successful credential update. | P0 | 0 | M01 |
| FR-A-07 | During the forced change the principal MAY also change its username, atomically in the same transaction as the password change. Username uniqueness MUST be enforced. | P0 | 0 | M01 |
| FR-A-08 | Passwords MUST be hashed with Argon2id (m ≥ 64 MiB, t ≥ 3, p = 4). Plaintext MUST never be persisted or logged. | P0 | 0 | M01 |
| FR-A-09 | Password policy MUST be enforced server-side: minimum 12 characters, at least 3 of 4 character classes, not equal to the previous password, not present in the bundled common-password list. Policy MUST be returned to the client so the UI can display it. | P0 | 1 | M01 |
| FR-A-10 | The system MUST support login, logout, session refresh, and "who am I" (`/v1/me`). Web sessions MUST be opaque server-side tokens in `HttpOnly; Secure; SameSite=Lax` cookies, revocable immediately. | P0 | 1 | M01 |
| FR-A-11 | The system MUST rate-limit authentication attempts per account and per source IP, with exponential lockout. Responses MUST NOT reveal whether a username exists (identical message and comparable timing). | P0 | 1 | M01 |
| FR-A-12 | An administrator MUST be able to force a credential change on any user by setting `must_change_password`, reusing the identical flow. | P1 | 1 | M01 |
| FR-A-13 | `credential_version` MUST increment on any credential change, invalidating all existing sessions and change tokens for that principal. | P0 | 1 | M01 |
| FR-A-14 | The system SHOULD support OIDC/SAML single sign-on as an alternative to local accounts. | P2 | 5 | M01 |

## FR-B — Authorization & Access Control

| ID | Requirement | Pri | Phase | Module |
| --- | --- | --- | --- | --- |
| FR-B-01 | The system MUST support exactly two roles: `admin` and `user`. Roles govern platform-level capability only. | P0 | 1 | M02 |
| FR-B-02 | Administrators MUST be able to create, read, update, delete, enable, disable, and list regular user accounts. | P0 | 1 | M01 |
| FR-B-03 | Administrators MUST be able to create, modify, revoke, and inspect permission grants held by regular users. | P0 | 1 | M02 |
| FR-B-04 | Permissions MUST be expressed as `<resource>:<verb>` and scoped to a specific resource instance or to the workspace. Defined set: `kb:read`, `kb:query`, `kb:write`, `kb:manage`, `kb:create`, `pipeline:read`, `pipeline:edit`, `pipeline:run`, `function:use`, `function:edit`, `model:use`, `model:manage`, `eval:read`, `eval:run`. | P0 | 1 | M02 |
| FR-B-05 | Platform capabilities MUST be role-gated and non-grantable to `user` accounts: `platform:users`, `platform:models`, `platform:storage`, `platform:settings`, `platform:audit`. | P0 | 1 | M02 |
| FR-B-06 | Effective permissions MUST be the union of a subject's grants. Grants MAY carry an expiry; expired grants MUST NOT be considered. | P0 | 1 | M02 |
| FR-B-07 | `kb:query` MUST be independently grantable from `kb:read`, so a principal can retrieve from a KB without being able to enumerate or export its documents. | P0 | 1 | M02 |
| FR-B-08 | API keys MUST be first-class principals with their own scopes, KB bindings, rate limit, optional CIDR allowlist, and expiry. Effective permission MUST be `key_grants ∩ owner_grants`, computed at authentication time. | P0 | 1 | M02 |
| FR-B-09 | The workspace setting `admin_content_access` MUST support `always`, `on_grant`, and `break_glass` (default `break_glass`). Under `break_glass` an administrator manages KBs but MUST NOT read chunk content without a time-boxed self-grant. | P1 | 1 | M02 |
| FR-B-10 | Break-glass grants MUST be time-boxed (default 60 min), require a stated reason, be recorded in the audit log, and notify the KB owner. | P1 | 1 | M02, M15 |
| FR-B-11 | Permission resolution on the data plane MUST be O(1) — a single cache lookup, no database join on the happy path. | P0 | 2 | M02 |
| FR-B-12 | Permission cache invalidation MUST be by version bump, never by key enumeration, and MUST take effect within 60 s of a grant change. | P0 | 2 | M02 |
| FR-B-13 | API keys MUST be displayed in plaintext exactly once at creation. Storage MUST be SHA-256 of the key; the plaintext MUST NOT be recoverable. | P0 | 1 | M02 |
| FR-B-14 | Administrators MUST be able to list, inspect usage of, and revoke any API key in the workspace. | P1 | 1 | M02 |

## FR-C — Knowledge Base Management

| ID | Requirement | Pri | Phase | Module |
| --- | --- | --- | --- | --- |
| FR-C-01 | Users with `kb:create` MUST be able to create knowledge bases. | P0 | 2 | M03 |
| FR-C-02 | A KB MUST support: name, slug, description, icon/cover image, and free-form metadata. | P0 | 2 | M03 |
| FR-C-03 | A KB MUST be configured with a vector storage backend and an object storage backend, selected from admin-registered storage bindings. | P0 | 2 | M03 |
| FR-C-04 | A KB MUST be configured with an embedding model. This MUST be immutable after the first successful index build; changing it requires an explicit reindex operation. | P0 | 2 | M03 |
| FR-C-05 | A KB MUST carry a chunking configuration: strategy, target size, overlap, parent window size, separator set. | P0 | 2 | M03 |
| FR-C-06 | A KB MUST carry default retrieval configuration: search mode, fusion method, weights, rerank model, top_k, score threshold. Callers MAY override per request. | P0 | 2 | M03 |
| FR-C-07 | KB operations MUST include create, read, update, delete (soft then hard), list with filters, and duplicate-as-template. | P0 | 2 | M03 |
| FR-C-08 | Deleting a KB MUST cascade to documents, chunks, vectors, and object-store artifacts, executed asynchronously with progress reporting. | P0 | 2 | M03 |
| FR-C-09 | A KB MUST expose live counters: document count, chunk count, bytes stored, vector count, last indexed at. | P1 | 2 | M03 |
| FR-C-10 | Cover images MUST be validated (MIME sniffing, ≤ 2 MB, image types only), stored in the object store, and served via time-limited signed URLs. | P1 | 2 | M03, M04 |
| FR-C-11 | A KB MUST support an ownership transfer operation, permitted to `kb:manage` holders and administrators. | P2 | 3 | M03 |

## FR-D — Document Upload

| ID | Requirement | Pri | Phase | Module |
| --- | --- | --- | --- | --- |
| FR-D-01 | Users with `kb:write` MUST be able to upload single files. | P0 | 2 | M03, M07 |
| FR-D-02 | Bulk upload of multiple files in one operation MUST be supported, with per-file result reporting. | P0 | 2 | M03 |
| FR-D-03 | Supported input formats MUST include: PDF, DOCX, PPTX, XLSX, TXT, Markdown, HTML, CSV, JSON, EPUB, and images (PNG/JPG/TIFF via OCR). | P0 | 2 | M07 |
| FR-D-04 | Archive upload (ZIP) MUST be supported with safe extraction: path-traversal prevention, decompression-ratio limits, entry count limits. | P1 | 3 | M07 |
| FR-D-05 | Uploads MUST be validated by content sniffing, not by file extension. Declared type mismatches MUST be rejected. | P0 | 2 | M03 |
| FR-D-06 | A per-workspace maximum upload size MUST be enforced (default 200 MB per file). | P0 | 2 | M03, M15 |
| FR-D-07 | Re-uploading content with an identical `content_hash` MUST be a no-op returning the existing document, not a duplicate. | P0 | 2 | M07 |
| FR-D-08 | Uploading modified content for an existing document MUST create a new revision and reprocess only changed chunks. | P1 | 3 | M07 |
| FR-D-09 | Resumable/chunked upload MUST be supported for files over 50 MB. | P2 | 3 | M03 |
| FR-D-10 | Users MUST be able to attach arbitrary key–value metadata at upload time; it MUST be inherited by all derived chunks and be filterable at retrieval. | P0 | 2 | M07 |

## FR-E — Web Crawling

| ID | Requirement | Pri | Phase | Module |
| --- | --- | --- | --- | --- |
| FR-E-01 | Users MUST be able to define a crawl job with seed URLs, maximum depth, and maximum page count. | P0 | 3 | M07 |
| FR-E-02 | Crawl scope MUST be controllable by include and exclude URL regex patterns and by same-domain restriction. | P0 | 3 | M07 |
| FR-E-03 | The crawler MUST respect `robots.txt` and report pages skipped for that reason. A per-workspace admin-only override MUST exist. | P0 | 3 | M07 |
| FR-E-04 | The crawler MUST support JavaScript-rendered pages via a headless browser. | P0 | 3 | M07 |
| FR-E-05 | The crawler MUST extract main content, discarding navigation, headers, footers, and boilerplate. | P0 | 3 | M07 |
| FR-E-06 | Crawl jobs MUST support scheduling (cron expression) for recurring recrawl. | P0 | 3 | M07 |
| FR-E-07 | Recrawl MUST be incremental: unchanged pages (by `content_hash`) MUST NOT be re-parsed or re-embedded. | P0 | 3 | M07 |
| FR-E-08 | Pages removed from the source site MUST be detectable and optionally removed from the KB. | P1 | 3 | M07 |
| FR-E-09 | Per-domain politeness MUST be enforced: configurable delay, concurrency cap, honouring `Retry-After`. | P0 | 3 | M07 |
| FR-E-10 | Sitemap discovery (`sitemap.xml`, `robots.txt` Sitemap directive) MUST be supported as a seeding mechanism. | P1 | 3 | M07 |
| FR-E-11 | Each crawl run MUST produce a report: pages fetched, added, updated, unchanged, skipped (with reason), failed (with reason). | P0 | 3 | M07 |

## FR-F — Parsing, Chunking, Enrichment

| ID | Requirement | Pri | Phase | Module |
| --- | --- | --- | --- | --- |
| FR-F-01 | Parsing MUST produce normalized Markdown plus structural metadata (headings, page numbers, table markers, reading order). | P0 | 2 | M07 |
| FR-F-02 | PDF parsing MUST preserve table structure and handle multi-column layouts with correct reading order. | P0 | 2 | M07 |
| FR-F-03 | OCR MUST be applied to scanned PDFs and images, with automatic detection of whether OCR is needed. | P0 | 3 | M07 |
| FR-F-04 | Parsing MUST support English and Chinese content, including mixed-language documents. | P0 | 2 | M07 |
| FR-F-05 | Chunking strategies MUST include: fixed-size with overlap, recursive separator, Markdown-heading-aware, semantic (embedding-boundary), and parent–child. | P0 | 2 | M07 |
| FR-F-06 | Parent–child chunking MUST be the default: small chunks are embedded, larger parent windows are returned when `expand_parent` is enabled. | P0 | 2 | M07 |
| FR-F-07 | Every chunk MUST retain provenance metadata: source document, ordinal, heading path, page number, source URL where applicable. | P0 | 2 | M07 |
| FR-F-08 | Users MUST be able to preview chunking results before committing a configuration change. | P1 | 3 | M07, M16 |
| FR-F-09 | Users MUST be able to manually edit, split, merge, or delete individual chunks; edits MUST survive reindex of unchanged documents. | P1 | 4 | M03 |
| FR-F-10 | Optional LLM enrichment MUST be available per KB: chunk summary, keyword extraction, question generation, classification. | P2 | 4 | M07, M11 |
| FR-F-11 | Token counting MUST use the tokenizer matching the KB's embedding model. | P0 | 2 | M08 |

## FR-G — Vectorization & Indexing

| ID | Requirement | Pri | Phase | Module |
| --- | --- | --- | --- | --- |
| FR-G-01 | The system MUST generate embeddings using the KB's configured model, batched for throughput. | P0 | 2 | M08 |
| FR-G-02 | Embedding MUST be cached by `sha256(model_id + normalized_text)` to avoid recomputation. | P0 | 2 | M08 |
| FR-G-03 | Embedding providers MUST include hosted APIs (OpenAI, Qwen/DashScope, Gemini, Cohere, Jina) and self-hosted servers (TEI, Infinity, Ollama). | P0 | 2 | M08, M10 |
| FR-G-04 | Vector dimension MUST be validated against the KB configuration on every write; mismatches MUST be rejected loudly, never silently truncated or padded. | P0 | 2 | M05 |
| FR-G-05 | Sparse representations for lexical search MUST be generated and stored alongside dense vectors. | P0 | 2 | M05, M09 |
| FR-G-06 | Indexing MUST be incremental: only new or changed chunks are embedded and upserted. | P0 | 2 | M07 |
| FR-G-07 | Vector storage MUST be pluggable behind one interface, with pgvector and Qdrant implementations. | P0 | 2/5 | M05 |
| FR-G-08 | Reindexing MUST be blue/green: a new `index_version` builds while the active version continues serving; activation is an atomic switch; the old version is dropped after a retention window. | P0 | 2 | M03, M05 |
| FR-G-09 | Index build progress MUST be observable: chunks embedded, chunks indexed, estimated completion. | P1 | 2 | M06, M16 |
| FR-G-10 | Vector quantization (scalar int8, binary with rescoring) MUST be configurable per KB where the backend supports it. | P1 | 5 | M05 |
| FR-G-11 | The system MUST support a "bring your own vectors" path where an embedding is supplied rather than computed. | P2 | 5 | M08 |

## FR-H — Retrieval

| ID | Requirement | Pri | Phase | Module |
| --- | --- | --- | --- | --- |
| FR-H-01 | The system MUST support dense vector search, sparse lexical search, and hybrid search. | P0 | 2 | M09 |
| FR-H-02 | Hybrid search MUST run dense and sparse concurrently and fuse them; Reciprocal Rank Fusion MUST be the default, with weighted-score fusion available. | P0 | 2 | M09 |
| FR-H-03 | Cross-encoder reranking MUST be supported and configurable per request and per KB. | P0 | 2 | M09 |
| FR-H-04 | Retrieval MUST support federated search across multiple KBs in one request, with per-target weights, provided the caller holds `kb:query` on all targets. | P0 | 2 | M09 |
| FR-H-05 | Metadata filtering MUST be supported with operators `$eq`, `$ne`, `$in`, `$nin`, `$gt`, `$gte`, `$lt`, `$lte`, `$exists`, and boolean `$and`/`$or`/`$not`. | P0 | 2 | M09 |
| FR-H-06 | Filtering by document ID set and by time range MUST be supported. | P0 | 2 | M09 |
| FR-H-07 | A score threshold MUST be supported; results below it are excluded. Returning zero results MUST be a valid, non-error outcome. | P0 | 2 | M09 |
| FR-H-08 | An `explain` mode MUST return per-stage candidate lists and scores for debugging. | P0 | 2 | M09 |
| FR-H-09 | `max_context_tokens` MUST be honoured: the system trims results to the budget and sets `truncated_to_token_budget`. | P0 | 2 | M09 |
| FR-H-10 | Parent expansion MUST be supported: return the parent window of a matched child chunk. | P0 | 2 | M09 |
| FR-H-11 | Result deduplication MUST be supported at chunk level and document level. | P1 | 2 | M09 |
| FR-H-12 | Maximal Marginal Relevance diversification MUST be available. | P1 | 3 | M09 |
| FR-H-13 | Callers MUST be able to supply `query_vector` directly, bypassing embedding. | P1 | 2 | M09 |
| FR-H-14 | Batch retrieval (N queries in one request) MUST be supported with a shared embedding batch. | P1 | 3 | M09 |
| FR-H-15 | Query rewriting/expansion (multi-query, HyDE) SHOULD be available as an optional retrieval-pipeline stage. | P2 | 4 | M11 |
| FR-H-16 | Any degradation (rerank skipped under load, partial target failure) MUST be reported in the response `degraded` field, never silently applied. | P0 | 2 | M09 |
| FR-H-17 | If one target KB in a federated query fails, the request MUST return partial results with the failure named, unless `strict: true` is set. | P1 | 3 | M09 |

## FR-I — Public API & SDKs

| ID | Requirement | Pri | Phase | Module |
| --- | --- | --- | --- | --- |
| FR-I-01 | The system MUST expose a versioned HTTP API at `/v1`, authenticated by bearer API key. | P0 | 2 | M09 |
| FR-I-02 | The retrieval request MUST accept: query content, target KB(s), and retrieval parameters, exactly as specified in `03-api/02-data-plane-api.md`. | P0 | 2 | M09 |
| FR-I-03 | The retrieval response MUST be a standardized envelope containing `request_id`, `results[]` with content, scores, metadata and source citation, and `usage` with per-stage latency. | P0 | 2 | M09 |
| FR-I-04 | Errors MUST follow RFC 9457 `application/problem+json` with a stable machine-readable `code`. | P0 | 2 | M00 |
| FR-I-05 | The API MUST evolve additively within `/v1`; breaking changes require `/v2` and a `Sunset` header with ≥ 6 months notice. | P0 | 2 | — |
| FR-I-06 | OpenAPI 3.1 MUST be generated and published at `/v1/openapi.json`. | P0 | 2 | M00 |
| FR-I-07 | Rate limit state MUST be communicated via RFC 9331 `RateLimit-*` headers on every response. | P0 | 2 | M02 |
| FR-I-08 | `X-Request-Id` MUST be returned on every response and accepted on request for correlation. | P0 | 0 | M00 |
| FR-I-09 | Official Python and TypeScript SDKs MUST be generated from the OpenAPI spec and published. | P1 | 3 | — |
| FR-I-10 | `Idempotency-Key` MUST be honoured on all resource-creating POST endpoints. | P1 | 2 | M00 |
| FR-I-11 | List endpoints MUST use cursor pagination; offset pagination MUST NOT be used. | P0 | 1 | M00 |
| FR-I-12 | A discovery endpoint MUST let a key holder list the KBs it can access. | P1 | 2 | M09 |

## FR-J — MCP Integration

| ID | Requirement | Pri | Phase | Module |
| --- | --- | --- | --- | --- |
| FR-J-01 | Cairn MUST expose an MCP server over streamable HTTP at `/mcp`. | P0 | 3 | M13 |
| FR-J-02 | The MCP server MUST expose tools `search_knowledge_base`, `list_knowledge_bases`, and `get_document`. | P0 | 3 | M13 |
| FR-J-03 | MCP authentication MUST use the same API key mechanism and enforce identical permissions. | P0 | 3 | M13 |
| FR-J-04 | MCP tool descriptions MUST be dynamically generated to include the KBs the calling key can access, with their descriptions, so the client model can route correctly. | P0 | 3 | M13 |
| FR-J-05 | MCP responses MUST include citations in a form clients can render. | P0 | 3 | M13 |
| FR-J-06 | Cairn SHOULD act as an MCP client so external MCP tools can be invoked from enrichment pipeline nodes. | P2 | 4 | M11 |
| FR-J-07 | MCP resources SHOULD be exposed so clients can browse KBs as resource trees. | P2 | 4 | M13 |

## FR-K — Model Gateway

| ID | Requirement | Pri | Phase | Module |
| --- | --- | --- | --- | --- |
| FR-K-01 | Administrators MUST be able to register model providers with credentials, base URL, and model list. | P0 | 3 | M10 |
| FR-K-02 | Supported provider families MUST include OpenAI, Anthropic (Claude), Google (Gemini), Alibaba (Qwen/DashScope), DeepSeek, Meta Llama via compatible hosts, Ollama, vLLM, and any OpenAI-compatible endpoint. | P0 | 3 | M10 |
| FR-K-03 | Model capability MUST be typed: `chat`, `embedding`, `rerank`. Only capability-appropriate models are selectable in each context. | P0 | 3 | M10 |
| FR-K-04 | Provider credentials MUST be encrypted at rest with envelope encryption and MUST NOT be readable through any API. | P0 | 3 | M10 |
| FR-K-05 | A connectivity test MUST be available per model, reporting latency and any error. | P0 | 3 | M10 |
| FR-K-06 | Token usage and estimated cost MUST be recorded per call, attributed to workspace, KB, and principal. | P1 | 3 | M10, M15 |
| FR-K-07 | Per-provider concurrency limits, timeouts, retries with backoff, and circuit breaking MUST be enforced. | P0 | 3 | M10 |
| FR-K-08 | Fallback chains (primary → secondary model on failure) MUST be configurable. | P2 | 5 | M10 |
| FR-K-09 | Streaming responses MUST be supported for chat models. | P1 | 3 | M10 |

## FR-L — Pipelines (Agent Workflow)

| ID | Requirement | Pri | Phase | Module |
| --- | --- | --- | --- | --- |
| FR-L-01 | Users MUST be able to define ingestion pipelines as DAGs over stages: fetch, parse, classify, extract, enrich, chunk, embed, index. | P0 | 4 | M11 |
| FR-L-02 | Users MUST be able to define retrieval pipelines as DAGs over stages: rewrite, route, search, fuse, filter, rerank, compress, assemble. | P0 | 4 | M11 |
| FR-L-03 | Pipelines MUST be versioned and immutable once published; editing produces a new version. | P0 | 4 | M11 |
| FR-L-04 | A pipeline MUST be attachable to a KB as its ingestion or retrieval pipeline. Absent one, built-in defaults apply. | P0 | 4 | M11, M03 |
| FR-L-05 | Node types MUST include: built-in stage nodes, LLM nodes, conditional branch, map-over-collection, and Function nodes. | P0 | 4 | M11 |
| FR-L-06 | Pipelines MUST be validated before publication: DAG acyclicity, type compatibility between ports, required inputs bound. | P0 | 4 | M11 |
| FR-L-07 | A pipeline MUST be testable against a sample document or query with per-node input/output inspection. | P0 | 4 | M11, M16 |
| FR-L-08 | Pipeline execution MUST be checkpointed so a worker failure resumes at the last completed node rather than restarting. | P1 | 4 | M11 |
| FR-L-09 | Retrieval pipeline execution MUST stay within the data-plane latency budget; nodes exceeding a per-node timeout MUST be skipped with `degraded` reported. | P0 | 4 | M11, M09 |
| FR-L-10 | The visual editor MUST support drag-and-drop node placement, edge connection, and inline node configuration. | P0 | 4 | M16 |
| FR-L-11 | Pipelines MUST be exportable and importable as JSON for version control and sharing. | P1 | 4 | M11 |

## FR-M — Function Library

| ID | Requirement | Pri | Phase | Module |
| --- | --- | --- | --- | --- |
| FR-M-01 | Users with `function:edit` MUST be able to author Python functions conforming to a typed slot signature. | P0 | 4 | M12 |
| FR-M-02 | Slot signatures MUST include at minimum: `parse`, `chunk`, `enrich`, `filter`, `rerank`. Each has a fixed input/output contract. | P0 | 4 | M12 |
| FR-M-03 | Function execution MUST occur in an isolated sandbox providing a genuine kernel-level boundary (gVisor or microVM). Interpreter-level restriction alone MUST NOT be used. | P0 | 4 | M12 |
| FR-M-04 | Sandboxes MUST default to no network egress; per-function allowlisted egress MAY be granted by an administrator. | P0 | 4 | M12 |
| FR-M-05 | Sandboxes MUST enforce CPU, memory, PID, and wall-clock limits, run as non-root with a read-only root filesystem and all capabilities dropped, and be destroyed after each execution. | P0 | 4 | M12 |
| FR-M-06 | Functions MUST be versioned; pipelines pin an exact version. | P0 | 4 | M12 |
| FR-M-07 | A function MUST be testable in the UI against sample input with captured stdout, return value, duration, and resource usage. | P0 | 4 | M12, M16 |
| FR-M-08 | A declared dependency allowlist MUST be enforced; arbitrary package installation at runtime MUST NOT be possible. | P0 | 4 | M12 |
| FR-M-09 | Function failures MUST be contained: the owning task fails with a clear error and does not affect other tasks or the host. | P0 | 4 | M12 |

## FR-N — Evaluation & Model Testing

| ID | Requirement | Pri | Phase | Module |
| --- | --- | --- | --- | --- |
| FR-N-01 | Users MUST be able to create golden sets of `(query, relevant_chunk_ids[], notes)`. | P0 | 5 | M14 |
| FR-N-02 | Golden sets MUST be importable from CSV/JSONL and generatable semi-automatically via an LLM from KB content, subject to human review. | P1 | 5 | M14 |
| FR-N-03 | Evaluation runs MUST compute at minimum: Recall@k, MRR, nDCG@k, Precision@k, and mean latency. | P0 | 5 | M14 |
| FR-N-04 | Two configurations MUST be comparable side by side on the same golden set, with per-query deltas and significance indication. | P0 | 5 | M14 |
| FR-N-05 | Configurations under test MUST include embedding model, chunking strategy, search mode, fusion weights, reranker, and top_k. | P0 | 5 | M14 |
| FR-N-06 | Evaluations that require reindexing MUST use a temporary `index_version` and clean it up afterwards. | P0 | 5 | M14, M05 |
| FR-N-07 | Run history MUST be retained and results exportable. | P1 | 5 | M14 |
| FR-N-08 | An LLM-as-judge answer-quality mode SHOULD be available for end-to-end assessment. | P2 | 5 | M14 |
| FR-N-09 | Chat models MUST be testable interactively in a console to verify provider connectivity and behaviour. | P1 | 3 | M16, M10 |

## FR-O — Platform Services

| ID | Requirement | Pri | Phase | Module |
| --- | --- | --- | --- | --- |
| FR-O-01 | All administrative actions, credential changes, permission changes, and break-glass events MUST be recorded in an append-only audit log with actor, action, resource, IP, user agent, before/after state, and timestamp. | P0 | 1 | M15 |
| FR-O-02 | The audit log MUST be queryable by actor, action, resource, and time range, and exportable as JSONL. | P0 | 1 | M15 |
| FR-O-03 | Audit log retention MUST be configurable with a documented minimum default of 365 days. | P1 | 1 | M15 |
| FR-O-04 | Per-workspace quotas MUST be enforceable: document count, storage bytes, vector count, monthly embedding tokens, retrieval requests per minute. | P1 | 3 | M15 |
| FR-O-05 | Quota breach MUST produce a clear, machine-readable error and MUST NOT corrupt in-flight work. | P1 | 3 | M15 |
| FR-O-06 | System settings MUST be manageable by administrators through the UI, with hot reload where safe. | P1 | 1 | M15 |
| FR-O-07 | Usage statistics (requests, latency, tokens, cost) MUST be viewable per workspace, KB, and API key. | P1 | 3 | M15 |
| FR-O-08 | Notifications (ingestion failure, quota threshold, break-glass) MUST be deliverable by email and webhook. | P2 | 5 | M15 |

## FR-P — Web User Interface

| ID | Requirement | Pri | Phase | Module |
| --- | --- | --- | --- | --- |
| FR-P-01 | The system MUST provide a browser-accessible web interface as the primary human surface. | P0 | 1 | M16 |
| FR-P-02 | The UI MUST implement the forced credential-change dialog as non-dismissable, blocking all navigation until completed. | P0 | 0 | M16 |
| FR-P-03 | Administrator screens MUST cover user management, permission management, API keys, model providers, storage bindings, settings, audit log, and usage. | P0 | 1 | M16 |
| FR-P-04 | KB screens MUST cover creation wizard, settings, document list with per-document state and stage, chunk browser, and retrieval test console. | P0 | 2 | M16 |
| FR-P-05 | The document list MUST show live ingestion progress with stage and actionable error messages. | P0 | 2 | M16 |
| FR-P-06 | The retrieval test console MUST expose all retrieval parameters and render `explain` output. | P0 | 2 | M16 |
| FR-P-07 | The pipeline editor MUST provide a drag-and-drop DAG canvas. | P0 | 4 | M16 |
| FR-P-08 | The UI MUST support English and Chinese via i18n. | P1 | 3 | M16 |
| FR-P-09 | The UI MUST be responsive at ≥ 1280 px and usable at 1024 px. | P1 | 2 | M16 |
| FR-P-10 | The UI MUST meet WCAG 2.1 AA for keyboard navigation and contrast on primary flows. | P2 | 5 | M16 |

---

# Non-Functional Requirements

## NFR-P — Performance

| ID | Requirement | Pri | Phase |
| --- | --- | --- | --- |
| NFR-P-01 | Retrieval p50 < 60 ms, p95 < 150 ms, p99 < 300 ms for hybrid search without rerank, `top_k ≤ 10`, KB ≤ 1M vectors, measured server-side excluding network. | P0 | 2 |
| NFR-P-02 | Retrieval p95 < 400 ms with local cross-encoder rerank over 100 candidates. | P0 | 2 |
| NFR-P-03 | A single `api-data` replica (4 vCPU) MUST sustain ≥ 200 RPS at NFR-P-01 latencies. | P0 | 5 |
| NFR-P-04 | Ingestion MUST sustain ≥ 120 documents/hour/parse-core for typical 100-page PDFs. | P0 | 3 |
| NFR-P-05 | Embedding throughput MUST reach ≥ 2000 chunks/s on one L4-class GPU with bge-m3 at batch 64. | P1 | 3 |
| NFR-P-06 | Permission and API key resolution MUST add < 5 ms p99 to the data plane. | P0 | 2 |
| NFR-P-07 | Query embedding cache hit rate SHOULD exceed 30% under representative agent traffic. | P1 | 3 |
| NFR-P-08 | Bulk vector upsert MUST reach ≥ 5000 chunks/s to the vector store. | P1 | 3 |

## NFR-S — Scalability

| ID | Requirement | Pri | Phase |
| --- | --- | --- | --- |
| NFR-S-01 | `api-data` and `api-control` MUST be stateless and horizontally scalable without coordination. | P0 | 2 |
| NFR-S-02 | Worker pools MUST scale independently per queue. | P0 | 2 |
| NFR-S-03 | The system MUST support ≥ 10M vectors per KB and ≥ 100M per deployment. | P1 | 5 |
| NFR-S-04 | Every table MUST carry `workspace_id` to permit future sharding by workspace. | P0 | 1 |
| NFR-S-05 | Vector backend MUST be swappable per KB without application code change. | P0 | 2 |
| NFR-S-06 | Worker autoscaling MUST key on task backlog depth per queue, not CPU. | P1 | 5 |
| NFR-S-07 | The design MUST support ≥ 1000 KBs per deployment without vector-store degradation (hybrid collection layout). | P1 | 5 |

## NFR-R — Reliability

| ID | Requirement | Pri | Phase |
| --- | --- | --- | --- |
| NFR-R-01 | Data-plane availability target ≥ 99.9% monthly. | P0 | 5 |
| NFR-R-02 | Control-plane unavailability MUST NOT affect the data plane. | P0 | 2 |
| NFR-R-03 | Task enqueue MUST be transactional with the state change that requires it — no dual-write window. | P0 | 1 |
| NFR-R-04 | A worker crash MUST NOT lose work; leases expire and tasks are reclaimed within 2× the lease interval. | P0 | 1 |
| NFR-R-05 | Ingestion MUST be resumable from the last completed stage, not restarted from the beginning. | P0 | 2 |
| NFR-R-06 | All ingestion operations MUST be idempotent under retry. | P0 | 2 |
| NFR-R-07 | Vector store or embedding provider unavailability MUST degrade gracefully with clear errors, not hang or corrupt state. | P0 | 2 |
| NFR-R-08 | Backup and documented, rehearsed restore MUST cover Postgres, object store, and vector store. | P0 | 5 |
| NFR-R-09 | Database migrations MUST be forward-compatible with the previous release to permit rolling deployment. | P1 | 3 |

## NFR-SEC — Security

| ID | Requirement | Pri | Phase |
| --- | --- | --- | --- |
| NFR-SEC-01 | All external traffic MUST be TLS 1.2+; TLS termination is documented at the ingress. | P0 | 0 |
| NFR-SEC-02 | Secrets MUST be encrypted at rest with AES-256-GCM envelope encryption keyed from `CAIRN_MASTER_KEY` or a KMS. | P0 | 3 |
| NFR-SEC-03 | No secret, password, API key, or token may appear in logs, traces, error messages, or API responses. | P0 | 0 |
| NFR-SEC-04 | All state-changing cookie-authenticated requests MUST be CSRF-protected (double-submit token plus `Origin` check). | P0 | 1 |
| NFR-SEC-05 | All input MUST be validated at the API boundary by schema; unknown fields MUST be rejected on write endpoints. | P0 | 1 |
| NFR-SEC-06 | User-supplied code MUST execute only inside a kernel-level sandbox. | P0 | 4 |
| NFR-SEC-07 | Uploaded files MUST be type-sniffed, size-limited, stored outside the web root, and served only via signed URLs. | P0 | 2 |
| NFR-SEC-08 | Archive extraction MUST be protected against path traversal and decompression bombs. | P0 | 3 |
| NFR-SEC-09 | All outbound fetches from crawler and pipeline HTTP nodes MUST enforce SSRF protection: post-resolution IP checks against private/loopback/link-local/metadata ranges, re-checked on every redirect hop, with redirect and size caps. | P0 | 3 |
| NFR-SEC-10 | Retrieved content MUST be treated as untrusted when passed to LLM nodes; delimiting and tool allowlists are required. | P1 | 4 |
| NFR-SEC-11 | Dependencies MUST be scanned for vulnerabilities and licences in CI; the build fails on high-severity findings. | P0 | 1 |
| NFR-SEC-12 | Containers MUST run as non-root with read-only root filesystems where feasible. | P0 | 0 |
| NFR-SEC-13 | Rate limiting MUST be enforced per API key and per IP at the edge and in-application. | P0 | 2 |

## NFR-O — Observability

| ID | Requirement | Pri | Phase |
| --- | --- | --- | --- |
| NFR-O-01 | All logs MUST be structured JSON including `request_id`, `workspace_id`, `principal_id`, and `trace_id`. | P0 | 0 |
| NFR-O-02 | OpenTelemetry traces MUST span ingress → API → vector store → model provider, and ingress → API → task → worker. | P0 | 2 |
| NFR-O-03 | Prometheus metrics MUST cover retrieval latency histograms, queue depth per queue, ingestion throughput, provider error rate, and cache hit rate. | P0 | 2 |
| NFR-O-04 | `/healthz` (liveness) and `/readyz` (dependency checks) MUST be provided. | P0 | 0 |
| NFR-O-05 | Ingestion state MUST be observable per document with stage and error code. | P0 | 2 |
| NFR-O-06 | Grafana dashboards and alert rules MUST ship with the deployment. | P1 | 5 |

## NFR-D — Deployability

| ID | Requirement | Pri | Phase |
| --- | --- | --- | --- |
| NFR-D-01 | A single `docker compose up -d` MUST produce a working system with no manual steps beyond reading the printed admin password. | P0 | 0 |
| NFR-D-02 | Optional components (Qdrant, GPU embedding, OCR) MUST be Compose profiles, not required services. | P0 | 0 |
| NFR-D-03 | Migrations MUST run in a dedicated one-shot container under an advisory lock, never in the API entrypoint. | P0 | 0 |
| NFR-D-04 | Images MUST be multi-stage, non-root, with pinned base digests, and total core image size < 2 GB. | P1 | 1 |
| NFR-D-05 | A Helm chart supporting HPA and KEDA MUST be provided. | P1 | 5 |
| NFR-D-06 | Three documented sizing presets (small/medium/large) with capacity figures MUST be provided. | P1 | 5 |
| NFR-D-07 | All configuration MUST be environment-variable driven with documented defaults; no config file editing required for standard deployment. | P0 | 0 |

## NFR-M — Maintainability

| ID | Requirement | Pri | Phase |
| --- | --- | --- | --- |
| NFR-M-01 | Module dependency rules MUST be enforced in CI by `import-linter`. | P0 | 0 |
| NFR-M-02 | Data-plane modules MUST NOT import control-plane modules. CI MUST fail on violation. | P0 | 0 |
| NFR-M-03 | Line coverage ≥ 80% overall; ≥ 90% for M02 authz, M09 retrieval, M06 tasks. | P0 | 2 |
| NFR-M-04 | Public interfaces MUST be fully type-annotated; `mypy --strict` MUST pass. | P0 | 0 |
| NFR-M-05 | Every module MUST have a spec document kept in sync with its implementation; spec changes are part of the PR. | P0 | 0 |
| NFR-M-06 | No cross-module ORM model imports; modules communicate through service facades and DTOs. | P0 | 0 |

## NFR-C — Compatibility

| ID | Requirement | Pri | Phase |
| --- | --- | --- | --- |
| NFR-C-01 | The Dify External Knowledge API contract MUST be implemented exactly. | P1 | 3 |
| NFR-C-02 | LangChain and LlamaIndex retriever packages MUST be published. | P1 | 3 |
| NFR-C-03 | The MCP implementation MUST conform to the current MCP specification over streamable HTTP. | P0 | 3 |
| NFR-C-04 | Any OpenAI-compatible embedding or chat endpoint MUST be usable as a provider. | P0 | 3 |
| NFR-C-05 | Supported browsers: last 2 versions of Chrome, Edge, Firefox, Safari. | P1 | 2 |

---

## Requirement counts

| Group | Count | P0 |
| --- | --- | --- |
| FR-A Authentication | 14 | 11 |
| FR-B Authorization | 14 | 11 |
| FR-C Knowledge Bases | 11 | 8 |
| FR-D Upload | 10 | 7 |
| FR-E Crawling | 11 | 8 |
| FR-F Parsing/Chunking | 11 | 7 |
| FR-G Vectorization | 11 | 8 |
| FR-H Retrieval | 17 | 12 |
| FR-I API | 12 | 8 |
| FR-J MCP | 7 | 5 |
| FR-K Model Gateway | 9 | 6 |
| FR-L Pipelines | 11 | 8 |
| FR-M Functions | 9 | 8 |
| FR-N Evaluation | 9 | 5 |
| FR-O Platform | 8 | 2 |
| FR-P Web UI | 10 | 6 |
| **Functional total** | **174** | **120** |
| NFR (all groups) | 55 | 43 |
| **Total** | **229** | **163** |
