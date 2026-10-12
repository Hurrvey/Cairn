# M05 — Vector Store Drivers

| | |
| --- | --- |
| **Package** | `cairn.vectorstore` |
| **Layer** | L1 driver — importable by the **data plane** |
| **Phase** | 2 · Qdrant-only since 2026-09-24 ([ADR-0009](../01-architecture/05-adr/ADR-0009-qdrant-only-vector-backend.md)); quantization later |
| **Owner** | Backend eng. B |
| **Depends on** | M00 only |
| **Depended on by** | M03, M07, M09 ★, M11, M14 |
| **Tables owned** | none in the app schema; **owns the vector read model** (ADR-0005) |
| **Requirements owned** | FR-G-04, FR-G-05, FR-G-07, FR-G-10, FR-H-05, NFR-S-03, NFR-S-05, NFR-S-07 |

---

## 1. Purpose and scope

The only path to vector storage, for both write and read. Because M09 imports this module, it
is held to data-plane latency standards.

**In scope:** the `VectorStore` Protocol, the Qdrant driver, namespace management,
the driver-neutral filter AST, dimension validation, hybrid (dense + sparse) search, physical
layout strategy, quantization.

**Out of scope:** generating embeddings (M08), ranking policy and fusion (M09), deciding what to
index (M07).

---

## 2. Core types

```python
@dataclass(frozen=True)
class Namespace:
    kb_id: UUID
    index_version: int
    def key(self) -> str: return f"kb_{self.kb_id.hex}_v{self.index_version}"

@dataclass(frozen=True)
class NamespaceSpec:
    dim: int
    metric: Literal["cosine", "dot", "l2"]
    sparse: bool = True
    quantization: Literal["none", "scalar_int8", "binary"] = "none"
    layout_hint: Literal["auto", "shared", "dedicated"] = "auto"
    driver_options: Mapping[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class Point:
    id: UUID
    dense: Sequence[float]
    sparse: SparseVector | None
    payload: Mapping[str, Any]

@dataclass(frozen=True)
class VectorQuery:
    dense: Sequence[float] | None
    sparse: SparseVector | None
    top_k: int
    filter: FilterNode | None
    with_payload: bool = True
    ef_search: int | None = None            # accuracy/latency knob
    score_threshold: float | None = None

@dataclass(frozen=True)
class Hit:
    id: UUID
    score: float
    payload: Mapping[str, Any]              # includes denormalized `content` (ADR-0005)

@dataclass(frozen=True)
class Capabilities:
    sparse_vectors: bool
    quantization: frozenset[str]
    payload_partitioning: bool
    filter_operators: frozenset[str]
    max_top_k: int
```

---

## 3. Public interface

```python
class VectorStore(Protocol):
    def capabilities(self) -> Capabilities: ...
    async def ensure_namespace(self, ns: Namespace, spec: NamespaceSpec) -> None: ...
    async def drop_namespace(self, ns: Namespace) -> None: ...
    async def namespace_exists(self, ns: Namespace) -> bool: ...

    async def upsert(self, ns: Namespace, points: Sequence[Point]) -> UpsertResult: ...
    async def delete(self, ns: Namespace, *, ids: Sequence[UUID] | None = None,
                     filter: FilterNode | None = None) -> int: ...

    async def search(self, ns: Namespace, q: VectorQuery) -> list[Hit]: ...
    async def search_batch(self, ns: Namespace, qs: Sequence[VectorQuery]) -> list[list[Hit]]: ...
    async def fetch(self, ns: Namespace, ids: Sequence[UUID]) -> list[Hit]: ...

    async def count(self, ns: Namespace, filter: FilterNode | None = None) -> int: ...
    async def health(self) -> HealthStatus: ...

class VectorStoreRegistry:
    async def for_binding(self, binding: VectorBindingRef) -> VectorStore: ...
```

---

## 4. Dimension validation (`FR-G-04`) — non-negotiable

```python
async def upsert(self, ns, points) -> UpsertResult:
    spec = await self._namespace_spec(ns)
    for p in points:
        if len(p.dense) != spec.dim:
            raise EmbeddingDimensionMismatchError(
                f"Expected {spec.dim} dimensions, received {len(p.dense)}.",
                namespace=ns.key(), chunk_id=str(p.id))
    ...
```

**Never truncate. Never pad. Never coerce.** A dimension mismatch means the wrong embedding
model produced this vector; silently reshaping it creates exactly the invisible corruption
ADR-0006 exists to prevent. Fail loudly, at the write, where it is still debuggable.

---

## 5. Filter AST — driver-neutral (`FR-H-05`)

Filter semantics must be **identical** across drivers. Divergence here produces silently
different results depending on the backend, which is a correctness bug that no test the user
writes will catch.

