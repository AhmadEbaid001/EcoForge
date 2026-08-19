#!/usr/bin/env bash
#
# Put a backup back.
#
# A backup nobody has ever restored is a hypothesis. This script exists so the
# restore is a rehearsed procedure rather than something invented under pressure -
# and so that rehearsing it is one command, which is the only reason anyone ever
# actually does it.
#
#   restore.sh .deploy/backups/20260819T120000Z
#   restore.sh <dir> --into gemp_restore_test    # rehearse without touching the real one
#
# The rehearsal form is the important one. Restoring over the live database to find
# out whether the backup works destroys the thing you were trying to protect if it
# does not. `--into` restores to a scratch database on the same server, counts what
# came back, and drops it.

set -euo pipefail

APP_DIR="${GEMP_APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
DB_SERVICE=timescaledb

SRC="${1:?usage: restore.sh <backup-dir> [--into <database>]}"
TARGET_DB=""
if [ "${2:-}" = "--into" ]; then
  TARGET_DB="${3:?--into needs a database name}"
fi

log() { printf '%s  %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
die() { log "FAIL: $*"; exit 1; }

cd "${APP_DIR}" || die "no application directory at ${APP_DIR}"
[ -f "${SRC}/database.dump" ] || die "no database.dump in ${SRC}"

set -a
# shellcheck disable=SC1091
[ -f .env ] && . ./.env
set +a
DB_USER="${GEMP_DB_USER:-gemp}"
LIVE_DB="${GEMP_DB_NAME:-gemp}"

[ -f "${SRC}/MANIFEST" ] && { log "restoring:"; sed 's/^/    /' "${SRC}/MANIFEST"; }

# TimescaleDB's documented restore procedure, and it is not optional.
#
# A hypertable is a parent table plus hundreds of chunks wired together by the
# extension's own catalogue. Restoring that with a plain pg_restore lets the
# extension's triggers fire while the chunks are still arriving, which is how the
# first drill here restored 7.5 million rows and silently failed to recreate the
# foreign key on `reading`. `timescaledb_pre_restore()` puts the extension into a
# mode where it stays out of the way; `timescaledb_post_restore()` puts it back and
# rebuilds its catalogue.
prepare_target() {
  local db="$1"
  docker compose exec -T "${DB_SERVICE}" \
    psql --username "${DB_USER}" --dbname "${db}" \
    -c "CREATE EXTENSION IF NOT EXISTS timescaledb;" >/dev/null
  docker compose exec -T "${DB_SERVICE}" \
    psql --username "${DB_USER}" --dbname "${db}" \
    -c "SELECT timescaledb_pre_restore();" >/dev/null
}

# Returns the number of errors pg_restore reported, on stdout.
run_restore() {
  local db="$1" out
  out="$(docker compose exec -T "${DB_SERVICE}" \
    pg_restore --username "${DB_USER}" --dbname "${db}" \
               --no-owner --no-privileges --single-transaction \
    < "${SRC}/database.dump" 2>&1 || true)"

  docker compose exec -T "${DB_SERVICE}" \
    psql --username "${DB_USER}" --dbname "${db}" \
    -c "SELECT timescaledb_post_restore();" >/dev/null

  printf '%s' "${out}" | tail -5 >&2
  printf '%s' "${out}" | sed -n 's/.*errors ignored on restore: \([0-9]*\).*/\1/p' | tail -1 | grep -E '^[0-9]+$' || echo 0
}

if [ -n "${TARGET_DB}" ]; then
  # ---- rehearsal ---------------------------------------------------------
  log "rehearsing into ${TARGET_DB} (the live database is not touched)"

  docker compose exec -T "${DB_SERVICE}" \
    psql --username "${DB_USER}" --dbname postgres \
    -c "DROP DATABASE IF EXISTS ${TARGET_DB};" >/dev/null
  docker compose exec -T "${DB_SERVICE}" \
    psql --username "${DB_USER}" --dbname postgres \
    -c "CREATE DATABASE ${TARGET_DB};" >/dev/null

  prepare_target "${TARGET_DB}"
  errors="$(run_restore "${TARGET_DB}")"

  log "counting what came back:"
  docker compose exec -T "${DB_SERVICE}" \
    psql --username "${DB_USER}" --dbname "${TARGET_DB}" -t -A -F' ' -c "
      SELECT 'buildings', count(*) FROM building
      UNION ALL SELECT 'readings', count(*) FROM reading
      UNION ALL SELECT 'anomalies', count(*) FROM anomaly
      UNION ALL SELECT 'runs', count(*) FROM optimization_run
      UNION ALL SELECT 'users', count(*) FROM app_user;" | sed 's/^/    /'

  # Rows are not the whole database. The first drill restored every row and quietly
  # lost the foreign key from `reading` to `building` - the constraint the ingester
  # depends on to refuse readings for buildings the portfolio does not contain. A
  # restore that drops constraints produces a database that looks right and behaves
  # differently, so the constraints are counted too.
  log "checking the constraints came back:"
  missing="$(docker compose exec -T "${DB_SERVICE}" \
    psql --username "${DB_USER}" --dbname "${TARGET_DB}" -t -A -c "
      SELECT count(*) FROM pg_constraint
      WHERE conname = 'reading_building_id_fkey';" | tr -d '[:space:]')"
  log "    reading_building_id_fkey present: ${missing}"

  docker compose exec -T "${DB_SERVICE}" \
    psql --username "${DB_USER}" --dbname postgres \
    -c "DROP DATABASE ${TARGET_DB};" >/dev/null

  if [ "${errors}" != "0" ] || [ "${missing}" != "1" ]; then
    die "the rehearsal restored with ${errors} error(s) and fkey=${missing}. \
This backup does NOT restore cleanly - fix that before relying on it."
  fi

  log "OK: the backup restores cleanly. Scratch database dropped."
  exit 0
fi

# ---- the real thing ------------------------------------------------------
#
# Destructive, and it says so. Restoring over a live database while the ingester is
# writing to it produces a mixture of both, which is worse than either.
cat <<WARNING

  This REPLACES the contents of the '${LIVE_DB}' database on this host.
  Everything currently in it is discarded.

WARNING
read -r -p "  Type the database name to continue: " answer
[ "${answer}" = "${LIVE_DB}" ] || die "not confirmed - nothing was changed"

log "stopping the writers"
docker compose stop core sim || true

log "restoring into ${LIVE_DB}"
docker compose exec -T "${DB_SERVICE}" \
  psql --username "${DB_USER}" --dbname "${LIVE_DB}" \
  -c "SELECT timescaledb_pre_restore();" >/dev/null

docker compose exec -T "${DB_SERVICE}" \
  pg_restore --username "${DB_USER}" --dbname "${LIVE_DB}" \
             --clean --if-exists --no-owner --no-privileges \
  < "${SRC}/database.dump" 2>&1 | tail -10 || true

docker compose exec -T "${DB_SERVICE}" \
  psql --username "${DB_USER}" --dbname "${LIVE_DB}" \
  -c "SELECT timescaledb_post_restore();" >/dev/null

if [ -f "${SRC}/anchor.tar.gz" ]; then
  log "restoring the anchor file"
  tar -xzf "${SRC}/anchor.tar.gz" -C "${APP_DIR}"
fi

if [ -f "${SRC}/env" ]; then
  # Not copied over automatically: if the running .env has a DIFFERENT signing key
  # from the one in the backup, every restored reading fails verification - and
  # silently overwriting the live key is how a restore turns into an integrity
  # incident. The operator decides.
  if ! diff -q "${SRC}/env" .env >/dev/null 2>&1; then
    log "WARNING: the backup's .env differs from the one on this host."
    log "         The signing key must match the restored readings or the chain"
    log "         cannot be verified. The backup's copy is at ${SRC}/env."
  fi
fi

log "starting the application"
docker compose up -d core sim
docker compose restart nginx

log "OK. Verify a chain on the integrity screen before trusting this."
