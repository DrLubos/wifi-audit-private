-- Fixture test of ingest_stage_create() and ingest_merge() (schema.sql, schema
-- 14): the one merge of the snapshot import and the live ingest.
--
-- Writes nothing: everything runs in one transaction that is rolled back. A
-- scratch schema first on the search path gets empty copies of the tables the
-- merge writes; the session staging tables are the ones ingest_stage_create()
-- makes; the deployed functions then run against the copies (their table
-- names resolve through the search path). Run it as the owner (CREATE SCHEMA):
--
--   docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
--     -v ON_ERROR_STOP=1 < tests/sql/ingest_merge_test.sql
--
-- Prints NOTICE "ingest_merge: PASS". Cases:
--   1. a snapshot: every table merged in foreign-key order, the client device
--      and client tables accepted, non-AP observations and load rows dropped,
--      polls with seq and received_at, a v5 load sample through
--      load_ap_load_samples() on the channel of the history merged before,
--      the returned counts, ingest_state.dirty_from = the earliest new ts;
--   2. the same stage again: nothing inserted twice, every table unchanged;
--   3. a live batch: a new client device, a client's update, client tables
--      and a client's frequency histogram are ignored; a newer AP row wins its
--      configuration, a NULL config_changed_at / cur_at keeps the stored
--      values, first_seen does not move later; ts_offset_s stored; a
--      duplicate alert counted as a conflict; dirty_from stays the earliest;
--   4. an older device row loses the configuration but widens first_seen; a
--      load row with fewer polls loses, one with more wins;
--   5. a snapshot fills the seq of a poll stored without one;
--   6. a device-only change marks the sensor dirty from now();
--   7. unknown sensor and unknown via are refused; ingest_stage_create() is
--      idempotent.

\set ON_ERROR_STOP on
BEGIN;
SET LOCAL TimeZone = 'UTC';

DO $$
DECLARE t text;
BEGIN
  CREATE SCHEMA ingest_merge_test;
  PERFORM set_config('search_path', 'ingest_merge_test, public', true);
  FOREACH t IN ARRAY ARRAY['sensors', 'devices', 'polls', 'device_config_history', 'observations',
                           'ap_load_hourly', 'device_freq_hist', 'associations', 'client_bssids',
                           'probes', 'alerts', 'ingest_state'] LOOP
    EXECUTE format('CREATE TABLE %I (LIKE public.%I INCLUDING ALL)', t, t);
  END LOOP;
  -- LIKE copies no trigger: the deployed ever_ap rule on the copy (schema 10)
  CREATE TRIGGER devices_ever_ap BEFORE INSERT OR UPDATE ON devices
    FOR EACH ROW EXECUTE FUNCTION public.devices_ever_ap();
  -- the copied default would draw ids from the real alerts sequence
  CREATE SEQUENCE alerts_id_seq;
  ALTER TABLE alerts ALTER COLUMN id SET DEFAULT nextval('ingest_merge_test.alerts_id_seq');
END $$;

INSERT INTO sensors (id, name, tz) VALUES (1, 'test', 'UTC');

CREATE FUNCTION pg_temp.u(t text) RETURNS bigint
LANGUAGE sql IMMUTABLE AS $$ SELECT extract(epoch FROM t::timestamptz)::bigint $$;

CREATE FUNCTION pg_temp.check_rows(label text, got text, want text) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
  missing text;
  extra text;
BEGIN
  EXECUTE format('SELECT string_agg(x::text, E''\n'') FROM ((%s) EXCEPT ALL (%s)) x', want, got) INTO missing;
  EXECUTE format('SELECT string_agg(x::text, E''\n'') FROM ((%s) EXCEPT ALL (%s)) x', got, want) INTO extra;
  IF missing IS NOT NULL OR extra IS NOT NULL THEN
    RAISE EXCEPTION E'ingest_merge: FAIL (%)\nmissing:\n%\nunexpected:\n%',
      label, coalesce(missing, 'none'), coalesce(extra, 'none');
  END IF;
