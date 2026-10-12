from __future__ import annotations

import httpx
import pytest
from tests.unit.retrieval.test_contract_options import ask, corpus, service, v2
from tests.unit.retrieval.test_http_composition import settings
from tests.unit.retrieval.test_service import ALLOWED_KB_ID, principal

from apps.api.main import create_app
from cairn.core.errors import ValidationFailed
from cairn.core.ids import encode_id
from cairn.retrieval.dify import DifyRequest, metadata_filter


def test_dify_caps_top_k_and_accepts_null_optional_fields():
    request = DifyRequest.model_validate(
        {
            "knowledge_id": "kb_x",
            "query": "text",
            "retrieval_setting": {"top_k": 1000, "score_threshold": None},
            "metadata_condition": None,
            "future_field": True,
        }
    )
    assert request.retrieval_setting.top_k == 100
    assert request.retrieval_setting.score_threshold is None


def test_dify_metadata_preserves_existence_and_refuses_substring_search():
    assert metadata_filter(
        {"conditions": [{"name": "team", "comparison_operator": "is not", "value": "ops"}]}
    ) == {"$and": [{"$and": [{"team": {"$ne": "ops"}}, {"team": {"$exists": True}}]}]}
    with pytest.raises(ValidationFailed, match="contains"):
        metadata_filter(
            {"conditions": [{"name": "team", "comparison_operator": "contains", "value": "ops"}]}
        )


async def test_fulltext_dify_scoring_keeps_ranking_and_adds_bounded_dense_scores():
    store = await corpus()
    retrieval = service(store)
    request = ask(query="Saturn", options={"expand_parent": False})
    native = await retrieval.query(principal(ALLOWED_KB_ID), request, request_id="req_n")
    response = await retrieval.query_external(principal(ALLOWED_KB_ID), request, request_id="req_d")
    assert [hit.chunk_id for hit in response.results] == [hit.chunk_id for hit in native.results]
    assert all(0 <= hit.scores["relevance"] <= 1 for hit in response.results)
    assert all(
        hit.score == old.score for hit, old in zip(response.results, native.results, strict=True)
    )
    assert store.queries[-1].ids


async def test_unnormalized_dot_scores_are_explicitly_refused():
    from dataclasses import replace

    active = v2()
    active = active.model_copy(
        update={
            "metric": "dot",
            "embedding_model": replace(active.embedding_model, normalize=False),
        }
    )
    retrieval = service(await corpus(), active)
    with pytest.raises(ValidationFailed, match="normalized"):
        await retrieval.query_external(principal(ALLOWED_KB_ID), ask(), request_id="req_d")


@pytest.mark.parametrize(
    "metric,native,expected", [("cosine", 0.7, 0.7), ("dot", 0.7, 0.7), ("l2", -1.0, 0.5)]
)
async def test_external_scores_convert_normalized_metrics(metric, native, expected):
    from cairn.retrieval.dto import RetrievalHit
    from cairn.retrieval.service import _Encodings

    active = v2().model_copy(update={"metric": metric})
    retrieval = service(await corpus(), active)
    prepared = await retrieval.prepare(principal(ALLOWED_KB_ID), ask())
    hits = [
        RetrievalHit(
            chunk_id="chk_test",
            document_id="doc_test",
            knowledge_base_id=encode_id("kb", active.id),
            content="passage",
            score=0.02,
            scores={"dense": native},
        )
    ]
    await retrieval._external_scores(hits, prepared, active, _Encodings({}, {}, 0, None, [], []))
    assert hits[0].scores["relevance"] == pytest.approx(expected)


async def test_external_runtime_lookup_is_after_authorization():
    from cairn.core.errors import PermissionDenied
    from cairn.retrieval.errors import KbRuntimeNotPublished

    class Missing:
        calls = 0

        async def load_one(self, *args):
            self.calls += 1
            raise KbRuntimeNotPublished()

    retrieval = service(None)
    loader = Missing()
    retrieval._runtime_loader = loader
    with pytest.raises(PermissionDenied):
        await retrieval.query_external(principal(), ask(), request_id="req_d")
    assert loader.calls == 0
    with pytest.raises(KbRuntimeNotPublished):
        await retrieval.query_external(principal(ALLOWED_KB_ID), ask(), request_id="req_d")


async def test_external_score_namespace_race_is_an_upstream_error():
    from cairn.core.errors import UpstreamUnavailable
    from cairn.vectorstore.errors import NamespaceNotFound

    store = await corpus()
    original = store.search

    async def search(ns, query):
        if query.ids is not None:
            raise NamespaceNotFound("retired")
        return await original(ns, query)

    store.search = search
    with pytest.raises(UpstreamUnavailable):
        await service(store).query_external(principal(ALLOWED_KB_ID), ask(), request_id="req_d")


@pytest.mark.parametrize("header,code", [(None, 1002), ("Basic bad", 1001), ("Bearer bad", 1002)])
async def test_dify_authentication_errors_use_compatibility_shape(header, code):
    app = create_app(settings("data"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        result = await client.post(
            "/retrieval",
            json={"knowledge_id": "", "query": ""},
            headers={"Authorization": header} if header else {},
        )
    assert result.status_code == 403
    assert result.json()["error_code"] == code


async def test_dify_response_normalizes_score_without_replacing_native_threshold(monkeypatch):
    from tests.unit.mcpserver.test_transport import HEADERS

    app = create_app(settings("data"))
    caller = principal(ALLOWED_KB_ID)

    class Auth:
        async def authenticate_api_key(self, token, *, ip=None):
            return caller

    monkeypatch.setattr("cairn.authz.dataplane.get_dataplane_authz", lambda: Auth())
    from cairn.authz.ratelimit import RateLimiter
    from cairn.core.cache import InMemoryCache

    monkeypatch.setattr(
        "cairn.authz.ratelimit.get_rate_limiter", lambda: RateLimiter(InMemoryCache())
    )
    app.state.retrieval_service = service(await corpus())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver", headers=HEADERS
    ) as client:
        probe = await client.post("/retrieval", json={"knowledge_id": "", "query": ""})
        assert probe.status_code == 400
        result = await client.post(
            "/retrieval",
            json={
                "knowledge_id": encode_id("kb", ALLOWED_KB_ID),
                "query": "Saturn",
                "retrieval_setting": {"top_k": 5, "score_threshold": 0.5},
            },
        )
    assert result.status_code == 200, result.text
    assert result.json()["records"]
    assert all(0.5 <= row["score"] <= 1 for row in result.json()["records"])
