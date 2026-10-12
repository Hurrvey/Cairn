import json
from pathlib import Path
from uuid import uuid4

import pytest

from cairn.ingestion.base import ParseContext
from cairn.ingestion.docling_pdf import DoclingPdfParser, normalize_docling
from cairn.ingestion.ir import bind_document, project_document
from cairn.ingestion.pdf import PdfEngineUnavailable


async def test_disabled_ocr_does_not_constrain_native_context_language(tmp_path, monkeypatch):
    from cairn.ingestion.ir import digest

    manifest = {"docling": "2.135.0", "files": [], "packages": {}}
    manifest["identity"] = digest(manifest)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    parser = DoclingPdfParser(
        assets_path=str(tmp_path), manifest_path=str(path), ocr=False, languages=("eng",)
    )
    calls = []

    async def worker(command, data, **kwargs):
        config = json.loads(command[-1])
        calls.append(config)
        payload = {
            "identity": manifest["identity"],
            "pages": {"1": {"size": {"width": 100, "height": 100}}},
            "items": [
                {
                    "self_ref": "#/texts/0",
                    "label": "text",
                    "text": "bonjour",
                    "prov": [{"page_no": 1, "bbox": {"l": 0, "b": 0, "r": 80, "t": 20}}],
                }
            ],
            "pdf_budget_facts": {
                "schema": 1,
                "output_bytes": 500,
                "pages": [
                    {
                        "width": 100,
                        "height": 100,
                        "characters": 7,
                        "words": 1,
                        "objects": 1,
                        "rasters": 0,
                        "layout_blocks": 1,
                        "render_pixels": 0,
                        "render_bytes": 0,
                    }
                ],
            },
        }
        return 0, json.dumps(payload).encode(), b""

    monkeypatch.setattr("cairn.ingestion.docling_pdf.run_bounded", worker)
    parsed = await parser.parse(b"%PDF-fixture", ParseContext(language="fr"))
    assert parsed.language == "fr" and calls[0]["ocr_languages"] == []


def test_formula_native_is_preserved_without_fabricated_latex():
    parsed = normalize_docling(
        {
            "pages": {"1": {"size": {"width": 100, "height": 100}}},
            "items": [
                {
                    "self_ref": "#/texts/0",
                    "label": "formula",
                    "text": "",
                    "orig": "x=y",
                    "prov": [
                        {
                            "page_no": 1,
                            "bbox": {
                                "l": 1,
                                "b": 1,
                                "r": 50,
                                "t": 20,
                                "coord_origin": "BOTTOMLEFT",
                            },
                        }
                    ],
                }
            ],
        }
    )
    assert parsed.elements[0].formula.native == "x=y"
    assert parsed.elements[0].formula.status == "unrecognized"


def test_same_page_multiple_regions_preserve_one_text_without_guessing_separator_spans():
    parsed = normalize_docling(
        {
            "pages": {"1": {"size": {"width": 100, "height": 100}}},
            "items": [
                {
                    "self_ref": "#/texts/0",
                    "label": "text",
                    "text": "first second",
                    "orig": "first second",
                    "prov": [
                        {
                            "page_no": 1,
                            "charspan": span,
                            "bbox": {
                                "l": 1,
                                "b": y,
                                "r": 50,
                                "t": y + 10,
                                "coord_origin": "BOTTOMLEFT",
                            },
                        }
                        for span, y in [([0, 5], 10), ([6, 12], 30)]
                    ],
                }
            ],
        }
    )
    ir = bind_document(
        parsed,
        workspace_id=uuid4(),
        kb_id=uuid4(),
        document_id=uuid4(),
        revision=1,
        source_sha256="a" * 64,
        parser_fingerprint="b" * 64,
        context_fingerprint="c" * 64,
    )
    projection = project_document(ir)
    assert projection.markdown == "first second" and len(projection.blocks) == 1
    assert len(ir.elements[0].locations) == 2


def test_multipage_without_exact_spans_degrades_to_unplaced_text():
    parsed = normalize_docling(
        {
            "pages": {
                "1": {"size": {"width": 100, "height": 100}},
                "2": {"size": {"width": 100, "height": 100}},
            },
            "items": [
                {
                    "self_ref": "#/texts/0",
                    "label": "text",
                    "text": "abcdef",
                    "prov": [
                        {
                            "page_no": page,
                            "bbox": {
                                "l": 1,
                                "b": 1,
                                "r": 50,
                                "t": 20,
                                "coord_origin": "BOTTOMLEFT",
                            },
                        }
                        for page in (1, 2)
                    ],
                }
            ],
        }
    )
    ir = bind_document(
        parsed,
        workspace_id=uuid4(),
        kb_id=uuid4(),
        document_id=uuid4(),
        revision=1,
        source_sha256="a" * 64,
        parser_fingerprint="b" * 64,
        context_fingerprint="c" * 64,
    )
    projection = project_document(ir)
    # Text is kept exactly once; the page is not guessed from either location.
    assert projection.markdown == "abcdef"
    assert [(block.page, block.bbox) for block in projection.blocks] == [(None, None)]
    assert len(ir.elements[0].locations) == 2


