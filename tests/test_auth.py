"""Authentication, authorisation and the properties that make them worth having.

These are written the way a reviewer would attack the system, not the way the feature
was built: what happens with no cookie, a stolen cookie, a forged one, a cookie from a
disabled account, a password guessed a hundred times, a viewer reaching for an admin
route. A login page that works is easy; one that fails correctly is the deliverable.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("GEMP_HMAC_KEY", "cd" * 32)
os.environ["GEMP_INGEST_ENABLED"] = "0"

from gemp.api import main as api_main  # noqa: E402
from gemp.api.deps import get_session  # noqa: E402
from gemp.auth import passwords, service  # noqa: E402
from gemp.auth.deps import PUBLIC_PATHS  # noqa: E402
from gemp.auth.service import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE  # noqa: E402
from gemp.db import Base, SessionRow, UserRow  # noqa: E402
from gemp.repository import import_portfolio  # noqa: E402

ADMIN_PASSWORD = "correct-horse-battery-staple"
VIEWER_PASSWORD = "a-perfectly-fine-passphrase"
ANALYST_PASSWORD = "another-fine-long-passphrase"


@pytest.fixture
def client(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'auth.db'}")
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def scope():
        session = maker()
        try:
            yield session
            session.commit()
        finally:
            session.close()

    with scope() as session:
        import_portfolio(session)
        service.create_user(session, username="boss", password=ADMIN_PASSWORD,
                            role="admin")
        service.create_user(session, username="watcher", password=VIEWER_PASSWORD,
                            role="viewer")
        service.create_user(session, username="analyst", password=ANALYST_PASSWORD,
                            role="analyst")

    def override():
        session = maker()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    api_main.app.dependency_overrides[get_session] = override
    api_main.reset_context()
    service.reset_throttle()

    with TestClient(api_main.app) as test_client:
        test_client.session_scope = scope
        yield test_client

    api_main.app.dependency_overrides.clear()
    api_main.reset_context()
    service.reset_throttle()


def login(client, username: str, password: str):
    response = client.post("/api/v1/auth/login",
                           json={"username": username, "password": password})
    if response.status_code == 200:
        # The browser echoes the CSRF cookie in a header; httpx will not do it for us.
        client.headers[CSRF_HEADER] = response.json()["csrf_token"]
    return response


# --- unauthenticated --------------------------------------------------------


@pytest.mark.parametrize("path", [
    "/api/v1/meta",
    "/api/v1/buildings",
    "/api/v1/map/geojson",
    "/api/v1/metrics/anomaly",
    "/api/v1/runs/whatever",
    "/api/v1/integrity/verify/b001",
])
def test_reads_require_a_session(client, path):
    assert client.get(path).status_code == 401


def test_writes_require_a_session(client):
    assert client.post("/api/v1/optimize",
                       json={"budget_egp": 1e6}).status_code == 401
    assert client.post("/api/v1/candidates/recompute").status_code == 401


def test_the_session_endpoint_is_public_and_says_no(client):
    """The SPA asks this on load; an error for "not signed in yet" is noise."""
    body = client.get("/api/v1/auth/session").json()
    assert body == {"authenticated": False}


def test_health_stays_public(client):
    """A readiness probe cannot hold a credential."""
    assert client.get("/health").status_code in (200, 503)


# --- login ------------------------------------------------------------------


def test_login_sets_an_httponly_session_cookie(client):
    response = login(client, "boss", ADMIN_PASSWORD)
    assert response.status_code == 200

    cookie = response.headers["set-cookie"]
    assert "httponly" in cookie.lower()
    assert "samesite=lax" in cookie.lower()
    assert client.cookies.get(SESSION_COOKIE)
    # The CSRF cookie must NOT be HttpOnly - the page has to read it.
    assert client.cookies.get(CSRF_COOKIE)


def test_the_cookie_is_not_the_stored_token(client):
    """A database dump must not yield replayable sessions."""
    login(client, "boss", ADMIN_PASSWORD)
    token = client.cookies.get(SESSION_COOKIE)

    with client.session_scope() as session:
        rows = session.query(SessionRow).all()

    assert len(rows) == 1
    assert rows[0].token_hash != token
    assert token not in rows[0].token_hash


def test_a_wrong_password_and_a_missing_user_are_indistinguishable(client):
    """Anything else turns the login form into an account-enumeration oracle."""
    wrong = client.post("/api/v1/auth/login",
                        json={"username": "boss", "password": "not-the-password"})
    missing = client.post("/api/v1/auth/login",
                          json={"username": "ghost", "password": "not-the-password"})

    assert wrong.status_code == missing.status_code == 401
    assert wrong.json()["detail"] == missing.json()["detail"]


def test_repeated_failures_are_throttled(client):
    for _ in range(service.MAX_FAILURES):
        client.post("/api/v1/auth/login",
                    json={"username": "boss", "password": "wrong-password-here"})

    blocked = client.post("/api/v1/auth/login",
                          json={"username": "boss", "password": ADMIN_PASSWORD})
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) > 0


def test_a_disabled_account_cannot_log_in(client):
    with client.session_scope() as session:
        user = session.query(UserRow).filter_by(username="watcher").one()
        user.is_active = False

    assert login(client, "watcher", VIEWER_PASSWORD).status_code == 401


def test_disabling_an_account_kills_its_live_sessions(client):
    """Otherwise a revoked account keeps working until its cookie expires - a week."""
    login(client, "watcher", VIEWER_PASSWORD)
    assert client.get("/api/v1/meta").status_code == 200

    with client.session_scope() as session:
        user = session.query(UserRow).filter_by(username="watcher").one()
        service.revoke_all_sessions(session, user.id)

    assert client.get("/api/v1/meta").status_code == 401


# --- authorisation ----------------------------------------------------------


def test_a_viewer_can_read_but_not_solve(client):
    login(client, "watcher", VIEWER_PASSWORD)

    assert client.get("/api/v1/meta").status_code == 200
    assert client.post("/api/v1/optimize",
                       json={"budget_egp": 1e6, "persist": False}).status_code == 403


def test_an_analyst_can_solve_but_not_administer(client):
    login(client, "analyst", ANALYST_PASSWORD)

    assert client.post("/api/v1/optimize",
                       json={"budget_egp": 1e6, "persist": False}).status_code == 200
    assert client.get("/api/v1/admin/users").status_code == 403
    assert client.post("/api/v1/candidates/recompute").status_code == 403


def test_an_admin_can_do_everything(client):
    login(client, "boss", ADMIN_PASSWORD)

    assert client.get("/api/v1/meta").status_code == 200
    assert client.post("/api/v1/optimize",
                       json={"budget_egp": 1e6, "persist": False}).status_code == 200
    assert client.get("/api/v1/admin/users").status_code == 200


def test_forbidden_is_403_not_404(client):
    """Hiding an endpoint from someone already authenticated buys nothing.

    They can read the OpenAPI schema, and a 404 makes a permissions problem look like
    a broken deployment - which is an hour of the wrong debugging.
    """
    login(client, "watcher", VIEWER_PASSWORD)
    assert client.get("/api/v1/admin/users").status_code == 403


# --- deny by default --------------------------------------------------------


def iter_api_routes(app):
    """Every API route, including those inside included routers.

    FastAPI 0.141 keeps an included router as a single `_IncludedRouter` entry in
    `app.routes` rather than flattening its children into the list, and that wrapper
    exposes the child routes as `original_router`, not `routes`. Walking only the top
    level reports that the auth and admin endpoints do not exist - exactly the wrong
    answer for a test whose whole job is to notice an unprotected route.
    """
    stack = list(app.routes)
    while stack:
        route = stack.pop()
        nested = getattr(route, "original_router", None)
        children = getattr(nested, "routes", None) or getattr(route, "routes", None)
        if children:
            stack.extend(children)
            continue
        if getattr(route, "path", "").startswith("/api/"):
            yield route


def test_every_api_route_is_authenticated_or_deliberately_public():
    """The test that keeps this true after everyone forgets about it.

    Authentication is enforced by middleware, so a new route is protected the moment
    it exists. This asserts the other half: that nothing has quietly been added to the
    public list.
    """
    api_paths = {route.path for route in iter_api_routes(api_main.app)}
    public = {path for path in api_paths if path in PUBLIC_PATHS}

    assert public == {
        "/api/v1/auth/login",
        "/api/v1/auth/logout",
        "/api/v1/auth/session",
    }, f"unexpected public API routes: {public}"


def test_every_data_route_declares_a_role():
    """A route with no role is one that any signed-in viewer can call.

    Middleware guarantees a session; it cannot know whether an endpoint reads or
    writes. That distinction only exists where the route is declared, so this fails
    when a new one omits it.
    """
    exempt = PUBLIC_PATHS | {
        "/api/v1/auth/password",      # gated by current_principal, by design
        "/api/v1/auth/sessions",      # your own sessions; any authenticated user
    }

    missing = []
    for route in iter_api_routes(api_main.app):
        path = route.path
        if path in exempt:
            continue
        declared = [
            dependency.call for dependency in route.dependant.dependencies
            if hasattr(dependency.call, "__gemp_required_role__")
        ]
        if not declared:
            missing.append(f"{sorted(route.methods)} {path}")

    assert not missing, f"routes without a declared role: {missing}"


# --- CSRF -------------------------------------------------------------------


def test_a_state_change_without_the_csrf_header_is_refused(client):
    login(client, "boss", ADMIN_PASSWORD)
    del client.headers[CSRF_HEADER]

    assert client.post("/api/v1/optimize",
                       json={"budget_egp": 1e6, "persist": False}).status_code == 403


def test_a_wrong_csrf_token_is_refused(client):
    login(client, "boss", ADMIN_PASSWORD)
    client.headers[CSRF_HEADER] = "not-the-token"

    assert client.post("/api/v1/optimize",
                       json={"budget_egp": 1e6, "persist": False}).status_code == 403


def test_reads_do_not_need_a_csrf_token(client):
    login(client, "boss", ADMIN_PASSWORD)
    del client.headers[CSRF_HEADER]

    assert client.get("/api/v1/meta").status_code == 200


# --- sessions ---------------------------------------------------------------


def test_logout_revokes_the_session(client):
    login(client, "boss", ADMIN_PASSWORD)
    assert client.get("/api/v1/meta").status_code == 200

    client.post("/api/v1/auth/logout")
    assert client.get("/api/v1/meta").status_code == 401


def test_a_forged_cookie_is_rejected(client):
    client.cookies.set(SESSION_COOKIE, "a" * 43)
    assert client.get("/api/v1/meta").status_code == 401


def test_an_expired_session_stops_working(client):
    login(client, "boss", ADMIN_PASSWORD)

    with client.session_scope() as session:
        row = session.query(SessionRow).one()
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)

    assert client.get("/api/v1/meta").status_code == 401


def test_an_idle_session_stops_working(client):
    """An absolute lifetime alone leaves an abandoned browser logged in for days."""
    login(client, "boss", ADMIN_PASSWORD)

    with client.session_scope() as session:
        row = session.query(SessionRow).one()
        row.last_seen_at = datetime.now(UTC) - service.IDLE_TIMEOUT - timedelta(minutes=1)

    assert client.get("/api/v1/meta").status_code == 401


def test_login_issues_a_fresh_token_rather_than_adopting_one(client):
    """Session fixation: a cookie planted before login must not survive it."""
    client.cookies.set(SESSION_COOKIE, "planted-by-an-attacker")
    response = login(client, "boss", ADMIN_PASSWORD)

    # Read what the server SET, not what the local jar ended up holding: httpx keys
    # a manually planted cookie under a different domain than the response's, so both
    # can coexist in the jar in a way a real browser would not allow.
    issued = [value for name, value in response.headers.multi_items()
              if name.lower() == "set-cookie" and value.startswith(SESSION_COOKIE)]
    assert issued and "planted-by-an-attacker" not in issued[0]


# --- passwords --------------------------------------------------------------


def test_a_weak_password_is_refused(client):
    login(client, "boss", ADMIN_PASSWORD)
    response = client.post("/api/v1/admin/users", json={
        "username": "newbie", "password": "short", "role": "viewer",
    })
    assert response.status_code == 422


def test_changing_a_password_requires_the_current_one(client):
    login(client, "boss", ADMIN_PASSWORD)
    response = client.post("/api/v1/auth/password", json={
        "current_password": "not-it", "new_password": "a-brand-new-passphrase",
    })
    assert response.status_code == 403


def test_changing_a_password_revokes_other_sessions(client, tmp_path):
    """A password change is what someone does when they think they are compromised."""
    login(client, "boss", ADMIN_PASSWORD)

    with client.session_scope() as session:
        user = session.query(UserRow).filter_by(username="boss").one()
        stolen, _row = service.issue_session(session, user, ip="10.0.0.9")

    response = client.post("/api/v1/auth/password", json={
        "current_password": ADMIN_PASSWORD, "new_password": "a-brand-new-passphrase",
    })
    assert response.status_code == 200

    with client.session_scope() as session:
        assert service.resolve_session(session, stolen) is None


def test_an_admin_issued_password_must_be_changed(client):
    login(client, "boss", ADMIN_PASSWORD)
    created = client.post("/api/v1/admin/users", json={
        "username": "newbie", "password": "a-temporary-passphrase", "role": "analyst",
    })
    assert created.status_code == 201
    assert created.json()["must_change_password"]

    client.post("/api/v1/auth/logout")
    assert login(client, "newbie", "a-temporary-passphrase").status_code == 200

    # Everything is blocked except changing the password.
    assert client.get("/api/v1/meta").status_code == 409
    assert client.post("/api/v1/auth/password", json={
        "current_password": "a-temporary-passphrase",
        "new_password": "a-password-of-my-own",
    }).status_code == 200
    assert client.get("/api/v1/meta").status_code == 200


def test_password_hashes_are_scrypt_and_salted():
    first = passwords.hash_password("the-same-passphrase")
    second = passwords.hash_password("the-same-passphrase")

    assert first.startswith("scrypt$")
    assert first != second, "identical passwords must not produce identical hashes"
    assert passwords.verify("the-same-passphrase", first)
    assert not passwords.verify("the-same-passphrase ", first)


def test_a_corrupt_hash_fails_the_login_rather_than_the_request():
    """A 500 here tells an attacker the account exists and is special."""
    assert not passwords.verify("anything", "")
    assert not passwords.verify("anything", "bcrypt$whatever")
    assert not passwords.verify("anything", "scrypt$n=x,r=y,p=z$zz$zz")


# --- admin ------------------------------------------------------------------


def test_an_admin_cannot_lock_themselves_out(client):
    """Leaving a deployment with no admin is recoverable only with shell access."""
    login(client, "boss", ADMIN_PASSWORD)
    me = [u for u in client.get("/api/v1/admin/users").json()
          if u["username"] == "boss"][0]

    assert client.patch(f"/api/v1/admin/users/{me['id']}",
                        json={"role": "viewer"}).status_code == 409
    assert client.patch(f"/api/v1/admin/users/{me['id']}",
                        json={"is_active": False}).status_code == 409


def test_privileged_actions_are_audited(client):
    login(client, "boss", ADMIN_PASSWORD)
    client.post("/api/v1/admin/users", json={
        "username": "audited", "password": "a-temporary-passphrase", "role": "viewer",
    })

    actions = [row["action"] for row in client.get("/api/v1/admin/audit").json()]
    assert "user.create" in actions
    assert "auth.login" in actions


def test_a_failed_login_is_audited_with_no_user(client):
    client.post("/api/v1/auth/login",
                json={"username": "boss", "password": "wrong-password-here"})
    login(client, "boss", ADMIN_PASSWORD)

    denied = [row for row in client.get("/api/v1/admin/audit").json()
              if row["action"] == "auth.login" and row["outcome"] == "denied"]
    assert denied, "a failed login must leave a trace"


def test_the_audit_log_is_admin_only(client):
    login(client, "analyst", ANALYST_PASSWORD)
    assert client.get("/api/v1/admin/audit").status_code == 403


# --- headers ----------------------------------------------------------------


@pytest.mark.parametrize("header", [
    "Content-Security-Policy",
    "X-Frame-Options",
    "X-Content-Type-Options",
    "Referrer-Policy",
])
def test_security_headers_are_present(client, header):
    assert header in client.get("/api/v1/auth/session").headers


def test_the_csp_allows_nothing_external(client):
    """Also the offline guarantee (F13), enforced by the browser rather than remembered."""
    csp = client.get("/api/v1/auth/session").headers["Content-Security-Policy"]

    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "http://" not in csp and "https://" not in csp


def test_a_stale_session_cookie_does_not_block_logging_in(client):
    """The control must not lock out the person it exists to protect.

    A browser holding an expired or revoked session cookie has no CSRF cookie to go
    with it. If the CSRF check applied to the login endpoint, that user's only route
    back in - logging in again - would be refused, with no way to tell why.
    """
    client.cookies.set(SESSION_COOKIE, "left-over-from-last-week")
    assert login(client, "boss", ADMIN_PASSWORD).status_code == 200


def test_guessing_the_current_password_is_throttled_too(client):
    """The change-password form is the second place a password is guessed.

    Login is throttled; this was not. Somebody who has got hold of a session - a
    shared machine, an unlocked screen - could grind the current password here at
    whatever rate the hash allows, and that secret is the only thing between them
    and permanent ownership of the account. It draws on the same counters as the
    login form, so the two cannot be used to double the budget either.
    """
    assert login(client, "boss", ADMIN_PASSWORD).status_code == 200

    for _ in range(service.MAX_FAILURES):
        refused = client.post("/api/v1/auth/password", json={
            "current_password": "not-the-right-one",
            "new_password": "a-brand-new-long-passphrase",
        })
        assert refused.status_code == 403

    blocked = client.post("/api/v1/auth/password", json={
        "current_password": "not-the-right-one",
        "new_password": "a-brand-new-long-passphrase",
    })
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) > 0

    # And the budget is shared with the login form, not private to this endpoint.
    assert login(client, "boss", ADMIN_PASSWORD).status_code == 429


def test_the_api_schema_is_not_served_to_anonymous_callers(client):
    """`/docs`, `/redoc` and `/openapi.json` are public paths when they are mounted.

    Mounted, they hand anyone who can reach the port the complete endpoint
    inventory, every parameter shape and the role each route demands. That is a
    reconnaissance map and it is worth nothing to the people this is demonstrated
    to, so it is off unless GEMP_API_DOCS says otherwise.
    """
    for path in ("/openapi.json", "/docs", "/redoc"):
        assert client.get(path).status_code == 404, path


def test_the_app_refuses_to_run_as_more_than_one_worker(monkeypatch):
    """The login throttle counters live in process memory.

    Under `--workers 4` they become four independent counters - five failures each,
    twenty attempts before anything is refused - and the control reads as though it
    is doing its job. Nothing about that failure announces itself, so the assumption
    is checked at startup rather than written in a comment.
    """
    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    with pytest.raises(RuntimeError, match="single worker"):
        api_main._refuse_multiple_workers()

    monkeypatch.setenv("WEB_CONCURRENCY", "1")
    api_main._refuse_multiple_workers()   # must not raise

    monkeypatch.delenv("WEB_CONCURRENCY")
    api_main._refuse_multiple_workers()   # nor when it is unset


def test_the_posture_screen_counts_hashes_below_the_current_cost(client):
    """A hash is upgraded on its owner's next successful sign-in, so an account that
    has not signed in since the cost was raised keeps the weaker one - protected at
    the lower factor, and faster to verify than the equal-cost work done for an
    account that does not exist. Neither is visible anywhere unless it is reported.
    """
    assert login(client, "boss", ADMIN_PASSWORD).status_code == 200

    clean = client.get("/api/v1/admin/security").json()
    assert clean["password_hashes_below_current_cost"] == 0
    assert clean["password_hash_cost"] == passwords.DEFAULT_N

    # Write one row back at a weaker cost, as an older deployment would have left it.
    with client.session_scope() as session:
        user = session.query(UserRow).filter_by(username="watcher").one()
        user.password_hash = passwords.hash_password(VIEWER_PASSWORD, n=1024)

    stale = client.get("/api/v1/admin/security").json()
    assert stale["password_hashes_below_current_cost"] == 1
    assert any("below the current one" in w for w in stale["warnings"])
