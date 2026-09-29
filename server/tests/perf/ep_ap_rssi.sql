-- GET /api/aps/{key}/rssi?bucket=3600 (api/app/routers/aps.py ap_rssi), the
-- AP page's default request: whole history, no from/to.
\echo == /api/aps/{key}/rssi: device exists
EXPLAIN (ANALYZE, BUFFERS)
SELECT 1 FROM devices WHERE sensor_id = :sid AND device_key = :'key';
\echo == /api/aps/{key}/rssi: baseline
EXPLAIN (ANALYZE, BUFFERS)
SELECT rssi_median AS median, rssi_robust_sd AS robust_sd, rssi_sd AS sd
FROM ap_baselines WHERE sensor_id = :sid AND device_key = :'key';
\echo == /api/aps/{key}/rssi: buckets (3600 s)
EXPLAIN (ANALYZE, BUFFERS)
SELECT date_bin(make_interval(secs => 3600), ts, '2000-01-01') AS t,
       count(rssi_valid(rssi)) AS n,
       count(*) FILTER (WHERE rssi_is_floor(rssi, rssi_floor)) AS n_floor,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY rssi_valid(rssi)) AS median,
       min(rssi_valid(rssi)) AS min, max(rssi_valid(rssi)) AS max
FROM observations
WHERE sensor_id = :sid AND device_key = :'key' AND (rssi IS NOT NULL OR rssi_floor)
GROUP BY 1 ORDER BY 1;
