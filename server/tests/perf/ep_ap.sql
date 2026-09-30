-- GET /api/aps/{key} (api/app/routers/aps.py get_ap) for the busiest AP,
-- schema 6: one ap_summary row and the AP's rows of the materialized
-- ap_config_changes.
\echo == /api/aps/{key}: ap_summary + country_expected
EXPLAIN (ANALYZE, BUFFERS)
SELECT device_key, upper(bssid::text) AS bssid, upper(left(bssid::text, 8)) AS oui,
       random_bssid, hidden, ssid, manuf, crypt, adv_channel, ht_mode,
       first_seen, last_seen, extract(epoch FROM last_seen - first_seen)::float8 AS lifetime_s,
       trusted, rssi_median, rssi_robust_sd, rssi_sd, baseline_n_obs, baseline_n_days,
       cloaked, mfp_sup, mfp_req, beacon_rate, country, config_changed_at, hidden_beacon,
       name_seen, has_baseline, rssi_mean, rssi_p5, rssi_p95, main_freq_khz, baseline_n_floor,
       baseline_window_start, baseline_window_end, baseline_at, trusted_at, note,
       n_obs, n_valid, n_floor, first_obs, last_obs, history_rows, n_changes,
       (SELECT country_expected FROM sensor_summary ss WHERE ss.sensor_id = a.sensor_id)
         AS country_expected
FROM ap_summary a WHERE sensor_id = :sid AND device_key = :'key';
\echo == /api/aps/{key}: config changes (materialized ap_config_changes, index ap_config_changes_key)
EXPLAIN (ANALYZE, BUFFERS)
SELECT ts, json_agg(json_build_object('field', field, 'old', old_value, 'new', new_value)
                    ORDER BY field) AS changes
FROM ap_config_changes WHERE sensor_id = :sid AND device_key = :'key'
GROUP BY ts ORDER BY ts DESC LIMIT 200;
