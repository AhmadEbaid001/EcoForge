"""The one command that mints a credential, and the ways it could mint a bad one.

`gemp.auth.bootstrap` had no test at all: 0% coverage on the module that decides what
the first administrator's password is and whether a second administrator can be
created by accident. Everything it does is a decision somebody could get wrong once
and never notice, which is the definition of something to pin.

Written the way the module argues rather than the way it is structured. Its docstring
makes four promises - no default credential, no password on the command line, a
generated password printed exactly once, and a refusal to bootstrap into a system that
already has accounts - and each of those is a test below.
"""

from __future__ import annotations

import os
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("GEMP_HMAC_KEY", "cd" * 32)
os.environ["GEMP_INGEST_ENABLED"] = "0"

from gemp.auth import bootstrap, passwords, service  # noqa: E402
from gemp.db import AuditRow, Base, UserRow  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A throwaway database, wired in where the module reaches for one.

    `bootstrap` imports `session_scope` by name at module load, so patching
    `gemp.db.session_scope` would not reach it. Patch the module's own reference.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'bootstrap.db'}")
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

    monkeypatch.setattr(bootstrap, "session_scope", scope)
    monkeypatch.delenv("GEMP_BOOTSTRAP_PASSWORD", raising=False)
    return scope


# --- the generated credential ----------------------------------------------


def test_the_generated_password_is_stronger_than_one_a_person_would_invent():
    """A credential minted under time pressure is the one that survives for years.

    Three properties, because any one of them alone is satisfiable by a bad answer:
    it must pass the project's own strength rule, it must not repeat, and it must
    carry the entropy the module claims rather than merely look long.
    """
    first = bootstrap.generate_password()
    second = bootstrap.generate_password()

    passwords.validate(first)          # raises WeakPassword if it does not hold
    assert first != second
    # token_urlsafe(18) is 24 characters of base64url; anything materially shorter
    # means GENERATED_PASSWORD_BYTES was lowered without anyone re-reading the
    # comment above it.
    assert len(first) >= 24


def test_a_generated_password_is_printed_once_and_never_returned(db, capsys):
    """It is not recoverable, so the single print IS the delivery mechanism."""
    assert bootstrap.main(["--username", "ahmed"]) == 0

    out = capsys.readouterr().out
    printed = [line for line in out.splitlines() if line.strip().startswith("password:")]
    assert len(printed) == 1, out
    secret = printed[0].split("password:", 1)[1].strip()

    assert out.count(secret) == 1
    with db() as session:
        stored = session.query(UserRow).one()
        # Printed, hashed, and nowhere else: the row must not carry the plaintext.
        assert secret not in str(stored.password_hash)
        assert passwords.verify(secret, stored.password_hash)


def test_a_supplied_password_is_never_echoed(db, capsys, monkeypatch):
    """An operator-supplied secret is already theirs; reprinting it only spreads it."""
    monkeypatch.setenv("GEMP_BOOTSTRAP_PASSWORD", "correct-horse-battery-staple-9")

    assert bootstrap.main(["--username", "ahmed"]) == 0

    out = capsys.readouterr().out
    assert "correct-horse-battery-staple-9" not in out
    assert "GEMP_BOOTSTRAP_PASSWORD" in out


def test_the_password_cannot_be_passed_as_an_argument():
    """Arguments land in shell history and in the process list.

    The absence of the flag is the security property, so it is worth a test: adding
    `--password` for convenience would be a one-line change that nothing else notices.
    """
    with pytest.raises(SystemExit) as exit_info:
        bootstrap.main(["--username", "ahmed", "--password", "hunter2"])
    assert exit_info.value.code == 2


# --- refusing to bootstrap twice --------------------------------------------


def test_bootstrapping_a_populated_system_is_refused(db, capsys):
    """A re-run deployment script is the usual cause, and a silent second admin the
    usual result: an account nobody remembers creating, with nobody's name on it."""
    assert bootstrap.main(["--username", "first"]) == 0
    capsys.readouterr()

    assert bootstrap.main(["--username", "second"]) == 1

    err = capsys.readouterr().err
    assert "already exist" in err
    with db() as session:
        assert [u.username for u in session.query(UserRow).all()] == ["first"]


def test_force_is_the_only_way_past_that_refusal(db, capsys):
    assert bootstrap.main(["--username", "first"]) == 0
    capsys.readouterr()

    assert bootstrap.main(["--username", "second", "--force"]) == 0

    with db() as session:
        assert session.query(UserRow).count() == 2


# --- refusing a weak credential ---------------------------------------------


def test_a_weak_supplied_password_is_refused_before_the_database_is_opened(
    db, capsys, monkeypatch
):
    """Order matters. Validating after the insert would leave a real account behind
    whose password the operator was then told was unacceptable."""
    monkeypatch.setenv("GEMP_BOOTSTRAP_PASSWORD", "short")

    opened = False

    @contextmanager
    def tripwire():
        nonlocal opened
        opened = True
        yield None

    monkeypatch.setattr(bootstrap, "session_scope", tripwire)

    assert bootstrap.main(["--username", "ahmed"]) == 1
    assert not opened, "the password was validated after the database was opened"
    assert "FAIL" in capsys.readouterr().err


# --- what the account comes out as ------------------------------------------


def test_a_generated_password_must_be_changed_and_a_supplied_one_need_not_be(
    db, monkeypatch
):
    """The distinction is the point of generating one: it is a one-time credential,
    and leaving it usable indefinitely turns it into a shipped default."""
    assert bootstrap.main(["--username", "generated"]) == 0
    with db() as session:
        assert session.query(UserRow).one().must_change_password is True

    monkeypatch.setenv("GEMP_BOOTSTRAP_PASSWORD", "correct-horse-battery-staple-9")
    assert bootstrap.main(["--username", "supplied", "--force"]) == 0
    with db() as session:
        supplied = session.query(UserRow).filter_by(username="supplied").one()
        assert supplied.must_change_password is False


def test_the_account_is_an_administrator_unless_told_otherwise(db):
    assert bootstrap.main(["--username", "ahmed"]) == 0
    with db() as session:
        assert session.query(UserRow).one().role == "admin"

    assert bootstrap.main(["--username", "reader", "--role", "viewer", "--force"]) == 0
    with db() as session:
        reader = session.query(UserRow).filter_by(username="reader").one()
        assert reader.role == "viewer"


def test_creating_the_first_administrator_is_audited(db):
    """The one account that predates every other account is the one whose creation
    most needs a record, because no signed-in principal authorised it."""
    assert bootstrap.main(["--username", "ahmed"]) == 0

    with db() as session:
        actions = [row.action for row in session.query(AuditRow).all()]
    assert "user.bootstrap" in actions


# --- failing without a traceback --------------------------------------------


def test_a_database_failure_reports_and_exits_rather_than_raising(db, capsys, monkeypatch):
    """This runs on a host, by hand, at an awkward hour. A traceback is a worse
    answer than one line saying what went wrong."""
    def explode(*_args, **_kwargs):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(service, "user_count", explode)

    assert bootstrap.main(["--username", "ahmed"]) == 1
    err = capsys.readouterr().err
    assert "RuntimeError" in err and "connection refused" in err
