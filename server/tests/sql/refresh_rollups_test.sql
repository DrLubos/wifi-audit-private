-- Fixture test of refresh_rollups() and the dashboard tables (schema.sql,
-- schema 6).
--
-- Writes nothing: everything runs in one transaction that is rolled back. A
-- scratch schema, first on the search path, gets empty copies of the source
-- tables, the four read tables and the ap_config_changes materialized view
-- (built from the deployed definition); the deployed refresh_rollups() then
-- runs against them (its table names resolve through the search path). No
-- real row is read or written; explicit ids, so no real sequence moves. Run it
-- as the owner (CREATE SCHEMA):
--
--   docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
--     -v ON_ERROR_STOP=1 < tests/sql/refresh_rollups_test.sql
--
-- Prints NOTICE "refresh_rollups: PASS"; a failing case raises an exception
-- with the differing rows. Cases:
--   1. a full refresh, compared with hand-computed rows: RSSI statistics over
--      valid readings only (a raw -106 floor, a v5 floor flag and a no-reading
--      row are counted, never averaged), a poll gap split over three hours, a
--      hidden AP (cloaked beacon + named history record = no change event,
--      name kept), a channel change, a foreign country, a client and a bridged
--      device kept out of the AP tables;
--   2. an incremental refresh after a later import - new polls closing a gap,
--      a new AP, a client re-classified as an AP, an AP re-classified as
--      bridged - leaves exactly the rows of a full refresh;
--   3. a second full refresh changes nothing (idempotent).
-- Schema 9 adds: the collecting / limited polls per hour, the security
-- columns of ap_summary, ap_security_periods, the account-level aps and
-- ap_sensors (an AP re-classified away keeps its aps row, loses its mapping),
-- sensor_periods, and the derived events of refresh_events() - each checked
-- after the full and the incremental run, and incremental = full for all of
-- them. (tests/sql/events_test.sql covers the event rules in detail.)

\set ON_ERROR_STOP on
BEGIN;

DO $$
DECLARE
  cfg text := rtrim(pg_get_viewdef('public.ap_config_changes'::regclass), E'; \n');
  t text;
BEGIN
  CREATE SCHEMA refresh_rollups_test;
  PERFORM set_config('search_path', 'refresh_rollups_test, public', true);
  FOREACH t IN ARRAY ARRAY['sensors', 'polls', 'devices', 'device_config_history', 'observations',
                           'alerts', 'ap_baselines', 'ap_rssi_hourly', 'sensor_hourly',
                           'ap_summary', 'sensor_summary',
                           -- schema 9
                           'accounts', 'account_settings', 'networks', 'aps', 'ap_sensors',
                           'ap_security_periods', 'sensor_periods', 'sensor_annotations',
                           'kismet_alert_rules', 'detections'] LOOP
    EXECUTE format('CREATE TABLE %I (LIKE public.%I INCLUDING ALL)', t, t);
  END LOOP;
  -- the copies keep the real tables' sequence defaults: give the two tables
  -- the refresh inserts into without an id their own sequences
  CREATE SEQUENCE refresh_rollups_test.aps_id;
  ALTER TABLE aps ALTER COLUMN id SET DEFAULT nextval('refresh_rollups_test.aps_id');
  CREATE SEQUENCE refresh_rollups_test.detections_id;
  ALTER TABLE detections ALTER COLUMN id SET DEFAULT nextval('refresh_rollups_test.detections_id');
  EXECUTE 'CREATE MATERIALIZED VIEW ap_config_changes AS ' || cfg || ' WITH NO DATA';
  CREATE UNIQUE INDEX ON ap_config_changes (sensor_id, device_key, ts, field);
END $$;

-- --- fixtures: 2026-09-20, UTC -------------------------------------------------------
INSERT INTO accounts (id, name) VALUES (1, 'test');
INSERT INTO account_settings (account_id) VALUES (1);
INSERT INTO sensors (id, name, tz) VALUES (1, 'test', 'UTC');

-- polls: 10:00-10:59:30 every 30 s (10:05 failed; ds_hop_ok NULL before 10:30,
-- true 10:30-10:44:30, false after), a gap to 12:20, then 12:20-12:29:30
INSERT INTO polls (sensor_id, ts, new_obs, ok, ds_hop_ok, ds_running, devices_active)
SELECT 1, t, 1, t <> timestamptz '2026-09-20 10:05:00+00',
       CASE WHEN t < timestamptz '2026-09-20 10:30:00+00' THEN NULL
            WHEN t < timestamptz '2026-09-20 10:45:00+00' THEN true ELSE false END, true, 3
