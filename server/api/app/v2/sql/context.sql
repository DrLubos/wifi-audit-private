-- The context of every v2 request (app/v2/deps.py): the account with the
-- versions its ETags are made of, its sensors, and the capture time per sensor
-- and hour that the period endpoints turn into coverage (app/v2/shape.py).

-- name: account
-- refreshed_at: the newest refresh_rollups() of its sensors; events_at: the
-- newest change of an event row (detectors, refresh_events(), status writes).
SELECT a.id, a.name, a.tz, a.write_version,
       st.sensor_offline_min AS offline_min, st.ignore_random_bssid,
       (SELECT max(ss.refreshed_at) FROM sensor_summary ss JOIN sensors s ON s.id = ss.sensor_id
        WHERE s.account_id = a.id) AS refreshed_at,
       (SELECT max(d.updated_at) FROM detections d JOIN sensors s ON s.id = d.sensor_id
        WHERE s.account_id = a.id) AS events_at,
       (SELECT coalesce(json_agg(json_build_object(
                 'id', s.id, 'name', s.name, 'first_poll', ss.first_poll, 'last_poll', ss.last_poll,
                 'last_capture_at', ss.last_capture_at) ORDER BY s.id), '[]'::json)
        FROM sensors s LEFT JOIN sensor_summary ss ON ss.sensor_id = s.id
        WHERE s.account_id = a.id) AS sensors
FROM accounts a
LEFT JOIN account_settings st ON st.account_id = a.id
WHERE a.id = %(account_id)s

-- name: sensor_hours
-- Poll health per selected sensor and UTC hour of the period (sensor_hourly):
-- capture seconds = (3600 - gap_s) x capture_polls / polls (PLAN.md 7.2).
SELECT h.sensor_id, h.hour, h.polls, h.capture_polls, h.limited_polls, h.gap_s
FROM sensor_hourly h
WHERE h.sensor_id = ANY (%(sensor_ids)s::int[])
  AND h.hour >= %(from)s::timestamptz AND h.hour < %(to)s::timestamptz
ORDER BY h.hour, h.sensor_id
