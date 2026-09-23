from __future__ import annotations

import asyncio
from dataclasses import replace
from hashlib import sha256
from typing import Any, ClassVar, Self
from uuid import uuid4

import pytest

from cairn.core.errors import ValidationFailed
from cairn.core.modelref import ModelRef
from cairn.embedding.errors import TokenizerUnavailable
from cairn.embedding.tokenizers import TokenizerRegistry
from cairn.ingestion import runtime as runtime_module
from cairn.ingestion.runtime import PipelineRuntime, _binding_fingerprint
from cairn.modelgw.dto import EmbeddingRuntimeRef


class _Tokenizer:
    fingerprint = "runtime-tokenizer-v1"

    def count(self, text: str) -> int:
        return len(text)

    def truncate(self, text: str, limit: int) -> str:
        return text[:limit]


class _Models:
    def __init__(self, runtime: EmbeddingRuntimeRef) -> None:
        self.runtime = runtime
        self.calls = 0

    async def get_embedding_runtime(self, _model_id):  # type: ignore[no-untyped-def]
        self.calls += 1
        await asyncio.sleep(0)
        return self.runtime


class _Provider:
    instances: ClassVar[list[_Provider]] = []
    fail_enter = False
    enter_delay = 0.0

    def __init__(self, **config: Any) -> None:
        self.config = config
        self.max_batch_size = int(config["max_batch_size"])
        self.cache_namespace = sha256(repr(sorted(config.items())).encode()).hexdigest()
        self.opened = False
        self.closed = False
        type(self).instances.append(self)

    async def __aenter__(self) -> Self:
        self.opened = True
        await asyncio.sleep(type(self).enter_delay)
        if type(self).fail_enter:
            raise RuntimeError("provider startup failed")
        return self

    async def close(self) -> None:
        self.closed = True
        self.opened = False

    async def embed(self, model, texts, *, purpose):  # type: ignore[no-untyped-def]
        raise AssertionError("runtime lifecycle tests do not invoke the provider")


@pytest.fixture(autouse=True)
def _reset_provider() -> None:
    _Provider.instances = []
    _Provider.fail_enter = False
    _Provider.enter_delay = 0.0


def _runtime_ref(*, config: dict[str, Any] | None = None) -> EmbeddingRuntimeRef:
    model = ModelRef(
        id=uuid4(),
        provider_family="tei",
        model_key="runtime-test",
        capability="embedding",
        dimension=3,
        max_input_tokens=32,
        normalize=False,
        tokenizer_id="runtime-tokenizer",
    )
    return EmbeddingRuntimeRef(
        model=model,
        provider_id=uuid4(),
        provider_family="tei",
        base_url="https://runtime.test",
        config={"binding_revision": "r1", **(config or {})},
        has_credentials=False,
    )


def _build_runtime(
    monkeypatch: pytest.MonkeyPatch,
    models: _Models,
    *,
    tokenizer: bool = True,
) -> PipelineRuntime:
    tokenizers = TokenizerRegistry()
    if tokenizer:
        tokenizers.register("runtime-tokenizer", _Tokenizer())
    monkeypatch.setattr(runtime_module, "_load_tokenizers", lambda: tokenizers)
    monkeypatch.setattr(runtime_module, "TeiProvider", _Provider)
    return PipelineRuntime(models=models)  # type: ignore[arg-type]


@pytest.mark.anyio
async def test_tokenizer_is_validated_before_provider_is_opened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ref = _runtime_ref()
    runtime = _build_runtime(monkeypatch, _Models(ref), tokenizer=False)

    with pytest.raises(TokenizerUnavailable):
        await runtime.embedding_for(ref.model)

    assert _Provider.instances == []


@pytest.mark.anyio
async def test_provider_startup_failure_is_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ref = _runtime_ref()
    runtime = _build_runtime(monkeypatch, _Models(ref))
    _Provider.fail_enter = True

    with pytest.raises(RuntimeError, match="provider startup failed"):
        await runtime.embedding_for(ref.model)

    assert len(_Provider.instances) == 1
    assert _Provider.instances[0].closed


@pytest.mark.anyio
async def test_concurrent_resolution_opens_one_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ref = _runtime_ref()
    models = _Models(ref)
    runtime = _build_runtime(monkeypatch, models)
    _Provider.enter_delay = 0.05

    first, second = await asyncio.gather(
        runtime.embedding_for(ref.model), runtime.embedding_for(ref.model)
    )

    assert first is second
    assert len(_Provider.instances) == 1
    await runtime.close()
    assert _Provider.instances[0].closed


@pytest.mark.anyio
async def test_runtime_cache_tracks_config_without_closing_inflight_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_ref = _runtime_ref(config={"deployment": {"name": "blue"}})
    models = _Models(first_ref)
    runtime = _build_runtime(monkeypatch, models)

    first = await runtime.embedding_for(first_ref.model)
    models.runtime = replace(
        first_ref,
        model=replace(first_ref.model, model_key="runtime-test-v2"),
        config={"binding_revision": "r1", "deployment": {"name": "green"}},
    )
    second = await runtime.embedding_for(models.runtime.model)

    assert first is not second
    assert first.binding_fingerprint != second.binding_fingerprint
    assert len(_Provider.instances) == 2
    assert not _Provider.instances[0].closed
    assert not _Provider.instances[1].closed
    await runtime.close()
    assert all(provider.closed for provider in _Provider.instances)


@pytest.mark.anyio
async def test_registered_model_drift_is_rejected_before_provider_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = _runtime_ref()
    changed = replace(original, model=replace(original.model, tokenizer_id="changed-tokenizer"))
    runtime = _build_runtime(monkeypatch, _Models(changed))

    with pytest.raises(ValidationFailed, match="snapshot"):
        await runtime.embedding_for(original.model)

    assert _Provider.instances == []


@pytest.mark.anyio
async def test_dynamic_provider_provenance_does_not_look_like_model_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_snapshot = _runtime_ref()
    assert old_snapshot.model.dynamic_provider is False
    registered = replace(
        old_snapshot,
        model=replace(old_snapshot.model, dynamic_provider=True),
    )
    runtime = _build_runtime(monkeypatch, _Models(registered))

    prepared = await runtime.embedding_for(old_snapshot.model)

    assert prepared.model.dynamic_provider is True
    await runtime.close()


def test_dynamic_provider_provenance_does_not_change_binding_fingerprint() -> None:
    old_snapshot = _runtime_ref()
    registered = replace(
        old_snapshot,
        model=replace(old_snapshot.model, dynamic_provider=True),
    )

    assert _binding_fingerprint(old_snapshot, _Tokenizer()) == _binding_fingerprint(
        registered, _Tokenizer()
    )
