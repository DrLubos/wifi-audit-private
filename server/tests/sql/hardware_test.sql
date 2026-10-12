-- Fixture test of the capture hardware (schema.sql, schema 11):
-- sensor_hardware_kinds(), hardware_period() and the hardware-bounded
-- refresh_ap_baselines().
--
-- Writes nothing: everything runs in one transaction that is rolled back. A
-- scratch schema first on the search path gets empty copies of the tables
-- these functions read or write; the deployed functions resolve them through
-- the search path. Ids are given explicitly, so no real sequence moves. Run it
-- as the owner (CREATE SCHEMA):
--
--   docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
--     -v ON_ERROR_STOP=1 < tests/sql/hardware_test.sql
--
-- Prints NOTICE "hardware: PASS". Cases:
--   1. an antenna given as the adapter (or an adapter as the antenna) is
--      refused;
--   2. hardware_period(): one period without records; before the first
--      record, between two, after the last;
--   3. an AP heard at -60.5 dBm with antenna 1 and at -50.5 dBm with antenna 2
--      (+10 dB, the swap): the baseline is learned from antenna 2 only
--      (hw_from = the swap), and with p_until before the swap from antenna 1
--      only - never a mix of both;
--   4. right after another swap no AP qualifies, so the statistics are
--      cleared (evil_twin (b) waits for the new hardware).

\set ON_ERROR_STOP on
BEGIN;

DO $$
DECLARE t text;
BEGIN
  CREATE SCHEMA hardware_test;
  PERFORM set_config('search_path', 'hardware_test, public', true);
  FOREACH t IN ARRAY ARRAY['accounts', 'sensors', 'devices', 'device_freq_hist', 'observations', 'ap_baselines',
                           'capture_hardware', 'sensor_hardware'] LOOP
    EXECUTE format('CREATE TABLE %I (LIKE public.%I INCLUDING ALL)', t, t);
  END LOOP;
  -- LIKE copies no trigger: the deployed kind check on the copy
  CREATE TRIGGER sensor_hardware_kinds BEFORE INSERT OR UPDATE ON sensor_hardware
    FOR EACH ROW EXECUTE FUNCTION public.sensor_hardware_kinds();
END $$;

INSERT INTO accounts (id, name) VALUES (1, 'test');
INSERT INTO sensors (id, account_id, name, tz) VALUES (1, 1, 'pi-test', 'UTC');
INSERT INTO devices (sensor_id, device_key, mac, type, first_seen, last_seen, ssid, adv_channel, ever_ap)
VALUES (1, 'A1', 'A8:00:00:00:00:01', 'ap', '2026-09-20 00:00+00', '2026-09-26 00:00+00', 'Net', '6', true);
-- one reading every 10 min: antenna 1 from 20 Sep (-60/-61), antenna 2 from 23 Sep (-50/-51)
INSERT INTO observations (sensor_id, ts, device_key, rssi, rssi_floor)
SELECT 1, t, 'A1', CASE WHEN t < '2026-09-23 00:00+00' THEN -60 ELSE -50 END
                   - (extract(minute FROM t)::int / 10 % 2), false
FROM generate_series(timestamptz '2026-09-20 00:00+00', '2026-09-25 23:50+00', interval '10 minutes') AS t;
INSERT INTO capture_hardware (id, account_id, kind, name, gain_dbi, bands) VALUES
  (1, 1, 'adapter', 'Adapter', NULL, ARRAY['2.4', '5']),
  (2, 1, 'antenna', 'Antenna 2 dBi', 2, ARRAY['2.4', '5']),
  (3, 1, 'antenna', 'Antenna 5 dBi', 5, ARRAY['2.4', '5']);

-- --- 1. kinds -------------------------------------------------------------------------------
DO $$
BEGIN
  BEGIN
    INSERT INTO sensor_hardware (sensor_id, valid_from, adapter_id, antenna_id) VALUES (1, '2026-09-20 00:00+00', 2, 3);
    RAISE EXCEPTION 'hardware: FAIL (an antenna accepted as the adapter)' USING ERRCODE = 'P0002';
  EXCEPTION WHEN raise_exception THEN NULL;
  END;
  BEGIN
    INSERT INTO sensor_hardware (sensor_id, valid_from, adapter_id, antenna_id) VALUES (1, '2026-09-20 00:00+00', 1, 1);
    RAISE EXCEPTION 'hardware: FAIL (an adapter accepted as the antenna)' USING ERRCODE = 'P0002';
  EXCEPTION WHEN raise_exception THEN NULL;
  END;
