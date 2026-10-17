-- Server schema 14 -> 15 (schema.sql, schema 15): an incremental
-- refresh_rollups() for the per-poll live ingest. ONE transaction: any error
-- leaves schema 14 as it is. Run through ops/migrate_schema15.sh (afterwards it
-- applies schema.sql and runs the fixture tests).
--
-- Why (docs/live-upload.md 6.4, N8, measured 2026-10-17 on the box): an
-- "incremental" refresh_rollups() took 13.9-16.6 s on a quiet box, almost all
-- of it whole-sensor work - the materialized ap_config_changes refreshed whole
-- (~33 s instrumented), the per-AP hour aggregate reading every observation
-- (16 s: a per-AP lower bound defeated the index), the security functions per
-- row (~20 s instrumented over ap_summary and the security periods), an
-- anti-join of the new-AP aggregate reading every observation (9 s). At one
-- refresh a minute that is the e2-micro's whole sustained CPU.
--
--   1. checks: schema 14; lock_timeout 10 s
--   2. ap_config_changes: the materialized view becomes a table with the same
--      columns, rows and unique index (copied, not recomputed); the rules stay
--      as the plain view ap_config_changes_v; ap_channel_changes recreated
--   3. refresh_config_changes() (per AP), refresh_security_periods() and
--      refresh_rollups() (constant-bound hour aggregate, per-AP timeline,
--      security functions once per distinct input)
--   4. schema_meta 15
\set ON_ERROR_STOP on
\pset pager off

BEGIN;
SET LOCAL lock_timeout = '10s';
DO $$
BEGIN
  IF (SELECT value FROM schema_meta WHERE key = 'schema_version') IS DISTINCT FROM '14' THEN
    RAISE EXCEPTION 'migrate_schema15: expected schema_version 14';
  END IF;
  IF (SELECT relkind FROM pg_class WHERE oid = to_regclass('public.ap_config_changes')) IS DISTINCT FROM 'm' THEN
    RAISE EXCEPTION 'migrate_schema15: ap_config_changes is not the materialized view of schema 14';
  END IF;
END $$;

\echo [2/4] ap_config_changes: materialized view -> table
CREATE TABLE ap_config_changes_t (
  sensor_id  integer NOT NULL,
  device_key text NOT NULL,
  ts         timestamptz NOT NULL,             -- when the old value was replaced
  field      text NOT NULL,
  old_value  text,
  new_value  text);
INSERT INTO ap_config_changes_t (sensor_id, device_key, ts, field, old_value, new_value)
SELECT sensor_id, device_key, ts, field, old_value, new_value FROM ap_config_changes;
SELECT (SELECT count(*) FROM ap_config_changes) AS matview_rows, (SELECT count(*) FROM ap_config_changes_t) AS table_rows;
DROP VIEW ap_channel_changes;
DROP MATERIALIZED VIEW ap_config_changes;
ALTER TABLE ap_config_changes_t RENAME TO ap_config_changes;
CREATE UNIQUE INDEX ap_config_changes_key
  ON ap_config_changes (sensor_id, device_key, ts, field);
COMMENT ON TABLE ap_config_changes IS
  'Changes of an AP''s advertised configuration, one row per changed field (old -> new), with the pre-v5 incomplete-record churn, Kismet''s missing-field crypt_bits 0 and the hidden-beacon/named-record alternation read as unknown (rules: view ap_config_changes_v). History is not rewritten. A table filled per AP by refresh_config_changes() (schema 15; a materialized view in schemas 6-14).';