FROM generate_series(timestamptz '2026-09-20 10:00:00+00', timestamptz '2026-09-20 10:59:30+00',
                     interval '30 seconds') t
UNION ALL
SELECT 1, t, 1, true, true, true, 3
FROM generate_series(timestamptz '2026-09-20 12:20:00+00', timestamptz '2026-09-20 12:29:30+00',
                     interval '30 seconds') t;

INSERT INTO devices (sensor_id, device_key, mac, type, first_seen, last_seen, ssid, cloaked,
                     crypt, crypt_bits, mfp_sup, mfp_req, adv_channel, ht_mode, beacon_rate, country)
VALUES
  (1, 'A1', 'A8:00:00:00:00:01', 'ap', '2026-09-20 10:00+00', '2026-09-20 12:21+00', 'Net', false,
   'WPA2', 274945016842, false, false, '6', 'HT20', 100, 'SK'),
  (1, 'A2', 'A8:00:00:00:00:02', 'ap', '2026-09-20 12:00+00', '2026-09-20 12:22+00', '', true,
   'WPA2', 274945016842, false, false, '1', 'HT20', 100, 'SK'),
  (1, 'A3', '02:00:00:00:00:03', 'ap', '2026-09-20 10:10+00', '2026-09-20 10:10+00', 'Phone', false,
   'WPA2', 274945016842, false, false, '11', 'HT20', 100, 'US'),
  (1, 'C1', '11:00:00:00:00:01', 'client', '2026-09-20 10:00+00', '2026-09-20 10:00+00',
   NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL),
  (1, 'B1', '11:00:00:00:00:02', 'bridged', '2026-09-20 12:25+00', '2026-09-20 12:25+00',
   NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL);
-- the latest AP load (schema 7), copied into ap_summary
UPDATE devices SET cur_n_clients = 3, cur_qbss_stations = 2, cur_util_pct = 12.5,
                   cur_at = '2026-09-20 12:21+00' WHERE device_key = 'A1';

-- observations: A1 in hour 10 has 5 valid readings, a raw floor (pre-v5), a v5
-- floor flag and a poll without a reading. The importer keeps AP rows only
-- (schema 7); the client and bridged rows check that the refresh does not
-- rely on that (and C1 is re-classified as an AP in case 2).
INSERT INTO observations (sensor_id, ts, device_key, rssi, rssi_floor)
SELECT 1, ts, k, r, f
FROM (VALUES
  ('A1', timestamptz '2026-09-20 10:00:00+00', -50::smallint, NULL::boolean),
  ('A1', '2026-09-20 10:01:00+00', -52, NULL),
  ('A1', '2026-09-20 10:02:00+00', -54, NULL),
  ('A1', '2026-09-20 10:03:00+00', -56, NULL),
  ('A1', '2026-09-20 10:04:00+00', -58, NULL),
  ('A1', '2026-09-20 10:05:30+00', -106, NULL),
  ('A1', '2026-09-20 10:06:00+00', NULL, true),
  ('A1', '2026-09-20 10:07:00+00', NULL, NULL),
  ('A1', '2026-09-20 12:20:00+00', -60, NULL),
  ('A1', '2026-09-20 12:21:00+00', -62, NULL),
  ('A2', '2026-09-20 12:22:00+00', -70, NULL),
  ('A3', '2026-09-20 10:10:00+00', -80, NULL),
  ('C1', '2026-09-20 10:00:00+00', -40, NULL),
  ('B1', '2026-09-20 12:25:00+00', -45, NULL)) AS v(k, ts, r, f);

-- history (the REPLACED configuration): A1 changed channel 1 -> 6 at 10:30;
-- A2 is a hidden AP whose named, uncloaked record was replaced at 10:40
INSERT INTO device_config_history (sensor_id, device_key, ts, ssid, cloaked, crypt, crypt_bits,
                                   mfp_sup, mfp_req, adv_channel, ht_mode, beacon_rate, country)
VALUES
  (1, 'A1', '2026-09-20 10:30+00', 'Net', false, 'WPA2', 274945016842, false, false, '1', 'HT20', 100, 'SK'),
  (1, 'A2', '2026-09-20 10:40+00', 'HiddenName', false, 'WPA2', 274945016842, false, false, '1', 'HT20', 100, 'SK');