```python
FilterNode = Union["And", "Or", "Not", "Compare", "Exists"]

@dataclass(frozen=True)
class Compare:
    field: str      # "kb_id" | "document_id" | "created_at" | "meta.lang" | "meta.tags"
    op: Literal["$eq","$ne","$in","$nin","$gt","$gte","$lt","$lte"]
    value: Any

@dataclass(frozen=True)
class And:    clauses: Sequence[FilterNode]
@dataclass(frozen=True)
class Or:     clauses: Sequence[FilterNode]
@dataclass(frozen=True)
class Not:    clause: FilterNode
@dataclass(frozen=True)
class Exists: field: str
```

Each driver implements `translate(node) -> native`. The shared conformance suite asserts
identical result sets across all drivers for the same corpus and filter — including the edge
cases that diverge in practice: `$in` on an array field (does it mean intersection?), `$ne` on
a missing field (does absence match?), and null handling.

**Normative semantics:**
- `$in` on an array field matches if the intersection is non-empty.
- `$ne` matches documents where the field is absent.
- `$exists` distinguishes absent from null.
- Numeric comparisons on a string field do not match; they do not error.

---

## 6. Physical layout strategy (`NFR-S-07`)

The non-obvious operational trap. Collection-per-KB is the intuitive design and it degrades
badly past a few hundred collections in Qdrant — each carries segment, thread, and file-handle
overhead. Most deployments have many small KBs plus a few large ones.

| KB size | Layout | Mechanism |
| --- | --- | --- |
| < 1M vectors | **shared** | One physical collection per `(workspace, dim, metric)`. Isolation by payload index on `kb_id` with `is_tenant: true`, so Qdrant physically groups tenant data and filtered search stays fast. |
| ≥ 1M vectors | **dedicated** | Its own collection. |

`Namespace` hides which is in use. Promotion from shared to dedicated is an operational action
(a reindex into a new namespace with `layout_hint="dedicated"`), not a schema migration.

`kb_index_version.layout` records the choice so the driver knows where to look.

> Every query, in **both** layouts, filters on `kb_id` and `index_version`. This is defence in
> depth: a KB is isolated even if the layout logic has a bug. Never rely on collection identity
> alone for tenant isolation.

---

## 7. Driver: pgvector (removed)

Removed on 2026-09-24 by [ADR-0009](../01-architecture/05-adr/ADR-0009-qdrant-only-vector-backend.md).
Migration `0013_qdrant` drops the extension and any per-namespace tables.

## 8. Driver: Qdrant (`cairn.vectorstore.qdrant`)

As built, which supersedes the shared-layout sketch in §6:

- **One collection per namespace** (`cairn_vec_{kb}_v{n}`). The sparse slot uses the `idf`
  modifier and Qdrant computes IDF per collection, so a shared collection would mix term
  statistics across knowledge bases. Dropping a version is `delete_collection`.
- Named vectors: `dense` (HNSW `m`/`ef_construct` from `driver_options`, `hnsw_ef` from
  `VectorQuery.ef_search`) and `sparse` (native sparse index, `modifier: idf`) — hybrid without a
  second system (`FR-G-05`).
- Lexical search is a sparse query: `cairn.embedding.sparse` (`bm25-jieba-v1`) turns chunk text
  into BM25 term-frequency weights at index time and query text into unit weights at query time.
  CJK runs use jieba search-mode segmentation; code tokens such as `XR-2200` are indexed whole and
  by their parts.
- `on_disk_payload: true` — the denormalized `content` costs disk, not RAM. Keyword payload indexes
  on `document_id` and `_paths`.
- Filters: `_paths` (every payload path present, hidden from readers) makes `$exists` exact;
  numeric equality is a closed range so `3 == 3.0`; ordered comparisons against non-numeric
  operands raise `UnsupportedFilter`. Known divergence: an ordered comparison against an array of
  numbers matches when any element does.
- Upserts use `wait=true` in batches of 256 so the index stage verifies its own writes.
- Scores are higher-is-better on every metric (Euclid distances are negated).
- Quantization (`FR-G-10`) remains future work: `scalar_int8` with `rescore: true` and
  `oversampling: 2.0` above 1M vectors.

### Capacity reference (1024-d)

| Mode | Bytes/vector | 1M | 10M |
| --- | --- | --- | --- |
| float32 + HNSW m=16 | ~4.2 KB | 4.2 GB | 42 GB |
| scalar int8 + rescore | ~1.1 KB | 1.1 GB | 11 GB |
| binary + rescore | ~0.15 KB | 0.15 GB | 1.5 GB |

A 16 GB Qdrant node comfortably serves ~10M vectors at int8.

---

## 9. Performance requirements

