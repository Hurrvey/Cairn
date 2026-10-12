from __future__ import annotations

import asyncio
from dataclasses import replace
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
import tiktoken
from tokenizers import Tokenizer as HFTokenizer
from tokenizers.models import WordLevel
from tokenizers.pre_tokenizers import Whitespace
from tokenizers.processors import TemplateProcessing

from cairn.catalog.config import ChunkConfig
from cairn.catalog.dto import ChunkSpec
from cairn.embedding.tokenizers import HuggingFaceTokenizer, TiktokenTokenizer, Tokenizer
from cairn.ingestion.base import Block, ParseContext, ParsedDocument
from cairn.ingestion.chunkers import DocumentChunker
from cairn.ingestion.errors import ChunkBudgetExceeded, ChunkUnsupportedStrategy
from cairn.ingestion.parsers import MarkdownParser

DOCUMENT_ID = UUID("ef240aa4-bdfc-4fda-9fd4-7d3830958f83")


@pytest.fixture
def tokenizer() -> TiktokenTokenizer:
    return TiktokenTokenizer(
        tiktoken.Encoding(
            name="chunk-test-byte",
            pat_str=r"(?s).",
            mergeable_ranks={bytes([token_id]): token_id for token_id in range(256)},
            special_tokens={},
        )
    )


def config(strategy: str = "fixed", **changes: object) -> ChunkConfig:
    return ChunkConfig.model_validate(
        {
            "strategy": strategy,
            "child_tokens": 48,
            "child_overlap": 8,
            "parent_tokens": 128,
            "min_chunk_tokens": 8,
            **changes,
        }
    )


def document(text: str, **changes: object) -> ParsedDocument:
    return ParsedDocument(markdown=text, blocks=[Block("paragraph", text, 0, **changes)])


async def chunk(
    doc: ParsedDocument,
    cfg: ChunkConfig,
    tokenizer: Tokenizer,
    document_id: UUID = DOCUMENT_ID,
    index_version: int = 1,
) -> list[ChunkSpec]:
    return await DocumentChunker(
        document_id=document_id,
        tokenizer=tokenizer,
        index_version=index_version,
    ).chunk(doc, cfg)


def leaves(chunks: list[ChunkSpec]) -> list[ChunkSpec]:
    return [item for item in chunks if item.metadata["embed"]]


def assert_source_coverage(doc: ParsedDocument, chunks: list[ChunkSpec]) -> None:
    for block in doc.blocks:
        ranges = sorted(
            (citation["start"], citation["end"])
            for item in leaves(chunks)
            for citation in item.metadata["citations"]
            if citation["block_ordinal"] == block.ordinal
        )
        position = 0
        for start, end in ranges:
            assert start <= position, (block.ordinal, position, start)
            assert start < end <= len(block.text)
            position = max(position, end)
        assert position == len(block.text)


async def test_fixed_exact_budget_overlap_and_hash(tokenizer: Tokenizer) -> None:
    """FR-F-05/07/11: windows use actual tokens and retain every source character."""
    text = "0123456789" * 15
    chunks = await chunk(document(text), config(), tokenizer)
    assert chunks[0].content == text[:48]
    assert chunks[1].content == text[40:88]
    assert [item.ordinal for item in chunks] == list(range(len(chunks)))
    for item in chunks:
        assert item.token_count == tokenizer.count(item.content) <= 48
        assert item.content_hash == sha256(item.content.encode()).hexdigest()
        assert item.document_id == DOCUMENT_ID
        assert item.parent_id is None
        assert item.metadata["role"] == "standalone"
    assert_source_coverage(document(text), chunks)


@pytest.mark.parametrize("strategy", ["fixed", "recursive", "markdown", "parent_child"])
async def test_cjk_no_lost_characters_or_invalid_unicode(
    strategy: str, tokenizer: Tokenizer
) -> None:
    """TC-M07-06, FR-F-11: CJK and emoji are sliced at Unicode-safe model boundaries."""
    doc = document("中文知识检索。包含表格\uff01还有代码\uff1f引用来源\uff1b🙂结束。" * 12)
    chunks = await chunk(doc, config(strategy), tokenizer)
    assert_source_coverage(doc, chunks)
    for item in chunks:
        assert "\ufffd" not in item.content
        assert item.token_count == len(item.content.encode())
        assert item.token_count <= (128 if item.metadata["role"] == "parent" else 48)


