#!/usr/bin/env bash
#
# Deploy one release to this host, prove it works, and put the old one back if it
# does not.
#
# This runs ON THE SERVER, invoked over SSH by .github/workflows/deploy.yml. That is
# the whole design: a rollback that needs the CI runner to still be alive is not a
# rollback. If the network drops mid-deploy, if the runner is cancelled, if GitHub
# has an incident - this script has already recorded what was running and will still
# restore it.
#
#   deploy.sh <image-ref> [git-sha]
#
# A release is TWO things and both move together:
#
#   * the image  - the Python application, its dependencies, and its migrations
#   * the commit - docker-compose.yml, the nginx config, and this script itself
#
# Shipping only the image was a real hole: change the compose file and the host
# would keep running the old one against the new image, silently, forever. So the
# checkout is synced to the commit the image was built from, and the rollback
# restores both.
#
# The sequence:
#
#   1. re-exec from a copy in /tmp, because step 3 rewrites this file
#   2. resolve the image to a digest - a tag is a pointer and pointers move
#   3. sync the checkout to the release commit
#   4. record what is running now: image AND commit
#   5. migrate, from inside the new image, before it serves anything
#   6. swap core and sim, restart nginx
#   7. health check, then a smoke test that a broken build cannot pass
#   8. on failure: restore the recorded image and commit, verify THAT, exit non-zero
#
# The database and the broker are never redeployed. Only the application moves.

set -euo pipefail

# --------------------------------------------------------------------------
# 1. run from a copy
# --------------------------------------------------------------------------
#
# Step 3 checks out a different commit, which rewrites this file underneath the
# running shell. Bash reads a script incrementally rather than all at once, so a
# file that changes mid-run makes it resume at a byte offset into different text -
# a failure mode that looks like the script silently doing something else. Copying
# to /tmp first costs nothing and removes the whole class of problem.
if [ "${GEMP_DEPLOY_REEXEC:-}" != "1" ]; then
  _copy="$(mktemp "${TMPDIR:-/tmp}/gemp-deploy.XXXXXX")"
  cat "$0" > "${_copy}"
  chmod +x "${_copy}"
  GEMP_DEPLOY_REEXEC=1 GEMP_DEPLOY_COPY="${_copy}" \
    GEMP_APP_DIR="${GEMP_APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}" \
    exec "${_copy}" "$@"
fi
trap 'rm -f "${GEMP_DEPLOY_COPY:-}"' EXIT

REF="${1:?usage: deploy.sh <image-ref> [git-sha]}"
SHA="${2:-}"

APP_DIR="${GEMP_APP_DIR:?GEMP_APP_DIR was not carried through the re-exec}"
STATE_DIR="${GEMP_STATE_DIR:-${APP_DIR}/.deploy}"
LAST_GOOD_IMAGE="${STATE_DIR}/last-good-image"
LAST_GOOD_COMMIT="${STATE_DIR}/last-good-commit"
LOG="${STATE_DIR}/deploy.log"

# How long the new containers get to become healthy. The API waits on the database
# and warms the anomaly detector from a month of readings at start-up, which is why
# this is minutes rather than seconds.
HEALTH_TIMEOUT_S="${GEMP_HEALTH_TIMEOUT_S:-180}"
HEALTH_INTERVAL_S="${GEMP_HEALTH_INTERVAL_S:-5}"
BASE_URL="${GEMP_BASE_URL:-http://localhost:8080}"

SERVICES=(core sim)

mkdir -p "${STATE_DIR}"

log() { printf '%s  %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" | tee -a "${LOG}"; }
die() { log "FAIL: $*"; exit 1; }

cd "${APP_DIR}" || die "no application directory at ${APP_DIR}"

# The signing key and the database password live here. A world-readable .env on a
# host anyone else can log into is the whole integrity guarantee handed over.
if [ -f .env ]; then
  perms="$(stat -c '%a' .env 2>/dev/null || stat -f '%Lp' .env 2>/dev/null || echo '600')"
  case "${perms}" in
    600|400) : ;;
    *) log "WARNING: .env is mode ${perms}; it holds the signing key. chmod 600 .env" ;;
  esac
