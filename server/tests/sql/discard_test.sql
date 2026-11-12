-- Fixture test of discard mode (schema.sql, schema 20; docs/discard-mode.md):
-- discard_start(), discard_stop(), discard_active(), discard_close_expired()
-- (the auto-expiry), discard_intervals() / sensor_excluded() (the excluded
-- windows), discarded_seqs() (the seq-gap / loss rule), ingest_resume(), the
-- excluded windows in refresh_sensor_periods(), and step 0 of ingest_merge()
-- (rows inside an excluded window are never stored, also when they arrive
-- late or with a snapshot).
--
-- Writes nothing: everything runs in one transaction that is rolled back. A
-- scratch schema first on the search path gets empty copies of every table
-- these functions read or write (with their own id sequences, so the real
-- ones do not move); the deployed functions resolve their tables through the
-- search path. Run it as the owner (CREATE SCHEMA):
--
--   docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
--     -v ON_ERROR_STOP=1 < tests/sql/discard_test.sql
--
-- Prints NOTICE "discard: PASS". Cases:
--   1. start / stop: a window of 6 h with its 'excluded' history entry; a
--      second start while one is on is refused; a duration of 0 or more than
--      24 h and an empty note are refused; stop closes it at now() (operator)
--      and a second stop finds nothing;
--   2. the auto-expiry: discard_active() is false from `until` on, before the
--      window is closed; discard_close_expired() closes exactly the expired
--      windows at their `until` (expiry), ends their history entry, writes one
--      audit_log row each and bumps the account's write_version once; a second
--      run does nothing; discard_start() closes an expired window of its
--      sensor first;
--   3. the excluded windows: a discarded backlog older than the start widens
--      the window back; an open window has no end; an expired, unclosed one
--      ends at `until`; overlapping and abutting windows merge;
--   4. sensor periods: no outage across a window, an outage after a window
--      that ended while the sensor was off starts at the window's end, a real
--      outage before a window ends at its start, lost polls outside a window
--      stay an outage; an open window after a gap;
--   5. loss: seq jumps covered by discarded batches are excluded, the rest
--      (polls never reported) stays loss - the rule of
--      evaluation/pipeline/gaps.sql and summary.sql;
--   6. ingest_resume(): the newest of the stored polls and the discarded batches;
--   7. ingest_merge() step 0: the polls, observations, configuration history,
--      alerts, load hours, frequency rows and devices of a late batch that fall
--      inside a closed window are dropped and reported (per window), the rest
--      is stored; a device first seen inside the window starts at its end;
--      a snapshot drops the same rows; a sensor without windows is unaffected.

\set ON_ERROR_STOP on
BEGIN;
SET LOCAL TimeZone = 'UTC';

DO $$
DECLARE t text;
BEGIN
  CREATE SCHEMA discard_test;
  PERFORM set_config('search_path', 'discard_test, public', true);
  FOREACH t IN ARRAY ARRAY['accounts', 'sensors', 'polls', 'devices', 'device_config_history', 'observations',
                           'ap_load_hourly', 'device_freq_hist', 'associations', 'client_bssids', 'probes',
                           'alerts', 'ingest_state', 'sensor_annotations', 'sensor_periods', 'audit_log',
                           'sensor_discard_windows', 'sensor_discarded_batches'] LOOP
    EXECUTE format('CREATE TABLE %I (LIKE public.%I INCLUDING ALL)', t, t);
  END LOOP;
  CREATE TRIGGER devices_ever_ap BEFORE INSERT OR UPDATE ON devices
    FOR EACH ROW EXECUTE FUNCTION public.devices_ever_ap();
  -- the copied defaults would draw ids from the real sequences
  CREATE SEQUENCE discard_test.alerts_id;
  ALTER TABLE alerts ALTER COLUMN id SET DEFAULT nextval('discard_test.alerts_id');
  CREATE SEQUENCE discard_test.audit_log_id;
  ALTER TABLE audit_log ALTER COLUMN id SET DEFAULT nextval('discard_test.audit_log_id');
  CREATE SEQUENCE discard_test.windows_id;
  ALTER TABLE sensor_discard_windows ALTER COLUMN id SET DEFAULT nextval('discard_test.windows_id');
  CREATE SEQUENCE discard_test.batches_id;
  ALTER TABLE sensor_discarded_batches ALTER COLUMN id SET DEFAULT nextval('discard_test.batches_id');
