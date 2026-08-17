"""Password hashing, from the standard library.

scrypt rather than bcrypt or argon2 for one reason worth stating plainly: it is in
`hashlib`. A security-critical primitive that arrives with no new dependency is one
that cannot fall out of date because nobody ran `pip install -U`, cannot be shipped in
a stale wheel, and cannot disagree with itself across the container and a laptop. It
is memory-hard, which is the property that matters against GPU cracking, and it is
what NIST SP 800-63B calls acceptable for a memorized-secret verifier.

Parameters are stored PER ROW rather than as a constant. Hardware gets faster, and a
deployment that hardcodes its cost factor can never raise it without invalidating
every password it holds. Storing them means an old hash stays verifiable while new
ones are written at the higher cost, and `needs_rehash` says when to upgrade one
during a login that already has the plaintext in hand.

The format is a single self-describing string, so the column is text and nothing has
to be migrated when the parameters move:

    scrypt$n=16384,r=8,p=1$<salt-hex>$<hash-hex>
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

# Roughly 16 MB of memory and ~100 ms on the container's CPU. Chosen to be
# uncomfortable for an attacker with a GPU farm and unnoticeable to someone logging
# in. Raising N is the lever; it doubles both cost and memory each step.
DEFAULT_N = 16384
DEFAULT_R = 8
DEFAULT_P = 1

SALT_BYTES = 16
KEY_BYTES = 32

# NIST SP 800-63B: length is what matters, composition rules are theatre that push
# people toward `Password1!`. Long minimum, no character-class requirements, no
# maximum short enough to block a passphrase, and no periodic expiry.
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 1024

# A tiny deny list. Not a substitute for a breach-corpus check, which needs a data
# file nobody will keep current here - it only catches the handful that would
# otherwise show up in a demonstration account.
OBVIOUS_PASSWORDS = frozenset({
    "password", "password123", "passw0rd123", "administrator", "changeme123",
    "letmein12345", "qwertyuiop12", "123456789012", "gempgempgemp",
})


class WeakPassword(ValueError):
    """Raised when a password fails policy. The message is shown to the user."""


def validate(password: str) -> None:
    """Policy check. Raises `WeakPassword` with a message worth reading."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise WeakPassword(
            f"password must be at least {MIN_PASSWORD_LENGTH} characters; "
            "a passphrase of a few words is easier to remember and harder to guess"
        )
    if len(password) > MAX_PASSWORD_LENGTH:
        raise WeakPassword(f"password must be at most {MAX_PASSWORD_LENGTH} characters")
    if password.lower() in OBVIOUS_PASSWORDS:
        raise WeakPassword("that password is one of the first an attacker would try")


def hash_password(
    password: str, *, n: int = DEFAULT_N, r: int = DEFAULT_R, p: int = DEFAULT_P
) -> str:
    """Hash a password into its self-describing storage form."""
    salt = secrets.token_bytes(SALT_BYTES)
    derived = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=KEY_BYTES,
        maxmem=_maxmem(n, r, p),
    )
    return f"scrypt$n={n},r={r},p={p}${salt.hex()}${derived.hex()}"


def verify(password: str, stored: str) -> bool:
    """Check a password against a stored hash. False on anything malformed.

    Never raises for a bad stored value. A corrupted row must fail the login, not
    return a 500 that tells an attacker the account exists and something is wrong
    with it specifically.
    """
    try:
        scheme, params, salt_hex, expected_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        n, r, p = _parse_params(params)
        derived = hashlib.scrypt(
            password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
            n=n, r=r, p=p, dklen=len(bytes.fromhex(expected_hex)),
            maxmem=_maxmem(n, r, p),
        )
    except (ValueError, TypeError, MemoryError):
        return False

    # Constant time. A timing difference here leaks how much of the hash matched,
    # which is enough to reconstruct it byte by byte given enough attempts.
    return hmac.compare_digest(derived, bytes.fromhex(expected_hex))


def needs_rehash(stored: str, *, n: int = DEFAULT_N) -> bool:
    """True when a stored hash was made with weaker parameters than the current ones.

    Called on successful login, where the plaintext is in hand and re-hashing costs
    one more scrypt evaluation. It is the only moment an upgrade is possible without
    asking the user to change anything.
    """
    try:
        _scheme, params, _salt, _hash = stored.split("$")
        current_n, _r, _p = _parse_params(params)
    except ValueError:
        return True
    return current_n < n


def _parse_params(params: str) -> tuple[int, int, int]:
    values = dict(part.split("=") for part in params.split(","))
    return int(values["n"]), int(values["r"]), int(values["p"])


def _maxmem(n: int, r: int, p: int) -> int:
    """OpenSSL's default memory ceiling rejects our own parameters, so state one.

    scrypt needs about 128 * N * r bytes. The default limit inside OpenSSL is 32 MB,
    which N=16384 sits close enough to that a future increase would start failing with
    a memory error rather than a clear message. Headroom is cheap here.
    """
    return 256 * n * r * p + (1 << 20)
