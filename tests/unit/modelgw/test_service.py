from __future__ import annotations

from typing import Any, ClassVar, Self
from uuid import uuid4

import pytest

from cairn.authz.model import Principal
from cairn.core.cache import InMemoryCache
from cairn.core.config import RetrievalSettings, Settings
from cairn.core.modelref import ModelRef
from cairn.embedding.probe import EmbeddingModelProbe
from cairn.modelgw.dto import EmbeddingRuntimeRef
from cairn.modelgw.service import ModelManagementService


class Tokenizer:
    fingerprint = "model-test-tokenizer"

    def count(self, text: str) -> int:
        return len(text)

    def truncate(self, text: str, limit: int) -> str:
        return text[:limit]


class Provider:
    instances: ClassVar[list[Provider]] = []
    max_batch_size = 8
    cache_namespace = "model-test-provider"

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.closed = False
        type(self).instances.append(self)

    async def __aenter__(self) -> Self:
        return self

    async def close(self) -> None:
        self.closed = True

    async def embed(self, model: ModelRef, texts: list[str], *, purpose: str) -> list[list[float]]:
        return [[1.0, 2.0, 3.0] for _text in texts]


class Catalog:
    def __init__(self, runtime: EmbeddingRuntimeRef) -> None:
        self.runtime = runtime
        self.health: list[tuple[object, object, bool]] = []

    async def get_embedding_runtime(self, model_id, *, workspace_id=None):  # type: ignore[no-untyped-def]
        assert model_id == self.runtime.model.id
        assert workspace_id == WORKSPACE_ID
        return self.runtime

    async def invocation_snapshot(self, workspace_id, model_id, *, principal_id, request_id):  # type: ignore[no-untyped-def]
        from cairn.core.provider_runtime import ProviderRuntimeProjection
        from cairn.modelgw.invocation import InvocationSnapshot

        return InvocationSnapshot(model=self.runtime.model,
            provider=ProviderRuntimeProjection(id=PROVIDER_ID,workspace_id=workspace_id,
                family="tei",base_url="http://127.0.0.1:9",binding_revision="mini-v1"),
            workspace_id=workspace_id,principal_id=principal_id,request_id=request_id)

    async def set_model_health(self, workspace_id, model_id, healthy):  # type: ignore[no-untyped-def]
        self.health.append((workspace_id, model_id, healthy))


WORKSPACE_ID = uuid4()
MODEL_ID = uuid4()
PROVIDER_ID = uuid4()


def actor() -> Principal:
    return Principal(
        type="user",
        id=uuid4(),
        workspace_id=WORKSPACE_ID,
        role="admin",
        username="admin",
    )


@pytest.mark.anyio
async def test_model_probe_uses_registered_provider_and_configured_tokenizer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Provider.instances.clear()
    model = ModelRef(
        id=MODEL_ID,
        provider_id=PROVIDER_ID,
        provider_family="tei",
        model_key="mini",
        capability="embedding",
        dimension=3,
        max_input_tokens=64,
        tokenizer_id="minilm",
        dynamic_provider=True,
    )
    catalog = Catalog(
        EmbeddingRuntimeRef(
            model=model,
            provider_id=PROVIDER_ID,
            provider_family="tei",
            base_url="http://127.0.0.1:9",
            config={
                "binding_revision": "mini-v1",
                "allow_private": True,
                "max_batch_size": 8,
            },
            has_credentials=False,
        )
    )
    settings = Settings.model_construct(
        retrieval=RetrievalSettings(tokenizer_files={"minilm": "tokenizer.json"})
    )
    probe = EmbeddingModelProbe(
        cache=InMemoryCache(),
        provider_types={"tei": Provider},  # type: ignore[dict-item]
        tokenizer_loader=lambda _path: Tokenizer(),
    )
    service = ModelManagementService(
        catalog=catalog,  # type: ignore[arg-type]
        settings=settings,
        probe=probe,
    )

    result = await service.test_model(actor().workspace_id, MODEL_ID)

    assert result.dimensions == 3
    assert result.tokens == len("Cairn health probe")
    assert Provider.instances[0].kwargs["base_url"] == "http://127.0.0.1:9"
    assert Provider.instances[0].closed
    assert catalog.health == [(WORKSPACE_ID, MODEL_ID, True)]
