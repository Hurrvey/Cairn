"""Frozen independent-reference protocol: region-iou-greedy/1, threshold 0.5."""

from __future__ import annotations

import math
from collections import Counter
from typing import Any

MATCHING_VERSION = "region-iou-greedy/1"


def iou(a: list[float], b: list[float]) -> float:
    overlap = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - overlap
    return overlap / union if union else 0.0


def match(
    refs: list[dict[str, Any]], predictions: list[dict[str, Any]]
) -> list[tuple[int, int, float]]:
    pairs = [
        (-iou(ref["bbox"], prediction["bbox"]), str(ref["id"]), prediction["order"], r, p)
        for r, ref in enumerate(refs)
        for p, prediction in enumerate(predictions)
        if iou(ref["bbox"], prediction["bbox"]) >= 0.5
    ]
    used_r, used_p = set(), set()
    result = []
    for negative, _id, _order, r, p in sorted(pairs):
        if r not in used_r and p not in used_p:
            used_r.add(r)
            used_p.add(p)
            result.append((r, p, -negative))
    return result


def f1(tp: int, fp: int, fn: int) -> dict[str, Any]:
    denominator = 2 * tp + fp + fn
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "f1": 2 * tp / denominator if denominator else None,
        "precision": tp / (tp + fp) if tp + fp else None,
        "recall": tp / (tp + fn) if tp + fn else None,
    }


def levenshtein(a: str, b: str) -> int:
    a = a.replace("\r\n", "\n")
    b = b.replace("\r\n", "\n")
    previous = list(range(len(b) + 1))
    for i, left in enumerate(a, 1):
        current = [i]
        for j, right in enumerate(b, 1):
            current.append(min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (left != right)))
        previous = current
    return previous[-1]


