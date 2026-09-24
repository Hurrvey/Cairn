"""BM25 sparse vectors for lexical retrieval on Qdrant.

Qdrant keeps these in each namespace's ``sparse`` slot with the ``idf``
modifier, so the index supplies inverse document frequency at query time and
this module only produces term-frequency weights. Everything here is
deterministic — the same text always yields the same vector — which is what
lets an index written by one worker be queried by another process.

Chinese has no word boundaries, so CJK runs go through jieba's search mode,
which emits words *and* their sub-words (``知识库`` -> ``知识``, ``知识库``).
Other letters and digits are split on everything else, and code-like tokens
such as ``XR-2200`` are indexed whole and by their parts: an exact product code
is precisely the query that embeddings are weakest at.

``ENCODER_ID`` names this contract. Anything that changes which indices or
weights a text produces is a new ID, and an index built under the old one must
be rebuilt.
"""

from __future__ import annotations

import logging
import re
import threading
import unicodedata
from collections import Counter
from hashlib import blake2b

from cairn.vectorstore.base import SparseVector

__all__ = ["ENCODER_ID", "encode_document", "encode_query", "tokenize", "warm_up"]

ENCODER_ID = "bm25-jieba-v1"

_K1 = 1.2
_B = 0.75
#: Fixed rather than measured: a corpus-wide average would make a chunk's
#: vector depend on every other chunk, and so change under a rebuild.
_AVGDL = 256.0

_CJK = "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
_LETTER = rf"(?:(?![{_CJK}])[^\W_])"
_RUN = re.compile(rf"([{_CJK}]+)|({_LETTER}+(?:[-_./]{_LETTER}+)*)")
_PART = re.compile(rf"{_LETTER}+")

_lock = threading.Lock()
_ready = False


def warm_up() -> None:
    """Load jieba's dictionary (~1 s) now rather than inside the first request."""
    global _ready
    if _ready:
        return
    with _lock:
        if not _ready:
            import jieba

            jieba.setLogLevel(logging.WARNING)
            jieba.initialize()
            _ready = True


def _segment(run: str) -> list[str]:
    warm_up()
    import jieba

    return [token for token in jieba.cut_for_search(run) if token.strip()]


def tokenize(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", text).lower()
    tokens: list[str] = []
    for match in _RUN.finditer(normalized):
        cjk, word = match.groups()
        if cjk:
            tokens.extend(_segment(cjk))
            continue
        tokens.append(word)
        parts = _PART.findall(word)
        if len(parts) > 1:
            tokens.extend(parts)
    return tokens


def _index(token: str) -> int:
    return int.from_bytes(blake2b(token.encode(), digest_size=4).digest(), "big")


def _vector(weights: dict[int, float]) -> SparseVector | None:
    if not weights:
        return None
    ordered = sorted(weights.items())
    return SparseVector(
        indices=tuple(index for index, _ in ordered),
        values=tuple(value for _, value in ordered),
    )


def encode_document(text: str) -> SparseVector | None:
    tokens = tokenize(text)
    if not tokens:
        return None
    norm = _K1 * (1 - _B + _B * len(tokens) / _AVGDL)
    weights: dict[int, float] = {}
    for token, tf in Counter(tokens).items():
        index = _index(token)
        weights[index] = weights.get(index, 0.0) + tf * (_K1 + 1) / (tf + norm)
    return _vector(weights)


def encode_query(text: str) -> SparseVector | None:
    return _vector({_index(token): 1.0 for token in set(tokenize(text))})