INSERT INTO alerts (id, sensor_id, hash, ts, header, raw) VALUES
  (1, 1, 'h1', '2026-09-20 10:15+00', 'DEAUTHFLOOD', '{}'),
  (2, 1, 'h2', '2026-09-20 12:25+00', 'BCASTDISCON', '{}');

INSERT INTO ap_baselines (sensor_id, device_key, bssid, ssid, crypt, trusted, rssi_median,
                          rssi_robust_sd, band, n_obs, n_floor, n_days, computed_at)
VALUES (1, 'A1', 'A8:00:00:00:00:01', 'Net', 'WPA2', true, -55, 1.5, '2.4', 300, 3, 2,
        '2026-09-21 00:00+00');

-- --- helpers ------------------------------------------------------------------------------
CREATE FUNCTION pg_temp.check_rows(label text, got text, want text) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
  missing text;
  extra text;
BEGIN
  EXECUTE format('SELECT string_agg(x::text, E''\n'') FROM ((%s) EXCEPT ALL (%s)) x', want, got) INTO missing;
  EXECUTE format('SELECT string_agg(x::text, E''\n'') FROM ((%s) EXCEPT ALL (%s)) x', got, want) INTO extra;
  IF missing IS NOT NULL OR extra IS NOT NULL THEN
    RAISE EXCEPTION E'refresh_rollups: FAIL (%)\nmissing:\n%\nunexpected:\n%',
      label, coalesce(missing, 'none'), coalesce(extra, 'none');
  END IF;
END $$;

-- the four tables as comparable rows (reals rounded, refreshed_at left out)
CREATE VIEW pg_temp.got_ap_hourly AS
  SELECT device_key, extract(hour FROM hour AT TIME ZONE 'UTC')::int AS h, n_obs, n_valid, n_floor,
         round(median::numeric, 1) AS median, round(p10::numeric, 1) AS p10,
         round(p90::numeric, 1) AS p90, min, max
  FROM ap_rssi_hourly;
CREATE VIEW pg_temp.got_sensor_hourly AS
  SELECT extract(hour FROM hour AT TIME ZONE 'UTC')::int AS h, polls, polls_ok, hop_ok_polls,
         hop_known_polls, gap_s, active_aps, new_aps, alerts, ap_obs
  FROM sensor_hourly;
CREATE VIEW pg_temp.got_ap_summary AS
  SELECT device_key, bssid, random_bssid, hidden, hidden_beacon, name_seen, country, has_baseline,
         trusted, rssi_median, band, cur_n_clients, cur_util_pct, cur_at,
         n_obs, n_valid, n_floor, first_obs, last_obs, history_rows,
         n_changes, last_change_at, last_change
  FROM ap_summary;
CREATE VIEW pg_temp.got_sensor_summary AS
  SELECT first_poll, last_poll, polls, polls_ok, gap_count, gap_total_s, gap_max_s, gaps,
         devices_by_type, obs_total, obs_ap, country_expected, alerts, alerts_last_ts, baselines
  FROM sensor_summary;
-- schema 9
CREATE VIEW pg_temp.got_capture AS
  SELECT extract(hour FROM hour AT TIME ZONE 'UTC')::int AS h, capture_polls, limited_polls FROM sensor_hourly;
CREATE VIEW pg_temp.got_security AS
  SELECT device_key, security, security_label, round(rssi_typical::numeric, 1) AS rssi_typical FROM ap_summary;
CREATE VIEW pg_temp.got_periods AS
  SELECT device_key, valid_from, valid_to, level, label FROM ap_security_periods;
CREATE VIEW pg_temp.got_aps AS
  SELECT bssid::text AS bssid, first_seen, last_seen FROM aps;
CREATE VIEW pg_temp.got_ap_sensors AS
  SELECT x.device_key, a.bssid::text AS bssid FROM ap_sensors x JOIN aps a ON a.id = x.ap_id;
CREATE VIEW pg_temp.got_sensor_periods AS
  SELECT kind, to_char(starts_at AT TIME ZONE 'UTC', 'HH24:MI:SS') AS t0,
         to_char(ends_at AT TIME ZONE 'UTC', 'HH24:MI:SS') AS t1, polls
  FROM sensor_periods;
