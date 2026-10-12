from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from hashlib import sha256
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
from cairn.embedding.tokenizers import HuggingFaceTokenizer, TiktokenTokenizer, Tokenizer
from cairn.ingestion.base import Block, ParsedDocument
from cairn.ingestion.chunkers import DocumentChunker
from cairn.ingestion.errors import ChunkBudgetExceeded

DOCUMENT_ID = UUID("ae39ed02-5895-4e90-90f5-95940d8024a8")


def byte_tokenizer() -> TiktokenTokenizer:
    return TiktokenTokenizer(
        tiktoken.Encoding(
            name="model-bounded-chunk-test-byte",
            pat_str=r"(?s).",
            mergeable_ranks={bytes([token_id]): token_id for token_id in range(256)},
            special_tokens={},
        )
    )


def special_tokenizer() -> HuggingFaceTokenizer:
    backend = HFBackend(
        WordLevel({"[UNK]": 0, "[CLS]": 1, "[SEP]": 2, "word": 3}, unk_token="[UNK]")
    )
    backend.pre_tokenizer = Whitespace()
    backend.post_processor = TemplateProcessing(
        single="[CLS] $A [SEP]",
        special_tokens=[("[CLS]", 1), ("[SEP]", 2)],
    )
    return HuggingFaceTokenizer(backend)


class SpecialByteTokenizer:
    fingerprint = "model-bounded-special-byte:v1"

    def __init__(self) -> None:
        self._delegate = byte_tokenizer()

    def count(self, text: str) -> int:
        return self._delegate.count(text) + 2

    def truncate(self, text: str, limit: int) -> str:
        if limit < 2:
            raise ValueError("token limit cannot accommodate special tokens")
        return self._delegate.truncate(text, limit - 2)


def config(strategy: str = "fixed", **changes: object) -> ChunkConfig:
    return ChunkConfig.model_validate(
        {
            "strategy": strategy,
            "child_tokens": 96,
            "child_overlap": 0,
            "parent_tokens": 192,
            "min_chunk_tokens": 1,
            **changes,
        }
    )


def normalized(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).split())


def searchable(chunks: list[ChunkSpec]) -> list[ChunkSpec]:
    return [chunk for chunk in chunks if chunk.metadata["embed"]]


def assert_model_bounded(
    chunks: list[ChunkSpec], tokenizer: Tokenizer, max_input_tokens: int
) -> None:
    for chunk in searchable(chunks):
        assert tokenizer.count(chunk.content) <= max_input_tokens
        assert tokenizer.count(normalized(chunk.content)) <= max_input_tokens


def assert_source_coverage(doc: ParsedDocument, chunks: list[ChunkSpec]) -> None:
    for block in doc.blocks:
        if not block.text.strip():
            continue
        ranges = sorted(
            (citation["start"], citation["end"])
            for chunk in searchable(chunks)
            for citation in chunk.metadata["citations"]
            if citation["block_ordinal"] == block.ordinal
        )
        position = 0
        for start, end in ranges:
            assert start <= position < end
            position = max(position, end)
        assert position == len(block.text)


@pytest.mark.parametrize("strategy", ["fixed", "recursive", "markdown"])
async def test_builtin_searchable_chunks_obey_model_limit(strategy: str) -> None:
    tokenizer = byte_tokenizer()
    text = "alpha beta gamma delta epsilon zeta eta theta"
    doc = ParsedDocument(markdown=text, blocks=[Block("paragraph", text, 0)])

    chunks = await DocumentChunker(
        document_id=DOCUMENT_ID,
        tokenizer=tokenizer,
        max_input_tokens=16,
    ).chunk(doc, config(strategy))

    assert_model_bounded(chunks, tokenizer, 16)
    assert_source_coverage(doc, chunks)


async def test_protected_table_is_split_without_losing_source_when_model_requires_it() -> None:
    tokenizer = byte_tokenizer()
    table = "| key | value |\n" * 12
    doc = ParsedDocument(markdown=table, blocks=[Block("table", table, 7, ("Data",), page=3)])

    chunks = await DocumentChunker(
        document_id=DOCUMENT_ID,
        tokenizer=tokenizer,
        max_input_tokens=32,
    ).chunk(doc, config())

    assert len(chunks) > 1
    assert_model_bounded(chunks, tokenizer, 32)
    assert_source_coverage(doc, chunks)
    assert all(chunk.metadata["heading_path"] == ["Data"] for chunk in chunks)
    assert all(chunk.metadata["citations"][0]["page"] == 3 for chunk in chunks)


