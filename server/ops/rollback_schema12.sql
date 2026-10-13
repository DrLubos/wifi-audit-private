-- Server schema 12 -> 11: the schema-11 constraints again. ONE transaction,
-- as the database owner; refuses while an event is labelled benign (schema
-- 11 has no such outcome). Then apply the schema.sql of the last schema-11
-- commit (git show 8a4ce91:server/schema.sql | psql ...).
\set ON_ERROR_STOP on
BEGIN;
DO $$
BEGIN
  IF (SELECT value FROM schema_meta WHERE key = 'schema_version') IS DISTINCT FROM '12' THEN
    RAISE EXCEPTION 'rollback_schema12: expected schema_version 12';
  END IF;
  IF EXISTS (SELECT 1 FROM detections WHERE status = 'benign') THEN
    RAISE EXCEPTION 'rollback_schema12: benign labels exist - relabel them first';
  END IF;
END $$;
ALTER TABLE detections DROP CONSTRAINT detections_status_reason;
ALTER TABLE detections DROP CONSTRAINT detections_status_check;
ALTER TABLE detections ADD CONSTRAINT detections_status_check CHECK (status IN ('open', 'confirmed', 'false_positive'));
ALTER TABLE detections ADD CONSTRAINT detections_status_reason CHECK (
    (status = 'open' AND status_reason IS NULL)
    OR (status = 'confirmed' AND status_reason IN ('security_relevant', 'expected_benign', 'staged_test'))
    OR (status = 'false_positive' AND status_reason IN ('rule_defect', 'artefact')));
ALTER TABLE sensor_config DROP CONSTRAINT sensor_config_channel_plan_check;
ALTER TABLE sensor_config ADD CONSTRAINT sensor_config_channel_plan_check
  CHECK (channel_plan IN ('auto', 'all_89', 'ht20_37', 'no_empty_dfs_59', 'locked', 'custom'));
ALTER TABLE capture_hardware DROP CONSTRAINT IF EXISTS capture_hardware_gain_check;
UPDATE schema_meta SET value = '11' WHERE key = 'schema_version';
COMMIT;
