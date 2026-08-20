# Single image for both the API process and the simulator; they differ only in the
# command they run. One image means one build, one dependency set, and no chance of
# the simulator and the ingester disagreeing about the message schema.

FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Debian security updates, applied at build time.
#
# python:3.11-slim is rebuilt on Docker's schedule, not on Debian's, so between
# rebuilds it carries every OS package fix published since - 69 of them when this
# was written, all in the util-linux family. Nothing in this project can patch those
# except pulling them here, and the security gate blocks on a fixable vulnerability
# whether or not it is ours. `upgrade`, not `dist-upgrade`: security fixes, not a
# release change under the build's feet.
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Dependencies first so a source edit does not invalidate the wheel download layer.
COPY pyproject.toml README.md ./
COPY src ./src
# Pinned, not floating. `pip install --upgrade pip` resolves to whatever was
# published the morning the build ran, which makes two builds of the same commit two
# different images; and the setuptools that ships inside python:3.11-slim is 65.5.0,
# which pip-audit reports against seven advisories including a path traversal in
# PackageIndex. Upgrading it here is the fix - the base image is not going to.
RUN pip install --no-cache-dir --upgrade \
        "pip==26.2.1" "setuptools==84.0.0" "wheel==0.48.0" \
    && pip install --no-cache-dir .

# The build tooling goes, now that there is nothing left to build.
#
# This is what cleared the last three fixable findings in the image scan, and none
# of them were reachable any other way: pip vendors its own copies of msgpack and
# setuptools under `pip/_vendor/`, trivy reads them out of `vendor.txt` as installed
# packages, and their versions are pinned by pip's release rather than by anything
# this project can pass to it. The only way to stop shipping a vulnerable msgpack is
# to stop shipping the package manager.
#
# Removing them is also just correct for a runtime image. Nothing here installs
# packages after this line, nothing in site-packages imports pkg_resources - it is
# gone from setuptools 81 onward in any case - and the only importers of setuptools
# itself are numpy's distutils build commands, which run when numpy is compiled and
# never when it is used. Verified against the built image, not assumed.
RUN python -m pip uninstall --yes pip setuptools wheel

COPY data ./data

# The schema, shipped WITH the code that expects it.
#
# Migrations are run from inside this image at deploy time - `alembic upgrade head`
# through `docker compose run` - so the migration and the application that needs it
# are the same artefact and cannot be at different versions. Left on the host
# instead, they would be whatever the checkout happened to hold.
COPY alembic.ini ./
COPY migrations ./migrations

# Run unprivileged. The container writes only to /app/anchor, which is bind-mounted.
RUN useradd --create-home --uid 10001 gemp \
    && mkdir -p /app/anchor \
    && chown -R gemp:gemp /app
# By UID, not by name. A name has to be resolved against the image's own passwd
# file, so a runtime that enforces `runAsNonRoot` cannot tell whether `gemp` is root
# until it is too late to refuse; a numeric id is checkable before the container
# starts. Same account either way - `useradd` above created it with this uid.
USER 10001

EXPOSE 8000

# The API's own liveness probe, so the image carries it rather than relying on
# every caller to know the URL. `python -c` rather than curl because this base image
# has no curl and adding one to run a health check would be a package installed for
# the benefit of the health check.
#
# `sim` runs from this same image with a different command and serves no HTTP, so
# compose disables the check for that service explicitly. Inherited, it would report
# a working simulator as permanently unhealthy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=4)"]

CMD ["uvicorn", "gemp.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
