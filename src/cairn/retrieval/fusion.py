"""Deterministic reciprocal-rank and normalized weighted fusion."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from uuid import UUID

from cairn.vectorstore.base import Hit


@dataclass(slots=True)
class FusedHit:
    kb_id: UUID
    hit: Hit
    score: float
    scores: dict[str, float] = field(default_factory=dict)


def rrf_fuse(
    kb_id: UUID,
    ranked: Sequence[tuple[str, Sequence[Hit], float]],
    *,
    k: int,
) -> list[FusedHit]:
    fused: dict[UUID, FusedHit] = {}
    for stage, hits, weight in ranked:
        for rank, hit in enumerate(hits, start=1):
            item = fused.setdefault(hit.id, FusedHit(kb_id=kb_id, hit=hit, score=0.0))
            item.score += weight / (k + rank)
            item.scores[stage] = hit.score
    for item in fused.values():
        item.scores["fused"] = item.score
    return sorted(fused.values(), key=lambda item: (-item.score, item.hit.id.int))


def weighted_fuse(
    kb_id: UUID,
    ranked: Sequence[tuple[str, Sequence[Hit], float]],
    *,
    weights: Mapping[str, float],
) -> list[FusedHit]:
    """Fuse independently normalized stages, preserving each raw stage score."""
    if any(not math.isfinite(weight) or weight < 0 for weight in weights.values()):
        raise ValueError("fusion weights must be finite and non-negative")
    fused: dict[UUID, FusedHit] = {}
    for stage, hits, target_weight in ranked:
        if not math.isfinite(target_weight) or target_weight < 0:
            raise ValueError("target weights must be finite and non-negative")
        if not hits:
            continue
        raw_scores = [hit.score for hit in hits]
        if not all(math.isfinite(score) for score in raw_scores):
            raise ValueError("stage scores must be finite")
        low = min(raw_scores)
        high = max(raw_scores)
        scale = max(abs(low), abs(high), 1.0)
        scaled_low = low / scale
        scaled_span = high / scale - scaled_low
        stage_weight = weights.get(stage, 0.0)
        for hit in hits:
            normalized = 1.0 if high == low else (hit.score / scale - scaled_low) / scaled_span
            item = fused.setdefault(hit.id, FusedHit(kb_id=kb_id, hit=hit, score=0.0))
            item.score += normalized * stage_weight * target_weight
            if not math.isfinite(item.score):
                raise ValueError("weighted fusion produced a non-finite score")
            item.scores[stage] = hit.score
            item.scores[f"{stage}_normalized"] = normalized
    for item in fused.values():
        item.scores["fused"] = item.score
    return sorted(fused.values(), key=lambda item: (-item.score, item.hit.id.int))
