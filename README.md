# Cairn — Knowledge Base Platform / RAG Infrastructure Server

> A standalone, high-performance infrastructure layer for knowledge base construction,
> management, indexing, and retrieval. Agents and applications are **clients**; Cairn is
> the **server** that produces, stores, indexes, and serves knowledge.

**Status:** Qdrant knowledge product with advanced retrieval, MCP directory/document tools, Dify compatibility, S3-compatible object storage, a session-authorised model gateway with usage accounting, and Python/TypeScript client SDKs. PDF parsing runs **Docling 2.135.0 by default** with a persistent model worker; scans that already carry a text layer are read by the pypdfium2 baseline instead of being OCR'd again ([PDF parsing](docs/06-ops/11-pdf-parsing.md)). The independent-human PDF quality gate remains **open**; the annotation kit is ready ([guide](docs/05-quality/05-pdf-annotation-guide.md)).
**Documentation baseline date:** 2026-08-28 · **Code status date:** 2026-10-13

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
| M04/M05 storage | `ObjectStore` and `VectorStore` protocols, local/S3-compatible object drivers and Qdrant vector storage, with shared conformance suites |
| M10 modelgw | Admin local/hosted provider registry, sealed credentials and live probe, dynamic Redis provider projection; session-authorised chat and rerank invocation with streaming, cancellation, retry and circuit breaker; usage accounting; admin and user model console |
| M03 catalog | Knowledge bases, real upload/download, chunks, blue/green indexes, automatic runtime refresh and protected document/KB/index cleanup |
| M07 ingestion | Text, Markdown, JSON, HTML, CSV and image-OCR parsers; PDF, Word and PowerPoint through one persistent Docling worker (immutable document IR, tables, pictures as assets) with the pypdfium2 baseline for scans that carry a text layer and the native Office parsers as a recorded fallback; XLSX via openpyxl keeping formula source; language detection; built-in/semantic/custom chunking; four-stage workers |
| M08 embedding | Model-bound tokenizers, TEI/Infinity, hosted embedding providers and optional bge-m3 dense/sparse adapters, batching/cache/retry |
| M09 retrieval | Bounded authenticated `/v1/retrieval/query`: Qdrant full-text/vector/hybrid, ACTIVE runtime snapshots, fusion/rerank/MMR, metadata/date filters, highlights/explain, batches and citations; Dify `/retrieval` adapter |
| M13 MCP | Streamable HTTP `/mcp` with scoped search, knowledge-base directory, ordered document passages and resources; `kb:read` required for document reading |
| M16 web UI | Redesigned workbench (Tailwind v4 + reka-ui): overview with attention list, master-detail knowledge bases with upload/progress/chunk editing/search console, models, storage, API keys, users/grants, audit, settings, MCP service; parse preview with citation overlay, retrieval explain panel, model chat console; light/dark, zh-CN/en-US, command palette |
| SDK | Python and TypeScript clients ([`sdk/`](sdk/)) with optional LangChain and LlamaIndex adapters; local packages, not published to a registry |

Not yet started: M11 pipelines, M12 functions, M14 evaluation. Quotas, web crawling and archive
import, SDK registry publication and production-scale gates (Helm/HPA, backup drills, scale
evidence) also remain planned. See the [completion map](docs/04-plan/37-current-completion-map.md).

**Verification (2026-10-12):** the full unit suite passes; the PDF, Office, pipeline and
stage-recovery integration tests pass against real PostgreSQL 16, Redis and Qdrant; Ruff,
mypy `--strict` and the import contracts are clean. Docling converted all 36 corpus PDFs
(241 pages) at 6.85 s per page on 2 CPUs with the model loaded once, extracting 96 tables
and 40 pictures, and a live product run uploaded, parsed (`engine: docling`), indexed and
retrieved real PDFs end to end. Earlier checkpoints and their counts are recorded in the
[execution ledger](docs/04-plan/15-execution-ledger.md) and the acceptance documents under
`docs/04-plan/`.

**Verification (2026-10-13):** a pilot with two of the project owner's real documents, a
64-page HR manual (PDF, native text) and a duty roster (XLSX), ran through the product stack.
Docling parsed the manual into 907 elements and 470 chunks and the roster into one table; after
the knowledge base was rebuilt from the bundled MiniLM onto the bge-m3 sidecar (dense +
learned sparse, CUDA on an RTX 3060: about 45 chunks/s, 45 ms per query) all seven test
questions return the correct passage at rank 1 (MiniLM: five of seven within the top three, but
only two at rank one and two missed). MiniLM is English-only and scored every Chinese chunk
alike (0.44-0.50), so Chinese retrieval had depended on BM25 alone. The pilot surfaced and fixed
a 4 GiB address-space
ceiling that failed the Docling child with `MemoryError`, an embedding deadline that covered a
whole document call instead of one batch, and the sidecar reporting healthy before its first,
slow CUDA forward pass.

