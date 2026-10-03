-- Server schema 6 -> 7: slim observations (AP rows only), devices.cur_*,
-- client_bssids, ap_baselines.band. ONE transaction: any error leaves schema 6
-- exactly as it was. Run through ops/migrate_schema7.sh (disk-space check
-- first; afterwards it applies schema.sql, vacuums and refreshes the rollups).
--
-- What happens:
--   1. checks: schema 6, no observations_v5 yet; ACCESS EXCLUSIVE lock on
--      observations (lock_timeout 10 s: a running reader makes it fail early,
--      not after minutes of work). Dashboard requests that touch the locked
--      tables wait until COMMIT.
--   2. row counts per device type, before
--   3. client_bssids from observations.bssid (deduplicated, first/last poll)
--   4. devices.cur_n_clients / cur_qbss_stations / cur_util_pct / cur_at from
--      each AP's latest observation
--   5. the new observations table: AP rows only, the columns the detectors and
--      the dashboard read, 8-byte columns first, inserted in ts order
--   6. swap: the old table becomes observations_v5 (kept, with its indexes,
--      until the developer drops it), the new one takes the name
--   7. PRIMARY KEY (sensor_id, device_key, ts) INCLUDE (rssi, rssi_floor),
--      foreign key, BRIN on ts
--   8. ap_baselines.band (ap_band(): advertised channel, else busiest
--      frequency) replaces main_freq_khz; ap_summary gets band and cur_*
--      (filled by the refresh afterwards)
--   9. schema_meta 7; row counts after - the AP rows must match exactly
--
-- Non-AP observation rows are not copied: they stay in observations_v5 until
-- it is dropped, on the Pi for its 14 days, and in the backup dump.
\set ON_ERROR_STOP on
\pset pager off
\timing on
SET lock_timeout = '10s';
SET statement_timeout = 0;
SET maintenance_work_mem = '128MB';         -- the 1 GB box: shared_buffers 128 MB, nothing else heavy runs
SET max_parallel_maintenance_workers = 0;   -- 0.25 vCPU sustained: workers only add memory
SET work_mem = '64MB';                      -- the ts-ordered copy sorts ~2.5M rows

BEGIN;

\echo [1/9] checks, lock
DO $$
DECLARE v text := (SELECT value FROM schema_meta WHERE key = 'schema_version');
BEGIN
  IF v IS DISTINCT FROM '6' THEN
    RAISE EXCEPTION 'migrate_schema7: expected schema_version 6, found %', v;
  END IF;
  IF to_regclass('observations_v5') IS NOT NULL THEN
    RAISE EXCEPTION 'migrate_schema7: observations_v5 already exists';
  END IF;
END $$;
LOCK TABLE observations IN ACCESS EXCLUSIVE MODE;
SELECT clock_timestamp()::timestamptz(0) AS started;

\echo [2/9] observation rows per device type, before
CREATE TEMP TABLE mig_before ON COMMIT DROP AS
SELECT o.sensor_id, coalesce(d.type, '?') AS type, count(*) AS n
FROM observations o
LEFT JOIN devices d ON d.sensor_id = o.sensor_id AND d.device_key = o.device_key
GROUP BY 1, 2;
SELECT * FROM mig_before ORDER BY sensor_id, n DESC;

\echo [3/9] client_bssids from observations.bssid
CREATE TABLE client_bssids (
  sensor_id  integer NOT NULL,
  client_key text NOT NULL,
  bssid      macaddr NOT NULL,
  first_seen timestamptz NOT NULL,
  last_seen  timestamptz NOT NULL,
  PRIMARY KEY (sensor_id, client_key, bssid),
  FOREIGN KEY (sensor_id, client_key) REFERENCES devices (sensor_id, device_key));
INSERT INTO client_bssids (sensor_id, client_key, bssid, first_seen, last_seen)
SELECT sensor_id, device_key, bssid, min(ts), max(ts)
FROM observations
WHERE bssid IS NOT NULL
GROUP BY 1, 2, 3;
CREATE INDEX client_bssids_bssid ON client_bssids (sensor_id, bssid);

