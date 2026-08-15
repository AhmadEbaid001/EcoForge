-- Runs once, on first initialisation of the data volume.
--
-- Grafana gets its own role with SELECT and nothing else. A dashboard has no reason
-- to be able to write, and the integrity claim is weaker if any process with a
-- connection string can edit the readings it is meant to be attesting to.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gemp_ro') THEN
        EXECUTE format('CREATE ROLE gemp_ro LOGIN PASSWORD %L',
                       coalesce(current_setting('gemp.ro_password', true), 'gemp_ro'));
    END IF;
END
$$;

GRANT CONNECT ON DATABASE gemp TO gemp_ro;
GRANT USAGE ON SCHEMA public TO gemp_ro;

-- Applies to tables created later by Alembic, not just those existing now.
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO gemp_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO gemp_ro;

-- TimescaleDB ships enabled in this image; this is belt and braces for a rebuild
-- onto a plain postgres image.
CREATE EXTENSION IF NOT EXISTS timescaledb;
