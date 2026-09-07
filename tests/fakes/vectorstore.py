"""In-memory VectorStore, maintained by the M05 owner.

Brute-force and exact: it computes every distance rather than approximating, so
it is the *oracle* for ranking as well as filtering. Real drivers use ANN and
may legitimately differ in recall — but never in which documents a filter
admits, which is what the conformance suite pins down.

Filtering delegates to :func:`cairn.vectorstore.filters.matches`, the reference
implementation. A driver that disagrees with this fake has a bug.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import replace
from typing import Any
from uuid import UUID

from cairn.vectorstore.base import (
    Capabilities,
    HealthStatus,
    Hit,
    Namespace,
    NamespaceSpec,
    Point,
    UpsertResult,
    VectorQuery,
    validate_dimensions,
)
from cairn.vectorstore.errors import NamespaceNotFound
from cairn.vectorstore.filters import OPERATORS, FilterNode, matches

__all__ = ["FAKE_CAPABILITIES", "FakeVectorStore"]

FAKE_CAPABILITIES = Capabilities(
    driver="fake",
    sparse_vectors=True,
    quantization=frozenset({"none"}),
    payload_partitioning=True,
    filter_operators=OPERATORS,
    max_top_k=10_000,
    fast_namespace_drop=True,
)


class FakeVectorStore:
    def __init__(self) -> None:
        self._specs: dict[str, NamespaceSpec] = {}
        self._points: dict[str, dict[UUID, Point]] = {}

    def capabilities(self) -> Capabilities:
        return FAKE_CAPABILITIES

    # --- namespace lifecycle ------------------------------------------------

    async def ensure_namespace(self, ns: Namespace, spec: NamespaceSpec) -> None:
        self._specs.setdefault(ns.key(), spec)
        self._points.setdefault(ns.key(), {})

    async def namespace_exists(self, ns: Namespace) -> bool:
        return ns.key() in self._specs

    async def drop_namespace(self, ns: Namespace) -> None:
        self._specs.pop(ns.key(), None)
        self._points.pop(ns.key(), None)

    def _require(self, ns: Namespace) -> tuple[NamespaceSpec, dict[UUID, Point]]:
        key = ns.key()
        if key not in self._specs:
            raise NamespaceNotFound(f"Vector namespace {key} does not exist.")
        return self._specs[key], self._points[key]

    # --- writes -------------------------------------------------------------

    async def upsert(self, ns: Namespace, points: Sequence[Point]) -> UpsertResult:
        spec, store = self._require(ns)
        validate_dimensions(points, spec.dim, ns.key())
        for point in points:
            store[point.id] = replace(point, payload=dict(point.payload))
        return UpsertResult(written=len(points), namespace=ns.key())

    async def delete(
        self,
        ns: Namespace,
        *,
        ids: Sequence[UUID] | None = None,
        filter: FilterNode | None = None,
    ) -> int:
        if ids is None and filter is None:
            raise ValueError("delete requires either ids or a filter")
        _spec, store = self._require(ns)

        doomed = set(ids or [])
        if filter is not None:
            doomed |= {
                point_id
                for point_id, point in store.items()
                if matches(filter, point.payload) and (ids is None or point_id in set(ids))
            }
        removed = 0
        for point_id in doomed:
            if store.pop(point_id, None) is not None:
                removed += 1
        return removed

    # --- reads --------------------------------------------------------------

    async def search(self, ns: Namespace, query: VectorQuery) -> list[Hit]:
        spec, store = self._require(ns)

        candidates = [p for p in store.values() if matches(query.filter, p.payload)]

        scored: list[Hit] = []
        for point in candidates:
            if query.dense is not None:
                score = _score(query.dense, point.dense, spec.metric)
            elif query.text:
                score = _lexical(query.text, str(point.payload.get("content", "")))
                if score <= 0:
                    continue
            elif query.sparse is not None:
                score = _sparse_dot(query, point)
                if score <= 0:
                    continue
            else:
                raise ValueError("search requires a dense vector, sparse vector, or text")

            scored.append(
                Hit(
                    id=point.id,
                    score=score,
                    payload=dict(point.payload) if query.with_payload else {},
                )
            )

        scored.sort(key=lambda hit: (-hit.score, str(hit.id)))
        if query.score_threshold is not None:
            scored = [hit for hit in scored if hit.score >= query.score_threshold]
        return scored[: query.top_k]

    async def fetch(self, ns: Namespace, ids: Sequence[UUID]) -> list[Hit]:
        _spec, store = self._require(ns)
        return [
            Hit(id=pid, score=0.0, payload=dict(store[pid].payload)) for pid in ids if pid in store
        ]

    async def count(self, ns: Namespace, filter: FilterNode | None = None) -> int:
        _spec, store = self._require(ns)
        if filter is None:
            return len(store)
        return sum(1 for point in store.values() if matches(filter, point.payload))

    async def health(self) -> HealthStatus:
        return HealthStatus(healthy=True, latency_ms=0.0)

    async def close(self) -> None:
        return None

    # --- test affordances ---------------------------------------------------

    def snapshot(self, ns: Namespace) -> dict[UUID, dict[str, Any]]:
        _spec, store = self._require(ns)
        return {pid: dict(point.payload) for pid, point in store.items()}


# --- scoring -----------------------------------------------------------------
#
# Every metric is normalised so higher means better, matching the drivers. A
# caller must never need to know which backend produced a score.


def _score(query: Sequence[float], stored: Sequence[float], metric: str) -> float:
    if metric == "cosine":
        return _cosine(query, stored)
    if metric == "dot":
        return sum(a * b for a, b in zip(query, stored, strict=True))
    return -math.sqrt(sum((a - b) ** 2 for a, b in zip(query, stored, strict=True)))


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def _lexical(query: str, content: str) -> float:
    """Crude term overlap. Enough to assert that lexical search finds what dense
    search misses; not a claim to model BM25."""
    terms = {t for t in query.lower().split() if t}
    if not terms:
        return 0.0
    body = content.lower()
    hits = sum(1 for term in terms if term in body)
    return hits / len(terms)


def _sparse_dot(query: VectorQuery, point: Point) -> float:
    if query.sparse is None or point.sparse is None:
        return 0.0
    stored = dict(zip(point.sparse.indices, point.sparse.values, strict=True))
    return sum(
        weight * stored.get(index, 0.0)
        for index, weight in zip(query.sparse.indices, query.sparse.values, strict=True)
    )