END $$;

INSERT INTO accounts (id, name) VALUES (1, 'test');
INSERT INTO sensors (id, name, tz) VALUES (1, 'periods', 'UTC'), (2, 'open', 'UTC'), (3, 'late', 'UTC'),
                                          (4, 'toggle', 'UTC'), (5, 'expired', 'UTC'), (6, 'restart', 'UTC'),
                                          (7, 'merge', 'UTC'), (8, 'plain', 'UTC');

CREATE FUNCTION pg_temp.u(t text) RETURNS bigint
LANGUAGE sql IMMUTABLE AS $$ SELECT extract(epoch FROM t::timestamptz)::bigint $$;

CREATE FUNCTION pg_temp.check_rows(label text, got text, want text) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
  missing text;
  extra text;
BEGIN
  EXECUTE format('SELECT string_agg(x::text, E''\n'') FROM ((%s) EXCEPT ALL (%s)) x', want, got) INTO missing;
  EXECUTE format('SELECT string_agg(x::text, E''\n'') FROM ((%s) EXCEPT ALL (%s)) x', got, want) INTO extra;
  IF missing IS NOT NULL OR extra IS NOT NULL THEN
    RAISE EXCEPTION E'discard: FAIL (%)\nmissing:\n%\nunexpected:\n%',
      label, coalesce(missing, 'none'), coalesce(extra, 'none');
  END IF;
END $$;

CREATE FUNCTION pg_temp.check_eq(label text, got text, want text) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
  IF got IS DISTINCT FROM want THEN
    RAISE EXCEPTION 'discard: FAIL (%: got %, expected %)', label, got, want;
  END IF;
END $$;

-- the SQLSTATE a statement fails with ('' = it succeeded)
CREATE FUNCTION pg_temp.fails(stmt text) RETURNS text
LANGUAGE plpgsql AS $$
BEGIN
  EXECUTE stmt;
  RETURN '';
EXCEPTION WHEN OTHERS THEN
  RETURN SQLSTATE;
END $$;

-- polls every 30 s: seq a..b from t0 (collecting)
CREATE FUNCTION pg_temp.polls(sid integer, a bigint, b bigint, t0 timestamptz) RETURNS void
LANGUAGE sql AS $$
  INSERT INTO polls (sensor_id, ts, seq, ok, ds_running, devices_active, ds_hop_ok)
  SELECT sid, t0 + (n - a) * interval '30 seconds', n, true, true, 5, true FROM generate_series(a, b) n
$$;

-- --- 1. start / stop ---------------------------------------------------------------------------
CREATE TEMP TABLE w4 AS SELECT discard_start(4, 6 * 3600, '  PoE power test  ') AS id;
SELECT pg_temp.check_rows('start: the window',
  $$SELECT sensor_id, started_at = now(), until = now() + interval '6 hours', ended_at, ended_by, note
    FROM sensor_discard_windows WHERE id = (SELECT id FROM w4)$$,
  $$VALUES (4, true, true, NULL::timestamptz, NULL::text, 'PoE power test')$$);
SELECT pg_temp.check_rows('start: the history entry',
  $$SELECT sensor_id, starts_at = now(), ends_at, kind, boundary, label, ref FROM sensor_annotations WHERE sensor_id = 4$$,
  $$SELECT 4, true, NULL::timestamptz, 'excluded', false, 'Discard mode: PoE power test',
           'sensor_discard_windows ' || (SELECT id FROM w4)$$);
SELECT pg_temp.check_eq('start: active', (SELECT discard_active(4) = id FROM w4)::text, 'true');
SELECT pg_temp.check_eq('start: a second one is refused',
                        pg_temp.fails($$SELECT discard_start(4, 3600, 'again')$$), '23505');
SELECT pg_temp.check_eq('start: 0 s refused', pg_temp.fails($$SELECT discard_start(8, 0, 'x')$$), '23514');
SELECT pg_temp.check_eq('start: 24 h is the maximum', pg_temp.fails($$SELECT discard_start(8, 86401, 'x')$$), '23514');
SELECT pg_temp.check_eq('start: a note is required', pg_temp.fails($$SELECT discard_start(8, 3600, '  ')$$), '23514');
SELECT pg_temp.check_eq('start: nothing left behind',
                        (SELECT count(*) FROM sensor_discard_windows WHERE sensor_id = 8)::text, '0');

