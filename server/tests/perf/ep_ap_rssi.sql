-- GET /api/aps/{key}/rssi (api/app/routers/aps.py ap_rssi), schema 6: the AP
-- page's default request (hourly, whole history, ap_rssi_hourly) and the
-- 15-minute view (raw observations, the last 48 h of the AP's data).
\echo == /api/aps/{key}/rssi: AP + baseline (ap_summary)
EXPLAIN (ANALYZE, BUFFERS)
SELECT has_baseline, rssi_median, rssi_robust_sd, rssi_sd FROM ap_summary
WHERE sensor_id = :sid AND device_key = :'key';
\echo == /api/aps/{key}/rssi: hourly (ap_rssi_hourly)
EXPLAIN (ANALYZE, BUFFERS)
SELECT hour AS t, median, p10, p90, min, max, n_valid AS n, n_floor
FROM ap_rssi_hourly
WHERE sensor_id = :sid AND device_key = :'key' AND n_valid + n_floor > 0
ORDER BY hour;
SELECT last_obs - interval '48 hours' + interval '1 second' AS raw_from,
       last_obs + interval '1 second' AS raw_to
FROM ap_summary WHERE sensor_id = :sid AND device_key = :'key' \gset
\echo == /api/aps/{key}/rssi?bucket=900, last 48 h (raw observations, index-only)
EXPLAIN (ANALYZE, BUFFERS)
SELECT date_bin(interval '900 seconds', ts, timestamptz '2000-01-01 00:00:00+00') AS t,
       count(rssi_valid(rssi)) AS n,
       count(*) FILTER (WHERE rssi_is_floor(rssi, rssi_floor)) AS n_floor,
       percentile_cont(ARRAY[0.5, 0.1, 0.9]) WITHIN GROUP (ORDER BY rssi_valid(rssi)) AS pct,
       min(rssi_valid(rssi)) AS min, max(rssi_valid(rssi)) AS max
FROM observations
WHERE sensor_id = :sid AND device_key = :'key' AND (rssi IS NOT NULL OR rssi_floor)
  AND ts >= :'raw_from' AND ts < :'raw_to'
GROUP BY 1 ORDER BY 1;
