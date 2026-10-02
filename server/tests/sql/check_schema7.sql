-- Read-only check of the schema-7 migration on the real data (not a fixture
-- test; it reads, never writes - runs as claude_ro too):
--
--   docker compose exec -T db psql -U claude_ro -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 < tests/sql/check_schema7.sql
--
-- The hourly rollups (ap_rssi_hourly) of every hour before the migration were
-- computed from the OLD table; recomputing them from the new one must give the
-- same rows - so every AP row was copied with the same ts, rssi and floor
-- flag. Also: only AP rows in observations, row counts per AP equal
-- ap_summary.n_obs and sensor_hourly.ap_obs, every client BSSID of
-- observations_v5 is in client_bssids, every AP has its latest load.
\set ON_ERROR_STOP on
\pset pager off
SET statement_timeout = '20min';
SET work_mem = '32MB';
SET jit = off;

\echo == ap_rssi_hourly (stored) vs recomputed from the new observations, every hour
WITH re AS (
  SELECT o.sensor_id, o.device_key, date_trunc('hour', o.ts, 'UTC') AS hour,
         count(*)::integer AS n_obs, count(rssi_valid(o.rssi))::integer AS n_valid,
         (count(*) FILTER (WHERE rssi_is_floor(o.rssi, o.rssi_floor)))::integer AS n_floor,
         round((percentile_cont(0.5) WITHIN GROUP (ORDER BY rssi_valid(o.rssi)))::numeric, 2) AS median,
         min(rssi_valid(o.rssi)) AS min, max(rssi_valid(o.rssi)) AS max
  FROM observations o
  JOIN devices d ON d.sensor_id = o.sensor_id AND d.device_key = o.device_key AND d.type = 'ap'
  GROUP BY 1, 2, 3
), st AS (
  SELECT sensor_id, device_key, hour, n_obs, n_valid, n_floor,
         round(median::numeric, 2) AS median, min, max
  FROM ap_rssi_hourly
)
SELECT (SELECT count(*) FROM st) AS stored_hours,
       (SELECT count(*) FROM re) AS recomputed_hours,
       (SELECT count(*) FROM (SELECT * FROM st EXCEPT SELECT * FROM re) x) AS only_stored,
       (SELECT count(*) FROM (SELECT * FROM re EXCEPT SELECT * FROM st) x) AS only_recomputed;

\echo == observations: device types (AP only), counts vs the read tables
SELECT d.type, count(*) AS rows
FROM observations o JOIN devices d ON d.sensor_id = o.sensor_id AND d.device_key = o.device_key
GROUP BY 1;
SELECT (SELECT count(*) FROM observations) AS observation_rows,
       (SELECT sum(ap_obs) FROM sensor_hourly) AS sensor_hourly_ap_obs,
       (SELECT sum(n_obs) FROM ap_summary) AS ap_summary_n_obs,
       (SELECT count(*) FROM ap_summary s
        WHERE s.n_obs <> (SELECT count(*) FROM observations o
                          WHERE o.sensor_id = s.sensor_id AND o.device_key = s.device_key))
         AS aps_count_mismatch;

\echo == client_bssids vs the client BSSIDs of observations_v5 (if still there)
DO $$
DECLARE r record;
BEGIN
  IF to_regclass('observations_v5') IS NULL THEN
    RAISE NOTICE 'client_bssids: observations_v5 dropped - skipped';
    RETURN;
  END IF;
  EXECUTE $q$
    SELECT count(*) AS pairs, count(*) FILTER (WHERE c.bssid IS NULL) AS missing,
           (SELECT count(*) FROM client_bssids) AS stored
    FROM (SELECT DISTINCT v.sensor_id, v.device_key, v.bssid
          FROM observations_v5 v WHERE v.bssid IS NOT NULL) p
    LEFT JOIN client_bssids c
      ON c.sensor_id = p.sensor_id AND c.client_key = p.device_key AND c.bssid = p.bssid$q$ INTO r;
  RAISE NOTICE 'client_bssids: % pairs in observations_v5, % stored, % missing', r.pairs, r.stored, r.missing;
END $$;

\echo == AP load and band
SELECT count(*) AS aps, count(cur_at) AS with_load,
       count(*) FILTER (WHERE band IS NOT NULL) AS with_band_in_summary
FROM ap_summary;
SELECT band, count(*) AS baselines FROM ap_baselines WHERE rssi_median IS NOT NULL GROUP BY 1 ORDER BY 1;
