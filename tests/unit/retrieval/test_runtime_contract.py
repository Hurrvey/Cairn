from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import UUID

import pytest

from cairn.catalog.config import RetrievalConfig as CatalogRetrievalConfig
from cairn.catalog.dto import BindingRefModel as CatalogBindingRefModel
from cairn.catalog.dto import KnowledgeBaseRuntime as CatalogKnowledgeBaseRuntime
from cairn.core.modelref import ModelRef
from cairn.core.retrieval_runtime import BindingRefModel, KnowledgeBaseRuntime, RetrievalConfig
from cairn.modelgw.catalog import ModelCatalog

MODEL_ID = UUID("10000000-0000-0000-0000-000000000001")
PROVIDER_ID = UUID("20000000-0000-0000-0000-000000000002")
KB_ID = UUID("30000000-0000-0000-0000-000000000003")
WORKSPACE_ID = UUID("40000000-0000-0000-0000-000000000004")
BINDING_ID = UUID("50000000-0000-0000-0000-000000000005")


def test_catalog_runtime_types_are_backward_compatible_exports() -> None:
    assert CatalogRetrievalConfig is RetrievalConfig
    assert CatalogBindingRefModel is BindingRefModel
    assert CatalogKnowledgeBaseRuntime is KnowledgeBaseRuntime


def test_model_ref_preserves_historical_positional_field_order() -> None:
    model = ModelRef(MODEL_ID, "tei", "legacy", "embedding", 3, 128, False)

    assert model.dimension == 3
    assert model.max_input_tokens == 128
    assert model.normalize is False
    assert model.provider_id is None


def test_old_runtime_json_without_provider_id_still_deserializes() -> None:
    raw = (
        '{"id":"30000000-0000-0000-0000-000000000003",'
        '"workspace_id":"40000000-0000-0000-0000-000000000004",'
        '"index_version":1,"embedding_model":{'
        '"id":"10000000-0000-0000-0000-000000000001",'
        '"provider_family":"tei","model_key":"legacy","capability":"embedding",'
        '"dimension":3,"max_input_tokens":128,"normalize":true,"query_prefix":null,'
        '"optimal_batch_size":16,"tokenizer_id":"legacy-tokenizer"},'
        '"metric":"cosine","vector_binding":{'
        '"id":"50000000-0000-0000-0000-000000000005","driver":"qdrant",'
        '"config":{}},"retrieval_config":{},"config_version":1,"status":"active"}'
    )

    runtime = KnowledgeBaseRuntime.model_validate_json(raw)

    assert runtime.embedding_model.provider_id is None
    dumped = json.loads(runtime.model_dump_json(exclude_none=True))
    assert "provider_id" not in dumped["embedding_model"]
    assert KnowledgeBaseRuntime.model_validate(dumped) == runtime


def test_new_runtime_json_preserves_provider_identity() -> None:
    runtime = KnowledgeBaseRuntime(
        id=KB_ID,
        workspace_id=WORKSPACE_ID,
        index_version=2,
        embedding_model=ModelRef(
            id=MODEL_ID,
            provider_id=PROVIDER_ID,
            provider_family="tei",
            model_key="current",
            capability="embedding",
            dimension=3,
            max_input_tokens=128,
            tokenizer_id="current-tokenizer",
        ),
        metric="cosine",
        vector_binding=BindingRefModel(id=BINDING_ID, driver="qdrant"),
        retrieval_config=RetrievalConfig(),
        config_version=2,
        status="indexing",
    )

    restored = KnowledgeBaseRuntime.model_validate_json(runtime.model_dump_json())

    assert restored == runtime
    assert restored.embedding_model.provider_id == PROVIDER_ID


@pytest.mark.anyio
async def test_model_catalog_ref_populates_provider_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = SimpleNamespace(
        id=MODEL_ID,
        provider_id=PROVIDER_ID,
        model_key="current",
        capability="embedding",
        dimension=3,
        max_input_tokens=128,
        normalize=True,
        query_prefix=None,
        optimal_batch_size=16,
        tokenizer_id="current-tokenizer",
        sparse=False,
        send_dimension=False,
    )
    provider = SimpleNamespace(id=PROVIDER_ID, family="tei")

    class Session:
        async def get(self, model_type: type[object], identity: UUID) -> object | None:
            assert identity in {MODEL_ID, PROVIDER_ID}
            return model if model_type.__name__ == "Model" else provider

    @asynccontextmanager
    async def fake_scope() -> AsyncIterator[Session]:
        yield Session()

    monkeypatch.setattr("cairn.modelgw.catalog.session_scope", fake_scope)

    ref = await ModelCatalog().get_ref(MODEL_ID)

    assert ref.provider_id == PROVIDER_ID
    assert ref.dynamic_provider is True