CREATE OR REPLACE VIEW ap_config_changes_v AS
WITH st AS (
  SELECT sensor_id, device_key, ts, ssid, cloaked, crypt, crypt_bits, mfp_sup, mfp_req,
         adv_channel, ht_mode, beacon_rate, country
  FROM device_config_history
  UNION ALL
  SELECT sensor_id, device_key, NULL::timestamptz, ssid, cloaked, crypt, crypt_bits, mfp_sup, mfp_req,
         adv_channel, ht_mode, beacon_rate, country
  FROM devices
), known AS (
  SELECT st.sensor_id, st.device_key, st.ts, f.field, f.val
  FROM st CROSS JOIN LATERAL (VALUES
    ('ssid',        nullif(st.ssid, '')),
    ('cloaked',     CASE WHEN st.ssid = '' THEN NULL ELSE st.cloaked::text END),
    ('crypt',       st.crypt),
    ('crypt_bits',  CASE WHEN st.crypt_bits = 0 AND st.crypt IS DISTINCT FROM 'Open' THEN NULL
                         ELSE st.crypt_bits::text END),
    ('mfp_sup',     st.mfp_sup::text),
    ('mfp_req',     st.mfp_req::text),
    ('adv_channel', st.adv_channel),
    ('ht_mode',     st.ht_mode),
    ('beacon_rate', st.beacon_rate::text),
    ('country',     st.country)) AS f(field, val)
  WHERE f.val IS NOT NULL AND NOT (st.crypt IS NULL AND st.adv_channel IS NULL)
), seq AS (
  SELECT k.sensor_id, k.device_key, k.ts, k.field, k.val,
         lead(k.val) OVER (PARTITION BY k.sensor_id, k.device_key, k.field
                           ORDER BY k.ts NULLS LAST) AS new_val
  FROM known k
)
SELECT sensor_id, device_key, ts, field, val AS old_value, new_val AS new_value
FROM seq
WHERE ts IS NOT NULL AND new_val IS NOT NULL AND new_val <> val;

CREATE OR REPLACE VIEW ap_channel_changes AS
SELECT sensor_id, device_key, ts, old_value AS from_channel, new_value AS to_channel
FROM ap_config_changes
WHERE field = 'adv_channel';

\echo [3/4] functions
CREATE OR REPLACE FUNCTION refresh_config_changes(p_sensor_id integer, p_keys text[] DEFAULT NULL)
RETURNS integer
LANGUAGE plpgsql
SET jit = off
AS $$
DECLARE v_n integer;
BEGIN
  IF p_keys IS NULL THEN
    DELETE FROM ap_config_changes WHERE sensor_id = p_sensor_id;
    INSERT INTO ap_config_changes (sensor_id, device_key, ts, field, old_value, new_value)
    SELECT v.sensor_id, v.device_key, v.ts, v.field, v.old_value, v.new_value
    FROM ap_config_changes_v v
    WHERE v.sensor_id = p_sensor_id;
  ELSE
    DELETE FROM ap_config_changes WHERE sensor_id = p_sensor_id AND device_key = ANY (p_keys);
    INSERT INTO ap_config_changes (sensor_id, device_key, ts, field, old_value, new_value)
    SELECT v.sensor_id, v.device_key, v.ts, v.field, v.old_value, v.new_value
    FROM ap_config_changes_v v
    WHERE v.sensor_id = p_sensor_id AND v.device_key = ANY (p_keys);
  END IF;
  GET DIAGNOSTICS v_n = ROW_COUNT;
  RETURN v_n;
END
$$;

