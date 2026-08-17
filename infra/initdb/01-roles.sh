#!/bin/bash
# Runs once, on first initialisation of the data volume.
#
# A shell script rather than plain SQL because the read-only password comes from the
# environment, and a .sql file cannot read one. The previous version tried
# current_setting('gemp.ro_password', true), a custom setting nothing ever set, so it
# silently fell back to a literal - and Grafana then failed to authenticate with the
# generated password while every dashboard rendered empty and said nothing about why.
set -euo pipefail

RO_USER="${GEMP_DB_RO_USER:-gemp_ro}"
RO_PASSWORD="${GEMP_DB_RO_PASSWORD:?GEMP_DB_RO_PASSWORD must be set}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<EOSQL
    DO \$\$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${RO_USER}') THEN
            CREATE ROLE ${RO_USER} LOGIN PASSWORD '${RO_PASSWORD}';
        ELSE
            ALTER ROLE ${RO_USER} WITH LOGIN PASSWORD '${RO_PASSWORD}';
        END IF;
    END
    \$\$;

    GRANT CONNECT ON DATABASE ${POSTGRES_DB} TO ${RO_USER};
    GRANT USAGE ON SCHEMA public TO ${RO_USER};

    -- Grafana reads and never writes. A dashboard has no business being able to
    -- edit the readings it is attesting to.
    ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO ${RO_USER};
    GRANT SELECT ON ALL TABLES IN SCHEMA public TO ${RO_USER};

    CREATE EXTENSION IF NOT EXISTS timescaledb;
EOSQL
