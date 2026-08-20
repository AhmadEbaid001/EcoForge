#!/usr/bin/env bash
#
# Turn a fresh Linux box into a host this pipeline can deploy to.
#
# Written for Ubuntu 22.04/24.04. Tested against Azure B2s (x86) and Oracle's
# Ampere A1 (ARM); the images differ only in the default login user, which this
# script does not care about because it creates its own. Run it once, with sudo:
#
#   curl -fsSL https://raw.githubusercontent.com/OWNER/REPO/main/infra/deploy/bootstrap.sh -o bootstrap.sh
#   less bootstrap.sh          # read it first. It is short, and it asks for root.
#   sudo bash bootstrap.sh https://github.com/OWNER/REPO.git
#
# Idempotent: running it twice changes nothing the second time.
#
# What it deliberately does NOT do:
#
#   * open port 22 to the internet. The runner reaches this host over Tailscale, so
#     SSH never needs a public listener at all. That is not a hardened public SSH -
#     it is no public SSH, which is a different and better thing.
#   * put a GitHub token on the host. The checkout is read-only and, for a private
#     repository, uses a deploy key you generate here and paste into the repo.
#   * start anything. The first deploy does that, so the first thing this host runs
#     is a release the pipeline built and signed.

set -euo pipefail

REPO_URL="${1:-}"
APP_DIR="${GEMP_APP_DIR:-/srv/gemp}"
DEPLOY_USER="${GEMP_DEPLOY_USER:-gemp}"

log()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
note() { printf '    %s\n' "$*"; }
die()  { printf '\nFAIL: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run with sudo"
[ -n "${REPO_URL}" ] || die "usage: sudo bash bootstrap.sh <repo-url>"

# --------------------------------------------------------------------------
log "System packages"
# --------------------------------------------------------------------------
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl git ufw jq >/dev/null
note "ok"

# --------------------------------------------------------------------------
log "Docker"
# --------------------------------------------------------------------------
if ! command -v docker >/dev/null; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "${VERSION_CODENAME}") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io \
                         docker-buildx-plugin docker-compose-plugin >/dev/null
fi
systemctl enable --now docker >/dev/null 2>&1 || true
note "$(docker --version)"
note "$(docker compose version)"

# --------------------------------------------------------------------------
log "Deploy user"
# --------------------------------------------------------------------------
#
# Membership of the `docker` group is root-equivalent on any Linux host - the daemon
# runs as root and will happily bind-mount /. That is why the SSH key this user
# accepts is restricted to a forced command further down: the restriction is not
# defence in depth, it IS the privilege boundary.
if ! id -u "${DEPLOY_USER}" >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash "${DEPLOY_USER}"
fi
usermod -aG docker "${DEPLOY_USER}"
install -d -m 700 -o "${DEPLOY_USER}" -g "${DEPLOY_USER}" "/home/${DEPLOY_USER}/.ssh"
note "user ${DEPLOY_USER}, in the docker group"

# --------------------------------------------------------------------------
log "Checkout"
# --------------------------------------------------------------------------
install -d -o "${DEPLOY_USER}" -g "${DEPLOY_USER}" "$(dirname "${APP_DIR}")"
if [ -d "${APP_DIR}/.git" ]; then
  note "already a checkout at ${APP_DIR}"
else
  if ! sudo -u "${DEPLOY_USER}" git clone --quiet "${REPO_URL}" "${APP_DIR}" 2>/dev/null; then
    cat <<CLONE

    Could not clone ${REPO_URL}.

    For a PRIVATE repository, give this host a read-only deploy key:

      sudo -u ${DEPLOY_USER} ssh-keygen -t ed25519 -N "" \\
           -f /home/${DEPLOY_USER}/.ssh/github -C "${DEPLOY_USER}@\$(hostname)"
      sudo cat /home/${DEPLOY_USER}/.ssh/github.pub

    Paste that into the repository: Settings > Deploy keys > Add, read-only.
    Then add to /home/${DEPLOY_USER}/.ssh/config:

      Host github.com
        IdentityFile ~/.ssh/github
        IdentitiesOnly yes

    and re-run this script with the SSH form of the URL:
      git@github.com:OWNER/REPO.git

    A deploy key is read-only and scoped to one repository. A personal access
    token on a server is neither.

CLONE
    die "clone failed - see above"
  fi
  note "cloned into ${APP_DIR}"
fi
chown -R "${DEPLOY_USER}:${DEPLOY_USER}" "${APP_DIR}"

# ...except the anchor directory, which belongs to the CONTAINER user.
#
# infra/Dockerfile runs the application as uid 10001. That uid owns /app/anchor
# inside the image, but compose bind-mounts the host directory over it and a bind
# mount carries the HOST ownership - so after the chown above, uid 10001 cannot
# write the one directory it is supposed to write. The simulator then dies on
#
#     PermissionError: [Errno 13] Permission denied: 'anchor/live_anomalies.jsonl'
#
# and `restart: always` turns that into a crash loop that still publishes
# readings, so the stack looks alive while the live anomaly ground truth is never
# written. The integrity anchor F5 rests on lives here too.
#
# Numeric on purpose: 10001 is a uid inside the image and need not exist on the
# host, so there is no name to resolve it by.
# Owner is the deploy user, GROUP is the container uid, and the directory is
# group-writable. Both need it and for different reasons:
#
#   the container (uid 10001) writes the integrity anchor and the live ground
#   truth into this directory on every run;
#
#   git (running as the deploy user) tracks anchor/.gitkeep, so a checkout sync
#   has to be able to create it. Handing the directory to 10001 outright makes
#   `git reset --hard` fail with "unable to create file anchor/.gitkeep", and
#   deploy.sh then aborts with "could not sync the checkout" - a deploy that
#   stops on a permission bit, having changed nothing.
#
# setgid so anything created here keeps the group, and numeric because 10001 is
# a uid inside the image that need not exist on the host.
install -d "${APP_DIR}/anchor"
chown -R "${DEPLOY_USER}:10001" "${APP_DIR}/anchor"
chmod 2775 "${APP_DIR}/anchor"
note "anchor/ is ${DEPLOY_USER}:10001, mode 2775 - writable by the host AND the container"

