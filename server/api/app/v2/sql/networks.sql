-- GET /api/v2/networks, /api/v2/networks/timeseries, /api/v2/clients/heatmap
-- (Networks board). A network is an SSID of the account's APs; a hidden AP
-- whose name was seen belongs to that SSID, otherwise it is its own network
-- 'hidden:<BSSID>' (account_aps.network_key). An AP's network, vendor and
-- security are those of its newest sensor row, its mobile flag
-- ap_is_mobile() - all as in the view account_aps, computed here without its
-- per-row classification (only the APs of configured SSIDs need one).

-- name: _ap
-- every account AP: network key, vendor, randomized bit, current security, mobile flag
ap AS MATERIALIZED (
  SELECT c.*, ap_is_mobile(%(account_id)s::int, c.random_bssid, c.manuf, c.first_seen, c.last_seen,
                           %(account_latest)s::timestamptz) AS mobile
  FROM (SELECT DISTINCT ON (a.id) a.id, a.bssid, a.first_seen, a.last_seen,
               coalesce(nullif(s.ssid, ''), s.name_seen, 'hidden:' || upper(a.bssid::text)) AS network_key,
               coalesce(nullif(s.ssid, ''), s.name_seen) IS NULL AS hidden,
               s.manuf, s.random_bssid, s.security, s.security_label
        FROM aps a
        JOIN ap_sensors x ON x.ap_id = a.id
        JOIN ap_summary s ON s.sensor_id = x.sensor_id AND s.device_key = x.device_key
        WHERE a.account_id = %(account_id)s
        ORDER BY a.id, s.last_seen DESC, s.sensor_id) c
)

-- name: networks
-- One row per network seen by the selected sensors in the period: its
-- configuration (networks), BSSIDs seen, vendors, security labels and the
-- weakest level, all-mobile, randomized BSSIDs, mean lifetime of its BSSIDs,
-- and its presence: distinct hours seen, of them in the daytime (07:00-20:00
-- local), on how many local days.
WITH {{_ap}}, nk AS MATERIALIZED (       -- networks numbered: integer keys hash and sort cheaply
  SELECT ap.id, dense_rank() OVER (ORDER BY ap.network_key) AS net FROM ap
), hl AS (                               -- the period's UTC hours in local time
  SELECT g AS hour, g AT TIME ZONE %(tz)s::text AS local
  FROM generate_series(%(from)s::timestamptz, %(to)s::timestamptz - interval '1 hour', interval '1 hour') AS g
), seen AS MATERIALIZED (
  SELECT x.ap_id, nk.net, h.hour
  FROM ap_rssi_hourly h
  JOIN ap_sensors x ON x.sensor_id = h.sensor_id AND x.device_key = h.device_key
  JOIN nk ON nk.id = x.ap_id
  WHERE h.sensor_id = ANY (%(sensor_ids)s::int[])
    AND h.hour >= %(from)s::timestamptz AND h.hour < %(to)s::timestamptz
), presence AS (
  SELECT nh.net, count(*)::int AS hours,
         count(*) FILTER (WHERE extract(hour FROM hl.local) BETWEEN 7 AND 19)::int AS day_hours,
         count(DISTINCT hl.local::date) FILTER (WHERE extract(hour FROM hl.local) BETWEEN 7 AND 19)::int AS days
  FROM (SELECT DISTINCT net, hour FROM seen) nh
  JOIN hl ON hl.hour = nh.hour
  GROUP BY nh.net
), per_net AS (
  SELECT nk.net, ap.network_key, bool_and(ap.hidden) AS hidden, count(*)::int AS bssids,
         bool_and(ap.mobile) AS mobile, bool_and(ap.random_bssid) AS randomized,
         array_agg(DISTINCT ap.manuf) FILTER (WHERE ap.manuf IS NOT NULL) AS vendors,
         array_agg(DISTINCT ap.security_label) AS labels,
         (array_agg(ap.security ORDER BY coalesce(security_rank(ap.security), 99)))[1] AS weakest,
         avg(extract(epoch FROM ap.last_seen - ap.first_seen))::int AS lifetime_s,
         max(ap.last_seen) AS last_seen
  FROM ap
  JOIN nk ON nk.id = ap.id
  JOIN (SELECT DISTINCT ap_id FROM seen) s ON s.ap_id = ap.id
  GROUP BY nk.net, ap.network_key
)
SELECT p.network_key, p.hidden, p.bssids, p.mobile, p.randomized, p.vendors, p.labels, p.weakest, p.lifetime_s,
       p.last_seen, pr.hours, pr.day_hours, pr.days, n.id AS network_id, n.kind, n.expected_security,
       n.require_mfp, n.note
FROM per_net p
JOIN presence pr ON pr.net = p.net
LEFT JOIN networks n ON n.account_id = %(account_id)s AND n.ssid = p.network_key
ORDER BY p.bssids DESC, p.network_key

