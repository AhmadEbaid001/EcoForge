# A free server, and how to prove the pipeline works on it

The demonstration host is still the laptop. This is a second machine to run the
pipeline against end to end, so that the first time a deploy happens is not the
morning of 31 August.

## What it has to be

Measured on the running stack, not estimated:

| | In use | What to provision |
|---|---|---|
| RAM | 1.7 GB, of which TimescaleDB is 1.46 GB | **4 GB** — CP-SAT bursts to eight search workers |
| Disk | 3.16 GB and growing at 720× | **20 GB+** |
| CPU | idles low, spikes during a solve | **2+ vCPU** |

That rules out most free tiers on the first line. AWS `t2.micro`, GCP `e2-micro` and
Azure `B1s` are all 1 GB, and the last two stop being free after twelve months.

## Without a credit card

Every mainstream cloud — Oracle, AWS, GCP, Azure, Hetzner, DigitalOcean — asks for a
card for identity verification, even where nothing is charged. If you do not have
one, these three are the real options, and the first two need no account at all.

### 1. The drill: no server, no account, no card

`.github/workflows/deploy-drill.yml`. The GitHub runner **is** the host: 4 cores,
16 GB, Docker, thrown away afterwards — which makes it a better place to test a
rollback than a machine you care about.

It runs on every pull request touching the deploy scripts, weekly on a schedule, and
on demand. In one job it:

1. builds the release, and a second image that starts and then dies
2. brings up the database and broker, as a real host would already have them
3. deploys the good release — migrations, swap, health, smoke
4. checks the schema was actually migrated, not assumed
5. deploys the broken one and **requires it to fail**
6. checks the previous release is serving again, by image identity
7. checks a failed deploy did not move the last-good pointer
8. takes a backup and restores it into a scratch database

That is everything except the SSH transport. Start here regardless of what else you
do: it needs nothing, and it keeps working after you stop paying attention to it.

### 2. Your own machine, reached over Tailscale

The most honest "real server" you have without a card is a machine you already own —
the laptop, or the VM mirror the project already plans for. Tailscale's free tier
needs no card, and the deploy workflow already supports it.

This gives you the *complete* pipeline including SSH: runner joins the tailnet, SSHes
in, `deploy.sh` runs on your machine. Nothing is exposed to the internet; the host
needs no public IP and no open port. The only cost is that the machine has to be
awake when you deploy, which for a test target is not a cost.

Run `infra/deploy/bootstrap.sh` on a Linux VM (VirtualBox, WSL2, or the mirror VM),
then follow the Tailscale section below — every step applies unchanged.

### 3. If you are a student

The **GitHub Student Developer Pack** verifies with a student ID or enrolment
document, **not a card**, and includes DigitalOcean and Azure credit. **Azure for
Students** separately gives $100 and, unusually, requires no card at all. For a team
entering RoboDam2026 this is likely the fastest route to a real always-on box.

Neither is permanent — the credit runs out — so treat it as a bridge rather than a
home, and keep the drill as the thing that always works.

## Azure, with the $200 trial or the $100 student credit

The standard free account gives **$200 over 30 days** and asks for a card for
identity verification (a debit card works). Azure for Students gives **$100 over 12
months** with **no card**, but needs a university email. Either is enough — a B2s at
roughly $30/month is about $1 a day, and the credit is not auto-charged when it ends:
Azure asks whether you want to continue, and disables the resources if you decline.

Create the VM:

- **Image**: Ubuntu Server 22.04 or 24.04 LTS
- **Size**: `B2s` — 2 vCPU, 4 GiB. That is the measured requirement, not a guess.
- **Inbound ports**: **none**. Uncheck SSH (22) as well; Tailscale is how you get in,
  and leaving 22 open to the internet is the thing this design avoids.
- **OS disk**: the default 30 GiB is *tight* — the database volume alone is already
  3.2 GB and grows at 720×. Take 64 GiB, or add a data disk and move
  `/var/lib/docker` onto it.
- **Authentication**: SSH public key. Azure calls the default login `azureuser`;
  bootstrap creates its own `gemp` user regardless.

Two Azure-specific things worth knowing:

- The **Network Security Group** is a separate firewall from `ufw` on the box. Leave
  it closed. Tailscale needs no inbound rule at all — it makes an outbound connection.
