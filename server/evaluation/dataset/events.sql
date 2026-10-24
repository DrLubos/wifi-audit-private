-- Dataset overview: events per kind, type and severity, with the label
-- status (totals: type '*'). psql variables: sensor, from, to. Read-only.
COPY (
SELECT d.kind, coalesce(d.type, '*') AS type, coalesce(d.severity, '*') AS severity,
       count(*) AS events,
       count(*) FILTER (WHERE d.status = 'open') AS open,
       count(*) FILTER (WHERE d.status = 'confirmed') AS confirmed,
       count(*) FILTER (WHERE d.status = 'benign') AS benign,
       count(*) FILTER (WHERE d.status = 'false_positive') AS false_positive
FROM detections d JOIN sensors s ON s.id = d.sensor_id
WHERE s.name = :'sensor' AND d.ts >= :'from'::timestamptz AND d.ts < :'to'::timestamptz
GROUP BY GROUPING SETS ((d.kind, d.type, d.severity), (d.kind))
ORDER BY d.kind, d.type NULLS FIRST, d.severity
) TO STDOUT WITH (FORMAT csv, HEADER);