CREATE VIEW pg_temp.got_last_capture AS
  SELECT last_capture_at, last_hop_n, last_hop_visited, last_hop_ok FROM sensor_summary;
CREATE VIEW pg_temp.got_events AS
  SELECT kind, type, device_key, to_char(ts AT TIME ZONE 'UTC', 'HH24:MI:SS') AS t0,
         to_char(ts_end AT TIME ZONE 'UTC', 'HH24:MI:SS') AS t1, severity, summary
  FROM detections;

-- --- 1. full refresh -----------------------------------------------------------------
DO $$
DECLARE r record;
BEGIN
  SELECT * INTO r FROM refresh_rollups(1);
  IF r.from_hour <> '-infinity' OR r.ap_hours <> 4 OR r.sensor_hours <> 3 OR r.aps <> 3 THEN
    RAISE EXCEPTION 'refresh_rollups: FAIL (full run returned %)', r;
  END IF;
END $$;

-- A1 hour 10: valid -58 -56 -54 -52 -50 -> median -54, p10 -57.2, p90 -50.8
SELECT pg_temp.check_rows('ap_rssi_hourly, full', 'SELECT * FROM pg_temp.got_ap_hourly', $$
  VALUES ('A1', 10, 8, 5, 2, -54.0, -57.2, -50.8, -58::smallint, -50::smallint),
         ('A1', 12, 2, 2, 0, -61.0, -61.8, -60.2, -62, -60),
         ('A2', 12, 1, 1, 0, -70.0, -70.0, -70.0, -70, -70),
         ('A3', 10, 1, 1, 0, -80.0, -80.0, -80.0, -80, -80) $$);

-- gap 10:59:30 -> 12:20:00 = 4830 s: 30 s in hour 10, 3600 in 11, 1200 in 12
SELECT pg_temp.check_rows('sensor_hourly, full', 'SELECT * FROM pg_temp.got_sensor_hourly', $$
  VALUES (10, 120, 119, 30, 60, 30::real, 2, 2, 1, 9),
         (11, 0, 0, 0, 0, 3600, 0, 0, 0, 0),
         (12, 20, 20, 20, 20, 1200, 2, 1, 1, 3) $$);

SELECT pg_temp.check_rows('ap_summary, full', 'SELECT * FROM pg_temp.got_ap_summary', $$
  VALUES ('A1', macaddr 'A8:00:00:00:00:01', false, false, false, 'Net', 'SK', true, true, -55::real,
          '2.4', 3, 12.5::real, timestamptz '2026-09-20 12:21+00',
          10::bigint, 7::bigint, 2::bigint, timestamptz '2026-09-20 10:00+00', timestamptz '2026-09-20 12:21+00',
          1, 1, timestamptz '2026-09-20 10:30+00', '[{"field": "adv_channel", "old": "1", "new": "6"}]'::jsonb),
         ('A2', 'A8:00:00:00:00:02', false, true, true, 'HiddenName', 'SK', false, false, NULL,
          NULL, NULL, NULL, NULL,
          1, 1, 0, '2026-09-20 12:22+00', '2026-09-20 12:22+00', 1, 0, NULL, NULL),
         ('A3', '02:00:00:00:00:03', true, false, false, 'Phone', 'US', false, false, NULL,
          NULL, NULL, NULL, NULL,
          1, 1, 0, '2026-09-20 10:10+00', '2026-09-20 10:10+00', 0, 0, NULL, NULL) $$);

SELECT pg_temp.check_rows('sensor_summary, full', 'SELECT * FROM pg_temp.got_sensor_summary', $$
  VALUES (timestamptz '2026-09-20 10:00+00', timestamptz '2026-09-20 12:29:30+00', 140, 139, 1,
          4830::float8, 4830::float8,
          '[{"gap_start": "2026-09-20T10:59:30+00:00", "gap_end": "2026-09-20T12:20:00+00:00", "gap_s": 4830}]'::jsonb,
          '{"ap": 3, "client": 1, "bridged": 1}'::jsonb, 140::bigint, 12::bigint, 'SK', 2,
          timestamptz '2026-09-20 12:25+00', 1) $$);

-- schema 9 after the full run
SELECT pg_temp.check_rows('capture, full', 'SELECT * FROM pg_temp.got_capture', $$
  VALUES (10, 119, 30), (11, 0, 0), (12, 20, 0) $$);