-- --- 2. the auto-expiry -----------------------------------------------------------------------------
-- an expired window not closed yet (the refresher was down), and its history entry
INSERT INTO sensor_discard_windows (sensor_id, started_at, until, note)
VALUES (5, now() - interval '7 hours', now() - interval '1 hour', 'old test');
INSERT INTO sensor_annotations (sensor_id, starts_at, kind, label, ref)
SELECT 5, started_at, 'excluded', 'Discard mode: old test', 'sensor_discard_windows ' || id
FROM sensor_discard_windows WHERE sensor_id = 5;
SELECT pg_temp.check_eq('expiry: inactive from until on', (discard_active(5) IS NULL)::text, 'true');
SELECT pg_temp.check_rows('expiry: an expired window ends at until before it is closed',
  'SELECT excl_from, excl_to FROM discard_intervals(5)',
  $$VALUES (now() - interval '7 hours', now() - interval '1 hour')$$);

CREATE TEMP TABLE closed AS SELECT * FROM discard_close_expired();
SELECT pg_temp.check_rows('expiry: what was closed', 'SELECT sensor_id, until FROM closed',
                          $$VALUES (5, now() - interval '1 hour')$$);
SELECT pg_temp.check_rows('expiry: the window',
  $$SELECT sensor_id, ended_at, ended_by FROM sensor_discard_windows WHERE sensor_id IN (4, 5)$$,
  $$VALUES (4, NULL::timestamptz, NULL::text), (5, now() - interval '1 hour', 'expiry')$$);
SELECT pg_temp.check_rows('expiry: the history entry ends',
  $$SELECT sensor_id, ends_at FROM sensor_annotations WHERE sensor_id IN (4, 5)$$,
  $$VALUES (4, NULL::timestamptz), (5, now() - interval '1 hour')$$);
SELECT pg_temp.check_rows('expiry: the audit row',
  $$SELECT account_id, action, target, (new ->> 'ended_at')::timestamptz, new ->> 'ended_by', new ->> 'note'
    FROM audit_log$$,
  $$VALUES (1, 'sensor.discard.expire', 'sensor:expired', now() - interval '1 hour', 'expiry', 'old test')$$);
SELECT pg_temp.check_eq('expiry: write_version', (SELECT write_version FROM accounts)::text, '1');
SELECT pg_temp.check_eq('expiry: a second run', (SELECT count(*) FROM discard_close_expired())::text, '0');
SELECT pg_temp.check_eq('expiry: no second audit row', (SELECT count(*) FROM audit_log)::text, '1');
SELECT pg_temp.check_eq('expiry: write_version unchanged', (SELECT write_version FROM accounts)::text, '1');

-- discard_start() closes its sensor's expired window first (it never blocks a new one)
INSERT INTO sensor_discard_windows (sensor_id, started_at, until, note)
VALUES (6, now() - interval '3 hours', now() - interval '10 minutes', 'first');
SELECT pg_temp.check_eq('restart after expiry: allowed', pg_temp.fails($$SELECT discard_start(6, 1800, 'second')$$), '');
SELECT pg_temp.check_rows('restart after expiry: the old one expired, the new one open',
  $$SELECT note, ended_at, ended_by FROM sensor_discard_windows WHERE sensor_id = 6$$,
  $$VALUES ('first', now() - interval '10 minutes', 'expiry'), ('second', NULL::timestamptz, NULL::text)$$);

-- stop: at now(), by the operator
SELECT pg_temp.check_eq('stop: the open window', (SELECT discard_stop(4) = id FROM w4)::text, 'true');
SELECT pg_temp.check_rows('stop: closed now',
  $$SELECT ended_at, ended_by FROM sensor_discard_windows WHERE sensor_id = 4$$, $$VALUES (now(), 'operator')$$);
SELECT pg_temp.check_eq('stop: history entry ends now',
                        (SELECT ends_at = now() FROM sensor_annotations WHERE sensor_id = 4)::text, 'true');