END $$;

CREATE FUNCTION pg_temp.check_eq(label text, got text, want text) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
  IF got IS DISTINCT FROM want THEN
    RAISE EXCEPTION 'ingest_merge: FAIL (%: got %, expected %)', label, got, want;
  END IF;
END $$;

CREATE FUNCTION pg_temp.clear_stage() RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
  TRUNCATE stage_devices, stage_polls, stage_device_config_history, stage_observations,
           stage_ap_load_hourly, stage_load_samples, stage_device_freq_hist, stage_associations,
           stage_client_bssids, stage_probes, stage_alerts;
END $$;

-- the views the cases compare (text, so EXCEPT ALL compares like with like)
CREATE VIEW pg_temp.got_devices AS
  SELECT device_key, mac::text, type, first_seen::text, last_seen::text, ssid, crypt, adv_channel,
         config_changed_at::text, cur_n_clients, cur_at::text, ever_ap
  FROM devices;
CREATE VIEW pg_temp.got_polls AS
  SELECT ts::text, seq, received_at IS NOT NULL AS received, ts_offset_s, ok FROM polls;
CREATE VIEW pg_temp.got_counts AS
  SELECT 'devices' AS t, count(*) FROM devices UNION ALL
  SELECT 'polls', count(*) FROM polls UNION ALL
  SELECT 'device_config_history', count(*) FROM device_config_history UNION ALL
  SELECT 'observations', count(*) FROM observations UNION ALL
  SELECT 'ap_load_hourly', count(*) FROM ap_load_hourly UNION ALL
  SELECT 'device_freq_hist', count(*) FROM device_freq_hist UNION ALL
  SELECT 'associations', count(*) FROM associations UNION ALL
  SELECT 'client_bssids', count(*) FROM client_bssids UNION ALL
  SELECT 'probes', count(*) FROM probes UNION ALL
  SELECT 'alerts', count(*) FROM alerts;

SELECT ingest_stage_create();
SELECT ingest_stage_create();          -- 7. idempotent (no error, no second table)

-- --- 1. a snapshot -------------------------------------------------------------------------
INSERT INTO stage_devices (device_key, mac, type, first_seen, last_seen, ssid, cloaked, crypt,
                           crypt_bits, mfp_sup, mfp_req, adv_channel, config_changed_at,
                           cur_n_clients, cur_qbss_stations, cur_util_pct, cur_at)
VALUES ('A', 'A8:00:00:00:00:01', 'ap', pg_temp.u('2026-09-20 10:00+00'), pg_temp.u('2026-09-20 10:10+00'),
        'Net', 0, 'WPA2', 1, 1, 0, '6', pg_temp.u('2026-09-20 10:10+00'), 3, 2, 12.5, pg_temp.u('2026-09-20 10:10+00')),
       ('C', '11:00:00:00:00:01', 'client', pg_temp.u('2026-09-20 10:00+00'), pg_temp.u('2026-09-20 10:10+00'),
        NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL);
INSERT INTO stage_polls (seq, ts, kismet_ts, devices_active, ds_running, ok)
VALUES (1, pg_temp.u('2026-09-20 10:00+00'), pg_temp.u('2026-09-20 10:00+00'), 2, 1, 1),
       (2, pg_temp.u('2026-09-20 10:10+00'), pg_temp.u('2026-09-20 10:10+00'), 2, 1, 1);
INSERT INTO stage_device_config_history (device_key, ts, ssid, crypt, adv_channel)
VALUES ('A', pg_temp.u('2026-09-20 10:10+00'), 'Net', 'WPA2', '1');      -- channel 1 until 10:10
INSERT INTO stage_observations (ts, device_key, rssi, pk_total)
VALUES (pg_temp.u('2026-09-20 10:00+00'), 'A', -60, 10),
       (pg_temp.u('2026-09-20 10:10+00'), 'A', -61, 20),
       (pg_temp.u('2026-09-20 10:00+00'), 'C', -70, 5);                  -- a client: dropped
