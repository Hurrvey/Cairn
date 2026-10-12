from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from cairn.catalog.dto import ChunkSpec
from cairn.core.document_metadata import (
    DocumentFacts,
    DocumentMetadataTooLarge,
    check_document_metadata,
)
from cairn.core.sparse import SparseSpec
from cairn.ingestion.pipeline import (
    PipelineArtifactError,
    _attach_parent_snapshots,
    _chunk_reembed_payload,
    _manual_reembed_token_counts,
    _points,
)
from cairn.vectorstore.base import SparseVector


def test_index_payload_persists_exact_chunk_token_count() -> None:
    document_id = uuid4()
    kb_id = uuid4()
    chunk = ChunkSpec(
        id=uuid4(),
        document_id=document_id,
        ordinal=0,
        content="Saturn has rings.",
        content_hash="a" * 64,
        token_count=7,
        metadata={"page": 4},
    )
    run = SimpleNamespace(
        document_id=document_id,
        kb_id=kb_id,
        index_version=1,
        revision=2,
        sparse=SparseSpec(),
        document=None,
        payload_schema=1,
        ordinal=0,
    )
    chunks = SimpleNamespace(chunks=(chunk,))
    terms = SparseVector((4,), (1.0,))
    embeddings = SimpleNamespace(
        vectors={chunk.content_hash: (1.0, 2.0, 3.0)}, sparse={chunk.content_hash: terms}
    )

    point = _points(run, chunks, embeddings)[0]

    assert point.payload["token_count"] == 7
    assert point.sparse == terms


def test_manual_reembedding_payload_persists_remeasured_token_count() -> None:
    target = SimpleNamespace(
        metadata={"page": 4},
        chunk_id=uuid4(),
        content="The Mariana Trench lies in the Pacific.",
        content_hash="b" * 64,
        document_id=uuid4(),
        index_version=2,
        kb_id=uuid4(),
        parent_id=None,
        revision=3,
        edit_generation=2,
        document=None,
        payload_schema=1,
        ordinal=0,
    )

    payload = _chunk_reembed_payload(target, actual_token_count=9)

    assert payload["token_count"] == 9


def test_manual_reembedding_distinguishes_embedding_and_raw_response_token_counts() -> None:
    class ExactTokenizer:
        def count(self, text: str) -> int:
            return len(text)

    raw = "  \uff21   B  "
    embedding_count, response_count = _manual_reembed_token_counts(ExactTokenizer(), raw)

    assert embedding_count == len("A B")
    assert response_count == len(raw)
    assert response_count > embedding_count


def test_fresh_chunk_payload_embeds_revision_scoped_parent_snapshot() -> None:
    document_id = uuid4()
    kb_id = uuid4()
    parent_id = uuid4()
    child_id = uuid4()
    run = SimpleNamespace(
        document_id=document_id,
        kb_id=kb_id,
        index_version=3,
        revision=5,
    )
    parent = ChunkSpec(
        id=parent_id,
        document_id=document_id,
        ordinal=0,
        content="Complete parent context",
        content_hash="c" * 64,
        token_count=11,
        metadata={
            "embed": False,
            "role": "parent",
            "page": 2,
            "source_url": "https://example.test/parent",
            "heading_path": ["Parent"],
            "citations": [{"page": 2, "bbox": [1.0, 2.0, 3.0, 4.0]}],
        },
    )
    child = ChunkSpec(
        id=child_id,
        document_id=document_id,
        ordinal=1,
        content="matched child",
        content_hash="d" * 64,
        token_count=4,
        parent_id=parent_id,
        metadata={"embed": True, "role": "child"},
    )

    enriched = _attach_parent_snapshots(run, [parent, child], edited_ordinals={1})
    snapshot = enriched[1].metadata["_parent_snapshot"]

    assert snapshot == {
        "id": str(parent_id),
        "content": "Complete parent context",
        "token_count": 11,
        "document_id": str(document_id),
        "kb_id": str(kb_id),
        "index_version": 3,
        "revision": 5,
        "metadata": {
            "heading_path": ["Parent"],
            "page": 2,
            "source_url": "https://example.test/parent",
            "citations": [{"page": 2, "bbox": [1.0, 2.0, 3.0, 4.0]}],
        },
    }
    assert enriched[1].metadata["_manual_edit_preserved"] is True