else
  die ".env is missing. Run scripts/setup_env.py on this host before deploying."
fi

# --------------------------------------------------------------------------
# 2. what exactly are we deploying
# --------------------------------------------------------------------------

log "requested ${REF}${SHA:+ at ${SHA}}"

# --------------------------------------------------------------------------
# Registry credentials, if any, arrive on STDIN.
# --------------------------------------------------------------------------
#
# The package is private because the repository is, so the pull below needs a
# credential. Three things this deliberately is not:
#
#   * not an argument. Arguments are visible in `ps` to every user on the box for
#     as long as the pull takes.
#   * not a file. bootstrap.sh does not put a GitHub token on the host and this
#     does not either - the token exists in this process and nowhere else.
#   * not required. Deploying from a local `docker load`, or from a public
#     registry, sends nothing and this block does nothing.
#
# The workflow pipes its GITHUB_TOKEN, which is minted per job and expires with it.
# `read -t` rather than `cat` so that a stdin nobody closes costs ten seconds
# rather than hanging the deploy forever.
REGISTRY_TOKEN=""
if [ ! -t 0 ]; then
  IFS= read -r -t 10 REGISTRY_TOKEN <&0 || REGISTRY_TOKEN=""
fi

if [ -n "${REGISTRY_TOKEN}" ]; then
  REGISTRY_HOST="${REF%%/*}"
  # Log out on the way out however we leave, including a failed deploy: the
  # credential must not outlive the process that was given it.
  trap 'docker logout "${REGISTRY_HOST}" >/dev/null 2>&1 || true; rm -f "${GEMP_DEPLOY_COPY:-}"' EXIT
  if printf '%s' "${REGISTRY_TOKEN}"        | docker login "${REGISTRY_HOST}"                       --username "${GEMP_REGISTRY_USER:-x-access-token}"                       --password-stdin >/dev/null 2>&1; then
    log "authenticated to ${REGISTRY_HOST}"
  else
    log "WARNING: could not authenticate to ${REGISTRY_HOST}; trying the pull anyway"
  fi
  REGISTRY_TOKEN=""
fi

# Pull if we can, use what is already here if we cannot. The registry is the normal
# path; it is not the only one. This host is also meant to run with the cable out,
# where an image arrives as a file over `docker load` and there is nothing to pull
# from. What matters is that the image exists and that its signature was verified
# before it was put here, which the deploy workflow does.
if docker pull --quiet "${REF}" >/dev/null 2>&1; then
  log "pulled ${REF}"
elif docker image inspect "${REF}" >/dev/null 2>&1; then
  log "not in a registry; using the copy already on this host"
else
  die "cannot pull ${REF} and it is not present locally - nothing changed"
fi

DIGEST_REF="$(docker inspect --format '{{index .RepoDigests 0}}' "${REF}" 2>/dev/null || true)"
[ -n "${DIGEST_REF}" ] || DIGEST_REF="${REF}"
log "resolved to ${DIGEST_REF}"

# --------------------------------------------------------------------------
# 3. record what is running, then move the checkout
# --------------------------------------------------------------------------

current_image() {
  local id
  id="$(docker compose ps -q core 2>/dev/null | head -n1)"
  [ -n "${id}" ] || return 0
  docker inspect --format '{{.Image}}' "${id}" 2>/dev/null || true
}

PREVIOUS_IMAGE="$(current_image || true)"
PREVIOUS_COMMIT="$(git -C "${APP_DIR}" rev-parse HEAD 2>/dev/null || true)"

