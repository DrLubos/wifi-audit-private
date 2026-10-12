-- GET /api/v2/aps, /api/v2/aps/{bssid}, /aps/{bssid}/rssi, /aps/{bssid}/load
-- (Aps and ApDetail boards). An account AP is one BSSID (account_aps);
-- signal strength belongs to the pair AP + sensor. Clients of an AP: the
-- station count of its BSS Load element when it sends one, else Kismet's
-- client count (SPEC section 4; before buffer v7 a BSS Load element is
-- inferred from a station count or utilization > 0, as in ap_load_hourly).

-- name: _clients
CASE WHEN coalesce(v.cur_qbss_stations, 0) > 0 OR coalesce(v.cur_util_pct, 0) > 0
     THEN v.cur_qbss_stations ELSE v.cur_n_clients END

-- name: _clients_source
-- where _clients comes from: 'ap' = the station count of its BSS Load
-- element; 'sensor' = Kismet's num_associated_clients, which counts every
-- client seen associated since Kismet began tracking the AP (Kismet
-- 2025-09-R1 never prunes associated_client_map) - not a current count
CASE WHEN coalesce(v.cur_qbss_stations, 0) > 0 OR coalesce(v.cur_util_pct, 0) > 0 THEN 'ap'
     WHEN v.cur_n_clients IS NOT NULL THEN 'sensor' END

