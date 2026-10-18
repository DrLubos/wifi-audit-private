-- Contact gaps: consecutive heartbeats further apart than the threshold
-- (:'gap_s', default 180 s - the rule of refresh_contact_periods()), clipped to the range.
COPY (
WITH h AS (
  SELECT h.received_at AS prev, lead(h.received_at) OVER (ORDER BY h.received_at) AS next
  FROM sensor_heartbeats h JOIN sensors s ON s.id = h.sensor_id
  WHERE s.name = :'sensor'
    AND h.received_at >= :'from'::timestamptz - interval '1 day' AND h.received_at < :'to'::timestamptz + interval '1 day')
SELECT greatest(prev, :'from'::timestamptz) AS no_contact_from,
       least(coalesce(next, now()), :'to'::timestamptz) AS no_contact_to,
       round(extract(epoch FROM least(coalesce(next, now()), :'to'::timestamptz) - greatest(prev, :'from'::timestamptz))::numeric, 1) AS seconds,
       next IS NULL AS open
FROM h
WHERE coalesce(next, now()) - prev > make_interval(secs => :'gap_s'::int)
  AND coalesce(next, now()) > :'from'::timestamptz AND prev < :'to'::timestamptz
ORDER BY prev
) TO STDOUT WITH (FORMAT csv, HEADER);