- **`ufw` does not protect a published container port, and the NSG is what does.**
  compose publishes nginx as `8080:80`, which binds every interface including the
  public one. Docker inserts its own rules ahead of ufw's INPUT chain, so a
  `deny incoming` policy does not cover it — a well-known interaction, not a
  misconfiguration here. Verified from outside on this deployment: with the NSG
  empty, both 8080 and 22 refuse. That is the NSG alone. Open one inbound rule and
  the dashboard is on the internet whatever ufw says, so if you ever need a public
  port, publish it as `127.0.0.1:8080:80` and put something in front of it.
- **Deallocate when you are not testing.** Portal → Stop, and confirm it reads
  **"Stopped (deallocated)"**, not just "Stopped". The first halts compute billing;
  the second keeps charging. This is what turns $200 into months rather than weeks.

Set a budget alert first: Cost Management → Budgets → $50 with an email alert.

## Oracle Cloud Always Free

**Ampere A1 (ARM): up to 4 OCPU, 24 GB RAM, 200 GB storage, no expiry.** The only
mainstream always-free tier big enough to run this honestly — **if you have a card
to verify with**. If you do not, use the three options above; this section is here
for when that changes.

Three things to know before you start:

- **It is ARM.** `ci` now builds `linux/amd64` and `linux/arm64`, so the same digest
  runs on the laptop and on the server. Every dependency that matters here — ortools,
  scikit-learn, pandas, numpy, psycopg — publishes a Linux aarch64 wheel for cp311,
  so nothing compiles from source.
- **A card is required for identity verification.** Always Free resources are not
  charged, but it is asked for at signup.
- **"Out of capacity" is common** for A1 in busy regions, and your home region cannot
  be changed afterwards. Pick a quieter one.

Create the instance: Ubuntu 22.04 or 24.04, shape `VM.Standard.A1.Flex`, 2 OCPU and
6 GB is plenty (you may as well take 4/24, it costs nothing). You do **not** need to
open any ingress ports.

If Oracle refuses you, Google Cloud's $300 / 90-day credit on an `e2-medium` is a
real fallback — but it expires, so it is a bridge, not a home.

## Bootstrap

```bash
curl -fsSL https://raw.githubusercontent.com/OWNER/REPO/main/infra/deploy/bootstrap.sh -o bootstrap.sh
less bootstrap.sh                       # it asks for root; read it first
sudo bash bootstrap.sh git@github.com:OWNER/REPO.git
```

It installs Docker, creates the `gemp` deploy user, clones the repository, generates
`.env` at mode 600, installs Tailscale, and sets `ufw` to deny everything inbound
except on `tailscale0`. It starts nothing — the first thing the host runs should be
a release the pipeline built and signed.

For a private repository it prints the deploy-key procedure. Use a **deploy key**,
not a personal access token: read-only, and scoped to one repository.

## Reaching it without opening a port

Tailscale, free tier (100 devices). The runner joins your private network for the
length of the deploy job and connects over it, so the host never has a public SSH
listener.

On the server:

```bash
sudo tailscale up --ssh --hostname=gemp-staging
tailscale ip -4          # this is GEMP_SSH_HOST
```

In the Tailscale admin console:

1. **Access controls** → add a tag for CI and let it reach the server:
   ```json
   "tagOwners": { "tag:ci": ["autogroup:admin"] },
   "acls": [
     { "action": "accept", "src": ["tag:ci"], "dst": ["gemp-staging:22"] }
   ]
   ```
2. **Settings → OAuth clients** → generate one with scope `auth_keys`, tag `tag:ci`.

An OAuth client rather than a reusable auth key: it is scoped to a tag, it can be
revoked on its own, and it does not sit in a repository secret as a credential that
never expires.

## Repository configuration

Settings → Secrets and variables → Actions.

**Secrets**

| Name | Value |
|---|---|
| `GEMP_SSH_KEY` | private half of `ssh-keygen -t ed25519 -C gemp-deploy -f gemp-deploy -N ""` |
| `GEMP_SSH_KNOWN_HOSTS` | `ssh-keyscan -t ed25519 <tailscale-ip>` run from a machine already on the tailnet |
| `GEMP_SSH_USER` | `gemp` |
| `GEMP_SSH_HOST` | the Tailscale IP |
| `TS_OAUTH_CLIENT_ID` | from the Tailscale console |
| `TS_OAUTH_SECRET` | from the Tailscale console |

**Variables**

