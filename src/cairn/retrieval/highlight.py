"""Query-term highlight spans in returned content (``options.include_highlights``).

Terms come from the same tokenizer as the BM25 query encoder, so what is
highlighted is what full-text search matched on. Matching runs on a case-folded
copy of the content with a per-character map back to the original, so spans are
offsets into the ``content`` string the caller receives, counted in Unicode
code points (Python string indices), not UTF-16 units.
"""

from __future__ import annotations

import unicodedata

from cairn.embedding.sparse import tokenize

__all__ = ["MAX_HIGHLIGHTS", "highlight_spans", "query_terms"]

MAX_HIGHLIGHTS = 64
_CJK_RANGES = ((0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF))


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return any(low <= code <= high for low, high in _CJK_RANGES)


def query_terms(query: str) -> list[str]:
    """Distinct, highlightable terms of ``query``, longest first.

    Single ASCII letters/digits are dropped (they would light up every
    paragraph), and a single CJK character is dropped when a longer term
    already contains it.
    """
    terms = sorted(set(tokenize(query)), key=lambda term: (-len(term), term))
    kept: list[str] = []
    for term in terms:
        if len(term) == 1 and not _is_cjk(term):
            continue
        if len(term) == 1 and any(term in longer for longer in kept):
            continue
        kept.append(term)
    return kept


def _fold(content: str) -> tuple[str, list[int]]:
    """Case-folded content plus, per folded character, its original index."""
    folded: list[str] = []
    origin: list[int] = []
    for index, char in enumerate(content):
        for piece in unicodedata.normalize("NFKC", char).lower():
            folded.append(piece)
            origin.append(index)
    return "".join(folded), origin


def _word_edge(text: str, start: int, end: int) -> bool:
    before = text[start - 1] if start > 0 else ""
    after = text[end] if end < len(text) else ""
    return not (before.isalnum() and not _is_cjk(before)) and not (
        after.isalnum() and not _is_cjk(after)
    )


def highlight_spans(
    terms: list[str], content: str, *, limit: int = MAX_HIGHLIGHTS
) -> list[dict[str, int]]:
    """Merged ``{start, end}`` spans (end exclusive) where ``terms`` occur."""
    if not terms or not content:
        return []
    folded, origin = _fold(content)
    raw: list[tuple[int, int]] = []
    for term in terms:
        ascii_term = not any(_is_cjk(char) for char in term)
        position = folded.find(term)
        while position != -1:
            end = position + len(term)
            if not ascii_term or _word_edge(folded, position, end):
                raw.append((origin[position], origin[end - 1] + 1))
            position = folded.find(term, position + 1)
    if not raw:
        return []
    raw.sort()
    merged: list[list[int]] = [list(raw[0])]
    for start, end in raw[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [{"start": start, "end": end} for start, end in merged[:limit]]
