"""Recent retrieval latency, for load shedding (M09 §4.10, NFR-P-03).

Degrade quality before availability, and always say so: when the process's
recent p95 approaches the SLO, reranking is skipped; well past it, the
candidate pool shrinks too. Per process, because it is the process's own queue
that is filling.
"""

from __future__ import annotations

import math
from collections import deque

__all__ = ["LoadMonitor"]


class LoadMonitor:
    def __init__(self, *, window: int = 512, min_samples: int = 50) -> None:
        self._samples: deque[float] = deque(maxlen=window)
        self._min_samples = min_samples
        self._cached: float | None = None
        self._since_cache = 0

    def record(self, latency_ms: float) -> None:
        if math.isfinite(latency_ms) and latency_ms >= 0:
            self._samples.append(latency_ms)
            self._since_cache += 1

    def p95(self) -> float | None:
        """p95 of the window, or ``None`` until there are enough samples to mean anything.

        Recomputed at most every 16 new samples: exactness matters less than
        keeping the check off the hot path.
        """
        if len(self._samples) < self._min_samples:
            return None
        if self._cached is None or self._since_cache >= 16:
            ordered = sorted(self._samples)
            self._cached = ordered[min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)]
            self._since_cache = 0
        return self._cached
