-- Per live upload (one batch = one request that the server stored), by receive time.
COPY (
SELECT s.name AS sensor, u.received_at, u.buffer_id, u.batch_seq,
       u.polls_from, u.polls_to, coalesce((u.rows->>'polls')::int, 0) AS polls_inserted,
       u.attempt, u.attempt - 1 AS retries, u.replays,
       u.bytes, u.duration_ms AS merge_ms, u.sent_at,
       round(extract(epoch FROM u.received_at - u.sent_at)::numeric, 3) AS transfer_s,
       u.rows::text AS rows_inserted
FROM sensor_uploads u
JOIN sensors s ON s.id = u.sensor_id
WHERE s.name = :'sensor' AND u.via = 'ingest'
  AND u.received_at >= :'from'::timestamptz AND u.received_at < :'to'::timestamptz
ORDER BY u.received_at
) TO STDOUT WITH (FORMAT csv, HEADER);
