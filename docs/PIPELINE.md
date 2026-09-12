# The pipeline

One click from a merged pull request to a running deployment, with a security
review that can stop it and a host that can put itself back.

```
  push / PR ──► ci ──┬── gates            four checks, same as `make check`
                     ├── secure review    every scanner → one verdict
                     └── build            image, signed, SBOM attested
                            │
                            ▼  (main only, ci green)
                        deploy ──► verify signature ──► ssh
                                                         │
  ─────────────────────────────────────────────────────  │  ── on the host ──
                                                         ▼
                            sync checkout to the release commit
                                          │
                            migrate  (fails here = nothing swapped)
                                          │
                            swap core + sim, restart nginx
                                          │
                            health ──► smoke ─┬── passes ──► recorded as last good
                                              │
                                              └── fails ───► roll back image
                                                             AND commit
```

## What blocks

The policy is **block on anything**. Any finding from any scanner fails `ci`, and
a failed `ci` cannot deploy — the deploy workflow is triggered by the ci run and
reads its conclusion, so pushing straight to `main` does not skip the review.

Scanners, and what each is for:

| Tool | Looks at |
|---|---|
| ruff | style and dead-import drift (a gate, not a security tool) |
| bandit | Python code patterns — subprocess, weak hashes, asserts in production paths |
| semgrep | deeper SAST: OWASP top ten, injection, secrets in code |
| pip-audit | advisories against our declared dependencies |
| gitleaks | secrets in the tree **and in history** — a deleted secret was still published |
| hadolint | the Dockerfile |
| trivy | filesystem, IaC/compose misconfiguration, and the built image |

Every scanner runs with `continue-on-error`, deliberately. A scanner that crashes
must not be indistinguishable from a scanner that found nothing;
`scripts/security_gate.py` reads all their reports afterwards and is the only step
allowed to fail the job. It treats an unreadable report as a failure, not as an
absence of findings.

### The one thing that does not block: advisories with no fix

`block on anything` means anything that can be acted on. The base image carries
roughly 120 Debian advisories with no patched version published anywhere — not
"not upgraded yet", but nothing to upgrade *to*. Blocking on those makes red the
permanent state of the build, and a gate that is always red is one everybody
learns to click past; the exception register cannot absorb them either, because
120 entries that all say "upstream has not shipped a fix" is a rubber stamp with
a date on it.

So the two trivy scans that feed the gate pass `--ignore-unfixed`, and the
Dockerfile applies `apt-get upgrade` at build time — which means a fix becomes
*this build's problem* the moment Debian publishes one. That combination took the
image from 69 fixable findings to zero.

Nothing is hidden. A third scan records every finding including the unfixable ones
into `reports-informational/`, which is uploaded to the Security tab under the
`informational` category and kept as a build artefact. The gate does not read that
directory. When upstream ships a fix, the finding moves from that list into the
blocking one on its own.

The runtime image also ships no pip, setuptools or wheel. Removing them was the
only way to clear the last three: pip vendors its own msgpack and setuptools under
`pip/_vendor/`, pinned by pip's release rather than by anything this project can
pass to it.

Run exactly the same review locally before you push:

```bash
make security-tools   # once
make security
```

## Getting past a finding

`.security/allowlist.yml`, reviewed in a pull request like any other change. Every
entry names the finding, who accepted it, why, and **the date the acceptance runs
out**. An expired entry fails the build on its own, whether or not the finding is
still there.

That expiry is the mechanism, not a formality. An exception without one is a policy
change made by whoever was on shift, and it outlives every memory of why it was
made. The alternative — a `|| true` added to a workflow step at eleven at night —
is what actually happens to strict gates that have no honest way out.

## Deploying

Automatic on every green `ci` run on `main`. Manual from the Actions tab
(**deploy** → Run workflow), optionally with a digest, which is how you roll
forward to a specific known-good build without reverting anything.

Locally, on the host:

```bash
make deploy REF=ghcr.io/OWNER/REPO@sha256:…   # same script the pipeline runs
make rollback                                  # back to the recorded last-good
make released                                  # what is live right now
```

