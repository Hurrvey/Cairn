"""Reproducible PDF robustness run; never substitutes parsing for quality annotations."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
from collections import Counter
from pathlib import Path

from cairn.ingestion.base import ParseContext
from cairn.ingestion.errors import ParseError
from cairn.ingestion.ocr import TesseractOcrEngine
from cairn.ingestion.pdf import PdfParser


async def evaluate(manifest_path: Path, output: Path) -> None:
    root = (await asyncio.to_thread(manifest_path.resolve)).parent
    manifest = json.loads(await asyncio.to_thread(manifest_path.read_text, encoding="utf-8"))
    await asyncio.to_thread(output.mkdir, parents=True, exist_ok=True)
    parser = PdfParser(ocr=TesseractOcrEngine())
    results: list[dict[str, object]] = []
    for document in manifest["documents"]:
        identifier = document["id"]
        source = (root / document["local_pdf"]).resolve()
        if not source.is_relative_to(root):
            raise ValueError("Corpus source must be inside its manifest directory")
        data = source.read_bytes()
        if hashlib.sha256(data).hexdigest() != document["sha256"]:
            raise ValueError(f"Corpus integrity mismatch: {identifier}")
        record: dict[str, object] = {
            "id": identifier,
            "category": document["primary_category"],
            "sha256": document["sha256"],
            "expected_pages": document["page_count"],
            "artifact_scope": document["artifact_scope"],
        }
        started = time.perf_counter()
        try:
            parsed = await parser.parse(data, ParseContext(source_url=document["url"]))
            record.update(
                status="parsed",
                pages=parsed.pages,
                page_count_matches=parsed.pages == document["page_count"],
                blocks=len(parsed.blocks),
                characters=len(parsed.markdown),
                table_blocks=sum(block.kind == "table" for block in parsed.blocks),
                ocr_pages=parsed.metadata.get("ocr_pages", []),
                all_blocks_have_citations=all(
                    block.page is not None
                    and 1 <= block.page <= parsed.pages
                    and block.bbox is not None
                    and block.source_url == document["url"]
                    for block in parsed.blocks
                ),
            )
            (output / f"{identifier}.json").write_text(
                json.dumps(
                    {
                        "markdown": parsed.markdown,
                        "metadata": parsed.metadata,
                        "blocks": parsed.layout(),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except ParseError as exc:
            record.update(status="failed", code=exc.code)
        record["seconds"] = round(time.perf_counter() - started, 3)
        results.append(record)
        print(json.dumps(record, ensure_ascii=False), flush=True)
        summary = {
            "corpus_summary": manifest["summary"],
            "counts": dict(Counter(str(item["status"]) for item in results)),
            "results": results,
            "quality_gate_passed": False,
            "table_structure_f1": None,
            "reading_order_accuracy": None,
            "human_transcription_ocr_accuracy": None,
            "limitations": [
                "23 items are real source-page excerpts, not 23 complete original documents.",
                "Human layout-region labels do not supply table cell structure or reading order.",
                "Parsing and nonempty citations are robustness checks, not gold quality scores.",
                "Peak memory and 100-page throughput are not measured by this runner.",
            ],
        }
        (output / "report.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("manifest", type=Path)
    cli.add_argument("--output", type=Path, required=True)
    args = cli.parse_args()
    asyncio.run(evaluate(args.manifest, args.output))
