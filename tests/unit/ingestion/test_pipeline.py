from __future__ import annotations

import base64
import json
from uuid import uuid4

import pytest
from tokenizers import Tokenizer as HFTokenizer
from tokenizers.models import WordLevel
from tokenizers.pre_tokenizers import Whitespace
from tokenizers.processors import TemplateProcessing

from cairn.catalog.dto import ChunkSpec
from cairn.core.cache import InMemoryCache
from cairn.core.config import EmbeddingSettings
from cairn.core.modelref import ModelRef
from cairn.embedding.service import EmbeddingService
from cairn.embedding.tokenizers import HuggingFaceTokenizer, TokenizerRegistry
from cairn.ingestion.artifacts import (
    ChunkManifest,
    EmbeddingManifest,
    ParseManifest,
    decode_chunk_manifest,
    decode_embedding_manifest,
    decode_parse_manifest,
    encode_chunk_manifest,
    encode_embedding_manifest,
    encode_parse_manifest,
)
from cairn.ingestion.base import Block, ParsedDocument
from cairn.ingestion.pipeline import EmbeddingInputTooLarge, create_embedding_manifest


class _Tokenizer:
    fingerprint = "prepared-tokenizer-v1"

    def count(self, text: str) -> int:
        return len(text)

    def truncate(self, text: str, limit: int) -> str:
        return text[:limit]


class _Provider:
    cache_namespace = "deterministic-provider-v1"
    max_batch_size = 16

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def embed(self, model, texts, *, purpose):  # type: ignore[no-untyped-def]
        self.calls.append(list(texts))
        return [[float(len(text)), 1.0, 2.0] for text in texts]


def _embedding_service(provider: _Provider) -> EmbeddingService:
    tokenizers = TokenizerRegistry()
    tokenizers.register("prepared", _Tokenizer())
    return EmbeddingService(
        provider=provider,
        cache=InMemoryCache(),
        tokenizers=tokenizers,
    )


def _model(*, max_input_tokens: int = 128) -> ModelRef:
    return ModelRef(
        id=uuid4(),
        provider_family="test",
        model_key="deterministic",
        capability="embedding",
        dimension=3,
        max_input_tokens=max_input_tokens,
        tokenizer_id="prepared",
        normalize=False,
    )


def test_parse_manifest_round_trip_preserves_source_provenance() -> None:
    document_id = uuid4()
    manifest = ParseManifest(
        document_id=document_id,
        revision=3,
        index_version=2,
        source_content_hash="a" * 64,
        parsed=ParsedDocument(
            markdown="# Guide\n\nBody",
            blocks=[
                Block(
                    kind="paragraph",
                    text="Body",
                    ordinal=1,
                    heading_path=("Guide",),
                    page=4,
                    bbox=(1.0, 2.0, 3.0, 4.0),
                    source_url="https://example.test/guide",
                )
            ],
            pages=4,
            language="en",
            metadata={"author": "Ada"},
        ),
    )

    restored = decode_parse_manifest(encode_parse_manifest(manifest))

    assert restored == manifest


def test_embedding_manifest_rejects_tampered_payload() -> None:
    manifest = EmbeddingManifest(
        document_id=uuid4(),
        revision=1,
        index_version=1,
        source_content_hash="b" * 64,
        binding_fingerprint="binding-v1",
        dimension=3,
        normalized=True,
        vectors={"c" * 64: (1.0, 2.0, 3.0)},
    )
    encoded = encode_embedding_manifest(manifest)
    envelope = json.loads(encoded)
    payload = base64.b64decode(envelope["payload"]).replace(b"binding-v1", b"binding-v2")
    envelope["payload"] = base64.b64encode(payload).decode()
    tampered = json.dumps(envelope).encode()

    with pytest.raises(ValueError, match="checksum"):
        decode_embedding_manifest(tampered)


@pytest.mark.anyio
async def test_embedding_manifest_reuses_persisted_hashes_and_never_embeds_parents() -> None:
    document_id = uuid4()
    unchanged_hash = "1" * 64
    changed_hash = "2" * 64
    chunks = [
        ChunkSpec(
            id=uuid4(),
            document_id=document_id,
            ordinal=0,
            content="parent context",
            content_hash="0" * 64,
            token_count=14,
            metadata={"role": "parent", "embed": False},
        ),
        ChunkSpec(
            id=uuid4(),
            document_id=document_id,
            ordinal=1,
            content="unchanged",
            content_hash=unchanged_hash,
            token_count=9,
            metadata={"role": "child", "embed": True},
        ),
        ChunkSpec(
            id=uuid4(),
            document_id=document_id,
            ordinal=2,
            content="changed",
            content_hash=changed_hash,
            token_count=7,
            metadata={"role": "child", "embed": True},
        ),
    ]
    model = _model()
    previous = EmbeddingManifest(
        document_id=document_id,
        revision=1,
        index_version=1,
        source_content_hash="a" * 64,
        binding_fingerprint="binding-v1",
        dimension=3,
        normalized=False,
        vectors={unchanged_hash: (9.0, 1.0, 2.0)},
    )
    provider = _Provider()

    result = await create_embedding_manifest(
        document_id=document_id,
        revision=2,
        index_version=1,
        source_content_hash="b" * 64,
        chunks=chunks,
        model=model,
        binding_fingerprint="binding-v1",
        service=_embedding_service(provider),
        previous=previous,
    )

    assert provider.calls == [["changed"]]
    assert result.vectors[unchanged_hash] == (9.0, 1.0, 2.0)
    assert result.vectors[changed_hash] == (7.0, 1.0, 2.0)
    assert "0" * 64 not in result.vectors


