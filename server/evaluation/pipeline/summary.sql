-- One row per aggregate over the range (metric, value, unit, note); the
-- refresh rows are appended by export.sh from the refresher log. Discard mode
-- (schema 20): the excluded windows are left out - seq jumps they cover are
-- not loss (discarded_seqs()), their time is neither observed nor without
-- contact, their heartbeats are not in the clock figures.
COPY (
WITH ex AS (
  SELECT x.excl_from AS e0, coalesce(x.excl_to, now()) AS e1
  FROM sensors s CROSS JOIN LATERAL sensor_excluded(s.id) x WHERE s.name = :'sensor'),
p AS (
  SELECT p.seq, p.ts, extract(epoch FROM p.received_at - p.ts) AS lat, p.ts_offset_s,
         count(*) OVER (PARTITION BY u.id) AS bp, u.id AS uid
  FROM polls p JOIN sensors s ON s.id = p.sensor_id
  JOIN sensor_uploads u ON u.sensor_id = p.sensor_id AND u.received_at = p.received_at AND u.via = 'ingest'
  WHERE s.name = :'sensor' AND p.ts >= :'from'::timestamptz AND p.ts < :'to'::timestamptz),
u AS (
  SELECT u.* FROM sensor_uploads u JOIN sensors s ON s.id = u.sensor_id
  WHERE s.name = :'sensor' AND u.via = 'ingest'
    AND u.received_at >= :'from'::timestamptz AND u.received_at < :'to'::timestamptz),
q AS (
  SELECT p.seq, lag(p.seq) OVER (ORDER BY p.ts) AS prev, p.ts, lag(p.ts) OVER (ORDER BY p.ts) AS prev_ts, s.id AS sid
  FROM polls p JOIN sensors s ON s.id = p.sensor_id
  WHERE s.name = :'sensor' AND p.seq IS NOT NULL AND p.ts >= :'from'::timestamptz AND p.ts < :'to'::timestamptz),
qj AS (    -- the jumps, with the seqs a discarded batch covers
  SELECT seq - prev - 1 AS jump, discarded_seqs(sid, prev + 1, seq - 1, prev_ts, ts) AS excluded
  FROM q WHERE prev IS NOT NULL AND seq > prev + 1),
dz AS (
  SELECT count(*) FILTER (WHERE b.kind = 'batch') AS batches, coalesce(sum(b.polls), 0) AS polls,
         count(DISTINCT b.window_id) AS windows
  FROM sensor_discarded_batches b JOIN sensors s ON s.id = b.sensor_id
  WHERE s.name = :'sensor' AND b.received_at >= :'from'::timestamptz AND b.received_at < :'to'::timestamptz),
g AS (
  SELECT g.* FROM sensor_seq_gaps g JOIN sensors s ON s.id = g.sensor_id
  WHERE s.name = :'sensor' AND g.recorded_at >= :'from'::timestamptz AND g.recorded_at < :'to'::timestamptz),
g_sum AS (
  SELECT coalesce(sum(polls) FILTER (WHERE reason = 'lost'), 0) AS lost,
         coalesce(sum(polls) FILTER (WHERE reason = 'unverified'), 0) AS unverified FROM g),
h AS (
  SELECT h.* FROM sensor_heartbeats h JOIN sensors s ON s.id = h.sensor_id
  WHERE s.name = :'sensor' AND h.received_at >= :'from'::timestamptz AND h.received_at < :'to'::timestamptz
    AND NOT EXISTS (SELECT 1 FROM ex WHERE h.received_at >= ex.e0 AND h.received_at < ex.e1)),
hb AS (
  SELECT h.received_at AS prev, lead(h.received_at) OVER (ORDER BY h.received_at) AS next
  FROM sensor_heartbeats h JOIN sensors s ON s.id = h.sensor_id WHERE s.name = :'sensor'),
obs AS (
  SELECT greatest(:'from'::timestamptz, min(prev)) AS o0, least(:'to'::timestamptz, now()) AS o1 FROM hb),
obx AS (   -- excluded seconds of the observed time
  SELECT coalesce(sum(greatest(extract(epoch FROM least(ex.e1, obs.o1) - greatest(ex.e0, obs.o0)), 0)), 0) AS s
  FROM ex, obs),
nc AS (    -- contact gaps clipped to the observed time, minus their excluded part
  SELECT coalesce(sum(greatest(extract(epoch FROM least(coalesce(hb.next, now()), obs.o1) - greatest(hb.prev, obs.o0)), 0)
                      - xo.s), 0) AS s
  FROM hb CROSS JOIN obs
  CROSS JOIN LATERAL (
    SELECT coalesce(sum(greatest(extract(epoch FROM least(ex.e1, coalesce(hb.next, now()), obs.o1)
                                                     - greatest(ex.e0, hb.prev, obs.o0)), 0)), 0) AS s
    FROM ex) xo
  WHERE coalesce(hb.next, now()) - hb.prev > make_interval(secs => :'gap_s'::int)),
pct AS (
  SELECT percentile_cont(ARRAY[0.5, 0.95, 0.99]) WITHIN GROUP (ORDER BY lat) FILTER (WHERE bp = 1) AS steady,
         max(lat) FILTER (WHERE bp = 1) AS steady_max,
         count(*) FILTER (WHERE bp = 1) AS n_steady, count(*) FILTER (WHERE bp > 1) AS n_backlog,
         max(lat) FILTER (WHERE bp > 1) AS backlog_max, count(*) AS n,
         count(*) FILTER (WHERE ts_offset_s IS NOT NULL) AS n_corrected
  FROM p),
up AS (
  SELECT count(*) AS n, count(*) FILTER (WHERE attempt > 1) AS retried,
         coalesce(sum(attempt - 1), 0) AS retries, coalesce(sum(replays), 0) AS replays,
         percentile_cont(0.5) WITHIN GROUP (ORDER BY bytes) FILTER (WHERE (rows->>'polls')::int = 1) AS bytes_steady,
         percentile_cont(ARRAY[0.5, 0.95]) WITHIN GROUP (ORDER BY duration_ms) FILTER (WHERE (rows->>'polls')::int = 1) AS merge_steady
  FROM u),
loss AS (
  SELECT count(*) AS received,
         (SELECT coalesce(sum(jump - excluded), 0) FROM qj) AS missing,
         (SELECT coalesce(sum(excluded), 0) FROM qj) AS excluded,
         count(*) FILTER (WHERE prev IS NOT NULL AND seq < prev) AS buffer_changes
  FROM q),
clk AS (
  SELECT percentile_cont(ARRAY[0.5, 0.95]) WITHIN GROUP (ORDER BY clock_offset_s)
           FILTER (WHERE received_at >= '2026-10-17 21:32:30+00') AS off,
         percentile_cont(0.5) WITHIN GROUP (ORDER BY abs(chrony_offset_s)) AS chrony_abs,
         count(*) FILTER (WHERE NOT chrony_synced) AS unsynced, count(*) AS n
  FROM h)
SELECT metric, value, unit, note FROM pct, up, loss, g_sum, clk, obs, obx, nc, dz,
LATERAL (VALUES
  ('polls_uploaded', pct.n::numeric, 'polls', 'live ingest, poll ts in range'),
  ('polls_steady', pct.n_steady, 'polls', 'batch of one poll'),
  ('polls_backlog', pct.n_backlog, 'polls', 'batches of more than one poll (catch-up, retry)'),
  ('latency_steady_p50', round(pct.steady[1]::numeric, 3), 's', 'received_at - ts'),
  ('latency_steady_p95', round(pct.steady[2]::numeric, 3), 's', NULL),
  ('latency_steady_p99', round(pct.steady[3]::numeric, 3), 's', NULL),
  ('latency_steady_max', round(pct.steady_max::numeric, 3), 's', NULL),
  ('latency_backlog_max', round(pct.backlog_max::numeric, 1), 's', NULL),
  ('polls_clock_corrected', pct.n_corrected, 'polls', 'ts_offset_s set'),
  ('batches', up.n, 'batches', 'stored live uploads, received in range'),
  ('batches_retried', up.retried, 'batches', 'attempt > 1'),
  ('retries', up.retries, 'attempts', 'sum(attempt - 1)'),
  ('replays', up.replays, 'requests', 'a stored batch received again'),
  ('batch_bytes_steady_p50', round(up.bytes_steady::numeric, 0), 'bytes', 'gzip body'),
  ('merge_ms_steady_p50', round(up.merge_steady[1]::numeric, 0), 'ms', 'ingest_merge() on the server'),
  ('merge_ms_steady_p95', round(up.merge_steady[2]::numeric, 0), 'ms', NULL),
  ('seq_received', loss.received, 'polls', 'polls with a seq, ts in range'),
  ('seq_missing', loss.missing, 'polls', 'seq jumps, minus discarded seqs'),
  ('seq_excluded', loss.excluded, 'polls', 'seq jumps a discard window covers (not loss)'),
  ('seq_loss_rate', round(loss.missing::numeric / nullif(loss.received + loss.missing, 0), 6), 'ratio', NULL),
  ('buffer_changes', loss.buffer_changes, 'count', 'seq went down'),
  ('gaps_recorded_lost', g_sum.lost, 'polls', 'sensor_seq_gaps'),
  ('gaps_recorded_unverified', g_sum.unverified, 'polls', 'sensor_seq_gaps'),
  ('contact_observed_s', round(greatest(extract(epoch FROM obs.o1 - obs.o0) - obx.s, 0)::numeric, 0), 's',
   'from the first heartbeat, excluded windows left out'),
  ('contact_no_contact_s', round(nc.s::numeric, 0), 's', 'heartbeat gaps > gap_s, outside excluded windows'),
  ('contact_availability', CASE WHEN extract(epoch FROM obs.o1 - obs.o0) - obx.s > 0
                                THEN round(1 - nc.s::numeric / (extract(epoch FROM obs.o1 - obs.o0) - obx.s)::numeric, 6) END,
   'ratio', '1 - no_contact / observed; empty when nothing was observed'),
  ('excluded_s', round(obx.s::numeric, 0), 's', 'discard mode: excluded time in the observed range'),
  ('excluded_windows', dz.windows, 'count', 'discard windows that discarded a batch received in range'),
  ('batches_discarded', dz.batches, 'batches', 'answered 200, not stored (discard mode)'),
  ('polls_discarded', dz.polls, 'polls', 'whole batches and late rows inside a window'),
  ('clock_offset_p50', round(clk.off[1]::numeric, 3), 's', 'recv - send since 2026-10-17 21:32:30'),
  ('clock_offset_p95', round(clk.off[2]::numeric, 3), 's', NULL),
  ('chrony_abs_offset_p50', round(clk.chrony_abs::numeric, 6), 's', 'the Pi''s own NTP offset'),
  ('heartbeats', clk.n, 'count', 'stored, at most one a minute; outside excluded windows'),
  ('heartbeats_unsynced', clk.unsynced, 'count', 'chrony not synchronised')
) v(metric, value, unit, note)
) TO STDOUT WITH (FORMAT csv, HEADER);
