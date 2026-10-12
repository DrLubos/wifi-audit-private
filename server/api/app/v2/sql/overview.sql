-- GET /api/v2/overview/status and /api/v2/overview/period (Main board).
-- The period statements take the buckets as two arrays of UTC bucket starts
-- and ends (app/v2/periods.py); a time's bucket number (from 1) is
-- width_bucket(t, starts) - a binary search, no join - for t in [from, to).

-- name: status
-- Open events, compliance of the managed networks, the open events that need
-- attention. Compliance: the BSSIDs that broadcast a managed network (by SSID,
-- or the name seen for a hidden AP), seen by the selected sensors in the 24 h
-- before their latest capture and classified managed or unknown (a suspicious
-- BSSID is the rogue banner's, not the network's); each must meet the
-- network's policy (security_meets()). A BSSID of unknown security (no beacon
-- record) is not assessed: counted in not_assessed, never failing (decided
-- 2026-10-12); bssids counts the assessed ones.
WITH ev AS (
  SELECT d.severity FROM detections d
  WHERE d.sensor_id = ANY (%(sensor_ids)s::int[]) AND d.status = 'open'
), mn AS (
  SELECT n.id, n.ssid, n.expected_security, n.require_mfp
  FROM networks n WHERE n.account_id = %(account_id)s AND n.kind = 'managed'
), cand AS (
  SELECT DISTINCT x.ap_id
  FROM ap_summary s
  JOIN ap_sensors x ON x.sensor_id = s.sensor_id AND x.device_key = s.device_key
  JOIN mn ON mn.ssid = coalesce(nullif(s.ssid, ''), s.name_seen)
  WHERE s.sensor_id = ANY (%(sensor_ids)s::int[])
    AND s.last_seen >= %(latest)s::timestamptz - interval '24 hours'
), bss AS MATERIALIZED (
  SELECT mn.id AS network_id, mn.expected_security, mn.require_mfp, v.bssid, v.crypt, v.crypt_bits_known, v.mfp_req,
         coalesce(v.security, 'unknown') = 'unknown' AS unassessed
  FROM account_aps v
  JOIN mn ON mn.ssid = v.network_name
  WHERE v.ap_id = ANY (ARRAY(SELECT ap_id FROM cand))     -- each AP's view row once
    AND v.classification IN ('managed', 'unknown')
), verdict AS MATERIALIZED (   -- security_meets() once per policy and configuration (~1 ms a call on the e2-micro)
  SELECT c.*, security_meets(c.expected_security, c.require_mfp, c.crypt, c.crypt_bits_known, c.mfp_req) AS reason
  FROM (SELECT DISTINCT expected_security, require_mfp, crypt, crypt_bits_known, mfp_req FROM bss) c
), net AS (
  SELECT mn.ssid, count(b.bssid) FILTER (WHERE NOT b.unassessed)::int AS bssids,
         count(b.bssid) FILTER (WHERE b.unassessed)::int AS not_assessed,
         count(b.bssid) FILTER (WHERE b.reason IS NOT NULL AND NOT b.unassessed)::int AS failing,
         mode() WITHIN GROUP (ORDER BY b.reason) FILTER (WHERE b.reason IS NOT NULL AND NOT b.unassessed) AS reason
  FROM mn
  LEFT JOIN (SELECT bss.network_id, bss.bssid, bss.unassessed, v.reason
             FROM bss JOIN verdict v
               ON (v.expected_security, v.require_mfp, v.crypt, v.crypt_bits_known, v.mfp_req)
                  IS NOT DISTINCT FROM (bss.expected_security, bss.require_mfp, bss.crypt, bss.crypt_bits_known,
                                        bss.mfp_req)) b ON b.network_id = mn.id
  GROUP BY mn.ssid
)
SELECT (SELECT count(*) FROM ev)::int AS open_total,
       (SELECT coalesce(json_agg(json_build_object('severity', severity, 'n', n)), '[]'::json)
        FROM (SELECT severity, count(*)::int AS n FROM ev GROUP BY 1) t) AS open_by_severity,
       (SELECT coalesce(json_agg(json_build_object('network', ssid, 'bssids', bssids, 'failing', failing,
                                                   'reason', reason, 'not_assessed', not_assessed)
                                 ORDER BY ssid), '[]'::json)
        FROM net) AS networks

-- name: attention
-- Needs attention (decided 2026-10-12): ongoing sensor problems (an outage
-- or limited period of the selected sensors still going) first, then the
-- open events of severity medium or higher from the 7 days before the
-- selected sensors' latest capture, the most severe and newest first (the
-- page shows a few; total for "n more"; older ones: attention_older).
SELECT d.id, d.ts, d.ts_end, d.kind, d.type, d.severity, d.summary, d.ssid,
       CASE WHEN d.kind <> 'sensor' THEN upper(d.mac::text) END AS bssid, s.name AS sensor,
       (d.kind = 'sensor' AND d.ts_end IS NULL) AS ongoing,
       count(*) OVER ()::int AS total
