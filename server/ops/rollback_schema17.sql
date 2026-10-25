-- Server schema 17 -> 16: drops detections.status_cause (the causes of the
-- labels are lost - export them first: evaluation/rq2/export.sh). Deploy the
-- schema-16 api first. ONE transaction, as the database owner, from server/:
--
--   docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 < ops/rollback_schema17.sql
\set ON_ERROR_STOP on
BEGIN;
DO $$
BEGIN
  IF (SELECT value FROM schema_meta WHERE key = 'schema_version') IS DISTINCT FROM '17' THEN
    RAISE EXCEPTION 'rollback_schema17: expected schema_version 17';
  END IF;
END $$;
ALTER TABLE detections DROP CONSTRAINT detections_status_cause;
ALTER TABLE detections DROP COLUMN status_cause;
INSERT INTO schema_meta (key, value) VALUES ('schema_version', '16')
  ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
COMMIT;
