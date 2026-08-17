"""Three roles, ordered.

Ordered rather than a permission matrix because the distinctions here are genuinely
hierarchical - anything an analyst may do, an admin may do - and a matrix would invite
combinations nobody has thought about. If a permission ever needs to be granted
independently of the ladder, that is the moment to replace this, not before.

    viewer   read the portfolio, the map, stored runs, metrics and alerts
    analyst  + run the optimizer, acknowledge alerts
    admin    + manage users, recompute candidates, read the audit log
"""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    VIEWER = "viewer"
    ANALYST = "analyst"
    ADMIN = "admin"


# Index is authority. Comparing positions keeps `at_least` a single lookup and makes
# adding a tier a one-line change rather than an audit of every call site.
ORDER: tuple[Role, ...] = (Role.VIEWER, Role.ANALYST, Role.ADMIN)

ROLES: frozenset[str] = frozenset(role.value for role in Role)


def at_least(actual: str, required: str) -> bool:
    """True when `actual` is at or above `required` in the ladder.

    An unknown role is never sufficient. Roles arrive from the database, and a row
    holding a value this code does not recognise must fail closed - a typo in a
    migration should lock someone out, not let them past.
    """
    try:
        return ORDER.index(Role(actual)) >= ORDER.index(Role(required))
    except ValueError:
        return False
