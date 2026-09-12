"""Login, logout, self-service and user administration."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from gemp import mail
from gemp.api.deps import get_session
from gemp.auth import judge_pass, passwords, service
from gemp.auth.deps import ADMIN, client_ip, current_principal, require
from gemp.auth.roles import ROLES
from gemp.auth.service import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    AuthError,
    Principal,
    Throttled,
)
from gemp.config import get_settings
from gemp.db import AuditRow, UserRow

log = logging.getLogger("gemp.api.auth")

router = APIRouter(prefix="/api/v1", tags=["auth"])


# --- schemas ----------------------------------------------------------------


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=1, max_length=1024)


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)
    role: str = Field(default="viewer")
    display_name: str = Field(default="", max_length=128)


class ResetPasswordRequest(BaseModel):
    new_password: str = Field(min_length=1, max_length=1024)


class UpdateUserRequest(BaseModel):
    role: str | None = None
    is_active: bool | None = None
    display_name: str | None = Field(default=None, max_length=128)


def _user_json(user: UserRow) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "role": user.role,
        "is_active": user.is_active,
        "must_change_password": user.must_change_password,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
        "access_expires_at": _iso_utc(user.access_expires_at),
    }


def _iso_utc(value: datetime | None) -> str | None:
    """ISO 8601 WITH its offset, or None.

    SQLite hands datetimes back naive. Serialised as they are, a browser reads
    "2026-09-14T20:59:59" as its own local time, which puts the closing minute of a
    Judge Pass three hours out on every screen that counts down to it.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def _set_session_cookies(response: Response, token: str, csrf: str,
                         expires_at: datetime | None = None) -> None:
    """One HttpOnly cookie the browser cannot read, one it must.

    The session token is HttpOnly so that a cross-site scripting bug cannot exfiltrate
    it. The CSRF token deliberately is NOT: the page has to read it to echo it in a
    header, and that is exactly what an attacker on another origin cannot do.

    `expires_at` is the session row's own expiry, and the cookies are given the same
    one - so a browser drops a Judge Pass cookie at the minute the pass closes rather
    than holding a dead one for a week. The server stays the authority either way.
    """
    secure = get_settings().cookie_secure
    max_age = int(service.ABSOLUTE_LIFETIME.total_seconds())
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        remaining = int((expires_at - datetime.now(UTC)).total_seconds())
        max_age = max(1, min(max_age, remaining))
    response.set_cookie(
        SESSION_COOKIE, token,
        httponly=True, secure=secure, samesite="lax", path="/",
        max_age=max_age,
    )
    response.set_cookie(
        CSRF_COOKIE, csrf,
        httponly=False, secure=secure, samesite="lax", path="/",
        max_age=max_age,
    )


def _clear_session_cookies(response: Response) -> None:
    """Deleted with the same attributes they were set with.

    A browser matches a deletion on name, domain and path, so this worked - but
    `secure` and `samesite` were being dropped, which meant the expiring Set-Cookie
    did not describe the same cookie the login had issued. That is the kind of
    mismatch a stricter browser is entitled to reject, leaving a signed-out user
    holding a cookie for a session the server has already revoked.
    """
    secure = get_settings().cookie_secure
    for name in (SESSION_COOKIE, CSRF_COOKIE):
        response.delete_cookie(name, path="/", secure=secure, samesite="lax")


# --- session ----------------------------------------------------------------


@router.post("/auth/login")
def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
) -> dict:
    ip = client_ip(request)
    try:
        user = service.authenticate(session, body.username, body.password, ip=ip)
    except Throttled as exc:
        # 429 with Retry-After, so an honest client backs off and an attacker learns
        # only that they are being throttled - which they already knew.
        session.commit()
        raise HTTPException(
            429, str(exc), headers={"Retry-After": str(exc.retry_after_s)}
        ) from exc
    except AuthError as exc:
        session.commit()          # keep the audit row for the failed attempt
        raise HTTPException(401, str(exc)) from exc

    token, session_row = service.issue_session(
        session, user, ip=ip, user_agent=request.headers.get("user-agent", "")
    )
    csrf = service.issue_csrf_token()
    service.audit(session, "auth.login", username=user.username, ip=ip,
                  detail={"role": user.role})
    session.commit()

    _set_session_cookies(response, token, csrf, session_row.expires_at)
    return {
        "user": _user_json(user),
        "must_change_password": user.must_change_password,
        "csrf_token": csrf,
    }