SELECT pg_temp.check_eq('stop: not active', (discard_active(4) IS NULL)::text, 'true');
SELECT pg_temp.check_eq('stop: a second stop', (discard_stop(4) IS NULL)::text, 'true');

-- --- 3. the excluded windows -----------------------------------------------------------------------
-- sensor 7: [10:00, 12:00) widened back to 09:40 by a discarded backlog; [11:30, 13:00) overlaps it;
-- [13:00, 14:00) abuts; [15:00, 16:00) stands alone
INSERT INTO sensor_discard_windows (sensor_id, started_at, until, ended_at, ended_by, note) VALUES
  (7, '2026-11-01 10:00+00', '2026-11-01 12:00+00', '2026-11-01 12:00+00', 'expiry', 'a'),
  (7, '2026-11-01 11:30+00', '2026-11-01 14:00+00', '2026-11-01 13:00+00', 'operator', 'b'),
  (7, '2026-11-01 13:00+00', '2026-11-01 14:00+00', '2026-11-01 14:00+00', 'expiry', 'c'),
  (7, '2026-11-01 15:00+00', '2026-11-01 18:00+00', '2026-11-01 16:00+00', 'operator', 'd');
INSERT INTO sensor_discarded_batches (sensor_id, window_id, kind, buffer_id, batch_seq, polls, seq_from, seq_to,
                                      ts_from, ts_to)
SELECT 7, id, 'batch', '00000000-0000-0000-0000-000000000007', 1, 41, 100, 140,
       '2026-11-01 09:40+00', '2026-11-01 10:00+00'
FROM sensor_discard_windows WHERE sensor_id = 7 AND note = 'a';
SELECT pg_temp.check_rows('windows: per window',
  $$SELECT w.note, i.excl_from, i.excl_to FROM discard_intervals(7) i JOIN sensor_discard_windows w ON w.id = i.window_id$$,
  $$VALUES ('a', '2026-11-01 09:40+00'::timestamptz, '2026-11-01 12:00+00'::timestamptz),
           ('b', '2026-11-01 11:30+00', '2026-11-01 13:00+00'),
           ('c', '2026-11-01 13:00+00', '2026-11-01 14:00+00'),
           ('d', '2026-11-01 15:00+00', '2026-11-01 16:00+00')$$);
SELECT pg_temp.check_rows('windows: merged', 'SELECT excl_from, excl_to FROM sensor_excluded(7)',
  $$VALUES ('2026-11-01 09:40+00'::timestamptz, '2026-11-01 14:00+00'::timestamptz),
           ('2026-11-01 15:00+00', '2026-11-01 16:00+00')$$);
SELECT pg_temp.check_rows('windows: an open one has no end', 'SELECT excl_from, excl_to FROM sensor_excluded(6)',
  $$VALUES (now() - interval '3 hours', now() - interval '10 minutes'), (now(), NULL::timestamptz)$$);
SELECT pg_temp.check_eq('windows: none', (SELECT count(*) FROM sensor_excluded(8))::text, '0');

-- --- 4. sensor periods ---------------------------------------------------------------------------------
-- sensor 1 (2026-11-01, UTC):
--   seq 1-240     08:00:00 - 09:59:30   collecting
--   W1            10:00 - 14:00 (stopped; started 10:00, until 16:00), batches seq 241-720 discarded
--   seq 721-760   14:00:10 - 14:19:40   collecting; seq 761-770 never arrived (lost)
--   seq 771-840   14:25:10 - 14:59:40
--   W2            15:00 - 16:00 (expired), batch seq 841-890 discarded; 891-900 never reported; the sensor off
--   seq 901-960   16:30:00 - 16:59:30   then the sensor stops (a real outage)
--   W3            17:20 - 17:40 (stopped), nothing arrived
--   seq 961-1000  17:40:15 - 17:59:45
SELECT pg_temp.polls(1, 1, 240, '2026-11-01 08:00:00+00');
SELECT pg_temp.polls(1, 721, 760, '2026-11-01 14:00:10+00');
SELECT pg_temp.polls(1, 771, 840, '2026-11-01 14:25:10+00');
SELECT pg_temp.polls(1, 901, 960, '2026-11-01 16:30:00+00');
SELECT pg_temp.polls(1, 961, 1000, '2026-11-01 17:40:15+00');
INSERT INTO sensor_discard_windows (sensor_id, started_at, until, ended_at, ended_by, note) VALUES
  (1, '2026-11-01 10:00+00', '2026-11-01 16:00+00', '2026-11-01 14:00+00', 'operator', 'W1'),
  (1, '2026-11-01 15:00+00', '2026-11-01 16:00+00', '2026-11-01 16:00+00', 'expiry', 'W2'),
  (1, '2026-11-01 17:20+00', '2026-11-01 18:00+00', '2026-11-01 17:40+00', 'operator', 'W3');
