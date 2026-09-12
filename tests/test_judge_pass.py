"""The Judge Pass, tested the way a card in the wrong hands would use it.

Every card carries the same code, so the code is only a door. What stands in for a
key is here: a closing time, one pass per address, a ceiling in any 24 hours, an off
switch, a viewer role and nothing above it, and an account that stops working at the
minute the pass closes rather than when its cookie lapses. Each of those is a claim
somebody at the booth will be relying on without knowing it, so each is pinned.

No network. The mail sender is replaced for the flow tests and fed canned responses
for its own; nothing here reaches Resend.
"""

from __future__ import annotations

import io
import json
import os
import re
import urllib.error
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("GEMP_HMAC_KEY", "cd" * 32)
os.environ["GEMP_INGEST_ENABLED"] = "0"

from gemp import mail  # noqa: E402
from gemp.api import main as api_main  # noqa: E402
from gemp.api.deps import get_session  # noqa: E402
from gemp.auth import judge_pass, passwords, service  # noqa: E402
from gemp.auth.service import CSRF_HEADER, SESSION_COOKIE  # noqa: E402
from gemp.config import Settings, get_settings  # noqa: E402
from gemp.db import Base, JudgePassRow, SessionRow, UserRow  # noqa: E402
from gemp.repository import import_portfolio  # noqa: E402

CODE = "booth-code-for-the-test-suite"
ADMIN_PASSWORD = "correct-horse-battery-staple"
JUDGE = "judge@example.test"


def configure(monkeypatch, **overrides) -> None:
    values = {
        "GEMP_JUDGE_PASS_CODE": CODE,
        "GEMP_JUDGE_PASS_UNTIL": (datetime.now(UTC) + timedelta(hours=6)).isoformat(),
        "GEMP_JUDGE_PASS_DAILY_MAX": "90",
        "GEMP_PUBLIC_URL": "https://gemp.example.test",
        "GEMP_RESEND_API_KEY": "re_test",
        # Blank, so a key or a mailbox in a developer's .env cannot change what
        # these test.
        "GEMP_BREVO_API_KEY": "",
        "GEMP_SMTP_HOST": "",
        "GEMP_MAIL_FROM": "GEMP <pass@example.test>",
        "GEMP_MAIL_REPLY_TO": "team@example.test",
    } | overrides
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()


@pytest.fixture
def sent(monkeypatch):
    """Every message a pass tried to send, instead of sending it."""
    outbox: list[dict] = []

    def fake_send(settings, *, to, subject, html, text, idempotency_key=""):
        outbox.append({"to": to, "subject": subject, "html": html, "text": text,
                       "key": idempotency_key})
        return mail.Outcome("sent", "msg_test")

    monkeypatch.setattr(mail, "send", fake_send)
    return outbox


@pytest.fixture
def client(tmp_path, monkeypatch, sent):
    configure(monkeypatch)
    engine = create_engine(f"sqlite:///{tmp_path / 'pass.db'}")
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
    get_settings.cache_clear()


def redeem(client, email: str = JUDGE, *, code: str = CODE, lang: str = "en"):
    response = client.post("/api/v1/pass/redeem",
                           json={"code": code, "email": email, "lang": lang})
    if response.status_code == 200:
        client.headers[CSRF_HEADER] = response.json()["csrf_token"]
    return response


def check(client, code: str = CODE) -> dict:
    return client.post("/api/v1/pass/check", json={"code": code}).json()


def as_admin(client) -> None:
    response = client.post("/api/v1/auth/login",
                           json={"username": "boss", "password": ADMIN_PASSWORD})
    assert response.status_code == 200
    client.headers[CSRF_HEADER] = response.json()["csrf_token"]


def password_from(message: dict) -> str:
    match = re.search(r"^Password: (\S+)$", message["text"], re.M)
    assert match, "the email does not state a password"
    return match.group(1)


def pass_rows(client) -> list[JudgePassRow]:
    with client.session_scope() as session:
        return list(session.execute(select(JudgePassRow)).scalars())


# --- the door -----------------------------------------------------------------


def test_a_wrong_code_learns_nothing_and_creates_nothing(client, sent):
    """Not whether passes exist, not when they close, not who to ask."""
    assert check(client, "not-the-code-at-all") == {
        "state": "invalid", "until": None, "contact": None,
    }
    response = redeem(client, code="not-the-code-at-all")
    assert response.status_code == 404
    assert response.json()["reason"] == "invalid"
    assert pass_rows(client) == []
    assert sent == []


