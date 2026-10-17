-- The SQL of the sensor ingest (app/ingest/routes.py, docs/live-upload.md 6).
-- Named statements as in app/v2/sql (app/v2/queries.py parses them). Writes,
-- so never part of a perf read group. The merge itself is ingest_merge()
-- (schema.sql), shared with the snapshot import.

-- name: token
SELECT t.id, t.sensor_id, t.token_hash, t.revoked_at, s.name AS sensor, s.account_id
FROM sensor_tokens t JOIN sensors s ON s.id = t.sensor_id
WHERE t.id = %(token_id)s

-- name: token_used
UPDATE sensor_tokens SET last_used_at = now()
WHERE id = %(token_id)s AND (last_used_at IS NULL OR last_used_at < now() - interval '1 minute')

-- name: try_lock
-- One batch of a sensor at a time (a second one is a 409); ingest_merge() also
-- takes its own lock, which a running snapshot import holds.
SELECT pg_try_advisory_xact_lock(hashtext('ingest_batch'), %(sensor_id)s) AS locked

-- name: status_get
SELECT * FROM sensor_status WHERE sensor_id = %(sensor_id)s FOR UPDATE

-- name: status_write
INSERT INTO sensor_status (sensor_id, last_contact_at, last_batch_at, last_batch_seq, buffer_id, heartbeat,
                           reject_seq, reject_since, reject_count, reject_reason, auth_fail_since,
                           auth_fail_count, updated_at)
VALUES (%(sensor_id)s, %(last_contact_at)s, %(last_batch_at)s, %(last_batch_seq)s, %(buffer_id)s::uuid,
        %(heartbeat)s, %(reject_seq)s, %(reject_since)s, %(reject_count)s, %(reject_reason)s,
        %(auth_fail_since)s, %(auth_fail_count)s, now())
ON CONFLICT (sensor_id) DO UPDATE SET
  last_contact_at = EXCLUDED.last_contact_at, last_batch_at = EXCLUDED.last_batch_at,
  last_batch_seq = EXCLUDED.last_batch_seq, buffer_id = EXCLUDED.buffer_id, heartbeat = EXCLUDED.heartbeat,
  reject_seq = EXCLUDED.reject_seq, reject_since = EXCLUDED.reject_since,
  reject_count = EXCLUDED.reject_count, reject_reason = EXCLUDED.reject_reason,
  auth_fail_since = EXCLUDED.auth_fail_since, auth_fail_count = EXCLUDED.auth_fail_count,
  updated_at = now()

-- name: stall_write
-- The stall fields only (a refused batch, a failed authentication): no
-- contact - only an authenticated, accepted request is contact. A sensor
-- that never had contact has no row, and its stall is not tracked.
UPDATE sensor_status SET reject_seq = %(reject_seq)s, reject_since = %(reject_since)s,
  reject_count = %(reject_count)s, reject_reason = %(reject_reason)s,
  auth_fail_since = %(auth_fail_since)s, auth_fail_count = %(auth_fail_count)s, updated_at = now()
WHERE sensor_id = %(sensor_id)s

-- name: stage_create
SELECT ingest_stage_create()

-- name: merge
SELECT ingest_merge(%(sensor_id)s, 'ingest') AS merged

-- name: upload
-- One row per batch; the same batch again (its acknowledgement was lost) is
-- the same row with replays + 1.
INSERT INTO sensor_uploads (sensor_id, via, buffer_version, polls_from, polls_to, buffer_bytes, rows,
                            buffer_id, batch_seq, sent_at, attempt, bytes, duration_ms)
VALUES (%(sensor_id)s, 'ingest', %(buffer_version)s, to_timestamp(%(polls_from)s::double precision),
        to_timestamp(%(polls_to)s::double precision), %(buffer_bytes)s, %(rows)s, %(buffer_id)s::uuid,
        %(batch_seq)s, %(sent_at)s, %(attempt)s, %(bytes)s, %(duration_ms)s)
ON CONFLICT (sensor_id, buffer_id, batch_seq) DO UPDATE SET replays = sensor_uploads.replays + 1
RETURNING (xmax::text <> '0') AS replay

-- name: boot_upsert
-- The clock corrections of a boot (docs/live-upload.md 8.3): the first anchor
-- stays; counts and offsets widen (the uploader sends running totals).
INSERT INTO sensor_clock_boots (sensor_id, boot_id, source, epoch_true, epoch_stale, step_mono,
                                polls_corrected, offset_min_s, offset_max_s)
VALUES (%(sensor_id)s, %(boot_id)s::uuid, %(source)s, %(epoch_true)s, %(epoch_stale)s, %(step_mono)s,
        %(polls_corrected)s, %(offset_min_s)s, %(offset_max_s)s)
ON CONFLICT (sensor_id, boot_id) DO UPDATE SET
  polls_corrected = greatest(sensor_clock_boots.polls_corrected, EXCLUDED.polls_corrected),
  offset_min_s = least(sensor_clock_boots.offset_min_s, EXCLUDED.offset_min_s),
  offset_max_s = greatest(sensor_clock_boots.offset_max_s, EXCLUDED.offset_max_s),
  epoch_stale = coalesce(sensor_clock_boots.epoch_stale, EXCLUDED.epoch_stale),
  step_mono = coalesce(sensor_clock_boots.step_mono, EXCLUDED.step_mono)

-- name: gap_insert
INSERT INTO sensor_seq_gaps (sensor_id, buffer_id, seq_from, seq_to, reason, polls, raw_from, raw_to)
VALUES (%(sensor_id)s, %(buffer_id)s::uuid, %(seq_from)s, %(seq_to)s, %(reason)s, %(polls)s, %(raw_from)s,
        %(raw_to)s)
