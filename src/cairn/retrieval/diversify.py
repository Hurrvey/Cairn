"""Maximal marginal relevance over the final candidates (FR-H-12).

Similarity is the cosine of the candidates' BM25 term vectors, computed here
from their text. That works the same for vector, full-text and hybrid hits, and
across federated knowledge bases whose dense vectors come from different models
and are therefore not comparable. It targets the redundancy users see: passages
that say the same thing in the same words.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from cairn.embedding.sparse import encode_document

__all__ = ["MMR_TEXT_CHARACTERS", "mmr_order", "mmr_pool_size"]

#: Only the beginning of each candidate is compared; parents can be very long.
MMR_TEXT_CHARACTERS = 4000


def mmr_pool_size(top_k: int) -> int:
    return max(3 * top_k, 50)


def _unit(text: str) -> dict[int, float]:
    vector = encode_document(text[:MMR_TEXT_CHARACTERS])
    if vector is None:
        return {}
    norm = math.sqrt(sum(value * value for value in vector.values))
    return {index: value / norm for index, value in zip(vector.indices, vector.values, strict=True)}


def _cosine(left: dict[int, float], right: dict[int, float]) -> float:
    if len(left) > len(right):
        left, right = right, left
    return sum(value * right.get(index, 0.0) for index, value in left.items())


def mmr_order(
    relevance: Sequence[float], texts: Sequence[str], *, lambda_: float, k: int
) -> list[tuple[int, float]]:
    """Greedy MMR: indices of the ``k`` selected candidates with their objective.

    ``relevance`` is min-max normalised first so that ``lambda_`` means the same
    for every score scale (RRF, cosine, rerank logits). Ties keep input order.
    """
    count = len(relevance)
    if count == 0 or k <= 0:
        return []
    low, high = min(relevance), max(relevance)
    span = high - low
    rel = [(score - low) / span if span > 0 else 1.0 for score in relevance]
    vectors = [_unit(text) for text in texts]
    closest = [0.0] * count
    remaining = list(range(count))
    selected: list[tuple[int, float]] = []
    while remaining and len(selected) < k:
        best_index = remaining[0]
        best_value = -math.inf
        for index in remaining:
            value = lambda_ * rel[index] - (1 - lambda_) * closest[index]
            if value > best_value:
                best_index, best_value = index, value
        selected.append((best_index, best_value))
        remaining.remove(best_index)
        chosen = vectors[best_index]
        for index in remaining:
            closest[index] = max(closest[index], _cosine(vectors[index], chosen))
    return selected
