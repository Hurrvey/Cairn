"""Driver-neutral filter expressions, with normative semantics.

Every driver translates this AST into its own query language. If two drivers
disagree about what an operator means, a user gets different results depending
on a storage backend they chose for operational reasons — and no test they write
will catch it, because each backend is individually self-consistent.

So the semantics are declared here, once, and :func:`matches` is the reference
implementation the conformance suite checks every driver against.

NORMATIVE SEMANTICS
===================

Type handling
    Comparisons are attempted only between compatible types (number/number,
    string/string, datetime/datetime). A mismatch is **no match**, never an
    error: a filter on ``meta.version > 3`` must not fail a whole query because
    one document stored a string there.

Missing fields
    A missing field is not the same as ``null``. ``$exists`` distinguishes them;
    every other operator treats a missing field as not matching, *except* the
    negative operators.

Negative operators (``$ne``, ``$nin``)
    Match when the field is **absent**. ``{"deprecated": {"$ne": true}}`` is
    read as "not deprecated", and a document that never set the flag is not
    deprecated. The alternative — requiring the field to exist — makes the
    common case wrong.

Array fields
    A stored list is treated as a set of values:

    * ``$eq`` with a scalar   -> containment  (``value in stored``)
    * ``$in`` with a list     -> non-empty intersection
    * ``$ne`` / ``$nin``      -> the negation of the above

    This is what a user filtering ``tags`` expects, and it matches the mental
    model most people bring from document databases.

Field paths
    Dotted, resolved left to right: ``meta.lang``. A path that traverses a
    non-mapping yields "absent" rather than an error.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from cairn.core.errors import ValidationFailed

__all__ = [
    "MISSING",
    "OPERATORS",
    "And",
    "Compare",
    "Exists",
    "FilterNode",
    "Not",
    "Or",
    "matches",
    "parse_filter",
    "resolve_path",
]

Operator = Literal["$eq", "$ne", "$in", "$nin", "$gt", "$gte", "$lt", "$lte"]

OPERATORS: frozenset[str] = frozenset(
    {"$eq", "$ne", "$in", "$nin", "$gt", "$gte", "$lt", "$lte", "$exists"}
)

_NEGATIVE: frozenset[str] = frozenset({"$ne", "$nin"})
_ORDERED: frozenset[str] = frozenset({"$gt", "$gte", "$lt", "$lte"})


class _Missing:
    """Sentinel for an absent field. Distinct from ``None``, which is a value."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return "<missing>"

    def __bool__(self) -> bool:
        return False


MISSING = _Missing()


@dataclass(frozen=True, slots=True)
class Compare:
    field: str
    op: Operator
    value: Any


@dataclass(frozen=True, slots=True)
class Exists:
    field: str
    present: bool = True


@dataclass(frozen=True, slots=True)
class And:
    clauses: tuple[FilterNode, ...]


@dataclass(frozen=True, slots=True)
class Or:
    clauses: tuple[FilterNode, ...]


@dataclass(frozen=True, slots=True)
class Not:
    clause: FilterNode


FilterNode = Compare | Exists | And | Or | Not


# --- evaluation --------------------------------------------------------------


def resolve_path(payload: Mapping[str, Any], path: str) -> Any:
    """Walk a dotted path. Returns :data:`MISSING` if any segment is absent."""
    current: Any = payload
    for segment in path.split("."):
        if not isinstance(current, Mapping) or segment not in current:
            return MISSING
        current = current[segment]
    return current


def _comparable(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool)
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return True
    if isinstance(left, str) and isinstance(right, str):
        return True
    return isinstance(left, datetime) and isinstance(right, datetime)


def _as_set(value: Any) -> tuple[Any, ...]:
    """A stored list is a set of values; a scalar is a one-element set."""
    if isinstance(value, (list, tuple, set)):
        return tuple(value)
    return (value,)


def _eq(stored: Any, wanted: Any) -> bool:
    if stored is MISSING:
        return False
    if isinstance(stored, (list, tuple, set)):
        return wanted in _as_set(stored)
    return bool(stored == wanted)


def _in(stored: Any, wanted: Sequence[Any]) -> bool:
    if stored is MISSING:
        return False
    candidates = set(_as_set(stored))
    return any(item in candidates for item in wanted)


