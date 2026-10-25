-- Server schema 18 -> 19 (schema.sql, schema 19): the label cause 'duplicate'
-- (the event repeats an incident already reported - a grouping artefact; the
-- developer's addition of 2026-10-25). Only the constraint's list changes;
-- existing labels stay valid. ONE transaction: any error leaves schema 18.
-- Run through ops/migrate_schema19.sh; deploy the api and the frontend right
-- afterwards (the dialog's list).
\set ON_ERROR_STOP on
\pset pager off

BEGIN;
SET LOCAL lock_timeout = '10s';
DO $$
BEGIN
  IF (SELECT value FROM schema_meta WHERE key = 'schema_version') IS DISTINCT FROM '18' THEN
    RAISE EXCEPTION 'migrate_schema19: expected schema_version 18';
  END IF;
END $$;

\echo [1/2] detections_status_cause with 'duplicate'
ALTER TABLE detections DROP CONSTRAINT detections_status_cause;
ALTER TABLE detections ADD CONSTRAINT detections_status_cause CHECK (CASE
    WHEN status IN ('benign', 'false_positive') THEN status_cause IS NOT NULL
      AND status_cause IN ('rssi_floor', 'channel_hopping', 'kismet_rule', 'allow_list_incomplete', 'pipeline_bug',
                           'sensor_degraded', 'ap_behaviour', 'client_behaviour', 'staged_test', 'duplicate',
                           'unknown', 'other')
      AND (status_cause <> 'other' OR coalesce(btrim(status_note), '') <> '')
    ELSE status_cause IS NULL END);

\echo [2/2] schema_meta 19
INSERT INTO schema_meta (key, value) VALUES ('schema_version', '19')
  ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
COMMIT;
