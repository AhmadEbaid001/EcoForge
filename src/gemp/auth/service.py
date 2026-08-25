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
import ipaddress
import logging
import secrets
import threading
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

# When the client address cannot distinguish callers - which on this deployment is
# EVERY request, because they all arrive from nginx's private bridge address - the
# per-IP budget would never arm, and one address could grind candidate usernames
# forever at whatever rate scrypt allows. A single SHARED bucket arms instead: a
# ceiling an honest deployment never approaches (five failures to log in is a bad
# morning, fifty is an attack), tight enough that username spraying across the whole
# account space finishes in minutes rather than running unbounded. It is ten times
# the per-username budget precisely so it cannot become the shared-lockout lever
# `_distinguishing_ip` was written to remove: one user's five typos cost a tenth of
# it, not all of it.
SHARED_IP_KEY = "ip:_shared"
MAX_SHARED_FAILURES = MAX_FAILURES * 10

# What the shared bucket does when it fills, and it is deliberately NOT a lockout.
#
# Refusing on this key refuses EVERY caller, because every caller shares it - which
# is the shared-lockout DoS `_distinguishing_ip` was written to remove, returned at
# ten times the price. Measured against the previous revision: fifty sprayed
# failures locked an untouched account, and the real admin, for the full 900s; and
# since a throttled attempt raises before it records, the window decays from the
# last RECORDED failure, so fifty requests every fifteen minutes sustained a total
# login outage indefinitely.
#
# So the bucket buys time instead. Once armed, each attempt from the shared address
# pays a delay before its password is checked: spraying drops from roughly six
# hundred attempts a minute to under sixty, which is the same order of reduction the
# lockout was reaching for, and nobody is ever refused. Per-USERNAME lockout is
# untouched and remains the control that stops guessing a known account.
SHARED_DELAY_S = 1.0

# A delay holds a threadpool worker, so an unbounded number of them is its own
# outage. Past this many concurrent sleepers the delay is SKIPPED rather than
# queued - degrading the slowdown under a flood is survivable, running the pool dry
# is not.
MAX_CONCURRENT_SHARED_DELAYS = 8
_shared_delay_slots = threading.BoundedSemaphore(MAX_CONCURRENT_SHARED_DELAYS)

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

# Sync endpoints run in FastAPI's threadpool, so every mutation below is reachable
# from several threads at once. Individual deque operations are GIL-atomic, but the
# check-then-act sequences (trim, then test length; sweep, then append) are not -
# interleaving them costs an off-by-one attempt inside a window, which is harmless,
# and a KeyError mid-sweep racing a clear, which is not. One lock is cheaper than
# reasoning about which interleavings survive.
_throttle_lock = threading.Lock()

# The keys are attacker-chosen - `user:<whatever was typed at the login form>` - and
# nothing here ever removed one. A login flood with a fresh username each time grew
# this dictionary for the life of the process, and a window was only ever trimmed if
# that exact key came back. The sweep below is what bounds it: keys whose window has
# aged out are dropped, not merely emptied. Sweeping is O(keys), so it is deferred
# until there are enough keys for that to be worth doing - an honest deployment never
# reaches the threshold.
_SWEEP_ABOVE_KEYS = 512


def _trim(window: deque[float], now: float) -> deque[float]:
    """Drop attempts that have fallen out of the window. Returns the same deque."""
    cutoff = now - FAILURE_WINDOW.total_seconds()
    while window and window[0] < cutoff:
        window.popleft()
    return window


def _sweep(now: float) -> None:
    """Forget every key whose window is now empty."""
    stale = [key for key, window in _failures.items() if not _trim(window, now)]
    for key in stale:
        _failures.pop(key, None)


def _record_failure(key: str) -> None:
    now = time.monotonic()
    with _throttle_lock:
        if len(_failures) > _SWEEP_ABOVE_KEYS:
            _sweep(now)
        _trim(_failures[key], now).append(now)


def _retry_after(key: str, *, limit: int = MAX_FAILURES) -> int:
    with _throttle_lock:
        window = _failures.get(key)
        if not window:
            return 0
        now = time.monotonic()
        if not _trim(window, now):
            # Nothing left in the window, so stop holding the key open.
            _failures.pop(key, None)
            return 0
        if len(window) < limit:
            return 0
        return max(1, int(LOCKOUT.total_seconds() - (now - window[-1])))


def reset_throttle() -> None:
    """Clear all counters. For tests and for an admin unlocking an account."""
    with _throttle_lock:
        _failures.clear()


def lockout_keys(username: str, ip: str) -> list[str]:
    """The counters that may REFUSE this attempt.

    Only keys that identify one party: the account being tried, and the address when
    it names a single client. The shared key is deliberately absent - see
    SHARED_DELAY_S for why refusing on it refuses everybody.
    """
    keys = [f"user:{normalise_username(username)}"]
    if _distinguishing_ip(ip):
        keys.append(f"ip:{ip}")
    return keys


def login_throttle_keys(username: str, ip: str) -> list[str]:
    """Every counter a failed attempt is RECORDED against.

    The lockout keys, plus the shared bucket when the address cannot distinguish
    callers. Recording against it is what lets spraying arm the delay; it is never
    consulted to refuse. One definition, used by the login route to check, by the
    password-change route so its re-authentication sits under the same budget, and
    by `authenticate` to record - three places hand-building key lists is how one of
    them ends up checking a counter nothing records against.
    """
    keys = lockout_keys(username, ip)
    if not _distinguishing_ip(ip):
        keys.append(SHARED_IP_KEY)
    return keys


