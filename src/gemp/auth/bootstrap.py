"""Create the first administrator.

    python -m gemp.auth.bootstrap --username ahmed

There is no default account and no default password, and that is the whole point of
this file existing rather than a seed row in a migration. A shipped `admin/admin` is
the single most reliable way for a system to be compromised: it survives every
deployment, it is in the repository for anyone to read, and the person who meant to
change it was busy on the day.

The password is either generated here and printed once, or read from
`GEMP_BOOTSTRAP_PASSWORD` for an automated deployment. It is never taken as a command
line argument - arguments land in shell history and in the process list, where every
other user on the machine can read them.
"""

from __future__ import annotations

import argparse
import logging
import os
import secrets
import sys

from gemp.auth import passwords, service
from gemp.auth.roles import Role
from gemp.db import session_scope

log = logging.getLogger("gemp.auth.bootstrap")

# Four words of entropy from a CSPRNG. Long enough that the generated credential is
# stronger than anything a person would invent under time pressure, short enough to
# read across a room and type once.
GENERATED_PASSWORD_BYTES = 18


def generate_password() -> str:
    return secrets.token_urlsafe(GENERATED_PASSWORD_BYTES)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-5s %(message)s")
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--username", required=True)
    parser.add_argument("--display-name", default="")
    parser.add_argument("--role", default=Role.ADMIN.value,
                        choices=[role.value for role in Role])
    parser.add_argument(
        "--force", action="store_true",
        help="create even when accounts already exist (normally refused)",
    )
    args = parser.parse_args(argv)

    password = os.environ.get("GEMP_BOOTSTRAP_PASSWORD", "")
    generated = not password
    if generated:
        password = generate_password()

    try:
        passwords.validate(password)
    except passwords.WeakPassword as exc:
        print(f"FAIL  {exc}", file=sys.stderr)
        return 1

    try:
        with session_scope() as session:
            existing = service.user_count(session)
            if existing and not args.force:
                # Bootstrapping into a populated system is almost always an accident -
                # a re-run of a deployment script - and quietly minting another admin
                # is how a system acquires an account nobody remembers creating.
                print(
                    f"FAIL  {existing} account(s) already exist. Use the admin UI to "
                    "add users, or pass --force if you are certain.",
                    file=sys.stderr,
                )
                return 1

            user = service.create_user(
                session,
                username=args.username,
                password=password,
                role=args.role,
                display_name=args.display_name,
                # A generated password is a one-time credential. An operator-supplied
                # one is assumed to be theirs already.
                must_change_password=generated,
            )
            service.audit(session, "user.bootstrap", username=user.username,
                          detail={"role": user.role})
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"FAIL  {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(f"\n  created {args.role} account: {user.username}")
    if generated:
        # Printed once, to a terminal, never stored anywhere else.
        print(f"  password: {password}")
        print("\n  Write this down now - it is not recoverable, and it must be "
              "changed at first login.")
    else:
        print("  password: taken from GEMP_BOOTSTRAP_PASSWORD")
        # The environment of a process is readable by anything running as the same
        # user, and often lingers in deployment tooling and shell histories. The
        # generated path above exists because it is strictly safer; say so rather
        # than let convenience be the default without anyone noticing.
        print("  (prefer the generated path next time: an environment variable is "
              "readable by everything running as this user)")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