| Name | Value |
|---|---|
| `GEMP_USE_TAILSCALE` | `true` |
| `GEMP_REMOTE_DIR` | `/srv/gemp` |
| `GEMP_URL` | `http://<tailscale-ip>:8080` |
| `GEMP_AUTO_DEPLOY` | `true` |

`GEMP_AUTO_DEPLOY` is deliberately the LAST thing you set. Until it is `true` the
deploy workflow does not run itself on a green `ci`; set it only once the secrets
above exist and a manual dispatch has succeeded at least once. Without that switch
every green build on `main` tried to ssh to a host that had not been built yet,
failed, and opened an issue saying the deploy was broken - which it was not.
Manual dispatch ignores this variable, so it can never block a deploy you asked for.

Then put the public half of `gemp-deploy` into the server's
`/home/gemp/.ssh/authorized_keys`, prefixed with the forced command from
`infra/deploy/authorized_keys.example`. That prefix is the privilege boundary, not a
nicety: the `gemp` user is in the `docker` group, which is root-equivalent.

Create the `staging` environment under Settings → Environments and give it these
secrets. Leave `production` for the laptop, later.

## Testing the pipeline

In order, because each step tells you something different when it fails.

**1. Does the image build for ARM?** Push to a branch and open a pull request. `ci`
runs the gates and the security review but does not build — that is on purpose. Merge
it and watch `build and sign`; the multi-arch build is the slow part.

**2. Does the runner reach the host?** Actions → **deploy** → Run workflow → target
`staging`, digest blank. Watch *Join the private network* and *Deploy over SSH*. If
it hangs here it is ACLs, not your key.

**3. Does the host deploy?** The same run continues into `deploy.sh` on the server.
It syncs the checkout, migrates, swaps, health-checks and smoke-tests. On the server:

```bash
cd /srv/gemp && make released
tail -40 .deploy/deploy.log
```

**4. Does the rollback fire?** This is the one worth doing deliberately, because it
is the only part you cannot test by succeeding. On the server:

```bash
docker tag <the-image-you-just-deployed> gemp-core:v-broken
printf 'FROM gemp-core:v-broken\nCMD ["python","-c","import sys; sys.exit(1)"]\n' \
  | docker build -t gemp-core:v-broken -
GEMP_HEALTH_TIMEOUT_S=45 ./infra/deploy/deploy.sh gemp-core:v-broken
```

Expect: health check fails, the previous release is restored, exit code 1, and the
site is serving again. That exact test found a real bug in this script during
development — read the note in `bring_up` before changing anything there.

**5. Does the security gate stop a deploy?** Add a finding on a branch and watch
`secure code review` fail and `build` never run. A throwaway `assert` in a source
file is enough for bandit to object.

**6. Do backups restore?** On the server:

```bash
make backup && make backup-verify
```

The verify restores into a scratch database, counts rows *and constraints*, and drops
it. It fails loudly if the dump is not loadable — which is how the TimescaleDB
restore bug in this repository was found.

## Refreshing the history on the server

The simulator advances data time at `GEMP_SIM_SPEED`, so a host left running for weeks
holds readings that end wherever that ratchet took it rather than at today. And the
portfolio in `data/buildings.geojson` reaches the database only when something imports
it, which is the seeder. One operation fixes both:

```bash
gh workflow run reseed -f months=6 -f target=production
```

Or on the host, if you are already there:

```bash
infra/deploy/reseed.sh 6
```

It stops the simulator, wipes the readings and the integrity checkpoints, re-imports the
portfolio and the catalog, generates a fresh window that ends at this moment, restarts
the API so its anomaly window warms from the new history, starts the simulator again,
and health-checks before it reports success. Accounts, sessions, the audit log and
stored allocations survive it.

Two things worth knowing before running it during a demonstration week:

- **It destroys the readings.** A stored allocation still verifies against its own input
  hash, but the meter history those readings represented is gone. Take a backup first if
  the current history matters: `make backup` on the host.
- **It takes minutes, not seconds.** Six months is about nine hundred thousand signed
  readings for fifty buildings; sixty months is ten times that. The workflow allows an
  hour, and the API is down only for the restart at the end.

## What this does not change

The demonstration still runs on the laptop, air-gapped, per the existing plan. This
server exists so the pipeline is exercised somewhere real first. Nothing about F13 or
the offline guarantee moves: `deploy.sh` still accepts an image loaded from a file
with `docker load`, and the stack still runs with the cable out.
