-- Server schema 19 -> 20 (schema.sql, schema 20): discard mode
-- (docs/discard-mode.md) - the tables sensor_discard_windows and
-- sensor_discarded_batches, the annotation kind 'excluded', the functions
-- (discard_*, sensor_excluded(), discarded_seqs(), ingest_resume()), the view
-- discard_window_totals, refresh_sensor_periods() with the excluded windows
-- and ingest_merge() step 0 (the text of schema.sql; tests/test_schema_files.py).
-- No existing row changes. ONE transaction: any error leaves schema 19. Run through ops/migrate_schema20.sh; deploy the
-- api, the refresher and the frontend right afterwards.
\set ON_ERROR_STOP on
\pset pager off

BEGIN;
SET LOCAL lock_timeout = '10s';
DO $$
BEGIN
  IF (SELECT value FROM schema_meta WHERE key = 'schema_version') IS DISTINCT FROM '19' THEN
    RAISE EXCEPTION 'migrate_schema20: expected schema_version 19';
  END IF;
END $$;

\echo [1/4] sensor_annotations: the kind 'excluded'
ALTER TABLE sensor_annotations DROP CONSTRAINT sensor_annotations_kind_check;
ALTER TABLE sensor_annotations ADD CONSTRAINT sensor_annotations_kind_check
  CHECK (kind IN ('gap', 'no_frames', 'coverage', 'restart', 'change', 'hardware_change', 'lost', 'unreachable',
                  'excluded'));

\echo [2/4] sensor_discard_windows, sensor_discarded_batches
CREATE TABLE sensor_discard_windows (
  id          serial PRIMARY KEY,
  sensor_id   integer NOT NULL REFERENCES sensors(id),
  started_at  timestamptz NOT NULL DEFAULT now(),
  until       timestamptz NOT NULL,              -- the end set when it was turned on (switches itself off)
  ended_at    timestamptz,                       -- when it ended: NULL while on (or expired, not recorded yet)
  ended_by    text CHECK (ended_by IN ('operator', 'expiry')),
  note        text NOT NULL,                     -- the operator's note (what the test is)
  CONSTRAINT sensor_discard_windows_until CHECK (until > started_at AND until <= started_at + interval '24 hours'),
  CONSTRAINT sensor_discard_windows_ended CHECK ((ended_at IS NULL) = (ended_by IS NULL)
                                                 AND (ended_at IS NULL OR ended_at >= started_at)),
  CONSTRAINT sensor_discard_windows_note CHECK (btrim(note) <> '' AND char_length(note) <= 500));
-- one window on per sensor (an expired one not recorded yet still counts: discard_start() closes it first)
CREATE UNIQUE INDEX sensor_discard_windows_one_open
  ON sensor_discard_windows (sensor_id) WHERE ended_at IS NULL;

-- What a window discarded (schema 20): one row per batch the ingest answered
-- without storing ('batch'; a batch sent again is the same row, replays + 1),
-- and per later batch or snapshot import whose rows inside an excluded window
-- ingest_merge() dropped ('late'). seq_* are the buffer's poll ids of the
-- discarded polls - with the polls the uploader could not place in time
-- (unverified) - for the loss rule (discarded_seqs()); ts_* their times (the
-- receive time for a batch without polls); rows = rows per table.
CREATE TABLE sensor_discarded_batches (
  id               bigserial PRIMARY KEY,
  sensor_id        integer NOT NULL REFERENCES sensors(id),
  window_id        integer NOT NULL REFERENCES sensor_discard_windows(id),
  kind             text NOT NULL CHECK (kind IN ('batch', 'late')),
  received_at      timestamptz NOT NULL DEFAULT now(),
  buffer_id        uuid,                         -- NULL for a snapshot import
  batch_seq        bigint,
  polls            integer NOT NULL DEFAULT 0,
  seq_from         bigint,
  seq_to           bigint,
  ts_from          timestamptz,
  ts_to            timestamptz,
  unverified_polls integer NOT NULL DEFAULT 0,   -- of the seq range: reported unverified, not sent
  rows             jsonb NOT NULL DEFAULT '{}',
  bytes            integer,
  replays          integer NOT NULL DEFAULT 0,
  CONSTRAINT sensor_discarded_batches_seq CHECK ((seq_from IS NULL) = (seq_to IS NULL) AND seq_from <= seq_to));
CREATE UNIQUE INDEX sensor_discarded_batches_batch
  ON sensor_discarded_batches (sensor_id, buffer_id, batch_seq, kind, window_id);
CREATE INDEX sensor_discarded_batches_window ON sensor_discarded_batches (window_id);

\echo [3/4] the discard functions, refresh_sensor_periods() with the excluded windows, ingest_merge() step 0
-- The window of a sensor that is on now, or NULL: on from started_at until
-- `until` - exactly, whether or not the refresher has recorded the end yet.
CREATE OR REPLACE FUNCTION discard_active(p_sensor_id integer)
RETURNS integer
LANGUAGE sql STABLE AS $$
  SELECT w.id FROM sensor_discard_windows w
  WHERE w.sensor_id = p_sensor_id AND w.ended_at IS NULL AND w.until > now()
$$;