SELECT pg_temp.check_rows('security, full', 'SELECT * FROM pg_temp.got_security', $$
  VALUES ('A1', 'psk_no_mfp', 'WPA2-PSK', -57.5), ('A2', 'psk_no_mfp', 'WPA2-PSK', -70.0),
         ('A3', 'psk_no_mfp', 'WPA2-PSK', -80.0) $$);
SELECT pg_temp.check_rows('security periods, full', 'SELECT * FROM pg_temp.got_periods', $$
  VALUES ('A1', timestamptz '-infinity', timestamptz 'infinity', 'psk_no_mfp', 'WPA2-PSK'),
         ('A2', '-infinity', 'infinity', 'psk_no_mfp', 'WPA2-PSK'),
         ('A3', '-infinity', 'infinity', 'psk_no_mfp', 'WPA2-PSK') $$);
SELECT pg_temp.check_rows('aps, full', 'SELECT * FROM pg_temp.got_aps', $$
  VALUES ('a8:00:00:00:00:01', timestamptz '2026-09-20 10:00+00', timestamptz '2026-09-20 12:21+00'),
         ('a8:00:00:00:00:02', '2026-09-20 12:22+00', '2026-09-20 12:22+00'),
         ('02:00:00:00:00:03', '2026-09-20 10:10+00', '2026-09-20 10:10+00') $$);
SELECT pg_temp.check_rows('ap_sensors, full', 'SELECT * FROM pg_temp.got_ap_sensors', $$
  VALUES ('A1', 'a8:00:00:00:00:01'), ('A2', 'a8:00:00:00:00:02'), ('A3', '02:00:00:00:00:03') $$);
-- hop list degraded 10:45 - 10:59:30 (29 polls), then the gap to 12:20
SELECT pg_temp.check_rows('sensor periods, full', 'SELECT * FROM pg_temp.got_sensor_periods', $$
  VALUES ('limited', '10:45:00', '10:59:30', 29), ('outage', '10:59:30', '12:20:00', 0) $$);
SELECT pg_temp.check_rows('events, full', 'SELECT * FROM pg_temp.got_events', $$
  VALUES ('kismet', 'DEAUTHFLOOD', NULL, '10:15:00', '10:15:00', 'info', 'DEAUTHFLOOD'),
         ('kismet', 'BCASTDISCON', NULL, '12:25:00', '12:25:00', 'info', 'BCASTDISCON'),
         ('sensor', 'sensor_limited', NULL, '10:45:00', '10:59:30', 'low', 'Capture limited (hop list degraded)'),
         ('sensor', 'sensor_outage', NULL, '10:59:30', '12:20:00', 'medium', 'Sensor not collecting') $$);

-- --- 2. a later import, then an incremental refresh ---------------------------------------
INSERT INTO polls (sensor_id, ts, new_obs, ok, ds_hop_ok, ds_running, devices_active, ds_hop_n,
                   ds_hop_visited)
SELECT 1, t, 1, true, true, true, 3, 13, 13
FROM generate_series(timestamptz '2026-09-20 13:00:00+00', timestamptz '2026-09-20 13:04:30+00',
                     interval '30 seconds') t;
INSERT INTO devices (sensor_id, device_key, mac, type, first_seen, last_seen, ssid, cloaked, crypt,
                     crypt_bits, mfp_sup, mfp_req, adv_channel, country)
VALUES (1, 'A4', 'A8:00:00:00:00:04', 'ap', '2026-09-20 13:02+00', '2026-09-20 13:02+00', 'New',
        false, 'WPA2', 274945016842, false, false, '36', 'SK');
UPDATE devices SET type = 'ap' WHERE device_key = 'C1';        -- re-classified, history from 10:00
UPDATE devices SET type = 'bridged' WHERE device_key = 'A3';   -- no longer an AP
INSERT INTO observations (sensor_id, ts, device_key, rssi) VALUES
  (1, '2026-09-20 13:00+00', 'A1', -65),
  (1, '2026-09-20 13:02+00', 'A4', -75);
INSERT INTO alerts (id, sensor_id, hash, ts, header, raw) VALUES
  (3, 1, 'h3', '2026-09-20 13:03+00', 'DEAUTHFLOOD', '{}');