def test_the_right_code_opens_the_page(client):
    body = check(client)
    assert body["state"] == "open"
    assert body["until"]
    assert body["contact"] == "team@example.test"


@pytest.mark.parametrize("override", [
    {"GEMP_JUDGE_PASS_CODE": ""},
    {"GEMP_JUDGE_PASS_CODE": "short-code"},          # guessable, so off
    {"GEMP_JUDGE_PASS_UNTIL": "2026-09-14T23:59:59"},  # no offset, so ambiguous
    {"GEMP_JUDGE_PASS_UNTIL": ""},
    {"GEMP_JUDGE_PASS_UNTIL": "tomorrow evening"},
])
def test_a_half_configured_pass_is_no_pass_at_all(client, monkeypatch, override):
    configure(monkeypatch, **override)
    code = override.get("GEMP_JUDGE_PASS_CODE") or CODE
    assert check(client, code or "x")["state"] == "invalid"
    assert redeem(client, code=code or "x").status_code == 404


def test_a_blank_or_garbled_setting_never_takes_the_application_down(client, monkeypatch):
    """A setting pydantic refuses does not switch a feature off - it makes every
    call to get_settings() raise, and every request return 500, sign-in included.
    Found here first: .env.example shipped the closing time blank."""
    configure(monkeypatch, GEMP_JUDGE_PASS_UNTIL="", GEMP_JUDGE_PASS_DAILY_MAX="")
    assert client.get("/api/v1/auth/session").status_code == 200
    assert client.post("/api/v1/auth/login", json={
        "username": "boss", "password": ADMIN_PASSWORD}).status_code == 200
    assert get_settings().judge_pass_daily_max == 90
    assert get_settings().judge_pass_until is None


# --- what a pass gives ---------------------------------------------------------


def test_a_pass_signs_the_browser_in_as_a_viewer_on_the_spot(client):
    response = redeem(client, "  Judge@Example.TEST ")
    assert response.status_code == 200
    body = response.json()
    assert body["user"]["username"] == JUDGE
    assert body["user"]["role"] == "viewer"
    assert body["must_change_password"] is False
    assert body["pass"]["mail"] == "queued"

    session = client.get("/api/v1/auth/session").json()
    assert session["authenticated"] is True
    assert session["user"]["role"] == "viewer"
    assert session["user"]["access_expires_at"]

    # A viewer reads, and nothing more.
    assert client.get("/api/v1/meta").status_code == 200
    assert client.post("/api/v1/optimize", json={"budget_egp": 1e6}).status_code == 403
    assert client.get("/api/v1/admin/users").status_code == 403
    assert client.get("/api/v1/admin/judge-pass").status_code == 403
    assert client.put("/api/v1/admin/judge-pass", json={"enabled": False}).status_code == 403


def test_the_email_carries_a_password_that_works_on_any_device(client, sent):
    assert redeem(client).status_code == 200
    assert len(sent) == 1
    message = sent[0]
    assert message["to"] == JUDGE
    assert message["key"].startswith("judge-pass-")

    # A second device: no cookie, the password from the email.
    other = TestClient(api_main.app)
    response = other.post("/api/v1/auth/login",
                          json={"username": JUDGE, "password": password_from(message)})
    assert response.status_code == 200
    assert response.json()["must_change_password"] is False

    [row] = pass_rows(client)
    assert row.mail_status == "sent"
    assert row.mail_detail == "msg_test"


def test_the_password_is_never_stored(client, sent):
    redeem(client)
    password = password_from(sent[0])
    with client.session_scope() as session:
        user = session.execute(select(UserRow).where(UserRow.username == JUDGE)).scalar_one()
        assert password not in user.password_hash
        assert passwords.verify(password, user.password_hash)
        row = session.execute(select(JudgePassRow)).scalar_one()
        assert password not in json.dumps({c.name: str(getattr(row, c.name))
                                           for c in row.__table__.columns})