| Operation | Budget | Conditions |
| --- | --- | --- |
| `search` dense | **< 25 ms p95** | 1M vectors, top_k=100, `ef_search=100` |
| `search` sparse | < 25 ms p95 | same |
| `search` with filter | < 40 ms p95 | selectivity 1–10% |
| `fetch` by 100 IDs | < 10 ms p95 | |
| `upsert` batch of 256 | < 200 ms | ≥ 5000 chunks/s sustained (`NFR-P-08`) |
| `ensure_namespace` | < 2 s | |
| `drop_namespace` | < 30 s | 1M vectors |

Dense and sparse are issued **concurrently** by M09, so the hybrid cost is the max, not the sum.

---

## 10. Test requirements

The **conformance suite** (`tests/contract/vectorstore/`) is the primary control on this
module. It runs against Qdrant and `FakeVectorStore`. A driver is not done until it
passes.

| ID | Test |
| --- | --- |
| TC-M05-01 | upsert → search returns the point with correct payload |
| TC-M05-02 | **Dimension mismatch raises; nothing is written** |
| TC-M05-03 | Namespace isolation: identical vectors in two namespaces never cross |
| TC-M05-04 | **Every filter operator produces identical result sets across all drivers** |
| TC-M05-05 | `$in` on an array field: intersection semantics |
| TC-M05-06 | `$ne` on a missing field matches |
| TC-M05-07 | `$exists` distinguishes absent from null |
| TC-M05-08 | Nested `And`/`Or`/`Not` combinations |
| TC-M05-09 | Sparse search returns lexically-matching points a dense search misses |
| TC-M05-10 | `delete` by ID and by filter |
| TC-M05-11 | `drop_namespace` removes all points and leaves other namespaces intact |
| TC-M05-12 | Upsert of an existing ID replaces, does not duplicate |
| TC-M05-13 | `count` with and without a filter |
| TC-M05-14 | Backend errors surface as `UpstreamError` |
| TC-M05-15 | `search_batch` matches N sequential `search` calls |
| TC-M05-16 | Shared and dedicated layouts return identical results |
| TC-M05-17 | Quantized recall@10 ≥ 0.95 of unquantized on a 100k reference corpus |
| TC-M05-18 | Benchmark: 1M vectors, dense p95 < 25 ms |
| TC-M05-19 | Concurrent upsert during search returns no partial or torn reads |
| TC-M05-20 | 1000 KBs in shared layout: p95 stays within 20% of the 10-KB baseline |

Coverage target: **85%**; conformance suite must be 100% green on every driver.

---

## 11. Acceptance criteria

- [x] Conformance suite green on Qdrant and the fake (2026-09-24)
- [ ] Filter semantics identical across drivers (TC-M05-04..08)
- [ ] Benchmark TC-M05-18 met on the `medium` preset
- [ ] `capabilities()` accurate; callers degrade explicitly, never silently
- [ ] `FakeVectorStore` published and used by M09/M03/M07 tests
- [ ] Dimension mismatch is impossible to bypass

---

## 12. Task breakdown

| Task | Description | Est (d) | Deps |
| --- | --- | --- | --- |
| T-M05-01 | Protocol, core types, `Namespace`, `Capabilities` | 1.0 | |
| T-M05-02 | **Filter AST + normative semantics document** | 1.0 | T-M05-01 |
| T-M05-03 | `FakeVectorStore` (brute force, exact) | 1.0 | T-M05-02 |
| T-M05-04 | **Conformance suite skeleton** | 1.5 | T-M05-03 |
| T-M05-05 | pgvector driver: DDL, namespace management, upsert — removed by ADR-0009 | 2.0 | T-M05-02 |
| T-M05-06 | pgvector: dense search, HNSW tuning, `ef_search` — removed by ADR-0009 | 1.5 | T-M05-05 |
| T-M05-07 | pgvector: FTS sparse search + `zhparser` + score normalization — removed by ADR-0009 | 1.5 | T-M05-05 |
| T-M05-08 | pgvector: filter translation — removed by ADR-0009 | 1.0 | T-M05-02 |
| T-M05-09 | Registry, connection pooling, health | 0.5 | |
| T-M05-10 | Qdrant driver: collections, named vectors, tenant payload index | 2.0 | T-M05-02 |
| T-M05-11 | Qdrant: dense + sparse search, filter translation | 1.5 | T-M05-10 |
| T-M05-12 | Qdrant: quantization + `layout_hint` promotion | 1.0 | T-M05-10 |
| T-M05-13 | Benchmark harness + TC-M05-17..20 | 1.5 | all |
| T-M05-14 | Conformance tests TC-M05-01..16 | 2.0 | all |
| | **Total** | **19.0** (Phase 2: 11.5, Phase 5: 7.5) | |
