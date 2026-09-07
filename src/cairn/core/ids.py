"""Identifiers.

Internally every entity is a UUIDv7 (time-ordered, so B-tree inserts stay local).
Externally every entity is a prefixed, Crockford-base32 string: ``kb_01HQZX...``.

The prefix is a *security control*, not decoration: ``decode_id("kb", value)``
rejects a ``doc_`` identifier, which prevents type confusion where an attacker
substitutes one resource's ID for another's.
"""

from __future__ import annotations

import secrets
from typing import Final
from uuid import UUID

from cairn.core.time import utcnow_ms

__all__ = [
    "PREFIXES",
    "InvalidIdError",
    "decode_id",
    "encode_id",
    "new_public_id",
    "new_uuid",
]

# Crockford base32: no I, L, O, U — removes the ambiguous glyphs.
_ALPHABET: Final = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_DECODE: Final = {c: i for i, c in enumerate(_ALPHABET)}
_ENCODED_LEN: Final = 26  # ceil(128 / 5)

#: Normative prefix registry. See docs/01-architecture/06-cross-cutting-conventions.md §2.
PREFIXES: Final[frozenset[str]] = frozenset(
    {
        "ws",  # workspace
        "usr",  # user
        "key",  # api_key
        "grant",  # resource_grant
        "kb",  # knowledge_base
        "doc",  # document
        "chk",  # chunk
        "crawl",  # crawl_job
        "pipe",  # pipeline
        "fn",  # function
        "gs",  # golden_set
        "run",  # eval_run
        "mdl",  # model
        "prov",  # model_provider
        "bind",  # storage_binding
        "req",  # request
        "chg",  # credential change token
    }
)


class InvalidIdError(ValueError):
    """Raised when a public identifier is malformed or carries the wrong prefix."""


def new_uuid() -> UUID:
    """A UUIDv7: 48-bit millisecond timestamp + 74 random bits.

    Time-ordered, so consecutive inserts land in the same B-tree page instead of
    scattering across the index the way UUIDv4 does.
    """
    timestamp = utcnow_ms() & 0xFFFF_FFFF_FFFF  # 48 bits
    rand_a = secrets.randbits(12)
    rand_b = secrets.randbits(62)
    value = (
        (timestamp << 80)  # bits 127..80
        | (0x7 << 76)  # version 7, bits 79..76
        | (rand_a << 64)  # bits 75..64
        | (0b10 << 62)  # RFC 4122 variant, bits 63..62
        | rand_b  # bits 61..0
    )
    return UUID(int=value)


def _b32_encode(value: UUID) -> str:
    number = value.int
    chars = ["0"] * _ENCODED_LEN
    for index in range(_ENCODED_LEN - 1, -1, -1):
        chars[index] = _ALPHABET[number & 0x1F]
        number >>= 5
    return "".join(chars)


def _b32_decode(text: str) -> UUID:
    number = 0
    for char in text:
        digit = _DECODE.get(char)
        if digit is None:
            raise InvalidIdError(f"invalid character {char!r} in identifier")
        number = (number << 5) | digit
    if number >= 1 << 128:
        raise InvalidIdError("identifier out of range")
    return UUID(int=number)


def encode_id(prefix: str, value: UUID) -> str:
    """Render an internal UUID as a public identifier."""
    if prefix not in PREFIXES:
        raise InvalidIdError(f"unregistered prefix {prefix!r}")
    return f"{prefix}_{_b32_encode(value)}"


def decode_id(prefix: str, value: str) -> UUID:
    """Parse a public identifier, enforcing the expected prefix.

    The prefix check prevents passing a ``doc_`` id where a ``kb_`` id is
    expected — a type-confusion class that is otherwise invisible because both
    are opaque strings of the same shape.
    """
    if prefix not in PREFIXES:
        raise InvalidIdError(f"unregistered prefix {prefix!r}")
    expected = f"{prefix}_"
    if not value.startswith(expected):
        raise InvalidIdError(f"expected an identifier beginning {expected!r}")
    body = value[len(expected) :].upper()
    if len(body) != _ENCODED_LEN:
        raise InvalidIdError("identifier has the wrong length")
    return _b32_decode(body)


def new_public_id(prefix: str) -> str:
    """Convenience for identifiers that never need an internal UUID (e.g. requests)."""
    return encode_id(prefix, new_uuid())
