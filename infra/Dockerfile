# Single image for both the API process and the simulator; they differ only in the
# command they run. One image means one build, one dependency set, and no chance of
# the simulator and the ingester disagreeing about the message schema.

FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so a source edit does not invalidate the wheel download layer.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --upgrade pip && pip install .

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
USER gemp

EXPOSE 8000

CMD ["uvicorn", "gemp.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