FACTS = DocumentFacts(
    metadata={"lang": "en", "tags": ["ops"]},
    title="Ops Handbook",
    type="pdf",
    created_at=datetime(2026, 1, 2, tzinfo=UTC),
    revised_at=datetime(2026, 3, 4, tzinfo=UTC),
)


def test_schema_two_cannot_write_points_without_document_facts() -> None:
    run = SimpleNamespace(payload_schema=2, document=None)
    with pytest.raises(PipelineArtifactError, match="document facts"):
        _points(run, SimpleNamespace(chunks=()), SimpleNamespace(vectors={}, sparse={}))


def test_schema_two_cannot_reembed_without_document_facts() -> None:
    target = SimpleNamespace(payload_schema=2, document=None)
    with pytest.raises(PipelineArtifactError, match="document facts"):
        _chunk_reembed_payload(target)


def test_metadata_bounds_count_lists_as_containers() -> None:
    with pytest.raises(DocumentMetadataTooLarge, match="deep"):
        check_document_metadata({"a": [[[{"b": 1}]]]})
    assert check_document_metadata({"a": [[{"b": 1}]]}) == {"a": [[{"b": 1}]]}


def test_metadata_rejects_extreme_nesting_before_serialization() -> None:
    value: object = "value"
    for _ in range(1500):
        value = [value]
    with pytest.raises(DocumentMetadataTooLarge):
        check_document_metadata({"a": value})


def test_metadata_rejects_non_json_containers() -> None:
    with pytest.raises(DocumentMetadataTooLarge):
        check_document_metadata({"a": (1, 2)})


def test_index_payload_carries_document_facts_that_chunk_metadata_cannot_override() -> None:
    document_id, kb_id = uuid4(), uuid4()
    chunk = ChunkSpec(
        id=uuid4(),
        document_id=document_id,
        ordinal=0,
        content="Saturn has rings.",
        content_hash="c" * 64,
        token_count=7,
        # A custom chunker may emit arbitrary keys; the document's own win.
        metadata={"page": 4, "meta": {"lang": "xx"}, "doc_title": "forged"},
    )
    run = SimpleNamespace(
        document_id=document_id,
        kb_id=kb_id,
        index_version=1,
        revision=2,
        sparse=SparseSpec(),
        document=FACTS,
        payload_schema=2,
    )
    embeddings = SimpleNamespace(vectors={chunk.content_hash: (1.0, 2.0)}, sparse={})

    payload = _points(run, SimpleNamespace(chunks=(chunk,)), embeddings)[0].payload

    assert payload["meta"] == {"lang": "en", "tags": ["ops"]}
    assert payload["doc_title"] == "Ops Handbook"
    assert payload["doc_type"] == "pdf"
    assert payload["created_at"] == int(datetime(2026, 1, 2, tzinfo=UTC).timestamp())
    assert payload["doc_updated_at"] == int(datetime(2026, 3, 4, tzinfo=UTC).timestamp())
    assert payload["page"] == 4
    assert payload["ordinal"] == 0


def test_manual_reembedding_payload_carries_the_same_document_facts() -> None:
    target = SimpleNamespace(
        metadata={"page": 1},
        chunk_id=uuid4(),
        content="Rings.",
        content_hash="d" * 64,
        document_id=uuid4(),
        index_version=2,
        kb_id=uuid4(),
        parent_id=None,
        revision=3,
        edit_generation=1,
        document=FACTS,
        payload_schema=2,
        ordinal=7,
    )

    payload = _chunk_reembed_payload(target)

    assert payload["meta"] == {"lang": "en", "tags": ["ops"]}
    assert payload["doc_updated_at"] == int(datetime(2026, 3, 4, tzinfo=UTC).timestamp())
    assert payload["ordinal"] == 7


def test_oversized_legacy_document_metadata_fails_indexing() -> None:
    facts = DocumentFacts(
        metadata={"blob": "x" * 9000},
        title=None,
        type=None,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        revised_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    with pytest.raises(DocumentMetadataTooLarge):
        facts.payload()
