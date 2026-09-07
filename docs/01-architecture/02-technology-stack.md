# Technology Stack

**Document:** `01-architecture/02-technology-stack.md`
**Status:** Pinned baseline
**Date:** 2026-08-28

Versions are **minimums**, pinned exactly in lockfiles. Changing any row marked
**Load-bearing** requires an ADR.

---

## 1. Backend runtime

| Component | Choice | Version | Load-bearing | Rationale |
| --- | --- | --- | --- | --- |
| Language | Python | 3.12+ | ✅ | The document-processing and ML ecosystem is Python-only in practice. The retrieval hot path is I/O-bound, so async Python is not the bottleneck — provider latency and vector search are. |
| Web framework | FastAPI | 0.115+ | ✅ | Async-native, Pydantic-integrated, generates OpenAPI 3.1 (`FR-I-06`) |
| ASGI server | uvicorn + uvloop | 0.32+ | | uvloop gives ~2× throughput over asyncio's default loop |
| Process manager | gunicorn (uvicorn workers) | 23+ | | Graceful restarts, worker recycling |
| Validation | Pydantic | v2.9+ | ✅ | Rust core; fast enough for the hot path |
| ORM | SQLAlchemy | 2.0+ (async) | ✅ | Mature async, explicit unit of work |
| DB driver | asyncpg | 0.30+ | | Fastest Postgres driver for Python |
| Migrations | Alembic | 1.13+ | ✅ | `NFR-D-03` |
| HTTP client | httpx | 0.27+ | | Async, connection pooling, HTTP/2 |
| Password hashing | argon2-cffi | 23+ | ✅ | Argon2id per `FR-A-08` |
| Task queue | **PostgreSQL table + `FOR UPDATE SKIP LOCKED`** | — | ✅ | [ADR-0003](05-adr/ADR-0003-postgres-task-queue.md). Optionally `procrastinate` 2.x rather than hand-rolling. |
| Typing | mypy | 1.13+ strict | ✅ | `NFR-M-04` |
| Lint/format | ruff | 0.7+ | | Replaces black + isort + flake8 |
| Boundary enforcement | import-linter | 2.0+ | ✅ | `NFR-M-01`, `NFR-M-02` |
| Testing | pytest, pytest-asyncio, testcontainers | — | | Real Postgres/Redis/Qdrant in integration tests |

> **Not chosen: Go or Rust for the API.** The hot path is I/O-bound; Python is not the
> constraint. Choosing Go would mean reimplementing or FFI-ing the entire parsing and
> embedding stack. If profiling later shows a real ceiling, M09 retrieval is ~1500 lines behind
> a narrow interface and can be extracted in isolation.

## 2. Data stores

| Component | Choice | Version | Load-bearing | Rationale |
| --- | --- | --- | --- | --- |
| Metadata DB | PostgreSQL | 16+ | ✅ | JSONB, FTS, partitioning, `SKIP LOCKED`, `pgvector`. One database to operate. |
| Vector, default | pgvector | 0.8+ | ✅ | Zero extra operational surface; transactional with metadata; comfortable to ~5M vectors with HNSW |
| Vector, scale | Qdrant | 1.12+ | ✅ | Rust; native sparse vectors (hybrid without a second system); int8/binary quantization; tenant-partitioned payload indexes; sharding |
| Vector, optional | Elasticsearch / OpenSearch | 8.x / 2.x | | Only for deployments that already run it |
| CJK full-text | `zhparser` or `pg_jieba` | — | ✅ | Postgres' default parser is unusable for Chinese (`FR-F-04`). Decide before the FTS schema is written. |
| Cache / rate limit / pubsub | Redis | 7.2+ | ✅ | Never a source of truth |
| Object storage | MinIO (S3 API) | latest | | Same code path as S3/OSS/COS. AGPL — shipped as an unmodified separate container; S3 is the documented alternative. |
| Connection pooling | pgbouncer | 1.23+ | | Transaction mode; required above ~200 connections |

## 3. Document processing

| Purpose | Choice | Notes |
| --- | --- | --- |
| PDF text/layout | PyMuPDF (`fitz`) | Fast baseline extraction |
| PDF complex layout | **MinerU** or **Docling** | Tables, formulas, reading order. **This is where retrieval quality is won or lost** (`FR-F-02`). Evaluate both in Phase 2 spike `T-M07-01`. |
| Office | python-docx, python-pptx, openpyxl | |
| HTML → main content | trafilatura | Best-in-class boilerplate removal (`FR-E-05`) |
| Markdown | markdown-it-py | Heading-aware chunking source |
| EPUB | ebooklib | |
| OCR | RapidOCR (default) / PaddleOCR (CN-heavy) | ONNX runtime, CPU-viable (`FR-F-03`) |
| Crawling | Playwright + trafilatura | JS rendering (`FR-E-04`) |
| Tokenization | tiktoken + HF tokenizers | Must match the KB's embedding model (`FR-F-11`) |
| CJK segmentation | jieba | BM25 term generation |

## 4. Models and inference

| Purpose | Choice | Notes |
| --- | --- | --- |
| Provider abstraction | **LiteLLM**, wrapped in our own `ModelProvider` interface | Covers OpenAI, Anthropic, Gemini, DashScope/Qwen, DeepSeek, Bedrock, Ollama, vLLM (`FR-K-02`). **The wrapper is mandatory** — LiteLLM types must not leak into the domain. |
| Local embedding/rerank server | **Infinity** or HF **TEI** | Dynamic batching → ~10× throughput vs. naive transformers. Reduces query embedding from ~40–120 ms to ~8 ms (`NFR-P-05`). |
| Default embedding model | `BAAI/bge-m3` (1024-d) | Multilingual EN+CN, dense + sparse from one model |
| Default reranker | `BAAI/bge-reranker-v2-m3` | Multilingual cross-encoder |
| Alternatives (hosted) | OpenAI `text-embedding-3-large`, Qwen `text-embedding-v3`, Cohere/Jina rerank | Configurable per KB |

