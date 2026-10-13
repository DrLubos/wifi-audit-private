-- Server schema 12 -> 13 (schema.sql, schema 13): no antenna catalog. ONE
-- transaction: any error leaves schema 12 as it is. Run through
-- ops/migrate_schema13.sh (afterwards it applies schema.sql, recomputes the
-- RSSI baselines and runs the fixture tests).
--
-- Why: the antenna is passive and undetectable by the sensor, its gain feeds no
-- computation and it changes rarely; the only real need - a data boundary
-- where RSSI baselines shift - is a dated entry of the configuration history.
-- Interface names (wlan1) are unstable, display only; the adapter's identity
-- is VID:PID + MAC (reported by the sensor in the live-upload phase).
--
--   1. checks: schema 12; lock_timeout 10 s
--   2. sensor_adapters: the seeded adapter of each sensor (name and bands from
--      the catalog row; chipset, driver and interface of the RTL8821CU seed),
--      usb_id / mac from the psql variables adapter_usb_id, adapter_mac
--      ('' = not known yet)
--   3. the hardware changes recorded by hand (sensor_hardware rows not seeded)
--      become 'hardware_change' entries of sensor_annotations - data
--      boundaries; the antenna they named stays in the label
--   4. sensor_annotations accepts kind 'hardware_change'; hardware_period()
--      reads those entries
--   5. capture_hardware and sensor_hardware are dropped
--   6. schema_meta 13
\set ON_ERROR_STOP on
\pset pager off

BEGIN;
SET LOCAL lock_timeout = '10s';
DO $$
BEGIN
  IF (SELECT value FROM schema_meta WHERE key = 'schema_version') IS DISTINCT FROM '12' THEN
    RAISE EXCEPTION 'migrate_schema13: expected schema_version 12';
  END IF;
END $$;

CREATE TABLE sensor_adapters (
  sensor_id   integer PRIMARY KEY REFERENCES sensors(id),
  name        text NOT NULL,                   -- e.g. 'RTL8821CU USB adapter'
  usb_id      text CHECK (usb_id ~ '^[0-9a-f]{4}:[0-9a-f]{4}$'),   -- VID:PID, lower case
  mac         macaddr,
  chipset     text,
  driver      text,
  interface   text,                            -- display only (wlan1)
  bands       text[] NOT NULL DEFAULT '{}' CHECK (bands <@ ARRAY['2.4', '5', '6']),
  reported_by text NOT NULL DEFAULT 'seed' CHECK (reported_by IN ('seed', 'sensor')),
  updated_at  timestamptz NOT NULL DEFAULT now());

INSERT INTO sensor_adapters (sensor_id, name, usb_id, mac, chipset, driver, interface, bands, reported_by)
SELECT x.sensor_id, a.name, nullif(lower(:'adapter_usb_id'), ''), nullif(:'adapter_mac', '')::macaddr,
       CASE WHEN a.name ILIKE 'RTL8821CU%' THEN 'RTL8821CU' END,
       CASE WHEN a.name ILIKE 'RTL8821CU%' THEN 'rtw88_8821cu' END,
       CASE WHEN a.name ILIKE 'RTL8821CU%' THEN 'wlan1' END, a.bands, 'seed'
FROM (SELECT DISTINCT ON (sensor_id) sensor_id, adapter_id FROM sensor_hardware ORDER BY sensor_id, valid_from DESC) x
JOIN capture_hardware a ON a.id = x.adapter_id AND a.kind = 'adapter';

INSERT INTO sensor_annotations (sensor_id, starts_at, ends_at, kind, boundary, label, ref)
SELECT h.sensor_id, h.valid_from, NULL, 'hardware_change', true,
       'Hardware change' || coalesce(': ' || nullif(h.note, ''), '')
         || coalesce(' (antenna ' || n.name || ')', ''),
       'migrated from the hardware history (schema 13)'
FROM sensor_hardware h
LEFT JOIN capture_hardware n ON n.id = h.antenna_id
WHERE h.created_by <> 'seed'
ON CONFLICT DO NOTHING;

ALTER TABLE sensor_annotations DROP CONSTRAINT sensor_annotations_kind_check;
ALTER TABLE sensor_annotations ADD CONSTRAINT sensor_annotations_kind_check
  CHECK (kind IN ('gap', 'no_frames', 'coverage', 'restart', 'change', 'hardware_change'));

CREATE OR REPLACE FUNCTION hardware_period(p_sensor_id integer, p_ts timestamptz)
RETURNS tstzrange
LANGUAGE sql STABLE AS $$
  SELECT tstzrange(
    coalesce((SELECT max(a.starts_at) FROM sensor_annotations a
              WHERE a.sensor_id = p_sensor_id AND a.kind = 'hardware_change' AND a.starts_at <= p_ts), '-infinity'),
    coalesce((SELECT min(a.starts_at) FROM sensor_annotations a
              WHERE a.sensor_id = p_sensor_id AND a.kind = 'hardware_change' AND a.starts_at > p_ts), 'infinity'),
    '[)')
$$;

DROP TRIGGER sensor_hardware_kinds ON sensor_hardware;
DROP FUNCTION sensor_hardware_kinds();
DROP TABLE sensor_hardware;
DROP TABLE capture_hardware;

SELECT sensor_id, name, usb_id, mac, chipset, driver, interface, bands FROM sensor_adapters ORDER BY sensor_id;
SELECT sensor_id, starts_at, label FROM sensor_annotations WHERE kind = 'hardware_change' ORDER BY starts_at;
INSERT INTO schema_meta (key, value) VALUES ('schema_version', '13')
  ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
COMMIT;
