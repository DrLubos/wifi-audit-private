-- Server schema 20 -> 19: drops discard mode. Refused while a window exists
-- (its records and the exclusion of its time would be lost - the developer
-- decides that, not a rollback). Deploy the schema-19 api, refresher and
-- frontend first. ONE transaction, as the database owner, from server/:
--
--   docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 < ops/rollback_schema20.sql
--
-- then the schema-19 functions (refresh_sensor_periods() and ingest_merge()
-- without the windows; idempotent, sets schema_version 19):
--
--   git show 1bb7ed4:server/schema.sql | docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1
\set ON_ERROR_STOP on
BEGIN;
DO $$
BEGIN
  IF (SELECT value FROM schema_meta WHERE key = 'schema_version') IS DISTINCT FROM '20' THEN
    RAISE EXCEPTION 'rollback_schema20: expected schema_version 20';
  END IF;
  IF EXISTS (SELECT 1 FROM sensor_discard_windows) THEN
    RAISE EXCEPTION 'rollback_schema20: discard windows exist - their records and exclusions would be lost';
  END IF;
END $$;
DROP VIEW discard_window_totals;
DROP FUNCTION discard_stop(integer);
DROP FUNCTION discard_start(integer, integer, text);
DROP FUNCTION discard_close_expired();
DROP FUNCTION discard_close(integer, timestamptz, text);
DROP FUNCTION ingest_resume(integer);
DROP FUNCTION discarded_seqs(integer, bigint, bigint, timestamptz, timestamptz);
DROP FUNCTION sensor_excluded(integer);
DROP FUNCTION discard_intervals(integer);
DROP FUNCTION discard_active(integer);
DROP TABLE sensor_discarded_batches;
DROP TABLE sensor_discard_windows;
ALTER TABLE sensor_annotations DROP CONSTRAINT sensor_annotations_kind_check;
ALTER TABLE sensor_annotations ADD CONSTRAINT sensor_annotations_kind_check
  CHECK (kind IN ('gap', 'no_frames', 'coverage', 'restart', 'change', 'hardware_change', 'lost', 'unreachable'));
INSERT INTO schema_meta (key, value) VALUES ('schema_version', '19')
  ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
COMMIT;
