"""Qdrant driver.

**One collection per namespace**, named ``cairn_vec_{kb_hex}_v{version}``.

Qdrant's usual multi-tenancy pattern — a shared collection partitioned by a
tenant payload index — is wrong here: the ``sparse`` slot uses the ``idf``
modifier, and Qdrant computes IDF over the *whole collection*, so a shared
collection would rank one knowledge base's terms by every other knowledge
base's vocabulary. A collection per namespace keeps term statistics per index
build and makes retiring a version ``delete_collection``, the O(1) drop that
blue/green reindex (ADR-0007) relies on. The cost is a ceiling of a few hundred
live namespaces per node (ADR-0009).

Filters follow ``vectorstore.filters`` exactly, with one recorded divergence:
an ordered comparison against an *array* of numbers matches when any element
does (the reference never matches arrays there). Qdrant cannot tell an absent
field from ``[]``, so every point carries a hidden ``_paths`` keyword array of
the payload paths it has; ``$exists`` matches against it, and it is stripped
from every payload this driver returns.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import httpx
from qdrant_client import AsyncQdrantClient, models
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse

from cairn.core.logging import get_logger
from cairn.vectorstore.base import (
    Capabilities,
    HealthStatus,
    Hit,
    Metric,
    Namespace,
    NamespaceSpec,
    Point,
    UpsertResult,
    VectorQuery,
    validate_dimensions,
)
from cairn.vectorstore.errors import (
    DimensionMismatch,
    NamespaceNotFound,
    UnsupportedCapability,
    UnsupportedFilter,
    VectorStoreUnavailable,
)
from cairn.vectorstore.filters import OPERATORS, And, Compare, Exists, FilterNode, Not, Or

__all__ = ["DENSE", "PATHS", "QDRANT_CAPABILITIES", "SPARSE", "QdrantVectorStore", "translate"]

log = get_logger(__name__)

DENSE = "dense"
SPARSE = "sparse"
PATHS = "_paths"
_BATCH = 256

_DISTANCE: dict[str, models.Distance] = {
    "cosine": models.Distance.COSINE,
    "dot": models.Distance.DOT,
    "l2": models.Distance.EUCLID,
}
_METRIC: dict[models.Distance, Metric] = {
    models.Distance.COSINE: "cosine",
    models.Distance.DOT: "dot",
    models.Distance.EUCLID: "l2",
}

QDRANT_CAPABILITIES = Capabilities(
    driver="qdrant",
    sparse_vectors=True,
    quantization=frozenset({"none"}),
    payload_partitioning=False,
    filter_operators=OPERATORS,
    max_top_k=1000,
    fast_namespace_drop=True,
)


@asynccontextmanager
async def _backend(namespace: str) -> AsyncIterator[None]:
    """Map client failures onto the vector store's error vocabulary."""
    try:
        yield
    except UnexpectedResponse as exc:
        if exc.status_code == 404:
            raise NamespaceNotFound(f"Vector namespace {namespace} does not exist.") from exc
        raise VectorStoreUnavailable(
            f"Qdrant rejected the request (HTTP {exc.status_code})."
        ) from exc
    except (ResponseHandlingException, httpx.TransportError, OSError) as exc:
        raise VectorStoreUnavailable("Qdrant is not reachable.") from exc


