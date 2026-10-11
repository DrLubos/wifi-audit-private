-- Server schema 9 -> 8: removes what ops/migrate_schema9.sql added. ONE
-- transaction. Run as the database owner, from server/:
--
--   docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 < ops/rollback_schema9.sql
--
-- Then apply the schema.sql of the last schema-8 commit and rebuild that
-- commit's api (its routers read detections.acked):
--   git show 2ee5932:server/schema.sql | docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1
--   docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c 'SELECT * FROM refresh_rollups(1, NULL)'
--
-- LOST: the operator's data of schema 9 - networks, account_settings, manual
-- classifications and notes (aps), event statuses and notes, sensor_config
-- versions, audit_log. Export them first, e.g.
--   for t in networks account_settings aps detections sensor_config audit_log; do
--     docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "\copy $t TO STDOUT CSV HEADER" > ~/rollback9-$t.csv
--   done
-- Event statuses map back to acked = (status <> 'open'); the derived event rows
-- (kind <> 'detection') and the rule events are deleted. Last resort: the
-- backup taken before the migration.
\set ON_ERROR_STOP on
\pset pager off

BEGIN;
DO $$
DECLARE v text := (SELECT value FROM schema_meta WHERE key = 'schema_version');
BEGIN
  IF v IS DISTINCT FROM '9' THEN
    RAISE EXCEPTION 'rollback_schema9: expected schema_version 9, found %', v;
  END IF;
END $$;

DROP VIEW account_aps;
DROP FUNCTION refresh_events(integer);
DROP FUNCTION ap_is_mobile(integer, boolean, text, timestamptz, timestamptz, timestamptz);
DROP FUNCTION ap_classification(integer, text, text, macaddr, boolean, real);
DROP FUNCTION kismet_severity(smallint);
DROP FUNCTION security_meets(text, boolean, text, bigint, boolean);
DROP FUNCTION security_detail(bigint);
DROP FUNCTION security_rank(text);
DROP FUNCTION mfp_state(boolean, boolean);
DROP FUNCTION security_label(text, bigint);
DROP FUNCTION security_level(text, bigint, boolean, boolean);
DROP FUNCTION crypt_kind(text, bigint);
DROP FUNCTION crypt_has(bigint, integer[]);

-- detections back to the schema-8 shape
SELECT kind, status, count(*) AS rows_before FROM detections GROUP BY 1, 2 ORDER BY 1, 2;
DELETE FROM detections WHERE kind <> 'detection' OR type = 'unknown_bssid_managed_ssid';
DROP INDEX detections_feed;
DROP INDEX detections_open;
DROP INDEX detections_dedupe;
ALTER TABLE detections ADD COLUMN acked boolean NOT NULL DEFAULT false;
UPDATE detections SET acked = (status <> 'open');
ALTER TABLE detections RENAME COLUMN status_at TO acked_at;
ALTER TABLE detections RENAME COLUMN status_note TO ack_note;
ALTER TABLE detections DROP COLUMN kind, DROP COLUMN ts_end, DROP COLUMN status, DROP COLUMN updated_at;
CREATE INDEX detections_open ON detections (sensor_id) WHERE NOT acked;
CREATE UNIQUE INDEX detections_dedupe
  ON detections (sensor_id, type, coalesce(device_key, upper(mac::text)), ts);

DROP INDEX ap_rssi_hourly_hour;
ALTER TABLE ap_summary DROP COLUMN security, DROP COLUMN security_label, DROP COLUMN crypt_bits_known,
                       DROP COLUMN rssi_typical;
ALTER TABLE sensor_summary DROP COLUMN last_capture_at, DROP COLUMN last_hop_n, DROP COLUMN last_hop_visited,
                           DROP COLUMN last_hop_ok;
ALTER TABLE sensor_hourly DROP COLUMN capture_polls, DROP COLUMN limited_polls;

DROP TABLE sensor_periods, ap_security_periods, audit_log, kismet_alert_rules, sensor_uploads,
           sensor_config, sensor_annotations, ap_sensors, aps, networks, account_settings;
ALTER TABLE sensors DROP COLUMN account_id, DROP COLUMN hardware;
DROP TABLE accounts;

UPDATE schema_meta SET value = '8' WHERE key = 'schema_version';
SELECT count(*) AS detections_after FROM detections;
COMMIT;
