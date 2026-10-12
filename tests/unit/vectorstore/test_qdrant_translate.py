from __future__ import annotations

import pytest
from qdrant_client import models

from cairn.vectorstore.errors import UnsupportedFilter
from cairn.vectorstore.filters import FilterNode, parse_filter
from cairn.vectorstore.qdrant import PATHS, translate


def compile_filter(spec: dict[str, object]) -> models.Filter:
    node: FilterNode | None = parse_filter(spec)
    assert node is not None
    return translate(node)


def test_numbers_compare_by_range_so_int_matches_float() -> None:
    compiled = compile_filter({"meta.version": 3})
    assert isinstance(compiled.must, list)
    condition = compiled.must[0]
    assert isinstance(condition, models.FieldCondition)
    assert condition.range == models.Range(gte=3, lte=3)


def test_negative_operators_use_must_not() -> None:
    compiled = compile_filter({"meta.lang": {"$ne": "en"}})
    assert compiled.must_not and not compiled.must


def test_exists_reads_the_hidden_paths_field() -> None:
    compiled = compile_filter({"meta.tags": {"$exists": True}})
    assert isinstance(compiled.must, list)
    condition = compiled.must[0]
    assert isinstance(condition, models.FieldCondition)
    assert condition.key == PATHS
    assert condition.match == models.MatchValue(value="meta.tags")


@pytest.mark.parametrize(
    "spec",
    [
        {"meta.lang": {"$gt": "a"}},
        {"meta.flag": {"$lt": True}},
        {"meta.obj": {"$eq": {"nested": 1}}},
        {"meta.tags": {"$in": [["a"]]}},
    ],
)
def test_unrepresentable_filters_are_refused(spec: dict[str, object]) -> None:
    with pytest.raises(UnsupportedFilter):
        compile_filter(spec)