INSERT INTO sensor_discarded_batches (sensor_id, window_id, kind, buffer_id, batch_seq, polls, seq_from, seq_to,
                                      ts_from, ts_to)
SELECT 1, w.id, 'batch', '00000000-0000-0000-0000-000000000001', b.batch_seq, b.seq_to - b.seq_from + 1,
       b.seq_from, b.seq_to,
       '2026-11-01 08:00:00+00'::timestamptz + (b.seq_from - 1) * interval '30 seconds',
       '2026-11-01 08:00:00+00'::timestamptz + (b.seq_to - 1) * interval '30 seconds'
FROM (VALUES ('W1', 241::bigint, 241::bigint, 360::bigint), ('W1', 242, 361, 480), ('W1', 243, 481, 600),
             ('W1', 244, 601, 720)) b (note, batch_seq, seq_from, seq_to)
JOIN sensor_discard_windows w ON w.sensor_id = 1 AND w.note = b.note;
INSERT INTO sensor_discarded_batches (sensor_id, window_id, kind, buffer_id, batch_seq, polls, seq_from, seq_to,
                                      ts_from, ts_to)
SELECT 1, id, 'batch', '00000000-0000-0000-0000-000000000001', 300, 50, 841, 890,
       '2026-11-01 15:00:00+00', '2026-11-01 15:24:30+00'
FROM sensor_discard_windows WHERE sensor_id = 1 AND note = 'W2';

SELECT pg_temp.check_eq('periods: rows written', refresh_sensor_periods(1)::text, '3');
SELECT pg_temp.check_rows('periods: sensor 1', 'SELECT starts_at, ends_at, kind, polls FROM sensor_periods WHERE sensor_id = 1',
  $$VALUES ('2026-11-01 14:19:40+00'::timestamptz, '2026-11-01 14:25:10+00'::timestamptz, 'outage', 0),
           ('2026-11-01 16:00:00+00', '2026-11-01 16:30:00+00', 'outage', 0),
           ('2026-11-01 16:59:30+00', '2026-11-01 17:20:00+00', 'outage', 0)$$);
SELECT pg_temp.check_eq('periods: nothing inside a window',
  (SELECT count(*) FROM sensor_periods p, sensor_excluded(1) x
   WHERE p.sensor_id = 1 AND p.starts_at < coalesce(x.excl_to, 'infinity') AND coalesce(p.ends_at, 'infinity') > x.excl_from)::text,
  '0');

-- sensor 2: polls until 10 min ago, then 5 min without a poll (a real outage), then a window
-- that is still on - the outage ends at its start, nothing is ongoing
SELECT pg_temp.polls(2, 1, 100, date_trunc('second', now()) - interval '10 minutes' - 99 * interval '30 seconds');
INSERT INTO sensor_discard_windows (sensor_id, started_at, until, note)
VALUES (2, date_trunc('second', now()) - interval '5 minutes', now() + interval '1 hour', 'on');
SELECT pg_temp.check_eq('periods: open window', refresh_sensor_periods(2)::text, '1');
SELECT pg_temp.check_rows('periods: sensor 2', 'SELECT starts_at, ends_at, kind, polls FROM sensor_periods WHERE sensor_id = 2',
  $$VALUES (date_trunc('second', now()) - interval '10 minutes', date_trunc('second', now()) - interval '5 minutes',
            'outage', 0)$$);

