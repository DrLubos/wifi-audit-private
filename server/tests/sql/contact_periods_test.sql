-- Fixture test of refresh_contact_periods() (schema.sql, schema 14): stretches
-- of more than 3 minutes without a stored heartbeat become sensor_annotations
-- of kind 'unreachable'.
--
-- Writes nothing: one transaction, rolled back, on empty copies of sensors,
-- sensor_heartbeats and sensor_annotations in a scratch schema first on the
-- search path. Run it as the owner (CREATE SCHEMA):
--
--   docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
--     -v ON_ERROR_STOP=1 < tests/sql/contact_periods_test.sql
--
-- Prints NOTICE "contact_periods: PASS". Cases:
--   1. a full run: two closed gaps three days ago, one closed gap three hours
--      ago, an ongoing gap (no heartbeat for the last 10 minutes); a missed
--      single heartbeat (2 minutes) is no gap; other annotation kinds stay;
--   2. a default run recomputes only the tail (from the last heartbeat before
--      two days ago) and gives the same rows;
--   3. contact returns: the ongoing gap closes at the new heartbeat.

\set ON_ERROR_STOP on
BEGIN;

DO $$
DECLARE t text;
BEGIN
  CREATE SCHEMA contact_periods_test;
  PERFORM set_config('search_path', 'contact_periods_test, public', true);
  FOREACH t IN ARRAY ARRAY['sensors', 'sensor_heartbeats', 'sensor_annotations'] LOOP
    EXECUTE format('CREATE TABLE %I (LIKE public.%I INCLUDING ALL)', t, t);
  END LOOP;
END $$;

INSERT INTO sensors (id, name, tz) VALUES (1, 'test', 'UTC');

CREATE FUNCTION pg_temp.check_rows(label text, got text, want text) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
  missing text;
  extra text;
BEGIN
  EXECUTE format('SELECT string_agg(x::text, E''\n'') FROM ((%s) EXCEPT ALL (%s)) x', want, got) INTO missing;
  EXECUTE format('SELECT string_agg(x::text, E''\n'') FROM ((%s) EXCEPT ALL (%s)) x', got, want) INTO extra;
  IF missing IS NOT NULL OR extra IS NOT NULL THEN
    RAISE EXCEPTION E'contact_periods: FAIL (%)\nmissing:\n%\nunexpected:\n%',
      label, coalesce(missing, 'none'), coalesce(extra, 'none');
  END IF;
END $$;

CREATE FUNCTION pg_temp.check_n(label text, got integer, want integer) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
  IF got IS DISTINCT FROM want THEN
    RAISE EXCEPTION 'contact_periods: FAIL (% returned %, expected %)', label, got, want;
  END IF;
END $$;

-- heartbeats: three days ago two at 10 minutes' distance; three hours ago one a
-- minute for an hour, 10 minutes of silence, one a minute up to 10 minutes ago
-- with minute 100 missing (one lost heartbeat: no gap)
INSERT INTO sensor_heartbeats (sensor_id, received_at, sensor_ts)
SELECT 1, t, t FROM (
  SELECT now() - interval '3 days' AS t
  UNION ALL SELECT now() - interval '3 days' + interval '10 minutes'
  UNION ALL SELECT now() - interval '3 hours' + m * interval '1 minute' FROM generate_series(0, 60) m
  UNION ALL SELECT now() - interval '3 hours' + m * interval '1 minute' FROM generate_series(70, 170) m
            WHERE m <> 100) x;
-- an annotation of another kind is never touched
INSERT INTO sensor_annotations (sensor_id, starts_at, ends_at, kind, label)
VALUES (1, now() - interval '2 hours', now() - interval '1 hour', 'gap', 'collector down');

CREATE VIEW pg_temp.got AS
  SELECT starts_at, ends_at, kind, boundary FROM sensor_annotations;
CREATE TEMP TABLE want AS
  SELECT * FROM (VALUES
    (now() - interval '3 days', now() - interval '3 days' + interval '10 minutes', 'unreachable', false),
    (now() - interval '3 days' + interval '10 minutes', now() - interval '3 hours', 'unreachable', false),
    (now() - interval '3 hours' + interval '60 minutes', now() - interval '3 hours' + interval '70 minutes',
     'unreachable', false),
    (now() - interval '10 minutes', NULL::timestamptz, 'unreachable', false),
    (now() - interval '2 hours', now() - interval '1 hour', 'gap', false)) w (starts_at, ends_at, kind, boundary);

-- --- 1. a full run -------------------------------------------------------------------------
SELECT pg_temp.check_n('full run', refresh_contact_periods(1, true), 4);
SELECT pg_temp.check_rows('full run', 'SELECT * FROM pg_temp.got', 'SELECT * FROM want');

-- --- 2. the default run recomputes the tail only -------------------------------------------
-- (from the last heartbeat before two days ago: the gaps from 3 days ago + 10 min on)
SELECT pg_temp.check_n('default run', refresh_contact_periods(1), 3);
SELECT pg_temp.check_rows('default run', 'SELECT * FROM pg_temp.got', 'SELECT * FROM want');

-- --- 3. contact returns ----------------------------------------------------------------------
INSERT INTO sensor_heartbeats (sensor_id, received_at, sensor_ts) VALUES (1, now(), now());
UPDATE want SET ends_at = now() WHERE ends_at IS NULL AND kind = 'unreachable';
SELECT pg_temp.check_n('contact returns', refresh_contact_periods(1), 3);
SELECT pg_temp.check_rows('contact returns', 'SELECT * FROM pg_temp.got', 'SELECT * FROM want');

DO $$ BEGIN
  RAISE NOTICE 'contact_periods: PASS (gaps > 3 min, a missed heartbeat is no gap, ongoing gap open then closed, tail-only recompute = full run, other kinds untouched)';
END $$;

ROLLBACK;