DO $$
DECLARE r record;
BEGIN
  -- p_from 13:00 -> hour of the last poll before it (12:29:30) = 12:00; the
  -- re-classifications move the sensor hours back to 10:00
  SELECT * INTO r FROM refresh_rollups(1, '2026-09-20 13:00+00');
  IF r.from_hour <> timestamptz '2026-09-20 10:00+00' OR r.aps <> 4 THEN
    RAISE EXCEPTION 'refresh_rollups: FAIL (incremental run returned %)', r;
  END IF;
END $$;

SELECT pg_temp.check_rows('ap_rssi_hourly, incremental', 'SELECT * FROM pg_temp.got_ap_hourly', $$
  VALUES ('A1', 10, 8, 5, 2, -54.0, -57.2, -50.8, -58::smallint, -50::smallint),
         ('A1', 12, 2, 2, 0, -61.0, -61.8, -60.2, -62, -60),
         ('A1', 13, 1, 1, 0, -65.0, -65.0, -65.0, -65, -65),
         ('A2', 12, 1, 1, 0, -70.0, -70.0, -70.0, -70, -70),
         ('A4', 13, 1, 1, 0, -75.0, -75.0, -75.0, -75, -75),
         ('C1', 10, 1, 1, 0, -40.0, -40.0, -40.0, -40, -40) $$);

-- gap 12:29:30 -> 13:00:00 adds 1830 s to hour 12
SELECT pg_temp.check_rows('sensor_hourly, incremental', 'SELECT * FROM pg_temp.got_sensor_hourly', $$
  VALUES (10, 120, 119, 30, 60, 30::real, 2, 2, 1, 9),
         (11, 0, 0, 0, 0, 3600, 0, 0, 0, 0),
         (12, 20, 20, 20, 20, 3030, 2, 1, 1, 3),
         (13, 10, 10, 10, 10, 0, 2, 1, 1, 2) $$);

-- schema 9 after the incremental run: A3 (now bridged) keeps its aps row but
-- loses its mapping; C1 (now an AP) and A4 are new account APs
SELECT pg_temp.check_rows('capture, incremental', 'SELECT * FROM pg_temp.got_capture', $$
  VALUES (10, 119, 30), (11, 0, 0), (12, 20, 0), (13, 10, 0) $$);
SELECT pg_temp.check_rows('security, incremental', 'SELECT * FROM pg_temp.got_security', $$
  VALUES ('A1', 'psk_no_mfp', 'WPA2-PSK', -61.0), ('A2', 'psk_no_mfp', 'WPA2-PSK', -70.0),
         ('A4', 'psk_no_mfp', 'WPA2-PSK', -75.0), ('C1', 'unknown', 'Unknown', -40.0) $$);
SELECT pg_temp.check_rows('aps, incremental', 'SELECT * FROM pg_temp.got_aps', $$
  VALUES ('a8:00:00:00:00:01', timestamptz '2026-09-20 10:00+00', timestamptz '2026-09-20 13:00+00'),
         ('a8:00:00:00:00:02', '2026-09-20 12:22+00', '2026-09-20 12:22+00'),
         ('02:00:00:00:00:03', '2026-09-20 10:10+00', '2026-09-20 10:10+00'),
         ('a8:00:00:00:00:04', '2026-09-20 13:02+00', '2026-09-20 13:02+00'),
         ('11:00:00:00:00:01', '2026-09-20 10:00+00', '2026-09-20 10:00+00') $$);
SELECT pg_temp.check_rows('ap_sensors, incremental', 'SELECT * FROM pg_temp.got_ap_sensors', $$
  VALUES ('A1', 'a8:00:00:00:00:01'), ('A2', 'a8:00:00:00:00:02'), ('A4', 'a8:00:00:00:00:04'),
         ('C1', '11:00:00:00:00:01') $$);
SELECT pg_temp.check_rows('sensor periods, incremental', 'SELECT * FROM pg_temp.got_sensor_periods', $$
  VALUES ('limited', '10:45:00', '10:59:30', 29), ('outage', '10:59:30', '12:20:00', 0),
         ('outage', '12:29:30', '13:00:00', 0) $$);
