"""Explain mode (FR-H-08): what each retrieval stage did to the ranking.

A disabled recorder only accumulates stage timings for usage metrics.
Output is bounded: ``top`` lists hold at most ten entries and
``rank_changes`` at most twenty per stage.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from cairn.core.ids import encode_id
from cairn.vectorstore.base import Hit

__all__ = ["PREVIEW_CHARACTERS", "TOP_ENTRIES", "ExplainRecorder"]

TOP_ENTRIES = 10
RANK_CHANGES = 20
PREVIEW_CHARACTERS = 120
STAGES = (
    "embed",
    "dense",
    "sparse",
    "fuse",
    "filter",
    "rerank",
    "expand",
    "dedupe",
    "mmr",
    "budget",
)


def _preview(text: str) -> str:
    compact = " ".join(text.split())
    return (
        compact if len(compact) <= PREVIEW_CHARACTERS else compact[: PREVIEW_CHARACTERS - 1] + "…"
    )


class ExplainRecorder:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self.plan: dict[str, Any] = {}
        self.stages: list[dict[str, Any]] = []
        self.latency: dict[str, int] = dict.fromkeys(STAGES, 0)

    def stage(self, name: str, latency_ms: float, **fields: Any) -> None:
        self.latency[name] = self.latency.get(name, 0) + round(latency_ms)
        if self.enabled:
            self.stages.append(
                {"stage": name, "latency_ms": round(latency_ms), "applied": True, **fields}
            )

    def top_hits(
        self, hits: Sequence[Hit], *, terms: set[str] | None = None
    ) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        entries: list[dict[str, Any]] = []
        for rank, hit in enumerate(hits[:TOP_ENTRIES], start=1):
            entry: dict[str, Any] = {
                "chunk_id": encode_id("chk", hit.id),
                "score": hit.score,
                "rank": rank,
                "preview": _preview(hit.content),
            }
            if terms is not None:
                from cairn.embedding.sparse import tokenize

                entry["matched_terms"] = sorted(terms & set(tokenize(hit.content)))
            entries.append(entry)
        return entries

    @staticmethod
    def rank_changes(
        before: Sequence[str], after: Sequence[str], **extra: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Entries whose position moved, best final rank first."""
        previous = {chunk_id: rank for rank, chunk_id in enumerate(before, start=1)}
        changes: list[dict[str, Any]] = []
        for rank, chunk_id in enumerate(after, start=1):
            prior = previous.get(chunk_id)
            if prior == rank:
                continue
            change: dict[str, Any] = {"chunk_id": chunk_id, "before": prior, "after": rank}
            for key, values in extra.items():
                if chunk_id in values:
                    change[key] = values[chunk_id]
            changes.append(change)
            if len(changes) >= RANK_CHANGES:
                break
        return changes

    def output(self) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        stages = [
            item
            for name in STAGES
            for item in (
                [entry for entry in self.stages if entry["stage"] == name]
                or [{"stage": name, "latency_ms": 0, "applied": False}]
            )
        ]
        return {"plan": self.plan, "stages": stages}
