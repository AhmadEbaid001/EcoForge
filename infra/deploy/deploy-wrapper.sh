#!/usr/bin/env bash
#
# The forced command behind the deploy key. Everything that key can do, it does
# through here.
#
# SSH hands us the requested command in SSH_ORIGINAL_COMMAND. We do not run it. We
# read one argument out of it - the image reference - validate it hard, and call
# deploy.sh ourselves. That difference is the point: with `command=` alone the key
# still runs whatever the caller asked for, and a key that runs arbitrary commands
# on a host in the `docker` group is a root shell with extra steps.
#
# Install per infra/deploy/authorized_keys.example.

set -euo pipefail

APP_DIR="${GEMP_APP_DIR:-/srv/gemp}"
REGISTRY_PREFIX="${GEMP_REGISTRY_PREFIX:-ghcr.io/}"

deny() { printf 'refused: %s\n' "$*" >&2; exit 126; }

REQUEST="${SSH_ORIGINAL_COMMAND:-}"
[ -n "${REQUEST}" ] || deny "this key runs deployments and nothing else"

# Split the request into fields. NOT `${REQUEST##* }` - that takes the LAST token,
# and the workflow sends `deploy.sh <ref> <sha>`, so the last token is the commit.
# Every deploy was refused as "image is not from ghcr.io/" before this, and the sha
# was never forwarded either, so deploy.sh could not sync the checkout it needs for
# the compose file and the nginx config.
#
# `set -f` first: the fields are unquoted on purpose so the shell splits them, and
# without it a `*` in the request would be expanded against the filesystem.
set -f
# shellcheck disable=SC2086
set -- ${REQUEST}
set +f

SCRIPT="${1:-}"
REF="${2:-}"
SHA="${3:-}"

[ "$#" -le 3 ] || deny "too many arguments"

# reseed.sh is the one request that is not about an image. It takes a number of
# months and nothing else, so it is validated and dispatched here rather than
# falling through the image-reference checks below, which it would never pass.
#
# It is in this file at all because the alternative is worse: the operation needs
# `docker compose run` on the host, and the only other way to reach that is an
# interactive shell for the deploy key. A fixed vocabulary of three scripts, each
# with a validated argument, is the smaller privilege.
if [ "${SCRIPT##*/}" = "reseed.sh" ]; then
  MONTHS="${REF:-6}"
  case "${MONTHS}" in
    ''|*[!0-9]*) deny "reseed takes a whole number of months, got ${MONTHS}" ;;
  esac
  [ "${MONTHS}" -ge 1 ] && [ "${MONTHS}" -le 120 ] || deny "reseed months out of range: ${MONTHS}"
  exec "${APP_DIR}/infra/deploy/reseed.sh" "${MONTHS}"
fi

[ -n "${REF}" ] || deny "no image reference given"

# The commit is optional, and when present it is a hex object name and nothing else.
if [ -n "${SHA}" ] && ! printf '%s' "${SHA}" | grep -Eq '^[0-9a-f]{7,40}$'; then
  deny "not a commit: ${SHA}"
fi

# No shell metacharacters survive this: the value is never passed to anything that
# would interpret them, and anything outside the character class is refused rather
# than escaped.

case "${REF}" in
  *[\;\|\&\$\`\(\)\<\>\'\"\\]*) deny "image reference contains shell metacharacters" ;;
esac

if ! printf '%s' "${REF}" | grep -Eq '^[a-z0-9.:/_-]+(@sha256:[a-f0-9]{64}|:[a-zA-Z0-9._-]+)$'; then
  deny "not an image reference: ${REF}"
fi

case "${REF}" in
  "${REGISTRY_PREFIX}"*) : ;;
  *) deny "image is not from ${REGISTRY_PREFIX}" ;;
esac

# Matched on the script FIELD, not anywhere in the request: `*deploy.sh*` would also
# match an image tag that happened to contain the string.
#
# stdin is inherited by the exec, which is how the registry token reaches deploy.sh
# without ever being an argument.
case "${SCRIPT}" in
  */deploy.sh)   exec "${APP_DIR}/infra/deploy/deploy.sh" "${REF}" "${SHA}" ;;
  */rollback.sh) exec "${APP_DIR}/infra/deploy/rollback.sh" "${REF}" ;;
  *) deny "only deploy.sh, rollback.sh and reseed.sh may be run with this key" ;;
esac