def _ordered(stored: Any, wanted: Any, op: str) -> bool:
    if stored is MISSING or not _comparable(stored, wanted):
        # A type mismatch is not a match and not an error — one badly-typed
        # document must not fail the whole query.
        return False
    if op == "$gt":
        return bool(stored > wanted)
    if op == "$gte":
        return bool(stored >= wanted)
    if op == "$lt":
        return bool(stored < wanted)
    return bool(stored <= wanted)


def matches(node: FilterNode | None, payload: Mapping[str, Any]) -> bool:
    """Reference implementation of the normative semantics.

    Used by ``FakeVectorStore`` and by the conformance suite as the oracle every
    driver must agree with.
    """
    if node is None:
        return True

    if isinstance(node, And):
        return all(matches(clause, payload) for clause in node.clauses)
    if isinstance(node, Or):
        return any(matches(clause, payload) for clause in node.clauses)
    if isinstance(node, Not):
        return not matches(node.clause, payload)
    if isinstance(node, Exists):
        present = resolve_path(payload, node.field) is not MISSING
        return present is node.present

    stored = resolve_path(payload, node.field)

    if node.op == "$eq":
        return _eq(stored, node.value)
    if node.op == "$ne":
        # Absent counts as "not equal" — see the module docstring.
        return not _eq(stored, node.value)
    if node.op == "$in":
        return _in(stored, node.value)
    if node.op == "$nin":
        return not _in(stored, node.value)
    return _ordered(stored, node.value, node.op)


# --- parsing -----------------------------------------------------------------


def parse_filter(spec: Mapping[str, Any] | None) -> FilterNode | None:
    """Build an AST from the wire format used by the retrieval API.

    ::

        {
          "lang": "en",                        -> Compare(lang, $eq, "en")
          "tags": {"$in": ["a", "b"]},         -> Compare(tags, $in, [...])
          "version": {"$gte": 3, "$lt": 9},    -> And(...)
          "$or": [ {...}, {...} ]
        }
    """
    if not spec:
        return None

    clauses: list[FilterNode] = []
    for key, value in spec.items():
        if key == "$and":
            clauses.append(And(tuple(_parse_each(value))))
        elif key == "$or":
            clauses.append(Or(tuple(_parse_each(value))))
        elif key == "$not":
            inner = parse_filter(value)
            if inner is not None:
                clauses.append(Not(inner))
        elif key.startswith("$"):
            raise ValidationFailed(f"Unknown filter operator at top level: {key}")
        else:
            clauses.extend(_parse_field(key, value))

    if not clauses:
        return None
    return clauses[0] if len(clauses) == 1 else And(tuple(clauses))


def _parse_each(value: Any) -> list[FilterNode]:
    if not isinstance(value, (list, tuple)):
        raise ValidationFailed("$and and $or take a list of filter objects.")
    parsed = [parse_filter(item) for item in value]
    return [node for node in parsed if node is not None]


def _parse_field(field: str, value: Any) -> list[FilterNode]:
    # A bare value is shorthand for $eq — the overwhelmingly common case.
    if not isinstance(value, Mapping):
        return [Compare(field, "$eq", value)]

    nodes: list[FilterNode] = []
    for op, operand in value.items():
        if op not in OPERATORS:
            raise ValidationFailed(
                f"Unknown filter operator {op!r} on field {field!r}. "
                f"Supported: {', '.join(sorted(OPERATORS))}."
            )
        if op == "$exists":
            if not isinstance(operand, bool):
                raise ValidationFailed("$exists takes a boolean.")
            nodes.append(Exists(field, present=operand))
            continue
        if op in ("$in", "$nin") and not isinstance(operand, (list, tuple)):
            raise ValidationFailed(f"{op} takes a list.")
        nodes.append(Compare(field, op, operand))
    return nodes


def fields_referenced(node: FilterNode | None) -> set[str]:
    """Every payload field a filter touches. Drivers use this to check that the
    required payload indexes exist before running an expensive scan."""
    if node is None:
        return set()
    if isinstance(node, (Compare, Exists)):
        return {node.field}
    if isinstance(node, Not):
        return fields_referenced(node.clause)
    referenced: set[str] = set()
    for clause in node.clauses:
        referenced |= fields_referenced(clause)
    return referenced