if [ -n "${PREVIOUS_IMAGE}" ]; then
  printf '%s\n' "${PREVIOUS_IMAGE}" > "${LAST_GOOD_IMAGE}.candidate"
  [ -n "${PREVIOUS_COMMIT}" ] && printf '%s\n' "${PREVIOUS_COMMIT}" > "${LAST_GOOD_COMMIT}.candidate"
  log "currently running ${PREVIOUS_IMAGE} at ${PREVIOUS_COMMIT:-unknown commit}"
else
  log "nothing running yet - first deploy, nothing to roll back to"
fi

sync_checkout() {
  local sha="$1"
  [ -n "${sha}" ] || { log "no commit given; leaving the checkout as it is"; return 0; }
  git -C "${APP_DIR}" rev-parse --git-dir >/dev/null 2>&1 || {
    log "not a git checkout; leaving it as it is"; return 0; }

  # Fetch by SHA rather than by branch: the branch may have moved on since this
  # release was built, and deploying "whatever main points at now" is how a host
  # ends up running a commit nobody chose.
  git -C "${APP_DIR}" fetch --quiet origin "${sha}" 2>/dev/null \
    || git -C "${APP_DIR}" fetch --quiet origin || true

  if ! git -C "${APP_DIR}" checkout --quiet --force "${sha}" 2>/dev/null; then
    log "cannot check out ${sha}; the configuration on this host is unchanged"
    return 1
  fi
  log "checkout now at ${sha}"
}

sync_checkout "${SHA}" || die "could not sync the checkout - nothing was swapped"

# --------------------------------------------------------------------------
# 4. migrate, before anything serves the new code
# --------------------------------------------------------------------------
#
# Run from INSIDE the new image, so the migration and the application that needs it
# are the same artefact. A failure here aborts before the running containers are
# touched, which is the cheapest possible failure: nothing has changed yet.
#
# Forward only. There is no automatic downgrade and there must not be: a downgrade
# that drops a column destroys the data in it, and a rollback would then be the
# thing that loses the readings. A schema change therefore has to be compatible with
# the release BEFORE it - add columns, do not rename them, and remove the old ones a
# release later. That is the contract that makes rolling back safe.
migrate() {
  local image="$1"
  log "running migrations from ${image}"
  if ! GEMP_IMAGE="${image}" docker compose run --rm --no-deps \
        --entrypoint alembic core upgrade head 2>&1 | tee -a "${LOG}"; then
    return 1
  fi
}

migrate "${DIGEST_REF}" || die "migrations failed - nothing was swapped, the previous release is still serving"

# --------------------------------------------------------------------------
# 5. swap
# --------------------------------------------------------------------------

bring_up() {
  local image="$1"

  # A failure here is NOT fatal, and that is load-bearing.
  #
  # `sim` waits on `core` being healthy, so a release that starts and then fails its
  # container healthcheck makes compose exit non-zero with "dependency failed to
  # start". Under `set -e` that killed this script on the spot - before the health
  # check, and therefore before the rollback. The rollback path was unreachable for
  # the single most likely way a deploy goes wrong.
  if ! GEMP_IMAGE="${image}" docker compose up -d --no-build "${SERVICES[@]}"; then
    log "compose could not bring up ${image} cleanly; the checks below decide"
  fi

  # nginx resolves the `core` hostname once, at startup. A recreated core container
  # gets a new address and every request answers 502 while `docker ps` reports
  # everything healthy. Not optional, and not paranoia - it has happened here.
  docker compose restart nginx || log "nginx would not restart"

  return 0
}

# --------------------------------------------------------------------------
# 6. is it actually serving
# --------------------------------------------------------------------------

health_ok() {
  local deadline=$(( SECONDS + HEALTH_TIMEOUT_S ))
  while [ "${SECONDS}" -lt "${deadline}" ]; do
    if curl -fsS --max-time 5 "${BASE_URL}/health" 2>/dev/null | grep -q '"database":"ok"'; then
      return 0
    fi
    sleep "${HEALTH_INTERVAL_S}"
  done
  return 1
}

