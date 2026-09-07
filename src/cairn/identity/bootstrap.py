"""Bootstrap banner.

The generated password is written with ``print()``, deliberately **not** through
the structured logger: log lines are shipped to aggregation and retained, and a
credential must not end up there (NFR-SEC-03). stdout at container start is
read once by a human and is not persisted by Cairn.
"""

from __future__ import annotations

import sys

from cairn.identity.dto import BootstrapResult

__all__ = ["print_bootstrap_banner"]

_WIDTH = 66


def _line(text: str = "") -> str:
    return f"║  {text.ljust(_WIDTH - 2)}║"


def print_bootstrap_banner(result: BootstrapResult) -> None:
    """Print the initial credentials exactly once, unmissably."""
    if not result.password_printed or result.password is None:
        return

    banner = "\n".join(
        [
            "",
            "╔" + "═" * _WIDTH + "╗",
            _line("Cairn — initial administrator account created"),
            _line(),
            _line(f"  username:  {result.username}"),
            _line(f"  password:  {result.password}"),
            _line(),
            _line("This password is displayed ONCE and is not recoverable."),
            _line("You will be required to change it at first login."),
            "╚" + "═" * _WIDTH + "╝",
            "",
        ]
    )
    print(banner, file=sys.stdout, flush=True)