async def test_parent_may_exceed_model_limit_while_children_remain_bounded() -> None:
    tokenizer = byte_tokenizer()
    text = "0123456789" * 12
    doc = ParsedDocument(markdown=text, blocks=[Block("paragraph", text, 0)])

    chunks = await DocumentChunker(
        document_id=DOCUMENT_ID,
        tokenizer=tokenizer,
        max_input_tokens=16,
    ).chunk(doc, config("parent_child"))

    parents = [chunk for chunk in chunks if chunk.metadata["role"] == "parent"]
    assert any(tokenizer.count(parent.content) > 16 for parent in parents)
    assert_model_bounded(chunks, tokenizer, 16)
    assert_source_coverage(doc, chunks)


async def test_parent_child_omits_unusable_huge_heading_prefix() -> None:
    tokenizer = byte_tokenizer()
    heading = "H" * 240
    body = "source text remains searchable under its original heading"
    doc = ParsedDocument(
        markdown=body,
        blocks=[Block("paragraph", body, 0, (heading,), page=5)],
    )

    chunks = await DocumentChunker(
        document_id=DOCUMENT_ID,
        tokenizer=tokenizer,
        max_input_tokens=24,
    ).chunk(doc, config("parent_child"))

    parents = [chunk for chunk in chunks if chunk.metadata["role"] == "parent"]
    assert parents
    assert all(chunk.metadata["heading_path"] == [heading] for chunk in chunks)
    assert all(
        citation["heading_path"] == [heading]
        for chunk in chunks
        for citation in chunk.metadata["citations"]
    )
    assert_model_bounded(chunks, tokenizer, 24)
    assert_source_coverage(doc, chunks)


async def test_nfkc_normalized_and_raw_forms_are_both_bounded() -> None:
    tokenizer = SpecialByteTokenizer()
    text = "¼" * 24
    doc = ParsedDocument(markdown=text, blocks=[Block("paragraph", text, 0)])
    assert tokenizer.count(normalized(text)) > tokenizer.count(text)

    chunks = await DocumentChunker(
        document_id=DOCUMENT_ID,
        tokenizer=tokenizer,
        max_input_tokens=12,
    ).chunk(doc, config())

    assert_model_bounded(chunks, tokenizer, 12)
    assert_source_coverage(doc, chunks)


async def test_model_limit_includes_tokenizer_special_tokens() -> None:
    tokenizer = special_tokenizer()
    text = "word " * 20
    doc = ParsedDocument(markdown=text, blocks=[Block("paragraph", text, 0)])

    chunks = await DocumentChunker(
        document_id=DOCUMENT_ID,
        tokenizer=tokenizer,
        max_input_tokens=6,
    ).chunk(doc, config())

    assert_model_bounded(chunks, tokenizer, 6)
    assert_source_coverage(doc, chunks)


async def test_huge_heading_keeps_provenance_when_prefix_cannot_fit() -> None:
    tokenizer = byte_tokenizer()
    heading = "H" * 80
    body = "source text remains fully searchable"
    doc = ParsedDocument(
        markdown=f"{heading}\n\n{body}",
        blocks=[
            Block("heading", heading, 0, (heading,), page=4),
            Block("paragraph", body, 1, (heading,), page=4),
        ],
    )

    chunks = await DocumentChunker(
        document_id=DOCUMENT_ID,
        tokenizer=tokenizer,
        max_input_tokens=16,
    ).chunk(doc, config())

    assert_model_bounded(chunks, tokenizer, 16)
    assert_source_coverage(doc, chunks)
    assert all(chunk.metadata["heading_path"] == [heading] for chunk in chunks)
    assert all(
        citation["heading_path"] == [heading]
        for chunk in chunks
        for citation in chunk.metadata["citations"]
    )


async def test_semantic_boundary_inputs_and_searchable_chunks_obey_model_limit() -> None:
    tokenizer = byte_tokenizer()
    embedded: list[str] = []

    async def embed(texts: Sequence[str]) -> Sequence[Sequence[float]]:
        embedded.extend(texts)
        return [[1.0, float(index % 2)] for index, _text in enumerate(texts)]

    text = "alpha sentence. beta sentence. gamma sentence. delta sentence."
    doc = ParsedDocument(markdown=text, blocks=[Block("paragraph", text, 0)])
    chunks = await DocumentChunker(
        document_id=DOCUMENT_ID,
        tokenizer=tokenizer,
        max_input_tokens=16,
        semantic=semantic_module.CallbackSemanticEmbedding(
            identity="model-bounded-semantic:v1",
            callback=embed,
        ),
    ).chunk(doc, config("semantic"))

    assert embedded
    assert all(tokenizer.count(text) <= 16 for text in embedded)
    assert all(tokenizer.count(normalized(text)) <= 16 for text in embedded)
    assert_model_bounded(chunks, tokenizer, 16)
    assert_source_coverage(doc, chunks)


