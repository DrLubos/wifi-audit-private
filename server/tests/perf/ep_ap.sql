-- GET /api/aps/{key} (api/app/routers/aps.py get_ap) for the busiest AP.
\echo == /api/aps/{key}: identity (ap_inventory + country_expected)
EXPLAIN (ANALYZE, BUFFERS)
SELECT device_key, upper(bssid::text) AS bssid, upper(left(bssid::text, 8)) AS oui,
       random_bssid, hidden, ssid, manuf, crypt, adv_channel, ht_mode,
       first_seen, last_seen, extract(epoch FROM lifetime)::float8 AS lifetime_s,
       coalesce(trusted, false) AS trusted, rssi_median, rssi_robust_sd, rssi_sd,
       baseline_n_obs, baseline_n_days, cloaked, mfp_sup, mfp_req, beacon_rate, country,
       config_changed_at,
       (SELECT mode() WITHIN GROUP (ORDER BY c.country) FROM devices c
         WHERE c.sensor_id = i.sensor_id AND c.type = 'ap' AND c.country IS NOT NULL)
       AS country_expected
FROM ap_inventory i WHERE sensor_id = :sid AND device_key = :'key';
\echo == /api/aps/{key}: baseline
EXPLAIN (ANALYZE, BUFFERS)
SELECT rssi_median, rssi_mean, rssi_sd, rssi_robust_sd, rssi_p5, rssi_p95,
       main_freq_khz, n_obs, n_floor, n_days, window_start, window_end, computed_at, trusted,
       trusted_at, note FROM ap_baselines WHERE sensor_id = :sid AND device_key = :'key';
\echo == /api/aps/{key}: observation summary
EXPLAIN (ANALYZE, BUFFERS)
SELECT count(*) AS n_obs, count(rssi_valid(rssi)) AS n_rssi,
       count(*) FILTER (WHERE rssi_is_floor(rssi, rssi_floor)) AS n_floor,
       min(ts) AS first_obs, max(ts) AS last_obs
FROM observations WHERE sensor_id = :sid AND device_key = :'key';
\echo == /api/aps/{key}: raw history summary
EXPLAIN (ANALYZE, BUFFERS)
SELECT count(*) AS n, coalesce(bool_or(cloaked AND ssid = ''), false) AS hidden_beacon,
       (array_agg(ssid ORDER BY ts DESC) FILTER (WHERE ssid <> ''))[1] AS name_seen
FROM device_config_history WHERE sensor_id = :sid AND device_key = :'key';
\echo == /api/aps/{key}: config changes (ap_config_changes; look for the Index Cond on device_config_history / devices)
EXPLAIN (ANALYZE, BUFFERS)
SELECT ts, changes, count(*) OVER () AS total
FROM (SELECT ts, json_agg(json_build_object('field', field, 'old', old_value, 'new', new_value)
                          ORDER BY field) AS changes
      FROM ap_config_changes WHERE sensor_id = :sid AND device_key = :'key'
      GROUP BY ts) g
ORDER BY ts DESC LIMIT 200;
