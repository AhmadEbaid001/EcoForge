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

# Take the last whitespace-separated token and require it to look like an image
# reference we published. No shell metacharacters survive this: the value is never
# passed to anything that would interpret them, and anything outside the character
# class is refused rather than escaped.
REF="${REQUEST##* }"

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

case "${REQUEST}" in
  *deploy.sh*)   exec "${APP_DIR}/infra/deploy/deploy.sh" "${REF}" ;;
  *rollback.sh*) exec "${APP_DIR}/infra/deploy/rollback.sh" "${REF}" ;;
  *) deny "only deploy.sh and rollback.sh may be run with this key" ;;
esac
