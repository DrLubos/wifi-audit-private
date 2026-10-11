-- Read-only check of schema 9 on the real data after ops/migrate_schema9.sh
-- (not a fixture test; it reads, never writes - runs as claude_ro too):
--
--   docker compose exec -T db psql -U claude_ro -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 < tests/sql/check_schema9.sql
--
-- Every "must be 0" line must read 0.
\set ON_ERROR_STOP on
\pset pager off
SET statement_timeout = '10min';

\echo == version, account, sensors
SELECT value AS schema_version FROM schema_meta WHERE key = 'schema_version';
SELECT a.id, a.name, a.tz, a.write_version, st.managed_by_vendor, st.mobile_vendors, st.sensor_offline_min
FROM accounts a LEFT JOIN account_settings st ON st.account_id = a.id;
SELECT id, account_id, name, location, hardware, tz FROM sensors ORDER BY id;

\echo == account APs: one per BSSID, every AP device mapped (must be 0, 0, 0)
SELECT (SELECT count(*) FROM (SELECT DISTINCT mac FROM devices WHERE type = 'ap') x)
         - (SELECT count(*) FROM aps a WHERE EXISTS (SELECT 1 FROM ap_sensors x WHERE x.ap_id = a.id))
         AS aps_vs_bssids,
       (SELECT count(*) FROM devices d WHERE d.type = 'ap'
          AND NOT EXISTS (SELECT 1 FROM ap_sensors x WHERE x.sensor_id = d.sensor_id AND x.device_key = d.device_key))
         AS ap_devices_unmapped,
       (SELECT count(*) FROM ap_sensors x JOIN devices d USING (sensor_id, device_key) WHERE d.type <> 'ap')
         AS non_ap_mapped;
SELECT count(*) AS account_aps, count(*) FILTER (WHERE mobile) AS mobile,
       count(*) FILTER (WHERE classified_by = 'manual') AS manual
FROM account_aps;
SELECT classification, count(*) FROM account_aps GROUP BY 1 ORDER BY 2 DESC;

\echo == security of the current configuration (PLAN.md section 5.3)
SELECT security, security_label, count(*) FROM ap_summary GROUP BY 1, 2 ORDER BY 3 DESC;
SELECT count(*) AS periods, count(*) FILTER (WHERE valid_from > '-infinity') AS change_points,
       count(DISTINCT device_key) FILTER (WHERE valid_from > '-infinity') AS aps_with_changes
FROM ap_security_periods;
\echo == periods whose last row disagrees with ap_summary (must be 0)
SELECT count(*) AS mismatches
FROM ap_security_periods p JOIN ap_summary s USING (sensor_id, device_key)
WHERE p.valid_to = 'infinity' AND (p.level, p.label) IS DISTINCT FROM (s.security, s.security_label);

\echo == collecting / limited polls per hour = the polls themselves (must be 0, 0)
SELECT (SELECT coalesce(sum(capture_polls), 0) FROM sensor_hourly)
         - (SELECT count(*) FROM polls WHERE ok AND ds_running AND devices_active > 0) AS capture_diff,
       (SELECT coalesce(sum(limited_polls), 0) FROM sensor_hourly)
         - (SELECT count(*) FROM polls WHERE ok AND ds_running AND devices_active > 0 AND ds_hop_ok = false)
         AS limited_diff;
SELECT sensor_id, last_poll, last_capture_at, last_hop_n, last_hop_visited, last_hop_ok FROM sensor_summary;

\echo == sensor periods (findings section 6) and their annotations
SELECT p.kind, p.starts_at, p.ends_at, coalesce(p.ends_at, s.last_poll) - p.starts_at AS length, p.polls,
       (SELECT string_agg(a.label, '; ') FROM sensor_annotations a
        WHERE a.sensor_id = p.sensor_id
          AND CASE p.kind WHEN 'outage' THEN a.kind IN ('gap', 'no_frames') ELSE a.kind = 'coverage' END
          AND a.starts_at < coalesce(p.ends_at, 'infinity') AND coalesce(a.ends_at, a.starts_at) > p.starts_at) AS cause
FROM sensor_periods p JOIN sensor_summary s USING (sensor_id) ORDER BY p.starts_at;
SELECT kind, count(*) FROM sensor_annotations GROUP BY 1 ORDER BY 1;
SELECT version, channel_plan, n_channels, confirmed_at FROM sensor_config ORDER BY sensor_id, version;

\echo == events
SELECT kind, type, severity, status, count(*), min(ts), max(ts)
FROM detections GROUP BY 1, 2, 3, 4 ORDER BY 1, 2, 3, 4;
\echo == privacy: event subjects that are not AP BSSIDs, any other MAC in an event row (must be 0, 0)
SELECT (SELECT count(*) FROM detections e
        WHERE e.kind <> 'detection' AND e.mac IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM devices d WHERE d.sensor_id = e.sensor_id AND d.type = 'ap' AND d.mac = e.mac))
         AS non_ap_subjects,
       (SELECT count(*)
        FROM (SELECT e.sensor_id,
                     (regexp_matches(e::text, '([0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5})', 'g'))[1]::macaddr AS m
              FROM detections e WHERE e.kind <> 'detection') x
        WHERE NOT EXISTS (SELECT 1 FROM devices d WHERE d.sensor_id = x.sensor_id AND d.type = 'ap'
                                                    AND d.mac = x.m)) AS non_ap_macs_in_rows;

\echo == old api still served from detector rows only (detections of kind detection)
SELECT count(*) AS detector_rows, count(*) FILTER (WHERE status = 'open') AS open FROM detections WHERE kind = 'detection';
