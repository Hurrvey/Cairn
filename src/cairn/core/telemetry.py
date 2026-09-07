"""Metrics and tracing.

Metric naming is ``cairn_<subsystem>_<measure>_<unit>``.

**Cardinality rule:** never label by ``principal_id``, ``document_id``,
``chunk_id``, or query text. A metric with unbounded label values will take down
Prometheus long before it tells you anything useful.
"""

from __future__ import annotations

from typing import Any

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from prometheus_client.openmetrics.exposition import CONTENT_TYPE_LATEST

from cairn.core.config import Settings
from cairn.core.logging import get_logger

__all__ = [
    "CONTENT_TYPE_LATEST",
    "REGISTRY",
    "auth_login_total",
    "bootstrap_total",
    "configure_tracing",
    "http_request_duration_seconds",
    "http_requests_total",
    "render_metrics",
]

log = get_logger(__name__)

REGISTRY = CollectorRegistry(auto_describe=True)

# --- HTTP --------------------------------------------------------------------

http_requests_total = Counter(
    "cairn_http_requests_total",
    "HTTP requests handled.",
    labelnames=("method", "route", "status", "role"),
    registry=REGISTRY,
)

http_request_duration_seconds = Histogram(
    "cairn_http_request_duration_seconds",
    "HTTP request duration.",
    labelnames=("method", "route", "role"),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.15, 0.3, 0.5, 1.0, 2.5, 5.0, 10.0),
    registry=REGISTRY,
)

# --- Identity ----------------------------------------------------------------

auth_login_total = Counter(
    "cairn_auth_login_total",
    "Login attempts by outcome.",
    labelnames=("outcome",),  # ok | password_change_required | failed | locked
    registry=REGISTRY,
)

bootstrap_total = Counter(
    "cairn_bootstrap_total",
    "Bootstrap attempts by outcome.",
    labelnames=("outcome",),  # created | already_initialized
    registry=REGISTRY,
)

# --- Tasks (NFR-S-06) --------------------------------------------------------
#
# KEDA autoscales workers on `queue_depth`. Backlog is the leading indicator; a
# consumer's CPU only rises after work is already delayed, so CPU-based scaling
# always responds one task-duration too late.

task_enqueued_total = Counter(
    "cairn_task_enqueued_total",
    "Tasks enqueued.",
    labelnames=("queue", "outcome"),  # enqueued | deduplicated
    registry=REGISTRY,
)

task_queue_depth = Gauge(
    "cairn_task_queue_depth",
    "Tasks in state=ready.",
    labelnames=("queue",),
    registry=REGISTRY,
)

task_oldest_ready_age_seconds = Gauge(
    "cairn_task_oldest_ready_age_seconds",
    "Age of the oldest ready task. The real starvation signal — depth alone can "
    "look healthy while one task sits behind a saturated workspace.",
    labelnames=("queue",),
    registry=REGISTRY,
)

task_running = Gauge(
    "cairn_task_running",
    "Tasks currently executing on this worker.",
    labelnames=("queue",),
    registry=REGISTRY,
)

task_duration_seconds = Histogram(
    "cairn_task_duration_seconds",
    "Task execution duration.",
    labelnames=("queue", "kind", "outcome"),
    buckets=(0.1, 0.5, 1, 5, 15, 30, 60, 300, 900, 1800),
    registry=REGISTRY,
)

task_attempts_total = Counter(
    "cairn_task_attempts_total",
    "Task execution attempts.",
    labelnames=("queue", "kind"),
    registry=REGISTRY,
)

task_lease_expired_total = Counter(
    "cairn_task_lease_expired_total",
    "Leases reclaimed by the reaper. Non-zero and rising means workers are "
    "dying mid-task — usually OOM on the parse or ocr queue.",
    labelnames=("queue",),
    registry=REGISTRY,
)

task_dead_lettered_total = Counter(
    "cairn_task_dead_lettered_total",
    "Tasks that exhausted retries or failed terminally.",
    labelnames=("queue", "error_code"),
    registry=REGISTRY,
)

# --- Authorization -----------------------------------------------------------

authz_cache_total = Counter(
    "cairn_authz_cache_total",
    "Principal resolution cache outcomes.",
    labelnames=("result",),  # hit | miss
    registry=REGISTRY,
)

authz_resolve_seconds = Histogram(
    "cairn_authz_resolve_seconds",
    "Principal resolution latency. Budget: < 5 ms p99 on a cache hit (NFR-P-06).",
    labelnames=("source",),  # cache | database
    buckets=(0.0005, 0.001, 0.002, 0.003, 0.005, 0.01, 0.025, 0.05, 0.1),
    registry=REGISTRY,
)

ratelimit_rejected_total = Counter(
    "cairn_ratelimit_rejected_total",
    "Requests rejected by the rate limiter.",
    labelnames=("principal_type",),
    registry=REGISTRY,
)

readiness = Gauge(
    "cairn_readiness",
    "1 when every dependency check passes.",
    labelnames=("dependency",),
    registry=REGISTRY,
)


def render_metrics() -> bytes:
    return generate_latest(REGISTRY)


def configure_tracing(settings: Settings, app: Any = None) -> None:
    """Install OpenTelemetry if an OTLP endpoint is configured.

    Tracing is optional: the ``otel`` extra may not be installed, and a missing
    collector must never prevent the process from starting.
    """
    endpoint = settings.telemetry.otlp_endpoint
    if not endpoint:
        return

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.sampling import TraceIdRatioBased
    except ImportError:
        log.warning(
            "telemetry.otel_unavailable",
            detail="OTLP endpoint configured but the 'otel' extra is not installed.",
        )
        return

    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": settings.telemetry.service_name,
                "service.version": "0.1.0",
                "deployment.environment": settings.environment,
                "cairn.role": settings.role,
            }
        ),
        sampler=TraceIdRatioBased(settings.telemetry.trace_sample_ratio),
    )
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces"))
    )
    trace.set_tracer_provider(provider)

    if app is not None:
        try:
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

            FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)
        except ImportError:  # pragma: no cover
            pass

    log.info("telemetry.tracing_enabled", endpoint=endpoint)
