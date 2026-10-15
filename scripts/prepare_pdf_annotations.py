"""Prepare a human-annotation kit for the PDF corpus: an empty gold skeleton plus page templates.

The skeleton is a valid ``schema_version: 1`` annotation file that covers nothing, so the
evaluator accepts it without counting any document as gold. The page templates carry
every page's size in PDF points, so an annotator only has to add references and list
the pages they covered. Rules and the reference format are in
``docs/05-quality/05-pdf-annotation-guide.md``.

Usage (repository root):

    uv run python scripts/prepare_pdf_annotations.py \
        --corpus-manifest data/acceptance-pdf-rework-01/corpus-final.json \
        --corpus-root data/acceptance-pdf-20260921/corpus \
        --output data/annotations
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def page_sizes(pdf: Path) -> list[dict[str, Any]]:
    import pypdfium2 as pdfium  # type: ignore[import-untyped]

    document = pdfium.PdfDocument(str(pdf))
    sizes: list[dict[str, Any]] = []
    try:
        for index in range(len(document)):
            page = document[index]
            left, bottom, right, top = page.get_bbox()
            sizes.append(
                {
                    "page": index + 1,
                    "width": right - left,
                    "height": top - bottom,
                    "rotation": page.get_rotation(),
                }
            )
            page.close()
    finally:
        document.close()
    return sizes


def locate(item: dict[str, Any], corpus_root: Path) -> Path:
    recorded = item.get("path")
    if isinstance(recorded, str) and Path(recorded).is_file():
        return Path(recorded)
    return corpus_root / str(item["local_pdf"])


def main() -> None:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--corpus-manifest", type=Path, required=True)
    cli.add_argument(
        "--corpus-root", type=Path, required=True, help="directory local_pdf paths are relative to"
    )
    cli.add_argument("--output", type=Path, required=True, help="directory for the kit")
    args = cli.parse_args()

    manifest = json.loads(args.corpus_manifest.read_text(encoding="utf-8"))
    documents: list[dict[str, Any]] = manifest["documents"]
    skeleton: dict[str, Any] = {"schema_version": 1, "documents": []}
    templates: dict[str, Any] = {"schema_version": 1, "documents": []}
    for item in documents:
        pdf = locate(item, args.corpus_root)
        sizes = page_sizes(pdf)
        if len(sizes) != item["pages"]:
            raise SystemExit(
                f"page count mismatch for {item['id']}: {len(sizes)} != {item['pages']}"
            )
        skeleton["documents"].append(
            {
                "source_sha256": item["sha256"],
                "annotation_version": "draft-0",
                "method": "pending",
                "covered_pages": [],
                "pages": [],
            }
        )
        templates["documents"].append(
            {
                "id": item["id"],
                "category": item.get("primary_category") or item.get("category"),
                "source_sha256": item["sha256"],
                "local_pdf": item.get("local_pdf"),
                # The manifest points some documents at a corpus-relative local_pdf and
                # others at an absolute path; this is where the PDF was actually found.
                "resolved_path": str(pdf),
                "pages": [
                    {
                        "page": size["page"],
                        "width": size["width"],
                        "height": size["height"],
                        "rotation": size["rotation"],
                        "tables": [],
                        "order": [],
                        "geometry": [],
                    }
                    for size in sizes
                ],
            }
        )

    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "annotations.skeleton.json").write_text(
        json.dumps(skeleton, indent=2), encoding="utf-8"
    )
    (args.output / "page-templates.json").write_text(
        json.dumps(templates, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # Prove the skeleton loads under the frozen protocol before anyone starts annotating.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts.pdf_gold_metrics import validate_annotations

    validate_annotations(skeleton, documents)
    pages = sum(len(entry["pages"]) for entry in templates["documents"])
    print(f"{len(documents)} documents, {pages} page templates -> {args.output}")


if __name__ == "__main__":
    main()
