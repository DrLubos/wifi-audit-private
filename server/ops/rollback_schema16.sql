-- Server schema 16 -> 15: drops refresh_runs (its rows - the recorded refresh
-- durations - are lost; export them first: evaluation/pipeline/export.sh).
-- Deploy the schema-15 refresher first (it skips writing while the table is
-- missing, so the order is not critical). ONE transaction, as the database
-- owner, from server/:
--
--   docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 < ops/rollback_schema16.sql
\set ON_ERROR_STOP on
BEGIN;
DO $$
BEGIN
  IF (SELECT value FROM schema_meta WHERE key = 'schema_version') IS DISTINCT FROM '16' THEN
    RAISE EXCEPTION 'rollback_schema16: expected schema_version 16';
  END IF;
END $$;
DROP TABLE refresh_runs;
INSERT INTO schema_meta (key, value) VALUES ('schema_version', '15')
  ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
COMMIT;