-- name: bssids
-- The BSSIDs of the configured networks (and every AP broadcasting a managed
-- SSID - the rogue candidates) seen by the selected sensors in the period:
-- configuration, security, effective classification (account_aps, once per
-- AP), compliance with its network's policy (security_meets(), once per
-- policy and configuration), clients now, RSSI per sensor (median of the
-- hourly medians in the period), last seen, the open rule event.
WITH cand AS MATERIALIZED (
  SELECT DISTINCT x.ap_id
  FROM ap_summary s
  JOIN ap_sensors x ON x.sensor_id = s.sensor_id AND x.device_key = s.device_key
  JOIN networks n ON n.account_id = %(account_id)s AND n.ssid = coalesce(nullif(s.ssid, ''), s.name_seen)
  WHERE s.sensor_id = ANY (%(sensor_ids)s::int[])
    AND s.last_seen >= %(from)s::timestamptz AND s.first_seen < %(to)s::timestamptz
), v AS MATERIALIZED (
  SELECT v.*, n.kind, n.expected_security, n.require_mfp
  FROM account_aps v
  JOIN networks n ON n.account_id = v.account_id AND n.ssid = v.network_name
  WHERE v.ap_id = ANY (ARRAY(SELECT ap_id FROM cand))
), verdict AS MATERIALIZED (
  SELECT c.*, security_meets(c.expected_security, c.require_mfp, c.crypt, c.crypt_bits_known, c.mfp_req) AS reason
  FROM (SELECT DISTINCT expected_security, require_mfp, crypt, crypt_bits_known, mfp_req FROM v WHERE kind = 'managed') c
), rssi AS (
  SELECT x.ap_id, se.name AS sensor, percentile_cont(0.5) WITHIN GROUP (ORDER BY h.median) AS median,
         max(h.hour) AS last_hour
  FROM ap_sensors x
  JOIN sensors se ON se.id = x.sensor_id
  JOIN ap_rssi_hourly h ON h.sensor_id = x.sensor_id AND h.device_key = x.device_key
  WHERE x.ap_id IN (SELECT ap_id FROM cand) AND x.sensor_id = ANY (%(sensor_ids)s::int[])
    AND h.hour >= %(from)s::timestamptz AND h.hour < %(to)s::timestamptz
  GROUP BY x.ap_id, se.id, se.name
)
SELECT v.ap_id, upper(v.bssid::text) AS bssid, v.network_name AS network, v.kind, v.manuf, v.random_bssid,
       v.adv_channel AS channel, v.band, v.ht_mode, v.security, v.security_label, mfp_state(v.mfp_sup, v.mfp_req) AS mfp,
       v.classification, v.classified_by, vd.reason,
       coalesce(v.cur_qbss_stations, v.cur_n_clients) AS clients, v.cur_at, v.first_seen, v.last_seen,
       (SELECT json_object_agg(r.sensor, round(r.median::numeric, 1)) FROM rssi r WHERE r.ap_id = v.ap_id) AS rssi,
       (SELECT max(r.last_hour) FROM rssi r WHERE r.ap_id = v.ap_id) AS last_hour,
       (SELECT max(d.id) FROM detections d
        WHERE d.type = 'unknown_bssid_managed_ssid' AND d.mac = v.bssid
          AND d.sensor_id = ANY (%(sensor_ids)s::int[])) AS event_id
FROM v
LEFT JOIN verdict vd ON v.kind = 'managed'
     AND (vd.expected_security, vd.require_mfp, vd.crypt, vd.crypt_bits_known, vd.mfp_req)
         IS NOT DISTINCT FROM (v.expected_security, v.require_mfp, v.crypt, v.crypt_bits_known, v.mfp_req)
WHERE EXISTS (SELECT 1 FROM rssi r WHERE r.ap_id = v.ap_id)
ORDER BY v.network_name, v.bssid

-- name: security_changes
-- Security changes (config events) of the selected sensors in the period,
-- with the AP's network.
SELECT d.id, d.ts, d.severity, upper(d.mac::text) AS bssid,
       coalesce(nullif(su.ssid, ''), su.name_seen, d.ssid, 'hidden:' || upper(d.mac::text)) AS network,
       d.evidence #>> '{from,label}' AS from_label, d.evidence #>> '{to,label}' AS to_label,
       d.evidence #>> '{from,level}' AS from_level, d.evidence #>> '{to,level}' AS to_level,
       d.evidence #>> '{from,mfp}' AS from_mfp, d.evidence #>> '{to,mfp}' AS to_mfp, d.summary
FROM detections d
LEFT JOIN ap_summary su ON su.sensor_id = d.sensor_id AND su.device_key = d.device_key
WHERE d.kind = 'config' AND d.sensor_id = ANY (%(sensor_ids)s::int[])
  AND d.ts >= %(from)s::timestamptz AND d.ts < %(to)s::timestamptz
