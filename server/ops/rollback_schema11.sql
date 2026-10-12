-- Server schema 11 -> 10: removes what ops/migrate_schema11.sql and the
-- schema-11 schema.sql added. ONE transaction. Run as the database owner,
-- from server/:
--
--   docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 < ops/rollback_schema11.sql
--
-- Then apply the schema.sql of the last schema-10 commit (refresh_ap_baselines()
-- and the merge function without hardware periods and reasons) and recompute
-- the baselines:
--   git show ba79ea8:server/schema.sql | docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1
--   docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c 'SELECT s.id, refresh_ap_baselines(s.id, 200, 2) FROM sensors s'
--
-- LOST: the status reasons (the statuses stay - schema 10 has them without
-- a reason; the reasons remain in audit_log's new values) and the hardware
-- catalog and its history (the baselines are then learned across a swap
-- again).
\set ON_ERROR_STOP on
\pset pager off

BEGIN;
DO $$
DECLARE v text := (SELECT value FROM schema_meta WHERE key = 'schema_version');
BEGIN
  IF v IS DISTINCT FROM '11' THEN
    RAISE EXCEPTION 'rollback_schema11: expected schema_version 11, found %', v;
  END IF;
END $$;
SELECT status, status_reason, count(*) AS events FROM detections GROUP BY 1, 2 ORDER BY 1, 2;
DROP TRIGGER IF EXISTS sensor_hardware_kinds ON sensor_hardware;
DROP FUNCTION IF EXISTS sensor_hardware_kinds();
DROP FUNCTION IF EXISTS hardware_period(integer, timestamptz);
DROP TABLE IF EXISTS sensor_hardware;
DROP TABLE IF EXISTS capture_hardware;
ALTER TABLE ap_baselines DROP COLUMN IF EXISTS hw_from;
ALTER TABLE detections DROP CONSTRAINT IF EXISTS detections_status_reason;
ALTER TABLE detections DROP COLUMN IF EXISTS status_reason;
UPDATE schema_meta SET value = '10' WHERE key = 'schema_version';
COMMIT;
