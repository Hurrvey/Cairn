"""Embedding metrics with bounded labels; never label with model ids or text."""

from prometheus_client import Counter, Histogram

from cairn.core.telemetry import REGISTRY

cache_requests = Counter(
    "cairn_embedding_cache_total", "Embedding cache outcomes.", ("outcome",), registry=REGISTRY
)
provider_requests = Counter(
    "cairn_embedding_provider_total",
    "Provider attempts.",
    ("purpose", "outcome"),
    registry=REGISTRY,
)
truncated_inputs = Counter(
    "cairn_embedding_truncated_total", "Inputs truncated to model limits.", registry=REGISTRY
)
duration = Histogram(
    "cairn_embedding_duration_seconds",
    "End-to-end embedding duration.",
    ("purpose",),
    buckets=(0.001, 0.005, 0.01, 0.02, 0.05, 0.1, 0.5, 2, 10, 30),
    registry=REGISTRY,
)
