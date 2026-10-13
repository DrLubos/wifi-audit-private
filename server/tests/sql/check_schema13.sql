-- Read-only check of the schema-11..13 migrations (the label outcomes and their
-- reasons, the adapter record, the hardware_change boundary, hardware-bounded
-- baselines, the sensor history corrections). Runs as claude_ro:
--
--   docker compose exec -T db sh -c 'psql -X -v ON_ERROR_STOP=1 -U claude_ro -d "$POSTGRES_DB"' \
--     < tests/sql/check_schema13.sql
--
-- Expected: schema_version 13, no catalog tables left, the kind constraint with
-- 'hardware_change', every labelled event with a reason that fits its outcome
-- (the constraint), every sensor with an adapter record, every baseline with
-- statistics learned inside one hardware period (window_start >= hw_from),
-- the three corrected annotations.
\set ON_ERROR_STOP on
\pset pager off

SELECT (SELECT value FROM schema_meta WHERE key = 'schema_version') AS schema_version,
       (SELECT pg_get_constraintdef(oid) LIKE '%benign%' FROM pg_constraint WHERE conname = 'detections_status_reason') AS reason_constraint,
       (SELECT pg_get_constraintdef(oid) LIKE '%hardware_change%' FROM pg_constraint WHERE conname = 'sensor_annotations_kind_check') AS kind_constraint,
       to_regclass('capture_hardware') IS NULL AND to_regclass('sensor_hardware') IS NULL AS catalog_gone;

SELECT status, status_reason, count(*) AS events FROM detections GROUP BY 1, 2 ORDER BY 1, 2;

SELECT s.name AS sensor, a.name AS adapter, a.usb_id, a.mac, a.chipset, a.driver, a.interface, a.bands, a.reported_by,
       hardware_period(s.id, now()) AS current_period
FROM sensors s LEFT JOIN sensor_adapters a ON a.sensor_id = s.id ORDER BY s.id;

SELECT se.name AS sensor, a.starts_at, a.boundary, a.label FROM sensor_annotations a
JOIN sensors se ON se.id = a.sensor_id WHERE a.kind = 'hardware_change' ORDER BY a.starts_at;

SELECT sensor_id, hw_from, count(*) FILTER (WHERE rssi_median IS NOT NULL) AS with_statistics,
       count(*) FILTER (WHERE rssi_median IS NOT NULL AND hw_from IS NOT NULL AND window_start < hw_from)
         AS outside_their_period,
       count(*) AS baselines
FROM ap_baselines GROUP BY 1, 2 ORDER BY 1, 2;

SELECT starts_at, ends_at, kind, label FROM sensor_annotations
WHERE starts_at IN ('2026-09-15 13:53:07+00', '2026-09-27 12:17:20+00', '2026-10-06 12:23:51+00')
ORDER BY starts_at, kind;
