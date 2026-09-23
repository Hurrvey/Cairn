# syntax=docker/dockerfile:1.7
#
# Multi-stage, non-root, digest-pinned (NFR-D-04, NFR-SEC-12).
#

# ---------------------------------------------------------------- builder
FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254 AS builder

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.9.5@sha256:f459f6f73a8c4ef5d69f4e6fbbdb8af751d6fa40ec34b39a1ab469acd6e289b7 /uv /usr/local/bin/uv

WORKDIR /build

# Dependency layer: cached unless the lockfile changes.
COPY pyproject.toml uv.lock ./
ENV UV_PROJECT_ENVIRONMENT=/opt/venv
RUN uv sync --locked --no-dev --no-install-project

# Project layer.
COPY src/ ./src/
COPY apps/ ./apps/
COPY migrations/ ./migrations/
COPY alembic.ini ./
COPY README.md ./
RUN VIRTUAL_ENV=/opt/venv uv pip install --no-deps .

# ---------------------------------------------------------------- runtime
FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254 AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app/src:/app \
    CAIRN_ROLE=all

RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        curl \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-chi-sim \
        tesseract-ocr-chi-tra \
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

CMD ["uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
