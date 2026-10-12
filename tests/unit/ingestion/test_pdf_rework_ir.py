from uuid import uuid4

import pytest

from cairn.catalog.service import parsed_markdown
from cairn.core.document_ir import DocumentIR, Element, Location, Table, TableCell
from cairn.core.document_projection import project_ir
from cairn.ingestion.artifacts import ParseManifest, encode_parse_manifest
from cairn.ingestion.base import Block, ParsedDocument
from cairn.ingestion.ir import bind_document, project_document


def bind(elements=(), blocks=(), metadata=None):
    return bind_document(
        ParsedDocument("", list(blocks), elements=list(elements), metadata=metadata or {}, pages=2),
        workspace_id=uuid4(),
        kb_id=uuid4(),
        document_id=uuid4(),
        revision=1,
        source_sha256="a" * 64,
        parser_fingerprint="b" * 64,
        context_fingerprint="c" * 64,
    )


def loc(page, **kwargs):
    return Location(page=page, width=100, height=100, bbox=(0, 0, 80, 80), **kwargs)


def test_multipage_table_without_cell_source_degrades_to_unplaced_text():
    element = Element(
        local_id="t",
        order=0,
        type="table",
        locations=(loc(1), loc(2)),
        table=Table(
            rows=2,
            columns=1,
            cells=[
                TableCell(text=str(row), row_start=row, row_end=row + 1, col_start=0, col_end=1)
                for row in (0, 1)
            ],
        ),
    )
    ir = bind([element])
    with pytest.raises(ValueError, match="UNSAFE_PROJECTION"):
        project_ir(ir, strict=True)
    projected = project_document(ir)
    assert [block.page for block in projected.blocks] == [None]
    assert [block.bbox for block in projected.blocks] == [None]
    assert projected.blocks[0].kind == "table"
    assert projected.markdown.splitlines() == ["| 0 |", "| --- |", "| 1 |"]


def test_content_uses_same_orig_span_and_order_projection():
    first = Element(
        local_id="a",
        order=0,
        type="text",
        text="normalized differs",
        orig="FIRSTSECOND",
        locations=(
            loc(1, char_start=0, char_end=5, span_source="orig"),
            loc(2, char_start=5, char_end=11, span_source="orig"),
        ),
    )
    ir = bind([Element(local_id="z", order=1, type="text", text="END"), first])
    raw = encode_parse_manifest(
        ParseManifest(
            ir.document_id,
            1,
            1,
            ir.source_sha256,
            ParsedDocument("", [], ir=ir),
            ir.parser_fingerprint,
            ir.context_fingerprint,
        )
    )
    assert parsed_markdown(raw) == project_document(ir).markdown == "FIRST\n\nSECOND\n\nEND"


def test_legacy_escaped_pipe_roundtrip():
    text = "| A\\|B | C |\n| --- | --- |\n| D\\|E | 0 |"
    ir = bind(blocks=[Block("table", text, 0)])
    assert project_document(ir).markdown == text
    assert ir.elements[0].table.columns == 2


def test_source_bbox_can_extend_past_page_without_loss():
    location = Location(page=1, width=100, height=100, bbox=(-3, 10, 80, 40))
    assert location.bbox == (-3, 10, 80, 40)
    assert location.display_bbox == (0, 10, 80, 40)
    assert Location(page=1, width=100, height=100, bbox=(-30, 10, -3, 40)).display_bbox is None


def test_new_identity_rejects_id_and_semantic_metadata_tampering():
    ir = bind(blocks=[Block("paragraph", "text", 0)], metadata={"pdf_pages": [{"width": 100}]})
    payload = ir.model_dump(mode="json")
    assert payload["identity_version"] == 2
    payload["elements"][0]["element_id"] = "e" * 64
    with pytest.raises(ValueError):
        DocumentIR.model_validate(payload)
    payload = ir.model_dump(mode="json")
    payload["warnings"] = ["changed"]
    with pytest.raises(ValueError):
        DocumentIR.model_validate(payload)
    payload = ir.model_dump(mode="json")
    payload["metadata"]["pdf_pages"][0]["width"] = 200
    with pytest.raises(ValueError):
        DocumentIR.model_validate(payload)


def test_multipage_table_with_cell_provenance_fragments_once():
    cells = [
        TableCell(
            text=f"row{row}",
            row_start=row,
            row_end=row + 1,
            col_start=0,
            col_end=1,
            locations=(loc(row + 1),),
        )
        for row in (0, 1)
    ]
    ir = bind(
        [
            Element(
                local_id="t",
                order=0,
                type="table",
                locations=(loc(1), loc(2)),
                table=Table(rows=2, columns=1, cells=cells),
            )
        ]
    )
    projected = project_document(ir)
    assert [block.page for block in projected.blocks] == [1, 2]
    assert projected.markdown.count("row0") == projected.markdown.count("row1") == 1
    assert len(ir.elements[0].table.cells) == 2


def test_multipage_image_has_no_guessed_projection():
    ir = bind(
        [
            Element(
                local_id="image",
                order=0,
                type="image",
                text="caption",
                locations=(loc(1), loc(2)),
            )
        ]
    )
    with pytest.raises(ValueError, match="UNSAFE_PROJECTION"):
        project_ir(ir, strict=True)
    projected = project_document(ir)
    assert [(block.text, block.page, block.bbox) for block in projected.blocks] == [
        ("caption", None, None)
    ]


def test_runtime_noise_is_excluded_but_nested_mutation_cannot_save():
    ir = bind(blocks=[Block("paragraph", "text", 0)], metadata={"semantic": {"x": 1}})
    payload = ir.model_dump(mode="json")
    payload["metadata"]["pdf_worker_pid"] = 99
    assert DocumentIR.model_validate(payload).parse_version == ir.parse_version
    ir.metadata["semantic"]["x"] = 2
    with pytest.raises(ValueError):
        encode_parse_manifest(
            ParseManifest(
                ir.document_id,
                1,
                1,
                ir.source_sha256,
                ParsedDocument("", [], ir=ir),
                ir.parser_fingerprint,
                ir.context_fingerprint,
            )
        )


def test_legacy_identity_is_explicit_readable_and_never_reused():
    import hashlib
    import json

    from cairn.ingestion.parsers import TextParser
    from cairn.ingestion.registry import ParserRegistry

    ir = bind(blocks=[Block("paragraph", "text", 0)])
    payload = ir.model_dump(mode="json")
    payload.pop("identity_version")
    local = [element.model_dump(mode="json", exclude={"element_id"}) for element in ir.elements]
    owner = {
        key: payload[key]
        for key in (
            "workspace_id",
            "kb_id",
            "document_id",
            "revision",
            "source_sha256",
            "parser_fingerprint",
            "context_fingerprint",
        )
    }
    canonical = dict(owner, elements=local, pages=ir.pages, language=ir.language, assets=[])
    payload["parse_version"] = hashlib.sha256(
        json.dumps(
            canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode()
    ).hexdigest()
    legacy = DocumentIR.model_validate(payload)
    assert legacy.identity_version == 1
    registry = ParserRegistry()
    registry.register(TextParser())
    assert not registry.accepts_cached(project_document(legacy), mime="text/plain", source_bytes=4)
    payload["identity_version"] = 2
    with pytest.raises(ValueError):
        DocumentIR.model_validate(payload)