@router.post("/auth/logout")
def logout(
    request: Request, response: Response, session: Session = Depends(get_session)
) -> dict:
    token = request.cookies.get(SESSION_COOKIE, "")
    if token:
        principal = getattr(request.state, "principal", None)
        service.revoke_session(session, token)
        service.audit(session, "auth.logout", principal=principal,
                      ip=client_ip(request))
        session.commit()
    _clear_session_cookies(response)
    return {"ok": True}


@router.get("/auth/session")
def whoami(request: Request) -> dict:
    """Who the caller is, or that they are nobody.

    Public and never 401s: the SPA calls it on load to decide whether to show the
    login page, and an error response for the ordinary "not signed in yet" case would
    put a red line in every console on every first visit.
    """
    principal: Principal | None = getattr(request.state, "principal", None)
    if principal is None:
        return {"authenticated": False}
    return {
        "authenticated": True,
        "user": {
            "id": principal.user_id,
            "username": principal.username,
            "display_name": principal.display_name,
            "role": principal.role,
            # Present on a Judge Pass account, null on every other. The shell counts
            # down to it and ends the pass on screen at that minute.
            "access_expires_at": _iso_utc(principal.access_expires_at),
        },
        "must_change_password": principal.must_change_password,
    }


@router.get("/auth/sessions")
def my_sessions(
    request: Request,
    principal: Principal = Depends(current_principal),
    session: Session = Depends(get_session),
) -> list[dict]:
    """Where this account is signed in. Answers "is someone else using my login?".

    Each row says whether it is the caller's own session. Without that the screen
    lists several browsers and cannot say which one you are reading it in - which
    is exactly the question somebody checking for an intruder is asking. It is
    decided by hashing the presented cookie and comparing, not by guessing from
    `last_seen_at`: the newest row is usually the caller and "usually" is not good
    enough to put a mark against a session and invite someone to act on it.

    The comparison is over hashes. The token itself is never stored, and this does
    not put it anywhere new - the hash is already the primary key of the row.
    """
    token = request.cookies.get(SESSION_COOKIE, "")
    current_hash = service.hash_token(token) if token else None

    return [
        {
            "created_at": row.created_at.isoformat(),
            "last_seen_at": row.last_seen_at.isoformat(),
            "expires_at": row.expires_at.isoformat(),
            "ip": row.ip,
            "user_agent": row.user_agent,
            "current": row.token_hash == current_hash,
        }
        for row in service.active_sessions(session, principal.user_id)
    ]


@router.post("/auth/password")
def change_password(
    body: ChangePasswordRequest,
    request: Request,
    response: Response,
    principal: Principal = Depends(current_principal),
    session: Session = Depends(get_session),
) -> dict:
    """Change your own password.

    Uses `current_principal` rather than `require(...)`, because a user who must
    change their password has to be able to reach exactly this endpoint and nothing
    else - and `require` is what enforces that block.
    """
    user = session.get(UserRow, principal.user_id)
    if user is None:
        raise HTTPException(401, "authentication required")

    # Re-authenticate. Without this, an unattended browser is a password change, and
    # a password change is a permanent account takeover.
    #
    # Throttled on the same counters as the login form. It is a password guess against
    # a known account, so it belongs under the same limit - and without one, somebody
    # who has got hold of a session (a shared machine, an unlocked screen) could grind
    # the current password here at whatever rate scrypt allows, which is the one
    # secret standing between them and permanent ownership of the account.
    #
    # `login_throttled` checks every counter the login route would - username, IP
    # where the address distinguishes callers, and the shared bucket where it does
    # not. Pre-checking only the username key left a locked-out ADDRESS free to keep
    # guessing here until the username counter tripped on its own.
    wait = service.login_throttled(principal.username, client_ip(request))
    if wait:
        raise HTTPException(
            429, f"too many failed attempts; try again in {wait} seconds",
            headers={"Retry-After": str(wait)},
        )

    if not passwords.verify(body.current_password, user.password_hash):
        ip = client_ip(request)
        service.record_login_failure(principal.username, ip)
        service.audit(session, "auth.password_change", principal=principal,
                      outcome="denied", ip=ip)
        session.commit()
        raise HTTPException(403, "current password is incorrect")

    service.clear_failures(f"user:{principal.username}")

    try:
        service.set_password(session, user, body.new_password)
    except passwords.WeakPassword as exc:
        raise HTTPException(422, str(exc)) from exc

    # set_password revokes every session including this one, so issue a fresh pair.
    # The alternative is logging someone out of the page they are standing on.
    token, session_row = service.issue_session(
        session, user, ip=client_ip(request),
        user_agent=request.headers.get("user-agent", ""))
    csrf = service.issue_csrf_token()
    service.audit(session, "auth.password_change", principal=principal,
                  ip=client_ip(request))
    session.commit()

    _set_session_cookies(response, token, csrf, session_row.expires_at)
    return {"ok": True, "csrf_token": csrf}


