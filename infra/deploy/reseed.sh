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
# Written as `if` blocks rather than `[ … ] && …` lists on purpose: under `set -e` a
# list whose test fails IS a failed command, so `[ -f x ] && VAR=…` exits the script
# the moment the file is absent - which is precisely the case the fallback exists to
# handle.
IMAGE=""
if [ -f "${LAST_GOOD_IMAGE}" ]; then
  IMAGE="$(cat "${LAST_GOOD_IMAGE}")"
fi
if [ -z "${IMAGE}" ]; then
  # A host deployed before deploy.sh started recording, or one brought up by hand.
  # Ask the running container what it is rather than falling through to compose's
  # default of gemp-core:local, which on a deployed host does not exist and fails
  # as "image not found" after the simulator has already been stopped.
  container="$(docker compose ps -q core 2>/dev/null | head -n1 || true)"
  if [ -n "${container}" ]; then
    IMAGE="$(docker inspect -f '{{.Config.Image}}' "${container}" 2>/dev/null || true)"
  fi
  if [ -n "${IMAGE}" ]; then
    log "no recorded image; using the one core is running: ${IMAGE}"
  fi
fi
[ -n "${IMAGE}" ] || die "cannot tell which image is deployed; nothing was changed"

log "reseeding ${MONTHS} month(s) of history, ending now"

# BOTH writers stop, not just the simulator, and this is the correction that cost
# an afternoon to find.
#
# `core` runs the ingest consumer. The consumer holds each chain's head in memory
# and writes it to the external anchor once a minute. Stopping only `sim` leaves it
# running while the table is emptied and rebuilt underneath it, and the two ends of
# the integrity check then describe different worlds: measured on this host after a
# reseed, the anchor claimed sequence 69,066 for b001 while the table stopped at
# 69,063, and F5-b failed with "the tail has been deleted" - the alarm firing
# correctly at a discrepancy this operation had created.
#
# The API is therefore down for the length of the seed. That is the honest trade:
# an operation that rebuilds the entire reading history is not one to serve
# requests through, and the alternative is an integrity report nobody can trust.
log "stopping the simulator and the API"
docker compose stop sim core 2>&1 | tee -a "${LOG}" || log "they were not running"

if ! GEMP_IMAGE="${IMAGE}" docker compose run --rm --no-deps \
      core python -m gemp.seed --months "${MONTHS}" --force 2>&1 | tee -a "${LOG}"; then
  log "the seeder failed; starting the services again and leaving the data as it is"
  docker compose start core sim 2>&1 | tee -a "${LOG}" || true
  die "reseed failed"
fi

# Started rather than restarted: they have been down since before the wipe, so the
# consumer loads its chain heads from the history the seeder just wrote, and the
# detector warms its window from the same.
log "starting the API and the simulator"
docker compose start core 2>&1 | tee -a "${LOG}" || die "the API would not start"
docker compose start sim 2>&1 | tee -a "${LOG}" || die "the simulator would not start"

# Same check the deploy uses, for the same reason: a restart that comes back
# unhealthy should be reported here rather than discovered on the screen.
deadline=$(( $(date +%s) + ${GEMP_HEALTH_TIMEOUT_S:-180} ))
until curl -fsS --max-time 5 "${BASE_URL}/health" 2>/dev/null | grep -q '"database":"ok"'; do
  [ "$(date +%s)" -lt "${deadline}" ] || die "the API did not come back healthy after the reseed"
  sleep "${GEMP_HEALTH_INTERVAL_S:-5}"
done

log "OK: history now ends at $(date -u +%Y-%m-%dT%H:%M:%SZ), and the API is healthy"