def validate_annotations(
    payload: dict[str, Any], documents: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    if payload.get("schema_version") != 1:
        raise ValueError("unsupported annotation schema")
    sources = {item["sha256"]: item for item in documents}
    result = {}
    for annotation in payload["documents"]:
        sha = annotation["source_sha256"]
        if (
            sha not in sources
            or sha in result
            or not annotation.get("annotation_version")
            or not annotation.get("method")
        ):
            raise ValueError("annotation source/version mismatch or duplicate")
        covered = annotation["covered_pages"]
        if len(set(covered)) != len(covered) or any(
            type(page) is not int or not 1 <= page <= sources[sha]["pages"] for page in covered
        ):
            raise ValueError("invalid annotation coverage")
        if {page["page"] for page in annotation["pages"]} != set(covered):
            raise ValueError("annotation pages disagree with declared coverage")
        for page in annotation["pages"]:
            if not all(math.isfinite(page[key]) and page[key] > 0 for key in ("width", "height")):
                raise ValueError("invalid annotation page size")
            for modality in ("tables", "order", "geometry"):
                if modality not in page:
                    continue
                references = page[modality]
                if not isinstance(references, list) or len(
                    {ref["id"] for ref in references}
                ) != len(references):
                    raise ValueError("invalid annotation references")
                for reference in references:
                    bbox = reference["bbox"]
                    if (
                        len(bbox) != 4
                        or not all(math.isfinite(x) for x in bbox)
                        or bbox[0] >= bbox[2]
                        or bbox[1] >= bbox[3]
                    ):
                        raise ValueError("invalid reference region")
                    if modality == "tables":
                        for cell in reference["cells"]:
                            if not (
                                0 <= cell["row_start"] < cell["row_end"] <= reference["rows"]
                                and 0 <= cell["col_start"] < cell["col_end"] <= reference["cols"]
                            ):
                                raise ValueError("invalid reference cell")
            if "ocr_text" in page and not isinstance(page["ocr_text"], str):
                raise ValueError("invalid OCR reference")
        result[sha] = annotation
    return result


def predictions_for_page(parsed: Any, page: int) -> list[dict[str, Any]]:
    if parsed is None:
        return []
    elements = parsed.ir.elements if parsed.ir else parsed.elements
    result = []
    if elements:
        for element in elements:
            locations = [loc for loc in element.locations if loc.page == page]
            if not locations:
                continue
            bbox = [
                min(loc.bbox[0] for loc in locations),
                min(loc.bbox[1] for loc in locations),
                max(loc.bbox[2] for loc in locations),
                max(loc.bbox[3] for loc in locations),
            ]
            cells = []
            if element.table:
                multi = len({loc.page for loc in element.locations}) > 1
                cells = [
                    cell.model_dump(mode="json")
                    for cell in element.table.cells
                    if not multi or any(loc.page == page for loc in cell.locations)
                ]
            result.append(
                {"bbox": bbox, "order": element.order, "type": element.type, "cells": cells}
            )
    else:
        for block in parsed.blocks:
            if block.page == page and block.bbox is not None:
                result.append(
                    {
                        "bbox": list(block.bbox),
                        "order": block.ordinal,
                        "type": block.kind,
                        "cells": [],
                    }
                )
    return result


def cell_position(cell: dict[str, Any]) -> tuple[int, int, int, int]:
    return tuple(cell[key] for key in ("row_start", "row_end", "col_start", "col_end"))


def score_document(parsed: Any, annotation: dict[str, Any]) -> dict[str, Any]:
    tp = fp = fn = dtp = dfp = dfn = text_correct = text_total = 0
    edits = reference_chars = insertions = unsupported_ocr = 0
    correct_pairs = total_pairs = gt_regions = matched_regions = 0
    sum_iou = 0.0
    coverage = {"table_pages": 0, "ocr_pages": 0, "order_pages": 0, "geometry_pages": 0}
    for page in annotation["pages"]:
        predictions = predictions_for_page(parsed, page["page"])
        if "region" in page:
            predictions = [
                prediction
                for prediction in predictions
                if iou(prediction["bbox"], page["region"]) > 0
            ]
        if "tables" in page:
            coverage["table_pages"] += 1
            tables = [prediction for prediction in predictions if prediction["type"] == "table"]
            refs = page["tables"]
            pairs = match(refs, tables)
            dtp += len(pairs)
            dfp += len(tables) - len(pairs)
            dfn += len(refs) - len(pairs)
            used_r, used_p = set(), set()
            for r, p, _overlap in pairs:
                used_r.add(r)
                used_p.add(p)
                a = Counter(cell_position(cell) for cell in refs[r]["cells"])
                b = Counter(cell_position(cell) for cell in tables[p]["cells"])
                hits = sum((a & b).values())
                tp += hits
                fp += sum(b.values()) - hits
                fn += sum(a.values()) - hits
                texts = Counter((cell_position(cell), cell["text"]) for cell in tables[p]["cells"])
                expected = Counter((cell_position(cell), cell["text"]) for cell in refs[r]["cells"])
                text_correct += sum((texts & expected).values())
                text_total += sum(expected.values())
            for r, ref in enumerate(refs):
                if r not in used_r:
                    fn += len(ref["cells"])
                    text_total += len(ref["cells"])
            for p, prediction in enumerate(tables):
                if p not in used_p:
                    fp += len(prediction["cells"])
        if "ocr_text" in page:
            coverage["ocr_pages"] += 1
            raw = parsed.metadata.get("ocr_transcripts", {}) if parsed else {}
            key = str(page["page"])
            predicted = raw.get(key, "") if isinstance(raw, dict) else ""
            if parsed is not None and (not isinstance(raw, dict) or key not in raw):
                unsupported_ocr += 1
            reference = page["ocr_text"].replace("\r\n", "\n")
            edits += levenshtein(reference, predicted)
            reference_chars += len(reference)
            if not reference:
                insertions += len(predicted.replace("\r\n", "\n"))
        if "order" in page:
            coverage["order_pages"] += 1
            refs = page["order"]
            mapped = {r: predictions[p]["order"] for r, p, _overlap in match(refs, predictions)}
            for left in range(len(refs)):
                for right in range(left + 1, len(refs)):
                    total_pairs += 1
                    correct_pairs += int(
                        left in mapped and right in mapped and mapped[left] < mapped[right]
                    )
        if "geometry" in page:
            coverage["geometry_pages"] += 1
            refs = page["geometry"]
            pairs = match(refs, predictions)
            gt_regions += len(refs)
            matched_regions += len(pairs)
            sum_iou += sum(value for _r, _p, value in pairs)
    result = {
        "matching_version": MATCHING_VERSION,
        "threshold": 0.5,
        "coverage": coverage,
        "structure": f1(tp, fp, fn) if coverage["table_pages"] else None,
        "detection": f1(dtp, dfp, dfn) if coverage["table_pages"] else None,
        "cell_text": {
            "correct": text_correct,
            "expected": text_total,
            "accuracy": text_correct / text_total if text_total else None,
        }
        if coverage["table_pages"]
        else None,
        "ocr": {
            "edits": edits,
            "reference_chars": reference_chars,
            "cer": edits / reference_chars if reference_chars else None,
            "empty_reference_insertions": insertions,
            "unsupported_pages": unsupported_ocr,
        }
        if coverage["ocr_pages"]
        else None,
        "order": {
            "correct_pairs": correct_pairs,
            "total_pairs": total_pairs,
            "score": correct_pairs / total_pairs if total_pairs else None,
        }
        if coverage["order_pages"]
        else None,
        "geometry": {
            "sum_iou": sum_iou,
            "gt_regions": gt_regions,
            "matched_regions": matched_regions,
            "mean_iou": sum_iou / gt_regions if gt_regions else None,
            "matched_mean_iou": sum_iou / matched_regions if matched_regions else None,
        }
        if coverage["geometry_pages"]
        else None,
    }
    return result


def aggregate(scores: list[dict[str, Any]]) -> dict[str, Any]:
    result = {"documents_scored": len(scores), "micro": {}, "macro": {}}
    for modality, metric in [
        ("structure", "f1"),
        ("detection", "f1"),
        ("cell_text", "accuracy"),
        ("ocr", "cer"),
        ("order", "score"),
        ("geometry", "mean_iou"),
    ]:
        values = [score[modality] for score in scores if score.get(modality) is not None]
        defined = [value[metric] for value in values if value[metric] is not None]
        result["macro"][modality] = {
            "score": sum(defined) / len(defined) if defined else None,
            "annotated_documents": len(values),
            "defined_documents": len(defined),
        }
        counts = {
            key: sum(value[key] for value in values)
            for key in (values[0] if values else {})
            if key not in (metric, "precision", "recall", "matched_mean_iou")
        }
        if modality in ("structure", "detection"):
            micro = f1(counts.get("tp", 0), counts.get("fp", 0), counts.get("fn", 0))
        else:
            numerator, denominator = {
                "cell_text": ("correct", "expected"),
                "ocr": ("edits", "reference_chars"),
                "order": ("correct_pairs", "total_pairs"),
                "geometry": ("sum_iou", "gt_regions"),
            }[modality]
            micro = {
                **counts,
                metric: counts.get(numerator, 0) / counts[denominator]
                if counts.get(denominator)
                else None,
            }
        result["micro"][modality] = micro
    return result