@pytest.mark.parametrize("separator", ["。", "\uff01", "\uff1f", "\uff1b", ". ", "! ", "? ", "; "])
async def test_recursive_sentence_boundaries(separator: str, tokenizer: Tokenizer) -> None:
    """FR-F-05: CJK and Latin punctuation are recursive fallback separators."""
    sentence = "abcdefghi" + separator
    chunks = await chunk(document(sentence * 12), config("recursive", child_overlap=0), tokenizer)
    assert all(item.content.endswith(separator) for item in chunks)
    assert_source_coverage(document(sentence * 12), chunks)


async def test_recursive_prefers_paragraph_over_sentence(tokenizer: Tokenizer) -> None:
    """FR-F-05: separator hierarchy takes precedence over later sentence boundaries."""
    text = "first paragraph.\n\nsecond sentence. " + "x" * 70
    chunks = await chunk(document(text), config("recursive", child_overlap=0), tokenizer)
    assert chunks[0].content == "first paragraph.\n\n"
    assert_source_coverage(document(text), chunks)


async def test_small_fragment_merges_forward(tokenizer: Tokenizer) -> None:
    """TC-M07-11: a sub-minimum paragraph is packed with subsequent content."""
    text = "a\n\n" + "b" * 42 + "\n\n" + "c" * 70
    chunks = await chunk(document(text), config("recursive", child_overlap=0), tokenizer)
    assert chunks[0].content.startswith("a\n\nb")
    assert chunks[0].token_count >= 8
    assert_source_coverage(document(text), chunks)


async def test_markdown_never_crosses_repeated_heading(tokenizer: Tokenizer) -> None:
    """TC-M07-07/10: section boundaries survive repeated names and short sections."""
    doc = await MarkdownParser().parse(
        b"# Root\n\n## Same\n\nAAA\n\n## Same\n\nBBB\n\n## Next\n\nCCC",
        ParseContext(),
    )
    chunks = await chunk(doc, config("markdown"), tokenizer)
    assert not any("AAA" in item.content and "BBB" in item.content for item in chunks)
    assert not any("BBB" in item.content and "CCC" in item.content for item in chunks)
    assert all(
        item.content.startswith(" > ".join(item.metadata["heading_path"]) + "\n\n")
        for item in chunks
    )
    assert_source_coverage(doc, chunks)


async def test_parent_child_links_and_containment(tokenizer: Tokenizer) -> None:
    """TC-M07-08: parents are unembedded and each child belongs to one larger window."""
    doc = document("abcdefghij " * 50, heading_path=("Guide",))
    chunks = await chunk(doc, config("parent_child"), tokenizer)
    parents = {item.id: item for item in chunks if item.metadata["role"] == "parent"}
    assert len(parents) > 1
    assert all(not item.metadata["embed"] and item.parent_id is None for item in parents.values())
    for child in leaves(chunks):
        parent = parents[child.parent_id]
        child_range = child.metadata["citations"][0]
        parent_range = parent.metadata["citations"][0]
        assert (
            parent_range["start"]
            <= child_range["start"]
            < child_range["end"]
            <= parent_range["end"]
        )
        assert child.content.removeprefix("Guide\n\n") in parent.content
    assert_source_coverage(doc, chunks)

    multipage = ParsedDocument(
        markdown="",
        blocks=[
            Block("paragraph", "abc " * 30, 10, ("Guide",), page=1),
            Block("paragraph", "def " * 40, 20, ("Guide",), page=2),
            Block("paragraph", "ghi " * 30, 30, ("Guide",), page=3),
        ],
    )
    chunks = await chunk(multipage, config("parent_child", separators=[]), tokenizer)
    parents = {item.id: item for item in chunks if item.metadata["role"] == "parent"}
    assert any(len(parent.metadata["citations"]) > 1 for parent in parents.values())
    for child in leaves(chunks):
        parent = parents[child.parent_id]
        ranges = {span["block_ordinal"]: span for span in parent.metadata["citations"]}
        for span in child.metadata["citations"]:
            parent_span = ranges[span["block_ordinal"]]
            assert parent_span["start"] <= span["start"] < span["end"] <= parent_span["end"]
            assert span["page"] == parent_span["page"]
    assert_source_coverage(multipage, chunks)