class QdrantVectorStore:
    def __init__(self, client: AsyncQdrantClient) -> None:
        self._client = client
        self._specs: dict[str, NamespaceSpec] = {}

    def capabilities(self) -> Capabilities:
        return QDRANT_CAPABILITIES

    # --- namespace lifecycle ------------------------------------------------

    async def ensure_namespace(self, ns: Namespace, spec: NamespaceSpec) -> None:
        name = ns.key()
        options = dict(spec.driver_options)
        async with _backend(name):
            if not await self._client.collection_exists(name):
                try:
                    await self._client.create_collection(
                        name,
                        vectors_config={
                            DENSE: models.VectorParams(
                                size=spec.dim,
                                distance=_DISTANCE[spec.metric],
                                hnsw_config=models.HnswConfigDiff(
                                    m=int(options.get("hnsw_m", 16)),
                                    ef_construct=int(options.get("hnsw_ef_construction", 128)),
                                ),
                            )
                        },
                        sparse_vectors_config=(
                            {SPARSE: models.SparseVectorParams(modifier=models.Modifier.IDF)}
                            if spec.sparse
                            else None
                        ),
                        on_disk_payload=True,
                    )
                except UnexpectedResponse:
                    # Two index workers ensuring the same namespace race here;
                    # losing the race is success, anything else is not.
                    if not await self._client.collection_exists(name):
                        raise
                for field in ("document_id", PATHS):
                    await self._client.create_payload_index(
                        name, field, field_schema=models.PayloadSchemaType.KEYWORD, wait=True
                    )
        self._specs[name] = spec
        log.info(
            "vectorstore.namespace_ready",
            driver="qdrant",
            namespace=name,
            dim=spec.dim,
            metric=spec.metric,
        )

    async def namespace_exists(self, ns: Namespace) -> bool:
        async with _backend(ns.key()):
            return bool(await self._client.collection_exists(ns.key()))

    async def list_namespaces(self, kb_id: UUID) -> tuple[Namespace, ...]:
        prefix = f"cairn_vec_{kb_id.hex}_v"
        async with _backend(prefix):
            response = await self._client.get_collections()
        versions = sorted(
            int(suffix)
            for collection in response.collections
            if collection.name.startswith(prefix)
            and (suffix := collection.name.removeprefix(prefix)).isdigit()
            and int(suffix) > 0
        )
        return tuple(Namespace(kb_id, version) for version in versions)

    async def drop_namespace(self, ns: Namespace) -> None:
        name = ns.key()
        async with _backend(name):
            if await self._client.collection_exists(name):
                await self._client.delete_collection(name)
        self._specs.pop(name, None)
        log.info("vectorstore.namespace_dropped", driver="qdrant", namespace=name)

    async def _spec_for(self, ns: Namespace) -> NamespaceSpec:
        name = ns.key()
        cached = self._specs.get(name)
        if cached is not None:
            return cached
        async with _backend(name):
            info = await self._client.get_collection(name)
        vectors = info.config.params.vectors
        dense = vectors.get(DENSE) if isinstance(vectors, Mapping) else None
        if dense is None:
            raise NamespaceNotFound(f"Vector namespace {name} has no dense vector.")
        spec = NamespaceSpec(
            dim=dense.size,
            metric=_METRIC[dense.distance],
            sparse=bool(info.config.params.sparse_vectors),
        )
        self._specs[name] = spec
        return spec

    # --- writes -------------------------------------------------------------

    async def upsert(self, ns: Namespace, points: Sequence[Point]) -> UpsertResult:
        name = ns.key()
        if not points:
            return UpsertResult(written=0, namespace=name)
        spec = await self._spec_for(ns)
        validate_dimensions(points, spec.dim, name)
        structs = [_point_struct(point, spec, name) for point in points]
        async with _backend(name):
            for offset in range(0, len(structs), _BATCH):
                await self._client.upsert(name, points=structs[offset : offset + _BATCH], wait=True)
        return UpsertResult(written=len(structs), namespace=name)

    async def delete(
        self,
        ns: Namespace,
        *,
        ids: Sequence[UUID] | None = None,
        filter: FilterNode | None = None,
    ) -> int:
        if ids is None and filter is None:
            raise ValueError("delete requires either ids or a filter")
        if ids is not None and not ids:
            return 0
        conditions: list[Any] = []
        if ids is not None:
            conditions.append(models.HasIdCondition(has_id=[str(point_id) for point_id in ids]))
        if filter is not None:
            conditions.append(translate(filter))
        selector = models.Filter(must=conditions)
        name = ns.key()
        async with _backend(name):
            matched = (await self._client.count(name, count_filter=selector, exact=True)).count
            if matched:
                await self._client.delete(
                    name, points_selector=models.FilterSelector(filter=selector), wait=True
                )
        return int(matched)

    # --- reads --------------------------------------------------------------

    async def search(self, ns: Namespace, query: VectorQuery) -> list[Hit]:
        if (query.dense is None) == (query.sparse is None):
            raise ValueError("search takes exactly one of a dense or a sparse query vector")
        name = ns.key()
        spec = await self._spec_for(ns)
        vector: Any
        if query.sparse is not None:
            if not spec.sparse:
                raise UnsupportedCapability(f"Vector namespace {name} has no sparse vectors.")
            if not query.sparse.indices:
                return []
            vector = models.SparseVector(
                indices=list(query.sparse.indices), values=list(query.sparse.values)
            )
            using = SPARSE
        else:
            assert query.dense is not None
            vector = [float(value) for value in query.dense]
            if len(vector) != spec.dim:
                raise DimensionMismatch(
                    f"Expected a {spec.dim}-dimensional query vector, received {len(vector)}.",
                    namespace=name,
                )
            using = DENSE
        async with _backend(name):
            response = await self._client.query_points(
                name,
                query=vector,
                using=using,
                query_filter=translate(query.filter) if query.filter is not None else None,
                search_params=(
                    models.SearchParams(hnsw_ef=query.ef_search)
                    if query.ef_search is not None
                    else None
                ),
                limit=min(query.top_k, QDRANT_CAPABILITIES.max_top_k),
                with_payload=query.with_payload,
            )
        # Normalise every metric to "higher is better" so callers and fusion
        # never need to know which backend produced a score.
        sign = -1.0 if using == DENSE and spec.metric == "l2" else 1.0
        hits = [
            Hit(
                id=UUID(str(scored.id)),
                score=sign * float(scored.score),
                payload=_public(scored.payload) if query.with_payload else {},
            )
            for scored in response.points
        ]
        if query.score_threshold is not None:
            hits = [hit for hit in hits if hit.score >= query.score_threshold]
        return hits

    async def fetch(self, ns: Namespace, ids: Sequence[UUID]) -> list[Hit]:
        if not ids:
            return []
        name = ns.key()
        async with _backend(name):
            records = await self._client.retrieve(
                name, ids=[str(point_id) for point_id in ids], with_payload=True
            )
        return [
            Hit(id=UUID(str(record.id)), score=0.0, payload=_public(record.payload))
            for record in records
        ]

    async def count(self, ns: Namespace, filter: FilterNode | None = None) -> int:
        name = ns.key()
        async with _backend(name):
            result = await self._client.count(
                name,
                count_filter=translate(filter) if filter is not None else None,
                exact=True,
            )
        return int(result.count)

    async def health(self) -> HealthStatus:
        started = time.perf_counter()
        try:
            await self._client.get_collections()
        except Exception as exc:
            return HealthStatus(healthy=False, detail=type(exc).__name__)
        return HealthStatus(healthy=True, latency_ms=(time.perf_counter() - started) * 1000)

    async def close(self) -> None:
        await self._client.close()


