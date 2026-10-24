-- Dataset overview: Kismet alerts per header (whether each becomes an event:
-- kismet_alert_rules). psql variables: sensor, from, to. Read-only.
COPY (
SELECT a.header, count(*) AS alerts, coalesce(r.as_event, true) AS as_event,
       to_char(min(a.ts) AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS') AS first,
       to_char(max(a.ts) AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS') AS last
FROM alerts a JOIN sensors s ON s.id = a.sensor_id
LEFT JOIN kismet_alert_rules r ON r.header = a.header
WHERE s.name = :'sensor' AND a.ts >= :'from'::timestamptz AND a.ts < :'to'::timestamptz
GROUP BY a.header, r.as_event
ORDER BY count(*) DESC
) TO STDOUT WITH (FORMAT csv, HEADER);