@pytest.mark.parametrize("strategy", ["fixed", "recursive", "markdown", "parent_child"])
async def test_table_stays_whole_and_is_flagged(strategy: str, tokenizer: Tokenizer) -> None:
    """TC-M07-09: oversized tables are isolated, intact and explicitly identified."""
    table = "| Key | Value |\n| --- | --- |\n" + "| entry | data |\n" * 30
    doc = ParsedDocument(
        markdown=table,
        blocks=[
            Block("paragraph", "before", 0),
            Block("table", table, 1, page=2),
            Block("paragraph", "after", 2),
        ],
    )
    chunks = await chunk(doc, config(strategy), tokenizer)
    tables = [item for item in leaves(chunks) if table in item.content]
    assert len(tables) == 1
    assert tables[0].content == table
    assert tables[0].metadata["oversized_reason"] == "table"
    assert tables[0].metadata["page"] == 2
    assert_source_coverage(doc, chunks)


async def test_table_splitting_can_be_enabled(tokenizer: Tokenizer) -> None:
    """TC-M07-09: the opt-out applies the regular token budget to tables."""
    doc = ParsedDocument(markdown="x" * 200, blocks=[Block("table", "x" * 200, 0)])
    chunks = await chunk(doc, config(keep_tables_intact=False), tokenizer)
    assert len(chunks) > 1
    assert all(item.token_count <= 48 and not item.metadata["oversized"] for item in chunks)
    assert_source_coverage(doc, chunks)


async def test_provenance_across_pages_and_blocks(tokenizer: Tokenizer) -> None:
    """TC-M07-12: every contributing block retains geometry, page and source URL."""
    blocks = [
        Block(
            "paragraph", "first", 10, ("Topic",), page=2, bbox=(1, 2, 3, 4), source_url="source:a"
        ),
        Block("paragraph", "second", 20, ("Topic",), page=3, source_url="source:b"),
    ]
    doc = ParsedDocument(markdown="first\n\nsecond", blocks=blocks, language="zh")
    chunks = await chunk(doc, config(), tokenizer)
    assert len(chunks) == 1
    item = chunks[0]
    assert item.metadata["language"] == "zh"
    assert item.metadata["page"] == 2
    assert item.metadata["source_url"] == "source:a"
    assert item.metadata["citations"] == [
        {
            "block_ordinal": 10,
            "kind": "paragraph",
            "start": 0,
            "end": 5,
            "heading_path": ["Topic"],
            "page": 2,
            "bbox": [1, 2, 3, 4],
            "source_url": "source:a",
        },
        {
            "block_ordinal": 20,
            "kind": "paragraph",
            "start": 0,
            "end": 6,
            "heading_path": ["Topic"],
            "page": 3,
            "bbox": None,
            "source_url": "source:b",
        },
    ]
    assert_source_coverage(doc, chunks)


async def test_ids_are_repeatable_scoped_and_sensitive_to_citations(tokenizer: Tokenizer) -> None:
    """NFR-R-06: retries are deterministic without colliding across documents or indexes."""
    doc = document("repeat " * 40, page=1)
    cfg = config("parent_child")
    original = await chunk(doc, cfg, tokenizer)
    assert original == await chunk(doc, cfg, tokenizer)
    ids = {item.id for item in original}
    assert len(ids) == len(original)
    changed_cfg = cfg.model_copy(update={"child_overlap": 7})
    assert ids.isdisjoint(item.id for item in await chunk(doc, changed_cfg, tokenizer))

    class AlternateFingerprint:
        fingerprint = tokenizer.fingerprint + "-changed"

        def count(self, text: str) -> int:
            return tokenizer.count(text)

        def truncate(self, text: str, limit: int) -> str:
            return tokenizer.truncate(text, limit)

    assert ids.isdisjoint(item.id for item in await chunk(doc, cfg, AlternateFingerprint()))
    assert ids.isdisjoint(item.id for item in await chunk(doc, cfg, tokenizer, uuid4()))
    assert ids.isdisjoint(item.id for item in await chunk(doc, cfg, tokenizer, index_version=2))
    moved = replace(doc, blocks=[replace(doc.blocks[0], page=2)])
    assert ids.isdisjoint(item.id for item in await chunk(moved, cfg, tokenizer))
    assert [item.content_hash for item in original] == [
        item.content_hash for item in await chunk(moved, cfg, tokenizer)
    ]


