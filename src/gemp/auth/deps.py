"""FastAPI wiring for authentication and authorisation.

Two layers, and the split is the point.

**Authentication is enforced by middleware**, not by remembering to add a dependency.
Everything under `/api/` requires a session unless its path is on an explicit public
list. A route added next month is protected the moment it exists, by someone who has
never read this file - which is the only kind of default that survives a deadline.

**Authorisation is per route**, because only the route knows whether it reads or
writes. `Depends(require(Role.ANALYST))` is the whole vocabulary.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from gemp.auth.roles import Role
from gemp.auth.service import (
    CSRF_COOKIE,
    CSRF_HEADER,
    SESSION_COOKIE,
    Principal,
    resolve_session,
)

# Paths reachable without a session. Everything else under /api/ is denied.
#
# Deliberately short, and each entry is a decision:
#   /health         - a readiness probe cannot hold a credential, and it reports
#                     dependency status only, never data.
#   /api/v1/auth/*  - logging in without being logged in is the point.
PUBLIC_PATHS: frozenset[str] = frozenset({
    "/health",
    "/api/v1/auth/login",
    "/api/v1/auth/logout",
    "/api/v1/auth/session",
})

# Prefixes served to anonymous browsers: the login page and its assets. The SPA shell
# itself is public because it contains no data - it asks the API for everything, and
# the API is what refuses.
PUBLIC_PREFIXES: tuple[str, ...] = ("/docs", "/openapi.json", "/redoc")

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


def principal_from_request(request: Request) -> Principal | None:
    """The principal the middleware resolved, if any."""
    return getattr(request.state, "principal", None)


def current_principal(request: Request) -> Principal:
    """The authenticated caller. 401 when there is none.

    The middleware has already refused anonymous requests to non-public paths, so
    reaching this with no principal means a route was added to the public list by
    mistake. Failing closed here too costs one comparison.
    """
    principal = principal_from_request(request)
    if principal is None:
        raise HTTPException(401, "authentication required")
    return principal


def require(role: str) -> Callable[..., Principal]:
    """Dependency factory: demand at least `role`.

    The returned callable carries the requirement as an attribute so the deny-by-
    default test can read what each route asked for, rather than trusting that
    somebody remembered to ask.
    """

    def dependency(principal: Principal = Depends(current_principal)) -> Principal:
        if not principal.can(role):
            # 403, not 404. Hiding the existence of an endpoint from someone who is
            # already authenticated buys nothing - they can read the OpenAPI schema -
            # and it makes a permissions problem look like a broken deployment.
            raise HTTPException(403, f"this action requires the {role} role")

        # A forced password change blocks everything except changing it. Otherwise a
        # temporary credential handed out by an admin works indefinitely as long as
        # its holder never visits the settings page.
        if principal.must_change_password:
            raise HTTPException(
                409, "password change required", headers={"X-GEMP-Password-Change": "1"}
            )
        return principal

    dependency.__gemp_required_role__ = role  # type: ignore[attr-defined]
    return dependency


def check_csrf(request: Request) -> None:
    """Double-submit token check on state-changing requests.

    `SameSite=Lax` already stops the classic cross-site form post, and every mutating
    endpoint here consumes JSON, which a plain HTML form cannot send cross-origin
    without a preflight. This is the third layer: a token in a readable cookie that
    must be echoed in a header. An attacker on another origin can cause the cookie to
    be SENT but cannot READ it, so they cannot produce the header.

    Skipped when there is no session cookie at all - an anonymous request has nothing
    to forge on behalf of.

    Also skipped for the public auth endpoints, and that exemption is load-bearing
    rather than convenient. A browser holding a STALE session cookie and no CSRF
    cookie - an expired session, a revoked one, a cookie left over from a previous
    deployment - would otherwise have its login POST refused by this check, leaving
    the user unable to authenticate and unable to see why. They would be locked out by
    the security control rather than by the security decision.

    What that gives up is protection against login-CSRF, where an attacker forces a
    victim to log in as THEM and then reads what the victim does in that account.
    `SameSite=Lax` is what covers it: a cross-site POST does not carry cookies at all,
    so the forged login cannot arrive with anything attached.
    """
    if request.method in SAFE_METHODS:
        return
    if is_public(request.url.path):
        return
    if not request.cookies.get(SESSION_COOKIE):
        return

    cookie = request.cookies.get(CSRF_COOKIE, "")
    header = request.headers.get(CSRF_HEADER, "")
    if not cookie or not header or not _constant_time_equal(cookie, header):
        raise HTTPException(403, "missing or invalid CSRF token")


def _constant_time_equal(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left, right)


def resolve_principal(session: Session, request: Request) -> Principal | None:
    token = request.cookies.get(SESSION_COOKIE, "")
    return resolve_session(session, token) if token else None


def client_ip(request: Request) -> str:
    """Best-effort client address for audit rows and throttling.

    `X-Forwarded-For` is only consulted for its FIRST entry and only because nginx
    sits in front and sets it. It is attacker-controlled in general, which is why it
    is used for logging and rate-limit keys and never for an authorisation decision.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:45]
    return (request.client.host if request.client else "")[:45]


VIEWER = Role.VIEWER.value
ANALYST = Role.ANALYST.value
ADMIN = Role.ADMIN.value
