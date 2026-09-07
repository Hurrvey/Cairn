"""Password hashing and token generation.

Argon2id per FR-A-08 (m >= 64 MiB, t >= 3, p = 4). Not bcrypt (72-byte input
truncation, weak memory hardness), not PBKDF2 (GPU-friendly).
"""

from __future__ import annotations

import hashlib
import secrets
from contextlib import suppress

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from cairn.core.config import AuthSettings

__all__ = ["Hasher", "constant_time_equals", "hash_token", "new_token"]

#: Verified against on unknown-user login so the work — and therefore the
#: response time — matches a real check (FR-A-11, no enumeration by timing).
_DUMMY_PASSWORD = "cairn-timing-equalisation-placeholder"  # noqa: S105


class Hasher:
    def __init__(self, settings: AuthSettings) -> None:
        self._hasher = PasswordHasher(
            memory_cost=settings.argon2_memory_kib,
            time_cost=settings.argon2_time_cost,
            parallelism=settings.argon2_parallelism,
        )
        self._dummy_hash = self._hasher.hash(_DUMMY_PASSWORD)

    def hash(self, password: str) -> str:
        return self._hasher.hash(password)

    def verify(self, password_hash: str, password: str) -> bool:
        try:
            return self._hasher.verify(password_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    def consume_dummy(self) -> None:
        """Burn equivalent CPU on the unknown-user path.

        Without this, an unknown username returns in ~1 ms while a known one
        takes ~100 ms, which enumerates the user list over a few thousand
        requests regardless of how careful the error message is.
        """
        with suppress(VerifyMismatchError, VerificationError, InvalidHashError):
            self._hasher.verify(self._dummy_hash, "not-the-password")

    def needs_rehash(self, password_hash: str) -> bool:
        return bool(self._hasher.check_needs_rehash(password_hash))


def new_token(nbytes: int = 32) -> str:
    """A 256-bit opaque token, URL-safe."""
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> str:
    """SHA-256 of a token.

    Not Argon2: the token is 256 bits of true randomness, so there is nothing to
    brute-force, and lookup must be O(1) rather than a 100 ms KDF.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def constant_time_equals(left: str, right: str) -> bool:
    return secrets.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