async def test_heading_budget_and_overlap_progress(tokenizer: Tokenizer) -> None:
    """TC-M07-10, FR-F-11: headers count against limits; large overlap cannot stall."""
    doc = document("abcdefghijklmnop" * 30, heading_path=("h" * 37,))
    chunks = await asyncio.wait_for(chunk(doc, config(child_overlap=40), tokenizer), timeout=5)
    assert all(
        item.content.startswith("h" * 37 + "\n\n") and item.token_count <= 48 for item in chunks
    )
    assert_source_coverage(doc, chunks)


async def test_heading_can_be_disabled(tokenizer: Tokenizer) -> None:
    """TC-M07-10: disabling injection preserves heading provenance."""
    chunks = await chunk(
        document("body", heading_path=("Heading",)), config(prepend_heading_path=False), tokenizer
    )
    assert chunks[0].content == "body"
    assert chunks[0].metadata["heading_path"] == ["Heading"]


@pytest.mark.parametrize("heading", ["h" * 48, "h" * 44])
async def test_impossible_budget_fails_without_dropping_text(
    heading: str, tokenizer: Tokenizer
) -> None:
    """FR-F-11: insufficient heading/CJK budget is a terminal, explicit failure."""
    with pytest.raises(ChunkBudgetExceeded) as caught:
        await chunk(document("中文" * 40, heading_path=(heading,)), config(), tokenizer)
    assert caught.value.retryable is False


@pytest.mark.parametrize("strategy", ["semantic", "custom"])
async def test_unimplemented_strategy_fails_explicitly(strategy: str, tokenizer: Tokenizer) -> None:
    """FR-F-05: unsupported strategies never silently change retrieval semantics."""
    with pytest.raises(ChunkUnsupportedStrategy):
        await chunk(document("hello"), config(strategy, function_id="fn_example"), tokenizer)


async def test_empty_input_has_no_chunks(tokenizer: Tokenizer) -> None:
    """FR-F-07: empty parser output cannot create phantom searchable chunks."""
    assert await chunk(ParsedDocument(markdown="", blocks=[]), config(), tokenizer) == []
    assert await chunk(document(" \n "), config(), tokenizer) == []


async def test_huggingface_counts_special_tokens_and_preserves_text() -> None:
    """FR-F-11: model special tokens consume budget; whitespace is not lost by truncation."""
    backend = HFTokenizer(
        WordLevel({"[UNK]": 0, "[CLS]": 1, "[SEP]": 2, "word": 3}, unk_token="[UNK]")
    )
    backend.pre_tokenizer = Whitespace()
    backend.post_processor = TemplateProcessing(
        single="[CLS] $A [SEP]",
        special_tokens=[("[CLS]", 1), ("[SEP]", 2)],
    )
    tokenizer = HuggingFaceTokenizer(backend)
    doc = document("word   " * 180)
    chunks = await chunk(doc, config(child_overlap=0), tokenizer)
    assert all(item.token_count <= 48 for item in chunks)
    assert "".join(item.content for item in chunks) == doc.markdown
    assert_source_coverage(doc, chunks)


async def test_separator_empty_values_cannot_stall(tokenizer: Tokenizer) -> None:
    """FR-F-05: configurable empty separators do not produce zero-length windows."""
    doc = document("abcdef " * 40)
    chunks = await chunk(doc, config("recursive", separators=["", "", "\n#"]), tokenizer)
    assert_source_coverage(doc, chunks)


@pytest.mark.parametrize("strategy", ["fixed", "recursive", "markdown", "parent_child"])
async def test_varied_lengths_and_overlap_cover_all_input(
    strategy: str, tokenizer: Tokenizer
) -> None:
    """FR-F-05/07: boundary cases do not omit source ranges or exceed token limits."""
    for length in (1, 31, 32, 47, 48, 49, 95, 96, 97, 129, 251):
        for overlap in (0, 7, 47):
            doc = document(("a中。🙂\n\nbc" * 40)[:length])
            chunks = await chunk(doc, config(strategy, child_overlap=overlap), tokenizer)
            assert_source_coverage(doc, chunks)
            assert all(
                item.token_count <= (128 if item.metadata["role"] == "parent" else 48)
                for item in chunks
            )


async def test_recursive_boundary_recounts_bpe_prefix() -> None:
    """FR-F-11: cutting inside a BPE merge can increase the prefix token count."""
    vocabulary = {bytes([token_id]): token_id for token_id in range(256)}
    vocabulary.update({b"bc": 256, b"bcd": 257, b"bcde": 258, b"abcde": 259})
    tokenizer = TiktokenTokenizer(
        tiktoken.Encoding(
            name="chunk-test-merges",
            pat_str=r"(?s).+",
            mergeable_ranks=vocabulary,
            special_tokens={},
        )
    )
    doc = document("x" * 47 + "abcde" + "x" * 100)
    chunks = await chunk(doc, config("recursive", separators=["ab"], child_overlap=0), tokenizer)
    assert all(item.token_count <= 48 for item in chunks)
    assert_source_coverage(doc, chunks)


