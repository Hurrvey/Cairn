"""Local helpers for hand annotation: PDF-point page renders, scaffolds and coverage status.

Rules and the annotation format are in ``docs/05-quality/05-pdf-annotation-guide.md``.
This tool never annotates for you; it makes the mechanical parts practical.

``render`` draws a page with a grid every 50 PDF points (labels every 100) in the
page's stored PDF-point space, origin at the bottom-left corner - the space the
guide requires. Read boxes off the grid, never off a viewer's pixel readout.

``scaffold`` copies page templates (sizes and empty lists) into the working file for
the pages you are about to examine, and marks the document ``independent_human``.
Run it only for pages you are about to examine.

``status`` validates the working annotation file with the frozen validator and
prints per-document coverage so progress is visible.

Usage (repository root):

    uv run python scripts/pdf_annotation_helper.py render cjk-06 --pages 1,3-5
    uv run python scripts/pdf_annotation_helper.py render financial_table_heavy-01 --all
    uv run python scripts/pdf_annotation_helper.py render cjk-06 --pages 2 --crop 60 300 560 620
    uv run python scripts/pdf_annotation_helper.py scaffold cjk-06 --pages 1-3
    uv run python scripts/pdf_annotation_helper.py status
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

DEFAULT_MANIFEST = Path("data/acceptance-pdf-rework-01/corpus-final.json")
DEFAULT_CORPUS_ROOT = Path("data/acceptance-pdf-20260921/corpus")
DEFAULT_ANNOTATIONS = Path("data/annotations/annotations.gold.json")
DEFAULT_SKELETON = Path("data/annotations/annotations.skeleton.json")
DEFAULT_TEMPLATES = Path("data/annotations/page-templates.json")


def load_documents(manifest: Path) -> list[dict[str, Any]]:
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    documents = payload["documents"]
    if not isinstance(documents, list) or not documents:
        raise SystemExit(f"{manifest} has no documents list")
    return documents


def locate(item: dict[str, Any], corpus_root: Path) -> Path:
    recorded = item.get("path")
    if isinstance(recorded, str) and Path(recorded).is_file():
        return Path(recorded)
    return corpus_root / str(item["local_pdf"])


def parse_pages(spec: str, total: int) -> list[int]:
    pages: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        start, sep, end = part.partition("-")
        if sep:
            pages.extend(range(int(start), int(end) + 1))
        else:
            pages.append(int(start))
    if not pages or any(page < 1 or page > total for page in pages):
        raise SystemExit(f"pages must be within 1..{total}")
    return sorted(set(pages))


def draw_grid(
    image: Any,
    scale: float,
    left: float,
    bottom: float,
    right: float,
    top: float,
    spacing: float,
) -> None:
    from PIL import ImageDraw, ImageFont

    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=16)
    label_every = spacing * 2

    x = math.ceil(left / spacing) * spacing
    while x <= right:
        px = round((x - left) * scale)
        labelled = abs(x - round(x / label_every) * label_every) < 1e-6
        shade = (150, 150, 150) if labelled else (216, 216, 216)
        draw.line([(px, 0), (px, image.height)], fill=shade)
        if labelled:
            label = str(round(x))
            draw.rectangle([px + 1, 1, px + 10 + 8 * len(label), 20], fill=(255, 255, 255))
            draw.text((px + 4, 3), label, fill=(170, 0, 0), font=font)
        x += spacing

    y = math.ceil(bottom / spacing) * spacing
    while y <= top:
        py = round((top - y) * scale)
        labelled = abs(y - round(y / label_every) * label_every) < 1e-6
        shade = (150, 150, 150) if labelled else (216, 216, 216)
        draw.line([(0, py), (image.width, py)], fill=shade)
        if labelled:
            label = str(round(y))
            draw.rectangle([1, py - 19, 10 + 8 * len(label), py - 1], fill=(255, 255, 255))
            draw.text((4, py - 18), label, fill=(170, 0, 0), font=font)
        y += spacing


def render(args: argparse.Namespace) -> None:
    import pypdfium2 as pdfium  # type: ignore[import-untyped]

    documents = load_documents(args.manifest)
    item = next((entry for entry in documents if entry["id"] == args.doc), None)
    if item is None:
        raise SystemExit(f"no document {args.doc!r} in {args.manifest}")
    pdf = locate(item, args.corpus_root)
    if not pdf.is_file():
        raise SystemExit(f"missing source PDF: {pdf}")
    total = int(item["pages"])
    if args.all:
        pages = list(range(1, total + 1))
    elif args.pages:
        pages = parse_pages(args.pages, total)
    else:
        pages = [1]
    args.output.mkdir(parents=True, exist_ok=True)

    document = pdfium.PdfDocument(str(pdf))
    try:
        for page_number in pages:
            page = document[page_number - 1]
            page.set_rotation(0)  # annotate in the stored, unrotated space
            image = page.render(scale=args.scale).to_pil().convert("RGB")
            width_pt = image.width / args.scale
            height_pt = image.height / args.scale
            suffix = ""
            if args.crop is not None:
                x0, y0, x1, y1 = args.crop
                if not (0 <= x0 < x1 <= width_pt and 0 <= y0 < y1 <= height_pt):
                    raise SystemExit(
                        f"crop {args.crop} outside page {width_pt:.1f} x {height_pt:.1f} pt"
                    )
                box = (
                    max(0, min(image.width, round(x0 * args.scale))),
                    max(0, min(image.height, round((height_pt - y1) * args.scale))),
                    max(0, min(image.width, round(x1 * args.scale))),
                    max(0, min(image.height, round((height_pt - y0) * args.scale))),
                )
                image = image.crop(box)
                left, bottom, right, top = x0, y0, x1, y1
                suffix = f"-crop-{x0:g}-{y0:g}-{x1:g}-{y1:g}"
            else:
                left, bottom, right, top = 0.0, 0.0, width_pt, height_pt
            draw_grid(image, args.scale, left, bottom, right, top, args.spacing)
            target = args.output / f"{args.doc}-p{page_number:03d}{suffix}.png"
            image.save(target)
            print(f"{target}  page {page_number}/{total}  {width_pt:.1f} x {height_pt:.1f} pt")
    finally:
        document.close()


def scaffold(args: argparse.Namespace) -> int:
    documents = load_documents(args.manifest)
    item = next((entry for entry in documents if entry["id"] == args.doc), None)
    if item is None:
        raise SystemExit(f"no document {args.doc!r} in {args.manifest}")
    path = args.annotations
    if not path.is_file():
        raise SystemExit(f"missing {path}; copy the skeleton first (see status)")
    payload = json.loads(path.read_text(encoding="utf-8"))
    templates = json.loads(args.templates.read_text(encoding="utf-8"))
    template = next((entry for entry in templates["documents"] if entry["id"] == args.doc), None)
    if template is None:
        raise SystemExit(f"no templates for {args.doc!r} in {args.templates}")
    total = int(item["pages"])
    pages = parse_pages(args.pages, total) if args.pages else list(range(1, total + 1))
    annotation = next(
        (entry for entry in payload["documents"] if entry["source_sha256"] == item["sha256"]),
        None,
    )
    if annotation is None:
        raise SystemExit(f"{args.doc} has no entry in {path}")
    existing = {page["page"] for page in annotation["pages"]}
    added = 0
    for page in template["pages"]:
        if page["page"] not in pages or page["page"] in existing:
            continue
        annotation["pages"].append(json.loads(json.dumps(page)))
        added += 1
    annotation["pages"].sort(key=lambda page: page["page"])
    annotation["covered_pages"] = sorted(page["page"] for page in annotation["pages"])
    annotation["annotation_version"] = args.version
    annotation["method"] = "independent_human"

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts.pdf_gold_metrics import validate_annotations

    try:
        validate_annotations(payload, documents)
    except ValueError as exc:
        raise SystemExit(f"refusing to write invalid annotations: {exc}") from exc
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(
        f"{args.doc}: +{added} page(s) scaffolded, {len(annotation['pages'])} covered total, "
        f"version {args.version!r}"
    )
    print("fill tables/order/geometry per page, then run status")
    return 0


def status(args: argparse.Namespace) -> int:
    documents = load_documents(args.manifest)
    path = args.annotations
    if not path.is_file():
        print(f"missing {path}")
        print("start it by copying the skeleton:")
        print(f"  copy {args.skeleton} {path}")
        return 2
    payload = json.loads(path.read_text(encoding="utf-8"))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts.pdf_gold_metrics import validate_annotations

    try:
        accepted = validate_annotations(payload, documents)
    except ValueError as exc:
        print(f"INVALID: {exc}")
        return 2

    print(f"annotations: {path}  (valid)")
    header = (
        f"{'document':<34}{'method':<18}{'pages':>9}{'tables':>8}{'cells':>7}"
        f"{'order':>7}{'geom':>6}{'ocr':>6}"
    )
    print(header)
    gold_docs = covered = total_pages = 0
    tables = cells = orders = geoms = ocr_pages = 0
    for item in documents:
        total_pages += int(item["pages"])
        entry = accepted.get(item["sha256"])
        if entry is None:
            print(f"{item['id']:<34}{'-':<18}{'0/' + str(item['pages']):>9}")
            continue
        pages = entry["pages"]
        covered += len(entry["covered_pages"])
        if entry["method"] == "independent_human":
            gold_docs += 1
        page_tables = [table for page in pages for table in (page.get("tables") or [])]
        t = len(page_tables)
        c = sum(len(table.get("cells") or []) for table in page_tables)
        o = sum(len(page.get("order") or []) for page in pages)
        g = sum(len(page.get("geometry") or []) for page in pages)
        oc = sum(1 for page in pages if isinstance(page.get("ocr_text"), str))
        tables += t
        cells += c
        orders += o
        geoms += g
        ocr_pages += oc
        pages_cell = str(len(entry["covered_pages"])) + "/" + str(item["pages"])
        print(
            f"{item['id']:<34}{entry['method']:<18}{pages_cell:>9}{t:>8}{c:>7}{o:>7}{g:>6}{oc:>6}"
        )
    print("-" * 95)
    print(f"independent_human documents: {gold_docs}/{len(documents)}")
    print(f"covered pages: {covered}/{total_pages}")
    print(
        f"tables: {tables}  cells: {cells}  order refs: {orders}  "
        f"geometry refs: {geoms}  pages with ocr_text: {ocr_pages}"
    )
    return 0


def main() -> None:
    cli = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = cli.add_subparsers(dest="command", required=True)

    r = sub.add_parser("render", help="render pages with a PDF-point grid")
    r.add_argument("doc", help="corpus document id, e.g. cjk-06")
    r.add_argument("--pages", default=None, help="page list, e.g. 1,3-5 (default: page 1)")
    r.add_argument("--all", action="store_true", help="render every page of the document")
    r.add_argument("--scale", type=float, default=2.0, help="pixels per PDF point (default 2)")
    r.add_argument("--spacing", type=float, default=50.0, help="grid spacing in points")
    r.add_argument(
        "--crop",
        type=float,
        nargs=4,
        metavar=("X0", "Y0", "X1", "Y1"),
        default=None,
        help="zoom into a PDF-point region",
    )
    r.add_argument("--output", type=Path, default=Path("data/annotations/renders"))
    r.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    r.add_argument("--corpus-root", type=Path, default=DEFAULT_CORPUS_ROOT)
    r.set_defaults(func=render)

    sc = sub.add_parser("scaffold", help="copy page templates into the working file")
    sc.add_argument("doc", help="corpus document id, e.g. cjk-06")
    sc.add_argument("--pages", default=None, help="page list, e.g. 1,3-5 (default: all pages)")
    sc.add_argument("--version", default="r1", help="annotation_version to record")
    sc.add_argument("--annotations", type=Path, default=DEFAULT_ANNOTATIONS)
    sc.add_argument("--templates", type=Path, default=DEFAULT_TEMPLATES)
    sc.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    sc.set_defaults(func=scaffold)

    s = sub.add_parser("status", help="validate the working file and print coverage")
    s.add_argument("--annotations", type=Path, default=DEFAULT_ANNOTATIONS)
    s.add_argument("--skeleton", type=Path, default=DEFAULT_SKELETON)
    s.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    s.set_defaults(func=status)

    args = cli.parse_args()
    result = args.func(args)
    sys.exit(0 if result is None else result)


if __name__ == "__main__":
    main()
