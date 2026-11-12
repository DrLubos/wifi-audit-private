-- Discard mode (schema 20, docs/discard-mode.md): the windows of the sensor
-- that overlap the range - when each was on, its excluded time (widened by a
-- discarded backlog older than its start), what it discarded (whole batches
-- answered without storing; late rows dropped by ingest_merge()) and the
-- operator's note. Every other file leaves this time out.
COPY (
SELECT s.name AS sensor, t.id AS window_id, t.started_at, t.until, t.ended_at, t.ended_by,
       i.excl_from, i.excl_to,
       round(extract(epoch FROM coalesce(i.excl_to, now()) - i.excl_from)::numeric, 0) AS excluded_s,
       t.batches, t.replays, t.polls, t.late_polls, t.unverified_polls, t.seq_from, t.seq_to, t.note
FROM discard_window_totals t
JOIN sensors s ON s.id = t.sensor_id
JOIN LATERAL discard_intervals(t.sensor_id) i ON i.window_id = t.id
WHERE s.name = :'sensor' AND i.excl_from < :'to'::timestamptz
  AND coalesce(i.excl_to, 'infinity') > :'from'::timestamptz
ORDER BY t.started_at
) TO STDOUT WITH (FORMAT csv, HEADER);