ON CONFLICT DO NOTHING
RETURNING seq_from

-- name: lost_annotation
-- Polls pruned on the sensor before they were uploaded: a 'lost' entry from
-- the last poll before them (seq_from - 1, within the buffer's 14 days and
-- before the batch) to the first after them (in this batch, or unknown).
INSERT INTO sensor_annotations (sensor_id, starts_at, ends_at, kind, boundary, label, ref)
SELECT %(sensor_id)s, b.ts, %(after_ts)s::timestamptz, 'lost', false, %(label)s, 'sensor_seq_gaps'
FROM (SELECT p.ts FROM polls p
      WHERE p.sensor_id = %(sensor_id)s AND p.seq = %(seq_from)s - 1
        AND p.ts > now() - interval '20 days' AND p.ts < coalesce(%(after_ts)s::timestamptz, now())
      ORDER BY p.ts DESC LIMIT 1) b
ON CONFLICT DO NOTHING

-- name: event_open
-- A stall event (docs/live-upload.md 6.5): one open (ts_end NULL) per type and sensor.
INSERT INTO detections (sensor_id, kind, ts, type, severity, summary, evidence)
SELECT %(sensor_id)s, 'sensor', %(ts)s::timestamptz, %(type)s::text, 'high', %(summary)s::text, %(evidence)s::jsonb
WHERE NOT EXISTS (SELECT 1 FROM detections d
                  WHERE d.sensor_id = %(sensor_id)s AND d.kind = 'sensor' AND d.type = %(type)s
                    AND d.ts_end IS NULL)
ON CONFLICT DO NOTHING
RETURNING id

-- name: event_close
-- Closed by the request that shows the stall is over; the operator's label stays.
UPDATE detections SET ts_end = now(), updated_at = now()
WHERE sensor_id = %(sensor_id)s AND kind = 'sensor' AND type = ANY (%(types)s::text[]) AND ts_end IS NULL
RETURNING id, type

-- name: heartbeat_last
SELECT max(received_at) AS last FROM sensor_heartbeats WHERE sensor_id = %(sensor_id)s

-- name: heartbeat_insert
INSERT INTO sensor_heartbeats (sensor_id, received_at, sensor_ts, rtt_ms, clock_offset_s, chrony_synced,
                               chrony_offset_s, boot_id, uptime_s, last_poll_ts, backlog_polls,
                               backlog_oldest_ts, held_polls, unverified_polls, buffer_oldest_ts,
                               buffer_bytes, disk_free_bytes, uploader_version)
VALUES (%(sensor_id)s, %(received_at)s, %(sensor_ts)s, %(rtt_ms)s, %(clock_offset_s)s, %(chrony_synced)s,
        %(chrony_offset_s)s, %(boot_id)s::uuid, %(uptime_s)s, %(last_poll_ts)s, %(backlog_polls)s,
        %(backlog_oldest_ts)s, %(held_polls)s, %(unverified_polls)s, %(buffer_oldest_ts)s,
        %(buffer_bytes)s, %(disk_free_bytes)s, %(uploader_version)s)
ON CONFLICT DO NOTHING

-- name: adapter_get
SELECT usb_id, mac::text AS mac, name, chipset FROM sensor_adapters WHERE sensor_id = %(sensor_id)s

-- name: adapter_fill
-- D8: the identity the seeded record lacks, without a boundary; display
-- fields follow the report.
INSERT INTO sensor_adapters (sensor_id, name, usb_id, mac, chipset, driver, interface, reported_by, updated_at)
VALUES (%(sensor_id)s, %(name)s, %(usb_id)s, %(mac)s::macaddr, %(chipset)s, %(driver)s, %(interface)s,
        'sensor', now())
ON CONFLICT (sensor_id) DO UPDATE SET
  usb_id = coalesce(sensor_adapters.usb_id, EXCLUDED.usb_id),
  mac = coalesce(sensor_adapters.mac, EXCLUDED.mac),
  chipset = coalesce(EXCLUDED.chipset, sensor_adapters.chipset),
  driver = coalesce(EXCLUDED.driver, sensor_adapters.driver),
  interface = coalesce(EXCLUDED.interface, sensor_adapters.interface),
  reported_by = 'sensor', updated_at = now()

-- name: adapter_change
UPDATE sensor_adapters SET name = %(name)s, usb_id = %(usb_id)s, mac = %(mac)s::macaddr, chipset = %(chipset)s,
  driver = %(driver)s, interface = %(interface)s, reported_by = 'sensor', updated_at = now()
WHERE sensor_id = %(sensor_id)s

-- name: hardware_boundary
-- An adapter change is a data boundary (RSSI baselines start again, schema 13).
INSERT INTO sensor_annotations (sensor_id, starts_at, kind, boundary, label, ref)
VALUES (%(sensor_id)s, %(at)s, 'hardware_change', true, %(label)s, 'reported by the sensor')
ON CONFLICT DO NOTHING

-- name: audit
INSERT INTO audit_log (account_id, action, target, old, new)
VALUES (%(account_id)s, %(action)s, %(target)s, %(old)s, %(new)s)

-- name: write_version
UPDATE accounts SET write_version = write_version + 1 WHERE id = %(account_id)s

-- name: resume
-- Where a new uploader starts (D5): the server's newest poll of the sensor
-- (seq NULL for polls stored before schema 14 - the uploader then finds it by ts).
SELECT seq, extract(epoch FROM ts)::bigint AS ts FROM polls
WHERE sensor_id = %(sensor_id)s ORDER BY ts DESC LIMIT 1
