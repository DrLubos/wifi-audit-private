-- Fixture test of refresh_security_periods(), refresh_sensor_periods(),
-- refresh_events() and merge_duplicate_kismet_events() (schema.sql, schema
-- 9 and 10).
--
-- Writes nothing: everything runs in one transaction that is rolled back. A
-- scratch schema first on the search path gets empty copies of every table
-- these functions read or write (detections with its own id sequence, so the
-- real one does not move) and the ap_config_changes table with its rules view built
-- from the DEPLOYED definition (schema 15); the deployed functions resolve their tables
-- through the search path. Run it as the owner (CREATE SCHEMA):
--
--   docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
--     -v ON_ERROR_STOP=1 < tests/sql/events_test.sql
--
-- Prints NOTICE "events: PASS". Cases:
--   1. security periods of an AP that goes Open -> WPA2 -> Open -> WPA2 with
--      MFP capable (a downgrade in the middle), and of an AP without changes;
--   2. sensor periods: a poll gap followed by polls without devices (one
--      outage), limited hop coverage, a single failed poll (too short), an
--      outage still going at the last poll;
--   3. events: Kismet episodes per header and AP (1 h gap; attributed to the
--      AP of transmitter_mac; an alert on an unknown BSSID has no subject;
--      SOURCEERROR is not an event; the severity override), security
--      changes graded by level, sensor outages >= 15 min with their cause,
--      the classification rule event; no client MAC anywhere in a row;
--   4. a second run changes nothing; the operator's status survives an
--      episode that grows and an outage that ends; a detector row is never
--      touched;
--   5. schema 10 - Kismet re-types devices: an alert of a transmitter that is
--      not an account AP has no subject; once it is one, its episode adopts
--      that row (same id, label kept) instead of a second row; an AP re-typed
--      as a client keeps ever_ap and its attribution; the merge of the
--      duplicates stored before schema 10 keeps the labels and notes (an
--      operator's label wins over open, the later of two labels), logs each
--      merge, leaves an ambiguous row alone and finds nothing the second time;
--   6. schemas 11-12: each of the three outcomes (confirmed, benign,
--      false_positive) needs a reason of its own, never NULL (the
--      constraint), and the merge carries the winning label's reason.

\set ON_ERROR_STOP on
BEGIN;

DO $$
DECLARE
  cfg text := rtrim(pg_get_viewdef('public.ap_config_changes_v'::regclass), E'; \n');
  t text;
BEGIN
  CREATE SCHEMA events_test;
  PERFORM set_config('search_path', 'events_test, public', true);
  FOREACH t IN ARRAY ARRAY['accounts', 'account_settings', 'networks', 'sensors', 'polls', 'devices',
                           'device_config_history', 'alerts', 'kismet_alert_rules', 'detections',
                           'ap_summary', 'aps', 'ap_sensors', 'ap_security_periods', 'sensor_periods',
                           'sensor_annotations', 'audit_log'] LOOP
    EXECUTE format('CREATE TABLE %I (LIKE public.%I INCLUDING ALL)', t, t);
  END LOOP;
  CREATE SEQUENCE events_test.detections_id;
  ALTER TABLE detections ALTER COLUMN id SET DEFAULT nextval('events_test.detections_id');
  CREATE SEQUENCE events_test.audit_log_id;
  ALTER TABLE audit_log ALTER COLUMN id SET DEFAULT nextval('events_test.audit_log_id');
  -- LIKE copies no trigger: the deployed ever_ap rule on the copy (schema 10)
  CREATE TRIGGER devices_ever_ap BEFORE INSERT OR UPDATE ON devices
    FOR EACH ROW EXECUTE FUNCTION public.devices_ever_ap();
  -- schema 15: the rules as a view, the timeline a table refresh_config_changes() fills
  EXECUTE 'CREATE VIEW ap_config_changes_v AS ' || cfg;
  CREATE TABLE ap_config_changes (LIKE public.ap_config_changes INCLUDING ALL);
END $$;

-- --- fixtures: 2026-09-20, UTC ------------------------------------------------------------
INSERT INTO accounts (id, name) VALUES (1, 'test');
INSERT INTO account_settings (account_id) VALUES (1);                 -- sensor_offline_min 15
INSERT INTO sensors (id, account_id, name, tz) VALUES (1, 1, 'test', 'UTC');
INSERT INTO networks (id, account_id, ssid, kind, expected_security, allowed_bssids)
VALUES (1, 1, 'Corp', 'managed', 'enterprise', ARRAY['A8:00:00:00:00:09'::macaddr]);
INSERT INTO kismet_alert_rules (header, as_event, severity, label) VALUES
  ('SOURCEERROR', false, NULL, 'Capture source error'),
  ('NOCLIENTMFP', true, NULL, 'Client without MFP'),
  ('BEACONRATE', true, 'low', 'Beacon rate changed'),
  ('DEAUTHFLOOD', true, NULL, 'Deauthentication flood');

-- A1: Open until 12:00, WPA2-PSK until 14:00, Open until 15:00, then WPA2-PSK
-- with MFP capable (history rows hold the configuration REPLACED at ts)
INSERT INTO devices (sensor_id, device_key, mac, type, first_seen, last_seen, ssid, cloaked, crypt,
                     crypt_bits, mfp_sup, mfp_req, adv_channel)
VALUES (1, 'A1', 'A8:00:00:00:00:01', 'ap', '2026-09-20 09:00+00', '2026-09-20 16:00+00', 'Net', false,
        'WPA2 WPA2-PSK AES-CCMP', 274945016842, true, false, '6'),
       (1, 'A2', 'A8:00:00:00:00:02', 'ap', '2026-09-20 09:00+00', '2026-09-20 16:00+00', 'Corp', false,
        'Open', 0, false, false, '1'),
       (1, 'C1', '11:00:00:00:00:01', 'client', '2026-09-20 09:00+00', '2026-09-20 16:00+00', NULL, NULL,
        NULL, NULL, NULL, NULL, NULL);
INSERT INTO device_config_history (sensor_id, device_key, ts, ssid, cloaked, crypt, crypt_bits,
                                   mfp_sup, mfp_req, adv_channel)
VALUES (1, 'A1', '2026-09-20 12:00+00', 'Net', false, 'Open', 0, false, false, '6'),
       (1, 'A1', '2026-09-20 14:00+00', 'Net', false, 'WPA2 WPA2-PSK AES-CCMP', 274945016842, false, false, '6'),
       (1, 'A1', '2026-09-20 15:00+00', 'Net', false, 'Open', 0, false, false, '6');
SELECT refresh_config_changes(x.sensor_id) FROM (SELECT DISTINCT sensor_id FROM devices) x;
INSERT INTO ap_summary (sensor_id, device_key, bssid, random_bssid, ssid, hidden, crypt, crypt_bits,
                        crypt_bits_known, mfp_sup, mfp_req, adv_channel, first_seen, last_seen,
                        hidden_beacon, has_baseline, trusted, n_obs, n_valid, n_floor, first_obs,
                        history_rows, n_changes, security, security_label, rssi_typical)
VALUES (1, 'A1', 'A8:00:00:00:00:01', false, 'Net', false, 'WPA2 WPA2-PSK AES-CCMP', 274945016842,
        274945016842, true, false, '6', '2026-09-20 09:00+00', '2026-09-20 16:00+00', false, false, false,
        100, 100, 0, '2026-09-20 09:00+00', 3, 3, 'psk_no_mfp', 'WPA2-PSK', -60),
       (1, 'A2', 'A8:00:00:00:00:02', false, 'Corp', false, 'Open', 0, 0, false, false, '1',
        '2026-09-20 09:00+00', '2026-09-20 16:00+00', false, false, false,
        100, 100, 0, '2026-09-20 10:00+00', 0, 0, 'open', 'Open', -60);
INSERT INTO aps (id, account_id, bssid, first_seen, last_seen) VALUES
  (1, 1, 'A8:00:00:00:00:01', '2026-09-20 09:00+00', '2026-09-20 16:00+00'),
  (2, 1, 'A8:00:00:00:00:02', '2026-09-20 10:00+00', '2026-09-20 16:00+00');
INSERT INTO ap_sensors (ap_id, sensor_id, device_key) VALUES (1, 1, 'A1'), (2, 1, 'A2');

-- polls every 30 s 10:00 - 11:30: a gap 10:10 - 10:20, then no devices until
-- 10:40, hop list degraded 10:45 - 10:47, one failed poll at 11:00:30, failed
-- from 11:10:30 to the end
INSERT INTO polls (sensor_id, ts, ok, ds_running, devices_active, ds_hop_ok)
SELECT 1, t,
       NOT (t = timestamptz '2026-09-20 11:00:30+00' OR t >= timestamptz '2026-09-20 11:10:30+00'),
       true,
       CASE WHEN t BETWEEN timestamptz '2026-09-20 10:20+00' AND timestamptz '2026-09-20 10:40+00' THEN 0 ELSE 5 END,
       NOT (t BETWEEN timestamptz '2026-09-20 10:45+00' AND timestamptz '2026-09-20 10:47+00')
FROM generate_series(timestamptz '2026-09-20 10:00+00', timestamptz '2026-09-20 11:30+00',
                     interval '30 seconds') AS t
WHERE NOT (t > timestamptz '2026-09-20 10:10+00' AND t < timestamptz '2026-09-20 10:20+00');
INSERT INTO sensor_annotations (sensor_id, starts_at, ends_at, kind, label)
VALUES (1, '2026-09-20 10:12+00', '2026-09-20 10:30+00', 'gap', 'Test outage');

-- Kismet alerts; the NOCLIENTMFP rows carry the client's MAC like the real ones
INSERT INTO alerts (id, sensor_id, hash, ts, header, severity, source_mac, dest_mac, transmitter_mac, text, raw)
VALUES
  (1, 1, 'h1', '2026-09-20 10:05+00', 'NOCLIENTMFP', 5, '11:00:00:00:00:01', 'A8:00:00:00:00:01',
   'A8:00:00:00:00:01', 'network BSSID A8:00:00:00:00:01 client 11:00:00:00:00:01', '{"c": "11:00:00:00:00:01"}'),
  (2, 1, 'h2', '2026-09-20 10:30+00', 'NOCLIENTMFP', 5, '11:00:00:00:00:01', 'A8:00:00:00:00:01',
   'A8:00:00:00:00:01', 'network BSSID A8:00:00:00:00:01 client 11:00:00:00:00:01', '{}'),
  (3, 1, 'h3', '2026-09-20 12:00+00', 'NOCLIENTMFP', 5, '11:00:00:00:00:01', 'A8:00:00:00:00:01',
   'A8:00:00:00:00:01', 'network BSSID A8:00:00:00:00:01 client 11:00:00:00:00:01', '{}'),
  (4, 1, 'h4', '2026-09-20 10:50+00', 'BEACONRATE', 15, 'A8:00:00:00:00:01', 'FF:FF:FF:FF:FF:FF',
   'A8:00:00:00:00:01', 'beacon rate changed', '{}'),
  (5, 1, 'h5', '2026-09-20 11:00+00', 'DEAUTHFLOOD', 10, '22:00:00:00:00:01', '11:00:00:00:00:01',
   '22:00:00:00:00:01', 'flood', '{}'),
  (6, 1, 'h6', '2026-09-20 11:05+00', 'SOURCEERROR', 20, NULL, NULL, NULL, 'source error', '{}');

-- a detector's row: never touched by refresh_events()
INSERT INTO detections (id, sensor_id, kind, ts, type, severity, device_key, mac, summary, evidence,
                        status, status_at, status_reason, status_note, updated_at)
VALUES (1000, 1, 'detection', '2026-09-20 10:00+00', 'deauth_flood', 'low', 'A1', 'A8:00:00:00:00:01',
        'detector row', '{}', 'confirmed', '2026-09-21 08:00+00', 'staged_test', 'checked by hand',
        '2026-09-21 08:00+00');

CREATE FUNCTION pg_temp.check_rows(label text, got text, want text) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
  missing text;
  extra text;
BEGIN
  EXECUTE format('SELECT string_agg(x::text, E''\n'') FROM ((%s) EXCEPT ALL (%s)) x', want, got) INTO missing;
  EXECUTE format('SELECT string_agg(x::text, E''\n'') FROM ((%s) EXCEPT ALL (%s)) x', got, want) INTO extra;
  IF missing IS NOT NULL OR extra IS NOT NULL THEN
    RAISE EXCEPTION E'events: FAIL (%)\nmissing:\n%\nunexpected:\n%',
      label, coalesce(missing, 'none'), coalesce(extra, 'none');
  END IF;
END $$;

CREATE VIEW pg_temp.got_events AS
  SELECT kind, type, device_key, to_char(ts AT TIME ZONE 'UTC', 'HH24:MI:SS') AS t0,
         to_char(ts_end AT TIME ZONE 'UTC', 'HH24:MI:SS') AS t1, severity, summary, status
  FROM detections;

-- --- 1. security periods ------------------------------------------------------------------
DO $$ BEGIN
  IF refresh_security_periods(1) <> 5 THEN RAISE EXCEPTION 'events: FAIL (security periods: row count)'; END IF;
END $$;
SELECT pg_temp.check_rows('security periods', $$
  SELECT device_key, valid_from, valid_to, crypt, crypt_bits, mfp_sup, mfp_req, level, label
  FROM ap_security_periods$$, $$
  VALUES ('A1', timestamptz '-infinity', timestamptz '2026-09-20 12:00+00', 'Open', 0::bigint, false, false, 'open', 'Open'),
         ('A1', '2026-09-20 12:00+00', '2026-09-20 14:00+00', 'WPA2 WPA2-PSK AES-CCMP', 274945016842, false, false,
          'psk_no_mfp', 'WPA2-PSK'),
         ('A1', '2026-09-20 14:00+00', '2026-09-20 15:00+00', 'Open', 0, false, false, 'open', 'Open'),
         ('A1', '2026-09-20 15:00+00', 'infinity', 'WPA2 WPA2-PSK AES-CCMP', 274945016842, true, false,
          'psk_no_mfp', 'WPA2-PSK'),
         ('A2', '-infinity', 'infinity', 'Open', 0, false, false, 'open', 'Open') $$);

-- --- 2. sensor periods --------------------------------------------------------------------
DO $$ BEGIN
  IF refresh_sensor_periods(1) <> 3 THEN RAISE EXCEPTION 'events: FAIL (sensor periods: row count)'; END IF;
END $$;
SELECT pg_temp.check_rows('sensor periods', 'SELECT kind, starts_at, ends_at, polls FROM sensor_periods', $$
  VALUES ('outage', timestamptz '2026-09-20 10:10+00', timestamptz '2026-09-20 10:40:30+00', 41),
         ('limited', '2026-09-20 10:45+00', '2026-09-20 10:47:30+00', 5),
         ('outage', '2026-09-20 11:10:30+00', NULL, 40) $$);

-- --- 3. events ----------------------------------------------------------------------------
DO $$
DECLARE r record;
BEGIN
  SELECT * INTO r FROM refresh_events(1);
  IF (r.n_kismet, r.n_config, r.n_sensor, r.n_rule) IS DISTINCT FROM (4, 3, 2, 1) THEN
    RAISE EXCEPTION 'events: FAIL (first run returned %)', r;
  END IF;
END $$;
SELECT pg_temp.check_rows('events, first run', 'SELECT * FROM pg_temp.got_events', $$
  VALUES ('detection', 'deauth_flood', 'A1', '10:00:00', NULL, 'low', 'detector row', 'confirmed'),
         ('kismet', 'NOCLIENTMFP', 'A1', '10:05:00', '10:30:00', 'low', 'Client without MFP (2 alerts)', 'open'),
         ('kismet', 'NOCLIENTMFP', 'A1', '12:00:00', '12:00:00', 'low', 'Client without MFP', 'open'),
         ('kismet', 'BEACONRATE', 'A1', '10:50:00', '10:50:00', 'low', 'Beacon rate changed', 'open'),
         ('kismet', 'DEAUTHFLOOD', NULL, '11:00:00', '11:00:00', 'medium', 'Deauthentication flood', 'open'),
         ('config', 'security_change', 'A1', '12:00:00', NULL, 'info', 'Security Open → WPA2-PSK', 'open'),
         ('config', 'security_change', 'A1', '14:00:00', NULL, 'high', 'Security WPA2-PSK → Open', 'open'),
         ('config', 'security_change', 'A1', '15:00:00', NULL, 'info', 'Security Open → WPA2-PSK', 'open'),
         ('sensor', 'sensor_outage', NULL, '10:10:00', '10:40:30', 'medium', 'Sensor not collecting', 'open'),
         ('sensor', 'sensor_outage', NULL, '11:10:30', NULL, 'medium', 'Sensor not collecting', 'open'),
         ('detection', 'unknown_bssid_managed_ssid', 'A2', '10:00:00', NULL, 'high',
          'Unknown BSSID broadcasting managed SSID "Corp"', 'open') $$);
SELECT pg_temp.check_rows('event details', $$
  SELECT type, to_char(ts AT TIME ZONE 'UTC', 'HH24:MI') AS t0, mac::text,
         evidence->>'cause' AS cause, evidence->>'alerts' AS alerts, evidence->'to'->>'mfp' AS mfp_to
  FROM detections WHERE type IN ('sensor_outage', 'NOCLIENTMFP', 'DEAUTHFLOOD', 'security_change')$$, $$
  VALUES ('sensor_outage', '10:10', NULL, 'Test outage', NULL, NULL),
         ('sensor_outage', '11:10', NULL, NULL, NULL, NULL),
         ('NOCLIENTMFP', '10:05', 'a8:00:00:00:00:01', NULL, '2', NULL),
         ('NOCLIENTMFP', '12:00', 'a8:00:00:00:00:01', NULL, '1', NULL),
         ('DEAUTHFLOOD', '11:00', NULL, NULL, '1', NULL),
         ('security_change', '12:00', 'a8:00:00:00:00:01', NULL, NULL, 'no'),
         ('security_change', '14:00', 'a8:00:00:00:00:01', NULL, NULL, 'no'),
         ('security_change', '15:00', 'a8:00:00:00:00:01', NULL, NULL, 'capable') $$);
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM detections d
             WHERE d::text ILIKE '%11:00:00:00:00:01%' OR d::text ILIKE '%22:00:00:00:00:01%') THEN
    RAISE EXCEPTION 'events: FAIL (a client or unknown MAC reached an event row)';
  END IF;