def test_the_email_speaks_the_language_of_the_page(client, sent):
    redeem(client, "one@example.test", lang="ar")
    redeem(TestClient(api_main.app), "two@example.test", lang="en")
    arabic, english = sent

    assert 'dir="rtl"' in arabic["html"] and 'lang="ar"' in arabic["html"]
    assert 'dir="ltr"' in english["html"]
    assert arabic["subject"] != english["subject"]
    for message in sent:
        assert "https://gemp.example.test/#/signin" in message["html"]
        assert "https://gemp.example.test/#/signin" in message["text"]
        # Nothing the mail client would have to fetch - and block.
        assert "<img" not in message["html"]
        # And nothing hidden. Invisible text is one of the oldest spam tells, and a
        # new sender cannot afford any of them.
        assert "display:none" not in message["html"]


def test_the_email_escapes_what_it_was_given(client, sent, monkeypatch):
    message = judge_pass.compose(
        "en", email="a<b@example.test", password="x&y",
        until=datetime(2026, 9, 14, 23, 59, 59, tzinfo=UTC), sign_in_url="",
    )
    assert "a<b@" not in message.html and "a&lt;b@" in message.html
    assert "x&amp;y" in message.html
    # No address to sign in at means no button pointing nowhere.
    assert "<a href" not in message.html


# --- the rails -------------------------------------------------------------------


def test_one_pass_per_address_whatever_the_case(client, sent):
    assert redeem(client).status_code == 200
    again = redeem(TestClient(api_main.app), JUDGE.upper())
    assert again.status_code == 409
    assert again.json()["reason"] == "exists"
    assert len(pass_rows(client)) == 1
    assert len(sent) == 1, "a refused pass must not send anything"


def test_an_existing_account_is_not_a_second_way_in(client, sent):
    with client.session_scope() as session:
        service.create_user(session, username="owner@example.test",
                            password="a-long-enough-passphrase", role="admin")
    response = redeem(client, "owner@example.test")
    assert response.status_code == 409
    assert client.get("/api/v1/auth/session").json() == {"authenticated": False}
    assert sent == []


@pytest.mark.parametrize("email", ["not-an-email", "a@b", "x" * 60 + "@example.test", ""])
def test_a_malformed_address_is_refused_before_anything_is_created(client, sent, email):
    response = redeem(client, email)
    assert response.status_code == 422
    assert pass_rows(client) == []
    assert sent == []


def test_a_closed_event_issues_nothing_and_says_so(client, monkeypatch, sent):
    configure(monkeypatch,
              GEMP_JUDGE_PASS_UNTIL=(datetime.now(UTC) - timedelta(minutes=1)).isoformat())
    assert check(client)["state"] == "closed"
    response = redeem(client)
    assert response.status_code == 410
    assert response.json()["reason"] == "closed"
    assert sent == []


def test_the_daily_ceiling_stops_a_leaked_link(client, monkeypatch, sent):
    configure(monkeypatch, GEMP_JUDGE_PASS_DAILY_MAX="2")
    assert redeem(TestClient(api_main.app), "a@example.test").status_code == 200
    assert redeem(TestClient(api_main.app), "b@example.test").status_code == 200

    third = redeem(TestClient(api_main.app), "c@example.test")
    assert third.status_code == 429
    assert third.json()["reason"] == "full"
    assert check(client)["state"] == "full"
    assert len(sent) == 2


def test_the_off_switch_pauses_new_passes_and_nobody_else(client, sent):
    judge = TestClient(api_main.app)
    assert redeem(judge, "early@example.test").status_code == 200

    as_admin(client)
    body = client.put("/api/v1/admin/judge-pass", json={"enabled": False}).json()
    assert body["state"] == "paused" and body["enabled"] is False
    assert check(client)["state"] == "paused"

    refused = redeem(TestClient(api_main.app), "late@example.test")
    assert refused.status_code == 403
    assert refused.json()["reason"] == "paused"
    # Pausing stops new cards working. It does not sign anybody out.
    assert judge.get("/api/v1/meta").status_code == 200

    assert client.put("/api/v1/admin/judge-pass",
                      json={"enabled": True}).json()["state"] == "open"


def test_the_administrator_sees_who_holds_a_pass(client, sent):
    redeem(TestClient(api_main.app))
    as_admin(client)
    body = client.get("/api/v1/admin/judge-pass").json()

    assert body["state"] == "open"
    assert body["total"] == 1 and body["issued_24h"] == 1
    assert body["mail_configured"] is True
    assert body["link"] == f"https://gemp.example.test/#/pass/{CODE}"
    [holder] = body["passes"]
    assert holder["email"] == JUDGE
    assert holder["mail_status"] == "sent"


# --- the account ends at the minute the pass does ---------------------------------