FROM detections d
JOIN sensors s ON s.id = d.sensor_id
WHERE d.sensor_id = ANY (%(sensor_ids)s::int[]) AND d.status = 'open'
  AND ((d.kind = 'sensor' AND d.ts_end IS NULL)
       OR (d.severity IN ('critical', 'high', 'medium') AND d.ts >= %(latest)s::timestamptz - interval '7 days'))
ORDER BY (d.kind = 'sensor' AND d.ts_end IS NULL) DESC,
         CASE d.severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 ELSE 2 END, d.ts DESC, d.id DESC
LIMIT 20

-- name: attention_older
-- Open events of severity medium or higher older than the 7 days above.
SELECT count(*)::int AS n
FROM detections d
WHERE d.sensor_id = ANY (%(sensor_ids)s::int[]) AND d.status = 'open'
  AND d.severity IN ('critical', 'high', 'medium')
  AND d.ts < %(latest)s::timestamptz - interval '7 days'
  AND NOT (d.kind = 'sensor' AND d.ts_end IS NULL)

-- name: presence
-- Per bucket: distinct account APs seen by the selected sensors (fixed /
-- mobile, ap_is_mobile() as in account_aps) and those seen while their
-- security level was open; per period: APs seen, open APs, the security
-- distribution (each AP's level at its last hour in the period), new APs per
-- bucket (first observation by the selected sensors). AP-hours are reduced to
-- one row per bucket and AP device first (~11k of 43k rows at 30d); only the
-- few open security periods are joined back to their hours.
WITH ap AS MATERIALIZED (
  SELECT a.id,
         ap_is_mobile(a.account_id, bool_or(s.random_bssid), max(s.manuf), a.first_seen, a.last_seen,
                      %(account_latest)s::timestamptz) AS mobile,
         min(coalesce(s.first_obs, s.first_seen)) FILTER (WHERE s.sensor_id = ANY (%(sensor_ids)s::int[])) AS first_here,
         -- first seen in its sensor's warm-up (the 24 h after that sensor's
         -- first poll, decided 2026-10-12): not a new AP
         (array_agg(coalesce(s.first_obs, s.first_seen) < ss.first_poll + interval '24 hours'
                    ORDER BY coalesce(s.first_obs, s.first_seen))
            FILTER (WHERE s.sensor_id = ANY (%(sensor_ids)s::int[])))[1] AS warmup
  FROM aps a
  JOIN ap_sensors x ON x.ap_id = a.id
  JOIN ap_summary s ON s.sensor_id = x.sensor_id AND s.device_key = x.device_key
  LEFT JOIN sensor_summary ss ON ss.sensor_id = s.sensor_id
  WHERE a.account_id = %(account_id)s
  GROUP BY a.id
), seen AS MATERIALIZED (
  SELECT width_bucket(h.hour, %(starts)s::timestamptz[]) AS i, h.sensor_id, h.device_key, max(h.hour) AS last_hour
  FROM ap_rssi_hourly h
  WHERE h.sensor_id = ANY (%(sensor_ids)s::int[])
    AND h.hour >= %(from)s::timestamptz AND h.hour < %(to)s::timestamptz
  GROUP BY 1, h.sensor_id, h.device_key
), per_bucket AS (
  SELECT DISTINCT s.i, x.ap_id
  FROM seen s JOIN ap_sensors x ON x.sensor_id = s.sensor_id AND x.device_key = s.device_key
), open_seen AS (
  SELECT DISTINCT width_bucket(h.hour, %(starts)s::timestamptz[]) AS i, x.ap_id
  FROM ap_security_periods sp
  JOIN ap_rssi_hourly h ON h.sensor_id = sp.sensor_id AND h.device_key = sp.device_key
                       AND h.hour < sp.valid_to AND h.hour + interval '1 hour' > sp.valid_from
  JOIN ap_sensors x ON x.sensor_id = h.sensor_id AND x.device_key = h.device_key
  WHERE sp.level = 'open' AND sp.sensor_id = ANY (%(sensor_ids)s::int[])
    AND h.hour >= %(from)s::timestamptz AND h.hour < %(to)s::timestamptz
), last_level AS (
  SELECT DISTINCT ON (x.ap_id) x.ap_id, coalesce(sp.level, 'unknown') AS level
  FROM (SELECT sensor_id, device_key, max(last_hour) AS h FROM seen GROUP BY 1, 2) s
  JOIN ap_sensors x ON x.sensor_id = s.sensor_id AND x.device_key = s.device_key
  LEFT JOIN ap_security_periods sp
         ON sp.sensor_id = s.sensor_id AND sp.device_key = s.device_key
        AND sp.valid_from < s.h + interval '1 hour' AND sp.valid_to > s.h
  ORDER BY x.ap_id, s.h DESC, sp.valid_from DESC
)
SELECT (SELECT coalesce(json_agg(json_build_object('i', t.i, 'fixed', t.fixed, 'mobile', t.mobile,
                                                   'open', coalesce(o.n, 0)) ORDER BY t.i), '[]'::json)
        FROM (SELECT p.i, count(*) FILTER (WHERE NOT ap.mobile)::int AS fixed,
                     count(*) FILTER (WHERE ap.mobile)::int AS mobile
              FROM per_bucket p JOIN ap ON ap.id = p.ap_id
              GROUP BY p.i) t
        LEFT JOIN (SELECT i, count(*)::int AS n FROM open_seen GROUP BY i) o ON o.i = t.i) AS buckets,
       (SELECT count(*) FROM last_level)::int AS aps_seen,
       (SELECT count(DISTINCT ap_id) FROM open_seen)::int AS open_seen,
       (SELECT coalesce(json_agg(json_build_object('level', level, 'n', n)), '[]'::json)
        FROM (SELECT level, count(*)::int AS n FROM last_level GROUP BY level) t) AS security,
       (SELECT coalesce(json_agg(json_build_object('i', t.i, 'new', t.n, 'mobile', t.m) ORDER BY t.i), '[]'::json)
        FROM (SELECT width_bucket(ap.first_here, %(starts)s::timestamptz[]) AS i, count(*)::int AS n,
                     count(*) FILTER (WHERE ap.mobile)::int AS m
              FROM ap
              WHERE ap.first_here >= %(from)s::timestamptz AND ap.first_here < %(to)s::timestamptz
                AND NOT coalesce(ap.warmup, false)
              GROUP BY 1) t) AS new_aps,
       (SELECT count(*) FROM ap
        WHERE ap.first_here >= %(from)s::timestamptz AND ap.first_here < %(to)s::timestamptz
          AND ap.warmup)::int AS new_warmup

