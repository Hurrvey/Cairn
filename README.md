# Cairn — Knowledge Base Platform / RAG Infrastructure Server

> A standalone, high-performance infrastructure layer for knowledge base construction,
> management, indexing, and retrieval. Agents and applications are **clients**; Cairn is
> the **server** that produces, stores, indexes, and serves knowledge.

**Status:** Supported-format local product, PDF/OCR baseline and advanced retrieval accepted; full backend suite green (1066 passed). The genuine annotated30-PDF quality gate remains open.
**Documentation baseline date:** 2026-08-28 · **Code status date:** 2026-09-22

---

## What this repository currently contains

The `docs/` tree is a complete, implementation-ready specification set intended to be
consumed by multiple developers or coding Agents working in parallel. Alongside it,
these modules are implemented, typechecked (`mypy --strict`), and boundary-enforced
(`import-linter`):

| Module | What works |
| --- | --- |
| M00 core | Config, errors (RFC 9457), prefixed ids, DB/cache, SSRF-guarded HTTP, logging, telemetry, cursor pagination, health |
| M01 identity | Bootstrap admin, Argon2id, sessions, **forced credential change enforced in middleware** (FR-A-05) |
| M02 authz | Grants, API keys, the key ∩ owner intersection (FR-B-08), rate limits, break-glass |
| M06 tasks | Postgres-backed durable queue (`SKIP LOCKED` + per-workspace fairness), worker fleet |
| M15 platform | Audit log, workspace settings, maintenance |
| M04/M05 storage | `ObjectStore` and `VectorStore` protocols, local object store + Qdrant (the only vector backend, [ADR-0009](docs/01-architecture/05-adr/ADR-0009-qdrant-only-vector-backend.md)), shared conformance suite |
| M10 modelgw | Admin TEI/Infinity registry and live probe, safe setup options and dynamic Redis provider projection; general chat invocation remains Phase 3 |
| M03 catalog | Knowledge bases, real upload/download, chunks, blue/green indexes, automatic runtime refresh and protected document/KB/index cleanup |
| M07 ingestion | Text + Office parsers, language detection, built-in/semantic/custom chunking, four-stage workers; bounded acceptance only |
| M08 embedding | Model-bound tokenizers, dense TEI/Infinity adapters, batching/cache/retry; actual TEI pipeline test |
| M09 retrieval | Bounded authenticated `/v1/retrieval/query`: BM25 full-text (jieba sparse vectors on Qdrant)/vector/hybrid, ACTIVE runtime snapshots, RRF, citations, explicit degradation |
| M13 MCP | Generic read-only `search_knowledge_base` via Streamable HTTP `/mcp`, scoped bearer keys, cited/structured results; broader list/get/resources remain open |
| M16 web UI | Redesigned workbench (Tailwind v4 + reka-ui): overview with attention list, master-detail knowledge bases with upload/progress/chunk editing/search console, models, storage, API keys, users/grants, audit, settings, MCP service; light/dark, zh-CN/en-US, command palette |

Not yet started: M11 pipelines,
M12 functions, M14 evaluation. M13 currently covers the read-only search increment only.

**Historical verification checkpoint (2026-09-07):** the previously unexecuted integration suite ran against
real PostgreSQL 16 + pgvector and Redis. The full storage contract suite also runs on
the real pgvector driver rather than skipping it. This uncovered and fixed the missing
migration driver, task-claim SQL/ordering/fairness defects and stale test setup.
That checkpoint passed **400 tests**, with **81.47%** combined statement/branch coverage,
above the unchanged **80%** gate. Frontend: **27 tests**, typecheck and production build pass.
These historical checks are not the status of the expanded current worktree. Current pipeline and
advanced-chunking packages have independent acceptance, including real TEI, PostgreSQL/pgvector and
Redis. The September15 five-part supported-product increment passed independent review:
the expanded backend has **893 passing tests**, no assertion failures or skips,
**2 deferred PDF/OCR collection errors**, and **85.82%** coverage. Frontend: **36 tests**, typecheck,
API drift and production build pass. Real Chromium configuration/upload/query, actual model inference,
incremental/rebuild/edit/retry, Redis repair, persistence and protected cleanup are verified.
September22: PDF/OCR adapters and advanced retrieval landed; the full backend command now exits zero
(**1066 passed, 0 failures, 0 collection errors, 84.62% coverage**). See
[PDF/OCR continuation](docs/04-plan/34-pdf-ocr-continuation.md). September23: the web UI was rebuilt as a
workbench; frontend **60 tests**, typecheck, i18n check, build, web image and a **24-step live browser
acceptance** pass. See [web workbench redesign](docs/04-plan/35-web-workbench-redesign.md).