CREATE OR REPLACE FUNCTION refresh_security_periods(p_sensor_id integer)
RETURNS integer
LANGUAGE plpgsql
SET jit = off
AS $$
#variable_conflict use_column
DECLARE v_n integer;
BEGIN
  DELETE FROM ap_security_periods WHERE sensor_id = p_sensor_id;
  INSERT INTO ap_security_periods (sensor_id, device_key, valid_from, valid_to, crypt, crypt_bits,
                                   mfp_sup, mfp_req, level, label)
  WITH chg AS MATERIALIZED (
    SELECT device_key, ts, field, old_value, new_value
    FROM ap_config_changes
    WHERE sensor_id = p_sensor_id AND field IN ('crypt', 'crypt_bits', 'mfp_sup', 'mfp_req')
  ), pts AS (
    SELECT device_key, '-infinity'::timestamptz AS valid_from FROM ap_summary WHERE sensor_id = p_sensor_id
    UNION
    SELECT c.device_key, c.ts FROM chg c
    JOIN ap_summary s ON s.sensor_id = p_sensor_id AND s.device_key = c.device_key
  ), st AS (
    SELECT p.device_key, p.valid_from,
           lead(p.valid_from, 1, 'infinity'::timestamptz)
             OVER (PARTITION BY p.device_key ORDER BY p.valid_from) AS valid_to,
           coalesce(
             (SELECT c.new_value FROM chg c WHERE c.device_key = p.device_key AND c.field = 'crypt'
                AND c.ts <= p.valid_from ORDER BY c.ts DESC LIMIT 1),
             (SELECT c.old_value FROM chg c WHERE c.device_key = p.device_key AND c.field = 'crypt'
                AND c.ts > p.valid_from ORDER BY c.ts LIMIT 1),
             s.crypt) AS crypt,
           coalesce(
             (SELECT c.new_value FROM chg c WHERE c.device_key = p.device_key AND c.field = 'crypt_bits'
                AND c.ts <= p.valid_from ORDER BY c.ts DESC LIMIT 1),
             (SELECT c.old_value FROM chg c WHERE c.device_key = p.device_key AND c.field = 'crypt_bits'
                AND c.ts > p.valid_from ORDER BY c.ts LIMIT 1))::bigint AS chg_bits,
           EXISTS (SELECT 1 FROM chg c WHERE c.device_key = p.device_key AND c.field = 'crypt_bits')
             AS bits_changed,
           coalesce(
             (SELECT c.new_value FROM chg c WHERE c.device_key = p.device_key AND c.field = 'mfp_sup'
                AND c.ts <= p.valid_from ORDER BY c.ts DESC LIMIT 1),
             (SELECT c.old_value FROM chg c WHERE c.device_key = p.device_key AND c.field = 'mfp_sup'
                AND c.ts > p.valid_from ORDER BY c.ts LIMIT 1),
             s.mfp_sup::text)::boolean AS mfp_sup,
           coalesce(
             (SELECT c.new_value FROM chg c WHERE c.device_key = p.device_key AND c.field = 'mfp_req'
                AND c.ts <= p.valid_from ORDER BY c.ts DESC LIMIT 1),
             (SELECT c.old_value FROM chg c WHERE c.device_key = p.device_key AND c.field = 'mfp_req'
                AND c.ts > p.valid_from ORDER BY c.ts LIMIT 1),
             s.mfp_req::text)::boolean AS mfp_req,
           s.crypt AS cur_crypt, s.crypt_bits_known AS cur_bits
    FROM pts p
    JOIN ap_summary s ON s.sensor_id = p_sensor_id AND s.device_key = p.device_key
  ), v AS (
    SELECT st.*,
           CASE WHEN st.bits_changed THEN coalesce(st.chg_bits, st.cur_bits)
                WHEN st.crypt IS NOT DISTINCT FROM st.cur_crypt THEN st.cur_bits END AS crypt_bits
    FROM st
  ), sec AS MATERIALIZED (    -- the security functions once per distinct input (schema 15)
    SELECT u.crypt, u.crypt_bits, u.mfp_sup, u.mfp_req,
           security_level(u.crypt, u.crypt_bits, u.mfp_sup, u.mfp_req) AS sec_level,
           security_label(u.crypt, u.crypt_bits) AS sec_label
    FROM (SELECT DISTINCT x.crypt, x.crypt_bits, x.mfp_sup, x.mfp_req FROM v x) u
  )
  SELECT p_sensor_id, v.device_key, v.valid_from, v.valid_to, v.crypt, v.crypt_bits, v.mfp_sup, v.mfp_req,
         sec.sec_level, sec.sec_label
  FROM v
  JOIN sec ON sec.crypt IS NOT DISTINCT FROM v.crypt AND sec.crypt_bits IS NOT DISTINCT FROM v.crypt_bits
          AND sec.mfp_sup IS NOT DISTINCT FROM v.mfp_sup AND sec.mfp_req IS NOT DISTINCT FROM v.mfp_req;
  GET DIAGNOSTICS v_n = ROW_COUNT;
  RETURN v_n;
END
$$;

CREATE OR REPLACE FUNCTION refresh_rollups(p_sensor_id integer, p_from timestamptz DEFAULT NULL)
RETURNS TABLE (from_hour timestamptz, ap_hours integer, sensor_hours integer, aps integer,
               refreshed_at timestamptz)
LANGUAGE plpgsql
SET work_mem = '32MB'      -- the hour aggregate sorts ~1.8M rows on a full run
SET jit = off              -- JIT compilation costs more than it saves on these statements
AS $$
#variable_conflict use_column
DECLARE
  v_from timestamptz;        -- first hour recomputed for every AP
  v_sfrom timestamptz;       -- first hour recomputed per sensor (earlier after a re-classification)
  v_gone timestamptz;        -- first hour of rows dropped for devices that are no longer APs
  v_new  timestamptz;        -- first hour of APs computed over their whole history
  v_now  timestamptz := clock_timestamp();
  v_ap_hours integer;
  v_sensor_hours integer;
  v_aps integer;
  v_account integer;
  v_new_keys text[];         -- APs without any hour yet (schema 15)
  v_n integer;