# --------------------------------------------------------------------------
log "Secrets"
# --------------------------------------------------------------------------
if [ -f "${APP_DIR}/.env" ]; then
  note ".env already exists - left alone (regenerating invalidates every signature)"
else
  sudo -u "${DEPLOY_USER}" python3 "${APP_DIR}/scripts/setup_env.py" \
    || die "could not generate .env - run scripts/setup_env.py by hand"
  note "generated"
fi
chmod 600 "${APP_DIR}/.env"
chown "${DEPLOY_USER}:${DEPLOY_USER}" "${APP_DIR}/.env"
note "mode 600 - it holds the signing key"

# --------------------------------------------------------------------------
log "Broker credentials"
# --------------------------------------------------------------------------
#
# infra/mosquitto/passwd holds PBKDF2-SHA512 hashes of the password setup_env.py
# just generated, so it is gitignored and cannot arrive with the checkout. Generating
# .env without it leaves the host one file short of being able to start: compose
# bind-mounts that path into the broker, Docker creates a DIRECTORY where the file
# should be, mosquitto exits before its first health check and everything that
# depends on it reports `dependency failed to start`. That reads as a broken release
# and is a missing file - it cost the deploy drill exactly this, in CI.
if [ -f "${APP_DIR}/infra/mosquitto/passwd" ]; then
  note "already present - left alone"
else
  sudo -u "${DEPLOY_USER}" python3 "${APP_DIR}/scripts/setup_mqtt_auth.py"     || die "could not generate the broker password file - is Docker running?"
fi

# --------------------------------------------------------------------------
log "Tailscale"
# --------------------------------------------------------------------------
if ! command -v tailscale >/dev/null; then
  curl -fsSL https://tailscale.com/install.sh | sh >/dev/null
fi
note "installed. Join the network with:"
note "    sudo tailscale up --ssh --hostname=gemp-staging"
note "Then take the machine's Tailscale IP from: tailscale ip -4"

# --------------------------------------------------------------------------
log "Firewall"
# --------------------------------------------------------------------------
#
# Deny inbound by default. The pipeline arrives over the tailscale0 interface, which
# is authenticated before a packet reaches sshd - so the SSH brute-force surface that
# every public host spends its life fighting does not need to exist here.
#
# BUT: port 22 stays open until Tailscale is actually up.
#
# Enabling a tailscale0-only policy while you are still connected over the public IP
# closes the door you are standing in. On a cloud VM that means recovering through
# the serial console, which is a bad evening. So the lockdown is conditional on
# Tailscale genuinely being connected, and the last step is something you run
# deliberately once you have confirmed the new path works.
ufw --force reset >/dev/null 2>&1 || true
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow in on tailscale0 >/dev/null

if ip -4 addr show tailscale0 2>/dev/null | grep -q 'inet '; then
  ufw --force enable >/dev/null
  note "tailscale0 is up - inbound denied except over Tailscale"
  note "port 22 is NOT open to the internet"
else
  # Safety net, and it is explicitly temporary.
  ufw allow 22/tcp >/dev/null
  ufw --force enable >/dev/null
  note "Tailscale is not connected yet, so port 22 has been left OPEN so that this"
  note "session survives. That is a temporary state, not the design."
  note ""
  note "Once 'tailscale up' has worked and you can reach this host over the tailnet:"
  note "    sudo ufw delete allow 22/tcp     # close the public door"
  note "    sudo ufw status verbose          # confirm only tailscale0 is allowed"
fi

note ""
note "NOTE: the cloud provider has its own firewall as well - an Azure Network"
note "      Security Group, an Oracle security list. Neither needs any inbound rule"
note "      for Tailscale, so leave them closed."

# --------------------------------------------------------------------------
log "SSH authorisation for the pipeline"
# --------------------------------------------------------------------------
cat <<KEYS

    On your laptop, generate the key the pipeline will use:

      ssh-keygen -t ed25519 -C gemp-deploy -f gemp-deploy -N ""

    Put the PRIVATE half in the repository secret GEMP_SSH_KEY, then append the
    PUBLIC half to /home/${DEPLOY_USER}/.ssh/authorized_keys on this host,
    prefixed exactly as in infra/deploy/authorized_keys.example:

      command="${APP_DIR}/infra/deploy/deploy-wrapper.sh",no-agent-forwarding,\\
no-port-forwarding,no-pty,no-user-rc,no-X11-forwarding ssh-ed25519 AAAA... gemp-deploy

    That forced command is the boundary. Without it the key is a root shell,
    because ${DEPLOY_USER} is in the docker group.

KEYS

# --------------------------------------------------------------------------
log "Done"
# --------------------------------------------------------------------------
cat <<DONE
    Host:        $(hostname)
    Checkout:    ${APP_DIR}
    User:        ${DEPLOY_USER}
    Architecture: $(uname -m)

    Remaining, in order:
      1. sudo tailscale up --ssh --hostname=gemp-staging
      2. install authorized_keys as printed above
      3. set the repository secrets and variables (see docs/PIPELINE.md)
      4. Actions > deploy > Run workflow > target: staging

    Nothing is running yet, deliberately: the first thing this host runs should be
    a release the pipeline built and signed, not one bootstrapped by hand.
DONE
