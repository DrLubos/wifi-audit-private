-- Server schema 11 -> 12 (schema.sql, schema 12). ONE transaction: any
-- error leaves schema 11 as it is. Run through ops/migrate_schema12.sh.
--
--   1. checks: schema 11, no labelled event yet (none exists: the write
--      endpoints are not deployed); lock_timeout 10 s
--   2. the antenna of the migration-11 seed, corrected with the values given
--      (psql variables antenna_name, antenna_gain_dbi; '' = keep, 'unknown' =
--      NULL - the gain is not known)
--   3. detections: status gains 'benign'; the reason constraint becomes
--      NULL-safe (schema 11 let confirmed / false_positive pass with a NULL
--      reason - a CHECK that evaluates to NULL passes) with the three
--      outcomes' reasons
--   4. sensor_config: the 59-entry plan is dropped (no row uses it)
--   5. capture_hardware: gain within -10 .. 30 dBi (fails while the seeded
--      antenna's gain is out of range - give ANTENNA_GAIN_DBI)
--   6. schema_meta 12
\set ON_ERROR_STOP on
\pset pager off

BEGIN;
SET LOCAL lock_timeout = '10s';
DO $$
DECLARE
  v text := (SELECT value FROM schema_meta WHERE key = 'schema_version');
  n bigint := (SELECT count(*) FROM detections WHERE status <> 'open');
BEGIN
  IF v IS DISTINCT FROM '11' THEN
    RAISE EXCEPTION 'migrate_schema12: expected schema_version 11, found %', v;
  END IF;
  IF n > 0 THEN
    RAISE EXCEPTION 'migrate_schema12: % labelled events - map their reasons by hand first', n;
  END IF;
END $$;

UPDATE capture_hardware h
SET name = coalesce(nullif(:'antenna_name', ''), h.name),
    gain_dbi = CASE WHEN :'antenna_gain_dbi' = 'unknown' THEN NULL
                    ELSE coalesce(nullif(:'antenna_gain_dbi', '')::real, h.gain_dbi) END
WHERE h.id IN (SELECT x.antenna_id FROM sensor_hardware x WHERE x.created_by = 'seed');
DO $$
DECLARE r record;
BEGIN
  SELECT id, name, gain_dbi INTO r FROM capture_hardware WHERE NOT (gain_dbi BETWEEN -10 AND 30) LIMIT 1;
  IF FOUND THEN
    RAISE EXCEPTION 'migrate_schema12: catalog row % (%) has a gain of % dBi - give its real ANTENNA_GAIN_DBI (-10 .. 30), or unknown',
      r.id, r.name, r.gain_dbi;
  END IF;
END $$;

ALTER TABLE detections DROP CONSTRAINT detections_status_check;
ALTER TABLE detections ADD CONSTRAINT detections_status_check
  CHECK (status IN ('open', 'confirmed', 'benign', 'false_positive'));
ALTER TABLE detections DROP CONSTRAINT detections_status_reason;
ALTER TABLE detections ADD CONSTRAINT detections_status_reason CHECK (CASE status
    WHEN 'open' THEN status_reason IS NULL
    WHEN 'confirmed' THEN status_reason IS NOT NULL AND status_reason IN ('security_relevant', 'staged_test')
    WHEN 'benign' THEN status_reason IS NOT NULL AND status_reason IN ('expected_behaviour', 'accepted_risk')
    WHEN 'false_positive' THEN status_reason IS NOT NULL AND status_reason IN ('rule_defect', 'artefact')
    ELSE false END);
ALTER TABLE sensor_config DROP CONSTRAINT sensor_config_channel_plan_check;
ALTER TABLE sensor_config ADD CONSTRAINT sensor_config_channel_plan_check
  CHECK (channel_plan IN ('auto', 'all_89', 'ht20_37', 'locked', 'custom'));
ALTER TABLE capture_hardware ADD CONSTRAINT capture_hardware_gain_check CHECK (gain_dbi BETWEEN -10 AND 30);

SELECT id, kind, name, gain_dbi, connector FROM capture_hardware ORDER BY id;
INSERT INTO schema_meta (key, value) VALUES ('schema_version', '12')
  ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
COMMIT;
