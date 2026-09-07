"""Vector storage.

One Protocol, several backends (ADR-0004). ``FR-C-03`` makes the backend a
user-facing per-knowledge-base choice, so the abstraction is a product
requirement rather than hygiene.

The piece that matters most here is **filter semantics**. If ``$in`` on an array
means intersection in one driver and equality in another, users get different
results depending on a setting they chose for operational reasons — a
correctness bug that no test *they* write will catch. Hence `filters.py`
declares the semantics normatively and the conformance suite asserts every
driver agrees.
"""

from cairn.vectorstore.base import (
    Capabilities,
    Hit,
    Namespace,
    NamespaceSpec,
    Point,
    SparseVector,
    UpsertResult,
    VectorQuery,
    VectorStore,
)
from cairn.vectorstore.errors import (
    DimensionMismatch,
    NamespaceNotFound,
    VectorStoreUnavailable,
)
from cairn.vectorstore.filters import (
    And,
    Compare,
    Exists,
    FilterNode,
    Not,
    Or,
    matches,
    parse_filter,
)

__all__ = [
    "And",
    "Capabilities",
    "Compare",
    "DimensionMismatch",
    "Exists",
    "FilterNode",
    "Hit",
    "Namespace",
    "NamespaceNotFound",
    "NamespaceSpec",
    "Not",
    "Or",
    "Point",
    "SparseVector",
    "UpsertResult",
    "VectorQuery",
    "VectorStore",
    "VectorStoreUnavailable",
    "matches",
    "parse_filter",
]
