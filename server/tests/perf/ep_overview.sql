-- GET /api/overview (api/app/routers/overview.py), statement by statement.
\echo == /api/overview: polls
EXPLAIN (ANALYZE, BUFFERS)
SELECT min(ts) AS first_poll, max(ts) AS last_poll, count(*) AS total,
       count(*) FILTER (WHERE ok) AS ok FROM polls WHERE sensor_id = :sid;
\echo == /api/overview: gaps
EXPLAIN (ANALYZE, BUFFERS)
SELECT prev_ts AS gap_start, ts AS gap_end, extract(epoch FROM gap)::float8 AS gap_s
FROM (SELECT ts, lag(ts) OVER (ORDER BY ts) AS prev_ts,
             ts - lag(ts) OVER (ORDER BY ts) AS gap
      FROM polls WHERE sensor_id = :sid) g
WHERE gap > interval '90 seconds'
ORDER BY gap DESC LIMIT 10;
\echo == /api/overview: gap totals
EXPLAIN (ANALYZE, BUFFERS)
SELECT count(*) AS n,
       coalesce(extract(epoch FROM sum(gap)), 0)::float8 AS total_s,
       coalesce(extract(epoch FROM max(gap)), 0)::float8 AS max_s
FROM (SELECT ts - lag(ts) OVER (ORDER BY ts) AS gap
      FROM polls WHERE sensor_id = :sid) g
WHERE gap > interval '90 seconds';
\echo == /api/overview: device types
EXPLAIN (ANALYZE, BUFFERS)
SELECT type, count(*) AS n FROM devices WHERE sensor_id = :sid GROUP BY type;
\echo == /api/overview: observations = sum(polls.new_obs)
EXPLAIN (ANALYZE, BUFFERS)
SELECT coalesce(sum(new_obs), 0)::bigint AS n FROM polls WHERE sensor_id = :sid;
\echo == /api/overview: alerts
EXPLAIN (ANALYZE, BUFFERS)
SELECT count(*) AS total, max(ts) AS last_ts FROM alerts WHERE sensor_id = :sid;
\echo == /api/overview: baselines
EXPLAIN (ANALYZE, BUFFERS)
SELECT count(*) AS n FROM ap_baselines WHERE sensor_id = :sid AND rssi_median IS NOT NULL;
\echo == /api/overview: detections
EXPLAIN (ANALYZE, BUFFERS)
SELECT count(*) AS total, count(*) FILTER (WHERE NOT acked) AS open, max(ts) AS last_ts
FROM detections WHERE sensor_id = :sid;