def test_a_closing_time_in_cairo_survives_the_round_trip(client, monkeypatch):
    """Written with an offset, read back as the same instant.

    SQLite keeps a datetime's wall-clock digits and drops its offset, and a naive
    value is read back as UTC - so 23:59+03:00 went in and 23:59 UTC came out, and
    the shell counted down to a closing time three hours late. The pass response
    had it right and the next page load had it wrong, which is why it survived a
    first look."""
    cairo = timezone(timedelta(hours=3))
    until = (datetime.now(UTC) + timedelta(hours=5)).astimezone(cairo).replace(microsecond=0)
    configure(monkeypatch, GEMP_JUDGE_PASS_UNTIL=until.isoformat())

    assert redeem(client).status_code == 200
    reported = client.get("/api/v1/auth/session").json()["user"]["access_expires_at"]
    assert datetime.fromisoformat(reported) == until

    with client.session_scope() as session:
        user = session.execute(select(UserRow).where(UserRow.username == JUDGE)).scalar_one()
        assert not service.access_expired(user)
        assert service.access_expired(user, until + timedelta(seconds=1))


def test_the_session_never_outlives_the_pass(client):
    response = redeem(client)
    until = datetime.fromisoformat(response.json()["pass"]["until"])

    with client.session_scope() as session:
        row = session.execute(select(SessionRow)).scalars().all()[-1]
        expires = row.expires_at.replace(tzinfo=row.expires_at.tzinfo or UTC)
    assert expires <= until

    cookie = [h for h in response.headers.get_list("set-cookie") if SESSION_COOKIE in h][0]
    max_age = int(re.search(r"max-age=(\d+)", cookie, re.I).group(1))
    assert max_age <= (until - datetime.now(UTC)).total_seconds() + 5


def test_a_pass_account_stops_at_the_closing_minute(client, sent):
    assert redeem(client).status_code == 200
    password = password_from(sent[0])
    assert client.get("/api/v1/meta").status_code == 200

    with client.session_scope() as session:
        user = session.execute(select(UserRow).where(UserRow.username == JUDGE)).scalar_one()
        user.access_expires_at = datetime.now(UTC) - timedelta(seconds=1)

    # The open session dies on its next request, not when its cookie lapses...
    assert client.get("/api/v1/meta").status_code == 401

    # ...and the password stops working, with the same answer as a wrong one.
    service.reset_throttle()
    response = TestClient(api_main.app).post(
        "/api/v1/auth/login", json={"username": JUDGE, "password": password})
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid username or password"


def test_no_sender_still_signs_the_judge_in(client, monkeypatch, sent):
    configure(monkeypatch, GEMP_RESEND_API_KEY="")
    response = redeem(client)
    assert response.status_code == 200
    assert response.json()["pass"]["mail"] == "off"
    assert client.get("/api/v1/meta").status_code == 200
    assert sent == []
    [row] = pass_rows(client)
    assert row.mail_status == "off"


def test_a_failed_send_is_recorded_rather_than_raised(client, monkeypatch):
    monkeypatch.setattr(mail, "send", lambda *a, **k: mail.Outcome(
        "failed", "HTTP 403: The example.test domain is not verified"))
    assert redeem(client).status_code == 200
    [row] = pass_rows(client)
    assert row.mail_status == "failed"
    assert "not verified" in row.mail_detail


def test_generated_passwords_meet_the_policy_and_do_not_repeat():
    generated = {judge_pass.generate_password() for _ in range(60)}
    assert len(generated) >= 55
    for password in generated:
        passwords.validate(password)          # raises if it fails policy
        assert re.fullmatch(r"[a-z]+-[a-z]+-[a-z]+-\d\d", password)


def test_the_card_link_keeps_the_code_out_of_every_server_log(client):
    """The code rides in the fragment, which a browser never sends to a server."""
    link = judge_pass.link(get_settings())
    assert link.split("#", 1)[1] == f"/pass/{CODE}"


# --- the sender itself, against canned responses ------------------------------------


def _settings(**overrides) -> Settings:
    values = {"hmac_key": "cd" * 32, "resend_api_key": "re_live_looking_key",
              "brevo_api_key": "",
              "mail_from": "GEMP <pass@example.test>",
              "mail_reply_to": "team@example.test"} | overrides
    return Settings(**values)


