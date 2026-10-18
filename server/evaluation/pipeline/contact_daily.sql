-- Contact availability per UTC day: observed time (from the first heartbeat on,
-- up to now) minus the contact gaps, over the observed time.
COPY (
WITH first AS (
  SELECT min(h.received_at) AS t0 FROM sensor_heartbeats h JOIN sensors s ON s.id = h.sensor_id
  WHERE s.name = :'sensor'),
h AS (
  SELECT h.received_at AS prev, lead(h.received_at) OVER (ORDER BY h.received_at) AS next
  FROM sensor_heartbeats h JOIN sensors s ON s.id = h.sensor_id WHERE s.name = :'sensor'),
gaps AS (
  SELECT prev AS g0, coalesce(next, now()) AS g1 FROM h
  WHERE coalesce(next, now()) - prev > make_interval(secs => :'gap_s'::int)),
days AS (
  SELECT d AS d0, d + interval '1 day' AS d1
  FROM generate_series(date_trunc('day', :'from'::timestamptz), :'to'::timestamptz - interval '1 second', interval '1 day') d),
obs AS (
  SELECT d0, d1, greatest(d0, :'from'::timestamptz, (SELECT t0 FROM first)) AS o0,
         least(d1, :'to'::timestamptz, now()) AS o1
  FROM days)
SELECT o.d0::date AS day,
       round(greatest(extract(epoch FROM o.o1 - o.o0), 0)::numeric, 0) AS observed_s,
       -- FILTER: without a gap the LEFT JOIN row is NULLs, and least()/greatest() skip NULLs
       round(coalesce(sum(greatest(extract(epoch FROM least(g.g1, o.o1) - greatest(g.g0, o.o0)), 0))
                      FILTER (WHERE g.g0 IS NOT NULL), 0)::numeric, 0) AS no_contact_s,
       CASE WHEN o.o1 > o.o0 THEN round(1 - coalesce(sum(greatest(extract(epoch FROM least(g.g1, o.o1) - greatest(g.g0, o.o0)), 0))
                                                     FILTER (WHERE g.g0 IS NOT NULL), 0)
                                        / extract(epoch FROM o.o1 - o.o0), 6) END AS availability,
       (SELECT count(*) FROM sensor_heartbeats x JOIN sensors s ON s.id = x.sensor_id
        WHERE s.name = :'sensor' AND x.received_at >= o.o0 AND x.received_at < o.o1) AS heartbeats
FROM obs o LEFT JOIN gaps g ON g.g1 > o.o0 AND g.g0 < o.o1
GROUP BY o.d0, o.o0, o.o1
ORDER BY o.d0
) TO STDOUT WITH (FORMAT csv, HEADER);
