import pytest
from scripts.pdf_gold_metrics import score_document, validate_annotations

from cairn.core.document_ir import Element, Location, Table, TableCell
from cairn.ingestion.base import ParsedDocument


def test_duplicate_detection_wrong_text_and_order_use_full_denominators():
    def location(bbox):
        return Location(page=1, width=100, height=100, bbox=bbox)

    cell = TableCell(text="wrong", row_start=0, row_end=1, col_start=0, col_end=1)
    elements = [
        Element(
            local_id=str(i),
            order=i,
            type="table",
            table=Table(rows=1, columns=1, cells=[cell]),
            locations=[location((0, 0, 40, 40))],
        )
        for i in (0, 1)
    ]
    elements.extend(
        [
            Element(
                local_id="b", order=2, type="text", text="b", locations=[location((30, 50, 50, 70))]
            ),
            Element(
                local_id="a", order=3, type="text", text="a", locations=[location((0, 50, 20, 70))]
            ),
        ]
    )
    result = score_document(
        ParsedDocument("", [], elements=elements, metadata={"ocr_transcripts": {"1": "ac"}}), gold()
    )
    assert result["structure"]["tp"] == 1 and result["structure"]["fp"] == 1
    assert result["structure"]["f1"] == pytest.approx(2 / 3)
    assert result["detection"]["fp"] == 1 and result["cell_text"]["accuracy"] == 0
    assert result["ocr"]["cer"] == 0.5 and result["order"]["score"] == 0
    assert result["geometry"]["mean_iou"] == 1


def gold():
    return {
        "source_sha256": "a" * 64,
        "annotation_version": "human-1",
        "method": "synthetic",
        "covered_pages": [1],
        "pages": [
            {
                "page": 1,
                "width": 100,
                "height": 100,
                "tables": [
                    {
                        "id": "t",
                        "bbox": [0, 0, 40, 40],
                        "rows": 1,
                        "cols": 1,
                        "cells": [
                            {
                                "row_start": 0,
                                "row_end": 1,
                                "col_start": 0,
                                "col_end": 1,
                                "text": "0",
                            }
                        ],
                    }
                ],
                "ocr_text": "ab",
                "order": [
                    {"id": "a", "bbox": [0, 50, 20, 70]},
                    {"id": "b", "bbox": [30, 50, 50, 70]},
                ],
                "geometry": [{"id": "a", "bbox": [0, 50, 20, 70]}],
            }
        ],
    }


def test_failed_annotated_document_is_penalized_not_removed():
    result = score_document(None, gold())
    assert result["structure"]["fn"] == 1 and result["structure"]["f1"] == 0
    assert result["ocr"]["edits"] == 2 and result["ocr"]["cer"] == 1
    assert result["order"]["total_pairs"] == 1 and result["order"]["score"] == 0
    assert result["geometry"]["mean_iou"] == 0 and result["geometry"]["gt_regions"] == 1


def test_annotation_empty_is_different_from_unannotated():
    ref = gold()
    ref["pages"][0] = {"page": 1, "width": 100, "height": 100, "tables": []}
    result = score_document(None, ref)
    assert result["structure"]["f1"] is None
    assert result["coverage"]["table_pages"] == 1 and result["coverage"]["ocr_pages"] == 0


def test_source_version_mismatch_is_rejected():
    with pytest.raises(ValueError):
        validate_annotations(
            {"schema_version": 1, "documents": [gold()]}, [{"sha256": "b" * 64, "pages": 1}]
        )


def test_wrong_span_empty_ocr_and_explicit_empty_page_false_positive():
    ref = gold()
    ref["pages"][0]["ocr_text"] = ""
    element = Element(
        local_id="t",
        order=0,
        type="table",
        table=Table(
            rows=2,
            columns=1,
            cells=[TableCell(text="0", row_start=0, row_end=2, col_start=0, col_end=1)],
        ),
        locations=[Location(page=1, width=100, height=100, bbox=(0, 0, 40, 40))],
    )
    parsed = ParsedDocument(
        "", [], elements=[element], metadata={"ocr_transcripts": {"1": "x\r\ny"}}
    )
    result = score_document(parsed, ref)
    assert result["structure"]["tp"] == 0
    assert result["structure"]["fp"] == result["structure"]["fn"] == 1
    assert result["ocr"]["cer"] is None and result["ocr"]["empty_reference_insertions"] == 3
    ref["pages"][0]["tables"] = []
    result = score_document(parsed, ref)
    assert result["detection"]["fp"] == 1 and result["structure"]["fp"] == 1
