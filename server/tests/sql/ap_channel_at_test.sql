-- Fixture test of ap_channel_at() (schema.sql, schema 8): the advertised
-- channel of an AP at a point in time, used to attribute per-poll AP load
-- samples to a channel (load_ap_load_samples()).
--
-- Writes nothing: everything runs in one transaction that is rolled back. A
-- scratch schema first on the search path gets empty copies of devices and
-- device_config_history and the ap_config_changes table with its rules view built
-- from the DEPLOYED definition (schema 15); the deployed ap_channel_at() then reads them
-- (its table names resolve through the search path). Run it as the owner
-- (CREATE SCHEMA):
--
--   docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
--     -v ON_ERROR_STOP=1 < tests/sql/ap_channel_at_test.sql
--
-- Prints NOTICE "ap_channel_at: PASS". Cases: hand-computed channels around
-- the pre-v5 churn (A -> incomplete record -> A), a real change, a state
-- without a beacon record, an AP whose current record has no channel, an
-- unknown device; and the property that ap_channel_at() agrees with every
-- adv_channel change of ap_config_changes (old value just before, new value
-- from the change on).

\set ON_ERROR_STOP on
BEGIN;

DO $$
DECLARE
  cfg text := rtrim(pg_get_viewdef('public.ap_config_changes_v'::regclass), E'; \n');
BEGIN
  CREATE SCHEMA ap_channel_at_test;
  PERFORM set_config('search_path', 'ap_channel_at_test, public', true);
  CREATE TABLE devices (LIKE public.devices INCLUDING DEFAULTS);
  CREATE TABLE device_config_history (LIKE public.device_config_history INCLUDING DEFAULTS);
  -- schema 15: the rules as a view, the timeline a table refresh_config_changes() fills
  EXECUTE 'CREATE VIEW ap_config_changes_v AS ' || cfg;
  CREATE TABLE ap_config_changes (LIKE public.ap_config_changes INCLUDING ALL);
END $$;

-- --- fixtures: 2026-09-20, UTC --------------------------------------------------------
-- A: channel 1, an incomplete record (crypt known, channel NULL, the pre-v5
--    churn), 1 again, a change to 6 at 10:20 and to 11 at 10:30 (current)
-- B: no history, channel 36
-- C: a history state without a beacon record (crypt and channel NULL), current 6
-- D: history channel 44, current record without a channel
INSERT INTO devices (sensor_id, device_key, mac, type, first_seen, last_seen, crypt, adv_channel)
VALUES (1, 'A', 'A8:00:00:00:00:01', 'ap', '2026-09-20 09:00+00', '2026-09-20 12:00+00', 'WPA2', '11'),
       (1, 'B', 'A8:00:00:00:00:02', 'ap', '2026-09-20 09:00+00', '2026-09-20 12:00+00', 'WPA2', '36'),
       (1, 'C', 'A8:00:00:00:00:03', 'ap', '2026-09-20 09:00+00', '2026-09-20 12:00+00', 'WPA2', '6'),
       (1, 'D', 'A8:00:00:00:00:04', 'ap', '2026-09-20 09:00+00', '2026-09-20 12:00+00', NULL, NULL);
-- history rows hold the configuration REPLACED at ts
INSERT INTO device_config_history (sensor_id, device_key, ts, crypt, adv_channel)
VALUES (1, 'A', '2026-09-20 10:00+00', 'WPA2', '1'),
       (1, 'A', '2026-09-20 10:10+00', 'WPA2', NULL),
       (1, 'A', '2026-09-20 10:20+00', 'WPA2', '1'),
       (1, 'A', '2026-09-20 10:30+00', 'WPA2', '6'),
       (1, 'C', '2026-09-20 10:00+00', NULL, NULL),
       (1, 'D', '2026-09-20 10:00+00', 'WPA2', '44');
SELECT refresh_config_changes(x.sensor_id) FROM (SELECT DISTINCT sensor_id FROM devices) x;

CREATE FUNCTION pg_temp.check_rows(label text, got text, want text) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
  missing text;
  extra text;
BEGIN
  EXECUTE format('SELECT string_agg(x::text, E''\n'') FROM ((%s) EXCEPT ALL (%s)) x', want, got) INTO missing;
  EXECUTE format('SELECT string_agg(x::text, E''\n'') FROM ((%s) EXCEPT ALL (%s)) x', got, want) INTO extra;
  IF missing IS NOT NULL OR extra IS NOT NULL THEN
    RAISE EXCEPTION E'ap_channel_at: FAIL (%)\nmissing:\n%\nunexpected:\n%',
      label, coalesce(missing, 'none'), coalesce(extra, 'none');
  END IF;
END $$;

-- --- 1. hand-computed channels --------------------------------------------------------
CREATE TEMP TABLE ch_probes (device_key text, at timestamptz, want text);
INSERT INTO ch_probes VALUES
  ('A', '2026-09-20 09:00+00', '1'),      -- before any history: the first replaced state
  ('A', '2026-09-20 10:00+00', '1'),      -- the incomplete record at 10:10 says nothing
  ('A', '2026-09-20 10:15+00', '1'),
  ('A', '2026-09-20 10:20+00', '6'),      -- 1 replaced at 10:20 by the state replaced at 10:30
  ('A', '2026-09-20 10:29:59+00', '6'),
  ('A', '2026-09-20 10:30+00', '11'),     -- the current record from 10:30 on
  ('A', '2026-09-20 12:00+00', '11'),
  ('B', '2026-09-20 10:00+00', '36'),
  ('C', '2026-09-20 09:00+00', '6'),      -- the no-record state is skipped
  ('D', '2026-09-20 09:00+00', '44'),
  ('D', '2026-09-20 10:00+00', NULL),     -- the current record knows no channel
  ('Z', '2026-09-20 10:00+00', NULL);     -- unknown device

SELECT pg_temp.check_rows('hand-computed',
  'SELECT device_key, at, ap_channel_at(1, device_key, at) FROM ch_probes',
  'SELECT device_key, at, want FROM ch_probes');

-- --- 2. agreement with ap_config_changes ---------------------------------------------
DO $$
DECLARE
  n_changes integer;
  n_bad integer;
BEGIN
  SELECT count(*) INTO n_changes FROM ap_config_changes WHERE field = 'adv_channel';
  SELECT count(*) INTO n_bad
  FROM ap_config_changes c
  WHERE c.field = 'adv_channel'
    AND (ap_channel_at(c.sensor_id, c.device_key, c.ts - interval '1 second') IS DISTINCT FROM c.old_value
         OR ap_channel_at(c.sensor_id, c.device_key, c.ts) IS DISTINCT FROM c.new_value);
  IF n_changes <> 2 OR n_bad <> 0 THEN
    RAISE EXCEPTION 'ap_channel_at: FAIL (% adv_channel changes in the fixtures, expected 2; % disagree)',
      n_changes, n_bad;
  END IF;
  RAISE NOTICE 'ap_channel_at: PASS (12 hand-computed probes; agrees with the % adv_channel changes of ap_config_changes)',
    n_changes;
END $$;

ROLLBACK;