-- name: clients
-- Clients on managed APs per UTC hour (SPEC section 4): per AP and hour the
-- AP-reported station count when a beacon carried a BSS Load element, else
-- Kismet's client count; max over the hour's polls, channels and sensors;
-- summed over the APs whose effective classification is managed. One row per
-- hour with any load row of the selected sensors (clients NULL: load
-- recorded, no managed AP in it); hours without a row had no load recorded.
WITH cand AS (
  SELECT DISTINCT x.ap_id
  FROM ap_summary s
  JOIN ap_sensors x ON x.sensor_id = s.sensor_id AND x.device_key = s.device_key
  JOIN networks n ON n.account_id = %(account_id)s AND n.kind = 'managed'
                 AND n.ssid = coalesce(nullif(s.ssid, ''), s.name_seen)
  UNION
  SELECT a.id FROM aps a WHERE a.account_id = %(account_id)s AND a.classification = 'managed'
), mg AS (
  SELECT v.ap_id FROM account_aps v
  WHERE v.ap_id = ANY (ARRAY(SELECT ap_id FROM cand)) AND v.classification = 'managed'
), per_ap AS (
  SELECT l.hour, x.ap_id,
         CASE WHEN bool_or(l.qbss_polls > 0) THEN max(l.qbss_max) FILTER (WHERE l.qbss_polls > 0)
              ELSE max(l.clients_max) END AS clients
  FROM ap_load_hourly l
  JOIN ap_sensors x ON x.sensor_id = l.sensor_id AND x.device_key = l.device_key
  JOIN mg ON mg.ap_id = x.ap_id
  WHERE l.sensor_id = ANY (%(sensor_ids)s::int[])
    AND l.hour >= %(from)s::timestamptz AND l.hour < %(to)s::timestamptz
  GROUP BY l.hour, x.ap_id
)
SELECT lh.hour, m.clients::int AS clients
FROM (SELECT DISTINCT l.hour FROM ap_load_hourly l
      WHERE l.sensor_id = ANY (%(sensor_ids)s::int[])
        AND l.hour >= %(from)s::timestamptz AND l.hour < %(to)s::timestamptz) lh
LEFT JOIN (SELECT hour, sum(clients) AS clients FROM per_ap GROUP BY hour) m ON m.hour = lh.hour
ORDER BY lh.hour

-- name: events
-- Events of the selected sensors per bucket, the most severe first (the
-- period chart's event dots and tooltips).
SELECT width_bucket(d.ts, %(starts)s::timestamptz[]) AS i, d.id, d.severity, d.kind, d.summary
FROM detections d
WHERE d.sensor_id = ANY (%(sensor_ids)s::int[])
  AND d.ts >= %(from)s::timestamptz AND d.ts < %(to)s::timestamptz
ORDER BY 1, CASE d.severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2
                              WHEN 'low' THEN 3 ELSE 4 END, d.ts DESC, d.id DESC
