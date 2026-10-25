-- Server schema 19 -> 18: the cause list without 'duplicate'. Refused while
-- a label uses it (relabel those first). Deploy the schema-18 api and
-- frontend first. ONE transaction, as the database owner, from server/:
--
--   docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 < ops/rollback_schema19.sql
\set ON_ERROR_STOP on
BEGIN;
DO $$
BEGIN
  IF (SELECT value FROM schema_meta WHERE key = 'schema_version') IS DISTINCT FROM '19' THEN
    RAISE EXCEPTION 'rollback_schema19: expected schema_version 19';
  END IF;
  IF EXISTS (SELECT 1 FROM detections WHERE status_cause = 'duplicate') THEN
    RAISE EXCEPTION 'rollback_schema19: labels with the cause duplicate exist - relabel them first';
  END IF;
END $$;
ALTER TABLE detections DROP CONSTRAINT detections_status_cause;
ALTER TABLE detections ADD CONSTRAINT detections_status_cause CHECK (CASE
    WHEN status IN ('benign', 'false_positive') THEN status_cause IS NOT NULL
      AND status_cause IN ('rssi_floor', 'channel_hopping', 'kismet_rule', 'allow_list_incomplete', 'pipeline_bug',
                           'sensor_degraded', 'ap_behaviour', 'client_behaviour', 'staged_test', 'unknown', 'other')
      AND (status_cause <> 'other' OR coalesce(btrim(status_note), '') <> '')
    ELSE status_cause IS NULL END);
INSERT INTO schema_meta (key, value) VALUES ('schema_version', '18')
  ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
COMMIT;