# --- administration ---------------------------------------------------------


@router.get("/admin/users")
def list_users(
    _admin: Principal = Depends(require(ADMIN)),
    session: Session = Depends(get_session),
) -> list[dict]:
    rows = session.execute(select(UserRow).order_by(UserRow.username)).scalars().all()
    return [_user_json(row) for row in rows]


@router.post("/admin/users", status_code=201)
def add_user(
    body: CreateUserRequest,
    request: Request,
    admin: Principal = Depends(require(ADMIN)),
    session: Session = Depends(get_session),
) -> dict:
    if body.role not in ROLES:
        raise HTTPException(422, f"unknown role; choose from {sorted(ROLES)}")
    try:
        user = service.create_user(
            session,
            username=body.username, password=body.password, role=body.role,
            display_name=body.display_name,
            organization_id=admin.organization_id,
            # An admin-chosen password is a shared secret between two people. It gets
            # the account through the door once and no further.
            must_change_password=True,
        )
    except passwords.WeakPassword as exc:
        raise HTTPException(422, str(exc)) from exc
    except AuthError as exc:
        raise HTTPException(409, str(exc)) from exc

    service.audit(session, "user.create", principal=admin, target=user.username,
                  ip=client_ip(request), detail={"role": user.role})
    session.commit()
    return _user_json(user)


@router.patch("/admin/users/{user_id}")
def update_user(
    user_id: str,
    body: UpdateUserRequest,
    request: Request,
    admin: Principal = Depends(require(ADMIN)),
    session: Session = Depends(get_session),
) -> dict:
    user = session.get(UserRow, user_id)
    if user is None:
        raise HTTPException(404, "no such user")

    if body.role is not None:
        if body.role not in ROLES:
            raise HTTPException(422, f"unknown role; choose from {sorted(ROLES)}")
        # An admin demoting themselves can leave a deployment with no admin at all,
        # recoverable only by shell access to the database.
        if user.id == admin.user_id and body.role != "admin":
            raise HTTPException(409, "an admin cannot remove their own admin role")
        user.role = body.role

    if body.is_active is not None:
        if user.id == admin.user_id and not body.is_active:
            raise HTTPException(409, "an admin cannot disable their own account")
        user.is_active = body.is_active
        if not body.is_active:
            # Disabling has to end the sessions, or the account keeps working until
            # its cookie expires - up to a week later.
            service.revoke_all_sessions(session, user.id)

    if body.display_name is not None:
        user.display_name = body.display_name

    service.audit(session, "user.update", principal=admin, target=user.username,
                  ip=client_ip(request),
                  detail={"role": body.role, "is_active": body.is_active})
    session.commit()
    return _user_json(user)


@router.post("/admin/users/{user_id}/password")
def reset_password(
    user_id: str,
    body: ResetPasswordRequest,
    request: Request,
    admin: Principal = Depends(require(ADMIN)),
    session: Session = Depends(get_session),
) -> dict:
    user = session.get(UserRow, user_id)
    if user is None:
        raise HTTPException(404, "no such user")
    try:
        service.set_password(session, user, body.new_password, must_change=True)
    except passwords.WeakPassword as exc:
        raise HTTPException(422, str(exc)) from exc

    service.audit(session, "user.password_reset", principal=admin,
                  target=user.username, ip=client_ip(request))
    session.commit()
    # A response field whose NAME ends in password, not a password.
    return {"ok": True, "must_change_password": True}  # nosec B105


@router.get("/admin/audit")
def read_audit(
    _admin: Principal = Depends(require(ADMIN)),
    session: Session = Depends(get_session),
    limit: int = 200,
    action: str | None = None,
) -> list[dict]:
    stmt = select(AuditRow).order_by(AuditRow.ts.desc()).limit(min(max(limit, 1), 1000))
    if action:
        stmt = stmt.where(AuditRow.action == action)
    return [
        {
            "ts": row.ts.isoformat(),
            "username": row.username,
            "action": row.action,
            "target": row.target,
            "outcome": row.outcome,
            "ip": row.ip,
            "detail": row.detail,
        }
        for row in session.execute(stmt).scalars()
    ]


