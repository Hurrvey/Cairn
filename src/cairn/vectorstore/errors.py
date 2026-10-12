"""Vector store errors."""

from __future__ import annotations

from cairn.core.errors import NotFound, UpstreamUnavailable, ValidationFailed

__all__ = [
    "DimensionMismatch",
    "NamespaceNotFound",
    "NamespaceSpecMismatch",
    "UnsupportedCapability",
    "UnsupportedFilter",
    "VectorStoreTransient",
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


class NamespaceSpecMismatch(ValidationFailed):
    """An existing namespace was built with a different dimension, metric or sparse source."""

    code = "VECTOR_NAMESPACE_MISMATCH"
    title = "Vector namespace configuration mismatch"


class VectorStoreUnavailable(UpstreamUnavailable):
    """The vector store cannot serve the request as configured.

    Terminal by default: an unknown driver, a rejected request or bad
    credentials fail the same way on every retry.
    """

    code = "STORAGE_BINDING_UNAVAILABLE"
    title = "Vector store unavailable"
    retryable = False


class VectorStoreTransient(VectorStoreUnavailable):
    """A failure the same request may not hit again: the store was unreachable,
    overloaded (HTTP 5xx) or rate limiting (429). Ingestion retries these with
    backoff instead of failing the document."""

    retryable = True


class UnsupportedCapability(ValidationFailed):
    """The requested feature is not offered by this driver.

    Raised rather than silently degraded: a caller that asked for sparse search
    and got dense-only would be measuring the wrong thing.
    """

    code = "VECTOR_CAPABILITY_UNSUPPORTED"
    title = "Capability not supported by this vector store"


class UnsupportedFilter(UnsupportedCapability):
    """A filter this driver cannot evaluate with the normative semantics.

    Refused rather than approximated: an approximation would return a result set
    that differs from every other driver's, silently.
    """

    code = "VECTOR_FILTER_UNSUPPORTED"
    title = "Filter not supported by this vector store"