INSERT INTO stage_ap_load_hourly (device_key, hour, channel, polls, clients_polls, clients_max,
                                  clients_sum, qbss_polls)
VALUES ('A', pg_temp.u('2026-09-20 10:00+00'), '6', 2, 2, 3, 5, 0),
       ('C', pg_temp.u('2026-09-20 10:00+00'), '', 1, 0, NULL, NULL, 0);  -- a client: dropped
INSERT INTO stage_load_samples (ts, device_key, n_clients, qbss_stations, util_pct)
VALUES ('2026-09-20 09:30+00', 'A', 4, 0, 0);                            -- v5 sample: channel 1
INSERT INTO stage_device_freq_hist (device_key, freq_khz, packets, updated_ts)
VALUES ('A', 2437000, 10, pg_temp.u('2026-09-20 10:10+00')),
       ('C', 2412000, 10, pg_temp.u('2026-09-20 10:10+00'));
INSERT INTO stage_associations VALUES ('A', '11:00:00:00:00:01', pg_temp.u('2026-09-20 10:00+00'), pg_temp.u('2026-09-20 10:10+00'));
INSERT INTO stage_client_bssids VALUES ('C', 'A8:00:00:00:00:01', pg_temp.u('2026-09-20 10:00+00'), pg_temp.u('2026-09-20 10:10+00'));
INSERT INTO stage_probes VALUES ('C', 'Home', pg_temp.u('2026-09-20 10:00+00'), pg_temp.u('2026-09-20 10:10+00'));
INSERT INTO stage_alerts (hash, ts, header, severity, transmitter_mac, raw)
VALUES ('h1', pg_temp.u('2026-09-20 10:05+00'), 'DEAUTHFLOOD', 10, 'A8:00:00:00:00:01', '{}');

CREATE TEMP TABLE r1 AS SELECT ingest_merge(1, 'snapshot') AS j;

SELECT pg_temp.check_rows('snapshot: counts', 'SELECT * FROM pg_temp.got_counts', $$
  VALUES ('devices', 2::bigint), ('polls', 2), ('device_config_history', 1), ('observations', 2),
         ('ap_load_hourly', 2), ('device_freq_hist', 2), ('associations', 1), ('client_bssids', 1),
         ('probes', 1), ('alerts', 1) $$);
SELECT pg_temp.check_rows('snapshot: polls', 'SELECT * FROM pg_temp.got_polls', $$
  VALUES ('2026-09-20 10:00:00+00', 1::bigint, true, NULL::real, true),
         ('2026-09-20 10:10:00+00', 2, true, NULL, true) $$);
SELECT pg_temp.check_rows('snapshot: load',
  $$SELECT device_key, hour::text, channel, polls, source FROM ap_load_hourly$$, $$
  VALUES ('A', '2026-09-20 10:00:00+00', '6', 2, 'buffer'),
         ('A', '2026-09-20 09:00:00+00', '1', 1, 'v5_obs') $$);
