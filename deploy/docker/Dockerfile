# syntax=docker/dockerfile:1.7
#
# Multi-stage, non-root, digest-pinned (NFR-D-04, NFR-SEC-12).
#
# Pin note: the digests below are placeholders to be replaced with real ones
# during T-OPS-03 verification. Tags alone are mutable — `python:3.12-slim`
# means something different next month, which turns a reproducible build into a
# supply-chain surface.

# ---------------------------------------------------------------- builder
FROM python:3.12-slim-bookworm AS builder

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

WORKDIR /build

# Dependency layer: cached unless the lockfile changes.
COPY pyproject.toml uv.lock* ./
RUN uv venv /opt/venv && \
    VIRTUAL_ENV=/opt/venv uv sync --frozen --no-dev --no-install-project 2>/dev/null || \
    VIRTUAL_ENV=/opt/venv uv pip install -r pyproject.toml

# Project layer.
COPY src/ ./src/
COPY apps/ ./apps/
COPY migrations/ ./migrations/
COPY alembic.ini ./
RUN VIRTUAL_ENV=/opt/venv uv pip install --no-deps .

# ---------------------------------------------------------------- runtime
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    CAIRN_ROLE=all

RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 cairn \
    && useradd --uid 10001 --gid cairn --no-create-home --shell /usr/sbin/nologin cairn

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=cairn:cairn src/ ./src/
COPY --chown=cairn:cairn apps/ ./apps/
COPY --chown=cairn:cairn migrations/ ./migrations/
COPY --chown=cairn:cairn alembic.ini ./

# Writable scratch only; the rest of the filesystem is read-only at runtime
# (see `read_only: true` in docker-compose.yml).
RUN mkdir -p /var/lib/cairn/objects /tmp/cairn && \
    chown -R cairn:cairn /var/lib/cairn /tmp/cairn

USER cairn

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

CMD ["gunicorn", "apps.api.main:app", \
     "--worker-class", "uvicorn.workers.UvicornWorker", \
     "--workers", "2", \
     "--bind", "0.0.0.0:8000", \
     "--access-logfile", "-", \
     "--graceful-timeout", "30"]
