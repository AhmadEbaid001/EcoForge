#!/usr/bin/env bash
#
# Put the last known-good release back, now, by hand.
#
# deploy.sh rolls back on its own when a release fails its checks - that path is
# automatic and needs nobody. This is the other case: the deploy passed every check
# and the release is still wrong. Checks answer "is it serving", not "is it
# correct", and the gap between those two is exactly where a person has to be able
# to intervene without waiting for a pipeline.
#
#   rollback.sh                    # back to the recorded last-good release
#   rollback.sh <image-ref> [sha]  # back to a specific one
#
# A release is the image AND the commit, so both go back together. What was running
# before is written to .deploy/rolled-back-from, so this can itself be undone
# without going to look for the digest.

set -euo pipefail

APP_DIR="${GEMP_APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
STATE_DIR="${GEMP_STATE_DIR:-${APP_DIR}/.deploy}"
LAST_GOOD_IMAGE="${STATE_DIR}/last-good-image"
LAST_GOOD_COMMIT="${STATE_DIR}/last-good-commit"
LOG="${STATE_DIR}/deploy.log"

HEALTH_TIMEOUT_S="${GEMP_HEALTH_TIMEOUT_S:-180}"
HEALTH_INTERVAL_S="${GEMP_HEALTH_INTERVAL_S:-5}"
BASE_URL="${GEMP_BASE_URL:-http://localhost:8080}"
SERVICES=(core sim)

log() { printf '%s  %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" | tee -a "${LOG}"; }
die() { log "FAIL: $*"; exit 1; }

cd "${APP_DIR}" || die "no application directory at ${APP_DIR}"
mkdir -p "${STATE_DIR}"

TARGET_IMAGE="${1:-}"
TARGET_COMMIT="${2:-}"

if [ -z "${TARGET_IMAGE}" ]; then
  [ -s "${LAST_GOOD_IMAGE}" ] || die "no recorded last-good release. Pass one: rollback.sh <image-ref> [sha]"
  TARGET_IMAGE="$(cat "${LAST_GOOD_IMAGE}")"
  [ -s "${LAST_GOOD_COMMIT}" ] && TARGET_COMMIT="$(cat "${LAST_GOOD_COMMIT}")"
fi

CURRENT_IMAGE=""
if id="$(docker compose ps -q core 2>/dev/null | head -n1)" && [ -n "${id}" ]; then
  CURRENT_IMAGE="$(docker inspect --format '{{.Image}}' "${id}" 2>/dev/null || true)"
fi
CURRENT_COMMIT="$(git -C "${APP_DIR}" rev-parse HEAD 2>/dev/null || true)"

if [ "${CURRENT_IMAGE}" = "${TARGET_IMAGE}" ] && [ "${CURRENT_COMMIT}" = "${TARGET_COMMIT}" ]; then
  log "already running ${TARGET_IMAGE}; nothing to do"
  exit 0
fi

log "rolling back from ${CURRENT_IMAGE:-unknown} to ${TARGET_IMAGE}"
[ -n "${CURRENT_IMAGE}" ] && printf '%s\n' "${CURRENT_IMAGE}" > "${STATE_DIR}/rolled-back-from"
[ -n "${CURRENT_COMMIT}" ] && printf '%s\n' "${CURRENT_COMMIT}" > "${STATE_DIR}/rolled-back-from-commit"

docker pull --quiet "${TARGET_IMAGE}" >/dev/null 2>&1 || log "not in the registry; using the local copy"

# The configuration goes back with the image: compose file, nginx config, and this
# script. Rolling back one without the other produces a third combination that was
# never tested anywhere.
if [ -n "${TARGET_COMMIT}" ] && git -C "${APP_DIR}" rev-parse --git-dir >/dev/null 2>&1; then
  git -C "${APP_DIR}" fetch --quiet origin "${TARGET_COMMIT}" 2>/dev/null || true
  git -C "${APP_DIR}" checkout --quiet --force "${TARGET_COMMIT}" \
    && log "checkout back at ${TARGET_COMMIT}" \
    || log "could not check out ${TARGET_COMMIT}; the image is going back without it"
fi

# No schema downgrade, deliberately. A downgrade that drops a column destroys the
# data in it, which would make the rollback the thing that loses the readings. The
# schema is expected to stay compatible with the previous release.

# Same reasoning as deploy.sh: compose exits non-zero when a dependent service
# cannot start, and that must not stop us short of the checks.
GEMP_IMAGE="${TARGET_IMAGE}" docker compose up -d --no-build "${SERVICES[@]}" \
  || log "compose could not bring up ${TARGET_IMAGE} cleanly; the checks decide"
docker compose restart nginx || log "nginx would not restart"

deadline=$(( SECONDS + HEALTH_TIMEOUT_S ))
while [ "${SECONDS}" -lt "${deadline}" ]; do
  if curl -fsS --max-time 5 "${BASE_URL}/health" 2>/dev/null | grep -q '"database":"ok"'; then
    if curl -fsS --max-time 10 "${BASE_URL}/" 2>/dev/null | grep -q 'id="root"'; then
      log "OK: ${TARGET_IMAGE} is live and serving"
      exit 0
    fi
  fi
  sleep "${HEALTH_INTERVAL_S}"
done

die "${TARGET_IMAGE} did not come back within ${HEALTH_TIMEOUT_S}s. The host needs attention."