@router.get("/admin/security")
def security_posture(
    _admin: Principal = Depends(require(ADMIN)),
    session: Session = Depends(get_session),
) -> dict:
    """What this deployment's configuration actually is, in one place.

    A security control nobody can see the state of is a control nobody maintains. It
    surfaces the two settings that are safe on an air-gapped demonstration network and
    dangerous anywhere else.
    """
    settings = get_settings()

    # Hashes still stored at a cost below the current one.
    #
    # `needs_rehash` upgrades a row on successful login, which is the only moment the
    # plaintext exists - so an account that has not signed in since the cost was
    # raised keeps its old, cheaper hash indefinitely. Two consequences, and the
    # second is the one worth surfacing: the password is protected at the weaker
    # factor, and verifying it is measurably FASTER than the deliberately-equal-cost
    # work done for an account that does not exist, which is the timing side of the
    # account-enumeration defence the login flow is built around.
    #
    # Neither is visible anywhere, so neither gets acted on. It is reported here for
    # the same reason the Secure flag is: a security control nobody can see the state
    # of is a control nobody maintains.
    stale_cost = sum(
        1 for row in session.execute(select(UserRow.password_hash)).scalars()
        if (cost := passwords.cost_of(row)) is not None and cost < passwords.DEFAULT_N
    )

    failed = session.execute(
        select(AuditRow).where(AuditRow.action == "auth.login",
                               AuditRow.outcome == "denied")
        .order_by(AuditRow.ts.desc()).limit(10)
    ).scalars().all()

    audit_failures = service.audit_failures()
    warnings = _warnings(settings, stale_cost)
    if audit_failures:
        warnings.append(
            f"{audit_failures} audit row(s) have failed to write since this "
            "process started. The trail is best-effort by design, but a gap in "
            "it means the actions behind those rows are unrecorded."
        )

    return {
        "cookie_secure": settings.cookie_secure,
        "session_idle_timeout_hours": service.IDLE_TIMEOUT.total_seconds() / 3600,
        "session_absolute_lifetime_days": service.ABSOLUTE_LIFETIME.days,
        "password_min_length": passwords.MIN_PASSWORD_LENGTH,
        "password_hash_cost": passwords.DEFAULT_N,
        "password_hashes_below_current_cost": stale_cost,
        "audit_write_failures": audit_failures,
        "users": service.user_count(session),
        "recent_failed_logins": [
            {"ts": row.ts.isoformat(), "username": row.username, "ip": row.ip}
            for row in failed
        ],
        "warnings": warnings,
    }


def _warnings(settings, stale_cost: int = 0) -> list[str]:
    out = []
    if stale_cost:
        out.append(
            f"{stale_cost} account(s) still hold a password hashed at a cost below "
            "the current one. A hash is upgraded on the owner's next successful "
            "sign-in; until then it is protected at the weaker factor and verifies "
            "faster than a nonexistent account does, which narrows the timing "
            "difference the login flow deliberately equalises."
        )
    if not settings.cookie_secure:
        out.append(
            "cookie_secure is off, so session cookies travel over plaintext HTTP. "
            "Set GEMP_COOKIE_SECURE=true and terminate TLS before exposing this "
            "beyond localhost."
        )
    return out


# --- judge pass ---------------------------------------------------------------
#
# Two public endpoints and two for administrators. The public pair is on
# PUBLIC_PATHS and demands the shared code before it answers anything; the logic,
# and every limit on what that code can do, lives in gemp/auth/judge_pass.py.


class PassCheckRequest(BaseModel):
    code: str = Field(min_length=1, max_length=128)


class PassRedeemRequest(BaseModel):
    code: str = Field(min_length=1, max_length=128)
    email: str = Field(min_length=1, max_length=254)
    lang: str = Field(default="en", max_length=8)


class PassSwitchRequest(BaseModel):
    enabled: bool


def _refused(reason: str, status: int, message: str) -> JSONResponse:
    """`reason` is a fixed word the pass page switches on; `detail` is the sentence
    for anything that only prints."""
    return JSONResponse({"detail": message, "reason": reason}, status_code=status)


@router.post("/pass/check")
def pass_check(body: PassCheckRequest, session: Session = Depends(get_session)) -> dict:
    """What the pass page should show.

    Without the right code the whole answer is "invalid": not whether passes
    exist, not when they close, not who to ask.
    """
    settings = get_settings()
    if not judge_pass.code_matches(settings, body.code):
        return {"state": "invalid", "until": None, "contact": None}
    return {
        "state": judge_pass.state(session, settings),
        "until": _iso_utc(settings.judge_pass_until),
        "contact": settings.mail_reply_to or None,
    }


