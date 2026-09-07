"""Unit tests for core identifiers — TC-M00-01."""

from __future__ import annotations

import uuid

import pytest

from cairn.core.ids import InvalidIdError, decode_id, encode_id, new_public_id, new_uuid


def test_new_uuid__is_version_7_and_variant_rfc4122() -> None:
    """UUIDv7 gives time-ordered primary keys, which keeps B-tree inserts local."""
    value = new_uuid()
    assert value.version == 7
    assert (value.int >> 62) & 0b11 == 0b10


def test_new_uuid__is_monotonic_across_calls() -> None:
    values = [new_uuid() for _ in range(50)]
    timestamps = [v.int >> 80 for v in values]
    assert timestamps == sorted(timestamps)


def test_encode_decode__round_trips() -> None:
    original = new_uuid()
    encoded = encode_id("kb", original)
    assert encoded.startswith("kb_")
    assert decode_id("kb", encoded) == original


def test_encode__rejects_unregistered_prefix() -> None:
    with pytest.raises(InvalidIdError, match="unregistered prefix"):
        encode_id("nope", new_uuid())


def test_decode_id__rejects_a_different_resources_prefix() -> None:
    """TC-M00-01: prefix validation is a security control, not decoration.

    Without it, a caller can pass a document id where a knowledge base id is
    expected and the type confusion is invisible — both are opaque strings of
    identical shape.
    """
    document_id = encode_id("doc", new_uuid())
    with pytest.raises(InvalidIdError, match="expected an identifier beginning 'kb_'"):
        decode_id("kb", document_id)


@pytest.mark.parametrize(
    "value",
    [
        "kb_",
        "kb_TOOSHORT",
        "kb_" + "A" * 27,
        "kb_01HQZX3N9K2M5P7R8T9V0W1XY!",  # invalid character
        "01HQZX3N9K2M5P7R8T9V0W1XYZ",  # no prefix
        "",
    ],
)
def test_decode_id__rejects_malformed(value: str) -> None:
    with pytest.raises(InvalidIdError):
        decode_id("kb", value)


def test_decode_id__is_case_insensitive_in_the_body() -> None:
    original = new_uuid()
    encoded = encode_id("kb", original)
    assert decode_id("kb", encoded.lower()) == original


def test_new_public_id__has_the_expected_shape() -> None:
    value = new_public_id("req")
    assert value.startswith("req_")
    assert len(value) == len("req_") + 26


def test_encoding__excludes_ambiguous_glyphs() -> None:
    """Crockford base32 drops I, L, O, U so a human can transcribe an id."""
    body = encode_id("kb", uuid.UUID(int=(1 << 128) - 1)).removeprefix("kb_")
    assert not (set(body) & set("ILOU"))
