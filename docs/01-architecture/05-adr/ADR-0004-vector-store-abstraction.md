# ADR-0004 — Pluggable vector store behind one Protocol

**Status:** Accepted · **Date:** 2026-08-28 · **Deciders:** Architecture

## Context

`FR-C-03` makes the storage backend a **user-facing per-KB configuration option**, so an
abstraction is a product requirement, not just hygiene. Beyond that, the right backend genuinely
differs by deployment:

| Backend | Sweet spot | Cost |
| --- | --- | --- |
| pgvector | ≤ 5M vectors, single node, zero extra ops | Weaker filtered search; no native sparse vectors; CJK FTS needs `zhparser` |
| Qdrant | 5M–500M vectors, native sparse, quantization, sharding | One more service to operate |
| Elasticsearch | Deployments that already run it | Heavier; vector support is not its strength |

## Decision

Define one `VectorStore` Protocol in `M05`. All vector access — write and read — goes through it.

```python
class VectorStore(Protocol):
    async def ensure_namespace(self, ns: Namespace, spec: NamespaceSpec) -> None: ...
    async def upsert(self, ns: Namespace, points: Sequence[Point]) -> UpsertResult: ...
    async def delete(self, ns: Namespace, *, ids=None, filter=None) -> int: ...
    async def search(self, ns: Namespace, q: VectorQuery) -> list[Hit]: ...
    async def count(self, ns: Namespace, filter=None) -> int: ...
    async def drop_namespace(self, ns: Namespace) -> None: ...
    async def health(self) -> HealthStatus: ...
```

`Namespace = (kb_id, index_version)`. The physical layout — shared collection with a
tenant-partitioned payload index versus a dedicated collection — is a driver concern, invisible
to callers.

**Ship pgvector and Qdrant. Elasticsearch is a Phase-5 stretch.**

## Consequences

**Positive**
- Deployments choose their operational complexity. `small` needs only Postgres.
- Backend migration is a reindex into a new `Namespace` on a different binding, then a switch
  (ADR-0007) — no application change.
- Drivers are testable in isolation against one shared conformance suite.
- New backends are additive.

**Negative**
- The interface must be the **intersection** of backend capabilities, so backend-specific
  features need explicit accommodation. Handled by `NamespaceSpec.driver_options`, a typed
  per-driver extension point, with a documented capability matrix.
- Two implementations to maintain and keep behaviourally identical.

## Implementation requirements

1. **A shared conformance test suite** (`tests/contract/vectorstore/`) runs against every
   driver, including `FakeVectorStore`. A driver is not "done" until it passes. This is the
   single most important control on this abstraction.
2. Capability introspection: `driver.capabilities() -> Capabilities` declaring
   `sparse_vectors`, `quantization`, `payload_partitioning`, `filter_operators`. Callers must
   degrade explicitly, never silently.
3. Dimension validation on every write (`FR-G-04`). Mismatch raises
   `EMBEDDING_DIMENSION_MISMATCH` — never truncate or pad.
4. Filter expressions use a **driver-neutral AST** (`M05` §5) translated per driver, so filter
   semantics are identical across backends. Divergence here is silent wrong results.

## Alternatives rejected

| Alternative | Why not |
| --- | --- |
| pgvector only | Cannot meet `NFR-S-03` (10M+/KB) or `FR-C-03` |
| Qdrant only | Forces every deployment to run it; breaks the `small` preset |
| LangChain `VectorStore` | Leaky abstraction, unstable API, drags in a large dependency tree |
| Direct SQL/HTTP per call site | Backend choice becomes untestable and unswappable |
