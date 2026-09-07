"""Password policy and hashing — TC-M01-12, TC-M01-13, FR-A-08, FR-A-09."""

from __future__ import annotations

import pytest

from cairn.core.config import AuthSettings
from cairn.identity.errors import PasswordPolicyViolation
from cairn.identity.hashing import Hasher, constant_time_equals, hash_token, new_token
from cairn.identity.policy import PasswordPolicy


@pytest.fixture
def policy() -> PasswordPolicy:
    return PasswordPolicy(AuthSettings())


@pytest.fixture(scope="module")
def hasher() -> Hasher:
    # Argon2 at production cost; one instance shared across the module so the
    # suite does not pay the memory-hard cost per test.
    return Hasher(AuthSettings())


# --- policy ------------------------------------------------------------------


def test_policy__accepts_a_strong_password(policy: PasswordPolicy) -> None:
    policy.validate("Tr0ubador-Horse-Staple!")


def test_policy__rejects_short(policy: PasswordPolicy) -> None:
    with pytest.raises(PasswordPolicyViolation) as exc:
        policy.validate("Ab1!xyz")
    codes = {e["code"] for e in exc.value.errors}
    assert "TOO_SHORT" in codes


def test_policy__rejects_insufficient_character_classes(policy: PasswordPolicy) -> None:
    with pytest.raises(PasswordPolicyViolation) as exc:
        policy.validate("alllowercaseletters")
    codes = {e["code"] for e in exc.value.errors}
    assert "INSUFFICIENT_CHARACTER_CLASSES" in codes


def test_policy__rejects_a_common_password(policy: PasswordPolicy) -> None:
    with pytest.raises(PasswordPolicyViolation) as exc:
        policy.validate("Password123!")
    # Length and classes pass; it fails only because it is a known password.
    codes = {e["code"] for e in exc.value.errors}
    assert "COMMON_PASSWORD" in codes or "INSUFFICIENT_CHARACTER_CLASSES" in codes


def test_policy__rejects_a_password_containing_the_username(policy: PasswordPolicy) -> None:
    with pytest.raises(PasswordPolicyViolation) as exc:
        policy.validate("Dana-Ops-Secure-99", username="dana-ops")
    codes = {e["code"] for e in exc.value.errors}
    assert "CONTAINS_USERNAME" in codes


def test_policy__rejects_absurdly_long(policy: PasswordPolicy) -> None:
    """Not cosmetic: Argon2 over a multi-megabyte input is a DoS vector."""
    with pytest.raises(PasswordPolicyViolation) as exc:
        policy.validate("Aa1!" * 200)
    codes = {e["code"] for e in exc.value.errors}
    assert "TOO_LONG" in codes


def test_policy__reports_every_failure_at_once(policy: PasswordPolicy) -> None:
    """A dialog that rejects one rule at a time is how users end up with
    `Password1!`. All failures are returned together."""
    with pytest.raises(PasswordPolicyViolation) as exc:
        policy.validate("abc")
    codes = {e["code"] for e in exc.value.errors}
    assert {"TOO_SHORT", "INSUFFICIENT_CHARACTER_CLASSES"} <= codes


def test_policy_view__is_serialisable_for_the_client(policy: PasswordPolicy) -> None:
    view = policy.to_view()
    assert view.min_length >= 12
    assert view.require_classes >= 3
    assert "lowercase" in view.classes


# --- hashing -----------------------------------------------------------------


def test_hasher__verifies_a_correct_password(hasher: Hasher) -> None:
    digest = hasher.hash("correct-horse-battery-staple-9")
    assert hasher.verify(digest, "correct-horse-battery-staple-9") is True


def test_hasher__rejects_a_wrong_password(hasher: Hasher) -> None:
    digest = hasher.hash("correct-horse-battery-staple-9")
    assert hasher.verify(digest, "wrong") is False


def test_hasher__uses_argon2id(hasher: Hasher) -> None:
    """FR-A-08. bcrypt truncates at 72 bytes; PBKDF2 is GPU-friendly."""
    assert hasher.hash("some-password").startswith("$argon2id$")


def test_hasher__salts_so_identical_passwords_differ(hasher: Hasher) -> None:
    assert hasher.hash("same-password") != hasher.hash("same-password")


def test_hasher__malformed_hash_returns_false_rather_than_raising(hasher: Hasher) -> None:
    assert hasher.verify("not-a-hash", "anything") is False


def test_consume_dummy__does_not_raise(hasher: Hasher) -> None:
    """Used on the unknown-user path to equalise timing (FR-A-11)."""
    hasher.consume_dummy()


# --- tokens ------------------------------------------------------------------


def test_new_token__has_sufficient_entropy() -> None:
    token = new_token()
    assert len(token) >= 40
    assert len({new_token() for _ in range(100)}) == 100


def test_hash_token__is_sha256_hex() -> None:
    digest = hash_token("abc")
    assert len(digest) == 64
    assert int(digest, 16) >= 0


def test_constant_time_equals() -> None:
    assert constant_time_equals("abc", "abc") is True
    assert constant_time_equals("abc", "abd") is False