\echo [4/9] devices.cur_* from the latest observation of each AP
ALTER TABLE devices
  ADD COLUMN cur_n_clients integer,
  ADD COLUMN cur_qbss_stations integer,
  ADD COLUMN cur_util_pct real,
  ADD COLUMN cur_at timestamptz;
UPDATE devices d
SET cur_n_clients = l.n_clients, cur_qbss_stations = l.qbss_stations,
    cur_util_pct = l.util_pct, cur_at = l.ts
FROM (SELECT a.sensor_id, a.device_key, o.ts, o.n_clients, o.qbss_stations, o.util_pct
      FROM devices a
      CROSS JOIN LATERAL (SELECT ts, n_clients, qbss_stations, util_pct
                          FROM observations o
                          WHERE o.sensor_id = a.sensor_id AND o.device_key = a.device_key
                          ORDER BY ts DESC LIMIT 1) o
      WHERE a.type = 'ap') l
WHERE d.sensor_id = l.sensor_id AND d.device_key = l.device_key;

\echo [5/9] new observations table: AP rows, slim columns, ts order
CREATE TABLE observations_new (
  ts               timestamptz NOT NULL,
  pk_total         bigint,
  pk_data          bigint,
  bss_timestamp    bigint,
  disconnects_last timestamptz,
  sensor_id        integer NOT NULL,
  disconnects      integer,
  rssi             smallint,
  rssi_floor       boolean,
  device_key       text NOT NULL);
-- One sequential read of the old heap, a hash join to the APs and one sort.
-- Left alone, the planner (estimating ~15k AP rows instead of ~2.5M) walks
-- the per-AP index and fetches every row's heap page at random - millions of
-- random reads on the 1 GB box (EXPLAIN 2026-10-02).
SET LOCAL enable_nestloop = off;
SET LOCAL enable_indexscan = off;
SET LOCAL enable_indexonlyscan = off;
SET LOCAL enable_bitmapscan = off;
SET LOCAL max_parallel_workers_per_gather = 0;
SET LOCAL jit = off;
INSERT INTO observations_new (ts, pk_total, pk_data, bss_timestamp, disconnects_last, sensor_id,
                              disconnects, rssi, rssi_floor, device_key)
SELECT o.ts, o.pk_total, o.pk_data, o.bss_timestamp, o.disconnects_last, o.sensor_id,
       o.disconnects, o.rssi, o.rssi_floor, o.device_key
FROM observations o
JOIN devices d ON d.sensor_id = o.sensor_id AND d.device_key = o.device_key AND d.type = 'ap'
ORDER BY o.ts, o.sensor_id, o.device_key;
RESET enable_nestloop;
RESET enable_indexscan;
RESET enable_indexonlyscan;
RESET enable_bitmapscan;
RESET max_parallel_workers_per_gather;
RESET jit;

\echo [6/9] swap: observations -> observations_v5, observations_new -> observations
ALTER TABLE observations RENAME TO observations_v5;
ALTER INDEX observations_pkey RENAME TO observations_v5_pkey;
ALTER INDEX observations_device_ts_rssi_floor RENAME TO observations_v5_device_ts_rssi_floor;
ALTER TABLE observations_new RENAME TO observations;
COMMENT ON TABLE observations_v5 IS
  'Schema-6 observations (every device type, all columns), kept after the schema-7 migration until the developer drops it: DROP TABLE observations_v5;';

\echo [7/9] primary key (index build), foreign key, BRIN
ALTER TABLE observations
  ADD CONSTRAINT observations_pkey PRIMARY KEY (sensor_id, device_key, ts) INCLUDE (rssi, rssi_floor);
ALTER TABLE observations
  ADD CONSTRAINT observations_device_fkey
  FOREIGN KEY (sensor_id, device_key) REFERENCES devices (sensor_id, device_key);
CREATE INDEX observations_ts_brin ON observations USING brin (ts);
COMMENT ON TABLE observations IS
  'One row per AP per poll in which the AP was active: the RSSI/counter time series the detectors and the dashboard read. AP rows only (schema 7).';

