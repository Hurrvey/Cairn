#!/usr/bin/env python
"""Fail if any route lacks an authorization dependency (T-OPS-08, FR-B-05).

Deny-by-default only holds if every route actually declares a guard. A single
handler that forgets one is invisible in review — it looks exactly like a
handler that is public on purpose — but is a complete authorization bypass for
whatever it exposes.

This walks the real route table rather than grepping source, so a route added
via any mechanism is covered.
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("CAIRN_DATABASE_URL", "postgresql+asyncpg://x:x@localhost:5432/x")
os.environ.setdefault("CAIRN_REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("CAIRN_MASTER_KEY", "dGVzdC1tYXN0ZXIta2V5LWZvci1jaS1vbmx5LTMyYnl0ZXM=")
os.environ.setdefault("CAIRN_ROLE", "all")
os.environ.setdefault("CAIRN_LOG_LEVEL", "ERROR")

from fastapi.routing import APIRoute

from apps.api.main import create_app
from cairn.identity.middleware import CREDENTIAL_SETUP_ALLOWLIST, PUBLIC_PATHS

#: Guard functions that establish a principal. A route depending on any of these
#: — directly or transitively — is considered protected.
GUARD_NAMES = frozenset(
    {
        "current_principal",
        "current_dataplane_principal",
        "current_mcp_principal",
        "dependency",  # the closure returned by require_permission / require_role
    }
)

#: Public on purpose. Anything not listed here must carry a guard.
INTENTIONALLY_PUBLIC = PUBLIC_PATHS | {
    "/metrics",
    "/v1/meta",
}


def _iter_api_routes(routes: object, seen: set[int] | None = None) -> list[APIRoute]:
    """Flatten the route tree.

    Recent FastAPI wraps `include_router` results rather than splicing routes
    into `app.routes`, so a flat scan silently sees almost nothing — which would
    make this check pass while auditing four routes.
    """
    seen = seen if seen is not None else set()
    found: list[APIRoute] = []
    for route in routes or []:  # type: ignore[union-attr]
        if id(route) in seen:
            continue
        seen.add(id(route))
        if isinstance(route, APIRoute):
            found.append(route)
            continue
        for attribute in ("routes", "original_router"):
            nested = getattr(route, attribute, None)
            if nested is None:
                continue
            found.extend(
                _iter_api_routes(
                    getattr(nested, "routes", nested) if attribute == "original_router" else nested,
                    seen,
                )
            )
    return found


def _has_guard(route: APIRoute) -> bool:
    for dependant in [route.dependant, *route.dependant.dependencies]:
        name = getattr(dependant.call, "__name__", "")
        if name in GUARD_NAMES:
            return True
        for nested in dependant.dependencies:
            if getattr(nested.call, "__name__", "") in GUARD_NAMES:
                return True
    return False


def main() -> int:
    app = create_app()
    routes = _iter_api_routes(app.routes)

    if len(routes) < 10:
        print(f"Route audit found only {len(routes)} routes — the walk is broken, not the app.")
        return 2

    unguarded: list[str] = []
    public: list[str] = []

    for route in routes:
        methods = ",".join(sorted(route.methods or {"GET"}))
        label = f"{methods:<12} {route.path}"
        if route.path in INTENTIONALLY_PUBLIC or route.path in CREDENTIAL_SETUP_ALLOWLIST:
            public.append(label)
            continue
        if not _has_guard(route):
            unguarded.append(label)

    print(f"Audited {len(routes)} routes: {len(public)} intentionally public.")

    if unguarded:
        print("\nRoutes with no authorization dependency:\n")
        for label in sorted(unguarded):
            print(f"  {label}")
        print(
            "\nAdd Depends(require_permission(...)) / Depends(current_principal), "
            "or add the path to PUBLIC_PATHS if it is public on purpose.\n"
        )
        return 1

    print("Every non-public route declares an authorization dependency.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
