-- Loss: polls of the buffer that never arrived. Two independent sources:
--   recorded  - sensor_seq_gaps (the uploader reports 'lost' / 'unverified' ranges)
--   seq_jump  - a jump in polls.seq between consecutive stored polls (seq = the
--               buffer's polls.id, consecutive on the sensor); a seq going down
--               is a new buffer ('buffer_change'), not a loss.
COPY (
WITH p AS (
  SELECT p.seq, p.ts, lag(p.seq) OVER w AS prev_seq, lag(p.ts) OVER w AS prev_ts
  FROM polls p JOIN sensors s ON s.id = p.sensor_id
  WHERE s.name = :'sensor' AND p.seq IS NOT NULL
    AND p.ts >= :'from'::timestamptz AND p.ts < :'to'::timestamptz
  WINDOW w AS (ORDER BY p.ts))
SELECT 'seq_jump' AS source, CASE WHEN seq < prev_seq THEN 'buffer_change' ELSE 'missing' END AS reason,
       prev_seq + 1 AS seq_from, seq - 1 AS seq_to,
       CASE WHEN seq < prev_seq THEN 0 ELSE seq - prev_seq - 1 END AS polls,
       prev_ts AS after_ts, ts AS before_ts, NULL::timestamptz AS recorded_at
FROM p WHERE prev_seq IS NOT NULL AND seq <> prev_seq + 1
UNION ALL
SELECT 'recorded', g.reason, g.seq_from, g.seq_to, g.polls, g.raw_from, g.raw_to, g.recorded_at
FROM sensor_seq_gaps g JOIN sensors s ON s.id = g.sensor_id
WHERE s.name = :'sensor' AND g.recorded_at >= :'from'::timestamptz AND g.recorded_at < :'to'::timestamptz
ORDER BY 6 NULLS LAST, 1
) TO STDOUT WITH (FORMAT csv, HEADER);
