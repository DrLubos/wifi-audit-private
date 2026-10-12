-- Read-only check of the schema-10 migration (devices.ever_ap, the Kismet
-- attribution, the duplicate merge). Runs as claude_ro:
--
--   docker compose exec -T db sh -c 'psql -X -v ON_ERROR_STOP=1 -U claude_ro -d "$POSTGRES_DB"' \
--     < tests/sql/check_schema10.sql
--
-- Expected: schema_version 10, the trigger present, no AP-typed device
-- without ever_ap, every ever_ap device of a refreshed sensor in ap_summary
-- and ap_sensors, no Kismet event without a subject that still has an
-- attributed twin (0 after ops/merge_duplicate_events.sql -v apply=1), every
-- attributed event's BSSID an account AP.
\set ON_ERROR_STOP on
\pset pager off

SELECT (SELECT value FROM schema_meta WHERE key = 'schema_version') AS schema_version,
       (SELECT count(*) FROM pg_trigger WHERE tgname = 'devices_ever_ap' AND NOT tgisinternal) AS trigger,
       (SELECT count(*) FROM devices WHERE type = 'ap' AND NOT ever_ap) AS ap_without_ever_ap;

SELECT type, count(*) FILTER (WHERE ever_ap) AS ever_ap, count(*) AS devices
FROM devices GROUP BY type ORDER BY devices DESC;

SELECT (SELECT count(*) FROM devices d
        WHERE d.ever_ap AND EXISTS (SELECT 1 FROM sensor_summary s WHERE s.sensor_id = d.sensor_id)
          AND NOT EXISTS (SELECT 1 FROM ap_summary a WHERE a.sensor_id = d.sensor_id AND a.device_key = d.device_key))
         AS ever_ap_not_in_ap_summary,
       (SELECT count(*) FROM ap_summary a
        WHERE NOT EXISTS (SELECT 1 FROM ap_sensors x WHERE x.sensor_id = a.sensor_id AND x.device_key = a.device_key))
         AS ap_summary_unmapped,
       (SELECT count(*) FROM ap_summary a JOIN devices d USING (sensor_id, device_key) WHERE d.type <> 'ap')
         AS retyped_aps_kept;

SELECT (SELECT count(*) FROM detections WHERE kind = 'kismet' AND mac IS NULL AND device_key IS NULL)
         AS kismet_without_subject,
       (SELECT count(*) FROM detections u
        WHERE u.kind = 'kismet' AND u.mac IS NULL AND u.device_key IS NULL
          AND EXISTS (SELECT 1 FROM detections k
                      WHERE k.sensor_id = u.sensor_id AND k.kind = 'kismet' AND k.type = u.type AND k.ts = u.ts
                        AND k.ts_end IS NOT DISTINCT FROM u.ts_end AND k.mac IS NOT NULL))
         AS duplicates_left,
       (SELECT count(*) FROM detections e JOIN sensors s ON s.id = e.sensor_id
        WHERE e.kind = 'kismet' AND e.mac IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM aps a WHERE a.account_id = s.account_id AND a.bssid = e.mac))
         AS kismet_mac_not_an_account_ap,
       (SELECT count(*) FROM audit_log WHERE action = 'event.merge') AS merges_logged;
