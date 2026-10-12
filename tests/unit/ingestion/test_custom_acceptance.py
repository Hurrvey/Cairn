"""Coordinator acceptance for the untrusted custom chunk output boundary."""

from __future__ import annotations

from uuid import UUID

import pytest

from cairn.catalog.config import ChunkConfig
from cairn.catalog.dto import ChunkSpec
from cairn.ingestion.base import Block, ParsedDocument
from cairn.ingestion.custom import (
    CustomChunkLimits,
    CustomChunkOutputInvalid,
    CustomChunkScope,
    chunk_id,
    content_hash,
    validate_custom_chunks,
)


class ExactTokenizer:
    fingerprint = "acceptance-character-tokenizer"

    def count(self, text: str) -> int:
        return len(text)

    def truncate(self, text: str, limit: int) -> str:
        return text[:limit]


def _validate(case: str) -> list[ChunkSpec]:
    scope = CustomChunkScope(UUID(int=100), 1, ExactTokenizer.fingerprint, "pinned-config")
    config = ChunkConfig(
        strategy="custom",
        function_id="approved-function",
        function_version=1,
        child_tokens=32,
        child_overlap=0,
        min_chunk_tokens=1,
    )
    source = "x" * 40 if case == "over-budget" else "original source"
    doc = ParsedDocument(markdown=source, blocks=[Block(kind="paragraph", text=source, ordinal=0)])
    content = "invented result" if case == "fabricated-content" else source
    citations: list[dict[str, object]] = [
        {
            "block_ordinal": 0,
            "kind": "paragraph",
            "start": 0,
            "end": len(source),
            "heading_path": [],
            "page": None,
            "bbox": None,
            "source_url": None,
        }
    ]
    if case == "missing-citations":
        citations = []
    if case == "out-of-range-citation":
        citations[0]["end"] = 99999
    metadata: dict[str, object] = {
        "role": "standalone",
        "embed": case != "hidden-standalone",
        "language": "und",
        "heading_path": [],
        "page": None,
        "source_url": None,
        "ordinal": 0,
        "citations": citations,
        "oversized": False,
        "oversized_reason": None,
    }
    if case == "non-finite-metadata":
        metadata["score"] = float("nan")
    digest = content_hash(content)
    chunk = ChunkSpec(
        id=chunk_id(scope, ordinal=0, role="standalone", content_hash=digest, citations=citations),
        document_id=scope.document_id,
        ordinal=0,
        content=content,
        content_hash=digest,
        token_count=len(content),
        metadata=metadata,
        parent_id=UUID(int=999) if case == "dangling-parent" else None,
    )
    return validate_custom_chunks(
        [] if case == "empty-output" else [chunk],
        doc=doc,
        cfg=config,
        tokenizer=ExactTokenizer(),
        scope=scope,
        limits=CustomChunkLimits(),
    )


def test_custom_valid_source_preserving_output_is_accepted() -> None:
    assert _validate("valid")[0].content == "original source"


@pytest.mark.parametrize(
    "case",
    [
        "fabricated-content",
        "missing-citations",
        "out-of-range-citation",
        "over-budget",
        "hidden-standalone",
        "non-finite-metadata",
        "dangling-parent",
        "empty-output",
    ],
)
def test_custom_rejects_unsafe_source_and_budget_output(case: str) -> None:
    with pytest.raises(CustomChunkOutputInvalid):
        _validate(case)
