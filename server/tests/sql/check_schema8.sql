-- Read-only check of schema 8 and the AP load backfill on the real data (not a
-- fixture test; it reads, never writes - runs as claude_ro too):
--
--   docker compose exec -T db psql -U claude_ro -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 < tests/sql/check_schema8.sql
--
-- The per-poll load samples are the AP observation rows of buffers <= v5, so
-- for every hour of source 'v5_obs' the polls of ap_load_hourly (summed over
-- the channels) must equal the observation rows of ap_rssi_hourly (n_obs),
-- apart from the few rows Kismet delivered without a client count (0.1 % in
-- the 2026-10-01 snapshot). Buffer-v7 hours are compared the same way.
\set ON_ERROR_STOP on
\pset pager off
SET statement_timeout = '10min';

\echo == schema version, rows by source
SELECT value AS schema_version FROM schema_meta WHERE key = 'schema_version';
SELECT sensor_id, source, count(*) AS rows, sum(polls) AS polls, count(DISTINCT device_key) AS aps,
       min(hour) AS first_hour, max(hour) AS last_hour,
       round(100.0 * sum(qbss_polls) / nullif(sum(polls), 0), 1) AS qbss_poll_pct,
       count(*) FILTER (WHERE channel = '') AS rows_without_channel
FROM ap_load_hourly GROUP BY 1, 2 ORDER BY 1, 2;

\echo == per UTC day: hours with AP observations vs hours with load rows; polls vs observation rows
WITH obs AS (
  SELECT sensor_id, hour, sum(n_obs) AS n_obs FROM ap_rssi_hourly GROUP BY 1, 2
), load AS (
  SELECT sensor_id, hour, sum(polls) AS polls, min(source) AS source FROM ap_load_hourly GROUP BY 1, 2
)
SELECT coalesce(o.sensor_id, l.sensor_id) AS sensor_id,
       (coalesce(o.hour, l.hour) AT TIME ZONE 'UTC')::date AS day,
       count(o.hour) AS hours_with_obs, count(l.hour) AS hours_with_load,
       count(*) FILTER (WHERE o.hour IS NOT NULL AND l.hour IS NULL) AS hours_missing_load,
       count(*) FILTER (WHERE l.hour IS NOT NULL AND o.hour IS NULL) AS hours_load_only,
       sum(o.n_obs) AS obs_rows, sum(l.polls) AS load_polls,
       round(100.0 * sum(l.polls) / nullif(sum(o.n_obs) FILTER (WHERE l.hour IS NOT NULL), 0), 2)
         AS polls_pct_of_obs,
       string_agg(DISTINCT l.source, ',') AS sources
FROM obs o FULL JOIN load l ON l.sensor_id = o.sensor_id AND l.hour = o.hour
GROUP BY 1, 2 ORDER BY 1, 2;

\echo == per AP-hour: load polls above the observation rows (must be 0)
SELECT count(*) AS ap_hours_with_more_polls_than_observations
FROM (SELECT sensor_id, device_key, hour, sum(polls) AS polls FROM ap_load_hourly GROUP BY 1, 2, 3) l
JOIN ap_rssi_hourly r USING (sensor_id, device_key, hour)
WHERE l.polls > r.n_obs;
