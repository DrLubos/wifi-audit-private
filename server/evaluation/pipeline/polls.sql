-- Per poll uploaded live: the upload latency and its parts (README.md).
-- psql variables: sensor, from, to (UTC). Read-only.
COPY (
SELECT s.name AS sensor, p.seq, p.ts, p.received_at,
       round(extract(epoch FROM p.received_at - p.ts)::numeric, 3) AS latency_s,
       round(extract(epoch FROM u.sent_at - p.ts)::numeric, 3)     AS queue_s,
       round(extract(epoch FROM p.received_at - u.sent_at)::numeric, 3) AS transfer_s,
       p.ts_offset_s,
       u.batch_seq,
       count(*) OVER (PARTITION BY u.id)                          AS batch_polls,
       CASE WHEN count(*) OVER (PARTITION BY u.id) = 1 THEN 'steady' ELSE 'backlog' END AS mode,
       u.attempt, u.attempt - 1 AS retries, u.replays,
       u.bytes AS batch_bytes, u.duration_ms AS merge_ms, u.sent_at, p.ok
FROM polls p
JOIN sensors s ON s.id = p.sensor_id
JOIN sensor_uploads u ON u.sensor_id = p.sensor_id AND u.received_at = p.received_at AND u.via = 'ingest'
WHERE s.name = :'sensor' AND p.ts >= :'from'::timestamptz AND p.ts < :'to'::timestamptz
ORDER BY p.ts
) TO STDOUT WITH (FORMAT csv, HEADER);
