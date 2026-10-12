from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import replace
from typing import ClassVar
from uuid import UUID

import pytest
import tiktoken
from tokenizers import Tokenizer as HFBackend
from tokenizers.models import WordLevel
from tokenizers.pre_tokenizers import Whitespace
from tokenizers.processors import TemplateProcessing

import cairn.ingestion.custom as custom_module
import cairn.ingestion.semantic as semantic_module
from cairn.catalog.config import ChunkConfig
from cairn.catalog.dto import ChunkSpec
from cairn.core.modelref import ModelRef
from cairn.embedding.base import Vector
from cairn.embedding.tokenizers import HuggingFaceTokenizer, TiktokenTokenizer, Tokenizer
from cairn.ingestion.base import Block, ParsedDocument
from cairn.ingestion.chunkers import DocumentChunker

DOCUMENT_ID = UUID("38df0663-d446-4fe6-88da-1ed17276efdf")


def tokenizer() -> TiktokenTokenizer:
    return TiktokenTokenizer(
        tiktoken.Encoding(
            name="advanced-chunk-test-byte",
            pat_str=r"(?s).",
            mergeable_ranks={bytes([token_id]): token_id for token_id in range(256)},
            special_tokens={},
        )
    )


def config(strategy: str, **changes: object) -> ChunkConfig:
    return ChunkConfig.model_validate(
        {
            "strategy": strategy,
            "child_tokens": 96,
            "child_overlap": 0,
            "parent_tokens": 192,
            "min_chunk_tokens": 8,
            **changes,
        }
    )


def document(text: str, **changes: object) -> ParsedDocument:
    return ParsedDocument(markdown=text, blocks=[Block("paragraph", text, 0, **changes)])


def special_tokenizer() -> HuggingFaceTokenizer:
    vocabulary = {"[UNK]": 0, "[CLS]": 1, "[SEP]": 2, "word": 3}
    backend = HFBackend(WordLevel(vocabulary, unk_token="[UNK]"))
    backend.pre_tokenizer = Whitespace()
    backend.post_processor = TemplateProcessing(
        single="[CLS] $A [SEP]",
        special_tokens=[("[CLS]", 1), ("[SEP]", 2)],
    )
    return HuggingFaceTokenizer(backend)


def assert_citation_coverage(doc: ParsedDocument, chunks: list[ChunkSpec]) -> None:
    for block in doc.blocks:
        if not block.text.strip():
            continue
        ranges = sorted(
            (citation["start"], citation["end"])
            for chunk in chunks
            if chunk.metadata["embed"]
            for citation in chunk.metadata["citations"]
            if citation["block_ordinal"] == block.ordinal
        )
        position = 0
        for start, end in ranges:
            assert start <= position < end
            position = max(position, end)
        assert position == len(block.text)


def citation(block: Block, start: int = 0, end: int | None = None) -> dict[str, object]:
    return {
        "block_ordinal": block.ordinal,
        "kind": block.kind,
        "start": start,
        "end": len(block.text) if end is None else end,
        "heading_path": list(block.heading_path),
        "page": block.page,
        "bbox": list(block.bbox) if block.bbox is not None else None,
        "source_url": block.source_url,
    }


def custom_scope(selected_tokenizer: Tokenizer) -> custom_module.CustomChunkScope:
    return custom_module.CustomChunkScope(
        document_id=DOCUMENT_ID,
        index_version=1,
        tokenizer_fingerprint=selected_tokenizer.fingerprint,
        config_key="advanced-custom:v1",
    )