-- sensor 8 has no window: the derivation is unchanged (a 5-min gap is an outage, the tail open)
SELECT pg_temp.polls(8, 1, 10, '2026-11-01 08:00:00+00');
SELECT pg_temp.polls(8, 11, 20, '2026-11-01 08:10:00+00');
SELECT pg_temp.check_eq('periods: without a window', refresh_sensor_periods(8)::text, '1');
SELECT pg_temp.check_rows('periods: sensor 8', 'SELECT starts_at, ends_at, kind, polls FROM sensor_periods WHERE sensor_id = 8',
  $$VALUES ('2026-11-01 08:04:30+00'::timestamptz, '2026-11-01 08:10:00+00'::timestamptz, 'outage', 0)$$);

-- --- 5. loss: the seq jumps between stored polls minus the discarded seqs -----------------------------
CREATE TEMP TABLE loss AS
WITH p AS (
  SELECT seq, ts, lag(seq) OVER w AS prev_seq, lag(ts) OVER w AS prev_ts
  FROM polls WHERE sensor_id = 1 AND seq IS NOT NULL WINDOW w AS (ORDER BY ts))
SELECT prev_seq, seq, seq - prev_seq - 1 AS jump, discarded_seqs(1, prev_seq + 1, seq - 1, prev_ts, ts) AS excluded
FROM p WHERE prev_seq IS NOT NULL AND seq > prev_seq + 1;
SELECT pg_temp.check_rows('loss: the jumps', 'SELECT prev_seq, seq, jump, excluded FROM loss',
  $$VALUES (240::bigint, 721::bigint, 480::bigint, 480::bigint),   -- W1: all discarded
           (760, 771, 10, 0),                                      -- lost outside a window
           (840, 901, 60, 50)$$);                                  -- W2: 50 discarded, 10 never reported
SELECT pg_temp.check_eq('loss: missing', (SELECT sum(jump - excluded) FROM loss)::text, '20');
SELECT pg_temp.check_eq('loss: excluded', (SELECT sum(excluded) FROM loss)::text, '530');
-- a discarded batch counts only for a jump in its time; overlapping records count a seq once
SELECT pg_temp.check_eq('loss: another time', discarded_seqs(1, 241, 720, '2026-11-02 00:00+00', '2026-11-02 01:00+00')::text, '0');
INSERT INTO sensor_discarded_batches (sensor_id, window_id, kind, buffer_id, batch_seq, polls, seq_from, seq_to,
                                      ts_from, ts_to)
SELECT 1, id, 'late', '00000000-0000-0000-0000-000000000001', 901, 20, 701, 720,
       '2026-11-01 13:50:00+00', '2026-11-01 13:59:30+00'
FROM sensor_discard_windows WHERE sensor_id = 1 AND note = 'W1';
SELECT pg_temp.check_eq('loss: counted once',
  discarded_seqs(1, 241, 720, '2026-11-01 09:59:30+00', '2026-11-01 14:00:10+00')::text, '480');

-- --- 6. ingest_resume(): the newest of the stored polls and the discarded batches ---------------------
SELECT pg_temp.check_rows('resume: stored poll newest', 'SELECT seq, ts FROM ingest_resume(1)',
  $$VALUES (1000::bigint, pg_temp.u('2026-11-01 17:59:45+00'))$$);
INSERT INTO sensor_discarded_batches (sensor_id, window_id, kind, buffer_id, batch_seq, polls, seq_from, seq_to,
                                      ts_from, ts_to)
SELECT 2, id, 'batch', '00000000-0000-0000-0000-000000000002', 7, 4, 101, 104,
       date_trunc('second', now()) - interval '2 minutes', date_trunc('second', now()) - interval '30 seconds'
FROM sensor_discard_windows WHERE sensor_id = 2;
SELECT pg_temp.check_rows('resume: discarded batch newest', 'SELECT seq, ts FROM ingest_resume(2)',
  $$VALUES (104::bigint, extract(epoch FROM date_trunc('second', now()) - interval '30 seconds')::bigint)$$);
SELECT pg_temp.check_eq('resume: nothing', (SELECT count(*) FROM ingest_resume(5))::text, '0');

