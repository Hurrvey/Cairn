"""The vector store contract.

``Namespace = (kb_id, index_version)`` is the addressing unit. Everything above
this layer refers to a namespace; whether that maps to a dedicated table, a
shared collection with a tenant-partitioned payload index, or something else is
a driver concern (ADR-0007, NFR-S-07).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable
from uuid import UUID

from cairn.vectorstore.filters import FilterNode

__all__ = [
    "Capabilities",
    "HealthStatus",
    "Hit",
    "Metric",
    "Namespace",
    "NamespaceSpec",
    "Point",
    "Quantization",
    "SparseVector",
    "UpsertResult",
    "VectorQuery",
    "VectorStore",
]

#: Anything a driver may splice into SQL must match this. `Namespace.key()` is
#: built from a UUID hex and an integer, so it always does — the check exists to
#: keep that true if the construction ever changes.
_SAFE_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]{0,62}")

Metric = Literal["cosine", "dot", "l2"]
Quantization = Literal["none", "scalar_int8", "binary"]
Layout = Literal["auto", "shared", "dedicated"]


@dataclass(frozen=True, slots=True)
class Namespace:
    """One complete build of one knowledge base's index.

    Retrieval always reads the *active* version; a rebuild writes a new one and
    the switch is a single atomic UPDATE (ADR-0007). Callers never construct a
    namespace from "latest" — that is what would let a query see a half-built
    index.
    """

    kb_id: UUID
    index_version: int

    def key(self) -> str:
        """Stable physical identifier. 46 chars, well inside Postgres' 63.

        Validated on the way out. Drivers interpolate this into DDL and DML —
        an identifier cannot be a bind parameter — so the safety of that
        interpolation rests here. Checking it makes the guarantee mechanical
        rather than a comment asking the reader to trust the caller.
        """
        identifier = f"cairn_vec_{self.kb_id.hex}_v{self.index_version}"
        if not _SAFE_IDENTIFIER.fullmatch(identifier):  # pragma: no cover — unreachable
            raise ValueError(f"unsafe namespace identifier: {identifier!r}")
        return identifier

    def __str__(self) -> str:
        return self.key()


@dataclass(frozen=True, slots=True)
class SparseVector:
    """Term weights for lexical search. Indices are vocabulary positions."""

    indices: tuple[int, ...]
    values: tuple[float, ...]

    def __post_init__(self) -> None:
        if len(self.indices) != len(self.values):
            raise ValueError("sparse indices and values must be the same length")


@dataclass(frozen=True, slots=True)
class NamespaceSpec:
    dim: int
    metric: Metric = "cosine"
    sparse: bool = True
    quantization: Quantization = "none"
    layout_hint: Layout = "auto"
    #: Escape hatch for backend-specific tuning (HNSW m/ef_construction, text
    #: search configuration). Typed per driver, ignored by drivers that do not
    #: recognise a key.
    driver_options: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Point:
    """One indexed chunk.

    ``payload`` carries a denormalised copy of the chunk text (ADR-0005), which
    is what makes retrieval a single round trip instead of a search followed by
    a hydration query against PostgreSQL.
    """

    id: UUID
    dense: Sequence[float]
    payload: Mapping[str, Any]
    sparse: SparseVector | None = None


@dataclass(frozen=True, slots=True)
class VectorQuery:
    """One search against one namespace.

    Lexical search needs different inputs on different backends, and pretending
    otherwise would force one of them into a lossy conversion:

    * Qdrant takes a ``sparse`` vector of learned term weights.
    * pgvector uses PostgreSQL full-text, which needs the query ``text``.

    Callers supply whichever they have — usually both — and each driver uses
    what it can. ``Capabilities.sparse_vectors`` says which is honoured.
    """

    top_k: int = 10
    dense: Sequence[float] | None = None
    sparse: SparseVector | None = None
    #: Raw query text for full-text backends.
    text: str | None = None
    filter: FilterNode | None = None
    with_payload: bool = True
    #: Accuracy/latency knob. Higher searches more of the graph.
    ef_search: int | None = None
    score_threshold: float | None = None


@dataclass(frozen=True, slots=True)
class Hit:
    id: UUID
    score: float
    payload: Mapping[str, Any] = field(default_factory=dict)

    @property
    def content(self) -> str:
        value = self.payload.get("content")
        return value if isinstance(value, str) else ""


@dataclass(frozen=True, slots=True)
class UpsertResult:
    written: int
    namespace: str


@dataclass(frozen=True, slots=True)
class HealthStatus:
    healthy: bool
    detail: str | None = None
    latency_ms: float | None = None


@dataclass(frozen=True, slots=True)
class Capabilities:
    """What a driver can actually do.

    Callers degrade **explicitly** against this. Silently falling back from
    hybrid to dense-only would mean an evaluation run measuring something other
    than what it reported.
    """

    driver: str
    sparse_vectors: bool
    quantization: frozenset[str]
    payload_partitioning: bool
    filter_operators: frozenset[str]
    max_top_k: int
    #: True when dropping a namespace is O(1) rather than a bulk delete. Governs
    #: whether blue/green reindex is cheap on this backend.
    fast_namespace_drop: bool = True


@runtime_checkable
class VectorStore(Protocol):
    """Every read and write of vector data goes through this."""

    def capabilities(self) -> Capabilities: ...

    # --- namespace lifecycle ---
    async def ensure_namespace(self, ns: Namespace, spec: NamespaceSpec) -> None: ...
    async def namespace_exists(self, ns: Namespace) -> bool: ...
    async def list_namespaces(self, kb_id: UUID) -> Sequence[Namespace]: ...
    async def drop_namespace(self, ns: Namespace) -> None: ...

    # --- writes ---
    async def upsert(self, ns: Namespace, points: Sequence[Point]) -> UpsertResult: ...
    async def delete(
        self,
        ns: Namespace,
        *,
        ids: Sequence[UUID] | None = None,
        filter: FilterNode | None = None,
    ) -> int: ...

    # --- reads ---
    async def search(self, ns: Namespace, query: VectorQuery) -> list[Hit]: ...
    async def fetch(self, ns: Namespace, ids: Sequence[UUID]) -> list[Hit]: ...
    async def count(self, ns: Namespace, filter: FilterNode | None = None) -> int: ...

    async def health(self) -> HealthStatus: ...
    async def close(self) -> None: ...


def validate_dimensions(points: Sequence[Point], expected: int, namespace: str) -> None:
    """Reject a dimension mismatch loudly, at the write (FR-G-04).

    Shared by every driver so the check cannot be forgotten in one of them.
    """
    from cairn.vectorstore.errors import DimensionMismatch

    for point in points:
        actual = len(point.dense)
        if actual != expected:
            raise DimensionMismatch(
                f"Expected {expected}-dimensional vectors, received {actual}. "
                "This usually means a different embedding model produced this "
                "vector than the knowledge base was configured with.",
                namespace=namespace,
                chunk_id=str(point.id),
            )
