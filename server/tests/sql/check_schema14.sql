-- Read-only check of the schema-14 migration (live upload, step 1) and, once
-- the uploader runs, of the live data. Runs as claude_ro:
--
--   docker compose exec -T db sh -c 'psql -X -v ON_ERROR_STOP=1 -U claude_ro -d "$POSTGRES_DB"' \
--     < tests/sql/check_schema14.sql
--
-- Expected right after the migration: schema_version 14, the six tables
-- (empty), the functions, the kind constraint with 'lost' / 'unreachable',
-- the autovacuum settings, every AP still ever_ap (the trigger now reads
-- ap_evidence()), polls without seq until the next snapshot import (which
-- fills it) or the first upload. Later: the seq gaps, the upload latency per
-- day, heartbeat continuity, the clock corrections.
\set ON_ERROR_STOP on
\pset pager off

SELECT (SELECT value FROM schema_meta WHERE key = 'schema_version') AS schema_version,
       (SELECT pg_get_constraintdef(oid) LIKE '%unreachable%' FROM pg_constraint
         WHERE conname = 'sensor_annotations_kind_check') AS kind_constraint,
       (SELECT count(*) FROM pg_constraint WHERE conname = 'sensor_uploads_batch') AS batch_key,
       to_regprocedure('ingest_merge(integer, text)') IS NOT NULL AS ingest_merge,
       to_regprocedure('ingest_stage_create()') IS NOT NULL AS ingest_stage_create,
       to_regprocedure('refresh_contact_periods(integer, boolean)') IS NOT NULL AS contact_periods,
       to_regprocedure('ap_evidence(text, text, text, text)') IS NOT NULL AS ap_evidence;

SELECT t AS live_upload_table, to_regclass(t) IS NOT NULL AS present,
       (xpath('/row/c/text()', query_to_xml(format('SELECT count(*) AS c FROM %I', t), false, true, '')))[1]::text::int AS rows
FROM unnest(ARRAY['sensor_tokens', 'sensor_status', 'sensor_heartbeats', 'ingest_state',
                  'sensor_clock_boots', 'sensor_seq_gaps']) t;

SELECT relname, reloptions FROM pg_class
WHERE relname IN ('observations', 'devices', 'ap_load_hourly') AND relkind = 'r' ORDER BY relname;

-- the trigger's rule is unchanged: every device with AP evidence is ever_ap
SELECT count(*) FILTER (WHERE ever_ap) AS ever_ap,
       count(*) FILTER (WHERE ap_evidence(type, ssid, crypt, adv_channel) AND NOT ever_ap) AS evidence_but_not_ever_ap
FROM devices;

-- polls: buffer row ids, receive times, clock corrections
SELECT s.name AS sensor, count(*) AS polls, count(p.seq) AS with_seq, count(p.received_at) AS with_received_at,
       count(p.ts_offset_s) AS clock_corrected, min(p.ts) FILTER (WHERE p.seq IS NOT NULL) AS seq_from,
       max(p.ts) AS last_poll
FROM polls p JOIN sensors s ON s.id = p.sensor_id GROUP BY s.name ORDER BY s.name;

-- seq gaps among the polls that carry one (loss analysis): the collector writes
-- a row for every poll, failed ones too, so a jump in seq within one buffer is
-- polls that never reached the server (sensor_seq_gaps says why, once the
-- uploader runs; before it, a gap is a stretch no snapshot covered)
SELECT sensor_id, seq + 1 AS missing_from, next_seq - 1 AS missing_to, next_seq - seq - 1 AS polls,
       ts AS after_poll, next_ts AS before_poll
FROM (SELECT sensor_id, seq, ts, lead(seq) OVER w AS next_seq, lead(ts) OVER w AS next_ts
      FROM polls WHERE seq IS NOT NULL
      WINDOW w AS (PARTITION BY sensor_id ORDER BY seq)) x
WHERE next_seq > seq + 1
ORDER BY sensor_id, seq
LIMIT 50;

-- upload latency of live polls per day (synced clock only)
SELECT sensor_id, date_trunc('day', ts) AS day, count(*) AS polls,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM received_at - ts))::numeric, 1) AS p50_s,
       round(percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM received_at - ts))::numeric, 1) AS p95_s,
       round(max(extract(epoch FROM received_at - ts))::numeric, 1) AS max_s
FROM polls p
WHERE ts_offset_s IS NULL AND EXISTS (SELECT 1 FROM sensor_uploads u WHERE u.sensor_id = p.sensor_id AND u.via = 'ingest')
  AND received_at > ts AND received_at - ts < interval '1 hour'
GROUP BY 1, 2 ORDER BY 1, 2 DESC LIMIT 14;

SELECT via, count(*) AS uploads, sum(replays) AS replays, max(received_at) AS last,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms) AS p50_merge_ms
FROM sensor_uploads GROUP BY via ORDER BY via;

SELECT * FROM sensor_status;
SELECT * FROM ingest_state;
SELECT * FROM sensor_clock_boots ORDER BY recorded_at DESC LIMIT 10;
SELECT * FROM sensor_seq_gaps ORDER BY recorded_at DESC LIMIT 10;
SELECT sensor_id, starts_at, ends_at, kind, label FROM sensor_annotations
WHERE kind IN ('lost', 'unreachable') ORDER BY starts_at DESC LIMIT 20;
