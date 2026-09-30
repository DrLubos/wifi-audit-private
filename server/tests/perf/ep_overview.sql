-- GET /api/overview (api/app/routers/overview.py), schema 6: the api's sensor
-- lookup (with sensor_summary.refreshed_at, the ETag), the live detection
-- counts and the sensor_summary row.
\echo == /api/overview: sensor + refreshed_at (deps.get_sensor)
EXPLAIN (ANALYZE, BUFFERS)
SELECT s.id, s.name, s.location, s.tz, ss.refreshed_at
FROM sensors s LEFT JOIN sensor_summary ss ON ss.sensor_id = s.id ORDER BY s.id LIMIT 1;
\echo == /api/overview: detections (live)
EXPLAIN (ANALYZE, BUFFERS)
SELECT count(*) AS total, count(*) FILTER (WHERE NOT acked) AS open, max(ts) AS last_ts
FROM detections WHERE sensor_id = :sid;
\echo == /api/overview: sensor_summary
EXPLAIN (ANALYZE, BUFFERS)
SELECT * FROM sensor_summary WHERE sensor_id = :sid;
