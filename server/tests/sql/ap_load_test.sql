-- Fixture test of load_ap_load_samples() and ap_load_hourly (schema.sql,
-- schema 8): per-poll AP load samples -> rows per AP, UTC hour and channel.
--
-- Writes nothing: everything runs in one transaction that is rolled back. A
-- scratch schema first on the search path gets empty copies of sensors,
-- devices, device_config_history and ap_load_hourly; the session temp table
-- stage_load_samples is what the importer and the dump backfill fill; the
-- deployed load_ap_load_samples() then runs against them (its table names
-- resolve through the search path). Run it as the owner (CREATE SCHEMA):
--
--   docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
--     -v ON_ERROR_STOP=1 < tests/sql/ap_load_test.sql
--
-- Prints NOTICE "ap_load: PASS". Cases:
--   1. aggregation: an AP that changes channel inside an hour (two rows, the
--      channel from the configuration history; a sample at the very ts of a
--      change already has the new channel), an AP with three channel periods
--      and a history row without a channel, a poll with a BSS Load value > 0
--      vs one without, a duplicate sample counted once, an AP without any
--      load value, a client and an unknown device ignored; the channel of
--      every sample equals ap_channel_at() (the loader computes it
--      set-based);
--   2. loading the same samples again changes nothing;
--   3. a shorter window of an hour (fewer polls) does not replace the stored
--      row; a fuller one does.

\set ON_ERROR_STOP on
BEGIN;

DO $$
DECLARE t text;
BEGIN
  CREATE SCHEMA ap_load_test;
  PERFORM set_config('search_path', 'ap_load_test, public', true);
  FOREACH t IN ARRAY ARRAY['sensors', 'devices', 'device_config_history', 'ap_load_hourly'] LOOP
    EXECUTE format('CREATE TABLE %I (LIKE public.%I INCLUDING ALL)', t, t);
  END LOOP;
END $$;

-- --- fixtures: 2026-09-20, UTC ---------------------------------------------------------
INSERT INTO sensors (id, name, tz) VALUES (1, 'test', 'UTC');
INSERT INTO devices (sensor_id, device_key, mac, type, first_seen, last_seen, crypt, adv_channel)
VALUES (1, 'A', 'A8:00:00:00:00:01', 'ap', '2026-09-20 10:00+00', '2026-09-20 11:05+00', 'WPA2', '6'),
       (1, 'B', 'A8:00:00:00:00:02', 'ap', '2026-09-20 10:20+00', '2026-09-20 10:20+00', 'WPA2', '36'),
       (1, 'E', 'A8:00:00:00:00:05', 'ap', '2026-09-20 10:00+00', '2026-09-20 10:50+00', 'WPA2', '36'),
       (1, 'C', '11:00:00:00:00:01', 'client', '2026-09-20 10:00+00', '2026-09-20 10:00+00', NULL, NULL);
-- A was on channel 1 until 10:30 (the configuration replaced at 10:30); E on
-- 1 until 10:05, then 11 (an incomplete record at 10:15 says nothing) until
-- 10:25, then 36 (current)
INSERT INTO device_config_history (sensor_id, device_key, ts, crypt, adv_channel)
VALUES (1, 'A', '2026-09-20 10:30+00', 'WPA2', '1'),
       (1, 'E', '2026-09-20 10:05+00', 'WPA2', '1'),
       (1, 'E', '2026-09-20 10:15+00', 'WPA2', NULL),
       (1, 'E', '2026-09-20 10:25+00', 'WPA2', '11');

CREATE TEMP TABLE stage_load_samples (
  ts timestamptz, device_key text, n_clients integer, qbss_stations integer, util_pct real);
INSERT INTO stage_load_samples VALUES
  ('2026-09-20 10:00+00', 'A', 2, 0, 0),       -- channel 1, no BSS Load value
  ('2026-09-20 10:10+00', 'A', 3, 1, 10),      -- channel 1
  ('2026-09-20 10:30+00', 'A', 4, 0, 0),       -- the change's own ts: channel 6
  ('2026-09-20 10:40+00', 'A', 5, 2, 20),      -- channel 6
  ('2026-09-20 10:40+00', 'A', 5, 2, 20),      -- duplicate sample: counted once
  ('2026-09-20 11:05+00', 'A', 1, 0, 5),       -- utilisation > 0: the AP sends BSS Load
  ('2026-09-20 10:20+00', 'B', 0, 0, 0),       -- an AP without any load value
  ('2026-09-20 10:00+00', 'E', 1, 0, 0),       -- channel 1
  ('2026-09-20 10:05+00', 'E', 1, 0, 0),       -- channel 11
  ('2026-09-20 10:20+00', 'E', 1, 0, 0),       -- channel 11
  ('2026-09-20 10:25+00', 'E', 1, 0, 0),       -- channel 36
  ('2026-09-20 10:50+00', 'E', 1, 0, 0),       -- channel 36
  ('2026-09-20 10:00+00', 'C', 7, 0, 0),       -- a client: ignored
  ('2026-09-20 10:00+00', 'Z', 1, 1, 1);       -- unknown device: ignored

CREATE FUNCTION pg_temp.check_rows(label text, got text, want text) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
  missing text;
  extra text;
