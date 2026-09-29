-- GET /api/aps (api/app/routers/aps.py list_aps).
\echo == /api/aps
EXPLAIN (ANALYZE, BUFFERS)
SELECT device_key, upper(bssid::text) AS bssid, upper(left(bssid::text, 8)) AS oui,
       random_bssid, hidden, ssid, manuf, crypt, adv_channel, ht_mode,
       first_seen, last_seen, extract(epoch FROM lifetime)::float8 AS lifetime_s,
       coalesce(trusted, false) AS trusted, rssi_median, rssi_robust_sd, rssi_sd,
       baseline_n_obs, baseline_n_days
FROM ap_inventory WHERE sensor_id = :sid ORDER BY last_seen DESC;
