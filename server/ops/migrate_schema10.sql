-- Server schema 9 -> 10: an AP stays an AP once it has been one
-- (devices.ever_ap; schema.sql, schema 10). ONE transaction: any error leaves
-- schema 9 exactly as it was. Run through ops/migrate_schema10.sh
-- (afterwards it applies schema.sql - the devices_ever_ap() trigger, the AP
-- sets on ever_ap, the new Kismet attribution -, runs a FULL refresh_rollups()
-- per sensor, the duplicate-event merge as a dry run and the fixture tests).
--
-- What happens:
--   1. checks: schema 9; lock_timeout 10 s
--   2. devices.ever_ap (default false: a metadata-only change), backfilled
--      from every sign that a device has been an AP: its type, an
--      advertised configuration (SSID, crypt, channel), a configuration
--      history row, an observation (the server keeps AP rows only, schema 7)
--      - measured read-only 2026-10-12: the 849 devices typed ap, 37 typed
--      client and 1 ad-hoc (an IBSS beacons its SSID too); ~9 s
--   3. the partial index devices_ever_ap
--   4. schema_meta 10
\set ON_ERROR_STOP on
\pset pager off

BEGIN;
SET LOCAL lock_timeout = '10s';
DO $$
DECLARE v text := (SELECT value FROM schema_meta WHERE key = 'schema_version');
BEGIN
  IF v IS DISTINCT FROM '9' THEN
    RAISE EXCEPTION 'migrate_schema10: expected schema_version 9, found %', v;
  END IF;
END $$;

ALTER TABLE devices ADD COLUMN ever_ap boolean NOT NULL DEFAULT false;
UPDATE devices d SET ever_ap = true
WHERE d.type = 'ap' OR d.ssid IS NOT NULL OR d.crypt IS NOT NULL OR d.adv_channel IS NOT NULL
   OR EXISTS (SELECT 1 FROM device_config_history h
              WHERE h.sensor_id = d.sensor_id AND h.device_key = d.device_key)
   OR EXISTS (SELECT 1 FROM observations o
              WHERE o.sensor_id = d.sensor_id AND o.device_key = d.device_key);
CREATE INDEX devices_ever_ap ON devices (sensor_id, device_key) WHERE ever_ap;

SELECT type, count(*) FILTER (WHERE ever_ap) AS ever_ap, count(*) AS devices
FROM devices GROUP BY type ORDER BY devices DESC;

INSERT INTO schema_meta (key, value) VALUES ('schema_version', '10')
  ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
COMMIT;
