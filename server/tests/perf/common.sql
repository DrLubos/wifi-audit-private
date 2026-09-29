-- Prepended to every endpoint file by measure.sh. Read-only: measure.sh runs
-- psql as claude_ro (ops/create_ro_role.sql).
\set ON_ERROR_STOP on
\pset pager off
SET track_io_timing = on;            -- I/O read time in the BUFFERS lines (superuser)
SET application_name = 'perf-measure';
-- The first sensor (get_sensor's default) and its busiest AP (most readings in
-- ap_baselines; a few pages, so the cold runs stay cold).
SELECT id AS sid FROM sensors ORDER BY id LIMIT 1 \gset
SELECT device_key AS key FROM ap_baselines WHERE sensor_id = :sid
 ORDER BY coalesce(n_obs, 0) + coalesce(n_floor, 0) DESC LIMIT 1 \gset