BEGIN
  EXECUTE format('SELECT string_agg(x::text, E''\n'') FROM ((%s) EXCEPT ALL (%s)) x', want, got) INTO missing;
  EXECUTE format('SELECT string_agg(x::text, E''\n'') FROM ((%s) EXCEPT ALL (%s)) x', got, want) INTO extra;
  IF missing IS NOT NULL OR extra IS NOT NULL THEN
    RAISE EXCEPTION E'ap_load: FAIL (%)\nmissing:\n%\nunexpected:\n%',
      label, coalesce(missing, 'none'), coalesce(extra, 'none');
  END IF;
END $$;

CREATE FUNCTION pg_temp.check_load(label text, want_hours integer, want_ins integer, want_upd integer)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE r record;
BEGIN
  SELECT * INTO r FROM load_ap_load_samples(1);
  IF (r.hours, r.inserted, r.updated) IS DISTINCT FROM (want_hours, want_ins, want_upd) THEN
    RAISE EXCEPTION 'ap_load: FAIL (% returned %, expected (%,%,%))',
      label, r, want_hours, want_ins, want_upd;
  END IF;
END $$;

CREATE VIEW pg_temp.got AS
  SELECT device_key, extract(hour FROM hour AT TIME ZONE 'UTC')::int AS h, channel, polls,
         clients_polls, clients_max, clients_sum, qbss_polls, qbss_max, qbss_sum,
         round(util_max::numeric, 1) AS util_max, round(util_sum::numeric, 1) AS util_sum, source
  FROM ap_load_hourly;

-- --- 1. aggregation ---------------------------------------------------------------------
SELECT pg_temp.check_load('first load', 7, 7, 0);
SELECT pg_temp.check_rows('first load', 'SELECT * FROM pg_temp.got', $$
  VALUES ('A', 10, '1', 2, 2, 3, 5, 1, 1, 1, 10.0, 10.0, 'v5_obs'),
         ('A', 10, '6', 2, 2, 5, 9, 1, 2, 2, 20.0, 20.0, 'v5_obs'),
         ('A', 11, '6', 1, 1, 1, 1, 1, 0, 0, 5.0, 5.0, 'v5_obs'),
         ('B', 10, '36', 1, 1, 0, 0, 0, NULL, NULL, NULL, NULL, 'v5_obs'),
         ('E', 10, '1', 1, 1, 1, 1, 0, NULL, NULL, NULL, NULL, 'v5_obs'),
         ('E', 10, '11', 2, 2, 1, 2, 0, NULL, NULL, NULL, NULL, 'v5_obs'),
         ('E', 10, '36', 2, 2, 1, 2, 0, NULL, NULL, NULL, NULL, 'v5_obs') $$);
-- the set-based channel equals ap_channel_at() for every sample
SELECT pg_temp.check_rows('channel = ap_channel_at()',
  'SELECT device_key, hour, channel, polls FROM ap_load_hourly',
  $$SELECT x.device_key, date_trunc('hour', x.ts, 'UTC'),
           coalesce(ap_channel_at(1, x.device_key, x.ts), ''), count(DISTINCT x.ts)::integer
    FROM stage_load_samples x
    JOIN devices d ON d.sensor_id = 1 AND d.device_key = x.device_key AND d.type = 'ap'
    GROUP BY 1, 2, 3$$);

-- --- 2. the same samples again -----------------------------------------------------------
CREATE TEMP TABLE first_rows AS SELECT * FROM pg_temp.got;
SELECT pg_temp.check_load('same samples again', 7, 0, 0);
SELECT pg_temp.check_rows('same samples again', 'SELECT * FROM pg_temp.got', 'SELECT * FROM first_rows');

-- --- 3. shorter and fuller windows of one hour -----------------------------------------
TRUNCATE stage_load_samples;
INSERT INTO stage_load_samples VALUES ('2026-09-20 10:00+00', 'A', 9, 9, 90);   -- 1 poll < 2 stored
SELECT pg_temp.check_load('shorter window', 1, 0, 0);
SELECT pg_temp.check_rows('shorter window', 'SELECT * FROM pg_temp.got', 'SELECT * FROM first_rows');

TRUNCATE stage_load_samples;
INSERT INTO stage_load_samples VALUES
  ('2026-09-20 10:00+00', 'A', 2, 0, 0),
  ('2026-09-20 10:10+00', 'A', 3, 1, 10),
  ('2026-09-20 10:20+00', 'A', 4, 3, 30);                                         -- 3 polls > 2
SELECT pg_temp.check_load('fuller window', 1, 0, 1);
SELECT pg_temp.check_rows('fuller window', 'SELECT * FROM pg_temp.got', $$
  SELECT * FROM first_rows WHERE NOT (device_key = 'A' AND h = 10 AND channel = '1')
  UNION ALL
  VALUES ('A', 10, '1', 3, 3, 4, 9, 2, 3, 4, 30.0, 40.0, 'v5_obs') $$);

DO $$ BEGIN
  RAISE NOTICE 'ap_load: PASS (aggregation per AP, hour and channel = ap_channel_at(); duplicates, clients and unknown devices ignored; re-load idempotent; the row with more polls wins)';
END $$;

ROLLBACK;
