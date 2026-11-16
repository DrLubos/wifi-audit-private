-- Fixture test of the refresher's dirty-mark statements (app/refresher.py:
-- SQL_TAKE_MARK, SQL_MARK_DONE, SQL_RESTORE_TAKEN; schema 21). The texts
-- below are the refresher's own, with sensor 1 for its parameter
-- (tests/test_schema_files.py compares them), so the migration's test step
-- runs on the server what the refresher will run.
--
-- Writes nothing: one transaction, rolled back; a scratch schema first on the
-- search path holds an empty copy of ingest_state. Run it as the owner
-- (CREATE SCHEMA):
--
--   docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
--     -v ON_ERROR_STOP=1 < tests/sql/refresher_marks_test.sql
--
-- Prints NOTICE "refresher marks: PASS". Checked: a refresh takes the mark
-- into taken_from and leaves dirty_from free for the next ingest; done clears
-- only taken_from; a mark left in taken_from (the refresh failed, or the
-- refresher died with it) goes back into dirty_from - alone, or as the
-- earliest of it and a newer mark, with the earliest waiting time; a taken
-- mark already there is not overwritten by a later one; a sensor without a
-- mark gives no row and loses nothing.

\set ON_ERROR_STOP on
BEGIN;
SET LOCAL TimeZone = 'UTC';

DO $$
BEGIN
  CREATE SCHEMA refresher_marks_test;
  PERFORM set_config('search_path', 'refresher_marks_test, public', true);
  CREATE TABLE ingest_state (LIKE public.ingest_state INCLUDING DEFAULTS);
END $$;

CREATE FUNCTION refresher_marks_test.check_eq(p_what text, p_got text, p_want text) RETURNS void
LANGUAGE plpgsql AS $f$
BEGIN
  IF p_got IS DISTINCT FROM p_want THEN
    RAISE EXCEPTION 'refresher marks: FAIL (%: got %, want %)', p_what, p_got, p_want;
  END IF;
END $f$;

-- dirty_from | dirty_since | taken_from | taken_since of a sensor ('-' = NULL)
CREATE FUNCTION refresher_marks_test.state(p_sensor integer) RETURNS text
LANGUAGE sql AS $f$
  SELECT concat_ws(' | ', coalesce(dirty_from::text, '-'), coalesce(dirty_since::text, '-'),
                   coalesce(taken_from::text, '-'), coalesce(taken_since::text, '-'))
  FROM ingest_state WHERE sensor_id = p_sensor;
$f$;

DO $$
DECLARE
  v_from  timestamptz;
  v_since timestamptz;
  v_n     integer;
  v_s     integer;
