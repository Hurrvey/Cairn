"""Health endpoints.

``/healthz`` is liveness and MUST NOT touch a dependency. If it did, a slow
database would trigger a container restart loop — turning a degradation into an
outage, which is precisely backwards.

``/readyz`` is readiness and checks dependencies. An unready replica is removed
from the load balancer but is not killed.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from fastapi import APIRouter, Request, Response
from sqlalchemy import text

from cairn.core.cache import get_cache
from cairn.core.config import get_settings
from cairn.core.db import session_scope
from cairn.core.logging import get_logger
from cairn.core.telemetry import readiness

__all__ = ["check_readiness", "router"]

log = get_logger(__name__)
router = APIRouter(tags=["health"])

CheckState = Literal["ok", "failed"]


async def _check_database() -> tuple[CheckState, str | None]:
    try:
        async with session_scope() as session:
            await asyncio.wait_for(session.execute(text("SELECT 1")), timeout=3.0)
        return "ok", None
    except Exception as exc:
        return "failed", type(exc).__name__


async def _check_cache() -> tuple[CheckState, str | None]:
    try:
        await asyncio.wait_for(get_cache().ping(), timeout=3.0)
        return "ok", None
    except Exception as exc:
        return "failed", type(exc).__name__


async def check_readiness() -> tuple[bool, dict[str, Any]]:
    database, cache = await asyncio.gather(_check_database(), _check_cache())
    checks = {
        "database": {"state": database[0], "error": database[1]},
        "cache": {"state": cache[0], "error": cache[1]},
    }
    for name, result in checks.items():
        readiness.labels(dependency=name).set(1 if result["state"] == "ok" else 0)
    healthy = all(result["state"] == "ok" for result in checks.values())
    return healthy, checks


@router.get("/healthz", summary="Liveness probe", include_in_schema=False)
async def healthz() -> dict[str, str]:
    """Process is alive. Deliberately performs no I/O."""
    return {"status": "ok"}


@router.get("/readyz", summary="Readiness probe", include_in_schema=False)
async def readyz(response: Response) -> dict[str, Any]:
    healthy, checks = await check_readiness()
    if not healthy:
        response.status_code = 503
        log.warning("health.not_ready", checks=checks)
    return {"status": "ok" if healthy else "degraded", "checks": checks}


@router.get("/v1/meta", summary="Public capability discovery", tags=["meta"])
async def meta(request: Request) -> dict[str, Any]:
    """Unauthenticated. Lets a client discover what this deployment supports
    rather than finding out via a 404."""
    settings = getattr(request.app.state, "settings", None) or get_settings()
    retrieval_available = settings.serves_data_plane
    return {
        "product": "Cairn",
        "version": "0.1.0",
        "api_version": "v1",
        "role": settings.role,
        "capabilities": {
            # Phase 0: only identity exists. Each lands with its module.
            "mcp": settings.serves_data_plane,
            "dify_compat": False,
            "retrieval_query": retrieval_available,
            "hybrid_search": retrieval_available,
            "weighted_fusion": retrieval_available,
            "parent_expansion": retrieval_available,
            "rerank": retrieval_available and bool(settings.retrieval.rerank_endpoints),
            "pipelines": False,
            "functions": False,
        },
        "docs_url": "https://docs.cairn.io",
    }