def custom_spec(
    doc: ParsedDocument,
    cfg: ChunkConfig,
    selected_tokenizer: Tokenizer,
    scope: custom_module.CustomChunkScope,
    *,
    ordinal: int,
    role: custom_module.ChunkRole,
    citations: list[dict[str, object]],
    parent_id: UUID | None = None,
    metadata_changes: dict[str, object] | None = None,
) -> ChunkSpec:
    heading_path = citations[0]["heading_path"] if citations else []
    blocks = {block.ordinal: block for block in doc.blocks}
    body = "\n\n".join(
        blocks[int(item["block_ordinal"])].text[int(item["start"]) : int(item["end"])]
        for item in citations
    )
    prefix = " > ".join(heading_path) + "\n\n" if cfg.prepend_heading_path and heading_path else ""
    content = prefix + body
    digest = custom_module.content_hash(content)
    tokens = selected_tokenizer.count(content)
    budget = cfg.parent_tokens if role == "parent" else cfg.child_tokens
    oversized = tokens > budget
    metadata: dict[str, object] = {
        "role": role,
        "embed": role != "parent",
        "language": doc.language,
        "heading_path": heading_path,
        "page": citations[0]["page"] if citations else None,
        "source_url": citations[0]["source_url"] if citations else None,
        "ordinal": ordinal,
        "citations": citations,
        "oversized": oversized,
        "oversized_reason": "table" if oversized else None,
    }
    metadata.update(metadata_changes or {})
    return ChunkSpec(
        id=custom_module.chunk_id(
            scope,
            ordinal=ordinal,
            role=role,
            content_hash=digest,
            citations=citations,
        ),
        document_id=scope.document_id,
        ordinal=ordinal,
        content=content,
        content_hash=digest,
        token_count=tokens,
        parent_id=parent_id,
        metadata=metadata,
    )


