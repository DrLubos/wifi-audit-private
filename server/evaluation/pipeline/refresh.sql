-- Refresher steps from refresh_runs (schema 16): the sensor's own steps
-- (rollups, ap_baselines) and the all-sensor ones (contact_periods, round).
-- Same columns as the log fallback of export.sh (source 'db').
COPY (
SELECT to_char(r.started_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"') AS ts,
       r.step, s.name AS sensor,
       to_char(r.refresh_from AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS refresh_from,
       r.duration_s, r.ap_hours, r.sensor_hours, r.waited_s, r.baselines, r.gaps, r.next_in_s,
       r.ok, r.error, 'db' AS source
FROM refresh_runs r LEFT JOIN sensors s ON s.id = r.sensor_id
WHERE (s.name = :'sensor' OR r.sensor_id IS NULL)
  AND r.started_at >= :'from'::timestamptz AND r.started_at < :'to'::timestamptz
ORDER BY r.started_at, r.id
) TO STDOUT WITH (FORMAT csv);