END $$;

-- --- 4. a second run; the operator's labels survive changes -------------------------------
DO $$
DECLARE r record;
BEGIN
  SELECT * INTO r FROM refresh_events(1);
  IF (r.n_kismet, r.n_config, r.n_sensor, r.n_rule) IS DISTINCT FROM (0, 0, 0, 0) THEN
    RAISE EXCEPTION 'events: FAIL (second run returned %, expected no change)', r;
  END IF;
END $$;

UPDATE detections SET status = 'false_positive', status_at = '2026-09-21 09:00+00', status_reason = 'rule_defect',
                      status_cause = 'kismet_rule', status_note = 'lab test'
WHERE type = 'NOCLIENTMFP' AND ts = '2026-09-20 10:05+00';
UPDATE detections SET status = 'benign', status_at = '2026-09-21 09:00+00', status_reason = 'expected_behaviour',
                      status_cause = 'sensor_degraded', status_note = 'known outage'
WHERE type = 'sensor_outage' AND ts = '2026-09-20 11:10:30+00';
-- schema 12: every outcome needs a reason of its own (a NULL reason too is refused)
DO $$
DECLARE bad text[][] := ARRAY[['confirmed', NULL], ['benign', NULL], ['false_positive', NULL],
                              ['confirmed', 'rule_defect'], ['confirmed', 'expected_behaviour'],
                              ['benign', 'security_relevant'], ['benign', 'artefact'],
                              ['false_positive', 'staged_test'], ['false_positive', 'accepted_risk'],
                              ['open', 'artefact'], ['closed', NULL]];
  i int;
