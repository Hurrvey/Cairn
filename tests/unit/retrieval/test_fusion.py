from __future__ import annotations

import math
from uuid import UUID

import pytest

from cairn.retrieval.fusion import rrf_fuse, weighted_fuse
from cairn.vectorstore.base import Hit

KB_ID = UUID("10000000-0000-0000-0000-000000000001")
A_ID = UUID("20000000-0000-0000-0000-000000000001")
B_ID = UUID("20000000-0000-0000-0000-000000000002")


def _hit(chunk_id: UUID, score: float) -> Hit:
    return Hit(id=chunk_id, score=score)


def test_weighted_fusion_normalizes_each_stage_and_applies_stage_and_target_weights() -> None:
    fused = weighted_fuse(
        KB_ID,
        [
            ("dense", [_hit(A_ID, 10.0), _hit(B_ID, 0.0)], 0.5),
            ("sparse", [_hit(B_ID, 100.0), _hit(A_ID, 50.0)], 0.5),
        ],
        weights={"dense": 0.75, "sparse": 0.25},
    )

    assert [item.hit.id for item in fused] == [A_ID, B_ID]
    assert [item.score for item in fused] == pytest.approx([0.375, 0.125])
    assert fused[0].scores == {
        "dense": 10.0,
        "dense_normalized": 1.0,
        "sparse": 50.0,
        "sparse_normalized": 0.0,
        "fused": 0.375,
    }


def test_weighted_fusion_equal_scores_are_tied_and_uuid_order_is_deterministic() -> None:
    fused = weighted_fuse(
        KB_ID,
        [("dense", [_hit(B_ID, -3.0), _hit(A_ID, -3.0)], 1.0)],
        weights={"dense": 1.0, "sparse": 0.0},
    )

    assert [item.hit.id for item in fused] == [A_ID, B_ID]
    assert [item.score for item in fused] == [1.0, 1.0]


def test_weighted_fusion_rejects_nonfinite_scores() -> None:
    with pytest.raises(ValueError, match="finite"):
        weighted_fuse(
            KB_ID,
            [("dense", [_hit(A_ID, math.nan)], 1.0)],
            weights={"dense": 1.0, "sparse": 0.0},
        )


def test_weighted_fusion_normalizes_extreme_finite_scores_without_overflow() -> None:
    fused = weighted_fuse(
        KB_ID,
        [("dense", [_hit(A_ID, 1e308), _hit(B_ID, -1e308)], 1.0)],
        weights={"dense": 1.0, "sparse": 0.0},
    )

    assert [item.score for item in fused] == [1.0, 0.0]


def test_rrf_formula_remains_rank_and_target_weight_only() -> None:
    fused = rrf_fuse(
        KB_ID,
        [
            ("dense", [_hit(A_ID, 0.01)], 0.5),
            ("sparse", [_hit(A_ID, 99.0)], 0.5),
        ],
        k=60,
    )

    assert fused[0].score == pytest.approx(1 / 61)
    assert fused[0].scores["dense"] == 0.01
    assert fused[0].scores["sparse"] == 99.0
