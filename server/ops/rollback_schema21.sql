-- Server schema 21 -> 20: drops ingest_state.taken_from / taken_since. Deploy
-- the schema-20 refresher first (the schema-21 one needs the columns, and
-- must not be in the middle of a refresh: stop it). A mark a refresh has
-- taken and not finished goes back into dirty_from before the columns go, so
-- no window is lost with them. ONE transaction, as the database owner, from
-- server/:
--
--   docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 < ops/rollback_schema21.sql
\set ON_ERROR_STOP on
BEGIN;
SET LOCAL lock_timeout = '10s';
DO $$
BEGIN
  IF (SELECT value FROM schema_meta WHERE key = 'schema_version') IS DISTINCT FROM '21' THEN
    RAISE EXCEPTION 'rollback_schema21: expected schema_version 21';
  END IF;
END $$;
UPDATE ingest_state SET dirty_from = least(dirty_from, taken_from), dirty_since = least(dirty_since, taken_since)
WHERE taken_from IS NOT NULL;
ALTER TABLE ingest_state DROP COLUMN taken_from, DROP COLUMN taken_since;
INSERT INTO schema_meta (key, value) VALUES ('schema_version', '20')
  ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
COMMIT;
