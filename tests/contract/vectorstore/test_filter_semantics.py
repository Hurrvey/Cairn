"""Filter semantics — TC-M05-04 … TC-M05-08.

These pin down the normative behaviour declared in
``cairn.vectorstore.filters``. They run against the reference implementation and
need no infrastructure; the driver conformance suite then asserts every backend
agrees with the same corpus.

The edge cases here are the ones that diverge in practice. A driver that gets
``$ne`` on a missing field wrong will pass every happy-path test and silently
return the wrong document set forever.
"""

from __future__ import annotations

import pytest

from cairn.core.errors import ValidationFailed
from cairn.vectorstore.filters import (
    MISSING,
    And,
    Compare,
    Exists,
    Not,
    Or,
    fields_referenced,
    matches,
    parse_filter,
    resolve_path,
)

DOC = {
    "document_id": "doc_a",
    "content": "Key rotation is performed via the admin CLI.",
    "page": 14,
    "score": 0.72,
    "published": True,
    "meta": {"lang": "en", "tags": ["security", "ops"], "version": 3, "note": None},
}


# --- path resolution ---------------------------------------------------------


def test_resolve_path__reaches_nested_values() -> None:
    assert resolve_path(DOC, "meta.lang") == "en"
    assert resolve_path(DOC, "page") == 14


def test_resolve_path__missing_is_distinct_from_none() -> None:
    assert resolve_path(DOC, "meta.absent") is MISSING
    assert resolve_path(DOC, "meta.note") is None


def test_resolve_path__traversing_a_scalar_yields_missing_not_an_error() -> None:
    assert resolve_path(DOC, "page.nested") is MISSING


# --- $eq / $ne ---------------------------------------------------------------


def test_eq__scalar_equality() -> None:
    assert matches(Compare("meta.lang", "$eq", "en"), DOC) is True
    assert matches(Compare("meta.lang", "$eq", "fr"), DOC) is False


def test_eq__on_an_array_field_means_containment() -> None:
    """TC-M05-05: a user filtering `tags` expects membership, not list equality."""
    assert matches(Compare("meta.tags", "$eq", "security"), DOC) is True
    assert matches(Compare("meta.tags", "$eq", "legal"), DOC) is False


def test_eq__on_a_missing_field_does_not_match() -> None:
    assert matches(Compare("meta.absent", "$eq", "x"), DOC) is False


def test_ne__matches_when_the_field_is_absent() -> None:
    """TC-M05-06 — the case that matters most.

    `{"deprecated": {"$ne": true}}` reads as "not deprecated". A document that
    never set the flag is not deprecated, so requiring the field to exist would
    make the common query wrong.
    """
    assert matches(Compare("deprecated", "$ne", True), DOC) is True
    assert matches(Compare("meta.lang", "$ne", "fr"), DOC) is True
    assert matches(Compare("meta.lang", "$ne", "en"), DOC) is False


def test_ne__on_an_array_field_is_the_negation_of_containment() -> None:
    assert matches(Compare("meta.tags", "$ne", "security"), DOC) is False
    assert matches(Compare("meta.tags", "$ne", "legal"), DOC) is True


# --- $in / $nin --------------------------------------------------------------


def test_in__on_an_array_means_non_empty_intersection() -> None:
    """TC-M05-05."""
    assert matches(Compare("meta.tags", "$in", ["legal", "ops"]), DOC) is True
    assert matches(Compare("meta.tags", "$in", ["legal", "finance"]), DOC) is False


def test_in__on_a_scalar_means_membership() -> None:
    assert matches(Compare("meta.lang", "$in", ["en", "de"]), DOC) is True
    assert matches(Compare("meta.lang", "$in", ["fr", "de"]), DOC) is False


def test_in__on_a_missing_field_does_not_match() -> None:
    assert matches(Compare("meta.absent", "$in", ["x"]), DOC) is False


def test_nin__matches_when_absent() -> None:
    assert matches(Compare("meta.absent", "$nin", ["x"]), DOC) is True
    assert matches(Compare("meta.tags", "$nin", ["legal"]), DOC) is True
    assert matches(Compare("meta.tags", "$nin", ["ops"]), DOC) is False


def test_in__with_an_empty_list_matches_nothing() -> None:
    assert matches(Compare("meta.tags", "$in", []), DOC) is False


# --- ordered comparison ------------------------------------------------------


