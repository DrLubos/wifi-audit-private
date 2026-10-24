-- Dataset overview: outages and limited-capture periods with their recorded
-- cause (sensor_annotations, as refresh_events() reads it). psql variables:
-- sensor, from, to. Read-only.
COPY (
SELECT sp.kind,
       to_char(sp.starts_at AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS') AS starts_at,
       to_char(sp.ends_at AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS') AS ends_at,
       round(extract(epoch FROM coalesce(sp.ends_at, now()) - sp.starts_at))::bigint AS duration_s,
       sp.polls,
       (SELECT string_agg(an.label, '; ' ORDER BY an.starts_at)
        FROM sensor_annotations an
        WHERE an.sensor_id = sp.sensor_id
          AND CASE sp.kind WHEN 'outage' THEN an.kind IN ('gap', 'no_frames') ELSE an.kind = 'coverage' END
          AND an.starts_at < coalesce(sp.ends_at, 'infinity')
          AND coalesce(an.ends_at, an.starts_at) > sp.starts_at) AS cause
FROM sensor_periods sp JOIN sensors s ON s.id = sp.sensor_id
WHERE s.name = :'sensor' AND sp.starts_at < :'to'::timestamptz
  AND coalesce(sp.ends_at, 'infinity') > :'from'::timestamptz
ORDER BY sp.starts_at
) TO STDOUT WITH (FORMAT csv, HEADER);
