-- Server schema 10 -> 11: dashboard v2 phase 5 (schema.sql, schema 11).
-- ONE transaction: any error leaves schema 10 exactly as it was. Run through
-- ops/migrate_schema11.sh (afterwards it applies schema.sql - the hardware
-- catalog, sensor_hardware, hardware_period(), the hardware-bounded
-- refresh_ap_baselines() -, seeds the capture hardware, applies the sensor
-- history corrections, recomputes the baselines and runs the fixture tests).
--
-- What happens:
--   1. checks: schema 10, no labelled event yet (a label without a reason
--      would violate the new constraint - none exists before the write
--      endpoints); lock_timeout 10 s
--   2. detections.status_reason + the constraint tying it to the status
--      (metadata only: every row is open, the check reads each row once)
--   3. ap_baselines.hw_from (metadata only)
--   4. schema_meta 11
\set ON_ERROR_STOP on
\pset pager off

BEGIN;
SET LOCAL lock_timeout = '10s';
DO $$
DECLARE
  v text := (SELECT value FROM schema_meta WHERE key = 'schema_version');
  n bigint := (SELECT count(*) FROM detections WHERE status <> 'open');
BEGIN
  IF v IS DISTINCT FROM '10' THEN
    RAISE EXCEPTION 'migrate_schema11: expected schema_version 10, found %', v;
  END IF;
  IF n > 0 THEN
    RAISE EXCEPTION 'migrate_schema11: % labelled events without a reason - give them one first', n;
  END IF;
END $$;

ALTER TABLE detections ADD COLUMN status_reason text;
ALTER TABLE detections ADD CONSTRAINT detections_status_reason CHECK (
    (status = 'open' AND status_reason IS NULL)
    OR (status = 'confirmed' AND status_reason IN ('security_relevant', 'expected_benign', 'staged_test'))
    OR (status = 'false_positive' AND status_reason IN ('rule_defect', 'artefact')));
ALTER TABLE ap_baselines ADD COLUMN hw_from timestamptz;

SELECT status, count(*) AS events FROM detections GROUP BY 1 ORDER BY 1;

INSERT INTO schema_meta (key, value) VALUES ('schema_version', '11')
  ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
COMMIT;