async def test_custom_oversized_searchable_output_fails_explicitly() -> None:
    tokenizer = byte_tokenizer()
    table = "| key | value |\n" * 6
    doc = ParsedDocument(markdown=table, blocks=[Block("table", table, 0)])

    class OversizedCustomExecutor:
        identity = "oversized-custom:v1"

        async def execute(
            self,
            ref: custom_module.FunctionVersionRef,
            callback_doc: ParsedDocument,
            cfg: ChunkConfig,
            scope: custom_module.CustomChunkScope,
            limits: custom_module.CustomChunkLimits,
        ) -> list[ChunkSpec]:
            del ref, limits
            citations = [
                {
                    "block_ordinal": 0,
                    "kind": "table",
                    "start": 0,
                    "end": len(table),
                    "heading_path": [],
                    "page": None,
                    "bbox": None,
                    "source_url": None,
                }
            ]
            digest = sha256(table.encode()).hexdigest()
            token_count = tokenizer.count(table)
            return [
                ChunkSpec(
                    id=custom_module.chunk_id(
                        scope,
                        ordinal=0,
                        role="standalone",
                        content_hash=digest,
                        citations=citations,
                    ),
                    document_id=scope.document_id,
                    ordinal=0,
                    content=table,
                    content_hash=digest,
                    token_count=token_count,
                    metadata={
                        "role": "standalone",
                        "embed": True,
                        "language": callback_doc.language,
                        "heading_path": [],
                        "page": None,
                        "source_url": None,
                        "ordinal": 0,
                        "citations": citations,
                        "oversized": token_count > cfg.child_tokens,
                        "oversized_reason": "table" if token_count > cfg.child_tokens else None,
                    },
                )
            ]

    with pytest.raises(ChunkBudgetExceeded, match="Custom"):
        await DocumentChunker(
            document_id=DOCUMENT_ID,
            tokenizer=tokenizer,
            max_input_tokens=32,
            custom=OversizedCustomExecutor(),
        ).chunk(
            doc,
            config("custom", function_id="fn_oversized", function_version=1),
        )


async def test_omitting_model_limit_preserves_existing_behavior() -> None:
    tokenizer = byte_tokenizer()
    table = "| key | value |\n" * 8
    doc = ParsedDocument(markdown=table, blocks=[Block("table", table, 0)])

    original = await DocumentChunker(document_id=DOCUMENT_ID, tokenizer=tokenizer).chunk(
        doc, config()
    )
    explicit_none = await DocumentChunker(
        document_id=DOCUMENT_ID,
        tokenizer=tokenizer,
        max_input_tokens=None,
    ).chunk(doc, config())
    generous_limit = await DocumentChunker(
        document_id=DOCUMENT_ID,
        tokenizer=tokenizer,
        max_input_tokens=512,
    ).chunk(doc, config())

    assert original == explicit_none == generous_limit
    assert len(original) == 1


@pytest.mark.parametrize("strategy", ["fixed", "recursive", "markdown", "parent_child"])
async def test_enabled_limit_keeps_in_budget_chunk_ids_and_results(strategy: str) -> None:
    tokenizer = byte_tokenizer()
    text = "short source text"
    doc = ParsedDocument(markdown=text, blocks=[Block("paragraph", text, 0, ("Guide",))])

    original = await DocumentChunker(document_id=DOCUMENT_ID, tokenizer=tokenizer).chunk(
        doc, config(strategy)
    )
    bounded = await DocumentChunker(
        document_id=DOCUMENT_ID,
        tokenizer=tokenizer,
        max_input_tokens=128,
    ).chunk(doc, config(strategy))

    assert bounded == original
    assert [chunk.id for chunk in bounded] == [chunk.id for chunk in original]


async def test_enabled_limit_keeps_in_budget_semantic_inputs_and_ids() -> None:
    tokenizer = byte_tokenizer()
    seen: list[list[str]] = []

    async def embed(texts: Sequence[str]) -> Sequence[Sequence[float]]:
        seen.append(list(texts))
        return [[1.0, float("beta" in text)] for text in texts]

    semantic = semantic_module.CallbackSemanticEmbedding(
        identity="in-budget-identity:v1",
        callback=embed,
    )
    text = "alpha sentence.  beta sentence."
    doc = ParsedDocument(markdown=text, blocks=[Block("paragraph", text, 0)])

    original = await DocumentChunker(
        document_id=DOCUMENT_ID,
        tokenizer=tokenizer,
        semantic=semantic,
    ).chunk(doc, config("semantic"))
    original_inputs = seen.pop()
    bounded = await DocumentChunker(
        document_id=DOCUMENT_ID,
        tokenizer=tokenizer,
        max_input_tokens=128,
        semantic=semantic,
    ).chunk(doc, config("semantic"))

    assert seen == [original_inputs]
    assert bounded == original
    assert [chunk.id for chunk in bounded] == [chunk.id for chunk in original]
