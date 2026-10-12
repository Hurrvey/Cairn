"""The retrieval contract options: filters, dedupe, MMR, highlights, explain, batch.

Runs the real service against the reference in-memory vector store, whose
filter semantics are the normative ones every driver is checked against.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from tests.fakes.vectorstore import FakeVectorStore
from tests.unit.retrieval.test_service import (
    ALLOWED_KB_ID,
    OTHER_KB_ID,
    Embeddings,
    Loader,
    principal,
    runtime,
)

from cairn.core.errors import InvalidId, UpstreamUnavailable, ValidationFailed
from cairn.core.ids import encode_id
from cairn.core.retrieval_runtime import KnowledgeBaseRuntime, RetrievalConfig
from cairn.embedding.sparse import encode_document
from cairn.retrieval.dto import BatchRequest, RetrievalRequest
from cairn.retrieval.errors import FilterRequiresRebuild
from cairn.retrieval.highlight import highlight_spans, query_terms
from cairn.retrieval.service import RetrievalService
from cairn.vectorstore.base import Namespace, NamespaceSpec, Point
from cairn.vectorstore.filters import Compare

DOC_A = UUID("a0000000-0000-0000-0000-00000000000a")
DOC_B = UUID("b0000000-0000-0000-0000-00000000000b")
DOC_C = UUID("c0000000-0000-0000-0000-00000000000c")
JAN = int(datetime(2026, 1, 15, tzinfo=UTC).timestamp())
JUN = int(datetime(2026, 6, 15, tzinfo=UTC).timestamp())


def v2(kb_id: UUID = ALLOWED_KB_ID, config: RetrievalConfig | None = None) -> KnowledgeBaseRuntime:
    return runtime(kb_id=kb_id, config=config).model_copy(update={"payload_schema": 2})


class Registry:
    def __init__(self, store: object) -> None:
        self.store = store

    async def for_binding(self, binding: object) -> object:
        return self.store


class RecordingStore(FakeVectorStore):
    def __init__(self) -> None:
        super().__init__()
        self.queries: list[object] = []

    async def search(self, ns, query):  # type: ignore[no-untyped-def]
        self.queries.append(query)
        return await super().search(ns, query)


async def corpus(kb_id: UUID = ALLOWED_KB_ID) -> RecordingStore:
    store = RecordingStore()
    ns = Namespace(kb_id, 1)
    await store.ensure_namespace(ns, NamespaceSpec(dim=3))
    rows = [
        (DOC_A, "Saturn has rings of ice.", {"lang": "en", "tags": ["space"]}, JAN, "a.pdf"),
        (
            DOC_A,
            "Saturn has rings of ice and rock.",
            {"lang": "en", "tags": ["space"]},
            JAN,
            "a.pdf",
        ),
        (
            DOC_B,
            "Saturn is a gas giant with many moons.",
            {"lang": "en", "version": 3},
            JUN,
            "b.md",
        ),
        (DOC_C, "土星有美丽的光环。Saturn", {"lang": "zh"}, JUN, "c.docx"),
    ]
    points = []
    for index, (document_id, content, meta, created, name) in enumerate(rows):
        chunk_id = uuid4()
        points.append(
            Point(
                id=chunk_id,
                dense=[1.0, 1.0 - index * 0.1, 0.5],
                sparse=encode_document(content),
                payload={
                    "chunk_id": str(chunk_id),
                    "document_id": str(document_id),
                    "kb_id": str(kb_id),
                    "index_version": 1,
                    "revision": 1,
                    "content": content,
                    "token_count": 8,
                    "page": index + 1,
                    "meta": meta,
                    "created_at": created,
                    "doc_title": name,
                    "doc_type": name.rsplit(".", 1)[1],
                    "doc_updated_at": created,
                },
            )
        )
    await store.upsert(ns, points)
    return store


def service(store: object, *runtimes: KnowledgeBaseRuntime) -> RetrievalService:
    return RetrievalService(
        runtime_loader=Loader({item.id: item for item in runtimes} or {ALLOWED_KB_ID: v2()}),
        vectors=Registry(store),
        embeddings=Embeddings(),
    )


def ask(**fields: object) -> RetrievalRequest:
    body: dict[str, object] = {
        "targets": [{"knowledge_base_id": encode_id("kb", ALLOWED_KB_ID)}],
        "query": "Saturn rings",
        "search_mode": "fulltext",
        "top_k": 10,
    }
    body.update(fields)
    return RetrievalRequest.model_validate(body)


async def run(store: object, request: RetrievalRequest, *runtimes: KnowledgeBaseRuntime):  # type: ignore[no-untyped-def]
    targets = runtimes or (v2(),)
    kb_ids = [item.id for item in targets]
    return await service(store, *targets).query(principal(*kb_ids), request, request_id="req_t")


# --- filters -----------------------------------------------------------------


@pytest.mark.anyio
async def test_timings_and_explain_include_inactive_stages() -> None:
    store = await corpus()
    response = await run(store, ask(options={"explain": True}))
    expected = {
        "embed",
        "dense",
        "sparse",
        "fuse",
        "filter",
        "rerank",
        "expand",
        "dedupe",
        "mmr",
        "budget",
    }
    assert expected <= response.usage.latency_ms.keys()
    stages = response.explain.stages
    assert expected <= {item["stage"] for item in stages}
    assert next(item for item in stages if item["stage"] == "rerank")["applied"] is False


@pytest.mark.anyio
@pytest.mark.parametrize("metadata,count", [({"$not": {}}, 0), ({"$or": [{}, {"lang": "xx"}]}, 4)])
async def test_empty_objects_keep_their_logical_meaning(metadata: dict, count: int) -> None:
    store = await corpus()
    result = await run(store, ask(query="Saturn", filters={"metadata": metadata}))
    assert len(result.results) == count


@pytest.mark.anyio
async def test_batch_resolves_default_mode_before_any_search() -> None:
    store = await corpus()
    retrieval = service(store, v2(config=RetrievalConfig(search_mode="fulltext")))
    request = ask(query=None, query_vector=[1.0, 0.0, 0.0], search_mode=None)
    batch = BatchRequest(queries=[ask(), request])
    with pytest.raises(ValidationFailed, match="query text"):
        await retrieval.batch(principal(ALLOWED_KB_ID), batch, request_id="req_b")
    assert store.queries == []


@pytest.mark.anyio
async def test_cancelled_encoding_waiter_does_not_cancel_other_batch_queries() -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    class SlowEmbeddings(Embeddings):
        async def embed_query(self, model, query):
            entered.set()
            await release.wait()
            return await super().embed_query(model, query)

    retrieval = service(None)
    retrieval._embeddings = SlowEmbeddings()
    memo = {}
    active = v2()

    async def encode():
        return await retrieval._encode_shared(
            memo, active.workspace_id, active.embedding_model, "Saturn", dense=True, sparse=False
        )

    first = asyncio.create_task(encode())
    await entered.wait()
    second = asyncio.create_task(encode())
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    assert (await second)[0] is not None


@pytest.mark.anyio
@pytest.mark.parametrize(
    "metadata",
    [{"$not": "x"}, {"$and": [1]}, {"$or": [None]}, {"$not": []}, {"$and": [False]}],
)
async def test_malformed_logical_filters_are_validation_errors(metadata: dict) -> None:
    store = await corpus()
    with pytest.raises(ValidationFailed, match="filter object"):
        await run(store, ask(filters={"metadata": metadata}))
    assert store.queries == []


@pytest.mark.anyio
async def test_metadata_filter_addresses_document_metadata_and_is_pushed_down() -> None:
    store = await corpus()

    response = await run(store, ask(filters={"metadata": {"lang": "en", "version": {"$gte": 3}}}))

    assert {hit.document_id for hit in response.results} == {encode_id("doc", DOC_B)}
    assert all(query.filter is not None for query in store.queries)  # type: ignore[attr-defined]
    assert "meta.lang" in repr(store.queries[0].filter)  # type: ignore[attr-defined]


@pytest.mark.anyio
async def test_array_metadata_and_date_filters() -> None:
    store = await corpus()

    tagged = await run(store, ask(filters={"metadata": {"tags": {"$in": ["space"]}}}))
    recent = await run(
        store, ask(filters={"created_after": "2026-03-01T00:00:00Z"}, query="Saturn")
    )

    assert {hit.document_id for hit in tagged.results} == {encode_id("doc", DOC_A)}
    assert {hit.document_id for hit in recent.results} == {
        encode_id("doc", DOC_B),
        encode_id("doc", DOC_C),
    }


@pytest.mark.anyio
async def test_document_id_filter_works_and_rejects_bad_ids() -> None:
    store = await corpus()

    response = await run(
        store, ask(query="Saturn", filters={"document_ids": [encode_id("doc", DOC_C)]})
    )

    assert {hit.document_id for hit in response.results} == {encode_id("doc", DOC_C)}
    with pytest.raises(InvalidId):
        await run(store, ask(filters={"document_ids": ["kb_not_a_document"]}))


@pytest.mark.anyio
async def test_naive_dates_and_unportable_conditions_are_caller_errors_before_search() -> None:
    store = await corpus()

    with pytest.raises(ValidationFailed, match="timezone"):
        await run(store, ask(filters={"created_after": "2026-03-01T00:00:00"}))
    with pytest.raises(ValidationFailed, match="needs a number"):
        await run(store, ask(filters={"metadata": {"lang": {"$gt": "a"}}}))
    with pytest.raises(ValidationFailed, match="field path"):
        await run(store, ask(filters={"metadata": {"a..b": 1}}))
    deep: dict[str, object] = {"x": 1}
    for _ in range(10):
        deep = {"$and": [deep]}
    with pytest.raises(ValidationFailed, match="deeper"):
        await run(store, ask(filters={"metadata": deep}))
    assert store.queries == []


@pytest.mark.anyio
async def test_old_indexes_refuse_metadata_filters_by_name_but_accept_document_ids() -> None:
    store = await corpus()
    legacy = runtime()

    with pytest.raises(FilterRequiresRebuild, match=encode_id("kb", ALLOWED_KB_ID)):
        await run(store, ask(filters={"metadata": {"lang": "en"}}), legacy)
    response = await run(
        store, ask(query="Saturn", filters={"document_ids": [encode_id("doc", DOC_B)]}), legacy
    )
    assert [hit.document_id for hit in response.results] == [encode_id("doc", DOC_B)]


# --- post-fusion stages ------------------------------------------------------


@pytest.mark.anyio
async def test_by_document_dedupe_keeps_the_first_hit_per_document() -> None:
    store = await corpus()

    everything = await run(store, ask(query="Saturn rings ice"))
    deduped = await run(store, ask(query="Saturn rings ice", options={"dedupe": "by_document"}))

    documents = [hit.document_id for hit in deduped.results]
    assert len(documents) == len(set(documents))
    assert deduped.results[0].chunk_id == everything.results[0].chunk_id
    assert deduped.degraded == []


@pytest.mark.anyio
async def test_mmr_replaces_a_near_duplicate_with_a_different_passage() -> None:
    store = await corpus()

    plain = await run(store, ask(query="Saturn rings ice rock", top_k=2))
    diverse = await run(
        store,
        ask(
            query="Saturn rings ice rock",
            top_k=2,
            options={"mmr": {"enabled": True, "lambda": 0.3}},
        ),
    )

    assert {hit.document_id for hit in plain.results} == {encode_id("doc", DOC_A)}
    assert len({hit.document_id for hit in diverse.results}) == 2
    assert all("mmr" in hit.scores for hit in diverse.results)


@pytest.mark.anyio
async def test_conflicting_kb_defaults_need_an_explicit_option() -> None:
    store = await corpus()
    first = v2(config=RetrievalConfig(dedupe="by_document", rerank={"enabled": False}))
    second = v2(OTHER_KB_ID, RetrievalConfig(dedupe="none", rerank={"enabled": False}))
    request = ask(
        targets=[
            {"knowledge_base_id": encode_id("kb", ALLOWED_KB_ID)},
            {"knowledge_base_id": encode_id("kb", OTHER_KB_ID)},
        ]
    )

    with pytest.raises(ValidationFailed, match=r"options.dedupe"):
        await run(store, request, first, second)


@pytest.mark.anyio
async def test_results_carry_document_metadata_and_citation_source() -> None:
    store = await corpus()

    response = await run(store, ask(query="gas giant"))

    hit = response.results[0]
    assert hit.metadata["version"] == 3
    assert hit.metadata["page"] == 3
    assert "meta" not in hit.metadata and "created_at" not in hit.metadata
    assert hit.source is not None
    assert (hit.source.title, hit.source.type) == ("b.md", "md")
    assert hit.source.updated_at == datetime.fromtimestamp(JUN, UTC)


# --- highlights --------------------------------------------------------------


def test_highlight_spans_are_code_point_offsets_into_the_original_text() -> None:
    content = "SATURN has rings; Saturnalia is a festival. 土星的光环"

    spans = highlight_spans(query_terms("saturn 光环 a"), content)

    assert [content[span["start"] : span["end"]] for span in spans] == ["SATURN", "光环"]


def test_highlight_terms_merge_overlaps_and_skip_single_letters() -> None:
    terms = query_terms("知识库 x")

    spans = highlight_spans(terms, "这是知识库。")

    assert "x" not in terms
    assert spans == [{"start": 2, "end": 5}]


@pytest.mark.anyio
async def test_highlights_are_returned_only_when_requested() -> None:
    store = await corpus()

    plain = await run(store, ask(query="gas giant"))
    marked = await run(store, ask(query="gas giant", options={"include_highlights": True}))

    assert plain.results[0].highlights == []
    content = marked.results[0].content
    assert [content[s["start"] : s["end"]] for s in marked.results[0].highlights] == [
        "gas",
        "giant",
    ]


# --- explain -----------------------------------------------------------------


@pytest.mark.anyio
async def test_explain_reports_the_plan_and_every_stage_that_ran() -> None:
    store = await corpus()

    silent = await run(store, ask())
    response = await run(
        store,
        ask(
            search_mode="hybrid",
            options={"explain": True, "dedupe": "by_document", "max_context_tokens": 1000},
        ),
    )

    assert silent.explain is None
    assert response.explain is not None
    stages = [stage["stage"] for stage in response.explain.stages]
    for expected in ("embed", "dense", "sparse", "fuse", "expand", "dedupe", "budget"):
        assert expected in stages
    sparse = next(stage for stage in response.explain.stages if stage["stage"] == "sparse")
    assert "saturn" in sparse["top"][0]["matched_terms"]
    assert response.explain.plan["targets"][0]["index_version"] == 1
    assert set(response.usage.latency_ms) >= {"embed", "search", "total"}


# --- batch -------------------------------------------------------------------


@pytest.mark.anyio
async def test_batch_rejects_a_malformed_query_before_running_any() -> None:
    store = await corpus()
    batch = BatchRequest.model_validate(
        {
            "queries": [
                ask().model_dump(by_alias=True),
                {
                    **ask().model_dump(by_alias=True),
                    "query_vector": [1.0],
                    "query": None,
                    "search_mode": "vector",
                },
            ]
        }
    )

    with pytest.raises(ValidationFailed, match="dimension"):
        await service(store, v2()).batch(principal(ALLOWED_KB_ID), batch, request_id="req_b")
    assert store.queries == []


@pytest.mark.anyio
async def test_batch_reports_a_failing_query_without_failing_the_others() -> None:
    store = await corpus()
    loader = Loader({ALLOWED_KB_ID: v2()}, failures={OTHER_KB_ID: RuntimeError("down")})
    retrieval = RetrievalService(
        runtime_loader=loader, vectors=Registry(store), embeddings=Embeddings()
    )
    batch = BatchRequest.model_validate(
        {
            "queries": [
                ask().model_dump(by_alias=True),
                {
                    **ask().model_dump(by_alias=True),
                    "targets": [{"knowledge_base_id": encode_id("kb", OTHER_KB_ID)}],
                },
            ]
        }
    )

    response = await retrieval.batch(
        principal(ALLOWED_KB_ID, OTHER_KB_ID), batch, request_id="req_b"
    )

    assert response.responses[0].results
    assert response.responses[1].results == []
    assert response.responses[1].partial_failures[0].code == UpstreamUnavailable.code


@pytest.mark.anyio
async def test_batch_encodes_each_distinct_query_once() -> None:
    store = await corpus()
    embeddings = Embeddings()
    retrieval = RetrievalService(
        runtime_loader=Loader({ALLOWED_KB_ID: v2()}),
        vectors=Registry(store),
        embeddings=embeddings,
    )
    same = ask(search_mode="vector").model_dump(by_alias=True)
    batch = BatchRequest.model_validate({"queries": [same, same, {**same, "query": "moons"}]})

    response = await retrieval.batch(principal(ALLOWED_KB_ID), batch, request_id="req_b")

    assert [query for _model, query in embeddings.calls].count("Saturn rings") == 1
    assert len(embeddings.calls) == 2
    assert response.usage.embedding_tokens == 4


def test_batch_refuses_explain_and_more_than_twenty_queries() -> None:
    with pytest.raises(ValueError, match="explain"):
        BatchRequest.model_validate(
            {"queries": [ask(options={"explain": True}).model_dump(by_alias=True)]}
        )
    with pytest.raises(ValueError):
        BatchRequest.model_validate({"queries": [ask().model_dump(by_alias=True)] * 21})


def test_filter_compiles_dates_to_epoch_seconds() -> None:
    retrieval = RetrievalService(runtime_loader=Loader({}), vectors=Registry(None))
    node = retrieval._build_filter(
        ask(filters={"created_before": "2026-06-15T00:00:00+00:00"}), [v2()]
    )

    assert node == Compare("created_at", "$lt", JUN)


@pytest.mark.anyio
async def test_explain_has_its_own_budget_and_batches_cost_per_query() -> None:
    from cairn.authz.ratelimit import RateLimiter
    from cairn.core.cache import InMemoryCache
    from cairn.core.errors import RateLimitExceeded

    limiter = RateLimiter(InMemoryCache(), default_rpm=10)
    key = principal(ALLOWED_KB_ID)

    await limiter.check_scoped(key, "explain", limit=2)
    await limiter.check_scoped(key, "explain", limit=2)
    with pytest.raises(RateLimitExceeded):
        await limiter.check_scoped(key, "explain", limit=2)
    # The explain budget is separate from the key's normal budget ...
    state = await limiter.check(key)
    assert state.remaining == 9
    # ... and a batch spends one unit per query.
    state = await limiter.check(key, cost=5)
    assert state.remaining == 4


# --- load shedding (M09 §4.10, TC-M09-31) -------------------------------------


def _loaded(monitor_ms: float):  # type: ignore[no-untyped-def]
    from cairn.retrieval.load import LoadMonitor

    monitor = LoadMonitor(min_samples=10)
    for _ in range(20):
        monitor.record(monitor_ms)
    return monitor


class _Reranker:
    def supports(self, model_id: str | None) -> bool:
        return True

    async def score(self, workspace_id, model_id, query, documents, *, timeout_s):  # type: ignore[no-untyped-def]
        return [1.0 for _ in documents]


@pytest.mark.anyio
async def test_under_load_rerank_is_skipped_and_reported() -> None:
    store = await corpus()
    retrieval = RetrievalService(
        runtime_loader=Loader({ALLOWED_KB_ID: v2()}),
        vectors=Registry(store),
        embeddings=Embeddings(),
        reranker=_Reranker(),
        slo_p95_ms=100,
        load_monitor=_loaded(90),
    )

    response = await retrieval.query(
        principal(ALLOWED_KB_ID),
        ask(rerank={"enabled": True, "model_id": "rr", "top_n": 3}),
        request_id="req_l",
    )

    assert [(n.stage, n.reason) for n in response.degraded] == [("rerank", "load_shed")]
    assert all("rerank" not in hit.scores for hit in response.results)


@pytest.mark.anyio
async def test_far_over_the_objective_the_candidate_pool_shrinks_too() -> None:
    store = await corpus()
    retrieval = RetrievalService(
        runtime_loader=Loader({ALLOWED_KB_ID: v2()}),
        vectors=Registry(store),
        embeddings=Embeddings(),
        slo_p95_ms=100,
        load_monitor=_loaded(200),
    )

    response = await retrieval.query(
        principal(ALLOWED_KB_ID), ask(candidate_k=500), request_id="req_l"
    )

    assert ("search", "load_shed") in {(n.stage, n.reason) for n in response.degraded}
    assert all(query.top_k == 50 for query in store.queries)  # type: ignore[attr-defined]


@pytest.mark.anyio
async def test_without_enough_samples_or_an_objective_nothing_is_shed() -> None:
    from cairn.retrieval.load import LoadMonitor

    store = await corpus()
    quiet = RetrievalService(
        runtime_loader=Loader({ALLOWED_KB_ID: v2()}),
        vectors=Registry(store),
        embeddings=Embeddings(),
        slo_p95_ms=100,
        load_monitor=LoadMonitor(min_samples=50),
    )
    off = RetrievalService(
        runtime_loader=Loader({ALLOWED_KB_ID: v2()}),
        vectors=Registry(store),
        embeddings=Embeddings(),
        load_monitor=_loaded(10_000),
    )

    for retrieval in (quiet, off):
        response = await retrieval.query(principal(ALLOWED_KB_ID), ask(), request_id="req_l")
        assert response.degraded == []


def test_the_load_monitor_reports_p95_of_recent_queries() -> None:
    from cairn.retrieval.load import LoadMonitor

    monitor = LoadMonitor(window=100, min_samples=10)
    assert monitor.p95() is None
    for value in range(1, 101):
        monitor.record(float(value))
    assert monitor.p95() == 95.0
    monitor.record(float("nan"))
    assert monitor.p95() == 95.0


def test_disabled_reranking_does_not_increment_load_shedding_metrics() -> None:
    from cairn.core.telemetry import retrieval_load_shed_total

    retrieval = service(None)
    retrieval._slo_p95_ms = 100
    retrieval.load = _loaded(90)
    counter = retrieval_load_shed_total.labels(level="rerank")
    before = counter._value.get()
    notices = []
    retrieval._shed_load(None, notices)
    assert notices == []
    assert counter._value.get() == before