**Still open:** the genuine annotated 30-document PDF quality gate
([report](docs/05-quality/04-pdf-ocr-evaluation.md)) and the production M12 custom-function sandbox. Bounded fan-out, failed-stage recovery, manual reembedding and building-version cleanup are
accepted alongside general purge and retired-index cleanup. This does not close the original
PDF/OCR-inclusive E2E scope. The default custom worker
fails closed unless an explicit scoped executor is injected. The overall knowledge-base E2E gate is
not complete. The current restart point is the [execution ledger](docs/04-plan/15-execution-ledger.md).
See also the
[parser checkpoint](docs/04-plan/05-parser-spike.md) and
[continuation verification record](docs/04-plan/06-continuation-verification.md).

**Start here → [`docs/README.md`](docs/README.md)** (documentation map and reading order)

### Bounded retrieval startup

`POST /v1/retrieval/query` is mounted when `CAIRN_ROLE` is `data` or `all`. Full-text
queries and requests supplying `query_vector` require no model endpoint. To embed query
text for vector or hybrid search, configure the endpoint by the immutable provider ID in
the KB's published ACTIVE runtime and configure its tokenizer by `tokenizer_id`:

```dotenv
CAIRN_RETRIEVAL__EMBEDDING_ENDPOINTS={"20000000-0000-0000-0000-000000000002":{"dialect":"tei","base_url":"http://127.0.0.1:38742","allow_private":true,"namespace":"deployment-revision","max_batch_size":16}}
CAIRN_RETRIEVAL__TOKENIZER_FILES={"live-minilm":"D:/models/tokenizer.json"}
CAIRN_RETRIEVAL__REQUEST_TIMEOUT_S=5
CAIRN_RETRIEVAL__RUNTIME_TIMEOUT_S=0.25
CAIRN_RETRIEVAL__SEARCH_TIMEOUT_S=2
```

The endpoint implements RRF and weighted fusion, parent expansion from indexed parent snapshots and
optional reranking through operator-configured cross-encoder endpoints (see
[PDF/OCR and extended retrieval](docs/06-ops/07-pdf-and-retrieval.md)). MMR, filters, highlights and
explain mode are not implemented; explicit requests for those options fail clearly;
unsupported KB defaults are reported in `degraded`. The maintain worker now refreshes ACTIVE KB
and provider projections periodically (default60 seconds) and repairs Redis cache loss. Dynamic
providers fail closed while their projection is absent; retrieval never hydrates catalog ORM.

## Run the supported local product

```text
uv run python scripts/init_env.py
uv run python scripts/prepare_embedding_model.py
docker compose --profile local-model up -d --build
docker compose logs migrate
```

Open `http://127.0.0.1:8080` and complete the initial password change. The overview lists the three
setup steps (model, storage, knowledge base); then upload documents, watch them move through the
pipeline, and search from the knowledge base's **Search** section. Press Ctrl+K to jump anywhere.
The initializer does not overwrite existing secrets. Named volumes preserve PostgreSQL, Redis and
document files; never remove deployment volumes unless their data is intentionally disposable.

See [local product quickstart](docs/06-ops/04-local-product-quickstart.md) for exact model fields,
supported formats and limitations, and [coordinator acceptance](docs/04-plan/29-main-product-acceptance.md)
for actual browser/deployment evidence. This local preset is not an HTTPS production perimeter.

## Connect through MCP

Administrators can now manage MCP from **MCP服务** (`/mcp-service/nav`): choose a published port,
start/stop/restart, inspect actual heartbeat/state and filter/download safe operational logs.
Compose runs a dedicated supervisor on one selected port8081..8090; the existing8080/mcp endpoint
obeys the same state. See [MCP service management](docs/06-ops/06-mcp-service-management.md).

September17 acceptance:954 full-backend tests passed,2 unchanged deferred PDF/OCR collection errors,
no assertion failures/skips,86.29% overall coverage; final MCP/network/live-model matrix43 passed and
MCP module coverage93%. Generic search is verified with the official SDK and through actual Compose
nginx/data/TEI/pgvector. Agent-side integration remains intentionally deferred.

