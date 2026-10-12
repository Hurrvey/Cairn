"""Complete-source evaluation with explicit coverage and independent-gold accounting."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
from collections import Counter
from contextlib import suppress
from pathlib import Path
from typing import Any

from cairn.ingestion.base import ParseContext
from cairn.ingestion.docling_pdf import DoclingPdfParser
from cairn.ingestion.errors import ParseError
from cairn.ingestion.ocr import TesseractOcrEngine
from cairn.ingestion.pdf import PdfParser


def structure_score(
    predicted: list[tuple[int, int, int, int]], expected: list[tuple[int, int, int, int]] | None
) -> dict[str, Any] | None:
    if expected is None:
        return None
    correct = sum((Counter(predicted) & Counter(expected)).values())
    precision = correct / len(predicted) if predicted else 0.0
    recall = correct / len(expected) if expected else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "true_positive": correct,
        "predicted": len(predicted),
        "expected": len(expected),
    }


def inventory(root: Path) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    seen: set[str] = set()
    previous = root / "data/acceptance-pdf-20260921/corpus/manifest.json"
    old = json.loads(previous.read_text(encoding="utf-8"))
    for item in old["documents"]:
        if not item["artifact_scope"].startswith("complete "):
            continue
        documents.append(
            {
                **item,
                "path": str(previous.parent / item["local_pdf"]),
                "category": item["primary_category"],
                "pages": item["page_count"],
            }
        )
        seen.add(item["sha256"])
    directory = root / "data/acceptance-pdf-20261009/full-originals"
    for filename in ("manifest.json", "financial-manifest.json"):
        for item in json.loads((directory / filename).read_text(encoding="utf-8"))["documents"]:
            if item.get("status") != "downloaded" or item["sha256"] in seen:
                continue
            seen.add(item["sha256"])
            documents.append(
                {
                    **item,
                    "path": str(directory / item["path"]),
                    "category": item["id"].rsplit("-", 1)[0],
                }
            )
    return documents


async def evaluate(args: argparse.Namespace) -> None:
    import psutil

    root = Path.cwd()
    output = args.output
    output.mkdir(parents=True, exist_ok=True)
    documents = (
        json.loads(args.corpus_manifest.read_text(encoding="utf-8"))["documents"]
        if args.corpus_manifest
        else inventory(root)
    )
    from scripts.pdf_gold_metrics import aggregate, score_document, validate_annotations

    annotations = (
        validate_annotations(json.loads(args.annotations.read_text(encoding="utf-8")), documents)
        if args.annotations
        else {}
    )
    implementation_sha = hashlib.sha256(
        b"".join(
            path.read_bytes()
            for path in await asyncio.to_thread(
                lambda: sorted(Path("src/cairn/ingestion").glob("*.py"))
            )
        )
    ).hexdigest()
    scores = []
    (output / "complete-inventory.json").write_text(
        json.dumps(
            {
                "documents": documents,
                "count": len(documents),
                "by_category": dict(Counter(item["category"] for item in documents)),
                "independent_table_gold_documents": 0,
                "required_documents": 30,
                "status": "local evaluation only; no redistribution authorization inferred",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    parser = (
        DoclingPdfParser(
            python=args.python,
            assets_path=str(args.assets),
            manifest_path=str(args.manifest),
            ocr=True,
        )
        if args.engine == "docling"
        else PdfParser(ocr=TesseractOcrEngine())
    )
    results = []
    for item in documents:
        source = await asyncio.to_thread(Path(item["path"]).read_bytes)
        if hashlib.sha256(source).hexdigest() != item["sha256"]:
            raise ValueError("source inventory checksum mismatch")
        stopped = asyncio.Event()
        peak = 0

        async def sample(stopped: asyncio.Event = stopped) -> None:
            nonlocal peak
            parent = psutil.Process()
            while not stopped.is_set():
                rss = parent.memory_info().rss
                for child in parent.children(recursive=True):
                    with suppress(psutil.Error):
                        rss += child.memory_info().rss
                peak = max(peak, rss)
                await asyncio.sleep(0.05)

        task = asyncio.create_task(sample())
        record = {
            "id": item["id"],
            "sha256": item["sha256"],
            "pages": item["pages"],
            "category": item["category"],
            "engine": args.engine,
            "structure_f1": None,
            "ocr_cer": None,
            "reading_order": None,
            "gold_status": "missing independent annotations",
        }
        started = time.perf_counter()
        parsed = None
        try:
            parsed = await parser.parse(source, ParseContext(source_url=item["url"]))
            from uuid import UUID

            from cairn.core.document_projection import UnsafeProjection
            from cairn.ingestion.ir import bind_document, project_document

            try:
                parsed = project_document(
                    bind_document(
                        parsed,
                        workspace_id=UUID(int=1),
                        kb_id=UUID(int=2),
                        document_id=UUID(item["sha256"][:32]),
                        revision=1,
                        source_sha256=item["sha256"],
                        parser_fingerprint=None,
                        context_fingerprint=None,
                    )
                )
            except UnsafeProjection:
                from cairn.ingestion.docling_pdf import PdfUnsafeProjection

                raise PdfUnsafeProjection() from None
            record.update(
                status="parsed",
                actual_pages=parsed.pages,
                text_chars=len(parsed.markdown),
                table_count=sum(element.type == "table" for element in parsed.elements)
                if parsed.elements
                else sum(block.kind == "table" for block in parsed.blocks),
            )
            (output / f"{args.engine}-{item['id']}.json").write_text(
                json.dumps(
                    {
                        "elements": [
                            element.model_dump(mode="json") for element in parsed.elements
                        ],
                        "blocks": parsed.layout(),
                        "metadata": parsed.metadata,
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except ParseError as exc:
            record.update(status="failed", code=exc.code)
            parsed = None
        finally:
            stopped.set()
            await task
        record.update(seconds=time.perf_counter() - started, peak_parent_tree_rss_bytes=peak)
        annotation = annotations.get(item["sha256"])
        if annotation:
            scoring = score_document(parsed, annotation)
            scores.append(scoring)
            record.update(
                metrics=scoring,
                gold_status=annotation["method"],
                annotation_version=annotation["annotation_version"],
                structure_f1=scoring["structure"]["f1"] if scoring["structure"] else None,
                ocr_cer=scoring["ocr"]["cer"] if scoring["ocr"] else None,
                reading_order=scoring["order"]["score"] if scoring["order"] else None,
            )
        record["implementation_sha256"] = implementation_sha
        results.append(record)
        (output / f"{args.engine}-quality.json").write_text(
            json.dumps(
                {
                    "results": results,
                    "engine": args.engine,
                    "quality_gate_passed": False,
                    "performance_gate_passed": False,
                    "complete_source_count": len(documents),
                    "source_count_measured": len(results),
                    "annotation_coverage": {
                        "annotated_documents": len(scores),
                        "measured_documents": len(results),
                        "declared_gate_documents": 30,
                        "independent_human_documents": sum(
                            annotation.get("method") == "independent_human"
                            for annotation in annotations.values()
                        ),
                    },
                    "metrics": aggregate(scores),
                    "cold_warm": "new worker per document",
                    "required": {
                        "structure_f1": 0.85,
                        "100_page_p95_seconds": 30,
                        "100_page_docs_per_hour_per_core": 120,
                        "ocr_seconds_per_page": 3,
                        "steady_rss_bytes": 2_000_000_000,
                    },
                    "missing": [
                        *(
                            ["30 complete documents with six per category"]
                            if len(documents) != 30
                            or any(
                                v != 6
                                for v in Counter(item["category"] for item in documents).values()
                            )
                            else []
                        ),
                        "independent gold",
                        "100-page latency distribution and long scan throughput",
                    ],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(json.dumps(record), flush=True)


if __name__ == "__main__":
    cli = argparse.ArgumentParser()
    cli.add_argument("--engine", choices=["pdfium", "docling"], required=True)
    cli.add_argument("--output", type=Path, required=True)
    cli.add_argument("--python", default="python")
    cli.add_argument("--assets", type=Path)
    cli.add_argument("--manifest", type=Path)
    cli.add_argument("--corpus-manifest", type=Path)
    cli.add_argument("--annotations", type=Path)
    asyncio.run(evaluate(cli.parse_args()))