## 5. Frontend

| Component | Choice | Version | Rationale |
| --- | --- | --- | --- |
| Framework | Vue 3 (Composition API) | 3.5+ | Best fit for dense admin/data UI; matches the MaxKB interaction model the product references |
| Language | TypeScript | 5.6+ strict | |
| Build | Vite | 5+ | |
| Component library | Element Plus | 2.8+ | Mature tables, forms, trees — the bulk of this UI |
| State | Pinia | 2.2+ | |
| Server state | TanStack Query (vue-query) | 5+ | Caching, polling for ingestion status (`FR-P-05`) |
| Router | Vue Router | 4+ | Navigation guard implements the forced-change block (`FR-P-02`) |
| DAG canvas | Vue Flow (`@vue-flow/core`) | 1.41+ | Pipeline editor (`FR-P-07`) |
| Code editor | Monaco | — | Function authoring (`FR-M-01`) |
| Charts | ECharts | 5+ | Evaluation and usage dashboards |
| i18n | vue-i18n | 10+ | EN + ZH (`FR-P-08`) |
| API client | Generated from OpenAPI (`openapi-typescript`) | — | Never hand-write client types |
| Testing | Vitest + Playwright | — | Unit + E2E |

## 6. Infrastructure and operations

| Component | Choice | Rationale |
| --- | --- | --- |
| Containerization | Docker, multi-stage, non-root, pinned digests | `NFR-D-04`, `NFR-SEC-12` |
| Single-node orchestration | Docker Compose v2 with profiles | `NFR-D-01`, `NFR-D-02` |
| Cluster orchestration | Kubernetes + Helm | Phase 5, `NFR-D-05` |
| Worker autoscaling | KEDA (PostgreSQL scaler on queue depth) | `NFR-S-06` |
| Reverse proxy | Nginx (default) / Traefik | TLS, path routing, static SPA |
| Sandbox runtime | **gVisor (`runsc`)**; Firecracker as the hardened alternative | `FR-M-03`, `NFR-SEC-06`. Interpreter-level restriction is **not** acceptable. |
| Tracing | OpenTelemetry SDK → OTLP | `NFR-O-02` |
| Metrics | prometheus-client → Prometheus | `NFR-O-03` |
| Dashboards | Grafana (shipped as provisioned JSON) | `NFR-O-06` |
| Logging | structlog → JSON → stdout | `NFR-O-01` |
| Error tracking | Sentry (optional) | |
| CI | GitHub Actions (or GitLab CI) | |
| Dependency/licence scan | pip-audit, `uv`, license-checker | `NFR-SEC-11` |

## 7. Dependency licence policy

| Verdict | Licences |
| --- | --- |
| ✅ Permitted | MIT, Apache-2.0, BSD-2/3, ISC, MPL-2.0, PSF, Unlicense |
| ⚠️ Review required | LGPL (dynamic linking only), CC-BY |
| ❌ Prohibited in the distributed image | GPL-2.0, GPL-3.0, AGPL-3.0, SSPL, commercial-restricted |

**Exceptions, documented and approved:**

| Package | Licence | Justification |
| --- | --- | --- |
| MinIO server | AGPL-3.0 | Shipped as a separate unmodified container, not linked. S3 is offered as an alternative. Reviewed: Platform lead. |
| PyMuPDF | AGPL-3.0 **or** commercial | ⚠️ **`RISK-11` — blocking decision required in Phase 2.** Options: (a) purchase a commercial licence, (b) use `pypdfium2` (Apache-2.0/BSD) as the baseline extractor, (c) isolate PyMuPDF into a separately-distributed optional container. Owner: Platform lead. Do not build on PyMuPDF until resolved. |
| MinerU | AGPL-3.0 | Same treatment — run as a separate parser sidecar container, not imported in-process. Alternative: **Docling (MIT)**, which is the preferred default for this reason. |

> **Action:** the Phase 2 parser spike (`T-M07-01`) MUST evaluate Docling (MIT) and pypdfium2
> (permissive) as the primary path precisely to avoid the AGPL exposure. Treat MinerU/PyMuPDF
> as fallback options requiring the container-isolation pattern.

## 8. Version pinning policy

- Python dependencies: `uv` with a committed `uv.lock`. Exact pins.
- Frontend: **`npm` with committed `package-lock.json`.** (Changed from pnpm during
  T-M16-01: pnpm was not present in the build environment and `corepack` adds a
  bootstrap step to every CI job and developer machine for no benefit at this size.
  Revisit if a workspace/monorepo layout arrives.)
- Base images: pinned by **digest**, not tag.
- Model weights: pinned by revision hash, cached in the image or a volume.
- Renovate/Dependabot runs weekly; security patches merged within 7 days; minor upgrades batched monthly.

## 9. Local development

| Need | Tool |
| --- | --- |
| Python env | `uv venv` + `uv sync` |
| Run everything | `docker compose --profile dev up` (`CAIRN_ROLE=all`) |
| Hot reload | `uvicorn --reload` for API, `vite dev` for UI |
| Test dependencies | testcontainers spins real Postgres/Redis/Qdrant per session |
| Task runner | `make` (`make dev`, `make test`, `make lint`, `make migrate`) |
| Pre-commit | ruff, mypy, import-linter, secret scan |