BEGIN
  FOR i IN 1 .. array_length(bad, 1) LOOP
    BEGIN
      UPDATE detections SET status = bad[i][1], status_reason = bad[i][2], status_at = now(),
                            status_cause = CASE WHEN bad[i][1] IN ('benign', 'false_positive') THEN 'unknown' END
      WHERE type = 'NOCLIENTMFP' AND ts = '2026-09-20 10:05+00';
      RAISE EXCEPTION 'events: FAIL (status % with reason % accepted)', bad[i][1], bad[i][2];
    EXCEPTION WHEN check_violation THEN NULL;
    END;
  END LOOP;
END $$;
CREATE TEMP TABLE ids AS SELECT id, type, ts FROM detections;

-- the first NOCLIENTMFP episode grows; the ongoing outage ends at 11:31
INSERT INTO alerts (id, sensor_id, hash, ts, header, severity, source_mac, dest_mac, transmitter_mac, text, raw)
VALUES (7, 1, 'h7', '2026-09-20 10:50+00', 'NOCLIENTMFP', 5, '11:00:00:00:00:01', 'A8:00:00:00:00:01',
        'A8:00:00:00:00:01', 'network BSSID A8:00:00:00:00:01 client 11:00:00:00:00:01', '{}');
INSERT INTO polls (sensor_id, ts, ok, ds_running, devices_active, ds_hop_ok)
VALUES (1, '2026-09-20 11:31+00', true, true, 5, true);
SELECT refresh_sensor_periods(1);
DO $$
DECLARE r record;
BEGIN
  SELECT * INTO r FROM refresh_events(1);
  IF (r.n_kismet, r.n_config, r.n_sensor, r.n_rule) IS DISTINCT FROM (1, 0, 1, 0) THEN
    RAISE EXCEPTION 'events: FAIL (run after the changes returned %)', r;
  END IF;