class TopicEmbeddings:
    identity = "topic-model:v1"
    max_batch_size = 8

    async def embed_documents(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        return [[1.0, 0.0] if "cat" in text else [0.0, 1.0] for text in texts]


async def test_semantic_uses_embedding_distance_peak_within_token_budget() -> None:
    text = "cats purr. cats nap. qubits spin. photons wave."
    chunks = await DocumentChunker(
        document_id=DOCUMENT_ID,
        tokenizer=tokenizer(),
        semantic=TopicEmbeddings(),
    ).chunk(document(text), config("semantic"))

    assert [chunk.content for chunk in chunks] == [
        "cats purr. cats nap. ",
        "qubits spin. photons wave.",
    ]
    assert "".join(chunk.content for chunk in chunks) == text
    assert all(chunk.token_count == len(chunk.content.encode()) <= 96 for chunk in chunks)


async def test_semantic_callback_batches_and_identity_scope_chunk_ids() -> None:
    calls: list[list[str]] = []

    async def embed(texts: Sequence[str]) -> Sequence[Sequence[float]]:
        calls.append(list(texts))
        return [[1.0, float(index % 2)] for index, _ in enumerate(texts)]

    text = "one sentence. two sentence. three sentence. four sentence. five sentence."
    first = semantic_module.CallbackSemanticEmbedding(
        identity="registered:model:v1",
        callback=embed,
        max_batch_size=2,
    )
    first_chunks = await DocumentChunker(
        document_id=DOCUMENT_ID,
        tokenizer=tokenizer(),
        semantic=first,
    ).chunk(document(text), config("semantic"))
    second_chunks = await DocumentChunker(
        document_id=DOCUMENT_ID,
        tokenizer=tokenizer(),
        semantic=semantic_module.CallbackSemanticEmbedding(
            identity="registered:model:v2",
            callback=embed,
            max_batch_size=2,
        ),
    ).chunk(document(text), config("semantic"))

    assert calls
    assert max(map(len, calls)) <= 2
    assert {chunk.id for chunk in first_chunks}.isdisjoint(chunk.id for chunk in second_chunks)


async def test_embedding_service_adapter_binds_model_and_deployment_identity() -> None:
    model = ModelRef(
        id=UUID("bc92dfbc-b813-4a69-966b-85fb79676c5f"),
        provider_family="local",
        model_key="semantic-v1",
        capability="embedding",
        dimension=2,
        max_input_tokens=128,
        optimal_batch_size=3,
        tokenizer_id="byte",
    )

    class Service:
        calls: ClassVar[list[tuple[ModelRef, list[str], int | None]]] = []

        def count_tokens(self, selected: ModelRef, text: str) -> int:
            assert selected == model
            return tokenizer().count(text)

        async def embed_documents(
            self,
            selected: ModelRef,
            texts: Sequence[str],
            *,
            batch_size: int | None = None,
        ) -> list[Vector]:
            self.calls.append((selected, list(texts), batch_size))
            return [Vector.from_values([1.0, 0.0], dim=2, normalize=True) for _ in texts]

    service = Service()
    adapter = semantic_module.EmbeddingServiceSemanticEmbedding(
        service=service,
        model=model,
        binding_identity="provider-account:deployment-7",
    )
    await DocumentChunker(
        document_id=DOCUMENT_ID,
        tokenizer=tokenizer(),
        semantic=adapter,
    ).chunk(document("one sentence. two sentence. three sentence."), config("semantic"))

    assert adapter.max_batch_size == 3
    assert "provider-account:deployment-7" in adapter.identity
    assert service.calls
    assert all(call[0] == model and call[2] == 3 for call in service.calls)


async def test_service_adapter_rejects_inputs_the_model_would_truncate() -> None:
    selected_tokenizer = special_tokenizer()
    model = ModelRef(
        id=UUID("bc92dfbc-b813-4a69-966b-85fb79676c5f"),
        provider_family="local",
        model_key="semantic-v1",
        capability="embedding",
        dimension=2,
        max_input_tokens=4,
        optimal_batch_size=3,
        tokenizer_id="word-level",
    )

    class Service:
        called = False

        def count_tokens(self, selected: ModelRef, text: str) -> int:
            assert selected == model
            return selected_tokenizer.count(text)

        async def embed_documents(
            self,
            selected: ModelRef,
            texts: Sequence[str],
            *,
            batch_size: int | None = None,
        ) -> list[Vector]:
            self.called = True
            return [Vector.from_values([1.0, 0.0], dim=2, normalize=True) for _ in texts]

    service = Service()
    adapter = semantic_module.EmbeddingServiceSemanticEmbedding(
        service=service,  # type: ignore[arg-type] - focused service seam for adapter validation
        model=model,
        binding_identity="provider-account:deployment-7",
    )

    with pytest.raises(semantic_module.SemanticChunkingError, match="token limit"):
        await adapter.embed_documents(["word word word"])
    assert service.called is False


async def test_semantic_coalesces_overlapping_delimiters_without_losing_whitespace() -> None:
    calls: list[list[str]] = []

    async def embed(texts: Sequence[str]) -> Sequence[Sequence[float]]:
        calls.append(list(texts))
        assert all(text.strip() for text in texts)
        return [[1.0, 0.0] for _ in texts]

    text = (
        "The planet Saturn has prominent rings made of ice and rock. "
        "Its moons orbit the gas giant. Marine scientists study currents in the ocean."
    )
    doc = document(text, heading_path=("Astronomy",))
    chunks = await DocumentChunker(
        document_id=DOCUMENT_ID,
        tokenizer=special_tokenizer(),
        semantic=semantic_module.CallbackSemanticEmbedding(
            identity="real-tokenizer:whitespace-regression",
            callback=embed,
            max_batch_size=4,
        ),
    ).chunk(
        doc,
        config(
            "semantic",
            child_tokens=64,
            separators=[". ", "."],
        ),
    )

    assert calls
    assert_citation_coverage(doc, chunks)
    assert all(chunk.token_count <= 64 for chunk in chunks)

    byte_doc = document("first." + " " * 100 + "second.")
    byte_chunks = await DocumentChunker(
        document_id=DOCUMENT_ID,
        tokenizer=tokenizer(),
        semantic=semantic_module.CallbackSemanticEmbedding(
            identity="byte-tokenizer:budget-whitespace-regression",
            callback=embed,
            max_batch_size=4,
        ),
    ).chunk(
        byte_doc,
        config(
            "semantic",
            child_tokens=32,
            min_chunk_tokens=8,
            separators=[". ", "."],
        ),
    )
    assert_citation_coverage(byte_doc, byte_chunks)
    assert all(chunk.token_count <= 32 for chunk in byte_chunks)


async def test_semantic_unit_target_does_not_turn_legal_long_heading_into_hard_limit() -> None:
    selected_tokenizer = special_tokenizer()
    calls: list[list[str]] = []

    async def embed(texts: Sequence[str]) -> Sequence[Sequence[float]]:
        calls.append(list(texts))
        assert all(text.strip() for text in texts)
        return [[1.0, 0.0] for _ in texts]

    heading = " ".join(["word"] * 20)
    doc = document("word. word. word.", heading_path=(heading,))
    assert selected_tokenizer.count(f"{heading}\n\n{doc.blocks[0].text}") < 64

    chunks = await DocumentChunker(
        document_id=DOCUMENT_ID,
        tokenizer=selected_tokenizer,
        semantic=semantic_module.CallbackSemanticEmbedding(
            identity="real-tokenizer:long-heading",
            callback=embed,
        ),
    ).chunk(
        doc,
        config("semantic", child_tokens=64, separators=[". ", "."]),
    )

    assert calls
    assert all(chunk.token_count <= 64 for chunk in chunks)
    assert_citation_coverage(doc, chunks)


async def test_semantic_preserves_cjk_headings_and_does_not_embed_protected_table() -> None:
    embedded: list[str] = []

    async def embed(texts: Sequence[str]) -> Sequence[Sequence[float]]:
        embedded.extend(texts)
        return [[1.0, float("海洋" in text)] for text in texts]

    table = "| 星体 | 特征 |\n| --- | --- |\n| 土星 | 光环 |\n" * 4
    doc = ParsedDocument(
        markdown="天文学研究土星。海洋科学研究洋流。\n\n" + table,
        language="zh",
        blocks=[
            Block("paragraph", "天文学研究土星。海洋科学研究洋流。", 0, ("研究",)),
            Block("table", table, 1, ("数据",), page=2),
        ],
    )
    chunks = await DocumentChunker(
        document_id=DOCUMENT_ID,
        tokenizer=tokenizer(),
        semantic=semantic_module.CallbackSemanticEmbedding(
            identity="cjk-table:v1",
            callback=embed,
            max_batch_size=2,
        ),
    ).chunk(doc, config("semantic", child_tokens=64))

    assert embedded and all(table not in text for text in embedded)
    assert any(chunk.content == "数据\n\n" + table for chunk in chunks)
    assert_citation_coverage(doc, chunks)


@pytest.mark.parametrize(
    "vectors",
    [
        [[1.0, 0.0]],
        [[1.0, 0.0], [1.0, 0.0], [1.0, 0.0]],
        [[], []],
        [[0.0, 0.0], [1.0, 0.0]],
        [[float("nan"), 0.0], [1.0, 0.0]],
        [[float("inf"), 0.0], [1.0, 0.0]],
        [[1.0, 0.0], [1.0, 0.0, 0.0]],
        ["not-a-vector", "not-a-vector"],
        [[1.0, 0.0], 7],
    ],
    ids=[
        "missing-row",
        "extra-row",
        "empty",
        "zero",
        "nan",
        "infinity",
        "dimension-change",
        "string-row",
        "scalar-row",
    ],
)
async def test_semantic_rejects_malformed_vector_shapes(vectors: object) -> None:
    class MalformedEmbeddings:
        identity = "malformed:shape"
        max_batch_size = 8

        async def embed_documents(self, texts: Sequence[str]) -> object:
            return vectors

    with pytest.raises(semantic_module.SemanticChunkingError):
        await semantic_module.SemanticBoundaryDetector(MalformedEmbeddings()).boundaries(["a", "b"])


async def test_semantic_rejects_vectors_above_the_dimension_limit() -> None:
    class OversizedEmbeddings:
        identity = "malformed:oversized-dimension"
        max_batch_size = 8

        async def embed_documents(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
            row = [1.0] * (semantic_module.SEMANTIC_MAX_VECTOR_DIMENSION + 1)
            return [row for _ in texts]

    with pytest.raises(semantic_module.SemanticChunkingError):
        await semantic_module.SemanticBoundaryDetector(OversizedEmbeddings()).boundaries(["a", "b"])


@pytest.mark.parametrize("provider_limit", [1, 7, 1000])
async def test_semantic_batches_at_provider_and_local_limits(provider_limit: int) -> None:
    calls: list[int] = []

    async def embed(texts: Sequence[str]) -> Sequence[Sequence[float]]:
        calls.append(len(texts))
        return [[1.0, 0.0] for _ in texts]

    detector = semantic_module.SemanticBoundaryDetector(
        semantic_module.CallbackSemanticEmbedding(
            identity=f"batch:{provider_limit}",
            callback=embed,
            max_batch_size=provider_limit,
        )
    )
    await detector.boundaries([f"segment-{index}" for index in range(130)])

    assert sum(calls) == 130
    assert max(calls) <= min(provider_limit, semantic_module.SEMANTIC_MAX_BATCH_SIZE)


async def test_semantic_propagates_cancellation_without_wrapping() -> None:
    async def cancel(texts: Sequence[str]) -> Sequence[Sequence[float]]:
        raise asyncio.CancelledError

    detector = semantic_module.SemanticBoundaryDetector(
        semantic_module.CallbackSemanticEmbedding(
            identity="cancel:v1",
            callback=cancel,
        )
    )
    with pytest.raises(asyncio.CancelledError):
        await detector.boundaries(["a", "b"])


async def test_semantic_rejects_boolean_vector_components() -> None:
    class BooleanEmbeddings:
        identity = "malformed:boolean"
        max_batch_size = 8

        async def embed_documents(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
            return [[True, 0.0] for _ in texts]

    with pytest.raises(semantic_module.SemanticChunkingError):
        await DocumentChunker(
            document_id=DOCUMENT_ID,
            tokenizer=tokenizer(),
            semantic=BooleanEmbeddings(),
        ).chunk(document("one sentence. two sentence."), config("semantic"))


async def test_custom_calls_only_injected_version_pinned_executor() -> None:
    calls: list[
        tuple[
            custom_module.FunctionVersionRef,
            custom_module.CustomChunkScope,
            custom_module.CustomChunkLimits,
        ]
    ] = []

    class RegisteredExecutor:
        identity = "registered-executor:v1"

        async def execute(
            self,
            ref: custom_module.FunctionVersionRef,
            doc: ParsedDocument,
            cfg: ChunkConfig,
            scope: custom_module.CustomChunkScope,
            limits: custom_module.CustomChunkLimits,
        ) -> list[ChunkSpec]:
            calls.append((ref, scope, limits))
            citations = [
                {
                    "block_ordinal": 0,
                    "kind": "paragraph",
                    "start": 0,
                    "end": 11,
                    "heading_path": [],
                    "page": None,
                    "bbox": None,
                    "source_url": None,
                }
            ]
            content = "hello world"
            content_hash = custom_module.content_hash(content)
            return [
                ChunkSpec(
                    id=custom_module.chunk_id(
                        scope,
                        ordinal=0,
                        role="standalone",
                        content_hash=content_hash,
                        citations=citations,
                    ),
                    document_id=scope.document_id,
                    ordinal=0,
                    content=content,
                    content_hash=content_hash,
                    token_count=len(content),
                    metadata={
                        "role": "standalone",
                        "embed": True,
                        "language": doc.language,
                        "heading_path": [],
                        "page": None,
                        "source_url": None,
                        "ordinal": 0,
                        "citations": citations,
                        "oversized": False,
                        "oversized_reason": None,
                    },
                )
            ]

    chunks = await DocumentChunker(
        document_id=DOCUMENT_ID,
        tokenizer=tokenizer(),
        custom=RegisteredExecutor(),
    ).chunk(
        document("hello world"),
        config("custom", function_id="fn_domain", function_version=7),
    )

    assert [chunk.content for chunk in chunks] == ["hello world"]
    assert calls[0][0] == custom_module.FunctionVersionRef("fn_domain", 7)
    assert calls[0][1].document_id == DOCUMENT_ID
    assert calls[0][2] == custom_module.CustomChunkLimits()


def test_custom_accepts_source_preserving_parent_child_output() -> None:
    selected_tokenizer = tokenizer()
    cfg = config("custom", function_id="fn_domain", function_version=7)
    doc = document("parent and child source", heading_path=("Guide",))
    scope = custom_scope(selected_tokenizer)
    spans = [citation(doc.blocks[0])]
    parent = custom_spec(
        doc,
        cfg,
        selected_tokenizer,
        scope,
        ordinal=0,
        role="parent",
        citations=spans,
    )
    child = custom_spec(
        doc,
        cfg,
        selected_tokenizer,
        scope,
        ordinal=1,
        role="child",
        citations=spans,
        parent_id=parent.id,
    )

    assert custom_module.validate_custom_chunks(
        [parent, child],
        doc=doc,
        cfg=cfg,
        tokenizer=selected_tokenizer,
        scope=scope,
        limits=custom_module.CustomChunkLimits(),
    ) == [parent, child]


def test_custom_rejects_source_faithful_chunks_that_omit_source_ranges() -> None:
    selected_tokenizer = tokenizer()
    cfg = config("custom", function_id="fn_domain", function_version=7)
    doc = document("source must remain covered")
    scope = custom_scope(selected_tokenizer)
    chunk = custom_spec(
        doc,
        cfg,
        selected_tokenizer,
        scope,
        ordinal=0,
        role="standalone",
        citations=[citation(doc.blocks[0], 0, 6)],
    )

    with pytest.raises(custom_module.CustomChunkOutputInvalid):
        custom_module.validate_custom_chunks(
            [chunk],
            doc=doc,
            cfg=cfg,
            tokenizer=selected_tokenizer,
            scope=scope,
            limits=custom_module.CustomChunkLimits(),
        )


@pytest.mark.parametrize(
    ("citation_change", "metadata_change"),
    [
        ({"kind": "table"}, {}),
        ({"heading_path": ["Forged"]}, {}),
        ({"page": 99}, {}),
        ({"bbox": [9.0, 9.0, 9.0, 9.0]}, {}),
        ({"source_url": "forged:source"}, {}),
        ({}, {"language": "forged"}),
        ({}, {"heading_path": ["Forged"]}),
        ({}, {"page": 99}),
        ({}, {"source_url": "forged:source"}),
        ({}, {"ordinal": 99}),
    ],
)
def test_custom_rejects_forged_citation_and_top_level_provenance(
    citation_change: dict[str, object],
    metadata_change: dict[str, object],
) -> None:
    selected_tokenizer = tokenizer()
    cfg = config("custom", function_id="fn_domain", function_version=7)
    doc = ParsedDocument(
        markdown="source",
        language="zh",
        blocks=[
            Block(
                "paragraph",
                "source",
                4,
                ("Guide",),
                page=2,
                bbox=(1.0, 2.0, 3.0, 4.0),
                source_url="source:actual",
            )
        ],
    )
    scope = custom_scope(selected_tokenizer)
    span = citation(doc.blocks[0])
    span.update(citation_change)
    chunk = custom_spec(
        doc,
        cfg,
        selected_tokenizer,
        scope,
        ordinal=0,
        role="standalone",
        citations=[span],
        metadata_changes=metadata_change,
    )

    with pytest.raises(custom_module.CustomChunkOutputInvalid):
        custom_module.validate_custom_chunks(
            [chunk],
            doc=doc,
            cfg=cfg,
            tokenizer=selected_tokenizer,
            scope=scope,
            limits=custom_module.CustomChunkLimits(),
        )


def test_custom_rejects_child_citations_outside_parent_bounds() -> None:
    selected_tokenizer = tokenizer()
    cfg = config("custom", function_id="fn_domain", function_version=7)
    doc = document("abcdefghij")
    scope = custom_scope(selected_tokenizer)
    parent = custom_spec(
        doc,
        cfg,
        selected_tokenizer,
        scope,
        ordinal=0,
        role="parent",
        citations=[citation(doc.blocks[0], 0, 5)],
    )
    child = custom_spec(
        doc,
        cfg,
        selected_tokenizer,
        scope,
        ordinal=1,
        role="child",
        citations=[citation(doc.blocks[0])],
        parent_id=parent.id,
    )

    with pytest.raises(custom_module.CustomChunkOutputInvalid):
        custom_module.validate_custom_chunks(
            [parent, child],
            doc=doc,
            cfg=cfg,
            tokenizer=selected_tokenizer,
            scope=scope,
            limits=custom_module.CustomChunkLimits(),
        )


def test_custom_protected_table_must_be_whole_but_may_exceed_budget() -> None:
    selected_tokenizer = tokenizer()
    cfg = config(
        "custom",
        function_id="fn_domain",
        function_version=7,
        child_tokens=32,
        min_chunk_tokens=1,
    )
    table = "| key | value |\n" * 8
    doc = ParsedDocument(markdown=table, blocks=[Block("table", table, 0)])
    scope = custom_scope(selected_tokenizer)
    whole = custom_spec(
        doc,
        cfg,
        selected_tokenizer,
        scope,
        ordinal=0,
        role="standalone",
        citations=[citation(doc.blocks[0])],
    )
    assert whole.token_count > cfg.child_tokens
    assert custom_module.validate_custom_chunks(
        [whole],
        doc=doc,
        cfg=cfg,
        tokenizer=selected_tokenizer,
        scope=scope,
        limits=custom_module.CustomChunkLimits(),
    ) == [whole]

    midpoint = len(table) // 2
    first = custom_spec(
        doc,
        cfg,
        selected_tokenizer,
        scope,
        ordinal=0,
        role="standalone",
        citations=[citation(doc.blocks[0], 0, midpoint)],
    )
    second = custom_spec(
        doc,
        cfg,
        selected_tokenizer,
        scope,
        ordinal=1,
        role="standalone",
        citations=[citation(doc.blocks[0], midpoint, len(table))],
    )
    with pytest.raises(custom_module.CustomChunkOutputInvalid):
        custom_module.validate_custom_chunks(
            [first, second],
            doc=doc,
            cfg=cfg,
            tokenizer=selected_tokenizer,
            scope=scope,
            limits=custom_module.CustomChunkLimits(),
        )


def test_custom_budget_counts_heading_and_model_special_tokens() -> None:
    selected_tokenizer = special_tokenizer()
    cfg = config(
        "custom",
        function_id="fn_domain",
        function_version=7,
        child_tokens=32,
        min_chunk_tokens=1,
    )
    doc = document("word " * 20, heading_path=("word " * 15,))
    scope = custom_scope(selected_tokenizer)
    chunk = custom_spec(
        doc,
        cfg,
        selected_tokenizer,
        scope,
        ordinal=0,
        role="standalone",
        citations=[citation(doc.blocks[0])],
        metadata_changes={"oversized": False, "oversized_reason": None},
    )
    assert selected_tokenizer.count(doc.blocks[0].text) <= cfg.child_tokens
    assert chunk.token_count > cfg.child_tokens

    with pytest.raises(custom_module.CustomChunkOutputInvalid):
        custom_module.validate_custom_chunks(
            [chunk],
            doc=doc,
            cfg=cfg,
            tokenizer=selected_tokenizer,
            scope=scope,
            limits=custom_module.CustomChunkLimits(),
        )


@pytest.mark.parametrize(
    "limits",
    [
        custom_module.CustomChunkLimits(max_chunks=0),
        custom_module.CustomChunkLimits(max_output_bytes=1),
        custom_module.CustomChunkLimits(max_chunk_bytes=1),
        custom_module.CustomChunkLimits(max_citations_per_chunk=0),
    ],
    ids=["chunk-count", "serialized-output", "chunk-bytes", "citations"],
)
def test_custom_enforces_each_local_output_limit(limits: custom_module.CustomChunkLimits) -> None:
    selected_tokenizer = tokenizer()
    cfg = config("custom", function_id="fn_domain", function_version=7)
    doc = document("source")
    scope = custom_scope(selected_tokenizer)
    chunk = custom_spec(
        doc,
        cfg,
        selected_tokenizer,
        scope,
        ordinal=0,
        role="standalone",
        citations=[citation(doc.blocks[0])],
    )

    with pytest.raises(custom_module.CustomChunkOutputInvalid):
        custom_module.validate_custom_chunks(
            [chunk],
            doc=doc,
            cfg=cfg,
            tokenizer=selected_tokenizer,
            scope=scope,
            limits=limits,
        )


@pytest.mark.parametrize("field", ["document", "ordinal", "hash", "tokens", "id", "scope"])
def test_custom_retains_scope_identity_hash_and_ordinal_checks(field: str) -> None:
    selected_tokenizer = tokenizer()
    cfg = config("custom", function_id="fn_domain", function_version=7)
    doc = document("source")
    scope = custom_scope(selected_tokenizer)
    chunk = custom_spec(
        doc,
        cfg,
        selected_tokenizer,
        scope,
        ordinal=0,
        role="standalone",
        citations=[citation(doc.blocks[0])],
    )
    validation_scope = scope
    if field == "document":
        chunk = replace(chunk, document_id=UUID(int=999))
    elif field == "ordinal":
        chunk = replace(chunk, ordinal=1)
    elif field == "hash":
        chunk = replace(chunk, content_hash="0" * 64)
    elif field == "tokens":
        chunk = replace(chunk, token_count=chunk.token_count + 1)
    elif field == "id":
        chunk = replace(chunk, id=UUID(int=999))
    else:
        validation_scope = custom_module.CustomChunkScope(
            document_id=scope.document_id,
            index_version=scope.index_version,
            tokenizer_fingerprint="different-tokenizer",
            config_key=scope.config_key,
        )

    with pytest.raises(custom_module.CustomChunkOutputInvalid):
        custom_module.validate_custom_chunks(
            [chunk],
            doc=doc,
            cfg=cfg,
            tokenizer=selected_tokenizer,
            scope=validation_scope,
            limits=custom_module.CustomChunkLimits(),
        )


@pytest.mark.parametrize("malformation", ["wrong-type", "invalid-unicode", "bad-citation-type"])
def test_custom_malformed_fields_fail_with_stable_chunk_error(malformation: str) -> None:
    selected_tokenizer = tokenizer()
    cfg = config("custom", function_id="fn_domain", function_version=7)
    doc = document("source")
    scope = custom_scope(selected_tokenizer)
    chunk = custom_spec(
        doc,
        cfg,
        selected_tokenizer,
        scope,
        ordinal=0,
        role="standalone",
        citations=[citation(doc.blocks[0])],
    )
    if malformation == "wrong-type":
        chunk = replace(chunk, content=7)  # type: ignore[arg-type]
    elif malformation == "invalid-unicode":
        chunk = replace(chunk, content="\ud800")
    else:
        bad_citation = dict(chunk.metadata["citations"][0])
        bad_citation["start"] = "zero"
        metadata = dict(chunk.metadata)
        metadata["citations"] = [bad_citation]
        chunk = replace(chunk, metadata=metadata)

    with pytest.raises(custom_module.CustomChunkOutputInvalid):
        custom_module.validate_custom_chunks(
            [chunk],
            doc=doc,
            cfg=cfg,
            tokenizer=selected_tokenizer,
            scope=scope,
            limits=custom_module.CustomChunkLimits(),
        )


async def test_custom_executor_cannot_mutate_validation_source_or_budget() -> None:
    selected_tokenizer = tokenizer()
    original = "x" * 40
    doc = document(original)
    cfg = config(
        "custom",
        function_id="fn_domain",
        function_version=7,
        child_tokens=32,
        min_chunk_tokens=1,
    )

    class MutatingExecutor:
        identity = "mutating-executor:v1"

        async def execute(
            self,
            ref: custom_module.FunctionVersionRef,
            callback_doc: ParsedDocument,
            callback_cfg: ChunkConfig,
            scope: custom_module.CustomChunkScope,
            limits: custom_module.CustomChunkLimits,
        ) -> list[ChunkSpec]:
            callback_doc.markdown = "y" * 40
            callback_doc.blocks[:] = [Block("paragraph", "y" * 40, 0)]
            callback_cfg.child_tokens = 96
            return [
                custom_spec(
                    callback_doc,
                    callback_cfg,
                    selected_tokenizer,
                    scope,
                    ordinal=0,
                    role="standalone",
                    citations=[citation(callback_doc.blocks[0])],
                )
            ]

    with pytest.raises(custom_module.CustomChunkOutputInvalid):
        await DocumentChunker(
            document_id=DOCUMENT_ID,
            tokenizer=selected_tokenizer,
            custom=MutatingExecutor(),
        ).chunk(doc, cfg)
    assert doc.markdown == original
    assert doc.blocks[0].text == original
    assert cfg.child_tokens == 32
