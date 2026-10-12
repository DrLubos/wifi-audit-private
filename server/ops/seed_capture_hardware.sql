-- The capture hardware of pi-fri (schema 11): the RTL8821CU adapter and the
-- developer's antenna in the catalog (capture_hardware), and that pair
-- attached since the start of the measurements (sensor_hardware) - one
-- hardware period so far. Idempotent (ON CONFLICT DO NOTHING; a second run
-- adds nothing). Run by ops/migrate_schema11.sh with the antenna as psql
-- variables; by hand:
--
--   docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 \
--     -v antenna_name='<name>' -v antenna_gain_dbi=<dBi> -v antenna_bands='2.4,5' \
--     -v antenna_connector='RP-SMA' -v antenna_since='2026-09-15 13:53:07+00' < ops/seed_capture_hardware.sql
--
-- antenna_connector may be '' (unknown). antenna_since: when it was attached;
-- the default of the migration is pi-fri's first poll. Further adapters and
-- antennas are rows of capture_hardware (Settings > Sensors or an INSERT).
\set ON_ERROR_STOP on
BEGIN;
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM sensors WHERE name = 'pi-fri') THEN
    RAISE EXCEPTION 'seed_capture_hardware: no sensor pi-fri';
  END IF;
END $$;

INSERT INTO capture_hardware (account_id, kind, name, gain_dbi, bands, connector, note)
SELECT s.account_id, 'adapter', 'RTL8821CU USB adapter', NULL, ARRAY['2.4', '5'], 'USB',
       'Driver rtw88_8821cu; RSSI floors -106 dBm (CCK) and -120 dBm (OFDM) - the floor list of rssi_valid(). '
       || 'Another adapter needs its own floors before its RSSI is trusted.'
FROM sensors s WHERE s.name = 'pi-fri'
ON CONFLICT DO NOTHING;

INSERT INTO capture_hardware (account_id, kind, name, gain_dbi, bands, connector, note)
SELECT s.account_id, 'antenna', :'antenna_name', (:'antenna_gain_dbi')::real,
       string_to_array(:'antenna_bands', ','), nullif(:'antenna_connector', ''), NULL
FROM sensors s WHERE s.name = 'pi-fri'
ON CONFLICT DO NOTHING;

INSERT INTO sensor_hardware (sensor_id, valid_from, adapter_id, antenna_id, created_by, note)
SELECT s.id, (:'antenna_since')::timestamptz,
       (SELECT h.id FROM capture_hardware h
        WHERE h.account_id = s.account_id AND h.kind = 'adapter' AND h.name = 'RTL8821CU USB adapter'),
       (SELECT h.id FROM capture_hardware h
        WHERE h.account_id = s.account_id AND h.kind = 'antenna' AND h.name = :'antenna_name'),
       'seed', 'The hardware of the measurements so far'
FROM sensors s WHERE s.name = 'pi-fri'
ON CONFLICT DO NOTHING;

SELECT id, kind, name, gain_dbi, bands, connector FROM capture_hardware ORDER BY id;
SELECT s.name AS sensor, h.valid_from, a.name AS adapter, n.name AS antenna, n.gain_dbi
FROM sensor_hardware h JOIN sensors s ON s.id = h.sensor_id
JOIN capture_hardware a ON a.id = h.adapter_id LEFT JOIN capture_hardware n ON n.id = h.antenna_id
ORDER BY s.id, h.valid_from;
COMMIT;
