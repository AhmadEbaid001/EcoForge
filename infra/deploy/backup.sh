#!/usr/bin/env bash
#
# Take a backup that can actually be restored.
#
# Everything this platform demonstrates lives in one Docker volume: 7 million signed
# readings, the anomaly ground truth the F9 claim is measured against, and every
# stored allocation. There was no backup at all. Losing `pgdata` before the
# demonstration is not an inconvenience, it is unrecoverable - re-seeding produces a
# different portfolio and destroys the ground truth the claims rest on.
#
# Three things are captured, because restoring any two of them is not a restore:
#
#   * the database, in pg_dump's custom format so it can be restored selectively
#   * the anchor file, which lives OUTSIDE the database volume on purpose (F5) and
#     is what proves the chain was not rebuilt along with the rows
#   * the .env, which holds the signing key - readings restored without it can
#     never be verified again, and the integrity feature becomes decoration
#
#   backup.sh                 # into .deploy/backups
#   backup.sh /mnt/usb/gemp   # somewhere else, ideally another disk
#
# A backup on the same disk as the thing it backs up is a copy, not a backup.

set -euo pipefail

APP_DIR="${GEMP_APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
DEST="${1:-${APP_DIR}/.deploy/backups}"
KEEP="${GEMP_BACKUP_KEEP:-7}"

DB_SERVICE=timescaledb
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="${DEST}/${STAMP}"

log() { printf '%s  %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
die() { log "FAIL: $*"; exit 1; }

cd "${APP_DIR}" || die "no application directory at ${APP_DIR}"
mkdir -p "${OUT}"

# The credentials come from .env, the same place the running stack reads them.
set -a
# shellcheck disable=SC1091
[ -f .env ] && . ./.env
set +a
DB_NAME="${GEMP_DB_NAME:-gemp}"
DB_USER="${GEMP_DB_USER:-gemp}"

log "backing up ${DB_NAME} to ${OUT}"

# --format=custom, not plain SQL: it compresses, and it lets a restore pick single
# tables - which is what you want at 3am when one table is corrupt and re-importing
# seven million readings would take longer than the outage.
if ! docker compose exec -T "${DB_SERVICE}" \
      pg_dump --username "${DB_USER}" --dbname "${DB_NAME}" \
              --format=custom --compress=6 \
      > "${OUT}/database.dump"; then
  rm -rf "${OUT}"
  die "pg_dump failed - no partial backup has been left behind to be mistaken for a good one"
fi

# A zero-length dump is a failure that exited 0. It has happened to everyone once.
size="$(wc -c < "${OUT}/database.dump")"
[ "${size}" -gt 4096 ] || { rm -rf "${OUT}"; die "the dump is only ${size} bytes; refusing to keep it"; }
log "database: ${size} bytes"

if [ -d anchor ]; then
  tar -czf "${OUT}/anchor.tar.gz" anchor
  log "anchor: $(wc -c < "${OUT}/anchor.tar.gz") bytes"
fi

if [ -f .env ]; then
  cp .env "${OUT}/env"
  chmod 600 "${OUT}/env"
  log "env: copied (mode 600 - it holds the signing key)"
fi

# What this backup is OF. A directory of dumps with no provenance is a guessing game
# during the one hour you cannot afford to guess.
{
  echo "taken:      $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "host:       $(hostname)"
  echo "commit:     $(git -C "${APP_DIR}" rev-parse HEAD 2>/dev/null || echo unknown)"
  echo "image:      $(cat "${APP_DIR}/.deploy/last-good-image" 2>/dev/null || echo unknown)"
  echo "database:   ${DB_NAME}"
  echo "data clock: $(curl -fsS --max-time 5 "${GEMP_BASE_URL:-http://localhost:8080}/health" 2>/dev/null | sed -n 's/.*"readings":"\([^"]*\)".*/\1/p' || echo unknown)"
} > "${OUT}/MANIFEST"

log "wrote ${OUT}"

# Keep the last N. Unbounded backups fill the disk, and a full disk stops the
# database - the backup becoming the outage is a genuinely common way to lose a
# system.
mapfile -t old < <(find "${DEST}" -maxdepth 1 -mindepth 1 -type d | sort -r | tail -n +$((KEEP + 1)))
for dir in "${old[@]:-}"; do
  [ -n "${dir}" ] || continue
  log "pruning ${dir}"
  rm -rf "${dir}"
done

log "OK. Restore with: infra/deploy/restore.sh ${OUT}"