# --- points ------------------------------------------------------------------


def _paths(payload: Mapping[str, Any], prefix: str = "") -> Iterator[str]:
    for key, value in payload.items():
        path = f"{prefix}{key}"
        yield path
        if isinstance(value, Mapping):
            yield from _paths(value, f"{path}.")


def _point_struct(point: Point, spec: NamespaceSpec, namespace: str) -> models.PointStruct:
    # Round-trip through JSON so UUIDs and datetimes are stored as strings,
    # exactly as the read model has always stored them.
    payload: dict[str, Any] = json.loads(json.dumps(dict(point.payload), default=str))
    payload[PATHS] = sorted(set(_paths(payload)))
    vector: dict[str, Any] = {DENSE: [float(value) for value in point.dense]}
    if point.sparse is not None and point.sparse.indices:
        if not spec.sparse:
            raise UnsupportedCapability(f"Vector namespace {namespace} has no sparse vectors.")
        vector[SPARSE] = models.SparseVector(
            indices=list(point.sparse.indices), values=list(point.sparse.values)
        )
    return models.PointStruct(id=str(point.id), vector=vector, payload=payload)


def _public(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    return {key: value for key, value in (payload or {}).items() if key != PATHS}


# --- filter translation ------------------------------------------------------

#: Matches nothing: the negation of the empty filter, which matches everything.
_NOTHING = models.Filter(must_not=[models.Filter()])
_BOUND = {"$gt": "gt", "$gte": "gte", "$lt": "lt", "$lte": "lte"}


def translate(node: FilterNode) -> models.Filter:
    """Compile a filter AST to a Qdrant filter with the normative semantics.

    Every divergence from ``vectorstore.filters.matches`` is a correctness bug,
    which is why the conformance suite runs the same corpus through both.
    """
    if isinstance(node, And):
        return models.Filter(must=[translate(clause) for clause in node.clauses])
    if isinstance(node, Or):
        if not node.clauses:
            return _NOTHING
        return models.Filter(should=[translate(clause) for clause in node.clauses])
    if isinstance(node, Not):
        return models.Filter(must_not=[translate(node.clause)])
    if isinstance(node, Exists):
        present = _match(PATHS, node.field)
        return models.Filter(must=[present]) if node.present else models.Filter(must_not=[present])
    return _compare(node)


def _compare(node: Compare) -> models.Filter:
    if node.op == "$eq":
        return _eq(node.field, node.value)
    if node.op == "$ne":
        # must_not admits documents without the field: absent is "not equal".
        return models.Filter(must_not=[_eq(node.field, node.value)])
    if node.op in ("$in", "$nin"):
        any_of = (
            models.Filter(should=[_eq(node.field, value) for value in node.value])
            if node.value
            else _NOTHING
        )
        return any_of if node.op == "$in" else models.Filter(must_not=[any_of])
    value = node.value
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise UnsupportedFilter(
            f"Ordered comparison on {node.field!r} needs a number; Qdrant cannot "
            "order strings, booleans or dates."
        )
    return models.Filter(
        must=[models.FieldCondition(key=node.field, range=models.Range(**{_BOUND[node.op]: value}))]
    )


def _eq(field: str, value: Any) -> models.Filter:
    if value is None:
        return models.Filter(must=[models.IsNullCondition(is_null=models.PayloadField(key=field))])
    if isinstance(value, bool | str):
        return models.Filter(must=[_match(field, value)])
    if isinstance(value, int | float):
        # A range rather than a match: `3` must equal a stored `3.0`, as it
        # does in the reference implementation.
        return models.Filter(
            must=[models.FieldCondition(key=field, range=models.Range(gte=value, lte=value))]
        )
    raise UnsupportedFilter(f"Equality on {field!r} needs a string, number, boolean or null.")


def _match(field: str, value: bool | str) -> models.FieldCondition:
    return models.FieldCondition(key=field, match=models.MatchValue(value=value))
