"""Identity, access control and audit.

Kept out of `domain/` on purpose: the optimizer must stay runnable from CSV fixtures
with no database, no web framework and no notion of who is asking. Authentication is
a property of the delivery layer, not of the decision model.
"""

from __future__ import annotations

from gemp.auth.roles import ROLES, Role, at_least

__all__ = ["ROLES", "Role", "at_least"]
