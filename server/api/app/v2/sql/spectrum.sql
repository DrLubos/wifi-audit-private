-- GET /api/v2/spectrum and /api/v2/spectrum/utilization (Spectrum board).
-- Spectrum counts exclude mobile APs (hotspots). An AP's primary channel in
-- the period = the advertised channel with the most polls (ap_load_hourly);
-- an AP seen in the period without load rows there (the gap 2026-10-03 11:00
-- until collector v7) takes its current advertised channel. Utilization =
-- what the APs report in their BSS Load element: per AP and hour the mean
-- of its reports, per channel and hour the mean over its reporting APs.

-- name: _fixed
-- the account's fixed APs (not mobile, ap_is_mobile() as in account_aps) with
-- their current channel, HT mode and network
fixed AS MATERIALIZED (
  SELECT c.id, c.adv_channel, c.ht_mode, c.network_key
  FROM (SELECT DISTINCT ON (a.id) a.id, a.account_id, a.first_seen, a.last_seen, s.random_bssid, s.manuf,
               s.adv_channel, s.ht_mode,
               coalesce(nullif(s.ssid, ''), s.name_seen, 'hidden:' || upper(a.bssid::text)) AS network_key
        FROM aps a
        JOIN ap_sensors x ON x.ap_id = a.id
        JOIN ap_summary s ON s.sensor_id = x.sensor_id AND s.device_key = x.device_key
        WHERE a.account_id = %(account_id)s
        ORDER BY a.id, s.last_seen DESC, s.sensor_id) c
  WHERE NOT ap_is_mobile(c.account_id, c.random_bssid, c.manuf, c.first_seen, c.last_seen,
                         %(account_latest)s::timestamptz)
)

-- name: spectrum
-- One statement for the page (each scan once): per fixed AP seen by the
-- selected sensors in the period its primary channel, HT mode, network,
-- local days observed and channel changes (ap_config_changes) in the
-- period; per UTC hour and channel the utilization over the APs that
-- reported it. The api filters the band.
WITH {{_fixed}}, hl AS (                 -- the period's UTC hours -> local date
  SELECT g AS hour, (g AT TIME ZONE %(tz)s::text)::date AS day
  FROM generate_series(%(from)s::timestamptz, %(to)s::timestamptz - interval '1 hour', interval '1 hour') AS g
), days AS (
  SELECT d.ap_id, count(*)::int AS days
  FROM (SELECT DISTINCT x.ap_id, hl.day
        FROM ap_rssi_hourly h
        JOIN hl ON hl.hour = h.hour
        JOIN ap_sensors x ON x.sensor_id = h.sensor_id AND x.device_key = h.device_key
        JOIN fixed f ON f.id = x.ap_id
        WHERE h.sensor_id = ANY (%(sensor_ids)s::int[])
          AND h.hour >= %(from)s::timestamptz AND h.hour < %(to)s::timestamptz) d
  GROUP BY d.ap_id
), load AS MATERIALIZED (                -- per hour, channel and fixed AP (over its sensors)
  SELECT l.hour, l.channel, x.ap_id, sum(l.polls)::int AS polls,
         sum(l.util_sum) FILTER (WHERE l.qbss_polls > 0) / nullif(sum(l.qbss_polls), 0) AS util
  FROM ap_load_hourly l
  JOIN ap_sensors x ON x.sensor_id = l.sensor_id AND x.device_key = l.device_key
  JOIN fixed f ON f.id = x.ap_id
  WHERE l.sensor_id = ANY (%(sensor_ids)s::int[]) AND l.channel <> ''
    AND l.hour >= %(from)s::timestamptz AND l.hour < %(to)s::timestamptz
  GROUP BY l.hour, l.channel, x.ap_id
), prim AS (
  SELECT DISTINCT ON (ap_id) ap_id, channel
  FROM (SELECT ap_id, channel, sum(polls) AS polls FROM load GROUP BY ap_id, channel) t
  ORDER BY ap_id, polls DESC, channel
), changes AS (
  SELECT x.ap_id, count(DISTINCT c.ts)::int AS n
  FROM ap_config_changes c
  JOIN ap_sensors x ON x.sensor_id = c.sensor_id AND x.device_key = c.device_key
  WHERE c.field = 'adv_channel' AND c.sensor_id = ANY (%(sensor_ids)s::int[])
    AND c.ts >= %(from)s::timestamptz AND c.ts < %(to)s::timestamptz
  GROUP BY x.ap_id
)
SELECT (SELECT coalesce(json_agg(json_build_object(
                 'ap_id', d.ap_id, 'channel', coalesce(p.channel, f.adv_channel), 'current', p.channel IS NULL,
                 'ht_mode', f.ht_mode, 'network', f.network_key, 'days', d.days, 'changes', coalesce(c.n, 0))),
                 '[]'::json)
        FROM days d
        JOIN fixed f ON f.id = d.ap_id
        LEFT JOIN prim p ON p.ap_id = d.ap_id
        LEFT JOIN changes c ON c.ap_id = d.ap_id) AS aps,
       (SELECT coalesce(json_agg(json_build_object('hour', u.hour, 'channel', u.channel, 'util', round(u.util::numeric, 1),
                                                   'aps', u.aps) ORDER BY u.hour, u.channel), '[]'::json)
        FROM (SELECT hour, channel, avg(util) AS util, count(*)::int AS aps
              FROM load WHERE util IS NOT NULL GROUP BY hour, channel) u) AS util

-- name: utilization
-- Utilization per UTC hour and channel of the band: the mean over the fixed
-- APs that reported it (each AP: the mean of its reports over its sensors).
WITH {{_fixed}}, per_ap AS (
  SELECT l.hour, l.channel, x.ap_id, sum(l.util_sum) / sum(l.qbss_polls) AS util
  FROM ap_load_hourly l
  JOIN ap_sensors x ON x.sensor_id = l.sensor_id AND x.device_key = l.device_key
  JOIN fixed f ON f.id = x.ap_id
  WHERE l.sensor_id = ANY (%(sensor_ids)s::int[]) AND l.qbss_polls > 0
    AND l.hour >= %(from)s::timestamptz AND l.hour < %(to)s::timestamptz
    AND band_of_channel(l.channel) = %(band)s::text
  GROUP BY l.hour, l.channel, x.ap_id
)
SELECT hour, channel, avg(util)::real AS util, count(*)::int AS aps
FROM per_ap
GROUP BY hour, channel
ORDER BY hour, channel
