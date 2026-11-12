-- Contact gaps: consecutive heartbeats further apart than the threshold
-- (:'gap_s', default 180 s - the rule of refresh_contact_periods()), clipped to the range.
-- excluded_s: the part inside discard windows (schema 20) - not counted as
-- no contact in contact_daily.csv and summary.csv.
COPY (
WITH ex AS (
  SELECT x.excl_from AS e0, coalesce(x.excl_to, now()) AS e1
  FROM sensors s CROSS JOIN LATERAL sensor_excluded(s.id) x WHERE s.name = :'sensor'),
h AS (
  SELECT h.received_at AS prev, lead(h.received_at) OVER (ORDER BY h.received_at) AS next
  FROM sensor_heartbeats h JOIN sensors s ON s.id = h.sensor_id
  WHERE s.name = :'sensor'
    AND h.received_at >= :'from'::timestamptz - interval '1 day' AND h.received_at < :'to'::timestamptz + interval '1 day'),
g AS (
  SELECT greatest(prev, :'from'::timestamptz) AS g0, least(coalesce(next, now()), :'to'::timestamptz) AS g1,
         next IS NULL AS open
  FROM h
  WHERE coalesce(next, now()) - prev > make_interval(secs => :'gap_s'::int)
    AND coalesce(next, now()) > :'from'::timestamptz AND prev < :'to'::timestamptz)
SELECT g0 AS no_contact_from, g1 AS no_contact_to,
       round(extract(epoch FROM g1 - g0)::numeric, 1) AS seconds,
       open,
       round((SELECT coalesce(sum(greatest(extract(epoch FROM least(ex.e1, g1) - greatest(ex.e0, g0)), 0)), 0)
              FROM ex)::numeric, 1) AS excluded_s
FROM g
ORDER BY g0
) TO STDOUT WITH (FORMAT csv, HEADER);