def test_overlapping_cell_text_is_deduplicated_but_unique_cells_untouched():
    repeated = "$ 9,761 \r\n$ 9,761 $ 9,761 \r\n$ 9,761"
    distinct = " Net income (loss) from continuing operations "
    parsed = normalize_docling(
        {
            "pages": {"1": {"size": {"width": 100, "height": 100}}},
            "items": [
                {
                    "self_ref": "#/tables/0",
                    "label": "table",
                    "text": "",
                    "prov": [
                        {
                            "page_no": 1,
                            "bbox": {
                                "l": 1,
                                "b": 1,
                                "r": 90,
                                "t": 40,
                                "coord_origin": "BOTTOMLEFT",
                            },
                        }
                    ],
                    "data": {
                        "num_rows": 1,
                        "num_cols": 2,
                        "table_cells": [
                            {
                                "text": distinct,
                                "start_row_offset_idx": 0,
                                "end_row_offset_idx": 1,
                                "start_col_offset_idx": 0,
                                "end_col_offset_idx": 1,
                            },
                            {
                                "text": repeated,
                                "start_row_offset_idx": 0,
                                "end_row_offset_idx": 1,
                                "start_col_offset_idx": 1,
                                "end_col_offset_idx": 2,
                            },
                        ],
                    },
                }
            ],
        }
    )
    cells = parsed.elements[0].table.cells
    assert cells[0].text == distinct
    assert cells[1].text == "$ 9,761"


def test_selected_engine_requires_verified_manifest(tmp_path):
    with pytest.raises(PdfEngineUnavailable):
        DoclingPdfParser(assets_path=str(tmp_path), manifest_path=str(tmp_path / "missing.json"))
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"docling": "2.135.0", "identity": "forged"}))
    with pytest.raises(PdfEngineUnavailable):
        DoclingPdfParser(assets_path=str(tmp_path), manifest_path=str(path))


def test_real_recorded_native_table_normalizes_without_grid_correction():
    path = Path("data/acceptance-pdf-stage-20261009/offline-native/financial_table_heavy-05.json")
    if not path.exists():
        pytest.skip("Local real inference evidence is optional for portable unit runs")
    parsed = normalize_docling(json.loads(path.read_text()))
    ir = bind_document(
        parsed,
        workspace_id=uuid4(),
        kb_id=uuid4(),
        document_id=uuid4(),
        revision=1,
        source_sha256="a" * 64,
        parser_fingerprint="b" * 64,
        context_fingerprint="c" * 64,
    )
    assert sum(item.table is not None for item in ir.elements) == 1
    assert project_document(ir).markdown


async def test_effective_ocr_languages_psm_and_asset_identity_are_worker_owned(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from cairn.ingestion.ir import digest

    env = {
        "languages": ["eng", "chi_sim"],
        "traineddata": {"eng": "a" * 64, "chi_sim": "b" * 64},
        "binary_sha256": "c" * 64,
        "version": "Tesseract test",
    }
    manifest = {"docling": "2.135.0", "files": [], "packages": {}, "ocr_environment": env}
    manifest["identity"] = digest(manifest)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    monkeypatch.setattr(
        "cairn.ingestion.docling_pdf.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(stdout=json.dumps(env).encode()),
    )
    parser = DoclingPdfParser(
        assets_path=str(tmp_path),
        manifest_path=str(path),
        ocr=True,
        languages=("eng", "chi_sim"),
        psm=4,
    )

    class Captured(Exception):
        pass

    async def worker(command, *args, **kwargs):
        config = json.loads(command[-1])
        assert config["ocr_languages"] == ["chi_sim", "eng"]
        assert config["ocr_psm"] == 4
        raise Captured()

    monkeypatch.setattr("cairn.ingestion.docling_pdf.run_bounded", worker)
    with pytest.raises(Captured):
        await parser.parse(b"%PDF-fixture", ParseContext(language="zh"))
    old_identity = parser.identity
    env["traineddata"]["chi_sim"] = "d" * 64
    manifest.pop("identity")
    manifest["identity"] = digest(manifest)
    path.write_text(json.dumps(manifest))
    changed = DoclingPdfParser(
        assets_path=str(tmp_path),
        manifest_path=str(path),
        ocr=True,
        languages=("eng", "chi_sim"),
        psm=4,
    )
    assert changed.identity != old_identity
    env["traineddata"]["chi_sim"] = "e" * 64
    with pytest.raises(PdfEngineUnavailable):
        DoclingPdfParser(
            assets_path=str(tmp_path), manifest_path=str(path), ocr=True, languages=("chi_sim",)
        )
