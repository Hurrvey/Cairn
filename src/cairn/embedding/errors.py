"""Stable embedding failure codes for stage handlers and API adapters."""

from cairn.core.errors import CairnError


class EmbeddingError(CairnError):
    code = "EMBED_ERROR"
    title = "Embedding failed"
    http_status = 502
    retryable = False


class EmbeddingConfigurationError(EmbeddingError):
    code = "EMBED_CONFIG_INVALID"
    title = "Embedding configuration is incomplete or invalid."
    http_status = 422


class TokenizerUnavailable(EmbeddingConfigurationError):
    code = "EMBED_TOKENIZER_UNAVAILABLE"
    title = "The model tokenizer must be loaded before embedding."


class EmbeddingDimensionMismatch(EmbeddingError):
    code = "INDEX_DIMENSION_MISMATCH"
    title = "The provider returned an unexpected embedding dimension."


class EmbeddingInvalidVector(EmbeddingError):
    code = "EMBED_INVALID_VECTOR"
    title = "The provider returned an invalid embedding vector."


class EmbeddingProviderError(EmbeddingError):
    code = "EMBED_PROVIDER_ERROR"
    title = "The embedding provider is unavailable."
    retryable = True

    def __init__(self, *, retry_after: float | None = None) -> None:
        super().__init__()
        self.retry_after = retry_after


class EmbeddingProviderRejected(EmbeddingError):
    code = "EMBED_PROVIDER_REJECTED"
    title = "The provider rejected the embedding configuration or input."


class EmbeddingTimeout(EmbeddingProviderError):
    code = "EMBED_TIMEOUT"
    title = "The embedding request exceeded its deadline."
    http_status = 504


class EmbeddingCircuitOpen(EmbeddingProviderError):
    code = "EMBED_CIRCUIT_OPEN"
    title = "The embedding provider is temporarily unavailable."
    http_status = 503
