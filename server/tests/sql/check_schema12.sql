-- Read-only check of the schema-11 and schema-12 migrations (the label
-- outcomes and their reasons, the capture hardware, hardware-bounded
-- baselines, the sensor history corrections).
-- Runs as claude_ro:
--
--   docker compose exec -T db sh -c 'psql -X -v ON_ERROR_STOP=1 -U claude_ro -d "$POSTGRES_DB"' \
--     < tests/sql/check_schema12.sql
--
-- Expected: schema_version 12, the constraints and the trigger present, every
-- labelled event with a reason that fits its outcome (the constraint), every
-- sensor with a hardware record whose adapter and antenna are of the right
-- kind, every baseline with statistics learned inside one hardware period
-- (hw_from set, window_start >= hw_from), the three corrected annotations.
\set ON_ERROR_STOP on
\pset pager off

SELECT (SELECT value FROM schema_meta WHERE key = 'schema_version') AS schema_version,
       (SELECT pg_get_constraintdef(oid) LIKE '%benign%' FROM pg_constraint WHERE conname = 'detections_status_reason') AS reason_constraint_12,
       (SELECT count(*) FROM pg_constraint WHERE conname = 'capture_hardware_gain_check') AS gain_check,
       (SELECT count(*) FROM pg_trigger WHERE tgname = 'sensor_hardware_kinds' AND NOT tgisinternal) AS hw_trigger;

SELECT status, status_reason, count(*) AS events FROM detections GROUP BY 1, 2 ORDER BY 1, 2;

SELECT h.id, h.kind, h.name, h.gain_dbi, h.bands, h.connector FROM capture_hardware h ORDER BY h.id;

SELECT s.name AS sensor, x.valid_from, a.kind AS adapter_kind, a.name AS adapter, n.kind AS antenna_kind,
       n.name AS antenna, n.gain_dbi, hardware_period(s.id, now()) AS current_period
FROM sensors s
LEFT JOIN sensor_hardware x ON x.sensor_id = s.id
LEFT JOIN capture_hardware a ON a.id = x.adapter_id
LEFT JOIN capture_hardware n ON n.id = x.antenna_id
ORDER BY s.id, x.valid_from;

SELECT sensor_id, hw_from, count(*) FILTER (WHERE rssi_median IS NOT NULL) AS with_statistics,
       count(*) FILTER (WHERE rssi_median IS NOT NULL AND (hw_from IS NULL OR window_start < hw_from))
         AS outside_their_period,
       count(*) AS baselines
FROM ap_baselines GROUP BY 1, 2 ORDER BY 1, 2;

SELECT starts_at, ends_at, kind, label FROM sensor_annotations
WHERE starts_at IN ('2026-09-15 13:53:07+00', '2026-09-27 12:17:20+00', '2026-10-06 12:23:51+00')
ORDER BY starts_at, kind;