BEGIN
  SELECT account_id INTO v_account FROM sensors WHERE id = p_sensor_id;
  IF v_account IS NULL THEN
    RAISE EXCEPTION 'refresh_rollups: unknown sensor_id %', p_sensor_id;
  END IF;
  -- one refresh per sensor at a time: two would delete and insert the same rows
  PERFORM pg_advisory_xact_lock(hashtext('refresh_rollups'), p_sensor_id);

  -- 1. first hour to recompute
  IF p_from IS NULL OR NOT EXISTS (SELECT 1 FROM sensor_summary WHERE sensor_id = p_sensor_id) THEN
    v_from := '-infinity';
  ELSE
    v_from := date_trunc('hour',
                least(p_from, (SELECT max(ts) FROM polls WHERE sensor_id = p_sensor_id AND ts < p_from)),
                'UTC');
  END IF;

  -- 2. per AP and hour
  WITH gone AS (
    DELETE FROM ap_rssi_hourly h
    WHERE h.sensor_id = p_sensor_id
      AND NOT EXISTS (SELECT 1 FROM devices d
                      WHERE d.sensor_id = h.sensor_id AND d.device_key = h.device_key
                        AND d.ever_ap)
    RETURNING h.hour)
  SELECT min(hour) INTO v_gone FROM gone;
  DELETE FROM ap_rssi_hourly WHERE sensor_id = p_sensor_id AND hour >= v_from;
  SELECT min(date_trunc('hour', (SELECT min(o.ts) FROM observations o
                                 WHERE o.sensor_id = d.sensor_id AND o.device_key = d.device_key),
                        'UTC'))
    INTO v_new
  FROM devices d
  WHERE d.sensor_id = p_sensor_id AND d.ever_ap
    AND NOT EXISTS (SELECT 1 FROM ap_rssi_hourly h
                    WHERE h.sensor_id = d.sensor_id AND h.device_key = d.device_key);
  v_sfrom := least(v_from, v_gone, v_new);
  -- APs with hours before v_from: their observations from v_from on (one
  -- constant bound: index-only per AP from v_from; a per-AP bound made the
  -- planner read every observation, 16 s, schema 15); APs without: their
  -- whole history, looked up by key (new APs, few - an anti-join against all
  -- observations cost 9 s even with none)
  SELECT array_agg(d.device_key) INTO v_new_keys
  FROM devices d
  WHERE d.sensor_id = p_sensor_id AND d.ever_ap
    AND NOT EXISTS (SELECT 1 FROM ap_rssi_hourly h
                    WHERE h.sensor_id = d.sensor_id AND h.device_key = d.device_key);
  INSERT INTO ap_rssi_hourly (sensor_id, device_key, hour, n_obs, n_valid, n_floor,
                              median, p10, p90, min, max)
  SELECT p_sensor_id, device_key, hour, n_obs, n_valid, n_floor, pct[1], pct[2], pct[3], lo, hi
  FROM (
    SELECT o.device_key, date_trunc('hour', o.ts, 'UTC') AS hour,
           count(*)::integer AS n_obs,
           count(rssi_valid(o.rssi))::integer AS n_valid,
           (count(*) FILTER (WHERE rssi_is_floor(o.rssi, o.rssi_floor)))::integer AS n_floor,
           percentile_cont(ARRAY[0.5, 0.1, 0.9]) WITHIN GROUP (ORDER BY rssi_valid(o.rssi)) AS pct,
           min(rssi_valid(o.rssi)) AS lo,
           max(rssi_valid(o.rssi)) AS hi
    FROM observations o
    WHERE o.sensor_id = p_sensor_id AND o.ts >= v_from
      AND o.device_key IN (SELECT d.device_key FROM devices d
                           WHERE d.sensor_id = p_sensor_id AND d.ever_ap
                             AND EXISTS (SELECT 1 FROM ap_rssi_hourly h
                                         WHERE h.sensor_id = d.sensor_id AND h.device_key = d.device_key))
    GROUP BY 1, 2) s;
  GET DIAGNOSTICS v_ap_hours = ROW_COUNT;
  IF v_new_keys IS NOT NULL THEN
    INSERT INTO ap_rssi_hourly (sensor_id, device_key, hour, n_obs, n_valid, n_floor,
                                median, p10, p90, min, max)
    SELECT p_sensor_id, device_key, hour, n_obs, n_valid, n_floor, pct[1], pct[2], pct[3], lo, hi
    FROM (
      SELECT o.device_key, date_trunc('hour', o.ts, 'UTC') AS hour,
             count(*)::integer AS n_obs,
             count(rssi_valid(o.rssi))::integer AS n_valid,
             (count(*) FILTER (WHERE rssi_is_floor(o.rssi, o.rssi_floor)))::integer AS n_floor,
             percentile_cont(ARRAY[0.5, 0.1, 0.9]) WITHIN GROUP (ORDER BY rssi_valid(o.rssi)) AS pct,
             min(rssi_valid(o.rssi)) AS lo,
             max(rssi_valid(o.rssi)) AS hi
      FROM observations o
      WHERE o.sensor_id = p_sensor_id AND o.device_key = ANY (v_new_keys)
      GROUP BY 1, 2) s;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    v_ap_hours := v_ap_hours + v_n;
  END IF;

  -- 3. per sensor and hour (from v_sfrom: a re-classified device changed
  --    the AP counts of its hours before v_from too)
  DELETE FROM sensor_hourly WHERE sensor_id = p_sensor_id AND hour >= v_sfrom;
  INSERT INTO sensor_hourly (sensor_id, hour, polls, polls_ok, hop_ok_polls, hop_known_polls,
                             gap_s, active_aps, new_aps, alerts, ap_obs, capture_polls, limited_polls)
  WITH p AS (
    SELECT date_trunc('hour', ts, 'UTC') AS hour, count(*) AS polls,
           count(*) FILTER (WHERE ok) AS polls_ok,
           count(*) FILTER (WHERE ds_hop_ok) AS hop_ok, count(ds_hop_ok) AS hop_known,
           count(*) FILTER (WHERE ok AND ds_running AND devices_active > 0) AS capture,
           count(*) FILTER (WHERE ok AND ds_running AND devices_active > 0 AND ds_hop_ok = false) AS limited
    FROM polls WHERE sensor_id = p_sensor_id AND ts >= v_sfrom
    GROUP BY 1
  ), g AS (    -- poll gaps > 90 s as on the overview, incl. one ending at the first poll >= v_sfrom
    SELECT prev_ts, ts
    FROM (SELECT ts, lag(ts) OVER (ORDER BY ts) AS prev_ts
          FROM polls
          WHERE sensor_id = p_sensor_id
            AND ts >= coalesce((SELECT max(ts) FROM polls
                                WHERE sensor_id = p_sensor_id AND ts < v_sfrom), v_sfrom)) x
    WHERE ts - prev_ts > interval '90 seconds'
  ), gh AS (   -- ... split over the hours they cover
    SELECT h.hour,
           sum(extract(epoch FROM least(g.ts, h.hour + interval '1 hour') - greatest(g.prev_ts, h.hour))) AS gap_s
    FROM g
    CROSS JOIN LATERAL generate_series(date_trunc('hour', g.prev_ts, 'UTC'), g.ts,
                                       interval '1 hour') AS h(hour)
    WHERE h.hour >= v_sfrom AND h.hour < g.ts
    GROUP BY h.hour
  ), a AS (
    SELECT hour, count(*) AS active_aps, sum(n_obs) AS ap_obs
    FROM ap_rssi_hourly WHERE sensor_id = p_sensor_id AND hour >= v_sfrom
    GROUP BY hour
  ), f AS (    -- the first hour in which each AP was observed
    SELECT first_hour AS hour, count(*) AS new_aps
    FROM (SELECT min(hour) AS first_hour FROM ap_rssi_hourly
          WHERE sensor_id = p_sensor_id GROUP BY device_key) x
    WHERE first_hour >= v_sfrom
    GROUP BY first_hour
  ), al AS (
    SELECT date_trunc('hour', ts, 'UTC') AS hour, count(*) AS alerts
    FROM alerts WHERE sensor_id = p_sensor_id AND ts >= v_sfrom
    GROUP BY 1
  ), hrs AS (
    SELECT hour FROM p UNION SELECT hour FROM gh UNION SELECT hour FROM a UNION SELECT hour FROM al
  )
  SELECT p_sensor_id, hrs.hour, coalesce(p.polls, 0), coalesce(p.polls_ok, 0),
         coalesce(p.hop_ok, 0), coalesce(p.hop_known, 0), coalesce(gh.gap_s, 0),
         coalesce(a.active_aps, 0), coalesce(f.new_aps, 0), coalesce(al.alerts, 0),
         coalesce(a.ap_obs, 0), coalesce(p.capture, 0), coalesce(p.limited, 0)
  FROM hrs
  LEFT JOIN p  ON p.hour  = hrs.hour
  LEFT JOIN gh ON gh.hour = hrs.hour
  LEFT JOIN a  ON a.hour  = hrs.hour
  LEFT JOIN f  ON f.hour  = hrs.hour
  LEFT JOIN al ON al.hour = hrs.hour;
  GET DIAGNOSTICS v_sensor_hours = ROW_COUNT;

  -- 4. the configuration timeline of the APs with a history row from v_from
  --    on: a field once known stays known (the collector's field-wise merge),
  --    so the timeline changes only with a known -> different known change,
  --    which writes a history row in the same poll; all of the sensor's APs on
  --    a full run (schema 15; the APs merely seen since v_from were the busy
  --    ones - nearly every history row, 2.5 s a run)
  IF v_from = '-infinity' THEN
    PERFORM refresh_config_changes(p_sensor_id);
  ELSE
    PERFORM refresh_config_changes(p_sensor_id, ARRAY(
      SELECT DISTINCT h.device_key FROM device_config_history h
      WHERE h.sensor_id = p_sensor_id AND h.ts >= v_from));
  END IF;

  -- 5. one row per AP, rebuilt
  DELETE FROM ap_summary WHERE sensor_id = p_sensor_id;
  INSERT INTO ap_summary (
      sensor_id, device_key, bssid, random_bssid, manuf, ssid, hidden, cloaked, crypt, crypt_bits,
      mfp_sup, mfp_req, adv_channel, ht_mode, beacon_rate, country, first_seen, last_seen,
      config_changed_at, hidden_beacon, name_seen,
      has_baseline, trusted, trusted_at, note, rssi_median, rssi_mean, rssi_sd, rssi_robust_sd,
      rssi_p5, rssi_p95, band, baseline_n_obs, baseline_n_floor, baseline_n_days,
      baseline_window_start, baseline_window_end, baseline_at,
      n_obs, n_valid, n_floor, first_obs, last_obs,
      cur_n_clients, cur_qbss_stations, cur_util_pct, cur_at,
      history_rows, n_changes, last_change_at, last_change,
      security, security_label, crypt_bits_known, rssi_typical)
  -- the bitfield, or the last known one of the same crypt string when
  -- Kismet's missing-field 0 replaced it (ap_config_changes rule 3); the
  -- security functions once per distinct input (a few dozen, not ~900 calls
  -- at a few ms each on the e2-micro, schema 15)
  WITH dk AS MATERIALIZED (
    SELECT d.*, kb.bits
    FROM devices d
    LEFT JOIN LATERAL (
      SELECT CASE WHEN d.crypt_bits <> 0 OR d.crypt = 'Open' THEN d.crypt_bits
                  ELSE (SELECT h.crypt_bits FROM device_config_history h
                        WHERE h.sensor_id = d.sensor_id AND h.device_key = d.device_key
                          AND h.crypt IS NOT DISTINCT FROM d.crypt AND h.crypt_bits <> 0
                        ORDER BY h.ts DESC LIMIT 1) END AS bits) kb ON true
    WHERE d.sensor_id = p_sensor_id AND d.ever_ap
  ), sec AS MATERIALIZED (
    SELECT u.crypt, u.bits, u.mfp_sup, u.mfp_req,
           security_level(u.crypt, u.bits, u.mfp_sup, u.mfp_req) AS level,
           security_label(u.crypt, u.bits) AS label
    FROM (SELECT DISTINCT x.crypt, x.bits, x.mfp_sup, x.mfp_req FROM dk x) u
  )
  SELECT d.sensor_id, d.device_key, d.mac,
         (d.mac & macaddr '02:00:00:00:00:00') <> macaddr '00:00:00:00:00:00',
         d.manuf, d.ssid, (d.ssid IS NULL OR d.ssid = ''), d.cloaked, d.crypt, d.crypt_bits,
         d.mfp_sup, d.mfp_req, d.adv_channel, d.ht_mode, d.beacon_rate, d.country,
         d.first_seen, d.last_seen, d.config_changed_at,
         coalesce(hi.hidden_beacon, false) OR (coalesce(d.cloaked, false) AND coalesce(d.ssid, '') = ''),
         coalesce(nullif(d.ssid, ''), hi.name_seen),
         b.device_key IS NOT NULL, coalesce(b.trusted, false), b.trusted_at, b.note,
         b.rssi_median, b.rssi_mean, b.rssi_sd, b.rssi_robust_sd, b.rssi_p5, b.rssi_p95,
         b.band, b.n_obs, b.n_floor, b.n_days, b.window_start, b.window_end, b.computed_at,
         coalesce(r.n_obs, 0), coalesce(r.n_valid, 0), coalesce(r.n_floor, 0),
         (SELECT min(o.ts) FROM observations o
          WHERE o.sensor_id = d.sensor_id AND o.device_key = d.device_key),
         (SELECT max(o.ts) FROM observations o
          WHERE o.sensor_id = d.sensor_id AND o.device_key = d.device_key),
         d.cur_n_clients, d.cur_qbss_stations, d.cur_util_pct, d.cur_at,
         coalesce(hi.n_rows, 0), coalesce(c.n_changes, 0), c.last_change_at, c.last_change,
         sec.level, sec.label,
         d.bits, rt.rssi_typical
  FROM dk d
  LEFT JOIN sec ON sec.crypt IS NOT DISTINCT FROM d.crypt AND sec.bits IS NOT DISTINCT FROM d.bits
               AND sec.mfp_sup IS NOT DISTINCT FROM d.mfp_sup AND sec.mfp_req IS NOT DISTINCT FROM d.mfp_req
  LEFT JOIN ap_baselines b ON b.sensor_id = d.sensor_id AND b.device_key = d.device_key
  LEFT JOIN (SELECT device_key, sum(n_obs) AS n_obs, sum(n_valid) AS n_valid, sum(n_floor) AS n_floor
             FROM ap_rssi_hourly WHERE sensor_id = p_sensor_id
             GROUP BY device_key) r ON r.device_key = d.device_key
  LEFT JOIN (SELECT device_key, count(*) AS n_rows,
                    bool_or(cloaked AND ssid = '') AS hidden_beacon,
                    (array_agg(ssid ORDER BY ts DESC) FILTER (WHERE ssid <> ''))[1] AS name_seen
             FROM device_config_history WHERE sensor_id = p_sensor_id
             GROUP BY device_key) hi ON hi.device_key = d.device_key
  LEFT JOIN (SELECT device_key, count(*) AS n_changes, max(ts) AS last_change_at,
                    (array_agg(changes ORDER BY ts DESC))[1] AS last_change
             FROM (SELECT device_key, ts,
                          jsonb_agg(jsonb_build_object('field', field, 'old', old_value,
                                                       'new', new_value) ORDER BY field) AS changes
                   FROM ap_config_changes WHERE sensor_id = p_sensor_id
                   GROUP BY device_key, ts) e
             GROUP BY device_key) c ON c.device_key = d.device_key
  LEFT JOIN (SELECT device_key, percentile_cont(0.5) WITHIN GROUP (ORDER BY median) AS rssi_typical
             FROM ap_rssi_hourly WHERE sensor_id = p_sensor_id AND median IS NOT NULL
             GROUP BY device_key) rt ON rt.device_key = d.device_key;
  GET DIAGNOSTICS v_aps = ROW_COUNT;

  -- 5b. security periods (refresh_security_periods())
  PERFORM refresh_security_periods(p_sensor_id);

  -- 5c. account-level APs (one per BSSID of the account; first/last seen over
  --     all its sensors - never the manual classification or the note) and the
  --     sensor's mapping to them
  -- (alias t: the function's OUT parameter is also called aps)
  INSERT INTO aps AS t (account_id, bssid, first_seen, last_seen)
  SELECT v_account, s.bssid, min(coalesce(s.first_obs, s.first_seen)), max(coalesce(s.last_obs, s.last_seen))
  FROM ap_summary s
  JOIN sensors se ON se.id = s.sensor_id AND se.account_id = v_account
  WHERE s.bssid IN (SELECT x.bssid FROM ap_summary x WHERE x.sensor_id = p_sensor_id)
  GROUP BY s.bssid
  ON CONFLICT (account_id, bssid) DO UPDATE SET
      first_seen = EXCLUDED.first_seen, last_seen = EXCLUDED.last_seen
    WHERE (t.first_seen, t.last_seen) IS DISTINCT FROM (EXCLUDED.first_seen, EXCLUDED.last_seen);
  DELETE FROM ap_sensors WHERE sensor_id = p_sensor_id;
  INSERT INTO ap_sensors (ap_id, sensor_id, device_key)
  SELECT a.id, s.sensor_id, s.device_key
  FROM ap_summary s
  JOIN aps a ON a.account_id = v_account AND a.bssid = s.bssid
  WHERE s.sensor_id = p_sensor_id;

  -- 6. one row per sensor
  WITH gap AS (
    SELECT prev_ts AS gap_start, ts AS gap_end, ts - prev_ts AS len
    FROM (SELECT ts, lag(ts) OVER (ORDER BY ts) AS prev_ts
          FROM polls WHERE sensor_id = p_sensor_id) x
    WHERE ts - prev_ts > interval '90 seconds'
  )
  INSERT INTO sensor_summary (sensor_id, first_poll, last_poll, polls, polls_ok,
                              gap_count, gap_total_s, gap_max_s, gaps, devices_by_type,
                              obs_total, obs_ap, country_expected, alerts, alerts_last_ts,
                              baselines, refreshed_at, last_capture_at, last_hop_n, last_hop_visited,
                              last_hop_ok)
  SELECT p_sensor_id, pl.first_poll, pl.last_poll, pl.polls, pl.polls_ok,
         (SELECT count(*) FROM gap),
         (SELECT coalesce(extract(epoch FROM sum(len)), 0) FROM gap),
         (SELECT coalesce(extract(epoch FROM max(len)), 0) FROM gap),
         (SELECT coalesce(jsonb_agg(jsonb_build_object('gap_start', gap_start, 'gap_end', gap_end,
                                                       'gap_s', extract(epoch FROM len)::float8)
                                    ORDER BY len DESC), '[]'::jsonb)
          FROM (SELECT * FROM gap ORDER BY len DESC LIMIT 10) t),
         (SELECT coalesce(jsonb_object_agg(type, n), '{}'::jsonb)
          FROM (SELECT type, count(*) AS n FROM devices WHERE sensor_id = p_sensor_id
                GROUP BY type) t),
         pl.obs_total,
         (SELECT coalesce(sum(ap_obs), 0) FROM sensor_hourly WHERE sensor_id = p_sensor_id),
         (SELECT mode() WITHIN GROUP (ORDER BY country) FROM devices
          WHERE sensor_id = p_sensor_id AND ever_ap AND country IS NOT NULL),
         (SELECT count(*) FROM alerts WHERE sensor_id = p_sensor_id),
         (SELECT max(ts) FROM alerts WHERE sensor_id = p_sensor_id),
         (SELECT count(*) FROM ap_baselines WHERE sensor_id = p_sensor_id AND rssi_median IS NOT NULL),
         v_now,
         lp.ts, lp.ds_hop_n, lp.ds_hop_visited, lp.ds_hop_ok
  FROM (SELECT min(ts) AS first_poll, max(ts) AS last_poll, count(*) AS polls,
               count(*) FILTER (WHERE ok) AS polls_ok, coalesce(sum(new_obs), 0) AS obs_total
        FROM polls WHERE sensor_id = p_sensor_id) pl
  LEFT JOIN LATERAL (SELECT ts, ds_hop_n, ds_hop_visited, ds_hop_ok FROM polls
                     WHERE sensor_id = p_sensor_id AND ok AND ds_running AND devices_active > 0
                     ORDER BY ts DESC LIMIT 1) lp ON true
  ON CONFLICT (sensor_id) DO UPDATE SET
      first_poll = EXCLUDED.first_poll, last_poll = EXCLUDED.last_poll,
      polls = EXCLUDED.polls, polls_ok = EXCLUDED.polls_ok,
      gap_count = EXCLUDED.gap_count, gap_total_s = EXCLUDED.gap_total_s,
      gap_max_s = EXCLUDED.gap_max_s, gaps = EXCLUDED.gaps,
      devices_by_type = EXCLUDED.devices_by_type, obs_total = EXCLUDED.obs_total,
      obs_ap = EXCLUDED.obs_ap, country_expected = EXCLUDED.country_expected,
      alerts = EXCLUDED.alerts, alerts_last_ts = EXCLUDED.alerts_last_ts,
      baselines = EXCLUDED.baselines, refreshed_at = EXCLUDED.refreshed_at,
      last_capture_at = EXCLUDED.last_capture_at, last_hop_n = EXCLUDED.last_hop_n,
      last_hop_visited = EXCLUDED.last_hop_visited, last_hop_ok = EXCLUDED.last_hop_ok;

  -- 7. outages and limited capture (refresh_sensor_periods())
  PERFORM refresh_sensor_periods(p_sensor_id);

  -- 8. the derived events (Kismet episodes, security changes, sensor outages,
  --    the classification rule); never their status
  PERFORM refresh_events(p_sensor_id);

  RETURN QUERY SELECT v_sfrom, v_ap_hours, v_sensor_hours, v_aps, v_now;
END
$$;

\echo [4/4] schema_meta
INSERT INTO schema_meta (key, value) VALUES ('schema_version', '15')
  ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
COMMIT;
ANALYZE ap_config_changes;