@pytest.mark.parametrize(
    ("op", "value", "expected"),
    [
        ("$gt", 10, True),
        ("$gt", 14, False),
        ("$gte", 14, True),
        ("$lt", 20, True),
        ("$lte", 14, True),
        ("$lt", 14, False),
    ],
)
def test_ordered__numeric(op: str, value: int, expected: bool) -> None:
    assert matches(Compare("page", op, value), DOC) is expected  # type: ignore[arg-type]


def test_ordered__type_mismatch_is_no_match_not_an_error() -> None:
    """TC-M05-07. One badly-typed document must not fail an entire query — a
    filter on `meta.version > 3` should skip a document that stored a string
    there, not 500 the request."""
    assert matches(Compare("meta.lang", "$gt", 3), DOC) is False
    assert matches(Compare("page", "$gt", "abc"), DOC) is False


def test_ordered__on_a_missing_field_does_not_match() -> None:
    assert matches(Compare("meta.absent", "$gte", 1), DOC) is False


def test_ordered__booleans_are_not_numbers() -> None:
    """`True > 0` is true in Python. Treating booleans as numbers here would
    make `published > 0` quietly meaningful, which no user intends."""
    assert matches(Compare("published", "$gt", 0), DOC) is False


def test_ordered__strings_compare_lexically() -> None:
    assert matches(Compare("meta.lang", "$gt", "de"), DOC) is True
    assert matches(Compare("meta.lang", "$lt", "de"), DOC) is False


# --- $exists -----------------------------------------------------------------


def test_exists__distinguishes_absent_from_null() -> None:
    """TC-M05-07: `meta.note` is present and null."""
    assert matches(Exists("meta.note", present=True), DOC) is True
    assert matches(Exists("meta.absent", present=True), DOC) is False
    assert matches(Exists("meta.absent", present=False), DOC) is True
    assert matches(Exists("meta.note", present=False), DOC) is False


# --- boolean composition -----------------------------------------------------


def test_and_or_not_compose() -> None:
    """TC-M05-08."""
    node = And(
        (
            Compare("meta.lang", "$eq", "en"),
            Or(
                (
                    Compare("meta.tags", "$in", ["legal"]),
                    Compare("page", "$gte", 10),
                )
            ),
            Not(Compare("meta.version", "$eq", 99)),
        )
    )
    assert matches(node, DOC) is True


def test_empty_and_matches_everything_empty_or_matches_nothing() -> None:
    assert matches(And(()), DOC) is True
    assert matches(Or(()), DOC) is False


def test_none_filter_matches_everything() -> None:
    assert matches(None, DOC) is True


# --- parsing -----------------------------------------------------------------


def test_parse__bare_value_is_shorthand_for_eq() -> None:
    assert parse_filter({"meta.lang": "en"}) == Compare("meta.lang", "$eq", "en")


def test_parse__multiple_operators_on_one_field_become_and() -> None:
    node = parse_filter({"page": {"$gte": 3, "$lt": 9}})
    assert isinstance(node, And)
    assert len(node.clauses) == 2


def test_parse__multiple_fields_become_and() -> None:
    node = parse_filter({"meta.lang": "en", "page": 14})
    assert isinstance(node, And)
    assert matches(node, DOC) is True


def test_parse__or_and_not() -> None:
    node = parse_filter({"$or": [{"meta.lang": "fr"}, {"page": 14}]})
    assert matches(node, DOC) is True

    node = parse_filter({"$not": {"meta.lang": "en"}})
    assert matches(node, DOC) is False


def test_parse__exists() -> None:
    assert parse_filter({"meta.note": {"$exists": True}}) == Exists("meta.note", present=True)


def test_parse__rejects_an_unknown_operator() -> None:
    """A typo must fail loudly. Silently ignoring `$regexp` would return an
    unfiltered result set that looks plausible."""
    with pytest.raises(ValidationFailed, match="Unknown filter operator"):
        parse_filter({"meta.lang": {"$regexp": ".*"}})


def test_parse__rejects_non_list_operands_for_in() -> None:
    with pytest.raises(ValidationFailed, match=r"\$in takes a list"):
        parse_filter({"meta.tags": {"$in": "security"}})


def test_parse__empty_is_none() -> None:
    assert parse_filter(None) is None
    assert parse_filter({}) is None


def test_fields_referenced__walks_the_whole_tree() -> None:
    node = parse_filter(
        {"meta.lang": "en", "$or": [{"page": {"$gt": 1}}, {"meta.tags": {"$in": ["a"]}}]}
    )
    assert fields_referenced(node) == {"meta.lang", "page", "meta.tags"}
