-- GET /api/v2/sensors (sidebar, top bar, the sensor cards of Overview and
-- Sensors). Windows ("7 days", "24 h") end at the sensor's last poll: until
-- live ingest the data ends at the last import (SPEC Q7), and a window ending
-- now would read empty between imports.

-- name: sensors
SELECT se.id, se.name, se.location, se.hardware,
       ss.first_poll, ss.last_poll, ss.last_capture_at,
       ss.last_hop_n, ss.last_hop_visited, ss.last_hop_ok,
       -- poll interval: the median spacing of the last 120 polls
       (SELECT percentile_disc(0.5) WITHIN GROUP (ORDER BY d.s)
        FROM (SELECT extract(epoch FROM p.ts - lag(p.ts) OVER (ORDER BY p.ts)) AS s
              FROM (SELECT ts FROM polls WHERE sensor_id = se.id ORDER BY ts DESC LIMIT 121) p) d) AS poll_s,
       (SELECT json_build_object('at', u.received_at, 'via', u.via, 'buffer_bytes', u.buffer_bytes,
                                 'buffer_version', u.buffer_version, 'polls_from', u.polls_from,
                                 'polls_to', u.polls_to)
        FROM sensor_uploads u WHERE u.sensor_id = se.id ORDER BY u.received_at DESC LIMIT 1) AS last_upload,
       cr.crashes, cr.restarts, cr.within_60s, cr.max_recovery_s,
       -- not collecting since (the ongoing outage, e.g. the 2026-10-06 adapter
       -- loss); from sensor_periods - the same fact from the alerts walked back
       -- over 9k SOURCEERROR rows, 2.9 s with a cold cache
       (SELECT p.starts_at FROM sensor_periods p
        WHERE p.sensor_id = se.id AND p.kind = 'outage' AND p.ends_at IS NULL) AS outage_since,
       ap7.aps_7d, ap7.aps_only_here_7d,
       (SELECT json_build_object('version', c.version, 'plan', c.channel_plan, 'channels', c.channels,
                                 'n_channels', c.n_channels, 'dwell_s', c.dwell_s, 'poll_s', c.poll_s,
                                 'confirmed_at', c.confirmed_at)
        FROM sensor_config c WHERE c.sensor_id = se.id AND c.confirmed_at IS NOT NULL
        ORDER BY c.version DESC LIMIT 1) AS config,
       (SELECT json_build_object('version', c.version, 'plan', c.channel_plan, 'channels', c.channels,
                                 'n_channels', c.n_channels, 'dwell_s', c.dwell_s, 'poll_s', c.poll_s,
                                 'created_at', c.created_at)
        FROM sensor_config c WHERE c.sensor_id = se.id AND c.confirmed_at IS NULL
          AND c.version > coalesce((SELECT max(k.version) FROM sensor_config k
                                    WHERE k.sensor_id = se.id AND k.confirmed_at IS NOT NULL), 0)
        ORDER BY c.version DESC LIMIT 1) AS pending
FROM sensors se
LEFT JOIN sensor_summary ss ON ss.sensor_id = se.id
-- capture crashes in the 24 h before the last poll: Kismet re-opened the
-- source (SOURCEOPEN) after a SOURCEERROR; recovery = from the first
-- SOURCEERROR after the previous SOURCEOPEN to this one (the end of a long
-- outage counts too, hence within_60s). A re-open without an error before
-- it is a restart (Kismet started), not a crash.
LEFT JOIN LATERAL (
  SELECT count(*) FILTER (WHERE e.first IS NOT NULL)::int AS crashes,
         count(*) FILTER (WHERE e.first IS NULL)::int AS restarts,
         count(*) FILTER (WHERE o.ts - e.first <= interval '60 seconds')::int AS within_60s,
         max(extract(epoch FROM o.ts - e.first))::int AS max_recovery_s
  FROM alerts o
  CROSS JOIN LATERAL (
    SELECT min(e.ts) AS first FROM alerts e
    WHERE e.sensor_id = o.sensor_id AND e.header = 'SOURCEERROR' AND e.ts <= o.ts
      AND e.ts > coalesce((SELECT max(p.ts) FROM alerts p
                           WHERE p.sensor_id = o.sensor_id AND p.header = 'SOURCEOPEN' AND p.ts < o.ts),
                          '-infinity')) e
  WHERE o.sensor_id = se.id AND o.header = 'SOURCEOPEN'
    AND o.ts > ss.last_poll - interval '24 hours' AND o.ts <= ss.last_poll) cr ON true
