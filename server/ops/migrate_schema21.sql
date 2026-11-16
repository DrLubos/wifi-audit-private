-- Server schema 20 -> 21 (schema.sql, schema 21): ingest_state.taken_from /
-- taken_since - the dirty mark a refresh has taken stays in the table until
-- that refresh is done. It lived in the refresher's memory: a refresher that
-- died in the middle of a refresh (the stack restarted, the box rebooted)
-- lost the window it was refreshing. Two nullable columns on a table of one
-- row per sensor; nothing is rewritten and no other object changes.
-- ONE transaction: any error leaves schema 20. Run through
-- ops/migrate_schema21.sh; deploy the refresher right afterwards (the
-- schema-20 refresher keeps working on schema 21; the schema-21 refresher
-- needs the columns).
\set ON_ERROR_STOP on
\pset pager off

BEGIN;
SET LOCAL lock_timeout = '10s';
DO $$
BEGIN
  IF (SELECT value FROM schema_meta WHERE key = 'schema_version') IS DISTINCT FROM '20' THEN
    RAISE EXCEPTION 'migrate_schema21: expected schema_version 20';
  END IF;
END $$;

\echo [1/2] ingest_state.taken_from, taken_since
ALTER TABLE ingest_state ADD COLUMN taken_from timestamptz, ADD COLUMN taken_since timestamptz;

\echo [2/2] schema_meta 21
INSERT INTO schema_meta (key, value) VALUES ('schema_version', '21')
  ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
COMMIT;