SELECT pg_temp.check_rows('events, incremental', 'SELECT * FROM pg_temp.got_events', $$
  VALUES ('kismet', 'DEAUTHFLOOD', NULL, '10:15:00', '10:15:00', 'info', 'DEAUTHFLOOD'),
         ('kismet', 'BCASTDISCON', NULL, '12:25:00', '12:25:00', 'info', 'BCASTDISCON'),
         ('kismet', 'DEAUTHFLOOD', NULL, '13:03:00', '13:03:00', 'info', 'DEAUTHFLOOD'),
         ('sensor', 'sensor_limited', NULL, '10:45:00', '10:59:30', 'low', 'Capture limited (hop list degraded)'),
         ('sensor', 'sensor_outage', NULL, '10:59:30', '12:20:00', 'medium', 'Sensor not collecting'),
         ('sensor', 'sensor_outage', NULL, '12:29:30', '13:00:00', 'medium', 'Sensor not collecting') $$);

CREATE TEMP TABLE inc_ap_hourly AS SELECT * FROM pg_temp.got_ap_hourly;
CREATE TEMP TABLE inc_sensor_hourly AS SELECT * FROM pg_temp.got_sensor_hourly;
CREATE TEMP TABLE inc_ap_summary AS SELECT * FROM pg_temp.got_ap_summary;
CREATE TEMP TABLE inc_sensor_summary AS SELECT * FROM pg_temp.got_sensor_summary;
CREATE TEMP TABLE inc_capture AS SELECT * FROM pg_temp.got_capture;
CREATE TEMP TABLE inc_security AS SELECT * FROM pg_temp.got_security;
CREATE TEMP TABLE inc_periods AS SELECT * FROM pg_temp.got_periods;
CREATE TEMP TABLE inc_aps AS SELECT * FROM pg_temp.got_aps;
CREATE TEMP TABLE inc_ap_sensors AS SELECT * FROM pg_temp.got_ap_sensors;
CREATE TEMP TABLE inc_sensor_periods AS SELECT * FROM pg_temp.got_sensor_periods;
CREATE TEMP TABLE inc_events AS SELECT * FROM pg_temp.got_events;

-- --- 2b/3. the same after a full refresh, twice -------------------------------------------
SELECT count(*) FROM refresh_rollups(1, NULL);
SELECT pg_temp.check_rows('incremental = full: ' || t, format('SELECT * FROM pg_temp.got_%s', t),
                          format('SELECT * FROM inc_%s', t))
FROM unnest(ARRAY['ap_hourly', 'sensor_hourly', 'ap_summary', 'sensor_summary', 'capture', 'security',
                  'periods', 'aps', 'ap_sensors', 'sensor_periods', 'events']) t;
SELECT count(*) FROM refresh_rollups(1, NULL);
SELECT pg_temp.check_rows('idempotent: ' || t, format('SELECT * FROM pg_temp.got_%s', t),
                          format('SELECT * FROM inc_%s', t))
FROM unnest(ARRAY['ap_hourly', 'sensor_hourly', 'ap_summary', 'sensor_summary', 'capture', 'security',
                  'periods', 'aps', 'ap_sensors', 'sensor_periods', 'events']) t;

-- --- 4. the adapter lost (2026-10-06): Kismet still answers, its datasource
-- reports no channels, then the polls fail. The last capture keeps its hop list.
SELECT pg_temp.check_rows('last capture, before', 'SELECT * FROM pg_temp.got_last_capture', $$
  VALUES (timestamptz '2026-09-20 13:04:30+00', 13, 13, true) $$);
INSERT INTO polls (sensor_id, ts, new_obs, ok, ds_hop_ok, ds_running, devices_active, ds_hop_n,
                   ds_hop_visited)
SELECT 1, t, 0, true, false, false, 0, 0, 0
FROM generate_series(timestamptz '2026-09-20 13:05:00+00', timestamptz '2026-09-20 13:06:30+00',
                     interval '30 seconds') t
UNION ALL
SELECT 1, '2026-09-20 13:07:00+00', 0, false, NULL, NULL, NULL, NULL, NULL;
SELECT count(*) FROM refresh_rollups(1, '2026-09-20 13:05+00');
SELECT pg_temp.check_rows('last capture, adapter lost', 'SELECT * FROM pg_temp.got_last_capture', $$
  VALUES (timestamptz '2026-09-20 13:04:30+00', 13, 13, true) $$);

DO $$ BEGIN
  RAISE NOTICE 'refresh_rollups: PASS (full run vs hand-computed rows; incremental run = full run after a new AP, 2 re-classifications and a closed gap; idempotent; schema 9: capture polls, security, periods, aps, sensor periods, events, the hop list of the last capture)';
END $$;

ROLLBACK;