END $$;
SELECT pg_temp.check_rows('labels kept', $$
  SELECT d.type, to_char(d.ts AT TIME ZONE 'UTC', 'HH24:MI:SS'), to_char(d.ts_end AT TIME ZONE 'UTC', 'HH24:MI:SS'),
         d.summary, d.status, d.status_note, d.id = i.id AS same_row
  FROM detections d JOIN ids i ON i.type = d.type AND i.ts = d.ts
  WHERE d.status <> 'open'$$, $$
  VALUES ('deauth_flood', '10:00:00', NULL, 'detector row', 'confirmed', 'checked by hand', true),
         ('NOCLIENTMFP', '10:05:00', '10:50:00', 'Client without MFP (3 alerts)', 'false_positive', 'lab test', true),
         ('sensor_outage', '11:10:30', '11:31:00', 'Sensor not collecting', 'benign', 'known outage', true) $$);
DO $$ BEGIN
  IF (SELECT count(*) FROM detections) <> 11 THEN
    RAISE EXCEPTION 'events: FAIL (% rows after the changes, expected 11)', (SELECT count(*) FROM detections);
  END IF;
  IF (SELECT updated_at FROM detections WHERE id = 1000) <> '2026-09-21 08:00+00' THEN
    RAISE EXCEPTION 'events: FAIL (the detector row was touched)';
  END IF;
