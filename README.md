# Cairn — Knowledge Base Platform / RAG Infrastructure Server

> A standalone, high-performance infrastructure layer for knowledge base construction,
> management, indexing, and retrieval. Agents and applications are **clients**; Cairn is
> the **server** that produces, stores, indexes, and serves knowledge.

**Status:** Phase 2 in progress; knowledge-base E2E gate remains open.
**Documentation baseline date:** 2026-08-28 · **Code status date:** 2026-09-08

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
| M04/M05 storage | `ObjectStore` and `VectorStore` protocols, local + pgvector drivers, **137-test conformance suite** |
| M10 modelgw | Model/provider registry (the lookup half; invocation is Phase 3) |
| M03 catalog | Knowledge bases, documents, chunks, blue/green index versions, storage bindings, runtime publication |
| M07 ingestion | Text + Office parsers, language detection, built-in/semantic/custom chunking, four-stage workers; bounded acceptance only |
| M08 embedding | Model-bound tokenizers, dense TEI/Infinity adapters, batching/cache/retry; actual TEI pipeline test |
| M16 web UI | Login, forced-change dialog, users, API keys, grants, audit, settings |

Not yet started: M09 retrieval, M11 pipelines,
M12 functions, M13 MCP, M14 evaluation.

**Historical verification checkpoint (2026-09-07):** the previously unexecuted integration suite ran against
real PostgreSQL 16 + pgvector and Redis. The full storage contract suite also runs on
the real pgvector driver rather than skipping it. This uncovered and fixed the missing
migration driver, task-claim SQL/ordering/fairness defects and stale test setup.
That checkpoint passed **400 tests**, with **81.47%** combined statement/branch coverage,
above the unchanged **80%** gate. Frontend: **27 tests**, typecheck and production build pass.
These historical checks are not the status of the expanded current worktree. Current pipeline and
advanced-chunking packages have independent acceptance, including real TEI, PostgreSQL/pgvector and
Redis, but the broader backend still has known lifecycle failures and PDF/OCR collection errors.

**Still open:** PDF/OCR adapters and the genuine 30-document PDF quality gate, full-KB rebuild/manual
re-embedding/retirement, and the production M12 custom-function sandbox. The default custom worker
fails closed unless an explicit scoped executor is injected. The overall knowledge-base E2E gate is
not complete. The current restart point is the [execution ledger](docs/04-plan/15-execution-ledger.md).
See also the
[parser checkpoint](docs/04-plan/05-parser-spike.md) and
[continuation verification record](docs/04-plan/06-continuation-verification.md).

**Start here → [`docs/README.md`](docs/README.md)** (documentation map and reading order)

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