END $$;

-- --- 2. periods -----------------------------------------------------------------------------
DO $$
BEGIN
  IF hardware_period(1, '2026-09-21 00:00+00') <> tstzrange('-infinity', 'infinity', '[)') THEN
    RAISE EXCEPTION 'hardware: FAIL (no record: %)', hardware_period(1, '2026-09-21 00:00+00');
  END IF;
END $$;
INSERT INTO sensor_hardware (sensor_id, valid_from, adapter_id, antenna_id) VALUES
  (1, '2026-09-20 00:00+00', 1, 2), (1, '2026-09-23 00:00+00', 1, 3);
DO $$
DECLARE r record;
BEGIN
  FOR r IN SELECT * FROM (VALUES
      (timestamptz '2026-09-19 12:00+00', tstzrange('-infinity', '2026-09-20 00:00+00', '[)')),
      ('2026-09-20 00:00+00', tstzrange('2026-09-20 00:00+00', '2026-09-23 00:00+00', '[)')),
      ('2026-09-22 23:59:59+00', tstzrange('2026-09-20 00:00+00', '2026-09-23 00:00+00', '[)')),
      ('2026-09-23 00:00+00', tstzrange('2026-09-23 00:00+00', 'infinity', '[)')),
      ('infinity', tstzrange('2026-09-23 00:00+00', 'infinity', '[)'))) AS v(ts, want) LOOP
    IF hardware_period(1, r.ts) <> r.want THEN
      RAISE EXCEPTION 'hardware: FAIL (period of %: %, expected %)', r.ts, hardware_period(1, r.ts), r.want;
    END IF;
  END LOOP;
END $$;

-- --- 3. baselines inside one period ---------------------------------------------------------
DO $$
DECLARE b record;
BEGIN
  IF refresh_ap_baselines(1, 200, 2) <> 1 THEN RAISE EXCEPTION 'hardware: FAIL (antenna 2: no baseline)'; END IF;
  SELECT * INTO b FROM ap_baselines WHERE device_key = 'A1';
  IF (b.rssi_median, b.n_obs, b.hw_from) IS DISTINCT FROM (-50.5::real, 432, timestamptz '2026-09-23 00:00+00')
     OR b.window_start < b.hw_from THEN
    RAISE EXCEPTION 'hardware: FAIL (antenna 2 baseline: median %, n %, hw_from %, from %)',
      b.rssi_median, b.n_obs, b.hw_from, b.window_start;
  END IF;
  -- fitted on the window before the swap: antenna 1 only
  PERFORM refresh_ap_baselines(1, 200, 2, NULL, '2026-09-23 00:00+00');
  SELECT * INTO b FROM ap_baselines WHERE device_key = 'A1';
  IF (b.rssi_median, b.n_obs, b.hw_from) IS DISTINCT FROM (-60.5::real, 432, timestamptz '2026-09-20 00:00+00')
     OR b.window_end >= '2026-09-23 00:00+00' THEN
    RAISE EXCEPTION 'hardware: FAIL (antenna 1 baseline: median %, n %, hw_from %, to %)',
      b.rssi_median, b.n_obs, b.hw_from, b.window_end;
  END IF;
END $$;

-- --- 4. right after another swap ------------------------------------------------------------
INSERT INTO sensor_hardware (sensor_id, valid_from, adapter_id, antenna_id) VALUES (1, '2026-09-25 23:00+00', 1, 2);
-- the clearing step keeps rows refreshed by this call (computed_at = now(),
-- one transaction here): the earlier refresh is made older
UPDATE ap_baselines SET computed_at = computed_at - interval '1 hour';
DO $$
DECLARE b record;
BEGIN
  IF refresh_ap_baselines(1, 200, 2) <> 0 THEN RAISE EXCEPTION 'hardware: FAIL (6 readings qualified)'; END IF;
  SELECT * INTO b FROM ap_baselines WHERE device_key = 'A1';
  IF b.rssi_median IS NOT NULL OR b.hw_from IS NOT NULL THEN
    RAISE EXCEPTION 'hardware: FAIL (after the swap the statistics stayed: median %, hw_from %)', b.rssi_median, b.hw_from;
  END IF;
  RAISE NOTICE 'hardware: PASS (kinds checked; hardware periods; a baseline never spans a hardware change; cleared right after a swap)';
END $$;

ROLLBACK;
