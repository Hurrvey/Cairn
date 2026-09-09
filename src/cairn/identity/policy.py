"""Server-side password policy (FR-A-09).

The client receives the policy so it can render live validation, but the server
is the only authority — client-side checks are cosmetic and trivially bypassed
by anything that is not a browser.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from cairn.core.config import AuthSettings
from cairn.core.errors import FieldError
from cairn.identity.dto import PasswordPolicyView
from cairn.identity.errors import PasswordPolicyViolation
from cairn.identity.hashing import new_token

__all__ = ["PasswordPolicy"]

_COMMON_PASSWORDS_FILE = Path(__file__).parent / "common_passwords.txt"

_CLASSES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("lowercase", re.compile(r"[a-z]")),
    ("uppercase", re.compile(r"[A-Z]")),
    ("digit", re.compile(r"[0-9]")),
    ("symbol", re.compile(r"[^A-Za-z0-9]")),
)


@lru_cache(maxsize=1)
def _common_passwords() -> frozenset[str]:
    if not _COMMON_PASSWORDS_FILE.exists():  # pragma: no cover
        return frozenset()
    return frozenset(
        line.strip().lower()
        for line in _COMMON_PASSWORDS_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    )


@dataclass
class PasswordPolicy:
    settings: AuthSettings

    def generate(self, *, username: str) -> str:
        """Generate a CSPRNG password that satisfies the same policy as user input."""
        length = min(max(24, self.settings.password_min_length), self.settings.password_max_length)
        if length < self.settings.password_min_length:
            raise PasswordPolicyViolation("The configured password length limits conflict.")
        for _ in range(128):
            password = new_token((length * 3 + 3) // 4)[:length]
            try:
                self.validate(password, username=username)
            except PasswordPolicyViolation:
                continue
            return password
        raise PasswordPolicyViolation("Could not generate a password satisfying the policy.")

    def to_view(self) -> PasswordPolicyView:
        return PasswordPolicyView(
            min_length=self.settings.password_min_length,
            max_length=self.settings.password_max_length,
            require_classes=self.settings.password_require_classes,
        )

    @staticmethod
    def _is_common(password: str) -> bool:
        """Check the password, and its de-decorated form, against the corpus.

        Appending a digit or a ``!`` to a known-bad password is the single most
        common way to satisfy a character-class rule while changing nothing about
        how guessable the password is. Stripping leading/trailing non-letters
        before the lookup costs nothing and catches it.
        """
        corpus = _common_passwords()
        lowered = password.lower()
        if lowered in corpus:
            return True
        stripped = re.sub(r"^[^a-z]+|[^a-z]+$", "", lowered)
        return bool(stripped) and stripped in corpus

    def validate(self, password: str, *, username: str | None = None) -> None:
        """Raise :class:`PasswordPolicyViolation` listing *every* failed rule.

        Reporting all failures at once matters: a dialog that rejects a password
        one rule at a time is how users end up with ``Password1!``.
        """
        errors: list[FieldError] = []

        if len(password) < self.settings.password_min_length:
            errors.append(
                FieldError(
                    "new_password",
                    "TOO_SHORT",
                    f"must be at least {self.settings.password_min_length} characters",
                )
            )

        if len(password) > self.settings.password_max_length:
            # Not cosmetic: Argon2 over a multi-megabyte input is a DoS vector.
            errors.append(
                FieldError(
                    "new_password",
                    "TOO_LONG",
                    f"must be at most {self.settings.password_max_length} characters",
                )
            )

        matched = [name for name, pattern in _CLASSES if pattern.search(password)]
        if len(matched) < self.settings.password_require_classes:
            missing = [name for name, _ in _CLASSES if name not in matched]
            errors.append(
                FieldError(
                    "new_password",
                    "INSUFFICIENT_CHARACTER_CLASSES",
                    f"must contain at least {self.settings.password_require_classes} of "
                    f"lowercase, uppercase, digit, symbol (missing: {', '.join(missing)})",
                )
            )

        if self._is_common(password):
            errors.append(
                FieldError(
                    "new_password",
                    "COMMON_PASSWORD",
                    "this password appears in lists of commonly used passwords",
                )
            )

        if username and len(username) >= 3 and username.lower() in password.lower():
            errors.append(
                FieldError(
                    "new_password",
                    "CONTAINS_USERNAME",
                    "must not contain the username",
                )
            )

        if errors:
            raise PasswordPolicyViolation("The password does not meet the policy.", errors=errors)