@pytest.mark.anyio
async def test_oversized_table_fails_before_embedding_service_can_truncate() -> None:
    document_id = uuid4()
    provider = _Provider()
    model = _model(max_input_tokens=8)
    chunks = [
        ChunkSpec(
            id=uuid4(),
            document_id=document_id,
            ordinal=0,
            content="oversized table",
            content_hash="3" * 64,
            token_count=15,
            metadata={
                "embed": True,
                "oversized": True,
                "oversized_reason": "table",
            },
        )
    ]

    with pytest.raises(EmbeddingInputTooLarge):
        await create_embedding_manifest(
            document_id=document_id,
            revision=1,
            index_version=1,
            source_content_hash="a" * 64,
            chunks=chunks,
            model=model,
            binding_fingerprint="binding-v1",
            service=_embedding_service(provider),
        )

    assert provider.calls == []


@pytest.mark.anyio
async def test_ordinary_chunk_uses_actual_special_token_budget_without_truncation() -> None:
    backend = HFTokenizer(
        WordLevel(
            vocab={"[UNK]": 0, "[CLS]": 1, "[SEP]": 2, "one": 3, "two": 4, "three": 5},
            unk_token="[UNK]",
        )
    )
    backend.pre_tokenizer = Whitespace()
    backend.post_processor = TemplateProcessing(
        single="[CLS] $A [SEP]",
        special_tokens=[("[CLS]", 1), ("[SEP]", 2)],
    )
    tokenizer = HuggingFaceTokenizer(backend)
    tokenizers = TokenizerRegistry()
    tokenizers.register("prepared", tokenizer)
    provider = _Provider()
    model = _model(max_input_tokens=4)
    service = EmbeddingService(
        provider=provider,
        cache=InMemoryCache(),
        tokenizers=tokenizers,
        settings=EmbeddingSettings(max_retries=0),
    )
    chunk = ChunkSpec(
        id=uuid4(),
        document_id=uuid4(),
        ordinal=0,
        content="one two three",
        content_hash="4" * 64,
        token_count=3,
        metadata={"embed": True},
    )

    with pytest.raises(EmbeddingInputTooLarge):
        await create_embedding_manifest(
            document_id=chunk.document_id,
            revision=1,
            index_version=1,
            source_content_hash="a" * 64,
            chunks=[chunk],
            model=model,
            binding_fingerprint="binding-v1",
            service=service,
        )

    assert tokenizer.count(chunk.content) == 5
    assert provider.calls == []


def test_chunk_manifest_round_trip_preserves_parent_metadata_and_stale_ids() -> None:
    document_id = uuid4()
    parent_id = uuid4()
    stale_id = uuid4()
    manifest = ChunkManifest(
        document_id=document_id,
        revision=4,
        index_version=2,
        source_content_hash="d" * 64,
        chunks=(
            ChunkSpec(
                id=parent_id,
                document_id=document_id,
                ordinal=0,
                content="parent",
                content_hash="e" * 64,
                token_count=6,
                metadata={"embed": False, "role": "parent"},
            ),
            ChunkSpec(
                id=uuid4(),
                document_id=document_id,
                ordinal=1,
                content="child",
                content_hash="f" * 64,
                token_count=5,
                parent_id=parent_id,
                metadata={"embed": True, "role": "child"},
            ),
        ),
        stale_point_ids=(stale_id,),
    )

    restored = decode_chunk_manifest(encode_chunk_manifest(manifest))

    assert restored == manifest


@pytest.mark.parametrize(
    ("queue", "kinds"),
    [
        ("parse", {"document.parse"}),
        ("chunk", {"document.chunk"}),
        ("embed", {"document.embed", "chunk.reembed"}),
        ("index", {"document.index", "document.reindex_delete"}),
    ],
)
def test_worker_startup_registers_pipeline_handler(
    queue: str, kinds: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from apps.worker.main import build_worker
    from cairn.core.config import get_settings

    monkeypatch.setenv("CAIRN_DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
    monkeypatch.setenv("CAIRN_REDIS_URL", "redis://localhost:6379/15")
    get_settings.cache_clear()

    worker = build_worker(queue)

    assert set(worker._handlers) == kinds
