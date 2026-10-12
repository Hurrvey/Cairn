# Qdrant as the Only Vector Backend — Design

**Date:** 2026-09-23 · **Status:** Approved · **Sub-project:** 1 of 3

## Context and decomposition

The product moves all vector storage to Qdrant. PostgreSQL keeps metadata and
the task queue; Redis is unchanged. The request also brought in bge-m3 learned
sparse vectors and hosted embedding vendors, which are independent subsystems,
so the work is split:

| # | Sub-project | Outcome |
| --- | --- | --- |
| **1** | **Qdrant as the only vector backend, with built-in BM25 sparse vectors** (this document) | pgvector removed; vector, full-text and hybrid retrieval all run on Qdrant |
| 2 | bge-m3 sidecar (FlagEmbedding + torch) as the default model; learned sparse vectors; per-KB sparse source (model's own → local bge-m3 → BM25) | Best-quality hybrid retrieval |
| 3 | Hosted embedding vendors: DashScope native (dense + sparse), Volcengine Ark and OpenAI-compatible; encrypted API keys; UI | Online models usable |

Sub-project 1 must ship BM25 itself: removing pgvector without a sparse path
would break full-text and hybrid retrieval until sub-project 2 lands.

No existing vector data needs to be preserved, in Cairn or in the company fork,
so the cut-over is direct: no data migration tool.

## 1. Driver — `cairn.vectorstore.qdrant`

Built on the official `qdrant-client` (Apache-2.0), `AsyncQdrantClient` over REST.

**Layout: one collection per namespace**, named `Namespace.key()`
(`cairn_vec_{kb_hex}_v{version}`). A shared collection was rejected because
Qdrant computes the IDF modifier over the whole collection: sharing would mix
term statistics across knowledge bases and degrade BM25 ranking. Per-namespace
collections also keep blue/green drop O(1). The known ceiling is a few hundred
live namespaces per Qdrant node; a shared layout for many tiny KBs is a later
option behind `layout_hint`.

Collection schema:

- Named dense vector `dense`: size = `spec.dim`, distance from `spec.metric`
  (`Cosine`/`Dot`/`Euclid`), HNSW `m` / `ef_construct` from
  `driver_options` (`hnsw_m`, default 16; `hnsw_ef_construction`, default 128).
- Named sparse vector `sparse` with `modifier: idf`, created when `spec.sparse`.
- `on_disk_payload: true`.
- Payload indexes: `document_id` (keyword), `created_at` (datetime), `_paths`
  (keyword).

Operations:

| Operation | Behaviour |
| --- | --- |
| `ensure_namespace` | Create if absent; idempotent; payload indexes created on create |
| `namespace_exists` / `drop_namespace` | `collection_exists` / `delete_collection` (drop of a missing namespace is a no-op) |
| `list_namespaces(kb_id)` | List collections, keep `cairn_vec_{kb_hex}_v{n}` with `n > 0`, sorted |
| `upsert` | Dimension validated by `validate_dimensions`; `wait=true` so the index stage reads its own writes; batched |
| `delete` | Selector required. Count matches first (by retrieve for IDs, by exact count for filters), then delete with `wait=true`; returns the count |
| `search` | Dense → `query_points(using="dense")`; sparse → `query_points(using="sparse")`; `hnsw_ef` from `ef_search`; `limit ≤ max_top_k` (1000) |
| `fetch` | `retrieve(with_payload=True)` |
| `count` | `count(exact=True)` |
| `health` | Round trip to the server, latency reported |

Missing namespace on upsert/search/fetch/count/delete raises `NamespaceNotFound`.
Transport failures raise `VectorStoreUnavailable`.

Scores are normalised to "higher is better": cosine and dot pass through;
Euclid is negated. `score_threshold` is applied client-side after
normalisation so it means the same thing on every metric.

`Capabilities`: `driver="qdrant"`, `sparse_vectors=True`,
`quantization={"none"}` (quantization is a later task), `payload_partitioning=False`,
`filter_operators=OPERATORS`, `max_top_k=1000`, `fast_namespace_drop=True`.

## 2. Filter translation — exact normative semantics

The reference is `cairn.vectorstore.filters.matches`. Translation:

| AST | Qdrant |
| --- | --- |
| `And` / `Or` / `Not` | `must` / `should` / `must_not` (empty `And` → match all, empty `Or` → match none) |
| `$eq` string / bool | `MatchValue` |
| `$eq` number | `Range(gte=v, lte=v)` — so `3` matches a stored `3.0`, as in Python; bools are never ranged |
| `$eq None` | `IsNull` |
| `$in` | `should` of the per-value `$eq` conditions (mixed-type lists work) |
| `$ne` / `$nin` | `must_not` of the `$eq` / `$in` translation — absent fields match |
| `$gt/$gte/$lt/$lte` number | `Range`; a stored string or bool never matches, never errors |
| ordered comparison with a string, bool or datetime operand | **raises `UnsupportedFilter`** — Qdrant has no lexicographic range, and a silent divergence is worse than a refusal |
| `$exists` | `MatchValue` on the hidden `_paths` field |

`_paths` is a keyword array written by the driver on upsert, listing every
dotted path present in the payload (including paths whose value is `null` or
`[]`). It makes `$exists` exact, where Qdrant's `IsEmpty` cannot tell absent
from `[]`. The driver strips `_paths` from every payload it returns.

Only document deletion uses filters in the product today (by `document_id`);
the retrieval API still rejects filters, so the string-ordering refusal affects
no product path.

## 3. BM25 sparse encoder — `cairn.embedding.sparse`

A deterministic, dependency-light encoder producing `SparseVector`s.

- Normalisation: NFKC, lowercase.
- Tokenisation: CJK runs are segmented with jieba `cut_for_search` (MIT); latin
  and digit runs are split into words. Code-like tokens joined by `-`, `_`, `.`
  or `/` (`XR-2200`, `v4.1`) are indexed whole **and** as their parts. Single
  punctuation and whitespace are dropped.
- Index: first 4 bytes of `blake2b(token)` as an unsigned 32-bit integer.
  Collisions merge weights and are rare at this width.
- Document weights: BM25 term-frequency saturation
  `tf·(k1+1) / (tf + k1·(1 − b + b·dl/avgdl))`, `k1=1.2`, `b=0.75`,
  `avgdl=256`. Qdrant's `idf` modifier supplies the IDF term at query time.
- Query weights: `1.0` per unique token.
- `ENCODER_ID = "bm25-jieba-v1"` identifies the tokenisation contract; any
  change to it is a new ID and requires a rebuild.

Empty token sets produce `None`: such a chunk is indexed dense-only, and a query
with no tokens skips the sparse stage.

## 4. Call-site changes

- `VectorQuery.text` is removed; full-text is a `sparse` query. The fake store
  drops its crude lexical scorer and keeps its sparse dot product.
- Ingestion: every place that builds a `Point` (index stage, chunk edit,
  chunk re-embed) attaches `sparse=encode_document(content)`.
- Retrieval: the "sparse" stage encodes `request.query` with `encode_query` and
  searches with `sparse=`; an empty encoding contributes no hits. Stage name,
  weights, RRF and weighted fusion are unchanged.
- Router/OpenAPI descriptions that mention pgvector are reworded; the web
  client types are regenerated.

## 5. Removing pgvector and deployment

- Delete `cairn/vectorstore/pgvector.py`, its registry branch and the ruff S608
  exemption. The registry builds `QdrantVectorStore` from `Settings.qdrant`
  (one client per binding, closed on registry close).
- New settings `QdrantSettings`: `url` (default `http://localhost:6333`),
  `api_key: SecretStr | None`, `timeout_s` (default 10).
  Env: `CAIRN_QDRANT__URL`, `CAIRN_QDRANT__API_KEY`.
- Storage bindings accept only `driver="qdrant"` for vectors, with empty
  config — connection details are deployment configuration, like the
  database URL was for pgvector.
- Migration `0013_qdrant`: drop any `cairn_vec_*` tables, `DROP EXTENSION IF
  EXISTS vector`, rewrite `storage_binding.driver` from `pgvector` to `qdrant`.
  Migration `0004` becomes conditional on the extension being available so the
  chain runs on a plain PostgreSQL image.
- Compose: `postgres` uses the official `postgres:16` image; new `qdrant`
  service pinned by digest, with a `qdrantdata` volume, a healthcheck, and
  `QDRANT__SERVICE__API_KEY` from `.env` (generated by `scripts/init_env.py`);
  not published to the host. App services depend on it being healthy.
- CI: `postgres:16` plus a `qdrant` service; tests get `CAIRN_QDRANT__URL`.
- Tests: `postgres_url` uses `postgres:16`; a new session `qdrant_url` fixture
  (testcontainers or external), wired into the integration environment.
- Web: the storage page offers the Qdrant driver; copy updated in both locales.

## 6. Verification

- Conformance suite runs on `fake` and `qdrant`. TC-M05-09 uses BM25 sparse
  vectors. New cases: `$exists` with `null` and `[]`, `$eq` int vs float,
  mixed-type `$in`, string ordering raises `UnsupportedFilter`, Euclid score
  direction and threshold.
- Unit tests for the BM25 encoder: mixed Chinese/English, code tokens,
  determinism, empty input, TF saturation.
- Full backend suite against real PostgreSQL 16, Redis, Qdrant and TEI; ruff,
  mypy, import-linter, route-authz audit.
- Web: vitest, vue-tsc, build, `api:check`, i18n check.
- Compose build and start; the 24-check browser acceptance run against the
  live stack.

## 7. Documentation and the company fork

- ADR-0009 "Qdrant as the only vector backend", superseding ADR-0004's
  "ship pgvector and Qdrant" decision and its rejection of Qdrant-only.
- Update M05, technology stack, README, deployment and ops docs, execution
  ledger.
- Sync to the company fork (`D:/code/data-platform`) with its branding
  preserved; rebuild and restart it (no data to keep).

## Out of scope for sub-project 1

Quantization, shared layout / `layout_hint` promotion, `search_batch`,
learned sparse vectors, hosted vendors — the last two are sub-projects 2 and 3.