class _Reply:
    def __init__(self, body: bytes):
        self.body = body

    def read(self) -> bytes:
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_the_sender_speaks_brevos_api_when_its_key_is_set(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["request"] = request
        return _Reply(b'{"messageId": "<202609121200.1@smtp-relay.mailin.fr>"}')

    monkeypatch.setattr(mail.urllib.request, "urlopen", fake_urlopen)
    settings = _settings(resend_api_key="", brevo_api_key="brevo-test-key",
                         mail_from="GEMP - Team Ecoforge <pass@example.test>",
                         mail_reply_to="Team <team@example.test>")
    outcome = mail.send(settings, to=JUDGE, subject="s", html="<p>h</p>", text="h")

    assert outcome.status == "sent" and "mailin" in outcome.detail
    request = captured["request"]
    assert request.full_url == mail.BREVO_URL == "https://api.brevo.com/v3/smtp/email"
    assert request.get_header("Api-key") == "brevo-test-key"
    assert request.get_header("Authorization") is None
    body = json.loads(request.data)
    # Brevo takes the sender as an object, so the display name is split off.
    assert body["sender"] == {"name": "GEMP - Team Ecoforge", "email": "pass@example.test"}
    assert body["to"] == [{"email": JUDGE}]
    assert body["replyTo"] == {"email": "team@example.test"}
    assert body["htmlContent"] == "<p>h</p>" and body["textContent"] == "h"


def test_brevo_wins_when_both_keys_are_set(monkeypatch):
    """Switching provider is adding one key, not remembering to blank the other."""
    seen = []
    monkeypatch.setattr(mail.urllib.request, "urlopen",
                        lambda request, timeout: seen.append(request.full_url) or _Reply(b"{}"))
    mail.send(_settings(brevo_api_key="brevo-test-key"), to=JUDGE, subject="s",
              html="h", text="h")
    assert seen == [mail.BREVO_URL]
    assert mail.configured(_settings(resend_api_key="", brevo_api_key="brevo-test-key"))


def test_a_blocked_server_address_is_named_on_the_admin_screen(monkeypatch):
    """Brevo refuses API calls from an address it has not authorised, and says so
    in `message`. That sentence is what the administrator has to act on."""
    def refuse(request, timeout):
        raise urllib.error.HTTPError(
            mail.BREVO_URL, 401, "Unauthorized", None,
            io.BytesIO(b'{"code": "unauthorized", "message": "unauthorized: IP not authorized"}'))

    monkeypatch.setattr(mail.urllib.request, "urlopen", refuse)
    outcome = mail.send(_settings(brevo_api_key="brevo-test-key"), to=JUDGE, subject="s",
                        html="h", text="h")
    assert outcome.status == "failed"
    assert "IP not authorized" in outcome.detail
    assert "brevo-test-key" not in outcome.detail


def test_the_sender_speaks_resends_api(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return _Reply(b'{"id": "4ef9a417"}')

    monkeypatch.setattr(mail.urllib.request, "urlopen", fake_urlopen)
    outcome = mail.send(_settings(), to=JUDGE, subject="s", html="<p>h</p>", text="h",
                        idempotency_key="judge-pass-1")

    assert outcome == mail.Outcome("sent", "4ef9a417")
    request = captured["request"]
    assert request.full_url == mail.RESEND_URL == "https://api.resend.com/emails"
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == "Bearer re_live_looking_key"
    assert request.get_header("Idempotency-key") == "judge-pass-1"
    body = json.loads(request.data)
    assert body["to"] == [JUDGE]
    assert body["reply_to"] == "team@example.test"
    assert body["html"] == "<p>h</p>" and body["text"] == "h"
    assert captured["timeout"] == mail.TIMEOUT_S


def test_a_refusal_comes_back_in_the_providers_words_and_without_the_key(monkeypatch):
    def refuse(request, timeout):
        raise urllib.error.HTTPError(
            mail.RESEND_URL, 403, "Forbidden", None,
            io.BytesIO(b'{"message": "The example.test domain is not verified"}'))

    monkeypatch.setattr(mail.urllib.request, "urlopen", refuse)
    outcome = mail.send(_settings(), to=JUDGE, subject="s", html="h", text="h")
    assert outcome.status == "failed"
    assert "not verified" in outcome.detail
    assert "re_live_looking_key" not in outcome.detail


def test_an_unreachable_provider_is_an_outcome_not_an_exception(monkeypatch):
    def unreachable(request, timeout):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(mail.urllib.request, "urlopen", unreachable)
    assert mail.send(_settings(), to=JUDGE, subject="s", html="h", text="h").status == "failed"


def test_no_key_sends_nothing_at_all(monkeypatch):
    def must_not_run(request, timeout):  # pragma: no cover - the assertion is that it is not
        raise AssertionError("an unconfigured sender reached the network")

    monkeypatch.setattr(mail.urllib.request, "urlopen", must_not_run)
    outcome = mail.send(_settings(resend_api_key=""), to=JUDGE, subject="s", html="h",
                        text="h")
    assert outcome.status == "off"


# --- a Gmail mailbox over SMTP ---------------------------------------------------------


class _FakeSMTP:
    """Records the conversation instead of having it."""

    instances: list = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port = host, port
        self.calls: list = []
        self.sent: list = []
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.calls.append("quit")
        return False

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def send_message(self, message):
        self.sent.append(message)


def _gmail(**overrides) -> Settings:
    return _settings(**{
        "resend_api_key": "", "brevo_api_key": "",
        "smtp_host": "smtp.gmail.com", "smtp_port": 587,
        "smtp_user": "gemp.pass@gmail.com", "smtp_password": "app-password-for-tests",
        "mail_from": "GEMP - Team Ecoforge <gemp.pass@gmail.com>",
    } | overrides)


def test_a_gmail_mailbox_is_spoken_to_over_starttls(monkeypatch):
    _FakeSMTP.instances.clear()
    monkeypatch.setattr(mail.smtplib, "SMTP", _FakeSMTP)
    outcome = mail.send(_gmail(), to=JUDGE, subject="بطاقة المحكّم", html="<p>h</p>",
                        text="h")

    assert outcome.status == "sent" and outcome.detail == "accepted by smtp.gmail.com"
    [client] = _FakeSMTP.instances
    assert (client.host, client.port) == ("smtp.gmail.com", 587)
    # TLS first, then the login: the password never crosses in plain text.
    assert client.calls[:2] == ["starttls",
                                ("login", "gemp.pass@gmail.com", "app-password-for-tests")]
    [message] = client.sent
    # Gmail's own server stamps a genuine Message-ID; one minted here would claim
    # @gmail.com from a machine that is not Gmail's.
    assert message["Message-ID"] is None
    assert message["To"] == JUDGE
    assert message["Reply-To"] == "team@example.test"
    assert "gemp.pass@gmail.com" in message["From"]
    assert str(message["Subject"]) == "بطاقة المحكّم"
    assert [p.get_content_type() for p in message.iter_parts()] == ["text/plain", "text/html"]


def test_port_465_is_tls_from_the_first_byte(monkeypatch):
    _FakeSMTP.instances.clear()
    monkeypatch.setattr(mail.smtplib, "SMTP_SSL", _FakeSMTP)
    monkeypatch.setattr(mail.smtplib, "SMTP",
                        lambda *a, **k: pytest.fail("plain SMTP opened on port 465"))
    assert mail.send(_gmail(smtp_port=465), to=JUDGE, subject="s", html="h",
                     text="h").status == "sent"
    assert "starttls" not in _FakeSMTP.instances[0].calls


def test_a_refused_gmail_login_says_why_without_the_password(monkeypatch):
    """Gmail's sentence names the missed step - no app password, or a wrong one."""
    class Refusing(_FakeSMTP):
        def login(self, user, password):
            raise mail.smtplib.SMTPAuthenticationError(
                534, b"5.7.9 Application-specific password required.")

    monkeypatch.setattr(mail.smtplib, "SMTP", Refusing)
    outcome = mail.send(_gmail(), to=JUDGE, subject="s", html="h", text="h")
    assert outcome.status == "failed"
    assert "Application-specific password required" in outcome.detail
    assert "app-password-for-tests" not in outcome.detail


def test_smtp_is_the_fallback_and_the_mailbox_is_its_default_sender():
    assert mail.provider(_gmail()) == "smtp"
    assert mail.provider(_gmail(brevo_api_key="brevo-test-key")) == "brevo"
    assert mail.configured(_gmail(mail_from=""))
    assert mail.sender(_gmail(mail_from="")) == "gemp.pass@gmail.com"
    assert not mail.configured(_gmail(smtp_password=""))


def test_a_blank_smtp_port_is_the_submission_port_not_an_outage():
    assert _settings(smtp_port="").smtp_port == 587
    assert _settings(smtp_port="five-eight-seven").smtp_port == 587