ORDER BY d.ts DESC, d.id DESC

-- name: timeseries
-- Per bucket: networks seen by the selected sensors, as configured (in
-- networks) / mobile (every BSSID seen in the bucket mobile) / other, and by
-- the weakest security level among their BSSIDs seen in the bucket (each
-- AP's levels over the span of hours it was seen in the bucket).
WITH {{_ap}}, seen AS MATERIALIZED (
  SELECT width_bucket(h.hour, %(starts)s::timestamptz[]) AS i, h.sensor_id, h.device_key,
         min(h.hour) AS h0, max(h.hour) AS h1
  FROM ap_rssi_hourly h
  WHERE h.sensor_id = ANY (%(sensor_ids)s::int[])
    AND h.hour >= %(from)s::timestamptz AND h.hour < %(to)s::timestamptz
  GROUP BY 1, h.sensor_id, h.device_key
), lv AS (
  SELECT s.i, x.ap_id, min(coalesce(security_rank(sp.level), 99)) AS rank
  FROM seen s
  JOIN ap_sensors x ON x.sensor_id = s.sensor_id AND x.device_key = s.device_key
  LEFT JOIN ap_security_periods sp
         ON sp.sensor_id = s.sensor_id AND sp.device_key = s.device_key
        AND sp.valid_from < s.h1 + interval '1 hour' AND sp.valid_to > s.h0
  GROUP BY s.i, x.ap_id
), net AS (
  SELECT lv.i, ap.network_key, bool_and(ap.mobile) AS mobile, min(lv.rank) AS rank
  FROM lv JOIN ap ON ap.id = lv.ap_id
  GROUP BY lv.i, ap.network_key
)
SELECT net.i,
       count(*) FILTER (WHERE n.id IS NOT NULL)::int AS configured,
       count(*) FILTER (WHERE n.id IS NULL AND net.mobile)::int AS mobile,
       count(*) FILTER (WHERE n.id IS NULL AND NOT net.mobile)::int AS other,
       count(*) FILTER (WHERE net.rank = 0)::int AS open,
       count(*) FILTER (WHERE net.rank = 1)::int AS legacy,
       count(*) FILTER (WHERE net.rank = 2)::int AS psk_no_mfp,
       count(*) FILTER (WHERE net.rank = 3)::int AS strong,
       count(*) FILTER (WHERE net.rank = 99)::int AS unknown
FROM net
LEFT JOIN networks n ON n.account_id = %(account_id)s AND n.ssid = net.network_key
GROUP BY net.i
ORDER BY net.i

-- name: clients_hourly
-- Clients on managed APs per UTC hour and network (the heatmap and its
-- per-network split): as overview.clients, summed per network. load: every
-- hour with any load row of the selected sensors (network NULL).
WITH cand AS (
  SELECT DISTINCT x.ap_id
  FROM ap_summary s
  JOIN ap_sensors x ON x.sensor_id = s.sensor_id AND x.device_key = s.device_key
  JOIN networks n ON n.account_id = %(account_id)s AND n.kind = 'managed'
                 AND n.ssid = coalesce(nullif(s.ssid, ''), s.name_seen)
  UNION
  SELECT a.id FROM aps a WHERE a.account_id = %(account_id)s AND a.classification = 'managed'
), mg AS MATERIALIZED (
  SELECT v.ap_id, v.network_key FROM account_aps v
  WHERE v.ap_id = ANY (ARRAY(SELECT ap_id FROM cand)) AND v.classification = 'managed'
), per_ap AS (
  SELECT l.hour, mg.network_key,
         CASE WHEN bool_or(l.qbss_polls > 0) THEN max(l.qbss_max) FILTER (WHERE l.qbss_polls > 0)
              ELSE max(l.clients_max) END AS clients
  FROM ap_load_hourly l
  JOIN ap_sensors x ON x.sensor_id = l.sensor_id AND x.device_key = l.device_key
  JOIN mg ON mg.ap_id = x.ap_id
  WHERE l.sensor_id = ANY (%(sensor_ids)s::int[])
    AND l.hour >= %(from)s::timestamptz AND l.hour < %(to)s::timestamptz
  GROUP BY l.hour, x.ap_id, mg.network_key
)
SELECT hour, network_key AS network, sum(clients)::int AS clients FROM per_ap GROUP BY hour, network_key
UNION ALL
SELECT DISTINCT l.hour, NULL::text, NULL::int FROM ap_load_hourly l
WHERE l.sensor_id = ANY (%(sensor_ids)s::int[])
  AND l.hour >= %(from)s::timestamptz AND l.hour < %(to)s::timestamptz
ORDER BY 1, 2
