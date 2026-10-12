from uuid import uuid4

import pytest
from pydantic import ValidationError

from cairn.core.document_ir import Element, Formula, Location, Table, TableCell
from cairn.ingestion.base import Block, ParsedDocument
from cairn.ingestion.ir import bind_document, project_document


def test_ir_identity_tracks_content_not_runtime_noise():
    owner = {
        "workspace_id": uuid4(),
        "kb_id": uuid4(),
        "document_id": uuid4(),
        "revision": 1,
        "source_sha256": "a" * 64,
        "parser_fingerprint": "b" * 64,
        "context_fingerprint": "c" * 64,
    }
    parsed = ParsedDocument(
        "hello", [Block("paragraph", "hello", 0)], metadata={"pdf_worker_pid": 10}
    )
    first = bind_document(parsed, **owner)
    parsed.metadata["pdf_worker_pid"] = 20
    assert bind_document(parsed, **owner).parse_version == first.parse_version
    parsed.blocks = [Block("paragraph", "changed", 0)]
    assert bind_document(parsed, **owner).parse_version != first.parse_version
    assert project_document(first).blocks[0].element_id == first.elements[0].element_id


def test_table_span_projection_does_not_duplicate_content():
    table = Table(
        rows=2,
        columns=2,
        cells=[
            TableCell(text="merged", row_start=0, row_end=1, col_start=0, col_end=2),
            TableCell(text="0", row_start=1, row_end=2, col_start=0, col_end=1),
            TableCell(text="", row_start=1, row_end=2, col_start=1, col_end=2),
        ],
    )
    assert table.markdown().count("merged") == 1
    assert "0" in table.markdown()
    with pytest.raises(ValidationError):
        Table(rows=1, columns=1, cells=table.cells)


def test_location_and_formula_preserve_evidence():
    with pytest.raises(ValidationError):
        Location(page=1, width=100, height=100, bbox=(0, 0, float("nan"), 10))
    formula = Formula(native="x + y", status="unrecognized")
    element = Element(local_id="formula", order=0, type="formula", formula=formula)
    assert element.search_text == "x + y"