**Still open:** the PDF quality gate needs independent human annotation of the 30-document
corpus; `scripts/prepare_pdf_annotations.py` generates the empty gold skeleton and page
templates and the [annotation guide](docs/05-quality/05-pdf-annotation-guide.md) fixes the
format and rules. Formula recognition is not implemented (formula text is kept verbatim),
Docling occasionally merges two values into one table cell, and the production M12
custom-function sandbox is not built; the default custom worker fails closed unless an
explicit scoped executor is injected. `docs/` and `tests/` were git-ignored between
2026-09-23 and 2026-10-12 and are tracked again. A document whose embed task dead-letters now
names the failed document and stage on the building index version (the Versions panel renders
it; retry resumes the failed stage) and activation clears the note; the reindex estimate
assumes GPU-class embedding throughput; reranking runs only where the optional `reranker`
service is started and named in `CAIRN_RETRIEVAL__RERANK_ENDPOINTS`. The current restart point
is the
[completion map](docs/04-plan/37-current-completion-map.md).

**Start here → [`docs/README.md`](docs/README.md)** (documentation map and reading order)

### Bounded retrieval startup

`POST /v1/retrieval/query` is mounted when `CAIRN_ROLE` is `data` or `all`. BM25 full-text queries and requests supplying `query_vector` require no dense-model endpoint;
learned sparse search requires its configured provider. To embed query
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
[PDF/OCR and extended retrieval](docs/06-ops/07-pdf-and-retrieval.md)). MMR, document/date/metadata filters, highlights, explain and batch queries are available. Older
indexes require rebuilding for metadata/date filters and ordered MCP document reading.
See [S3 and external clients](docs/06-ops/09-s3-and-external-clients.md). The maintain worker now refreshes ACTIVE KB
and provider projections periodically (default60 seconds) and repairs Redis cache loss. Dynamic
providers fail closed while their projection is absent; retrieval never hydrates catalog ORM.

## Run the supported local product

```text
uv run python scripts/init_env.py
uv run python scripts/prepare_embedding_model.py
docker build -f deploy/pdf-worker/Dockerfile -t cairn-pdf-worker:2.135.0 .
# Prepare the Docling model assets and manifest once (docs/06-ops/11-pdf-parsing.md), then set
# CAIRN_PDF_MODELS_DIR and CAIRN_PDF_MANIFEST in .env; Compose fails fast while they are unset.
docker compose --profile local-model up -d --build
docker compose logs migrate
```

For Chinese or multilingual content register the bge-m3 sidecar instead of the bundled
English-only MiniLM, and optionally start the `reranker` service; both run on an NVIDIA GPU
through `docker-compose.gpu.yml` ([embedding providers](docs/06-ops/08-embedding-providers.md),
[reranking](docs/06-ops/07-pdf-and-retrieval.md#reranking-endpoint-configuration)).

Open `http://127.0.0.1:8080` and complete the initial password change. The overview lists the three
setup steps (model, storage, knowledge base); then upload documents, watch them move through the
pipeline, and search from the knowledge base's **Search** section. Press Ctrl+K to jump anywhere.
The initializer does not overwrite existing secrets. Named volumes preserve PostgreSQL, Redis and
document files; never remove deployment volumes unless their data is intentionally disposable.

The parse worker runs Docling with a 6 GB memory limit and keeps the models loaded between
documents. `CAIRN_PDF_ENGINE=pdfium` switches it to the lighter pypdfium2 baseline (no pictures,
heuristic tables); the two model-path variables must still point at existing paths because the
Compose file declares them required.

See [local product quickstart](docs/06-ops/04-local-product-quickstart.md) for exact model fields,
supported formats and limitations, and [coordinator acceptance](docs/04-plan/29-main-product-acceptance.md)
for actual browser/deployment evidence. This local preset is not an HTTPS production perimeter.

## Connect through MCP

Administrators can now manage MCP from **MCP服务** (`/mcp-service/nav`): choose a published port,
start/stop/restart, inspect actual heartbeat/state and filter/download safe operational logs.
Compose runs a dedicated supervisor on one selected port8081..8090; the existing8080/mcp endpoint
obeys the same state. See [MCP service management](docs/06-ops/06-mcp-service-management.md).

Generic search is verified with the official MCP SDK and through the actual Compose nginx/data/TEI
path; acceptance counts live in the [execution ledger](docs/04-plan/15-execution-ledger.md).
Agent-side integration remains intentionally deferred.

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
| Operations | [`docs/06-ops/`](docs/06-ops/) | Deployment topologies, observability, runbooks, PDF parsing and model worker |
| Clients | [`sdk/`](sdk/) | Python and TypeScript SDKs with optional LangChain/LlamaIndex adapters |

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