\echo [8/9] ap_baselines.band replaces main_freq_khz; ap_summary band + cur_*
-- Same definitions as in schema.sql (tests/test_migrate_schema7.py keeps them equal).
CREATE OR REPLACE FUNCTION band_of_channel(ch text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT CASE WHEN ch ~ '^[0-9]{1,3}$' THEN
           CASE WHEN ch::int BETWEEN 1 AND 14 THEN '2.4'
                WHEN ch::int BETWEEN 32 AND 177 THEN '5' END END
$$;

CREATE OR REPLACE FUNCTION band_of_freq(khz integer) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT CASE WHEN khz IS NULL OR khz <= 0 THEN NULL
              WHEN khz < 3000000 THEN '2.4'
              WHEN khz < 5925000 THEN '5' ELSE '6' END
$$;

CREATE OR REPLACE FUNCTION ap_band(p_sensor_id integer, p_device_key text) RETURNS text
LANGUAGE sql STABLE AS $$
  SELECT coalesce(
    (SELECT band_of_channel(adv_channel) FROM devices
     WHERE sensor_id = p_sensor_id AND device_key = p_device_key),
    (SELECT band_of_freq(freq_khz) FROM device_freq_hist
     WHERE sensor_id = p_sensor_id AND device_key = p_device_key AND packets > 0
     ORDER BY packets DESC, freq_khz LIMIT 1))
$$;

ALTER TABLE ap_baselines ADD COLUMN band text;
UPDATE ap_baselines SET band = ap_band(sensor_id, device_key) WHERE rssi_median IS NOT NULL;
SELECT band, count(*) AS baselines,
       count(*) FILTER (WHERE band = CASE WHEN main_freq_khz < 3000000 THEN '2.4'
                                          WHEN main_freq_khz < 5925000 THEN '5' ELSE '6' END)
         AS same_as_main_freq
FROM ap_baselines WHERE rssi_median IS NOT NULL GROUP BY 1 ORDER BY 1;
ALTER TABLE ap_baselines DROP COLUMN main_freq_khz;
ALTER TABLE ap_summary
  DROP COLUMN main_freq_khz,
  ADD COLUMN band text,
  ADD COLUMN cur_n_clients integer,
  ADD COLUMN cur_qbss_stations integer,
  ADD COLUMN cur_util_pct real,
  ADD COLUMN cur_at timestamptz;

\echo [9/9] schema 7; rows per device type after
INSERT INTO schema_meta (key, value) VALUES ('schema_version', '7')
  ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
DO $$
DECLARE
  v_ap_before bigint := (SELECT coalesce(sum(n), 0) FROM mig_before WHERE type = 'ap');
  v_after     bigint := (SELECT count(*) FROM observations);
  v_non_ap    bigint := (SELECT count(*) FROM observations o
                         JOIN devices d ON d.sensor_id = o.sensor_id AND d.device_key = o.device_key
                         WHERE d.type <> 'ap');
BEGIN
  IF v_after <> v_ap_before OR v_non_ap <> 0 THEN
    RAISE EXCEPTION 'migrate_schema7: % AP rows before, % rows after (% non-AP) - rolled back',
      v_ap_before, v_after, v_non_ap;
  END IF;
  RAISE NOTICE 'migrate_schema7: % AP rows copied; % rows of other device types left in observations_v5',
    v_after, (SELECT coalesce(sum(n), 0) FROM mig_before WHERE type <> 'ap');
END $$;
SELECT b.sensor_id, b.type, b.n AS rows_before,
       CASE WHEN b.type = 'ap' THEN (SELECT count(*) FROM observations o WHERE o.sensor_id = b.sensor_id)
            ELSE 0 END AS rows_after
FROM mig_before b ORDER BY b.sensor_id, b.n DESC;
SELECT (SELECT count(*) FROM client_bssids) AS client_bssid_pairs,
       (SELECT count(*) FROM devices WHERE cur_at IS NOT NULL) AS aps_with_load,
       pg_size_pretty(pg_table_size('observations')) AS new_heap,
       pg_size_pretty(pg_indexes_size('observations')) AS new_indexes,
       pg_size_pretty(pg_total_relation_size('observations_v5')) AS kept_v5;

COMMIT;
SELECT clock_timestamp()::timestamptz(0) AS committed;