September24 QDRANT: vectors moved to Qdrant v1.19.1; pgvector removed and PostgreSQL returns to the
official `postgres:16-bookworm` image. Full-text is BM25 sparse vectors with jieba segmentation. Full backend
**1100 passed, 0 failed**, coverage 85%, on PostgreSQL 16, Redis, Qdrant and real TEI.

Use `http://127.0.0.1:8080/mcp` with `Authorization: Bearer <Cairn API key>` from a client supporting
Streamable HTTP and configurable headers. Grant only `kb:query` on the intended knowledge bases.
Official SDK1.30.0 implements the2025-11-25 compatibility baseline; this increment is stateless JSON,
not stdio/legacy SSE, and does not include OAuth discovery or an Agent-specific client.

```powershell
$env:CAIRN_API_KEY = "your-private-key"
uv run python scripts/mcp_smoke.py --query "A short question about your documents"
```

Pass `--kb-id kb_YOUR_ID` when more than one knowledge base is authorized. See the
[MCP quickstart](docs/06-ops/05-mcp-quickstart.md) for arguments, host/origin protection,
permission-cache timing, compatibility limits and full verification instructions.

---

## The one-paragraph version

Cairn ingests documents (uploads, crawled web pages, connectors), parses and chunks them,
generates embeddings, and stores them in a pluggable vector index. External Agents retrieve
knowledge through a single authenticated HTTP API — service URL + API key + a JSON payload
naming the query, the target knowledge base(s), and retrieval parameters — or by mounting
Cairn as an **MCP server**. The system separates a latency-critical **data plane** (retrieval)
from a **control plane** (management), so administration workloads can never degrade retrieval
SLOs. Administrators manage users, permissions, models, and storage; regular users build and
own knowledge bases within granted scopes.

---

## Documentation map

| Area | Location | Purpose |
| --- | --- | --- |
| Product | [`docs/00-overview/`](docs/00-overview/) | Vision, personas, glossary, numbered requirements catalog |
| Architecture | [`docs/01-architecture/`](docs/01-architecture/) | System architecture, stack, data model, module boundaries, ADRs |
| Module specs | [`docs/02-modules/`](docs/02-modules/) | One implementation-ready spec per module (M00–M16) |
| API | [`docs/03-api/`](docs/03-api/) | Conventions, data-plane API, control-plane API, compatibility surfaces |
| Plan | [`docs/04-plan/`](docs/04-plan/) | Phased development plan, work breakdown, team workflow, risk register |
| Quality | [`docs/05-quality/`](docs/05-quality/) | Test strategy, performance SLOs, security baseline |
| Operations | [`docs/06-ops/`](docs/06-ops/) | Deployment topologies, observability, runbooks |

---

## Key architectural commitments

These are decided, recorded as ADRs, and should not be relitigated without a new ADR.

1. **Modular monolith + worker fleet** — not microservices. ([ADR-0001](docs/01-architecture/05-adr/ADR-0001-modular-monolith.md))
2. **Control plane / data plane split** — same binary, two deployments, one env var. ([ADR-0002](docs/01-architecture/05-adr/ADR-0002-control-data-plane-split.md))
3. **PostgreSQL-backed durable task queue** — not a message broker. ([ADR-0003](docs/01-architecture/05-adr/ADR-0003-postgres-task-queue.md))
4. **Pluggable vector store** behind one Protocol. ([ADR-0004](docs/01-architecture/05-adr/ADR-0004-vector-store-abstraction.md))
5. **Denormalized read model** in the vector store; Postgres is source of truth. ([ADR-0005](docs/01-architecture/05-adr/ADR-0005-denormalized-read-model.md))
6. **Embedding model immutable per knowledge base.** ([ADR-0006](docs/01-architecture/05-adr/ADR-0006-immutable-embedding-model.md))
7. **Blue/green index versioning** for zero-downtime reindex. ([ADR-0007](docs/01-architecture/05-adr/ADR-0007-blue-green-index-versioning.md))
8. **Pipelines, not an agent runtime** — "Agent Workflow" composes ingestion/retrieval DAGs. ([ADR-0008](docs/01-architecture/05-adr/ADR-0008-pipeline-not-agent-runtime.md))

---

## Licensing note

MaxKB is referenced for **product design and UX patterns only**. MaxKB is GPLv3; no MaxKB
source may be copied into this repository. See [`docs/00-overview/01-product-brief.md`](docs/00-overview/01-product-brief.md) §9.
