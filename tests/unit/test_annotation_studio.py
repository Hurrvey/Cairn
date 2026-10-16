"""Unit tests for the annotation studio server helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from scripts.annotation_studio import Studio, merge_document, summarize

SHA_A = "a" * 64
SHA_B = "b" * 64


def make_entry(**overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "source_sha256": SHA_A,
        "annotation_version": "draft-0",
        "method": "pending",
        "covered_pages": [],
        "pages": [],
    }
    entry.update(overrides)
    return entry


def make_page(page: int = 1) -> dict[str, Any]:
    return {
        "page": page,
        "width": 595.0,
        "height": 842.0,
        "rotation": 0,
        "tables": [],
        "order": [],
        "geometry": [],
    }


def test_summarize_none() -> None:
    summary = summarize(None)
    assert summary["covered"] == 0
    assert summary["method"] == "pending"
    assert summary["tables"] == 0


def test_summarize_counts() -> None:
    page = make_page()
    page["tables"].append(
        {
            "id": "t1",
            "bbox": [10.0, 10.0, 100.0, 50.0],
            "rows": 1,
            "cols": 2,
            "cells": [
                {"row_start": 0, "row_end": 1, "col_start": 0, "col_end": 1, "text": "a"},
                {"row_start": 0, "row_end": 1, "col_start": 1, "col_end": 2, "text": "b"},
            ],
        }
    )
    page["order"].append({"id": "o1", "bbox": [10.0, 10.0, 100.0, 50.0]})
    page["geometry"].append({"id": "g1", "bbox": [10.0, 10.0, 100.0, 50.0]})
    page["ocr_text"] = "hello"
    entry = make_entry(covered_pages=[1], pages=[page], method="independent_human")
    assert summarize(entry) == {
        "covered": 1,
        "tables": 1,
        "cells": 2,
        "order": 1,
        "geometry": 1,
        "ocr": 1,
        "method": "independent_human",
    }


def test_merge_document_replaces_matching_sha() -> None:
    payload = {"documents": [make_entry(), make_entry(source_sha256=SHA_B)]}
    replacement = make_entry(covered_pages=[1])
    merge_document(payload, {"sha256": SHA_A}, replacement)
    assert payload["documents"][0] is replacement
    assert payload["documents"][1]["source_sha256"] == SHA_B


def test_merge_document_appends_when_absent() -> None:
    payload: dict[str, Any] = {"documents": []}
    replacement = make_entry()
    merge_document(payload, {"sha256": SHA_A}, replacement)
    assert payload["documents"] == [replacement]


def test_merge_document_rejects_mismatched_sha() -> None:
    payload: dict[str, Any] = {"documents": []}
    with pytest.raises(ValueError):
        merge_document(payload, {"sha256": SHA_B}, make_entry())


@pytest.fixture()
def studio(tmp_path: Path) -> Studio:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"documents": [{"id": "doc-01", "sha256": SHA_A, "pages": 2}]}),
        encoding="utf-8",
    )
    templates = tmp_path / "templates.json"
    templates.write_text(
        json.dumps(
            {
                "documents": [
                    {
                        "id": "doc-01",
                        "pages": [
                            {"page": 1, "width": 595.0, "height": 842.0, "rotation": 0},
                            {"page": 2, "width": 595.0, "height": 842.0, "rotation": 0},
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    annotations = tmp_path / "annotations.json"
    annotations.write_text(
        json.dumps({"schema_version": 1, "documents": [make_entry()]}),
        encoding="utf-8",
    )
    return Studio(
        manifest=manifest,
        corpus_root=tmp_path,
        annotations=annotations,
        templates=templates,
        cache_dir=tmp_path / "cache",
    )


def test_state_reports_documents_and_totals(studio: Studio) -> None:
    state = studio.state()
    assert state["totals"]["documents"] == 1
    assert state["totals"]["pages"] == 2
    assert state["totals"]["covered"] == 0
    assert state["documents"][0]["id"] == "doc-01"
    assert state["documents"][0]["method"] == "pending"


def test_save_validates_and_writes(studio: Studio) -> None:
    before = studio.annotations.read_bytes()
    page = make_page()
    page["tables"].append(
        {
            "id": "t1",
            "bbox": [10.0, 10.0, 100.0, 50.0],
            "rows": 1,
            "cols": 1,
            "cells": [{"row_start": 0, "row_end": 1, "col_start": 0, "col_end": 1, "text": "x"}],
        }
    )
    annotation = make_entry(
        covered_pages=[1], pages=[page], method="independent_human", annotation_version="r1"
    )
    state = studio.save("doc-01", annotation)
    assert state["totals"]["covered"] == 1
    written = json.loads(studio.annotations.read_text(encoding="utf-8"))
    assert written["documents"][0]["method"] == "independent_human"
    backup = studio.annotations.with_name(studio.annotations.name + ".bak")
    assert backup.read_bytes() == before


def test_save_rejects_invalid_and_leaves_file(studio: Studio) -> None:
    before = studio.annotations.read_bytes()
    page = make_page()
    page["tables"].append(
        {
            "id": "t1",
            "bbox": [10.0, 10.0, 100.0, 50.0],
            "rows": 1,
            "cols": 1,
            "cells": [{"row_start": 0, "row_end": 2, "col_start": 0, "col_end": 1, "text": "x"}],
        }
    )
    annotation = make_entry(
        covered_pages=[1], pages=[page], method="independent_human", annotation_version="r1"
    )
    with pytest.raises(ValueError):
        studio.save("doc-01", annotation)
    assert studio.annotations.read_bytes() == before


def test_save_rejects_unknown_document(studio: Studio) -> None:
    with pytest.raises(ValueError):
        studio.save("nope", make_entry())


def test_page_png_bounds(studio: Studio) -> None:
    with pytest.raises(ValueError):
        studio.page_png("doc-01", 3, 2)
    with pytest.raises(ValueError):
        studio.page_png("doc-01", 1, 5)


def test_page_png_renders_and_caches(studio: Studio, tmp_path: Path) -> None:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument.new()
    pdf.new_page(200, 300)
    source = tmp_path / "source.pdf"
    pdf.save(str(source))
    pdf.close()
    studio.by_id["doc-01"]["path"] = str(source)
    data = studio.page_png("doc-01", 1, 2)
    assert data[1:4] == b"PNG"
    assert studio.page_png("doc-01", 1, 2) == data
    cached = list((tmp_path / "cache").glob("*.png"))
    assert len(cached) == 1
