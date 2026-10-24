-- Dataset overview: the configuration history - dataset boundaries and the
-- dated changes (sensor_annotations), as the Sensors page shows them.
-- psql variables: sensor, from, to. Read-only.
COPY (
SELECT to_char(an.starts_at AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS') AS starts_at,
       to_char(an.ends_at AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS') AS ends_at,
       an.kind, an.boundary, an.label, an.ref
FROM sensor_annotations an JOIN sensors s ON s.id = an.sensor_id
WHERE s.name = :'sensor' AND (an.boundary OR an.kind IN ('change', 'hardware_change', 'coverage'))
  AND an.starts_at < :'to'::timestamptz AND coalesce(an.ends_at, an.starts_at) >= :'from'::timestamptz
ORDER BY an.starts_at
) TO STDOUT WITH (FORMAT csv, HEADER);
