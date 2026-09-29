-- Storage report: every table and index, observations by device type and the
-- non-NULL share of every observations column. Read-only; the column query is
-- one sequential scan of observations (tens of seconds on the 1 GB box).
\set ON_ERROR_STOP on
\pset pager off
SELECT id AS sid FROM sensors ORDER BY id LIMIT 1 \gset

\echo == server
SELECT value AS schema_version FROM schema_meta WHERE key = 'schema_version';
SELECT version();
SELECT name, setting, unit FROM pg_settings
 WHERE name IN ('shared_buffers', 'work_mem', 'maintenance_work_mem', 'effective_cache_size',
                'max_wal_size', 'wal_level', 'random_page_cost', 'jit', 'autovacuum',
                'max_parallel_workers_per_gather', 'max_parallel_maintenance_workers')
 ORDER BY name;
SELECT pg_size_pretty(pg_database_size(current_database())) AS database,
       (SELECT pg_size_pretty(sum(size)) FROM pg_ls_waldir()) AS wal_dir;

\echo == tables (total = heap + toast + indexes)
SELECT c.relname AS table, c.reltuples::bigint AS est_rows,
       pg_size_pretty(pg_table_size(c.oid)) AS heap_toast,
       pg_size_pretty(pg_indexes_size(c.oid)) AS indexes,
       pg_size_pretty(pg_total_relation_size(c.oid)) AS total,
       pg_total_relation_size(c.oid) AS total_bytes
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind IN ('r', 'm', 'p')
ORDER BY pg_total_relation_size(c.oid) DESC;

\echo == indexes
SELECT t.relname AS table, i.relname AS index, pg_size_pretty(pg_relation_size(i.oid)) AS size,
       pg_relation_size(i.oid) AS bytes, pg_get_indexdef(i.oid) AS definition
FROM pg_index x JOIN pg_class i ON i.oid = x.indexrelid JOIN pg_class t ON t.oid = x.indrelid
JOIN pg_namespace n ON n.oid = t.relnamespace
WHERE n.nspname = 'public'
ORDER BY pg_relation_size(i.oid) DESC;

\echo == observations: physical column order (alignment padding)
SELECT attnum, attname, format_type(atttypid, atttypmod) AS type, attlen, attalign
FROM pg_attribute WHERE attrelid = 'observations'::regclass AND attnum > 0 AND NOT attisdropped
ORDER BY attnum;
SELECT relpages, reltuples::bigint, relallvisible,
       round(pg_relation_size(oid)::numeric / nullif(reltuples, 0)::numeric, 1) AS heap_bytes_per_row
FROM pg_class WHERE oid = 'observations'::regclass;
SELECT relname, n_live_tup, n_dead_tup, last_vacuum, last_autovacuum, last_analyze
FROM pg_stat_user_tables ORDER BY n_live_tup DESC;

\echo == observations: rows by devices.type and non-NULL share (%) of every column
\echo    (the Time: line is one full read of observations - the read part of a copy)
\x on
\timing on
SELECT d.type, count(*) AS rows,
       round(100.0 * count(*) / sum(count(*)) OVER (), 1) AS pct_of_rows,
       min(o.ts) AS first_ts, max(o.ts) AS last_ts,
       round(100.0 * count(o.last_time)        / count(*), 1) AS last_time,
       round(100.0 * count(o.freq_khz)         / count(*), 1) AS freq_khz,
       round(100.0 * count(o.channel)          / count(*), 1) AS channel,
       round(100.0 * count(o.rssi)             / count(*), 1) AS rssi,
       round(100.0 * count(o.rssi_min)         / count(*), 1) AS rssi_min,
       round(100.0 * count(o.rssi_max)         / count(*), 1) AS rssi_max,
       round(100.0 * count(o.rssi_floor)       / count(*), 1) AS rssi_floor,
       round(100.0 * count(o.pk_total)         / count(*), 1) AS pk_total,
       round(100.0 * count(o.pk_tx)            / count(*), 1) AS pk_tx,
       round(100.0 * count(o.pk_rx)            / count(*), 1) AS pk_rx,
       round(100.0 * count(o.pk_data)          / count(*), 1) AS pk_data,
       round(100.0 * count(o.bytes)            / count(*), 1) AS bytes,
       round(100.0 * count(o.n_clients)        / count(*), 1) AS n_clients,
       round(100.0 * count(o.disconnects)      / count(*), 1) AS disconnects,
       round(100.0 * count(o.disconnects_last) / count(*), 1) AS disconnects_last,
       round(100.0 * count(o.qbss_stations)    / count(*), 1) AS qbss_stations,
       round(100.0 * count(o.util_pct)         / count(*), 1) AS util_pct,
       round(100.0 * count(o.bss_timestamp)    / count(*), 1) AS bss_timestamp,
       round(100.0 * count(o.ie_checksum)      / count(*), 1) AS ie_checksum,
       round(100.0 * count(o.beacon_fp)        / count(*), 1) AS beacon_fp,
       round(100.0 * count(o.bssid)            / count(*), 1) AS bssid
FROM observations o
JOIN devices d ON d.sensor_id = o.sensor_id AND d.device_key = o.device_key
WHERE o.sensor_id = :sid
GROUP BY d.type ORDER BY count(*) DESC;
\timing off
\x off
SELECT coalesce(sum(new_obs), 0) AS sum_polls_new_obs FROM polls WHERE sensor_id = :sid;