END $$;

-- --- 5. schema 10: re-typed devices, the merge of old duplicates --------------------------
-- C2 is a client when its alert comes in: no account AP, so no subject
INSERT INTO devices (sensor_id, device_key, mac, type, first_seen, last_seen)
VALUES (1, 'C2', '33:00:00:00:00:03', 'client', '2026-09-20 13:00+00', '2026-09-20 13:30+00');
INSERT INTO alerts (id, sensor_id, hash, ts, header, severity, source_mac, dest_mac, transmitter_mac, text, raw)
VALUES (8, 1, 'h8', '2026-09-20 13:10+00', 'NOCLIENTMFP', 5, '11:00:00:00:00:01', '33:00:00:00:00:03',
        '33:00:00:00:00:03', 'network BSSID 33:00:00:00:00:03 client 11:00:00:00:00:01', '{}');
DO $$
DECLARE r record;
BEGIN
  IF (SELECT ever_ap FROM devices WHERE device_key = 'C2') THEN
    RAISE EXCEPTION 'events: FAIL (a client without an AP configuration got ever_ap)';
  END IF;
  SELECT * INTO r FROM refresh_events(1);
  IF (r.n_kismet, r.n_config, r.n_sensor, r.n_rule) IS DISTINCT FROM (1, 0, 0, 0) THEN
    RAISE EXCEPTION 'events: FAIL (the client''s alert returned %)', r;
  END IF;