-- name: account_aps
-- Every account AP (account_aps) with the policy check of its managed
-- network (security_meets(), once per policy and configuration) and the
-- clients / utilization as last measured. ~90 ms of per-row functions for
-- ~850 APs on the e2-micro, so the api caches the rows per account until the
-- next refresh or write (app/v2/apcache.py).
WITH v AS MATERIALIZED (
  SELECT v.* FROM account_aps v WHERE v.account_id = %(account_id)s
), pol AS MATERIALIZED (
  SELECT c.*, security_meets(c.expected_security, c.require_mfp, c.crypt, c.crypt_bits_known, c.mfp_req) AS reason
  FROM (SELECT DISTINCT n.expected_security, n.require_mfp, v.crypt, v.crypt_bits_known, v.mfp_req
        FROM v JOIN networks n ON n.id = v.network_id WHERE n.kind = 'managed') c
)
SELECT v.ap_id, upper(v.bssid::text) AS bssid, v.ssid, v.hidden, v.network_name, v.network_key, v.network_kind,
       v.manuf, v.random_bssid, v.mobile, v.classification, v.classified_by, v.security, v.security_label,
       v.network_kind = 'managed' AS has_policy, p.reason AS policy_reason,
       v.adv_channel AS channel, v.band, v.ht_mode, {{_clients}} AS clients, {{_clients_source}} AS clients_source,
       CASE WHEN coalesce(v.cur_qbss_stations, 0) > 0 OR coalesce(v.cur_util_pct, 0) > 0
            THEN v.cur_util_pct END AS util,
       v.first_seen, v.last_seen,
       -- first seen in the warm-up of the sensor that saw it first (its 24 h
       -- after that sensor's first poll): never a new AP (decided 2026-10-12)
       coalesce((SELECT coalesce(s.first_obs, s.first_seen) < ss.first_poll + interval '24 hours'
                 FROM ap_sensors x
                 JOIN ap_summary s ON s.sensor_id = x.sensor_id AND s.device_key = x.device_key
                 JOIN sensor_summary ss ON ss.sensor_id = x.sensor_id
                 WHERE x.ap_id = v.ap_id
                 ORDER BY coalesce(s.first_obs, s.first_seen) LIMIT 1), false) AS warmup
FROM v
LEFT JOIN networks n ON n.id = v.network_id
LEFT JOIN pol p ON n.kind = 'managed'
     AND (p.expected_security, p.require_mfp, p.crypt, p.crypt_bits_known, p.mfp_req)
         IS NOT DISTINCT FROM (n.expected_security, n.require_mfp, v.crypt, v.crypt_bits_known, v.mfp_req)

-- name: seen_by
-- The account APs seen by the selected sensors in the period, each with its
-- sensors and their median of the hourly RSSI medians in the period (Q6),
-- strongest first (NULL: floor readings only).
SELECT s.ap_id,
       json_agg(json_build_object('sensor', s.sensor, 'rssi_median', round(s.rssi::numeric, 1))
                ORDER BY s.rssi DESC NULLS LAST, s.sensor_id) AS seen_by
FROM (SELECT x.ap_id, se.id AS sensor_id, se.name AS sensor,
             percentile_cont(0.5) WITHIN GROUP (ORDER BY h.median) AS rssi
      FROM ap_rssi_hourly h
      JOIN ap_sensors x ON x.sensor_id = h.sensor_id AND x.device_key = h.device_key
      JOIN sensors se ON se.id = h.sensor_id
      WHERE h.sensor_id = ANY (%(sensor_ids)s::int[])
        AND h.hour >= %(from)s::timestamptz AND h.hour < %(to)s::timestamptz
      GROUP BY x.ap_id, se.id, se.name) s
GROUP BY s.ap_id

-- name: header
-- The page header: APs in the account, active now (seen within 5 min of
-- the latest collecting poll), new in the 24 h before it.
SELECT count(*)::int AS aps,
       count(*) FILTER (WHERE a.last_seen >= %(latest)s::timestamptz - interval '5 minutes')::int AS active,
       count(*) FILTER (WHERE a.first_seen >= %(latest)s::timestamptz - interval '24 hours'
                          AND NOT coalesce((SELECT coalesce(s.first_obs, s.first_seen) < ss.first_poll + interval '24 hours'
                                            FROM ap_sensors x
                                            JOIN ap_summary s ON s.sensor_id = x.sensor_id AND s.device_key = x.device_key
                                            JOIN sensor_summary ss ON ss.sensor_id = x.sensor_id
                                            WHERE x.ap_id = a.id
                                            ORDER BY coalesce(s.first_obs, s.first_seen) LIMIT 1), false))::int AS new_24h
FROM aps a
WHERE a.account_id = %(account_id)s
  AND EXISTS (SELECT 1 FROM ap_sensors x WHERE x.ap_id = a.id AND x.sensor_id = ANY (%(sensor_ids)s::int[]))

-- name: ap
-- One AP: identity, configuration and security of its newest sensor row
-- (account_aps), the policy of its managed network, the load as last
-- measured.
SELECT v.ap_id, upper(v.bssid::text) AS bssid, upper(left(v.bssid::text, 8)) AS oui, v.ssid, v.hidden,
       v.name_seen, v.network_name, v.network_key, v.network_kind, v.manuf, v.random_bssid, v.mobile,
       v.adv_channel AS channel, v.band, v.ht_mode, v.country, v.first_seen, v.last_seen,
       v.crypt, v.security, v.security_label, security_detail(v.crypt_bits_known) AS detail,
       mfp_state(v.mfp_sup, v.mfp_req) AS mfp,
       n.expected_security, n.require_mfp,
       CASE WHEN n.kind = 'managed'
            THEN security_meets(n.expected_security, n.require_mfp, v.crypt, v.crypt_bits_known, v.mfp_req) END
         AS policy_reason,
       v.cur_n_clients, v.cur_qbss_stations, v.cur_util_pct, v.cur_at, {{_clients}} AS clients,
       (SELECT name FROM sensors WHERE id = v.sensor_id) AS load_sensor,
       v.classification, v.classified_by, v.manual_classification, v.classified_at, v.note, v.note_at
FROM account_aps v
LEFT JOIN networks n ON n.id = v.network_id
WHERE v.account_id = %(account_id)s AND v.bssid = %(bssid)s::macaddr

-- name: ap_sensors
-- Per sensor of the AP: its baseline (ap_baselines via ap_summary; floor
-- share = censored readings / all readings of the window), observations in
-- the 7 days before the sensor's last poll, first observation, first / last
-- seen, configuration changes and days with observations.
SELECT se.name AS sensor, s.has_baseline, s.rssi_median, s.rssi_robust_sd, s.baseline_n_obs, s.baseline_n_floor,
       s.baseline_n_days, s.baseline_window_start, s.baseline_window_end, s.baseline_at, s.trusted,
       s.first_seen, s.last_seen, s.first_obs, s.last_obs, s.n_changes, s.rssi_typical,
       (SELECT coalesce(sum(h.n_obs), 0)::int FROM ap_rssi_hourly h
        WHERE h.sensor_id = s.sensor_id AND h.device_key = s.device_key
          AND h.hour >= ss.last_poll - interval '7 days') AS obs_7d,
       (SELECT count(DISTINCT date_trunc('day', h.hour, 'UTC'))::int FROM ap_rssi_hourly h
        WHERE h.sensor_id = s.sensor_id AND h.device_key = s.device_key) AS days_seen,
       (SELECT count(DISTINCT c.ts)::int FROM ap_config_changes c
        WHERE c.sensor_id = s.sensor_id AND c.device_key = s.device_key AND c.field = 'adv_channel') AS channel_changes
FROM ap_sensors x
JOIN sensors se ON se.id = x.sensor_id
JOIN ap_summary s ON s.sensor_id = x.sensor_id AND s.device_key = x.device_key
LEFT JOIN sensor_summary ss ON ss.sensor_id = x.sensor_id
WHERE x.ap_id = %(ap_id)s::bigint
ORDER BY s.rssi_typical DESC NULLS LAST, se.id

-- name: config_history
-- The cleaned configuration timeline (ap_config_changes) of the AP on every
-- sensor, newest first: one row per sensor and poll with the fields that
-- changed there (old -> new).
SELECT c.ts, se.name AS sensor,
       json_agg(json_build_object('field', c.field, 'old', c.old_value, 'new', c.new_value) ORDER BY c.field) AS changes
FROM ap_sensors x
JOIN sensors se ON se.id = x.sensor_id
JOIN ap_config_changes c ON c.sensor_id = x.sensor_id AND c.device_key = x.device_key
WHERE x.ap_id = %(ap_id)s::bigint
GROUP BY c.ts, se.id, se.name
ORDER BY c.ts DESC, se.id
LIMIT 200

-- name: events
-- The AP's events on every sensor, newest first.
SELECT d.id, d.ts, d.ts_end, d.severity, d.kind, d.type, d.summary, d.status, d.status_at, d.status_note,
       se.name AS sensor
FROM detections d
JOIN sensors se ON se.id = d.sensor_id AND se.account_id = %(account_id)s
WHERE d.mac = %(bssid)s::macaddr
ORDER BY d.ts DESC, d.id DESC
LIMIT 100

-- name: rssi
-- Hourly RSSI medians of the AP per sensor over the window (D8).
SELECT h.hour, se.name AS sensor, h.median, h.n_valid, h.n_floor
FROM ap_sensors x
JOIN sensors se ON se.id = x.sensor_id
JOIN ap_rssi_hourly h ON h.sensor_id = x.sensor_id AND h.device_key = x.device_key
WHERE x.ap_id = %(ap_id)s::bigint
  AND h.hour >= %(from)s::timestamptz AND h.hour < %(to)s::timestamptz
ORDER BY h.hour, se.id

-- name: load
-- The AP's load per UTC hour (all its sensors and channels): clients as
-- everywhere (BSS Load station count when present, else Kismet's count; max
-- over polls, channels and sensors), utilization = mean of the polls that
-- reported it, and the hourly peak.
SELECT l.hour,
       CASE WHEN bool_or(l.qbss_polls > 0) THEN max(l.qbss_max) FILTER (WHERE l.qbss_polls > 0)
            ELSE max(l.clients_max) END AS clients,
       sum(l.util_sum) FILTER (WHERE l.qbss_polls > 0) AS util_sum,
       sum(l.qbss_polls) FILTER (WHERE l.qbss_polls > 0)::int AS util_polls,
       max(l.util_max) FILTER (WHERE l.qbss_polls > 0) AS util_max
FROM ap_sensors x
JOIN ap_load_hourly l ON l.sensor_id = x.sensor_id AND l.device_key = x.device_key
WHERE x.ap_id = %(ap_id)s::bigint
  AND l.hour >= %(from)s::timestamptz AND l.hour < %(to)s::timestamptz
GROUP BY l.hour
ORDER BY l.hour

-- name: sensor_ids
-- The sensors of the AP (their capture hours make the gaps of its charts).
SELECT array_agg(x.sensor_id ORDER BY x.sensor_id) AS ids FROM ap_sensors x WHERE x.ap_id = %(ap_id)s::bigint