# /health proves the process is up and the database answers. It does not prove the
# release is any good: a build with a broken front end, a broken nginx upstream or
# an auth middleware that fell open passes it without noticing. These three are
# cheap, need no credentials, and each one fails for a different real reason.
smoke_ok() {
  local body status

  # The application shell is being served at all - catches a broken nginx or a
  # missing web/ mount, which /health cannot see because it never touches nginx's
  # static path.
  if ! curl -fsS --max-time 10 "${BASE_URL}/" 2>/dev/null | grep -q 'id="root"'; then
    log "smoke: the application shell is not being served"
    return 1
  fi

  # The API answers through nginx, and says nobody is signed in. Catches the stale
  # upstream 502 and a core that is up but not reachable.
  body="$(curl -fsS --max-time 10 "${BASE_URL}/api/v1/auth/session" 2>/dev/null || true)"
  if ! printf '%s' "${body}" | grep -q '"authenticated":false'; then
    log "smoke: /auth/session did not answer as an anonymous caller"
    return 1
  fi

  # And a protected route still refuses one. This is the check that would catch an
  # auth middleware that failed OPEN - the failure nobody notices, because
  # everything works and works for everyone.
  status="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "${BASE_URL}/api/v1/meta" || true)"
  if [ "${status}" != "401" ]; then
    log "smoke: /api/v1/meta answered ${status} to an anonymous caller, expected 401"
    return 1
  fi

  return 0
}

verify() {
  health_ok || { log "health check failed after ${HEALTH_TIMEOUT_S}s"; return 1; }
  smoke_ok  || { log "health passed but the smoke test did not"; return 1; }
  return 0
}

log "starting ${DIGEST_REF}"
bring_up "${DIGEST_REF}"

if verify; then
  printf '%s\n' "${DIGEST_REF}" > "${LAST_GOOD_IMAGE}"
  [ -n "${SHA}" ] && printf '%s\n' "${SHA}" > "${LAST_GOOD_COMMIT}"
  rm -f "${LAST_GOOD_IMAGE}.candidate" "${LAST_GOOD_COMMIT}.candidate"
  log "OK: ${DIGEST_REF} is live, healthy and serving"
  exit 0
fi

# --------------------------------------------------------------------------
# 7. it did not come up. Put back what did.
# --------------------------------------------------------------------------

docker compose logs --tail 50 core 2>&1 | tee -a "${LOG}" || true

pick() {
  local final="$1" candidate="$1.candidate"
  if [ -s "${final}" ]; then cat "${final}"
  elif [ -s "${candidate}" ]; then cat "${candidate}"
  fi
}

ROLLBACK_IMAGE="$(pick "${LAST_GOOD_IMAGE}")"
ROLLBACK_COMMIT="$(pick "${LAST_GOOD_COMMIT}")"

if [ -z "${ROLLBACK_IMAGE}" ]; then
  die "no previous image recorded - the host is left with ${DIGEST_REF}, which is not \
serving. This is a first deploy; there is nothing to go back to."
fi

log "rolling back to ${ROLLBACK_IMAGE} at ${ROLLBACK_COMMIT:-the current commit}"

# The configuration goes back with the image. Rolling back the application while
# leaving the new compose file in place is how a rollback produces a third state
# that was never tested.
[ -n "${ROLLBACK_COMMIT}" ] && sync_checkout "${ROLLBACK_COMMIT}" || true

# Deliberately no downgrade. See the note above `migrate`: the schema is expected to
# be compatible with the previous release, and a downgrade that drops a column would
# make the rollback the thing that loses data.
bring_up "${ROLLBACK_IMAGE}"

if verify; then
  log "rolled back: ${ROLLBACK_IMAGE} is live and serving. The deploy of ${DIGEST_REF} failed."
  exit 1
fi

die "rollback to ${ROLLBACK_IMAGE} ALSO failed its checks. The host needs attention: \
neither the new release nor the previous one is serving."
