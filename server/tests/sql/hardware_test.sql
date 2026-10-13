-- Fixture test of the hardware boundary (schema.sql, schema 11 and 13):
-- the 'hardware_change' entry of sensor_annotations, hardware_period(), the
-- hardware-bounded refresh_ap_baselines() and the adapter record.
--
-- Writes nothing: everything runs in one transaction that is rolled back. A
-- scratch schema first on the search path gets empty copies of the tables
-- these functions read or write; the deployed functions resolve them through
-- the search path. Run it as the owner (CREATE SCHEMA):
--
--   docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
--     -v ON_ERROR_STOP=1 < tests/sql/hardware_test.sql
--
-- Prints NOTICE "hardware: PASS". Cases:
--   1. the history accepts the entry kind 'hardware_change' and no other new
--      kind; the adapter record refuses a malformed VID:PID;
--   2. hardware_period(): one period without an entry; before the first
--      entry, between two, after the last - other kinds of entry (a channel
--      plan 'change') are not hardware boundaries;
--   3. an AP heard at -60.5 dBm before an entry and at -50.5 dBm after it
--      (+10 dB, a swapped antenna): the baseline is learned from the readings
--      after it only (hw_from = the entry), and with p_until before the entry
--      from the readings before it only - never a mix of both;
--   4. right after a second entry no AP qualifies, so the statistics are
--      cleared (evil_twin (b) waits for the new hardware).

\set ON_ERROR_STOP on
BEGIN;

DO $$
DECLARE t text;
BEGIN
  CREATE SCHEMA hardware_test;
  PERFORM set_config('search_path', 'hardware_test, public', true);
  FOREACH t IN ARRAY ARRAY['accounts', 'sensors', 'devices', 'device_freq_hist', 'observations', 'ap_baselines',
                           'sensor_annotations', 'sensor_adapters'] LOOP
    EXECUTE format('CREATE TABLE %I (LIKE public.%I INCLUDING ALL)', t, t);
  END LOOP;
END $$;

INSERT INTO accounts (id, name) VALUES (1, 'test');
INSERT INTO sensors (id, account_id, name, tz) VALUES (1, 1, 'pi-test', 'UTC');
INSERT INTO devices (sensor_id, device_key, mac, type, first_seen, last_seen, ssid, adv_channel, ever_ap)
VALUES (1, 'A1', 'A8:00:00:00:00:01', 'ap', '2026-09-20 00:00+00', '2026-09-26 00:00+00', 'Net', '6', true);
-- one reading every 10 min: -60 / -61 before 23 Sep, -50 / -51 from then on
INSERT INTO observations (sensor_id, ts, device_key, rssi, rssi_floor)
SELECT 1, t, 'A1', CASE WHEN t < '2026-09-23 00:00+00' THEN -60 ELSE -50 END
                   - (extract(minute FROM t)::int / 10 % 2), false
FROM generate_series(timestamptz '2026-09-20 00:00+00', '2026-09-25 23:50+00', interval '10 minutes') AS t;

-- --- 1. the entry kind and the adapter record -------------------------------------------------
DO $$
BEGIN
  BEGIN
    INSERT INTO sensor_annotations (sensor_id, starts_at, kind, boundary, label)
    VALUES (1, '2026-09-21 00:00+00', 'antenna_swap', true, 'x');
    RAISE EXCEPTION 'hardware: FAIL (an unknown entry kind accepted)' USING ERRCODE = 'P0002';
  EXCEPTION WHEN check_violation THEN NULL;
  END;
  BEGIN
    INSERT INTO sensor_adapters (sensor_id, name, usb_id) VALUES (1, 'adapter', '0BDA-C811');
    RAISE EXCEPTION 'hardware: FAIL (a malformed VID:PID accepted)' USING ERRCODE = 'P0002';
  EXCEPTION WHEN check_violation THEN NULL;
  END;
  INSERT INTO sensor_adapters (sensor_id, name, usb_id, bands) VALUES (1, 'adapter', '0bda:c811', ARRAY['2.4', '5']);
END $$;

-- --- 2. periods -----------------------------------------------------------------------------
DO $$
BEGIN
  IF hardware_period(1, '2026-09-21 00:00+00') <> tstzrange('-infinity', 'infinity', '[)') THEN
    RAISE EXCEPTION 'hardware: FAIL (no entry: %)', hardware_period(1, '2026-09-21 00:00+00');
  END IF;
END $$;
INSERT INTO sensor_annotations (sensor_id, starts_at, kind, boundary, label) VALUES
  (1, '2026-09-21 12:00+00', 'change', true, 'a channel-plan change is no hardware boundary'),
  (1, '2026-09-23 00:00+00', 'hardware_change', true, 'Hardware change: swapped antenna');
DO $$
DECLARE r record;
BEGIN
  FOR r IN SELECT * FROM (VALUES
      (timestamptz '2026-09-19 12:00+00', tstzrange('-infinity', '2026-09-23 00:00+00', '[)')),
      ('2026-09-22 23:59:59+00', tstzrange('-infinity', '2026-09-23 00:00+00', '[)')),
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
  IF refresh_ap_baselines(1, 200, 2) <> 1 THEN RAISE EXCEPTION 'hardware: FAIL (after the entry: no baseline)'; END IF;
  SELECT * INTO b FROM ap_baselines WHERE device_key = 'A1';
  IF (b.rssi_median, b.n_obs, b.hw_from) IS DISTINCT FROM (-50.5::real, 432, timestamptz '2026-09-23 00:00+00')
     OR b.window_start < b.hw_from THEN
    RAISE EXCEPTION 'hardware: FAIL (baseline after the entry: median %, n %, hw_from %, from %)',
      b.rssi_median, b.n_obs, b.hw_from, b.window_start;
  END IF;
  -- fitted on the window before the entry: the first period (no start)
  PERFORM refresh_ap_baselines(1, 200, 2, NULL, '2026-09-23 00:00+00');
  SELECT * INTO b FROM ap_baselines WHERE device_key = 'A1';
  IF (b.rssi_median, b.n_obs) IS DISTINCT FROM (-60.5::real, 432) OR b.hw_from IS NOT NULL
     OR b.window_end >= '2026-09-23 00:00+00' THEN
    RAISE EXCEPTION 'hardware: FAIL (baseline before the entry: median %, n %, hw_from %, to %)',
      b.rssi_median, b.n_obs, b.hw_from, b.window_end;
  END IF;
END $$;

-- --- 4. right after another entry -----------------------------------------------------------
INSERT INTO sensor_annotations (sensor_id, starts_at, kind, boundary, label)
VALUES (1, '2026-09-25 23:00+00', 'hardware_change', true, 'Hardware change');
-- the clearing step keeps rows refreshed by this call (computed_at = now(),
-- one transaction here): the earlier refresh is made older
UPDATE ap_baselines SET computed_at = computed_at - interval '1 hour';
DO $$
DECLARE b record;
BEGIN
  IF refresh_ap_baselines(1, 200, 2) <> 0 THEN RAISE EXCEPTION 'hardware: FAIL (6 readings qualified)'; END IF;
  SELECT * INTO b FROM ap_baselines WHERE device_key = 'A1';
  IF b.rssi_median IS NOT NULL OR b.hw_from IS NOT NULL THEN
    RAISE EXCEPTION 'hardware: FAIL (after the entry the statistics stayed: median %, hw_from %)', b.rssi_median, b.hw_from;
  END IF;
  RAISE NOTICE 'hardware: PASS (hardware_change entries are the boundary; a baseline never spans one; cleared right after one; adapter record checked)';
END $$;

ROLLBACK;