def login_throttled(username: str, ip: str) -> int:
    """Seconds until this username/address combination may try again, or 0."""
    waits = [_retry_after(key) for key in lockout_keys(username, ip)]
    return max(waits) if waits else 0


def shared_spray_delay() -> float:
    """Seconds this attempt should pay because the shared bucket is armed, or 0."""
    now = time.monotonic()
    with _throttle_lock:
        window = _failures.get(SHARED_IP_KEY)
        depth = len(_trim(window, now)) if window else 0
    return SHARED_DELAY_S if depth >= MAX_SHARED_FAILURES else 0.0


def _pay_shared_delay() -> None:
    """Sleep out the spray delay, if one is owed and a worker can be spared."""
    delay = shared_spray_delay()
    if not delay:
        return
    if not _shared_delay_slots.acquire(blocking=False):
        # Never queue. Waiting for a slot is exactly the outage this design exists
        # to avoid, so a flood loses the slowdown rather than the service.
        return
    try:
        time.sleep(delay)
    finally:
        _shared_delay_slots.release()


def record_login_failure(username: str, ip: str) -> None:
    """Record one failed attempt against every counter it was checked against."""
    for key in login_throttle_keys(username, ip):
        _record_failure(key)


def clear_failures(key: str) -> None:
    with _throttle_lock:
        _failures.pop(key, None)


# --- audit ------------------------------------------------------------------


# Best-effort means failures are swallowed, and a swallowed failure is invisible:
# an audit trail that has silently stopped writing looks exactly like a quiet
# system. The count turns that state into something /admin/security can report.
_audit_failures = 0


def audit_failures() -> int:
    """How many audit rows have failed to write since process start."""
    return _audit_failures


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
    best-effort, its own failures are logged, and they are counted where an admin can
    see them.
    """
    global _audit_failures
    try:
        session.add(AuditRow(
            ts=datetime.now(UTC),
            user_id=principal.user_id if principal else None,
            username=principal.username if principal else username,
            action=action, target=target, outcome=outcome, ip=ip, detail=detail,
        ))
        session.flush()
    except Exception:  # noqa: BLE001 - the audit trail must not break the request
        _audit_failures += 1
        log.exception("failed to write audit row for %s (%d so far)",
                      action, _audit_failures)


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


def _distinguishing_ip(ip: str) -> bool:
    """True when `ip` identifies ONE client, false for a shared proxy address.

    Per-IP throttling assumes the address names a single caller. It does not behind a
    reverse proxy that terminates the client connection - nginx here, and Tailscale
    Funnel in front of it - where every request arrives from the proxy's own address:
    loopback, or the container bridge. Keyed on that, five failed logins from anyone
    lock the login form for EVERYONE, which turns an account-protection control into
    an unauthenticated denial-of-service lever. Verified: a request forwarded through
    the proxy records one fixed source address for every visitor.

    So per-IP throttling is skipped when the observed address cannot distinguish
    clients - loopback, link-local, unspecified, or any private/shared range
    (RFC1918, and RFC6598 which covers a Tailscale tailnet). Per-USERNAME throttling
    is unaffected and remains the control that actually stops guessing an account:
    five attempts per username per fifteen minutes, whatever address they come from.
    A genuinely routable client address still gets its own per-IP budget, so if the
    real client IP is ever plumbed through, this re-activates on its own.
    """
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_unspecified
        or addr.is_reserved
        or addr.is_multicast
    )


def authenticate(session: Session, username: str, password: str,
                 *, ip: str = "") -> UserRow:
    """Check a credential. Raises `AuthError` for every kind of failure.

    Identical failure for "no such user", "wrong password" and "account disabled".
    Distinguishing them turns the login form into an account-enumeration oracle, and
    knowing which usernames are real is most of the work in a credential-stuffing
    campaign.
    """
    username = normalise_username(username)

    # Per-username always; per-IP when the address names one client. When it does
    # not - the normal case here, where every request arrives from nginx's private
    # address - a single SHARED bucket arms instead, so spraying candidate
    # usernames is still bounded (see MAX_SHARED_FAILURES). The keys are built by
    # `login_throttle_keys` so the pre-check in the password-change route and the
    # recording here can never disagree about what counts.
    throttle_keys = login_throttle_keys(username, ip)
    for key in lockout_keys(username, ip):
        wait = _retry_after(key)
        if wait:
            raise Throttled(wait)

    # Armed only by spraying, and it costs time rather than access.
    _pay_shared_delay()

    user = session.execute(
        select(UserRow).where(UserRow.username == username)
    ).scalars().first()

    # Hash even when the user does not exist, against a candidate that cannot match.
    # Returning early would make a missing account measurably faster to reject than a
    # wrong password, which is the timing side of the same enumeration problem.
    stored = user.password_hash if user else _ABSENT_USER_HASH
    ok = passwords.verify(password, stored)

    if not user or not ok or not user.is_active:
        for key in throttle_keys:
            _record_failure(key)
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


def hash_token(token: str) -> str:
    """The value stored for a session token. Public because the sessions endpoint
    needs it to say which row is the caller's own, and re-implementing a hash in a
    second place is how the two drift apart."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


_hash_token = hash_token


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
    "audit_failures",
    "authenticate",
    "create_user",
    "default_organization",
    "issue_csrf_token",
    "issue_session",
    "lockout_keys",
    "login_throttle_keys",
    "login_throttled",
    "normalise_username",
    "purge_expired_sessions",
    "record_login_failure",
    "reset_throttle",
    "shared_spray_delay",
    "resolve_session",
    "revoke_all_sessions",
    "revoke_session",
    "set_password",
    "user_count",
]
