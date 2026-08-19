# The commands worth having one name for.
#
# Everything here also exists as a longer command somebody could type; the point is
# that the pipeline and a person run the SAME one. A deploy that only CI knows how
# to do is a deploy nobody can perform at eight in the morning on the day of a
# demonstration.

SHELL := /bin/bash
.DEFAULT_GOAL := help

PY ?= python
IMAGE ?= ghcr.io/$(shell git config --get remote.origin.url | sed -E 's#.*github.com[:/]##; s#\.git$$##')

.PHONY: help
help:  ## What each target does
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[1m%-16s\033[0m %s\n", $$1, $$2}'

# --------------------------------------------------------------------- checks

.PHONY: check
check:  ## The four gates, exactly as CI runs them
	$(PY) scripts/check.py

.PHONY: security
security:  ## The security review locally: same scanners, same verdict as CI
	@mkdir -p reports
	-bandit -r src scripts -f sarif -o reports/bandit.sarif
	-semgrep scan --config p/python --config p/security-audit --config p/secrets \
	  --sarif --output reports/semgrep.sarif --metrics off src scripts
	-pip-audit --format json --output reports/pip-audit.json --progress-spinner off
	-trivy fs --scanners vuln,secret,misconfig --format sarif -o reports/trivy-fs.sarif .
	$(PY) scripts/security_gate.py reports

.PHONY: security-tools
security-tools:  ## Install the scanners the security target needs
	pip install "bandit[sarif]" semgrep pip-audit
	@command -v trivy >/dev/null || echo "trivy not installed: https://aquasecurity.github.io/trivy"

# ---------------------------------------------------------------- local stack

.PHONY: up
up:  ## Build and start the whole stack from this checkout
	docker compose up -d --build
	@$(MAKE) --no-print-directory wait

.PHONY: down
down:  ## Stop everything, keep the data
	docker compose down

.PHONY: logs
logs:  ## Follow the API log
	docker compose logs -f core

.PHONY: wait
wait:  ## Block until the stack answers its health check
	@echo "waiting for health…"
	@for i in $$(seq 1 60); do \
	  if curl -fsS --max-time 5 http://localhost:8080/health 2>/dev/null | grep -q '"database":"ok"'; then \
	    echo "healthy"; exit 0; fi; sleep 5; done; \
	  echo "did not become healthy" >&2; docker compose logs --tail 40 core; exit 1

.PHONY: rebuild
rebuild:  ## Rebuild the application image and restart it (src/ is baked, not mounted)
	docker compose build core sim
	docker compose up -d core sim
	docker compose restart nginx
	@$(MAKE) --no-print-directory wait

# -------------------------------------------------------------------- deploy

.PHONY: deploy
deploy:  ## Deploy a published image here. make deploy REF=ghcr.io/owner/repo@sha256:…
	@test -n "$(REF)" || { echo "REF is required, e.g. make deploy REF=$(IMAGE):latest" >&2; exit 2; }
	infra/deploy/deploy.sh "$(REF)"

.PHONY: rollback
rollback:  ## Put the last known-good image back. Optionally: make rollback REF=…
	infra/deploy/rollback.sh $(REF)

.PHONY: verify
verify:  ## Check an image was signed by this repository's ci workflow
	@test -n "$(REF)" || { echo "REF is required" >&2; exit 2; }
	cosign verify "$(REF)" \
	  --certificate-identity-regexp '^https://github.com/.+/\.github/workflows/ci\.yml@' \
	  --certificate-oidc-issuer https://token.actions.githubusercontent.com

.PHONY: backup
backup:  ## Back up the database, the anchor file and the signing key
	infra/deploy/backup.sh $(DEST)

.PHONY: backup-verify
backup-verify:  ## Restore the newest backup into a scratch database and check it
	@d=$$(ls -d .deploy/backups/*/ 2>/dev/null | tail -1); 	 test -n "$$d" || { echo "no backups yet - run make backup" >&2; exit 2; }; 	 infra/deploy/restore.sh "$$d" --into gemp_restore_test

.PHONY: restore
restore:  ## Restore a backup over the live database. make restore DIR=.deploy/backups/…
	@test -n "$(DIR)" || { echo "DIR is required" >&2; exit 2; }
	infra/deploy/restore.sh "$(DIR)"

.PHONY: released
released:  ## What is running here right now
	@docker compose ps --format 'table {{.Service}}\t{{.Image}}\t{{.Status}}'
	@echo
	@echo "last good image:  $$(cat .deploy/last-good-image 2>/dev/null || echo 'none recorded')"
	@echo "last good commit: $$(cat .deploy/last-good-commit 2>/dev/null || echo 'none recorded')"
	@echo "newest backup:    $$(ls -d .deploy/backups/*/ 2>/dev/null | tail -1 || echo 'none')"