-- --- 7. ingest_merge() step 0 ----------------------------------------------------------------------------
-- sensor 3: a closed window 10:00 - 12:00 on 2026-11-02; a late batch spanning it
INSERT INTO sensor_discard_windows (sensor_id, started_at, until, ended_at, ended_by, note)
VALUES (3, '2026-11-02 10:00+00', '2026-11-02 12:00+00', '2026-11-02 12:00+00', 'expiry', 'late');
SELECT ingest_stage_create();
INSERT INTO stage_devices (device_key, mac, type, first_seen, last_seen, ssid, crypt, adv_channel)
VALUES ('A', 'A8:00:00:00:00:01', 'ap', pg_temp.u('2026-11-02 09:00+00'), pg_temp.u('2026-11-02 12:00:30+00'), 'Home', 'WPA2', '6'),
       ('B', 'A8:00:00:00:00:02', 'ap', pg_temp.u('2026-11-02 10:30+00'), pg_temp.u('2026-11-02 11:00+00'), 'Away', 'WPA2', '1'),
       ('C', 'A8:00:00:00:00:03', 'ap', pg_temp.u('2026-11-02 10:30+00'), pg_temp.u('2026-11-02 12:00:30+00'), 'Both', 'WPA2', '11');
INSERT INTO stage_polls (seq, ts, devices_active, ds_running, ok)
VALUES (10, pg_temp.u('2026-11-02 09:59:00+00'), 3, 1, 1),
       (11, pg_temp.u('2026-11-02 10:30:00+00'), 3, 1, 1),
       (12, pg_temp.u('2026-11-02 11:59:30+00'), 3, 1, 1),
       (13, pg_temp.u('2026-11-02 12:00:30+00'), 3, 1, 1);
INSERT INTO stage_observations (ts, device_key, rssi, pk_total)
VALUES (pg_temp.u('2026-11-02 09:59:00+00'), 'A', -60, 1), (pg_temp.u('2026-11-02 10:30:00+00'), 'A', -40, 2),
       (pg_temp.u('2026-11-02 11:59:30+00'), 'A', -41, 3), (pg_temp.u('2026-11-02 12:00:30+00'), 'A', -61, 4);
INSERT INTO stage_device_config_history (device_key, ts, ssid, crypt, adv_channel)
VALUES ('A', pg_temp.u('2026-11-02 10:30+00'), 'Home', 'WPA2', '1'),
       ('A', pg_temp.u('2026-11-02 12:00:30+00'), 'Home', 'WPA2', '6');
INSERT INTO stage_alerts (hash, ts, header, severity, transmitter_mac, raw)
VALUES ('in', pg_temp.u('2026-11-02 10:45+00'), 'DEAUTHFLOOD', 10, 'A8:00:00:00:00:01', '{}'),
       ('out', pg_temp.u('2026-11-02 12:00:30+00'), 'DEAUTHFLOOD', 10, 'A8:00:00:00:00:01', '{}');
INSERT INTO stage_ap_load_hourly (device_key, hour, channel, polls, clients_polls, qbss_polls)
VALUES ('A', pg_temp.u('2026-11-02 09:00+00'), '6', 2, 0, 0), ('A', pg_temp.u('2026-11-02 10:00+00'), '6', 120, 0, 0),
       ('A', pg_temp.u('2026-11-02 11:00+00'), '6', 120, 0, 0), ('A', pg_temp.u('2026-11-02 12:00+00'), '6', 2, 0, 0);
INSERT INTO stage_device_freq_hist (device_key, freq_khz, packets, updated_ts)
VALUES ('A', 2437000, 50, pg_temp.u('2026-11-02 11:00+00')), ('A', 5180000, 5, pg_temp.u('2026-11-02 12:30+00'));

CREATE TEMP TABLE m1 AS SELECT ingest_merge(3, 'ingest') AS j;
SELECT pg_temp.check_rows('late: polls', 'SELECT ts, seq FROM polls WHERE sensor_id = 3',
  $$VALUES ('2026-11-02 09:59:00+00'::timestamptz, 10::bigint), ('2026-11-02 12:00:30+00', 13)$$);
SELECT pg_temp.check_rows('late: observations', 'SELECT ts, device_key FROM observations WHERE sensor_id = 3',
  $$VALUES ('2026-11-02 09:59:00+00'::timestamptz, 'A'), ('2026-11-02 12:00:30+00', 'A')$$);
SELECT pg_temp.check_rows('late: history', 'SELECT ts FROM device_config_history WHERE sensor_id = 3',
  $$VALUES ('2026-11-02 12:00:30+00'::timestamptz)$$);