END $$;
UPDATE detections SET status = 'benign', status_at = '2026-09-21 10:00+00', status_reason = 'expected_behaviour',
                      status_cause = 'client_behaviour', status_note = 'seen as a client'
WHERE type = 'NOCLIENTMFP' AND ts = '2026-09-20 13:10+00' AND mac IS NULL AND device_key IS NULL;
CREATE TEMP TABLE c2_row AS SELECT id FROM detections WHERE type = 'NOCLIENTMFP' AND ts = '2026-09-20 13:10+00';

-- Kismet re-types C2 as an AP; refresh_rollups() would add its account AP
UPDATE devices SET type = 'ap' WHERE device_key = 'C2';
INSERT INTO aps (id, account_id, bssid, first_seen, last_seen)
VALUES (3, 1, '33:00:00:00:00:03', '2026-09-20 13:00+00', '2026-09-20 13:30+00');
INSERT INTO ap_sensors (ap_id, sensor_id, device_key) VALUES (3, 1, 'C2');
-- ... and A1 as a client: it stays an AP
UPDATE devices SET type = 'client' WHERE device_key = 'A1';
DO $$
DECLARE r record;
BEGIN
  IF NOT (SELECT ever_ap FROM devices WHERE device_key = 'C2') OR NOT (SELECT ever_ap FROM devices WHERE device_key = 'A1') THEN
    RAISE EXCEPTION 'events: FAIL (ever_ap: C2 % A1 %, expected both true)',
      (SELECT ever_ap FROM devices WHERE device_key = 'C2'), (SELECT ever_ap FROM devices WHERE device_key = 'A1');
  END IF;
  SELECT * INTO r FROM refresh_events(1);
  IF (r.n_kismet, r.n_config, r.n_sensor, r.n_rule) IS DISTINCT FROM (1, 0, 0, 0) THEN
    RAISE EXCEPTION 'events: FAIL (the run after re-typing returned %, expected the adoption only)', r;
  END IF;
