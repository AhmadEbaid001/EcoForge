"""Generate the Mosquitto password file from the credentials in .env.

Mosquitto stores PBKDF2-SHA512 hashes in its own format, so the file is produced by
`mosquitto_passwd` from the official image rather than hand-rolled here. Needs Docker
running; nothing else in the setup does.

    python scripts/setup_mqtt_auth.py
"""

from __future__ import annotations

import argparse
import os
import subprocess  # nosec B404
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
PASSWD_PATH = ROOT / "infra" / "mosquitto" / "passwd"

IMAGE = "eclipse-mosquitto:2.0"


def read_env() -> dict[str, str]:
    if not ENV_PATH.exists():
        raise FileNotFoundError(f"{ENV_PATH} not found - run scripts/setup_env.py first")
    values = {}
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    if PASSWD_PATH.exists() and not args.force:
        print(f"{PASSWD_PATH} already exists. Use --force to regenerate.")
        return 1

    try:
        env = read_env()
    except FileNotFoundError as exc:
        print(f"FAIL  {exc}", file=sys.stderr)
        return 1

    user = env.get("GEMP_MQTT_USER", "gemp")
    password = env.get("GEMP_MQTT_PASSWORD", "")
    if not password:
        print("FAIL  GEMP_MQTT_PASSWORD is empty in .env", file=sys.stderr)
        return 1

    PASSWD_PATH.parent.mkdir(parents=True, exist_ok=True)
    PASSWD_PATH.write_text("", encoding="utf-8")

    mount = PASSWD_PATH.parent.as_posix()
    command = [
        "docker", "run", "--rm",
        "-v", f"{mount}:/work",
        IMAGE,
        "mosquitto_passwd", "-b", "/work/passwd", user, password,
    ]

    # An argument list, never a shell string, so the credential cannot be broken
    # out of its argv slot however it is punctuated.
    result = subprocess.run(command, capture_output=True, text=True,  # nosec B603
                            env={**os.environ, "MSYS_NO_PATHCONV": "1"})
    if result.returncode != 0:
        print(f"FAIL  mosquitto_passwd exited {result.returncode}", file=sys.stderr)
        print(result.stderr.strip(), file=sys.stderr)
        print("      is Docker Desktop running?", file=sys.stderr)
        PASSWD_PATH.unlink(missing_ok=True)
        return 1

    print(f"wrote {PASSWD_PATH} for user {user!r}")
    print("next: docker compose up -d")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
