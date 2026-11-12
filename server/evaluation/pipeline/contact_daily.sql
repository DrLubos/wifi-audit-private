-- Contact availability per UTC day: observed time (from the first heartbeat on,
-- up to now) minus the contact gaps, over the observed time. Discard mode
-- (schema 20): the excluded windows are neither observed nor without contact
-- (excluded_s = their part of the day).
COPY (
WITH ex AS (
  SELECT x.excl_from AS e0, coalesce(x.excl_to, now()) AS e1
  FROM sensors s CROSS JOIN LATERAL sensor_excluded(s.id) x WHERE s.name = :'sensor'),
first AS (
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
  FROM days),
per AS (
  SELECT o.d0, o.o0, o.o1,
         greatest(extract(epoch FROM o.o1 - o.o0), 0) AS span_s,
         -- FILTER: without a gap the LEFT JOIN row is NULLs, and least()/greatest() skip NULLs
         coalesce(sum(greatest(extract(epoch FROM least(g.g1, o.o1) - greatest(g.g0, o.o0)), 0)
                      - (SELECT coalesce(sum(greatest(extract(epoch FROM least(ex.e1, g.g1, o.o1)
                                                               - greatest(ex.e0, g.g0, o.o0)), 0)), 0) FROM ex))
                  FILTER (WHERE g.g0 IS NOT NULL), 0) AS no_contact_s,
         (SELECT coalesce(sum(greatest(extract(epoch FROM least(ex.e1, o.o1) - greatest(ex.e0, o.o0)), 0)), 0)
          FROM ex) AS excluded_s
  FROM obs o LEFT JOIN gaps g ON g.g1 > o.o0 AND g.g0 < o.o1
  GROUP BY o.d0, o.o0, o.o1)
SELECT p.d0::date AS day,
       round(greatest(p.span_s - p.excluded_s, 0)::numeric, 0) AS observed_s,
       round(p.no_contact_s::numeric, 0) AS no_contact_s,
       CASE WHEN p.span_s - p.excluded_s > 0 THEN round(1 - p.no_contact_s::numeric / (p.span_s - p.excluded_s)::numeric, 6) END
         AS availability,
       (SELECT count(*) FROM sensor_heartbeats x JOIN sensors s ON s.id = x.sensor_id
        WHERE s.name = :'sensor' AND x.received_at >= p.o0 AND x.received_at < p.o1) AS heartbeats,
       round(p.excluded_s::numeric, 0) AS excluded_s
FROM per p
ORDER BY p.d0
) TO STDOUT WITH (FORMAT csv, HEADER);