-- account APs seen by this sensor in the 7 days before its last poll, and of
-- them those no other sensor of the account saw in the same window
LEFT JOIN LATERAL (
  SELECT count(DISTINCT x.ap_id)::int AS aps_7d,
         count(DISTINCT x.ap_id) FILTER (WHERE NOT EXISTS (
           SELECT 1 FROM ap_sensors y
           JOIN ap_rssi_hourly h2 ON h2.sensor_id = y.sensor_id AND h2.device_key = y.device_key
           WHERE y.ap_id = x.ap_id AND y.sensor_id <> se.id
             AND h2.hour >= ss.last_poll - interval '7 days'))::int AS aps_only_here_7d
  FROM ap_rssi_hourly h
  JOIN ap_sensors x ON x.sensor_id = h.sensor_id AND x.device_key = h.device_key
  WHERE h.sensor_id = se.id AND h.hour >= ss.last_poll - interval '7 days') ap7 ON true
WHERE se.account_id = %(account_id)s
ORDER BY se.id

-- name: sensor
-- One sensor of the account by name (the per-sensor endpoints).
SELECT se.id, se.name, ss.first_poll, ss.last_poll, ss.last_capture_at
FROM sensors se LEFT JOIN sensor_summary ss ON ss.sensor_id = se.id
WHERE se.account_id = %(account_id)s AND se.name = %(name)s

-- name: outages
-- Outages and limited periods of a sensor, newest first: the periods
-- derived from its polls (sensor_periods) with the documented cause of the
-- overlapping annotations (gap / no frames for an outage, coverage for a
-- limited period), plus the documented windows no derived period covers
-- (limited capture before buffer v4 had no hop flag) and restarts.
SELECT p.kind AS type, p.starts_at, p.ends_at, p.ends_at IS NULL AS ongoing,
       extract(epoch FROM coalesce(p.ends_at, ss.last_poll) - p.starts_at)::int AS duration_s, p.polls,
       'polls' AS source,
       -- each documented label once, in the order they start
       (SELECT string_agg(x.label, '; ' ORDER BY x.first)
        FROM (SELECT a.label, min(a.starts_at) AS first FROM sensor_annotations a
              WHERE a.sensor_id = p.sensor_id
                AND CASE p.kind WHEN 'outage' THEN a.kind IN ('gap', 'no_frames') ELSE a.kind = 'coverage' END
                AND a.starts_at < coalesce(p.ends_at, 'infinity') AND coalesce(a.ends_at, a.starts_at) > p.starts_at
              GROUP BY a.label) x) AS cause
FROM sensor_periods p
JOIN sensor_summary ss ON ss.sensor_id = p.sensor_id
WHERE p.sensor_id = %(sensor_id)s
UNION ALL
SELECT CASE a.kind WHEN 'restart' THEN 'restart' WHEN 'coverage' THEN 'limited' ELSE 'outage' END,
       a.starts_at, a.ends_at, false, extract(epoch FROM a.ends_at - a.starts_at)::int, NULL, 'annotation', a.label
FROM sensor_annotations a
WHERE a.sensor_id = %(sensor_id)s AND a.kind IN ('restart', 'gap', 'no_frames', 'coverage')
  AND (a.kind = 'restart' OR NOT EXISTS (
        SELECT 1 FROM sensor_periods p
        WHERE p.sensor_id = a.sensor_id
          AND CASE p.kind WHEN 'outage' THEN a.kind IN ('gap', 'no_frames') ELSE a.kind = 'coverage' END
          AND p.starts_at < coalesce(a.ends_at, a.starts_at) AND coalesce(p.ends_at, 'infinity') > a.starts_at))
ORDER BY 2 DESC

-- name: coverage_notes
-- Documented windows of limited capture (sensor_annotations, coverage) in
-- the given time range: the polls before the hop guard cannot show them.
SELECT a.starts_at, a.ends_at FROM sensor_annotations a
WHERE a.sensor_id = %(sensor_id)s AND a.kind = 'coverage' AND a.ends_at IS NOT NULL
  AND a.starts_at < %(to)s::timestamptz AND a.ends_at > %(from)s::timestamptz
