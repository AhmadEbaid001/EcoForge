#!/usr/bin/env bash
#
# Rebuild the reading history so it ends now.
#
# The data clock is the reason this exists. The simulator advances data time at
# GEMP_SIM_SPEED, so a deployment left running for weeks holds history that ends
# wherever that ratchet took it, and a portfolio whose fixture has been replaced
# holds the OLD buildings until something re-imports them. Both are fixed by the
# same operation: import the portfolio again, wipe the readings, and generate a
# fresh window that ends at this moment.
#
#   ./reseed.sh 6          six months of history, ending now
#   ./reseed.sh            the default, which is also six
#
# WHAT THIS DESTROYS, stated plainly because the script cannot ask:
#
#   * every stored reading, and with it every anomaly and every integrity
#     checkpoint that referred to one
#   * the chain heads in the anchor file are rewritten from the new history
#
# WHAT IT KEEPS: accounts, sessions, the audit log, stored allocations, and the
# catalog. A stored allocation whose readings are gone is still a record of what
# was decided and still verifies against its own input hash.
#
# It runs from INSIDE the image that is currently deployed, the same way deploy.sh
# runs migrations - the seeder and the application that reads what it wrote are
# then the same artefact.

set -euo pipefail

MONTHS="${1:-6}"

case "${MONTHS}" in
  ''|*[!0-9]*) printf 'reseed: months must be a whole number, got %s\n' "${MONTHS}" >&2; exit 2 ;;
esac
if [ "${MONTHS}" -lt 1 ] || [ "${MONTHS}" -gt 120 ]; then
  printf 'reseed: months must be between 1 and 120, got %s\n' "${MONTHS}" >&2
  exit 2
fi

APP_DIR="${GEMP_APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
STATE_DIR="${GEMP_STATE_DIR:-${APP_DIR}/.deploy}"
LOG="${STATE_DIR}/reseed.log"
LAST_GOOD_IMAGE="${STATE_DIR}/last-good-image"
BASE_URL="${GEMP_BASE_URL:-http://localhost:8080}"

mkdir -p "${STATE_DIR}"

log() { printf '%s  %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" | tee -a "${LOG}"; }
die() { log "FAIL: $*"; exit 1; }

cd "${APP_DIR}" || die "no application directory at ${APP_DIR}"
[ -f .env ] || die ".env is missing; this host has never been set up"

# Whatever is deployed right now. Not `latest`: the point is to seed with the code
# that is serving, so the seeder and the readers agree about the schema.
IMAGE=""
[ -f "${LAST_GOOD_IMAGE}" ] && IMAGE="$(cat "${LAST_GOOD_IMAGE}")"
if [ -z "${IMAGE}" ]; then
  log "no recorded image; falling back to whatever compose resolves"
fi

log "reseeding ${MONTHS} month(s) of history, ending now"

# The simulator is stopped first and started last. It publishes readings
# continuously, and a node writing into the table while the seeder is deleting from
# it produces exactly the discontinuity this operation exists to remove.
log "stopping the simulator"
docker compose stop sim 2>&1 | tee -a "${LOG}" || log "sim was not running"

if ! GEMP_IMAGE="${IMAGE}" docker compose run --rm --no-deps \
      core python -m gemp.seed --months "${MONTHS}" --force 2>&1 | tee -a "${LOG}"; then
  log "the seeder failed; starting the simulator again and leaving the data as it is"
  docker compose start sim 2>&1 | tee -a "${LOG}" || true
  die "reseed failed"
fi

# The API warms its anomaly window from the readings at start-up, so it has to be
# restarted or it keeps scoring against a month of history that no longer exists.
log "restarting the API and the simulator"
docker compose restart core 2>&1 | tee -a "${LOG}" || die "the API would not restart"
docker compose start sim 2>&1 | tee -a "${LOG}" || die "the simulator would not start"

# Same check the deploy uses, for the same reason: a restart that comes back
# unhealthy should be reported here rather than discovered on the screen.
deadline=$(( $(date +%s) + ${GEMP_HEALTH_TIMEOUT_S:-180} ))
until curl -fsS --max-time 5 "${BASE_URL}/health" 2>/dev/null | grep -q '"database":"ok"'; do
  [ "$(date +%s)" -lt "${deadline}" ] || die "the API did not come back healthy after the reseed"
  sleep "${GEMP_HEALTH_INTERVAL_S:-5}"
done

log "OK: history now ends at $(date -u +%Y-%m-%dT%H:%M:%SZ), and the API is healthy"