BEGIN
  -- an ingest marked sensor 1 (ingest_merge()); sensor 2 has nothing to refresh
  INSERT INTO ingest_state (sensor_id, dirty_from, dirty_since, dirty_at)
  VALUES (1, '2026-11-16 10:00:00+00', '2026-11-16 10:00:30+00', '2026-11-16 10:00:30+00'),
         (2, NULL, NULL, NULL);

  -- 1. a round starts: no mark of an unfinished refresh
  WITH r AS (
    UPDATE ingest_state SET dirty_from = least(dirty_from, taken_from),
      dirty_since = least(dirty_since, taken_since), taken_from = NULL, taken_since = NULL
    WHERE taken_from IS NOT NULL RETURNING sensor_id, dirty_from)
  SELECT count(*) INTO v_n FROM r;
  PERFORM check_eq('1 nothing to restore', v_n::text, '0');
  PERFORM check_eq('1 state', state(1), '2026-11-16 10:00:00+00 | 2026-11-16 10:00:30+00 | - | -');

  -- 2. the refresh takes the mark
  UPDATE ingest_state i SET taken_from = least(i.taken_from, o.dirty_from),
    taken_since = least(i.taken_since, o.dirty_since), dirty_from = NULL, dirty_since = NULL
  FROM (SELECT sensor_id, dirty_from, dirty_since FROM ingest_state
        WHERE sensor_id = 1 AND dirty_from IS NOT NULL FOR UPDATE) o
  WHERE i.sensor_id = o.sensor_id
  RETURNING i.taken_from AS dirty_from, i.taken_since AS dirty_since INTO v_from, v_since;
  PERFORM check_eq('2 taken from', v_from::text, '2026-11-16 10:00:00+00');
  PERFORM check_eq('2 taken since', v_since::text, '2026-11-16 10:00:30+00');
  PERFORM check_eq('2 state', state(1), '- | - | 2026-11-16 10:00:00+00 | 2026-11-16 10:00:30+00');

  -- 3. an ingest during the refresh marks its own rows (the statement of ingest_merge())
  INSERT INTO ingest_state AS i (sensor_id, dirty_from, dirty_since, dirty_at)
  VALUES (1, '2026-11-16 10:01:00+00', '2026-11-16 10:01:02+00', '2026-11-16 10:01:02+00')
  ON CONFLICT (sensor_id) DO UPDATE SET dirty_from = least(i.dirty_from, EXCLUDED.dirty_from),
    dirty_since = coalesce(i.dirty_since, EXCLUDED.dirty_since), dirty_at = EXCLUDED.dirty_at;
  PERFORM check_eq('3 state', state(1),
                   '2026-11-16 10:01:00+00 | 2026-11-16 10:01:02+00 | 2026-11-16 10:00:00+00 | 2026-11-16 10:00:30+00');

  -- 4. the refresh is done: only the taken mark goes
  UPDATE ingest_state SET taken_from = NULL, taken_since = NULL WHERE sensor_id = 1;
  PERFORM check_eq('4 state', state(1), '2026-11-16 10:01:00+00 | 2026-11-16 10:01:02+00 | - | -');

  -- 5. the next refresh takes the new mark and never finishes (the refresher
  --    dies with it); no ingest since: the next round finds the mark and puts it back
  UPDATE ingest_state i SET taken_from = least(i.taken_from, o.dirty_from),
    taken_since = least(i.taken_since, o.dirty_since), dirty_from = NULL, dirty_since = NULL
  FROM (SELECT sensor_id, dirty_from, dirty_since FROM ingest_state
        WHERE sensor_id = 1 AND dirty_from IS NOT NULL FOR UPDATE) o
  WHERE i.sensor_id = o.sensor_id
  RETURNING i.taken_from AS dirty_from, i.taken_since AS dirty_since INTO v_from, v_since;
  PERFORM check_eq('5 state taken', state(1), '- | - | 2026-11-16 10:01:00+00 | 2026-11-16 10:01:02+00');
  WITH r AS (
    UPDATE ingest_state SET dirty_from = least(dirty_from, taken_from),
      dirty_since = least(dirty_since, taken_since), taken_from = NULL, taken_since = NULL
    WHERE taken_from IS NOT NULL RETURNING sensor_id, dirty_from)
  SELECT count(*), min(sensor_id), min(dirty_from) INTO v_n, v_s, v_from FROM r;
  PERFORM check_eq('5 restored rows', v_n::text, '1');
  PERFORM check_eq('5 restored sensor', v_s::text, '1');
  PERFORM check_eq('5 restored from', v_from::text, '2026-11-16 10:01:00+00');
  PERFORM check_eq('5 state', state(1), '2026-11-16 10:01:00+00 | 2026-11-16 10:01:02+00 | - | -');

  -- 6. an unfinished refresh AND a newer mark: the earliest window and the earliest wait win
  UPDATE ingest_state SET dirty_from = '2026-11-16 10:07:00+00', dirty_since = '2026-11-16 10:07:03+00',
                          taken_from = '2026-11-02 22:00:06+00', taken_since = '2026-11-16 10:05:00+00'
  WHERE sensor_id = 1;
  WITH r AS (
    UPDATE ingest_state SET dirty_from = least(dirty_from, taken_from),
      dirty_since = least(dirty_since, taken_since), taken_from = NULL, taken_since = NULL
    WHERE taken_from IS NOT NULL RETURNING sensor_id, dirty_from)
  SELECT count(*) INTO v_n FROM r;
  PERFORM check_eq('6 state', state(1), '2026-11-02 22:00:06+00 | 2026-11-16 10:05:00+00 | - | -');

  -- 7. a taken mark already there (a restore that did not run) is not overwritten by a later take
  UPDATE ingest_state SET dirty_from = '2026-11-16 10:09:00+00', dirty_since = '2026-11-16 10:09:01+00',
                          taken_from = '2026-11-16 09:00:00+00', taken_since = '2026-11-16 09:00:05+00'
  WHERE sensor_id = 1;
  UPDATE ingest_state i SET taken_from = least(i.taken_from, o.dirty_from),
    taken_since = least(i.taken_since, o.dirty_since), dirty_from = NULL, dirty_since = NULL
  FROM (SELECT sensor_id, dirty_from, dirty_since FROM ingest_state
        WHERE sensor_id = 1 AND dirty_from IS NOT NULL FOR UPDATE) o
  WHERE i.sensor_id = o.sensor_id
  RETURNING i.taken_from AS dirty_from, i.taken_since AS dirty_since INTO v_from, v_since;
  PERFORM check_eq('7 refresh from', v_from::text, '2026-11-16 09:00:00+00');
  PERFORM check_eq('7 waited since', v_since::text, '2026-11-16 09:00:05+00');
  PERFORM check_eq('7 state', state(1), '- | - | 2026-11-16 09:00:00+00 | 2026-11-16 09:00:05+00');
  UPDATE ingest_state SET taken_from = NULL, taken_since = NULL WHERE sensor_id = 1;
  PERFORM check_eq('7 done', state(1), '- | - | - | -');

  -- 8. nothing dirty: the take gives no row and changes nothing (sensor 1 now, sensor 2 always)
  v_from := NULL;
  UPDATE ingest_state i SET taken_from = least(i.taken_from, o.dirty_from),
    taken_since = least(i.taken_since, o.dirty_since), dirty_from = NULL, dirty_since = NULL
  FROM (SELECT sensor_id, dirty_from, dirty_since FROM ingest_state
        WHERE sensor_id = 1 AND dirty_from IS NOT NULL FOR UPDATE) o
  WHERE i.sensor_id = o.sensor_id
  RETURNING i.taken_from AS dirty_from, i.taken_since AS dirty_since INTO v_from, v_since;
  PERFORM check_eq('8 no row', (v_from IS NULL)::text, 'true');
  PERFORM check_eq('8 state', state(1), '- | - | - | -');
  PERFORM check_eq('8 sensor 2 untouched', state(2), '- | - | - | -');

  RAISE NOTICE 'refresher marks: PASS (take, done, restore alone and with a newer mark, an earlier taken mark kept, nothing dirty)';
END $$;

ROLLBACK;