-- Per window its excluded time [excl_from, excl_to): from the earlier of its
-- start and its first discarded poll (a backlog older than the start arrived
-- while it was on) to the later of its end and its last discarded poll;
-- excl_to NULL = still on. The end is ended_at, or `until` once that has
-- passed (expired, not recorded yet).
CREATE OR REPLACE FUNCTION discard_intervals(p_sensor_id integer)
RETURNS TABLE (window_id integer, excl_from timestamptz, excl_to timestamptz)
LANGUAGE sql STABLE AS $$
  SELECT w.id, least(w.started_at, b.ts_from),
         CASE WHEN w.ended_at IS NULL AND w.until > now() THEN NULL
              ELSE greatest(coalesce(w.ended_at, w.until), b.ts_to + interval '1 second') END
  FROM sensor_discard_windows w
  LEFT JOIN LATERAL (SELECT min(x.ts_from) AS ts_from, max(x.ts_to) AS ts_to
                     FROM sensor_discarded_batches x
                     WHERE x.window_id = w.id AND x.kind = 'batch') b ON true
  WHERE w.sensor_id = p_sensor_id
$$;

-- The excluded time of a sensor as disjoint intervals [excl_from, excl_to),
-- oldest first: overlapping and abutting windows merged, excl_to NULL = still
-- on. What refresh_sensor_periods(), the Sensors page's coverage and the
-- evaluation exports leave out.
CREATE OR REPLACE FUNCTION sensor_excluded(p_sensor_id integer)
RETURNS TABLE (excl_from timestamptz, excl_to timestamptz)
LANGUAGE sql STABLE AS $$
  WITH i AS (
    SELECT d.excl_from AS a, coalesce(d.excl_to, 'infinity') AS b FROM discard_intervals(p_sensor_id) d
  ), m AS (
    SELECT a, b, max(b) OVER (ORDER BY a, b ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS reach FROM i
  ), g AS (
    SELECT a, b, count(*) FILTER (WHERE reach IS NULL OR a > reach) OVER (ORDER BY a, b) AS grp FROM m
  )
  SELECT min(a), nullif(max(b), 'infinity'::timestamptz) FROM g GROUP BY grp ORDER BY 1
$$;

-- The loss rule of the evaluation (evaluation/pipeline gaps.sql, summary.sql):
-- of the buffer row ids p_seq_lo..p_seq_hi between two stored polls, those
-- the sensor's discarded batches cover - a batch whose time overlaps
-- [p_ts_lo, p_ts_hi] - each counted once. A jump of seq minus these is lost.
CREATE OR REPLACE FUNCTION discarded_seqs(p_sensor_id integer, p_seq_lo bigint, p_seq_hi bigint,
                                          p_ts_lo timestamptz, p_ts_hi timestamptz)
RETURNS bigint
LANGUAGE sql STABLE AS $$
  SELECT count(DISTINCT n)
  FROM sensor_discarded_batches b
  CROSS JOIN LATERAL generate_series(greatest(b.seq_from, p_seq_lo), least(b.seq_to, p_seq_hi)) n
  WHERE b.sensor_id = p_sensor_id AND b.seq_from <= p_seq_hi AND b.seq_to >= p_seq_lo
    AND b.ts_from <= p_ts_hi AND b.ts_to >= p_ts_lo
$$;

-- Where a new uploader starts (live upload D5, the heartbeat's "resume"): the
-- newest of the sensor's stored polls and of its discarded batches, so an
-- uploader that lost its state never sends a discarded window again (seq
-- NULL for polls stored before schema 14 - the uploader then finds it by ts).
CREATE OR REPLACE FUNCTION ingest_resume(p_sensor_id integer)
RETURNS TABLE (seq bigint, ts bigint)
LANGUAGE sql STABLE AS $$
  SELECT x.seq, extract(epoch FROM x.t)::bigint
  FROM ((SELECT p.seq, p.ts AS t FROM polls p WHERE p.sensor_id = p_sensor_id ORDER BY p.ts DESC LIMIT 1)
        UNION ALL
        (SELECT b.seq_to, b.ts_to FROM sensor_discarded_batches b
         WHERE b.sensor_id = p_sensor_id AND b.kind = 'batch' AND b.polls > 0
         ORDER BY b.ts_to DESC LIMIT 1)) x
  ORDER BY x.t DESC
  LIMIT 1
$$;

-- Ends a window at p_at (p_by 'operator' or 'expiry'): the window and its
-- 'excluded' history entry (ref 'sensor_discard_windows <id>'). Returns the
-- window id, NULL when it was not open.
CREATE OR REPLACE FUNCTION discard_close(p_window_id integer, p_at timestamptz, p_by text)
RETURNS integer
LANGUAGE plpgsql AS $$
DECLARE
  v_sensor integer;
BEGIN
  UPDATE sensor_discard_windows w SET ended_at = p_at, ended_by = p_by
  WHERE w.id = p_window_id AND w.ended_at IS NULL
  RETURNING w.sensor_id INTO v_sensor;
  IF NOT FOUND THEN
    RETURN NULL;
  END IF;
  UPDATE sensor_annotations a SET ends_at = p_at
  WHERE a.sensor_id = v_sensor AND a.kind = 'excluded' AND a.ref = 'sensor_discard_windows ' || p_window_id;
  RETURN p_window_id;
END
$$;

-- The auto-expiry (the refresher, every round; discard_start()): every window
-- still open at its `until` ends there (ended_by 'expiry'), with its history
-- entry, one audit_log row ('sensor.discard.expire') and the account's
-- write_version + 1 (every ETag shows it). Returns what it closed.
CREATE OR REPLACE FUNCTION discard_close_expired()
RETURNS TABLE (window_id integer, sensor_id integer, sensor text, until timestamptz)
LANGUAGE plpgsql AS $$
DECLARE r record;
BEGIN
  FOR r IN SELECT w.id AS wid, w.sensor_id AS sid, w.until AS wuntil, w.started_at AS wstart, w.note AS wnote,
                  s.name AS sname, s.account_id AS acc
           FROM sensor_discard_windows w JOIN sensors s ON s.id = w.sensor_id
           WHERE w.ended_at IS NULL AND w.until <= now()
           ORDER BY w.id
           FOR UPDATE OF w LOOP
    PERFORM discard_close(r.wid, r.wuntil, 'expiry');
    INSERT INTO audit_log (account_id, action, target, old, new)
    VALUES (r.acc, 'sensor.discard.expire', 'sensor:' || r.sname,
            jsonb_build_object('window', r.wid, 'started_at', r.wstart, 'until', r.wuntil),
            jsonb_build_object('window', r.wid, 'ended_at', r.wuntil, 'ended_by', 'expiry', 'note', r.wnote));
    UPDATE accounts a SET write_version = a.write_version + 1 WHERE a.id = r.acc;
    window_id := r.wid;
    sensor_id := r.sid;
    sensor := r.sname;
    until := r.wuntil;
    RETURN NEXT;
  END LOOP;
END
$$;

-- Turns a sensor's discard mode on for p_seconds (> 0 and at most 24 h: the
-- table's constraint) with the operator's note, after closing every expired
-- window. One window on per sensor (a second start is a unique_violation).
-- Its 'excluded' history entry starts now. Returns the window id. The api's
-- write (Settings > Sensors) adds the audit_log row.
CREATE OR REPLACE FUNCTION discard_start(p_sensor_id integer, p_seconds integer, p_note text)
RETURNS integer
LANGUAGE plpgsql AS $$
DECLARE v_id integer;
BEGIN
  PERFORM discard_close_expired();
  INSERT INTO sensor_discard_windows (sensor_id, started_at, until, note)
  VALUES (p_sensor_id, now(), now() + make_interval(secs => p_seconds), btrim(p_note))
  RETURNING id INTO v_id;
  INSERT INTO sensor_annotations (sensor_id, starts_at, ends_at, kind, boundary, label, ref)
  VALUES (p_sensor_id, now(), NULL, 'excluded', false, 'Discard mode: ' || btrim(p_note),
          'sensor_discard_windows ' || v_id);
  RETURN v_id;
END
$$;

-- Turns a sensor's discard mode off now (the operator). Returns the window id,
-- NULL when none was on.
CREATE OR REPLACE FUNCTION discard_stop(p_sensor_id integer)
RETURNS integer
LANGUAGE plpgsql AS $$
BEGIN
  RETURN discard_close(discard_active(p_sensor_id), now(), 'operator');
END
$$;

-- Per window its totals (Settings > Sensors, the Sensors page, the history,
-- the evaluation exports): what it discarded - polls of whole batches and of
-- late rows, the seq and time range, replays.
CREATE OR REPLACE VIEW discard_window_totals AS
SELECT w.id, w.sensor_id, w.started_at, w.until, w.ended_at, w.ended_by, w.note,
       (w.ended_at IS NULL AND w.until > now()) AS active,
       coalesce(t.batches, 0) AS batches, coalesce(t.replays, 0) AS replays,
       coalesce(t.polls, 0) AS polls, coalesce(t.late_polls, 0) AS late_polls,
       coalesce(t.unverified_polls, 0) AS unverified_polls,
       t.seq_from, t.seq_to, t.ts_from, t.ts_to, t.last_at
FROM sensor_discard_windows w
LEFT JOIN LATERAL (
  SELECT count(*) FILTER (WHERE b.kind = 'batch') AS batches, sum(b.replays) AS replays,
         sum(b.polls) AS polls, sum(b.polls) FILTER (WHERE b.kind = 'late') AS late_polls,
         sum(b.unverified_polls) AS unverified_polls,
         min(b.seq_from) AS seq_from, max(b.seq_to) AS seq_to, min(b.ts_from) AS ts_from,
         max(b.ts_to) AS ts_to, max(b.received_at) AS last_at
  FROM sensor_discarded_batches b WHERE b.window_id = w.id) t ON true;

CREATE OR REPLACE FUNCTION refresh_sensor_periods(p_sensor_id integer)
RETURNS integer
LANGUAGE plpgsql
SET jit = off
AS $$
#variable_conflict use_column
DECLARE v_n integer;
BEGIN
  DELETE FROM sensor_periods WHERE sensor_id = p_sensor_id;
  INSERT INTO sensor_periods (sensor_id, starts_at, ends_at, kind, polls)
  WITH x AS MATERIALIZED (
    SELECT e.excl_from, e.excl_to FROM sensor_excluded(p_sensor_id) e
  ), pl AS (
    SELECT p.ts, 0 AS mark,
           CASE WHEN NOT (p.ok AND coalesce(p.ds_running, false) AND coalesce(p.devices_active, 0) > 0) THEN 'outage'
                WHEN p.ds_hop_ok = false THEN 'limited' ELSE 'ok' END AS poll_st
    FROM polls p
    WHERE p.sensor_id = p_sensor_id
      AND NOT EXISTS (SELECT 1 FROM x WHERE p.ts >= x.excl_from AND p.ts < coalesce(x.excl_to, 'infinity'))
    UNION ALL
    SELECT x.excl_from, 1, 'excluded' FROM x
    UNION ALL
    SELECT x.excl_to, 2, 'resume' FROM x WHERE x.excl_to IS NOT NULL
  ), p AS (
    SELECT ts, mark, poll_st, lead(ts) OVER w AS next_ts, lead(poll_st) OVER w AS next_st
    FROM pl WINDOW w AS (ORDER BY ts, mark)
  ), s AS (
    SELECT ts, mark, next_ts, poll_st,
           CASE WHEN poll_st = 'excluded' THEN 'excluded'
                WHEN next_ts - ts > interval '90 seconds' THEN 'outage'
                WHEN poll_st = 'resume' THEN CASE WHEN next_st IN ('ok', 'limited', 'outage') THEN next_st
                                                  ELSE 'excluded' END
                ELSE poll_st END AS st
    FROM p
  ), r AS (
    SELECT y.*, sum(CASE WHEN y.st IS DISTINCT FROM y.prev_st THEN 1 ELSE 0 END) OVER (ORDER BY y.ts, y.mark) AS run
    FROM (SELECT s.*, lag(s.st) OVER (ORDER BY s.ts, s.mark) AS prev_st FROM s) y
  ), g AS (
    SELECT min(ts) AS starts_at,
           CASE WHEN bool_or(next_ts IS NULL) THEN NULL ELSE max(next_ts) END AS ends_at,
           max(ts) AS last_ts, min(st) AS kind, (count(*) FILTER (WHERE poll_st = st))::integer AS polls
    FROM r GROUP BY run
  )
  SELECT p_sensor_id, g.starts_at, g.ends_at, g.kind, g.polls
  FROM g
  WHERE g.kind IN ('outage', 'limited')
    AND coalesce(g.ends_at, g.last_ts) - g.starts_at
        >= CASE g.kind WHEN 'outage' THEN interval '90 seconds' ELSE interval '60 seconds' END;
  GET DIAGNOSTICS v_n = ROW_COUNT;
  RETURN v_n;
END
$$;

CREATE OR REPLACE FUNCTION ingest_merge(p_sensor_id integer, p_via text)
RETURNS jsonb
LANGUAGE plpgsql
SET work_mem = '64MB'      -- a snapshot stages ~2.5M observation rows
SET jit = off
AS $$
DECLARE
  v_staged    jsonb := '{}';
  v_inserted  jsonb := '{}';
  v_conflicts jsonb := '{}';
  v_s   bigint;
  v_n   bigint;
  v_t   timestamptz;
  v_min timestamptz;
  v_any boolean := false;
  v_excluded jsonb := '[]';
BEGIN
  IF NOT EXISTS (SELECT 1 FROM sensors WHERE id = p_sensor_id) THEN
    RAISE EXCEPTION 'ingest_merge: unknown sensor_id %', p_sensor_id;
  END IF;
  IF p_via IS NULL OR p_via NOT IN ('snapshot', 'ingest') THEN
    RAISE EXCEPTION 'ingest_merge: via must be snapshot or ingest, not %', p_via;
  END IF;
  PERFORM ingest_stage_create();
  PERFORM pg_advisory_xact_lock(hashtext('ingest_merge'), p_sensor_id);

  -- 0. discard mode (schema 20): nothing inside an excluded window is stored
  IF EXISTS (SELECT 1 FROM sensor_discard_windows w WHERE w.sensor_id = p_sensor_id) THEN
    WITH w AS MATERIALIZED (
      SELECT d.window_id, d.excl_from AS a, coalesce(d.excl_to, 'infinity') AS b
      FROM discard_intervals(p_sensor_id) d
    ), dp AS (
      DELETE FROM stage_polls s USING w WHERE to_timestamp(s.ts) >= w.a AND to_timestamp(s.ts) < w.b
      RETURNING w.window_id, s.seq, to_timestamp(s.ts) AS ts
    ), d1 AS (
      DELETE FROM stage_observations s USING w WHERE to_timestamp(s.ts) >= w.a AND to_timestamp(s.ts) < w.b
      RETURNING w.window_id
    ), d2 AS (
      DELETE FROM stage_device_config_history s USING w WHERE to_timestamp(s.ts) >= w.a AND to_timestamp(s.ts) < w.b
      RETURNING w.window_id
    ), d3 AS (
      DELETE FROM stage_alerts s USING w WHERE to_timestamp(s.ts) >= w.a AND to_timestamp(s.ts) < w.b
      RETURNING w.window_id
    ), d4 AS (
      DELETE FROM stage_ap_load_hourly s USING w
      WHERE to_timestamp(s.hour) < w.b AND to_timestamp(s.hour) + interval '1 hour' > w.a
      RETURNING w.window_id
    ), d5 AS (
      DELETE FROM stage_load_samples s USING w WHERE s.ts >= w.a AND s.ts < w.b
      RETURNING w.window_id
    ), d6 AS (
      DELETE FROM stage_device_freq_hist s USING w
      WHERE to_timestamp(s.updated_ts) >= w.a AND to_timestamp(s.updated_ts) < w.b
      RETURNING w.window_id
    ), d7 AS (
      DELETE FROM stage_devices s USING w WHERE to_timestamp(s.last_seen) >= w.a AND to_timestamp(s.last_seen) < w.b
      RETURNING w.window_id
    ), d8 AS (
      DELETE FROM stage_associations s USING w
      WHERE to_timestamp(s.last_seen) >= w.a AND to_timestamp(s.last_seen) < w.b
      RETURNING w.window_id
    ), d9 AS (
      DELETE FROM stage_client_bssids s USING w
      WHERE to_timestamp(s.last_seen) >= w.a AND to_timestamp(s.last_seen) < w.b
      RETURNING w.window_id
    ), d10 AS (
      DELETE FROM stage_probes s USING w WHERE to_timestamp(s.last_time) >= w.a AND to_timestamp(s.last_time) < w.b
      RETURNING w.window_id
    ), cnt AS (
      SELECT window_id, 'polls' AS t, count(*) AS n FROM dp GROUP BY 1
      UNION ALL SELECT window_id, 'observations', count(*) FROM d1 GROUP BY 1
      UNION ALL SELECT window_id, 'device_config_history', count(*) FROM d2 GROUP BY 1
      UNION ALL SELECT window_id, 'alerts', count(*) FROM d3 GROUP BY 1
      UNION ALL SELECT window_id, 'ap_load_hourly', count(*) FROM d4 GROUP BY 1
      UNION ALL SELECT window_id, 'load_samples', count(*) FROM d5 GROUP BY 1
      UNION ALL SELECT window_id, 'device_freq_hist', count(*) FROM d6 GROUP BY 1
      UNION ALL SELECT window_id, 'devices', count(*) FROM d7 GROUP BY 1
      UNION ALL SELECT window_id, 'associations', count(*) FROM d8 GROUP BY 1
      UNION ALL SELECT window_id, 'client_bssids', count(*) FROM d9 GROUP BY 1
      UNION ALL SELECT window_id, 'probes', count(*) FROM d10 GROUP BY 1
    ), rng AS (
      SELECT window_id, count(*) AS n, min(seq) AS seq_from, max(seq) AS seq_to, min(ts) AS ts_from,
             max(ts) AS ts_to
      FROM dp GROUP BY 1
    ), per AS (
      SELECT window_id, jsonb_object_agg(t, n) AS rows FROM cnt GROUP BY 1
    )
    SELECT coalesce(jsonb_agg(jsonb_build_object(
             'window_id', per.window_id, 'polls', coalesce(rng.n, 0), 'seq_from', rng.seq_from,
             'seq_to', rng.seq_to, 'ts_from', rng.ts_from, 'ts_to', rng.ts_to, 'rows', per.rows)
             ORDER BY per.window_id), '[]')
      INTO v_excluded
    FROM per LEFT JOIN rng ON rng.window_id = per.window_id;
    -- a device first seen inside a closed window, seen again after it, starts at its end
    UPDATE stage_devices s SET first_seen = extract(epoch FROM w.excl_to)::bigint
    FROM discard_intervals(p_sensor_id) w
    WHERE w.excl_to IS NOT NULL AND to_timestamp(s.first_seen) >= w.excl_from
      AND to_timestamp(s.first_seen) < w.excl_to;
  END IF;

  -- 1. devices (every other table references them)
  SELECT count(*) INTO v_s FROM stage_devices;
  IF v_s > 0 THEN
    INSERT INTO devices (sensor_id, device_key, mac, type, manuf, first_seen, last_seen, ssid,
                         cloaked, crypt, crypt_bits, mfp_sup, mfp_req, adv_channel, ht_mode,
                         beacon_rate, country, config_changed_at, cur_n_clients,
                         cur_qbss_stations, cur_util_pct, cur_at)
    SELECT p_sensor_id, s.device_key, s.mac::macaddr, s.type, s.manuf, to_timestamp(s.first_seen),
           to_timestamp(s.last_seen), s.ssid, s.cloaked::boolean, s.crypt, s.crypt_bits,
           s.mfp_sup::boolean, s.mfp_req::boolean, s.adv_channel, s.ht_mode, s.beacon_rate,
           s.country, to_timestamp(s.config_changed_at), s.cur_n_clients, s.cur_qbss_stations,
           s.cur_util_pct, to_timestamp(s.cur_at)
    FROM stage_devices s
    WHERE p_via = 'snapshot'
       OR ap_evidence(s.type, s.ssid, s.crypt, s.adv_channel)
       OR EXISTS (SELECT 1 FROM devices k
                  WHERE k.sensor_id = p_sensor_id AND k.device_key = s.device_key AND k.ever_ap)
    ON CONFLICT (sensor_id, device_key) DO UPDATE SET
      -- every SET expression sees the stored row, so all compare with the old last_seen
      mac = CASE WHEN EXCLUDED.last_seen >= devices.last_seen THEN EXCLUDED.mac ELSE devices.mac END,
      type = CASE WHEN EXCLUDED.last_seen >= devices.last_seen THEN EXCLUDED.type ELSE devices.type END,
      manuf = coalesce(CASE WHEN EXCLUDED.last_seen >= devices.last_seen THEN EXCLUDED.manuf END,
                       devices.manuf, EXCLUDED.manuf),
      first_seen = least(devices.first_seen, EXCLUDED.first_seen),
      ssid = CASE WHEN EXCLUDED.last_seen >= devices.last_seen THEN EXCLUDED.ssid ELSE devices.ssid END,
      cloaked = CASE WHEN EXCLUDED.last_seen >= devices.last_seen THEN EXCLUDED.cloaked ELSE devices.cloaked END,
      crypt = CASE WHEN EXCLUDED.last_seen >= devices.last_seen THEN EXCLUDED.crypt ELSE devices.crypt END,
      crypt_bits = CASE WHEN EXCLUDED.last_seen >= devices.last_seen THEN EXCLUDED.crypt_bits ELSE devices.crypt_bits END,
      mfp_sup = CASE WHEN EXCLUDED.last_seen >= devices.last_seen THEN EXCLUDED.mfp_sup ELSE devices.mfp_sup END,
      mfp_req = CASE WHEN EXCLUDED.last_seen >= devices.last_seen THEN EXCLUDED.mfp_req ELSE devices.mfp_req END,
      adv_channel = CASE WHEN EXCLUDED.last_seen >= devices.last_seen THEN EXCLUDED.adv_channel ELSE devices.adv_channel END,
      ht_mode = CASE WHEN EXCLUDED.last_seen >= devices.last_seen THEN EXCLUDED.ht_mode ELSE devices.ht_mode END,
      beacon_rate = CASE WHEN EXCLUDED.last_seen >= devices.last_seen THEN EXCLUDED.beacon_rate ELSE devices.beacon_rate END,
      country = CASE WHEN EXCLUDED.last_seen >= devices.last_seen THEN EXCLUDED.country ELSE devices.country END,
      config_changed_at = CASE WHEN EXCLUDED.last_seen >= devices.last_seen
                               THEN coalesce(EXCLUDED.config_changed_at, devices.config_changed_at)
                               ELSE devices.config_changed_at END,
      cur_n_clients = CASE WHEN EXCLUDED.cur_at IS NOT NULL AND (devices.cur_at IS NULL OR EXCLUDED.cur_at >= devices.cur_at)
                           THEN EXCLUDED.cur_n_clients ELSE devices.cur_n_clients END,
      cur_qbss_stations = CASE WHEN EXCLUDED.cur_at IS NOT NULL AND (devices.cur_at IS NULL OR EXCLUDED.cur_at >= devices.cur_at)
                               THEN EXCLUDED.cur_qbss_stations ELSE devices.cur_qbss_stations END,
      cur_util_pct = CASE WHEN EXCLUDED.cur_at IS NOT NULL AND (devices.cur_at IS NULL OR EXCLUDED.cur_at >= devices.cur_at)
                          THEN EXCLUDED.cur_util_pct ELSE devices.cur_util_pct END,
      cur_at = CASE WHEN EXCLUDED.cur_at IS NOT NULL AND (devices.cur_at IS NULL OR EXCLUDED.cur_at >= devices.cur_at)
                    THEN EXCLUDED.cur_at ELSE devices.cur_at END,
      last_seen = greatest(devices.last_seen, EXCLUDED.last_seen);
    GET DIAGNOSTICS v_n = ROW_COUNT;
    v_staged := v_staged || jsonb_build_object('devices', v_s);
    v_inserted := v_inserted || jsonb_build_object('devices', v_n);
    v_conflicts := v_conflicts || jsonb_build_object('devices', v_s - v_n);
    v_any := v_any OR v_n > 0;
  END IF;

  -- 2. polls
  SELECT count(*) INTO v_s FROM stage_polls;
  IF v_s > 0 THEN
    WITH ins AS (
      INSERT INTO polls (sensor_id, ts, kismet_ts, devices_total, devices_active, new_obs, ds_running,
                         ds_error, ds_packets, ds_hop_n, ds_hop_visited, ds_hop_ok, duration_ms, ok,
                         error, seq, received_at, ts_offset_s, config_version)
      SELECT p_sensor_id, to_timestamp(s.ts), to_timestamp(s.kismet_ts), s.devices_total,
             s.devices_active, s.new_obs, s.ds_running::boolean, s.ds_error, s.ds_packets, s.ds_hop_n,
             s.ds_hop_visited, s.ds_hop_ok::boolean, s.duration_ms, s.ok::boolean, s.error, s.seq,
             now(), s.ts_offset_s, s.config_version
      FROM stage_polls s
      ORDER BY s.ts
      ON CONFLICT DO NOTHING
      RETURNING ts)
    SELECT count(*), min(ts) INTO v_n, v_t FROM ins;
    v_staged := v_staged || jsonb_build_object('polls', v_s);
    v_inserted := v_inserted || jsonb_build_object('polls', v_n);
    v_conflicts := v_conflicts || jsonb_build_object('polls', v_s - v_n);
    v_min := least(v_min, v_t);
    v_any := v_any OR v_n > 0;
    IF p_via = 'snapshot' THEN
      -- polls stored before schema 14 learn their buffer row id from a later snapshot
      UPDATE polls p SET seq = s.seq
      FROM stage_polls s
      WHERE p.sensor_id = p_sensor_id AND p.ts = to_timestamp(s.ts) AND p.seq IS NULL
        AND s.seq IS NOT NULL;
      GET DIAGNOSTICS v_n = ROW_COUNT;
      v_inserted := v_inserted || jsonb_build_object('polls_seq_filled', v_n);
    END IF;
  END IF;

  -- 3. the configuration history (before the load samples: their channel comes from it)
  SELECT count(*) INTO v_s FROM stage_device_config_history;
  IF v_s > 0 THEN
    INSERT INTO device_config_history (sensor_id, device_key, ts, ssid, cloaked, crypt, crypt_bits,
                                       mfp_sup, mfp_req, adv_channel, ht_mode, beacon_rate, country)
    SELECT p_sensor_id, s.device_key, to_timestamp(s.ts), s.ssid, s.cloaked::boolean, s.crypt,
           s.crypt_bits, s.mfp_sup::boolean, s.mfp_req::boolean, s.adv_channel, s.ht_mode,
           s.beacon_rate, s.country
    FROM stage_device_config_history s
    JOIN devices d ON d.sensor_id = p_sensor_id AND d.device_key = s.device_key
                  AND (p_via = 'snapshot' OR d.ever_ap)
    ON CONFLICT DO NOTHING;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    v_staged := v_staged || jsonb_build_object('device_config_history', v_s);
    v_inserted := v_inserted || jsonb_build_object('device_config_history', v_n);
    v_conflicts := v_conflicts || jsonb_build_object('device_config_history', v_s - v_n);
    v_any := v_any OR v_n > 0;
  END IF;

  -- 4. observations: AP rows only (devices.ever_ap, schema 10)
  SELECT count(*) INTO v_s FROM stage_observations;
  IF v_s > 0 THEN
    WITH ins AS (
      INSERT INTO observations (ts, pk_total, pk_data, bss_timestamp, disconnects_last, sensor_id,
                                disconnects, rssi, rssi_floor, device_key)
      SELECT to_timestamp(s.ts), s.pk_total, s.pk_data, s.bss_timestamp,
             to_timestamp(s.disconnects_last), p_sensor_id, s.disconnects, s.rssi,
             s.rssi_floor::boolean, s.device_key
      FROM stage_observations s
      JOIN devices d ON d.sensor_id = p_sensor_id AND d.device_key = s.device_key AND d.ever_ap
      ORDER BY s.ts, s.device_key
      ON CONFLICT DO NOTHING
      RETURNING ts)
    SELECT count(*), min(ts) INTO v_n, v_t FROM ins;
    v_staged := v_staged || jsonb_build_object('observations', v_s);
    v_inserted := v_inserted || jsonb_build_object('observations', v_n);
    v_conflicts := v_conflicts || jsonb_build_object('observations', v_s - v_n);
    v_min := least(v_min, v_t);
    v_any := v_any OR v_n > 0;
  END IF;

  -- 5. AP load per hour (buffer v7): the row with more polls wins
  SELECT count(*) INTO v_s FROM stage_ap_load_hourly;
  IF v_s > 0 THEN
    INSERT INTO ap_load_hourly AS t (sensor_id, device_key, hour, channel, polls, clients_polls,
                                     clients_max, clients_sum, qbss_polls, qbss_max, qbss_sum,
                                     util_max, util_sum, source)
    SELECT p_sensor_id, s.device_key, to_timestamp(s.hour), s.channel, s.polls, s.clients_polls,
           s.clients_max, s.clients_sum, s.qbss_polls, s.qbss_max, s.qbss_sum, s.util_max,
           s.util_sum, 'buffer'
    FROM stage_ap_load_hourly s
    JOIN devices d ON d.sensor_id = p_sensor_id AND d.device_key = s.device_key AND d.ever_ap
    ON CONFLICT (sensor_id, device_key, hour, channel) DO UPDATE SET
      polls = EXCLUDED.polls, clients_polls = EXCLUDED.clients_polls,
      clients_max = EXCLUDED.clients_max, clients_sum = EXCLUDED.clients_sum,
      qbss_polls = EXCLUDED.qbss_polls, qbss_max = EXCLUDED.qbss_max,
      qbss_sum = EXCLUDED.qbss_sum, util_max = EXCLUDED.util_max,
      util_sum = EXCLUDED.util_sum, source = EXCLUDED.source
      WHERE EXCLUDED.polls > t.polls;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    v_staged := v_staged || jsonb_build_object('ap_load_hourly', v_s);
    v_inserted := v_inserted || jsonb_build_object('ap_load_hourly', v_n);
    v_any := v_any OR v_n > 0;
  END IF;

  -- 6. per-poll AP load samples (buffers <= v5; schema 8) - the same fuller-wins rule
  SELECT count(*) INTO v_s FROM stage_load_samples;
  IF v_s > 0 THEN
    SELECT coalesce(sum(l.inserted + l.updated), 0) INTO v_n FROM load_ap_load_samples(p_sensor_id) l;
    v_staged := v_staged || jsonb_build_object('load_samples', v_s);
    v_inserted := v_inserted || jsonb_build_object('load_samples', v_n);
    v_any := v_any OR v_n > 0;
  END IF;

  -- 7. frequency histogram: the newer updated_ts wins
  SELECT count(*) INTO v_s FROM stage_device_freq_hist;
  IF v_s > 0 THEN
    INSERT INTO device_freq_hist (sensor_id, device_key, freq_khz, packets, updated_ts)
    SELECT p_sensor_id, s.device_key, s.freq_khz, s.packets, to_timestamp(s.updated_ts)
    FROM stage_device_freq_hist s
    JOIN devices d ON d.sensor_id = p_sensor_id AND d.device_key = s.device_key
                  AND (p_via = 'snapshot' OR d.ever_ap)
    ON CONFLICT (sensor_id, device_key, freq_khz) DO UPDATE SET
      packets = EXCLUDED.packets, updated_ts = EXCLUDED.updated_ts
      WHERE EXCLUDED.updated_ts > device_freq_hist.updated_ts;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    v_staged := v_staged || jsonb_build_object('device_freq_hist', v_s);
    v_inserted := v_inserted || jsonb_build_object('device_freq_hist', v_n);
    v_any := v_any OR v_n > 0;
  END IF;

  -- 8. client tables: snapshot only (live upload decision D3)
  IF p_via = 'snapshot' THEN
    SELECT count(*) INTO v_s FROM stage_associations;
    IF v_s > 0 THEN
      INSERT INTO associations (sensor_id, ap_key, client_mac, first_seen, last_seen)
      SELECT p_sensor_id, s.ap_key, s.client_mac::macaddr, to_timestamp(s.first_seen),
             to_timestamp(s.last_seen)
      FROM stage_associations s
      ON CONFLICT (sensor_id, ap_key, client_mac) DO UPDATE SET
        first_seen = least(associations.first_seen, EXCLUDED.first_seen),
        last_seen = greatest(associations.last_seen, EXCLUDED.last_seen);
      GET DIAGNOSTICS v_n = ROW_COUNT;
      v_staged := v_staged || jsonb_build_object('associations', v_s);
      v_inserted := v_inserted || jsonb_build_object('associations', v_n);
    END IF;
    SELECT count(*) INTO v_s FROM stage_client_bssids;
    IF v_s > 0 THEN
      INSERT INTO client_bssids (sensor_id, client_key, bssid, first_seen, last_seen)
      SELECT p_sensor_id, s.device_key, s.bssid::macaddr, to_timestamp(s.first_seen),
             to_timestamp(s.last_seen)
      FROM stage_client_bssids s
      ON CONFLICT (sensor_id, client_key, bssid) DO UPDATE SET
        first_seen = least(client_bssids.first_seen, EXCLUDED.first_seen),
        last_seen = greatest(client_bssids.last_seen, EXCLUDED.last_seen);
      GET DIAGNOSTICS v_n = ROW_COUNT;
      v_staged := v_staged || jsonb_build_object('client_bssids', v_s);
      v_inserted := v_inserted || jsonb_build_object('client_bssids', v_n);
    END IF;
    SELECT count(*) INTO v_s FROM stage_probes;
    IF v_s > 0 THEN
      INSERT INTO probes (sensor_id, device_key, ssid, first_time, last_time)
      SELECT p_sensor_id, s.device_key, s.ssid, to_timestamp(s.first_time), to_timestamp(s.last_time)
      FROM stage_probes s
      ON CONFLICT (sensor_id, device_key, ssid) DO UPDATE SET
        first_time = least(probes.first_time, EXCLUDED.first_time),
        last_time = greatest(probes.last_time, EXCLUDED.last_time);
      GET DIAGNOSTICS v_n = ROW_COUNT;
      v_staged := v_staged || jsonb_build_object('probes', v_s);
      v_inserted := v_inserted || jsonb_build_object('probes', v_n);
    END IF;
  END IF;

  -- 9. Kismet's alerts, deduplicated by hash (unchanged, D3)
  SELECT count(*) INTO v_s FROM stage_alerts;
  IF v_s > 0 THEN
    WITH ins AS (
      INSERT INTO alerts (sensor_id, hash, ts, header, class, severity, source_mac, dest_mac,
                          transmitter_mac, other_mac, channel, freq_khz, device_key, text, raw)
      SELECT p_sensor_id, s.hash, to_timestamp(s.ts), s.header, s.class, s.severity,
             s.source_mac::macaddr, s.dest_mac::macaddr, s.transmitter_mac::macaddr,
             s.other_mac::macaddr, s.channel, s.freq_khz, s.device_key, s.text, s.raw::jsonb
      FROM stage_alerts s
      ON CONFLICT (sensor_id, hash) DO NOTHING
      RETURNING ts)
    SELECT count(*), min(ts) INTO v_n, v_t FROM ins;
    v_staged := v_staged || jsonb_build_object('alerts', v_s);
    v_inserted := v_inserted || jsonb_build_object('alerts', v_n);
    v_conflicts := v_conflicts || jsonb_build_object('alerts', v_s - v_n);
    v_min := least(v_min, v_t);
    v_any := v_any OR v_n > 0;
  END IF;

  -- 10. the refresher's work: rollups from the earliest new ts (a device-only
  --     change: the current hour and the whole-sensor steps)
  IF v_any THEN
    INSERT INTO ingest_state AS i (sensor_id, dirty_from, dirty_since, dirty_at)
    VALUES (p_sensor_id, coalesce(v_min, now()), now(), now())
    ON CONFLICT (sensor_id) DO UPDATE SET
      dirty_from = least(i.dirty_from, EXCLUDED.dirty_from),
      dirty_since = coalesce(i.dirty_since, EXCLUDED.dirty_since),
      dirty_at = EXCLUDED.dirty_at;
  END IF;

  RETURN jsonb_build_object('via', p_via, 'staged', v_staged, 'inserted', v_inserted,
                            'conflicts', v_conflicts, 'min_ts', v_min, 'excluded', v_excluded);
END
$$;

\echo [4/4] schema_meta 20
INSERT INTO schema_meta (key, value) VALUES ('schema_version', '20')
  ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
COMMIT;
