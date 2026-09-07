"""Vector store errors."""

from __future__ import annotations

from cairn.core.errors import NotFound, UpstreamUnavailable, ValidationFailed

__all__ = [
    "DimensionMismatch",
    "NamespaceNotFound",
    "UnsupportedCapability",
    "VectorStoreUnavailable",
]


class DimensionMismatch(ValidationFailed):
    """Raised when a vector's length disagrees with the namespace (FR-G-04).

    Never truncate or pad to make it fit. A mismatch means a different embedding
    model produced this vector, and silently reshaping it creates exactly the
    invisible corruption ADR-0006 exists to prevent — scores stay plausible and
    retrieval is just quietly wrong.
    """

    code = "EMBEDDING_DIMENSION_MISMATCH"
    title = "Embedding dimension mismatch"


class NamespaceNotFound(NotFound):
    code = "VECTOR_NAMESPACE_NOT_FOUND"
    title = "Vector namespace not found"


class VectorStoreUnavailable(UpstreamUnavailable):
    code = "STORAGE_BINDING_UNAVAILABLE"
    title = "Vector store unavailable"


class UnsupportedCapability(ValidationFailed):
    """The requested feature is not offered by this driver.

    Raised rather than silently degraded: a caller that asked for sparse search
    and got dense-only would be measuring the wrong thing.
    """

    code = "VECTOR_CAPABILITY_UNSUPPORTED"
    title = "Capability not supported by this vector store"
