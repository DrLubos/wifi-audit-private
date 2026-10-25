-- Server schema 16 -> 17 (schema.sql, schema 17): detections.status_cause,
-- the cause of a benign / false-positive label from a fixed list (RQ2,
-- evaluation/rq2), required for those two outcomes, 'other' only with a note.
-- ONE transaction: any error leaves schema 16 as it is. Run through
-- ops/migrate_schema17.sh. Refuses to run while a benign or false-positive
-- label exists (it would have no cause; none existed on 2026-10-25) - such
-- rows would need a cause chosen first. The api must be the schema-17 one
-- before anyone labels benign / false_positive (the old api sends no cause).
\set ON_ERROR_STOP on
\pset pager off

BEGIN;
SET LOCAL lock_timeout = '10s';
DO $$
BEGIN
  IF (SELECT value FROM schema_meta WHERE key = 'schema_version') IS DISTINCT FROM '16' THEN
    RAISE EXCEPTION 'migrate_schema17: expected schema_version 16';
  END IF;
  IF EXISTS (SELECT 1 FROM detections WHERE status IN ('benign', 'false_positive')) THEN
    RAISE EXCEPTION 'migrate_schema17: % benign / false-positive labels exist - they need a cause first',
      (SELECT count(*) FROM detections WHERE status IN ('benign', 'false_positive'));
  END IF;
END $$;

\echo [1/2] detections.status_cause
ALTER TABLE detections ADD COLUMN status_cause text;
ALTER TABLE detections ADD CONSTRAINT detections_status_cause CHECK (CASE
    WHEN status IN ('benign', 'false_positive') THEN status_cause IS NOT NULL
      AND status_cause IN ('rssi_floor', 'channel_hopping', 'kismet_rule', 'allow_list_incomplete', 'pipeline_bug',
                           'sensor_degraded', 'ap_behaviour', 'client_behaviour', 'staged_test', 'unknown', 'other')
      AND (status_cause <> 'other' OR coalesce(btrim(status_note), '') <> '')
    ELSE status_cause IS NULL END);

\echo [2/2] schema_meta 17
INSERT INTO schema_meta (key, value) VALUES ('schema_version', '17')
  ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
COMMIT;
