"""Ingestion metrics with bounded labels; document text never enters labels."""

from prometheus_client import Counter, Histogram

from cairn.core.telemetry import REGISTRY

chunk_results = Counter(
    "cairn_ingestion_chunk_total",
    "Chunking calls by strategy and outcome.",
    ("strategy", "outcome"),
    registry=REGISTRY,
)
chunk_duration = Histogram(
    "cairn_ingestion_chunk_duration_seconds",
    "Document chunking duration.",
    ("strategy",),
    registry=REGISTRY,
)
