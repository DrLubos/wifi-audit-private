-- Server schema 18 -> 17: drops what schema 18 added (the delivery records
-- and the per-poll configuration version are lost) and restores the schema-17
-- ingest functions. Deploy the schema-17 api first (an api that stages
-- config_version would fail against the old functions). ONE transaction, as
-- the database owner, from server/:
--
--   docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 < ops/rollback_schema18.sql
--
-- The schema-17 functions are those of git show 56c5857:server/schema.sql
-- (pipe that file's ingest_stage_create / ingest_merge after this script, or
-- the whole file: it is idempotent).
\set ON_ERROR_STOP on
BEGIN;
DO $$
BEGIN
  IF (SELECT value FROM schema_meta WHERE key = 'schema_version') IS DISTINCT FROM '18' THEN
    RAISE EXCEPTION 'rollback_schema18: expected schema_version 18';
  END IF;
END $$;
DROP TABLE sensor_config_attempts;
ALTER TABLE sensor_config DROP COLUMN sha256, DROP COLUMN delivered_at, DROP COLUMN refused_at,
  DROP COLUMN refused_reason;
ALTER TABLE sensor_status DROP COLUMN capabilities, DROP COLUMN capabilities_at;
DROP INDEX polls_seq;
ALTER TABLE polls DROP COLUMN config_version;
INSERT INTO schema_meta (key, value) VALUES ('schema_version', '17')
  ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
COMMIT;