END $$;
SELECT pg_temp.check_rows('the AP''s episode adopted the row without a subject', $$
  SELECT d.type, d.device_key, d.mac::text, to_char(d.ts AT TIME ZONE 'UTC', 'HH24:MI'), d.status, d.status_note,
         d.id = (SELECT id FROM c2_row) AS same_row
  FROM detections d WHERE d.type = 'NOCLIENTMFP' AND d.ts >= '2026-09-20 13:00+00'$$, $$
  VALUES ('NOCLIENTMFP', 'C2', '33:00:00:00:00:03', '13:10', 'benign', 'seen as a client', true) $$);
SELECT pg_temp.check_rows('A1 re-typed as a client keeps its episodes', $$
  SELECT device_key, to_char(ts AT TIME ZONE 'UTC', 'HH24:MI') FROM detections
  WHERE kind = 'kismet' AND device_key = 'A1'$$, $$
  VALUES ('A1', '10:05'), ('A1', '12:00'), ('A1', '10:50') $$);
DO $$
DECLARE r record;
BEGIN
  SELECT * INTO r FROM refresh_events(1);
  IF (r.n_kismet, r.n_config, r.n_sensor, r.n_rule) IS DISTINCT FROM (0, 0, 0, 0) THEN
    RAISE EXCEPTION 'events: FAIL (a run after the adoption returned %, expected no change)', r;
  END IF;
  IF (SELECT count(*) FROM detections) <> 12 THEN
    RAISE EXCEPTION 'events: FAIL (% rows after the adoption, expected 12)', (SELECT count(*) FROM detections);
  END IF;
END $$;

-- duplicates as refresh_events() stored them before schema 10: an attributed
-- row and a twin without a subject for the same window
INSERT INTO detections (id, sensor_id, kind, ts, ts_end, type, severity, device_key, mac, summary, evidence,
                        status, status_at, status_reason, status_note, status_cause)
