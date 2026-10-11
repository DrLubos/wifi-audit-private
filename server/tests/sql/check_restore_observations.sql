-- Read-only check after ops/restore_observations_from_dump.sh (runs as
-- claude_ro too):
--
--   docker compose exec -T db psql -U claude_ro -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 < tests/sql/check_restore_observations.sql
--
-- Before the restore (2026-10-11): 726 AP-hours on 15 devices had load rows
-- (ap_load_hourly, backfilled from the same dump) but no observation rows,
-- all before 2026-09-26 20:25 - the rows the schema-7 migration skipped. After
-- it both counts below must be 0, and no AP may start its observations at the
-- first snapshot after the migration while Kismet saw it earlier.
\set ON_ERROR_STOP on
\pset pager off
SET statement_timeout = '10min';

\echo == AP-hours with load rows but no observation rows (before: 726 on 15 devices)
SELECT count(*) AS ap_hours, count(DISTINCT l.device_key) AS devices,
       min(l.hour) AS first_hour, max(l.hour) AS last_hour
FROM (SELECT sensor_id, device_key, hour FROM ap_load_hourly GROUP BY 1, 2, 3) l
LEFT JOIN ap_rssi_hourly r USING (sensor_id, device_key, hour)
WHERE r.hour IS NULL;

\echo == AP-hours with more load polls than observation rows (before: 1)
SELECT count(*) AS ap_hours
FROM (SELECT sensor_id, device_key, hour, sum(polls) AS polls FROM ap_load_hourly GROUP BY 1, 2, 3) l
JOIN ap_rssi_hourly r USING (sensor_id, device_key, hour)
WHERE l.polls > r.n_obs;

\echo == APs whose first observation is the start of the 2026-10-10 snapshot although Kismet saw them before (before: 1)
SELECT count(*) AS aps
FROM ap_summary s
WHERE s.first_obs >= '2026-09-26 20:25:13+00' AND s.first_obs < '2026-09-26 21:00+00'
  AND s.first_seen < '2026-09-26 20:00+00';

\echo == observation rows and baselines per sensor
SELECT s.sensor_id, s.obs_ap AS observation_rows, s.baselines, s.refreshed_at
FROM sensor_summary s ORDER BY 1;
