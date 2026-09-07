"""Identity — who someone is, and whether they may hold a session at all.

The forced credential-change state machine is the load-bearing piece here
(FR-A-04..07). Its enforcement lives in three places, deliberately redundant:

1. :meth:`IdentityService.login` never mints a session for a flagged principal —
   so there is nothing to abuse.
2. :class:`ForcedCredentialChangeMiddleware` rejects every non-allowlisted route
   for a flagged principal — so a session minted by some future code path is
   inert.
3. ``user.must_change_password`` lives in PostgreSQL — so browser close, session
   loss, container restart, and redeploy all leave the requirement in force.

The UI dialog is a *consequence* of this state, never the enforcement.
"""

from cairn.identity.dto import LoginResult, SessionView, UserView
from cairn.identity.service import IdentityService, get_identity_service

__all__ = [
    "IdentityService",
    "LoginResult",
    "SessionView",
    "UserView",
    "get_identity_service",
]