@router.post("/pass/redeem", response_model=None)
def pass_redeem(
    body: PassRedeemRequest,
    request: Request,
    response: Response,
    background: BackgroundTasks,
    session: Session = Depends(get_session),
) -> dict | JSONResponse:
    """Issue a pass and sign this browser in with it, in one step.

    The judge is in before the email exists. It is sent after the response, so a
    slow provider - or none at all - never stands between a judge at the booth and
    the product. The email is for coming BACK, on this device or another.
    """
    settings = get_settings()
    if not judge_pass.code_matches(settings, body.code):
        return _refused("invalid", 404, "This pass link is not valid.")

    email = judge_pass.normalise_email(body.email)
    if email is None:
        return _refused("email", 422,
                        "Enter a complete email address of up to 64 characters.")
    lang = body.lang if body.lang in judge_pass.LANGS else "en"

    try:
        issued = judge_pass.issue(session, settings, email=email, lang=lang)
    except judge_pass.Refused as exc:
        log.info("judge pass refused: %s", exc.reason)
        return _refused(exc.reason, exc.status, exc.message)
    except IntegrityError:
        # Two confirmations for one address in the same instant; the first won.
        session.rollback()
        return _refused("exists", 409, judge_pass.EXISTS_MESSAGE)

    ip = client_ip(request)
    # Redeeming the pass IS the first sign-in, and the administration screen's
    # "last sign-in" column should say so.
    issued.user.last_login_at = datetime.now(UTC)
    token, session_row = service.issue_session(
        session, issued.user, ip=ip, user_agent=request.headers.get("user-agent", ""))
    csrf = service.issue_csrf_token()
    service.audit(session, "pass.redeem", username=issued.user.username, ip=ip,
                  detail={"lang": lang, "mail": issued.row.mail_status})
    session.commit()

    _set_session_cookies(response, token, csrf, session_row.expires_at)

    if issued.row.mail_status == "queued":
        message = judge_pass.compose(
            lang, email=email, password=issued.password,
            until=settings.judge_pass_until, sign_in_url=judge_pass.sign_in_url(settings),
        )
        # The session provider the request itself used, honouring any override -
        # which is how the test suite's database receives the outcome too.
        provider = request.app.dependency_overrides.get(get_session, get_session)
        background.add_task(judge_pass.deliver, provider, issued.row.id, email,
                            message, settings)

    return {
        "user": _user_json(issued.user),
        "must_change_password": False,
        "csrf_token": csrf,
        "pass": {"email": email, "until": _iso_utc(settings.judge_pass_until),
                 "mail": issued.row.mail_status},
    }


def _judge_pass_overview(session: Session) -> dict:
    settings = get_settings()
    now = datetime.now(UTC)
    problem = judge_pass.problem(settings)
    holders = judge_pass.holders(session)
    return {
        "state": judge_pass.state(session, settings, now),
        "problem": problem,
        "enabled": judge_pass.enabled(session),
        "until": _iso_utc(settings.judge_pass_until),
        "daily_max": settings.judge_pass_daily_max,
        "issued_24h": judge_pass.issued_in_window(session, now),
        "total": len(holders),
        "mail_configured": mail.configured(settings),
        # What the QR on the cards must encode, so a printed card can be checked
        # against it. The code is on every card anyway, and this is admin-only.
        "link": None if problem else judge_pass.link(settings),
        "passes": [
            {
                "email": row.email,
                "created_at": _iso_utc(row.created_at),
                "lang": row.lang,
                "mail_status": row.mail_status,
                "mail_detail": row.mail_detail,
                "mail_at": _iso_utc(row.mail_at),
                "last_login_at": _iso_utc(user.last_login_at),
                "is_active": user.is_active,
            }
            for row, user in holders
        ],
    }


@router.get("/admin/judge-pass")
def judge_pass_status(
    _admin: Principal = Depends(require(ADMIN)),
    session: Session = Depends(get_session),
) -> dict:
    return _judge_pass_overview(session)


@router.put("/admin/judge-pass")
def judge_pass_switch(
    body: PassSwitchRequest,
    request: Request,
    admin: Principal = Depends(require(ADMIN)),
    session: Session = Depends(get_session),
) -> dict:
    """Pause or resume issuing. Pausing stops new cards working; it signs nobody
    out - a judge halfway through the product is not what it exists to stop."""
    judge_pass.set_enabled(session, body.enabled, by=admin.username)
    service.audit(session, "pass.switch", principal=admin, ip=client_ip(request),
                  detail={"enabled": body.enabled})
    session.commit()
    return _judge_pass_overview(session)


def utcnow() -> datetime:
    return datetime.now(UTC)


__all__ = ["router"]