async def test_large_document_tokenization_work_is_bounded(tokenizer: Tokenizer) -> None:
    """NFR-P-04: chunking does not re-tokenize the entire remaining document per window."""

    class CountingTokenizer:
        fingerprint = tokenizer.fingerprint
        examined = 0

        def count(self, text: str) -> int:
            self.examined += len(text)
            return tokenizer.count(text)

        def truncate(self, text: str, limit: int) -> str:
            self.examined += len(text)
            return tokenizer.truncate(text, limit)

    counter = CountingTokenizer()
    doc = document("x" * 12000)
    chunks = await chunk(doc, config(child_overlap=0), counter)
    assert_source_coverage(doc, chunks)
    assert counter.examined < len(doc.markdown) * 40


async def test_heading_budget_is_checked_in_full_tokenizer_context() -> None:
    """FR-F-11: a BPE merge across the header/body boundary can fit the exact budget."""
    vocabulary = {bytes([token_id]): token_id for token_id in range(256)}
    vocabulary.update({b"\n\n": 256, b"\n\nx": 257})
    tokenizer = TiktokenTokenizer(
        tiktoken.Encoding(
            name="chunk-heading-merges",
            pat_str=r"(?s).+",
            mergeable_ranks=vocabulary,
            special_tokens={},
        )
    )
    doc = document("x", heading_path=("h" * 47,))
    chunks = await chunk(doc, config(), tokenizer)
    assert chunks[0].content == "h" * 47 + "\n\nx"
    assert chunks[0].token_count == 48


async def test_joining_whitespace_cannot_create_phantom_chunks(tokenizer: Tokenizer) -> None:
    """FR-F-07: synthetic block separators never become citation-free index rows."""
    doc = ParsedDocument(
        markdown="a\n\nb",
        blocks=[
            Block("paragraph", "a", 0, heading_path=("h" * 45,)),
            Block("paragraph", "b", 1, heading_path=("h" * 45,)),
        ],
    )
    chunks = await chunk(doc, config(child_overlap=0), tokenizer)
    assert len(chunks) == 2
    assert all(item.metadata["citations"] for item in chunks)
    assert_source_coverage(doc, chunks)


@pytest.mark.parametrize("index_version", [0, -1])
def test_invalid_index_version_is_rejected(index_version: int, tokenizer: Tokenizer) -> None:
    """NFR-R-06: chunk identity is never scoped to an invalid catalog index version."""
    with pytest.raises(ValueError, match="index_version"):
        DocumentChunker(document_id=DOCUMENT_ID, tokenizer=tokenizer, index_version=index_version)


async def test_non_prefix_tokenizer_fails_without_rewriting_source(tokenizer: Tokenizer) -> None:
    """FR-F-07/11: a nonconforming tokenizer must not silently corrupt source ranges."""

    class NonPrefixTokenizer:
        fingerprint = tokenizer.fingerprint

        def count(self, text: str) -> int:
            return tokenizer.count(text)

        def truncate(self, text: str, limit: int) -> str:
            return "rewritten"

    with pytest.raises(ChunkBudgetExceeded, match="original-text prefix"):
        await chunk(document("x" * 100), config(), NonPrefixTokenizer())


async def test_special_tokens_exceeding_budget_have_stable_chunk_error() -> None:
    """FR-F-11: model-added tokens exceeding the target produce a terminal chunk error."""
    backend = HFTokenizer(
        WordLevel({"[UNK]": 0, "[CLS]": 1, "[SEP]": 2, "word": 3}, unk_token="[UNK]")
    )
    backend.pre_tokenizer = Whitespace()
    backend.post_processor = TemplateProcessing(
        single=" ".join(["[CLS]"] * 48 + ["$A", "[SEP]"]),
        special_tokens=[("[CLS]", 1), ("[SEP]", 2)],
    )
    tokenizer = HuggingFaceTokenizer(backend)
    with pytest.raises(ChunkBudgetExceeded) as caught:
        await chunk(document("word " * 60), config(), tokenizer)
    assert caught.value.retryable is False