SELECT pg_temp.check_eq('snapshot: inserted polls', (SELECT j #>> '{inserted,polls}' FROM r1), '2');
SELECT pg_temp.check_eq('snapshot: inserted observations', (SELECT j #>> '{inserted,observations}' FROM r1), '2');
SELECT pg_temp.check_eq('snapshot: non-AP observation not inserted', (SELECT j #>> '{conflicts,observations}' FROM r1), '1');
SELECT pg_temp.check_eq('snapshot: staged devices', (SELECT j #>> '{staged,devices}' FROM r1), '2');
SELECT pg_temp.check_eq('snapshot: min_ts', (SELECT (j ->> 'min_ts')::timestamptz::text FROM r1), '2026-09-20 10:00:00+00');
SELECT pg_temp.check_eq('snapshot: via', (SELECT j ->> 'via' FROM r1), 'snapshot');
SELECT pg_temp.check_eq('snapshot: dirty_from', (SELECT dirty_from::text FROM ingest_state WHERE sensor_id = 1),
                        '2026-09-20 10:00:00+00');
SELECT pg_temp.check_eq('snapshot: dirty_since', (SELECT (dirty_since IS NOT NULL)::text FROM ingest_state), 'true');

-- --- 2. the same stage again ----------------------------------------------------------------
CREATE TEMP TABLE before2_devices AS SELECT * FROM pg_temp.got_devices;
CREATE TEMP TABLE before2_counts AS SELECT * FROM pg_temp.got_counts;
CREATE TEMP TABLE before2_load AS SELECT device_key, hour, channel, polls, source FROM ap_load_hourly;
CREATE TEMP TABLE r2 AS SELECT ingest_merge(1, 'snapshot') AS j;
SELECT pg_temp.check_rows('again: devices', 'SELECT * FROM pg_temp.got_devices', 'SELECT * FROM before2_devices');
SELECT pg_temp.check_rows('again: counts', 'SELECT * FROM pg_temp.got_counts', 'SELECT * FROM before2_counts');
SELECT pg_temp.check_rows('again: load', 'SELECT device_key, hour, channel, polls, source FROM ap_load_hourly',
                          'SELECT * FROM before2_load');
SELECT pg_temp.check_eq('again: polls', (SELECT j #>> '{inserted,polls}' FROM r2), '0');
SELECT pg_temp.check_eq('again: poll conflicts', (SELECT j #>> '{conflicts,polls}' FROM r2), '2');
SELECT pg_temp.check_eq('again: observations', (SELECT j #>> '{inserted,observations}' FROM r2), '0');
SELECT pg_temp.check_eq('again: alerts', (SELECT j #>> '{inserted,alerts}' FROM r2), '0');
SELECT pg_temp.check_eq('again: history', (SELECT j #>> '{inserted,device_config_history}' FROM r2), '0');
SELECT pg_temp.check_eq('again: min_ts', (SELECT j ->> 'min_ts' FROM r2), NULL);

-- --- 3. a live batch --------------------------------------------------------------------------
SELECT pg_temp.clear_stage();
INSERT INTO stage_devices (device_key, mac, type, first_seen, last_seen, ssid, cloaked, crypt,
                           crypt_bits, mfp_sup, mfp_req, adv_channel, config_changed_at,
                           cur_n_clients, cur_qbss_stations, cur_util_pct, cur_at)
VALUES ('A', 'A8:00:00:00:00:01', 'ap', pg_temp.u('2026-09-20 10:15+00'), pg_temp.u('2026-09-20 10:20+00'),
        'Net', 0, 'WPA2', 1, 1, 0, '11', NULL, NULL, NULL, NULL, NULL),
       ('C2', '22:00:00:00:00:02', 'client', pg_temp.u('2026-09-20 10:20+00'), pg_temp.u('2026-09-20 10:20+00'),
        NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL),
       ('C', '11:00:00:00:00:01', 'client', pg_temp.u('2026-09-20 10:00+00'), pg_temp.u('2026-09-20 10:20+00'),
        NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL);
INSERT INTO stage_polls (seq, ts, ts_offset_s, kismet_ts, devices_active, ds_running, ok)
VALUES (3, pg_temp.u('2026-09-20 10:20+00'), 5.0, pg_temp.u('2026-09-20 10:20+00'), 2, 1, 1);
INSERT INTO stage_observations (ts, device_key, rssi, pk_total)
VALUES (pg_temp.u('2026-09-20 10:20+00'), 'A', -62, 30),
       (pg_temp.u('2026-09-20 10:20+00'), 'C2', -75, 1);
INSERT INTO stage_device_freq_hist VALUES ('C', 2412000, 99, pg_temp.u('2026-09-20 10:20+00'));
INSERT INTO stage_associations VALUES ('A', '11:00:00:00:00:09', pg_temp.u('2026-09-20 10:20+00'), pg_temp.u('2026-09-20 10:20+00'));
INSERT INTO stage_probes VALUES ('C', 'Home', pg_temp.u('2026-09-20 09:00+00'), pg_temp.u('2026-09-20 10:20+00'));
INSERT INTO stage_alerts (hash, ts, header, severity, transmitter_mac, raw)
VALUES ('h1', pg_temp.u('2026-09-20 10:05+00'), 'DEAUTHFLOOD', 10, 'A8:00:00:00:00:01', '{}'),
       ('h2', pg_temp.u('2026-09-20 10:21+00'), 'BCASTDISCON', 10, 'A8:00:00:00:00:01', '{}');

CREATE TEMP TABLE r3 AS SELECT ingest_merge(1, 'ingest') AS j;

SELECT pg_temp.check_rows('live: devices', 'SELECT * FROM pg_temp.got_devices', $$
  VALUES ('A', 'a8:00:00:00:00:01', 'ap', '2026-09-20 10:00:00+00', '2026-09-20 10:20:00+00', 'Net', 'WPA2', '11',
          '2026-09-20 10:10:00+00', 3, '2026-09-20 10:10:00+00', true),
         ('C', '11:00:00:00:00:01', 'client', '2026-09-20 10:00:00+00', '2026-09-20 10:10:00+00', NULL, NULL, NULL,
          NULL, NULL, NULL, false) $$);
SELECT pg_temp.check_rows('live: counts', 'SELECT * FROM pg_temp.got_counts', $$
  VALUES ('devices', 2::bigint), ('polls', 3), ('device_config_history', 1), ('observations', 3),
         ('ap_load_hourly', 2), ('device_freq_hist', 2), ('associations', 1), ('client_bssids', 1),
         ('probes', 1), ('alerts', 2) $$);
SELECT pg_temp.check_rows('live: the poll', $$SELECT * FROM pg_temp.got_polls WHERE seq = 3$$, $$
  VALUES ('2026-09-20 10:20:00+00', 3::bigint, true, 5.0::real, true) $$);
SELECT pg_temp.check_eq('live: client freq untouched',
  (SELECT packets::text FROM device_freq_hist WHERE device_key = 'C'), '10');
SELECT pg_temp.check_eq('live: client probe untouched',
  (SELECT first_time::text FROM probes WHERE device_key = 'C'), '2026-09-20 10:00:00+00');
SELECT pg_temp.check_eq('live: duplicate alert', (SELECT j #>> '{conflicts,alerts}' FROM r3), '1');
SELECT pg_temp.check_eq('live: client device not accepted', (SELECT j #>> '{inserted,devices}' FROM r3), '1');
SELECT pg_temp.check_eq('live: min_ts', (SELECT (j ->> 'min_ts')::timestamptz::text FROM r3), '2026-09-20 10:20:00+00');
SELECT pg_temp.check_eq('live: dirty_from stays the earliest',
  (SELECT dirty_from::text FROM ingest_state WHERE sensor_id = 1), '2026-09-20 10:00:00+00');

-- --- 4. an older device row; fewer and more polls of one load hour ---------------------------
SELECT pg_temp.clear_stage();
INSERT INTO stage_devices (device_key, mac, type, first_seen, last_seen, ssid, crypt, adv_channel)
VALUES ('A', 'A8:00:00:00:00:01', 'ap', pg_temp.u('2026-09-20 09:50+00'), pg_temp.u('2026-09-20 10:05+00'),
        'Old', 'WPA2', '1');
INSERT INTO stage_ap_load_hourly (device_key, hour, channel, polls, clients_polls, qbss_polls)
VALUES ('A', pg_temp.u('2026-09-20 10:00+00'), '6', 1, 0, 0);
SELECT ingest_merge(1, 'ingest');
SELECT pg_temp.check_rows('older row', $$SELECT device_key, first_seen::text, last_seen::text, ssid, adv_channel
                                        FROM devices WHERE device_key = 'A'$$, $$
  VALUES ('A', '2026-09-20 09:50:00+00', '2026-09-20 10:20:00+00', 'Net', '11') $$);
SELECT pg_temp.check_eq('fewer polls lose',
  (SELECT polls::text FROM ap_load_hourly WHERE device_key = 'A' AND channel = '6'), '2');
SELECT pg_temp.clear_stage();
INSERT INTO stage_devices (device_key, mac, type, first_seen, last_seen, ssid, crypt, adv_channel)
VALUES ('A', 'A8:00:00:00:00:01', 'ap', pg_temp.u('2026-09-20 10:00+00'), pg_temp.u('2026-09-20 10:20+00'),
        'Net', 'WPA2', '11');
INSERT INTO stage_ap_load_hourly (device_key, hour, channel, polls, clients_polls, clients_max, clients_sum, qbss_polls)
VALUES ('A', pg_temp.u('2026-09-20 10:00+00'), '6', 3, 3, 4, 9, 0);
SELECT ingest_merge(1, 'ingest');
SELECT pg_temp.check_rows('more polls win',
  $$SELECT polls, clients_max, clients_sum FROM ap_load_hourly WHERE device_key = 'A' AND channel = '6'$$,
  $$VALUES (3, 4, 9)$$);

-- --- 5. a snapshot fills the seq of a poll stored without one ---------------------------------
INSERT INTO polls (sensor_id, ts, ok) VALUES (1, '2026-09-20 09:00+00', true);
SELECT pg_temp.clear_stage();
INSERT INTO stage_polls (seq, ts, ok) VALUES (7, pg_temp.u('2026-09-20 09:00+00'), 1),
                                            (2, pg_temp.u('2026-09-20 10:10+00'), 1);
CREATE TEMP TABLE r5 AS SELECT ingest_merge(1, 'snapshot') AS j;
SELECT pg_temp.check_eq('seq filled', (SELECT j #>> '{inserted,polls_seq_filled}' FROM r5), '1');
SELECT pg_temp.check_eq('seq of the old poll',
  (SELECT seq::text FROM polls WHERE ts = '2026-09-20 09:00+00'), '7');

-- --- 6. a device-only change marks the sensor dirty from now() --------------------------------
DELETE FROM ingest_state;
SELECT pg_temp.clear_stage();
INSERT INTO stage_devices (device_key, mac, type, first_seen, last_seen, ssid, crypt, adv_channel,
                           cur_n_clients, cur_at)
VALUES ('A', 'A8:00:00:00:00:01', 'ap', pg_temp.u('2026-09-20 10:00+00'), pg_temp.u('2026-09-20 10:20+00'),
        'Net', 'WPA2', '11', 9, pg_temp.u('2026-09-20 10:20+00'));
SELECT ingest_merge(1, 'ingest');
SELECT pg_temp.check_eq('device-only change: cur_* newer wins',
  (SELECT cur_n_clients::text FROM devices WHERE device_key = 'A'), '9');
SELECT pg_temp.check_eq('device-only change: dirty from now()',
  (SELECT (dirty_from = now())::text FROM ingest_state WHERE sensor_id = 1), 'true');

-- --- 7. refusals --------------------------------------------------------------------------------
DO $$
BEGIN
  BEGIN
    PERFORM ingest_merge(99, 'ingest');
    RAISE EXCEPTION 'ingest_merge: FAIL (unknown sensor accepted)';
  EXCEPTION WHEN raise_exception THEN
    IF SQLERRM NOT LIKE 'ingest_merge: unknown sensor_id%' THEN RAISE; END IF;
  END;
  BEGIN
    PERFORM ingest_merge(1, 'upload');
    RAISE EXCEPTION 'ingest_merge: FAIL (unknown via accepted)';
  EXCEPTION WHEN raise_exception THEN
    IF SQLERRM NOT LIKE 'ingest_merge: via must be%' THEN RAISE; END IF;
  END;
END $$;

DO $$ BEGIN
  RAISE NOTICE 'ingest_merge: PASS (snapshot and live merges, idempotent re-merge, no client data from the live ingest, newest-wins devices with NULL-keeps-stored, fuller-wins load, seq filled by a snapshot, dirty_from, refusals)';
END $$;

ROLLBACK;