VALUES
  -- both labelled: the later label (the twin's, with its reason) wins, both notes kept
  (2000, 1, 'kismet', '2026-09-20 14:00+00', '2026-09-20 14:00+00', 'DISCONCODEINVALID', 'low', 'A1',
   'A8:00:00:00:00:01', 'x', '{}', 'confirmed', '2026-09-21 10:00+00', 'security_relevant', 'kept note', NULL),
  (2001, 1, 'kismet', '2026-09-20 14:00+00', '2026-09-20 14:00+00', 'DISCONCODEINVALID', 'low', NULL, NULL,
   'x', '{}', 'false_positive', '2026-09-22 10:00+00', 'artefact', 'twin note', 'channel_hopping'),
  -- the kept row is open: it takes the twin's label and note
  (2002, 1, 'kismet', '2026-09-20 15:00+00', '2026-09-20 15:05+00', 'DEAUTHFLOOD', 'medium', 'A1',
   'A8:00:00:00:00:01', 'x', '{}', 'open', NULL, NULL, NULL, NULL),
  (2003, 1, 'kismet', '2026-09-20 15:00+00', '2026-09-20 15:05+00', 'DEAUTHFLOOD', 'medium', NULL, NULL,
   'x', '{}', 'false_positive', '2026-09-21 11:00+00', 'rule_defect', 'lab', 'client_behaviour'),
  -- two APs with the same window: the twin is ambiguous and stays
  (2004, 1, 'kismet', '2026-09-20 16:00+00', '2026-09-20 16:00+00', 'BSSTIMESTAMP', 'low', 'A1',
   'A8:00:00:00:00:01', 'x', '{}', 'open', NULL, NULL, NULL, NULL),
  (2005, 1, 'kismet', '2026-09-20 16:00+00', '2026-09-20 16:00+00', 'BSSTIMESTAMP', 'low', 'A2',
   'A8:00:00:00:00:02', 'x', '{}', 'open', NULL, NULL, NULL, NULL),
  (2006, 1, 'kismet', '2026-09-20 16:00+00', '2026-09-20 16:00+00', 'BSSTIMESTAMP', 'low', NULL, NULL,
   'x', '{}', 'open', NULL, NULL, NULL, NULL);
CREATE TEMP TABLE merges AS SELECT * FROM merge_duplicate_kismet_events(1);   -- once: it writes
SELECT pg_temp.check_rows('merge: the merges', 'SELECT * FROM merges', $$
  VALUES (2000::bigint, 2001::bigint, 'DISCONCODEINVALID', 'false_positive', E'kept note\ntwin note'),
         (2002, 2003, 'DEAUTHFLOOD', 'false_positive', 'lab') $$);
SELECT pg_temp.check_rows('merge: the rows left', $$
  SELECT id, status, status_at, status_reason, status_cause, status_note FROM detections WHERE id >= 2000$$, $$
  VALUES (2000::bigint, 'false_positive', timestamptz '2026-09-22 10:00+00', 'artefact', 'channel_hopping',
          E'kept note\ntwin note'),
         (2002, 'false_positive', '2026-09-21 11:00+00', 'rule_defect', 'client_behaviour', 'lab'),
         (2004, 'open', NULL, NULL, NULL, NULL), (2005, 'open', NULL, NULL, NULL, NULL),
         (2006, 'open', NULL, NULL, NULL, NULL) $$);
SELECT pg_temp.check_rows('merge: audit_log', $$
  SELECT action, target, old->'merged'->>'id', old->'merged'->>'status_note', new->>'status' FROM audit_log$$, $$
  VALUES ('event.merge', 'event:2000', '2001', 'twin note', 'false_positive'),
         ('event.merge', 'event:2002', '2003', 'lab', 'false_positive') $$);
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM merge_duplicate_kismet_events(1)) THEN
    RAISE EXCEPTION 'events: FAIL (a second merge found more)';
  END IF;
  RAISE NOTICE 'events: PASS (security and sensor periods; Kismet, config, sensor and rule events; no client MAC; idempotent; labels survive; schema 10: re-typed devices, adoption, the merge of old duplicates; schema 12: three outcomes, each with a required reason that survives a merge; schema 17: the cause too)';
END $$;

ROLLBACK;