The pipeline and a person run the *same* script. A deploy only CI knows how to
perform is a deploy nobody can do at eight in the morning on the day of a
demonstration.

## A release is two things

The **image** carries the application, its dependencies and its migrations. The
**commit** carries `docker-compose.yml`, the nginx config and the deploy scripts.
Both travel together and both roll back together.

Shipping only the image was a real hole: change the compose file and the host would
keep running the old one against the new image, silently, forever. `deploy.sh` now
syncs the checkout to the commit the image was built from, and re-execs itself from
`/tmp` first — because that sync rewrites the running script, and bash reads a
script incrementally rather than all at once.

## Migrations

`alembic upgrade head`, run from **inside the new image** through
`docker compose run`, before the new containers serve anything. The migration and
the application that needs it are the same artefact and cannot be at different
versions. A failure aborts before the running containers are touched — the cheapest
possible failure, because nothing has changed yet.

**Forward only.** There is no automatic downgrade and there must not be: a downgrade
that drops a column destroys the data in it, and the rollback would become the thing
that loses the readings. So a schema change has to be compatible with the release
*before* it — add columns, don't rename them, drop the old ones a release later.
That expand-then-contract discipline is what makes rolling back safe.

## Rollback

`infra/deploy/deploy.sh` runs **on the host**, not on the runner. That is the whole
design: a rollback that needs the CI runner to still be alive is not a rollback.

1. Resolve the reference to a digest — a tag can be moved, an image cannot
2. Pull it, or use the copy already on the host (the offline path)
3. Record what is running now
4. Swap `core` and `sim`, restart `nginx` — it resolves the upstream hostname once
   at startup, so a recreated core otherwise leaves every request answering 502
5. Migrate, from inside the new image
6. Health-check, then smoke-test
7. On failure: restore the recorded image *and commit*, verify that, exit non-zero

The database and the broker are never touched. They are not being deployed.

### What counts as "it works"

`/health` proves the process is up and the database answers. It does not prove the
release is any good — a build with a broken front end, a stale nginx upstream, or an
auth middleware that fell open passes it without noticing. So three more checks run
after it, each failing for a different real reason:

| Check | Catches |
|---|---|
| `/` serves the app shell | broken nginx, missing web mount |
| `/api/v1/auth/session` says nobody is signed in | stale upstream 502, unreachable core |
| `/api/v1/meta` returns 401 | **auth middleware failing open** |

The third is the one worth having. An auth bug that lets everyone in makes every
other check pass — everything works, and works for everyone.

**Verified, not assumed.** A deliberately broken image was deployed to this host
during development: it failed its check, the previous image was restored
automatically, and the service came back. That test found a real bug — `sim`
depends on `core` being healthy, so compose exited non-zero and `set -e` killed the
script *before* the health check, making the rollback unreachable for the commonest
failure. Read the comment in `bring_up` before removing that error handling.

## Backups

Everything this platform demonstrates lives in one Docker volume: 7.5 million signed
readings, the anomaly ground truth the F9 claim is measured against, and every stored
allocation. Losing it is not an inconvenience — re-seeding produces a different
portfolio and destroys the ground truth the claims rest on.

```bash
make backup          # database + anchor file + signing key, with a manifest
make backup-verify   # restore the newest into a scratch database, then drop it
make restore DIR=…   # over the live database; asks you to type the database name
```

Three things are captured because restoring any two of them is not a restore: the
database, the anchor file (which lives outside the volume on purpose, and is what
proves the chain was not rebuilt along with the rows), and `.env` (which holds the
signing key — readings restored without it can never be verified again).

Schedule both with `infra/deploy/gemp-backup.cron.example`. The drill is on that
schedule too, deliberately: a backup nobody has restored is a hypothesis.

**The drill earns its place.** The first real run here restored all 7.5 million rows
and reported success while silently failing to recreate the foreign key from
`reading` to `building` — the constraint the ingester depends on to refuse readings
for unknown buildings. A plain `pg_restore` is not enough for a hypertable; the
restore now uses `timescaledb_pre_restore()` / `timescaledb_post_restore()` and
**asserts** on both the error count and the constraint, rather than printing "OK"
regardless. Second run: 0 errors, foreign key present.