SELECT pg_temp.check_rows('late: alerts', 'SELECT hash FROM alerts WHERE sensor_id = 3', $$VALUES ('out')$$);
SELECT pg_temp.check_rows('late: load hours', 'SELECT hour FROM ap_load_hourly WHERE sensor_id = 3',
  $$VALUES ('2026-11-02 09:00+00'::timestamptz), ('2026-11-02 12:00+00')$$);
SELECT pg_temp.check_rows('late: frequency rows', 'SELECT freq_khz FROM device_freq_hist WHERE sensor_id = 3',
  $$VALUES (5180000)$$);
SELECT pg_temp.check_rows('late: devices', 'SELECT device_key, first_seen, last_seen FROM devices WHERE sensor_id = 3',
  $$VALUES ('A', '2026-11-02 09:00+00'::timestamptz, '2026-11-02 12:00:30+00'::timestamptz),
           ('C', '2026-11-02 12:00+00', '2026-11-02 12:00:30+00')$$);
SELECT pg_temp.check_rows('late: reported',
  $$SELECT (e ->> 'window_id')::int = (SELECT id FROM sensor_discard_windows WHERE sensor_id = 3),
           (e ->> 'polls')::int, (e ->> 'seq_from')::bigint, (e ->> 'seq_to')::bigint,
           (e ->> 'ts_from')::timestamptz, (e ->> 'ts_to')::timestamptz, e -> 'rows'
    FROM m1, jsonb_array_elements(j -> 'excluded') e$$,
  $$VALUES (true, 2, 11::bigint, 12::bigint, '2026-11-02 10:30:00+00'::timestamptz, '2026-11-02 11:59:30+00'::timestamptz,
            '{"polls": 2, "observations": 2, "device_config_history": 1, "alerts": 1, "ap_load_hourly": 2,
              "device_freq_hist": 1, "devices": 1}'::jsonb)$$);
SELECT pg_temp.check_eq('late: dirty from the earliest stored row',
                        (SELECT dirty_from FROM ingest_state WHERE sensor_id = 3)::text, '2026-11-02 09:59:00+00');

-- a snapshot covering the window stores nothing of it either
TRUNCATE stage_devices, stage_polls, stage_device_config_history, stage_observations, stage_ap_load_hourly,
         stage_load_samples, stage_device_freq_hist, stage_associations, stage_client_bssids, stage_probes, stage_alerts;
INSERT INTO stage_polls (seq, ts, devices_active, ds_running, ok)
VALUES (11, pg_temp.u('2026-11-02 10:30:00+00'), 3, 1, 1), (14, pg_temp.u('2026-11-02 12:01:00+00'), 3, 1, 1);
INSERT INTO stage_load_samples (ts, device_key, n_clients, qbss_stations, util_pct)
VALUES ('2026-11-02 10:40+00', 'A', 9, 0, 0);
CREATE TEMP TABLE m2 AS SELECT ingest_merge(3, 'snapshot') AS j;
SELECT pg_temp.check_rows('snapshot: polls', 'SELECT seq FROM polls WHERE sensor_id = 3',
  $$VALUES (10::bigint), (13), (14)$$);
SELECT pg_temp.check_eq('snapshot: reported', (SELECT j #>> '{excluded,0,rows}' FROM m2),
                        '{"polls": 1, "load_samples": 1}');

-- a sensor without windows: nothing dropped, nothing reported
TRUNCATE stage_polls, stage_load_samples;
INSERT INTO stage_polls (seq, ts, devices_active, ds_running, ok) VALUES (21, pg_temp.u('2026-11-02 10:30:00+00'), 3, 1, 1);
CREATE TEMP TABLE m3 AS SELECT ingest_merge(8, 'ingest') AS j;
SELECT pg_temp.check_eq('no window: stored', (SELECT j #>> '{inserted,polls}' FROM m3), '1');
SELECT pg_temp.check_eq('no window: nothing excluded', (SELECT j ->> 'excluded' FROM m3), '[]');

DO $$ BEGIN
  RAISE NOTICE 'discard: PASS (start/stop, auto-expiry with audit and write_version, excluded windows widened and merged, no outage across a window, outages outside kept, seq loss minus discarded seqs, resume point, late and snapshot rows inside a window dropped)';
END $$;

ROLLBACK;
