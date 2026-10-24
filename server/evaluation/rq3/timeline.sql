-- RQ3: the collecting polls (ok, datasource running, devices > 0) - the
-- times a simulated audit can start. psql variables: sensor, from, to. Read-only.
COPY (
SELECT extract(epoch FROM p.ts)::bigint AS ts
FROM polls p JOIN sensors s ON s.id = p.sensor_id
WHERE s.name = :'sensor' AND p.ts >= :'from'::timestamptz AND p.ts < :'to'::timestamptz
  AND p.ok AND p.ds_running AND p.devices_active > 0
ORDER BY p.ts
) TO STDOUT WITH (FORMAT csv, HEADER);
