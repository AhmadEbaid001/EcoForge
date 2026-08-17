"""Accounts, sessions, login throttling and audit.

The security-critical decisions live here and each is stated where it is made. The
short version:

* Session tokens are 32 bytes of CSPRNG output; only their SHA-256 is stored.
* A session has BOTH an idle timeout and an absolute lifetime.
* The token is regenerated on login, so a fixated cookie is worthless.
* Failed logins are throttled per username and per address, and the response is
  identical whether the account exists, is disabled, or the password is wrong.
* Every privileged action writes an audit row, including the ones that fail.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from gemp.auth import passwords
from gemp.auth.roles import ROLES, Role, at_least
from gemp.db import AuditRow, OrganizationRow, SessionRow, UserRow

log = logging.getLogger("gemp.auth")

SESSION_COOKIE = "gemp_session"
CSRF_COOKIE = "gemp_csrf"
CSRF_HEADER = "X-GEMP-CSRF"

TOKEN_BYTES = 32

# Idle timeout: a session left open on a shared machine stops working. Absolute
# lifetime: a stolen cookie that IS being used cannot renew itself indefinitely, which
# an idle timeout alone permits.
IDLE_TIMEOUT = timedelta(hours=8)
ABSOLUTE_LIFETIME = timedelta(days=7)

# `last_seen_at` is what enforces the idle timeout, so it has to be written - but
# writing it on every request turns a read-only page into a write on every poll. A
# minute of granularity costs nothing against an eight-hour window.
TOUCH_INTERVAL = timedelta(minutes=1)

# Throttling. Counted per username AND per address: per-username alone lets one
# address spray a password across every account, and per-address alone lets a botnet
# grind a single account.
MAX_FAILURES = 5
FAILURE_WINDOW = timedelta(minutes=15)
LOCKOUT = timedelta(minutes=15)

DEFAULT_ORG_NAME = "Default organization"


class AuthError(Exception):
    """Authentication failed. The message is safe to show a caller."""


class Throttled(AuthError):
    """Too many failures. Carries how long to wait."""

    def __init__(self, retry_after_s: int):
        super().__init__(
            f"too many failed attempts; try again in {retry_after_s} seconds"
        )
        self.retry_after_s = retry_after_s


@dataclass(frozen=True)
class Principal:
    """The authenticated caller, as the rest of the application sees them."""

    user_id: str
    username: str
    display_name: str
    role: str
    organization_id: str
    must_change_password: bool

    def can(self, required: str) -> bool:
        return at_least(self.role, required)


# --- login throttling -------------------------------------------------------
#
# In memory, which is a deliberate limit and not an oversight. The API is a single
# process by design (the modular-monolith decision), so a shared counter would mean a
# table write on every failed attempt - a free amplification primitive for anyone who
# wants to fill the disk. The cost is that a restart forgets lockouts; an attacker who
# can restart the process has already won.

_failures: dict[str, deque[float]] = defaultdict(deque)


def _record_failure(key: str) -> None:
    window = _failures[key]
    now = time.monotonic()
    window.append(now)
    cutoff = now - FAILURE_WINDOW.total_seconds()
    while window and window[0] < cutoff:
        window.popleft()


def _retry_after(key: str) -> int:
    window = _failures.get(key)
    if not window:
        return 0
    now = time.monotonic()
    cutoff = now - FAILURE_WINDOW.total_seconds()
    while window and window[0] < cutoff:
        window.popleft()
    if len(window) < MAX_FAILURES:
        return 0
    return max(1, int(LOCKOUT.total_seconds() - (now - window[-1])))


def reset_throttle() -> None:
    """Clear all counters. For tests and for an admin unlocking an account."""
    _failures.clear()


# --- audit ------------------------------------------------------------------


def audit(
    session: Session,
    action: str,
    *,
    principal: Principal | None = None,
    username: str = "",
    target: str = "",
    outcome: str = "ok",
    ip: str = "",
    detail: dict | None = None,
) -> None:
    """Append one audit row. Never raises - a failed audit must not fail the action.

    That trade is worth naming. Refusing the action when the log is unavailable would
    be the stricter choice, and for a system moving money it would be the right one.
    Here it would mean a database hiccup takes the demonstration down, so the log is
    best-effort and its own failures are logged.
    """
    try:
        session.add(AuditRow(
            ts=datetime.now(UTC),
            user_id=principal.user_id if principal else None,
            username=principal.username if principal else username,
            action=action, target=target, outcome=outcome, ip=ip, detail=detail,
        ))
        session.flush()
    except Exception:  # noqa: BLE001 - the audit trail must not break the request
        log.exception("failed to write audit row for %s", action)


# --- organizations and users ------------------------------------------------


def default_organization(session: Session) -> OrganizationRow:
    org = session.execute(select(OrganizationRow).limit(1)).scalars().first()
    if org is None:
        org = OrganizationRow(
            id=str(uuid.uuid4()), name=DEFAULT_ORG_NAME, created_at=datetime.now(UTC)
        )
        session.add(org)
        session.flush()
    return org


def normalise_username(username: str) -> str:
    """Case-insensitive and whitespace-trimmed.

    `Admin` and `admin` being two accounts is an impersonation waiting to happen, and
    the person who notices is the one already fooled by it.
    """
    return username.strip().lower()


def create_user(
    session: Session,
    *,
    username: str,
    password: str,
    role: str,
    display_name: str = "",
    organization_id: str | None = None,
    must_change_password: bool = False,
) -> UserRow:
    username = normalise_username(username)
    if not username:
        raise AuthError("username is required")
    if role not in ROLES:
        raise AuthError(f"unknown role {role!r}; choose from {sorted(ROLES)}")

    passwords.validate(password)

    if session.execute(
        select(UserRow).where(UserRow.username == username)
    ).scalars().first():
        raise AuthError(f"user {username!r} already exists")

    org_id = organization_id or default_organization(session).id
    user = UserRow(
        id=str(uuid.uuid4()),
        organization_id=org_id,
        username=username,
        display_name=display_name or username,
        role=role,
        password_hash=passwords.hash_password(password),
        must_change_password=must_change_password,
        is_active=True,
        created_at=datetime.now(UTC),
        password_changed_at=datetime.now(UTC),
    )
    session.add(user)
    session.flush()
    return user


def set_password(session: Session, user: UserRow, password: str,
                 *, must_change: bool = False) -> None:
    passwords.validate(password)
    user.password_hash = passwords.hash_password(password)
    user.password_changed_at = datetime.now(UTC)
    user.must_change_password = must_change
    # Every other session for this account dies. A password change is the action
    # someone takes when they believe a credential is compromised, and leaving the
    # attacker's session alive would defeat the point of taking it.
    revoke_all_sessions(session, user.id)
    session.flush()


def user_count(session: Session) -> int:
    return int(session.execute(select(func.count()).select_from(UserRow)).scalar_one())


# --- authentication ---------------------------------------------------------


def authenticate(session: Session, username: str, password: str,
                 *, ip: str = "") -> UserRow:
    """Check a credential. Raises `AuthError` for every kind of failure.

    Identical failure for "no such user", "wrong password" and "account disabled".
    Distinguishing them turns the login form into an account-enumeration oracle, and
    knowing which usernames are real is most of the work in a credential-stuffing
    campaign.
    """
    username = normalise_username(username)

    for key in (f"user:{username}", f"ip:{ip}"):
        wait = _retry_after(key)
        if wait:
            raise Throttled(wait)

    user = session.execute(
        select(UserRow).where(UserRow.username == username)
    ).scalars().first()

    # Hash even when the user does not exist, against a candidate that cannot match.
    # Returning early would make a missing account measurably faster to reject than a
    # wrong password, which is the timing side of the same enumeration problem.
    stored = user.password_hash if user else _ABSENT_USER_HASH
    ok = passwords.verify(password, stored)

    if not user or not ok or not user.is_active:
        _record_failure(f"user:{username}")
        _record_failure(f"ip:{ip}")
        audit(session, "auth.login", username=username, outcome="denied", ip=ip,
              detail={"reason": _denial_reason(user, ok)})
        raise AuthError("invalid username or password")

    if passwords.needs_rehash(user.password_hash):
        # The only moment the plaintext is available to upgrade the cost factor.
        user.password_hash = passwords.hash_password(password)

    _failures.pop(f"user:{username}", None)
    user.last_login_at = datetime.now(UTC)
    session.flush()
    return user


def _denial_reason(user: UserRow | None, password_ok: bool) -> str:
    """For the audit trail only. Never returned to the caller."""
    if user is None:
        return "no_such_user"
    if not password_ok:
        return "bad_password"
    return "disabled"


# A well-formed hash of a value nobody knows, so the "no such user" path does the same
# work as the real one.
_ABSENT_USER_HASH = passwords.hash_password(secrets.token_urlsafe(32))


# --- sessions ---------------------------------------------------------------


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_session(session: Session, user: UserRow, *, ip: str = "",
                  user_agent: str = "") -> tuple[str, SessionRow]:
    """Create a session and return (token, row). The token is shown once, here.

    Called only after a successful `authenticate`, and it always mints a NEW token -
    never adopting one the client supplied. That is what makes session fixation
    impossible: an attacker who plants a cookie value before login finds it replaced
    the moment the login succeeds.
    """
    token = secrets.token_urlsafe(TOKEN_BYTES)
    now = datetime.now(UTC)
    row = SessionRow(
        token_hash=_hash_token(token),
        user_id=user.id,
        created_at=now,
        last_seen_at=now,
        expires_at=now + ABSOLUTE_LIFETIME,
        ip=ip[:45],
        user_agent=(user_agent or "")[:256],
    )
    session.add(row)
    session.flush()
    return token, row


def resolve_session(session: Session, token: str) -> Principal | None:
    """Validate a token and return the principal, or None.

    Returns None for every failure mode - unknown, revoked, expired, idle too long,
    user disabled - because the caller's only correct response to any of them is the
    same: treat this request as anonymous.
    """
    if not token:
        return None

    row = session.get(SessionRow, _hash_token(token))
    if row is None or row.revoked_at is not None:
        return None

    now = datetime.now(UTC)
    if _as_utc(row.expires_at) <= now:
        return None
    if now - _as_utc(row.last_seen_at) > IDLE_TIMEOUT:
        return None

    user = session.get(UserRow, row.user_id)
    if user is None or not user.is_active:
        return None

    # Sliding idle window, written at most once a minute.
    if now - _as_utc(row.last_seen_at) > TOUCH_INTERVAL:
        row.last_seen_at = now
        session.flush()

    return Principal(
        user_id=user.id,
        username=user.username,
        display_name=user.display_name,
        role=user.role,
        organization_id=user.organization_id,
        must_change_password=user.must_change_password,
    )


def revoke_session(session: Session, token: str) -> None:
    row = session.get(SessionRow, _hash_token(token))
    if row is not None and row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)
        session.flush()


def revoke_all_sessions(session: Session, user_id: str) -> int:
    result = session.execute(
        update(SessionRow)
        .where(SessionRow.user_id == user_id, SessionRow.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )
    return int(result.rowcount or 0)


def purge_expired_sessions(session: Session) -> int:
    """Housekeeping. Expired rows are dead weight and a needless disclosure risk."""
    result = session.execute(
        SessionRow.__table__.delete().where(SessionRow.expires_at < datetime.now(UTC))
    )
    return int(result.rowcount or 0)


def active_sessions(session: Session, user_id: str) -> list[SessionRow]:
    now = datetime.now(UTC)
    return list(session.execute(
        select(SessionRow).where(
            SessionRow.user_id == user_id,
            SessionRow.revoked_at.is_(None),
            SessionRow.expires_at > now,
        ).order_by(SessionRow.last_seen_at.desc())
    ).scalars())


def _as_utc(value: datetime) -> datetime:
    """SQLite hands back naive datetimes; PostgreSQL does not.

    Comparing a naive datetime with an aware one raises, so every session check would
    fail with a TypeError on the SQLite-backed tests while working in production - or
    the reverse, which is worse.
    """
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def issue_csrf_token() -> str:
    return secrets.token_urlsafe(32)


__all__ = [
    "ABSOLUTE_LIFETIME",
    "CSRF_COOKIE",
    "CSRF_HEADER",
    "IDLE_TIMEOUT",
    "SESSION_COOKIE",
    "AuthError",
    "Principal",
    "Role",
    "Throttled",
    "active_sessions",
    "audit",
    "authenticate",
    "create_user",
    "default_organization",
    "issue_csrf_token",
    "issue_session",
    "normalise_username",
    "purge_expired_sessions",
    "reset_throttle",
    "resolve_session",
    "revoke_all_sessions",
    "revoke_session",
    "set_password",
    "user_count",
]
