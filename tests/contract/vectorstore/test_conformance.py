"""Driver conformance — every VectorStore implementation, same assertions.

This is the primary control on ADR-0004. A driver is not "done" until it passes.

The fake runs everywhere; real drivers are skipped when their infrastructure is
unavailable, so the suite still gives useful signal on a laptop without Docker.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from tests.fakes.vectorstore import FakeVectorStore

from cairn.core.ids import new_uuid
from cairn.vectorstore.base import Namespace, NamespaceSpec, Point, VectorQuery
from cairn.vectorstore.errors import DimensionMismatch, NamespaceNotFound
from cairn.vectorstore.filters import parse_filter

DIM = 8


def vec(*seed: float) -> list[float]:
    """A DIM-length vector from a short prefix, padded with zeros."""
    return list(seed) + [0.0] * (DIM - len(seed))


# --- driver matrix -----------------------------------------------------------


async def _fake() -> AsyncIterator[object]:
    store = FakeVectorStore()
    yield store
    await store.close()


async def _pgvector() -> AsyncIterator[object]:
    if os.environ.get("CAIRN_TEST_USE_EXTERNAL_SERVICES") != "1" and not os.environ.get(
        "CAIRN_DATABASE_URL"
    ):
        pytest.skip("pgvector conformance needs a database")

    from sqlalchemy.ext.asyncio import create_async_engine

    from cairn.vectorstore.pgvector import PgVectorStore, ensure_extension

    url = os.environ.get(
        "CAIRN_DATABASE_URL", "postgresql+asyncpg://cairn:cairn@localhost:5432/cairn_test"
    )
    engine = create_async_engine(url)
    try:
        await ensure_extension(engine)
    except Exception as exc:
        await engine.dispose()
        pytest.skip(f"pgvector unavailable: {type(exc).__name__}")

    store = PgVectorStore(engine)
    yield store
    await engine.dispose()


DRIVERS = {"fake": _fake, "pgvector": _pgvector}


@pytest.fixture(params=sorted(DRIVERS))
async def store(request: pytest.FixtureRequest) -> AsyncIterator[object]:
    async for instance in DRIVERS[request.param]():
        yield instance


@pytest.fixture
async def namespace(store) -> AsyncIterator[Namespace]:  # type: ignore[no-untyped-def]
    ns = Namespace(kb_id=uuid4(), index_version=1)
    await store.ensure_namespace(ns, NamespaceSpec(dim=DIM, metric="cosine"))
    yield ns
    await store.drop_namespace(ns)


def point(payload: dict[str, object], dense: list[float] | None = None) -> Point:
    body = {"document_id": str(uuid4()), "content": "", **payload}
    return Point(id=new_uuid(), dense=dense or vec(1.0), payload=body)


# --- lifecycle ---------------------------------------------------------------


async def test_namespace_lifecycle(store) -> None:  # type: ignore[no-untyped-def]
    ns = Namespace(kb_id=uuid4(), index_version=1)
    assert await store.namespace_exists(ns) is False

    await store.ensure_namespace(ns, NamespaceSpec(dim=DIM))
    assert await store.namespace_exists(ns) is True

    # ensure is idempotent — reindex retries must not fail on an existing table.
    await store.ensure_namespace(ns, NamespaceSpec(dim=DIM))

    await store.drop_namespace(ns)
    assert await store.namespace_exists(ns) is False


async def test_drop_is_idempotent(store) -> None:  # type: ignore[no-untyped-def]
    ns = Namespace(kb_id=uuid4(), index_version=7)
    await store.drop_namespace(ns)


async def test_operations_on_a_missing_namespace_raise(store) -> None:  # type: ignore[no-untyped-def]
    ns = Namespace(kb_id=uuid4(), index_version=99)
    with pytest.raises(NamespaceNotFound):
        await store.upsert(ns, [point({})])


# --- writes ------------------------------------------------------------------


async def test_upsert_then_fetch_round_trips_the_payload(store, namespace) -> None:  # type: ignore[no-untyped-def]
    """TC-M05-01."""
    p = point({"content": "hello", "meta": {"lang": "en"}})
    await store.upsert(namespace, [p])

    hits = await store.fetch(namespace, [p.id])
    assert len(hits) == 1
    assert hits[0].id == p.id
    assert hits[0].payload["content"] == "hello"
    assert hits[0].payload["meta"]["lang"] == "en"


async def test_upsert_replaces_rather_than_duplicates(store, namespace) -> None:  # type: ignore[no-untyped-def]
    """TC-M05-12 — the property that makes the index stage idempotent."""
    p = point({"content": "first"})
    await store.upsert(namespace, [p])
    await store.upsert(
        namespace, [Point(id=p.id, dense=p.dense, payload={**p.payload, "content": "second"})]
    )

    assert await store.count(namespace) == 1
    hits = await store.fetch(namespace, [p.id])
    assert hits[0].payload["content"] == "second"


async def test_dimension_mismatch_raises_and_writes_nothing(store, namespace) -> None:  # type: ignore[no-untyped-def]
    """TC-M05-02 / FR-G-04.

    Never truncate or pad. A mismatch means a different embedding model produced
    this vector, and silently reshaping it is the invisible corruption ADR-0006
    exists to prevent.
    """
    bad = Point(id=new_uuid(), dense=[1.0, 2.0], payload={"content": "x"})
    with pytest.raises(DimensionMismatch):
        await store.upsert(namespace, [bad])
    assert await store.count(namespace) == 0


async def test_empty_upsert_is_a_noop(store, namespace) -> None:  # type: ignore[no-untyped-def]
    result = await store.upsert(namespace, [])
    assert result.written == 0


# --- isolation ---------------------------------------------------------------


async def test_namespaces_are_isolated(store) -> None:  # type: ignore[no-untyped-def]
    """TC-M05-03 — blue/green depends on this absolutely.

    A query against the active version must never see a point written to the
    version being built.
    """
    kb = uuid4()
    v1, v2 = Namespace(kb, 1), Namespace(kb, 2)
    await store.ensure_namespace(v1, NamespaceSpec(dim=DIM))
    await store.ensure_namespace(v2, NamespaceSpec(dim=DIM))
    try:
        shared_id = new_uuid()
        await store.upsert(v1, [Point(id=shared_id, dense=vec(1.0), payload={"content": "v1"})])
        await store.upsert(v2, [Point(id=shared_id, dense=vec(1.0), payload={"content": "v2"})])

        assert (await store.fetch(v1, [shared_id]))[0].payload["content"] == "v1"
        assert (await store.fetch(v2, [shared_id]))[0].payload["content"] == "v2"
        assert await store.count(v1) == 1

        await store.drop_namespace(v1)
        assert await store.count(v2) == 1
    finally:
        await store.drop_namespace(v1)
        await store.drop_namespace(v2)


# --- search ------------------------------------------------------------------


async def test_dense_search_ranks_by_similarity(store, namespace) -> None:  # type: ignore[no-untyped-def]
    near = Point(id=new_uuid(), dense=vec(1.0, 0.0), payload={"content": "near"})
    far = Point(id=new_uuid(), dense=vec(0.0, 1.0), payload={"content": "far"})
    await store.upsert(namespace, [near, far])

    hits = await store.search(namespace, VectorQuery(dense=vec(1.0, 0.05), top_k=2))
    assert [h.payload["content"] for h in hits] == ["near", "far"]
    assert hits[0].score > hits[1].score


async def test_top_k_bounds_the_result_set(store, namespace) -> None:  # type: ignore[no-untyped-def]
    await store.upsert(
        namespace,
        [
            Point(id=new_uuid(), dense=vec(1.0, i / 10), payload={"content": str(i)})
            for i in range(10)
        ],
    )
    hits = await store.search(namespace, VectorQuery(dense=vec(1.0), top_k=3))
    assert len(hits) == 3


async def test_score_threshold_excludes_weak_matches(store, namespace) -> None:  # type: ignore[no-untyped-def]
    await store.upsert(
        namespace,
        [
            Point(id=new_uuid(), dense=vec(1.0, 0.0), payload={"content": "near"}),
            Point(id=new_uuid(), dense=vec(-1.0, 0.0), payload={"content": "opposite"}),
        ],
    )
    hits = await store.search(
        namespace, VectorQuery(dense=vec(1.0, 0.0), top_k=10, score_threshold=0.5)
    )
    assert [h.payload["content"] for h in hits] == ["near"]


async def test_with_payload_false_omits_the_payload(store, namespace) -> None:  # type: ignore[no-untyped-def]
    p = point({"content": "hidden"})
    await store.upsert(namespace, [p])
    hits = await store.search(namespace, VectorQuery(dense=vec(1.0), top_k=1, with_payload=False))
    assert hits[0].payload == {}


async def test_lexical_search_finds_what_dense_search_misses(store, namespace) -> None:  # type: ignore[no-untyped-def]
    """TC-M05-09 — the reason hybrid exists.

    An exact product code or acronym is precisely where embeddings are weak.
    """
    await store.upsert(
        namespace,
        [
            Point(
                id=new_uuid(),
                dense=vec(0.0, 1.0),
                payload={"content": "The XR-2200 controller requires firmware 4.1."},
            ),
            Point(
                id=new_uuid(),
                dense=vec(1.0, 0.0),
                payload={"content": "General information about hardware maintenance."},
            ),
        ],
    )
    hits = await store.search(namespace, VectorQuery(text="XR-2200", top_k=5))
    assert len(hits) >= 1
    assert "XR-2200" in hits[0].payload["content"]


# --- filtering ---------------------------------------------------------------

CORPUS = [
    {"content": "alpha", "meta": {"lang": "en", "tags": ["security", "ops"], "version": 3}},
    {"content": "bravo", "meta": {"lang": "fr", "tags": ["ops"], "version": 5}},
    {"content": "charlie", "meta": {"lang": "en", "tags": ["legal"], "version": 1}},
    {"content": "delta", "meta": {"lang": "en"}},  # no tags, no version
]


@pytest.fixture
async def corpus(store, namespace) -> dict[str, UUID]:  # type: ignore[no-untyped-def]
    ids: dict[str, UUID] = {}
    points = []
    for row in CORPUS:
        p = point(dict(row))
        ids[str(row["content"])] = p.id
        points.append(p)
    await store.upsert(namespace, points)
    return ids


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ({"meta.lang": "en"}, {"alpha", "charlie", "delta"}),
        ({"meta.lang": {"$ne": "en"}}, {"bravo"}),
        # $ne matches the document that has no `version` at all.
        ({"meta.version": {"$ne": 3}}, {"bravo", "charlie", "delta"}),
        ({"meta.tags": {"$in": ["ops"]}}, {"alpha", "bravo"}),
        ({"meta.tags": {"$in": ["legal", "security"]}}, {"alpha", "charlie"}),
        ({"meta.tags": {"$nin": ["ops"]}}, {"charlie", "delta"}),
        ({"meta.version": {"$gte": 3}}, {"alpha", "bravo"}),
        ({"meta.version": {"$lt": 3}}, {"charlie"}),
        ({"meta.version": {"$exists": True}}, {"alpha", "bravo", "charlie"}),
        ({"meta.version": {"$exists": False}}, {"delta"}),
        ({"meta.lang": "en", "meta.version": {"$gte": 3}}, {"alpha"}),
        ({"$or": [{"meta.lang": "fr"}, {"meta.tags": {"$in": ["legal"]}}]}, {"bravo", "charlie"}),
        ({"$not": {"meta.lang": "en"}}, {"bravo"}),
        # A numeric comparison against a string field matches nothing and errors nothing.
        ({"meta.lang": {"$gt": 3}}, set()),
    ],
)
async def test_filters_agree_across_drivers(  # type: ignore[no-untyped-def]
    store, namespace, corpus, spec: dict[str, object], expected: set[str]
) -> None:
    """TC-M05-04 — the test that makes the abstraction safe.

    Divergence here is a correctness bug users cannot detect: each backend is
    individually self-consistent, so results simply differ depending on a
    storage choice made for operational reasons.
    """
    hits = await store.search(
        namespace, VectorQuery(dense=vec(1.0), top_k=50, filter=parse_filter(spec))
    )
    assert {hit.payload["content"] for hit in hits} == expected


async def test_count_honours_filters(store, namespace, corpus) -> None:  # type: ignore[no-untyped-def]
    """TC-M05-13."""
    assert await store.count(namespace) == 4
    assert await store.count(namespace, parse_filter({"meta.lang": "en"})) == 3


# --- deletes -----------------------------------------------------------------


async def test_delete_by_ids(store, namespace, corpus) -> None:  # type: ignore[no-untyped-def]
    """TC-M05-10."""
    removed = await store.delete(namespace, ids=[corpus["alpha"]])
    assert removed == 1
    assert await store.count(namespace) == 3
    assert await store.fetch(namespace, [corpus["alpha"]]) == []


async def test_delete_by_filter(store, namespace, corpus) -> None:  # type: ignore[no-untyped-def]
    """TC-M05-10 — how a document's chunks are removed on re-ingest."""
    removed = await store.delete(namespace, filter=parse_filter({"meta.lang": "en"}))
    assert removed == 3
    assert await store.count(namespace) == 1


async def test_delete_requires_a_selector(store, namespace) -> None:  # type: ignore[no-untyped-def]
    """Deleting everything must be an explicit drop_namespace, never an
    accidental no-argument call."""
    with pytest.raises(ValueError, match="ids or a filter"):
        await store.delete(namespace)


async def test_delete_of_unknown_ids_is_zero_not_an_error(store, namespace) -> None:  # type: ignore[no-untyped-def]
    assert await store.delete(namespace, ids=[new_uuid()]) == 0


# --- capabilities and health -------------------------------------------------


async def test_capabilities_are_declared(store) -> None:  # type: ignore[no-untyped-def]
    """Callers degrade explicitly against these. Silently falling back from
    hybrid to dense-only would make an evaluation run measure the wrong thing."""
    caps = store.capabilities()
    assert caps.driver
    assert caps.max_top_k > 0
    assert "$eq" in caps.filter_operators
    assert isinstance(caps.sparse_vectors, bool)


async def test_health_reports(store) -> None:  # type: ignore[no-untyped-def]
    status = await store.health()
    assert status.healthy is True
