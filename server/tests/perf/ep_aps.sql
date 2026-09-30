-- GET /api/aps (api/app/routers/aps.py list_aps), schema 6: ap_summary, the
-- JSON text built by PostgreSQL.
\echo == /api/aps
EXPLAIN (ANALYZE, BUFFERS)
SELECT json_build_object(
         'count', count(*),
         'aps', coalesce(json_agg(a ORDER BY a.last_seen DESC, a.device_key), '[]'::json))::text AS body
FROM (SELECT device_key, upper(bssid::text) AS bssid, upper(left(bssid::text, 8)) AS oui,
             random_bssid, hidden, ssid, manuf, crypt, adv_channel, ht_mode,
             first_seen, last_seen, extract(epoch FROM last_seen - first_seen)::float8 AS lifetime_s,
             trusted, rssi_median, rssi_robust_sd, rssi_sd, baseline_n_obs, baseline_n_days
      FROM ap_summary WHERE sensor_id = :sid) a;
