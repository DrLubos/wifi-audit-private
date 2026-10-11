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
                                 'buffer_version', u.buffer_version, 'polls_to', u.polls_to)
        FROM sensor_uploads u WHERE u.sensor_id = se.id ORDER BY u.received_at DESC LIMIT 1) AS last_upload,
       cr.crashes, cr.max_recovery_s,
       -- a capture that failed and was not re-opened (the 2026-10-06 adapter loss)
       (SELECT min(e.ts) FROM alerts e
        WHERE e.sensor_id = se.id AND e.header = 'SOURCEERROR'
          AND e.ts > coalesce((SELECT max(o.ts) FROM alerts o
                               WHERE o.sensor_id = se.id AND o.header = 'SOURCEOPEN'), '-infinity')) AS unrecovered_since,
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
-- source (SOURCEOPEN); recovery = from the first SOURCEERROR after the
-- previous SOURCEOPEN to this one
LEFT JOIN LATERAL (
  SELECT count(*)::int AS crashes, max(extract(epoch FROM o.ts - e.first))::int AS max_recovery_s
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