One caveat worth stating: `.deploy/backups` is on the same disk as the database. That
covers corruption and a bad restore. It does not cover the disk failing or the laptop
being lost — pass a destination on other media, and take one onto a USB stick the day
before the demonstration.

## What you must provision

The deploy is over SSH and none of this exists yet.

**Repository secrets** (Settings → Secrets and variables → Actions):

| Secret | What |
|---|---|
| `GEMP_SSH_KEY` | private half of a deploy-only keypair |
| `GEMP_SSH_KNOWN_HOSTS` | `ssh-keyscan -t ed25519 your.host` — pins the host |
| `GEMP_SSH_USER` | the deploy user |
| `GEMP_SSH_HOST` | the host |

**Repository variables**: `GEMP_REMOTE_DIR` (checkout path on the host),
`GEMP_SSH_PORT` (optional), `GEMP_URL` (shown on the deployment).

**On the host**: the checkout at `GEMP_REMOTE_DIR`, a `.env`, and the deploy user's
`~/.ssh/authorized_keys` written per `infra/deploy/authorized_keys.example` — a
forced command through `infra/deploy/deploy-wrapper.sh`, which validates the image
reference and will run nothing else. That restriction is not defence in depth: the
deploy user must be in the `docker` group, which on any Linux host is equivalent to
root, so the forced command *is* the boundary.

**Environments**: create `staging` and `production` under Settings → Environments,
each with its own copy of the four secrets above. Manual runs default to `staging`,
so the careless click is the safe one. Give `production` a required reviewer and
every deploy there waits for approval; leave it open and it is one click.

The VM mirror this project already keeps is the natural staging host: deploy there,
look at it, then promote the *same digest* to the laptop.

**Branch protection** — this is the part that makes "block on anything" mean
anything, and I cannot set it from here. Settings → Rules → new ruleset on `main`:

- Require a pull request before merging, 1 approval
- Require review from Code Owners (`.github/CODEOWNERS` is committed)
- Require status checks: `gates`, `secure code review`
- Block force pushes
- Do **not** allow bypass for administrators — an exception for the person most
  likely to be in a hurry is not an exception, it is the normal path

Without these, the gates run on a direct push and nothing stops the push.

## Notification

A failed deploy opens a GitHub issue labelled `deploy`, with the image, the commit
and a link to the run. The host has already rolled itself back by then, which is
exactly why it needs saying out loud: the site is up and serving the *previous*
release, and without a notification the next thing anyone notices is demonstrating
yesterday's build. An issue rather than a chat webhook — no third party, no secret
to configure, and it survives the demonstration network.

## Known gaps

- **Actions are pinned to major tags, not commit SHAs.** SHA pinning is stricter and
  is the right end state; a wrong SHA is a workflow that cannot run at all, so it is
  a deliberate follow-up rather than a value guessed here. Dependabot watches the
  tags weekly.
- **The deploy path needs the network.** The runtime guarantee (F13) is unaffected —
  the stack still runs with the cable out — but shipping a new release to an
  air-gapped host means `docker save` / `docker load` and then `make deploy` with the
  loaded tag. `deploy.sh` handles that case; nothing automates the transfer.
- **The signing key is on the host in `.env`.** `deploy.sh` refuses to run without
  it and warns if it is not mode 600, but nothing provisions or rotates it —
  `scripts/setup_env.py` generates one, and regenerating invalidates every existing
  signature. That is a deliberate property, not a bug, and it means the key must be
  backed up with the data it signs.
- **The fifteen integration tests are not a gate anywhere.** `ci` runs
  `pytest -q --cov` with no `services:` block, so the MQTT and PostgreSQL integration
  tests skip in CI exactly as they skip on a laptop with nothing running. They are
  exercised only when somebody brings the stack up by hand and runs pytest against it.
  Every claim the evaluation harness checks is independent of them, and the four gates
  in `scripts/check.py` pass without them — but "525 passed" in a CI log means 525 of
  540, and it is worth knowing which fifteen are missing. Adding two service
  containers to the workflow is the fix; it was not done close to a deadline because a
  newly-running integration test that fails also blocks every deploy.
