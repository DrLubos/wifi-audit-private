-- Fixture test of the label constraints (schema.sql, schema 12 and 17): the
-- outcome's reason and the cause of a benign / false-positive label.
--
-- Writes nothing: one transaction, rolled back; a scratch schema first on the
-- search path holds an empty copy of detections with its constraints. Run it
-- as the owner (CREATE SCHEMA):
--
--   docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
--     -v ON_ERROR_STOP=1 < tests/sql/labels_test.sql
--
-- Prints NOTICE "labels: PASS". Accepted: open without reason or cause;
-- confirmed with a reason and no cause; benign / false_positive with a reason
-- and a cause of the list, 'unknown' included; 'other' with a note. Refused:
-- a cause on open or confirmed, benign / false_positive without a cause, a
-- cause outside the list, 'other' without a note (or a blank one).

\set ON_ERROR_STOP on
BEGIN;

DO $$
BEGIN
  CREATE SCHEMA labels_test;
  PERFORM set_config('search_path', 'labels_test, public', true);
  CREATE TABLE detections (LIKE public.detections INCLUDING ALL);
END $$;

DO $$
DECLARE
  ok text[][] := ARRAY[
    ARRAY['open', NULL, NULL, NULL],
    ARRAY['confirmed', 'security_relevant', NULL, NULL],
    ARRAY['benign', 'expected_behaviour', 'kismet_rule', NULL],
    ARRAY['benign', 'accepted_risk', 'unknown', NULL],
    ARRAY['false_positive', 'artefact', 'rssi_floor', NULL],
    ARRAY['benign', 'expected_behaviour', 'duplicate', NULL],
    ARRAY['false_positive', 'rule_defect', 'other', 'Kismet restarted']];
  bad text[][] := ARRAY[
    ARRAY['open', NULL, 'unknown', NULL],
    ARRAY['confirmed', 'staged_test', 'staged_test', NULL],
    ARRAY['benign', 'expected_behaviour', NULL, NULL],
    ARRAY['false_positive', 'artefact', 'moon_phase', NULL],
    ARRAY['false_positive', 'artefact', 'other', NULL],
    ARRAY['benign', 'accepted_risk', 'other', '   ']];
  i integer;
  refused boolean;
BEGIN
  FOR i IN 1 .. array_length(ok, 1) LOOP
    INSERT INTO detections (sensor_id, ts, type, severity, summary, status, status_reason, status_cause, status_note)
    VALUES (1, now() + make_interval(secs => i), 't', 'low', 's', ok[i][1], ok[i][2], ok[i][3], ok[i][4]);
  END LOOP;
  FOR i IN 1 .. array_length(bad, 1) LOOP
    refused := false;
    BEGIN
      INSERT INTO detections (sensor_id, ts, type, severity, summary, status, status_reason, status_cause, status_note)
      VALUES (1, now() - make_interval(secs => i), 't', 'low', 's', bad[i][1], bad[i][2], bad[i][3], bad[i][4]);
    EXCEPTION WHEN check_violation THEN refused := true;
    END;
    IF NOT refused THEN
      RAISE EXCEPTION 'labels: FAIL (accepted status % reason % cause % note %)', bad[i][1], bad[i][2], bad[i][3], bad[i][4];
    END IF;
  END LOOP;
  -- a relabel to open must clear the cause
  refused := false;
  BEGIN
    UPDATE detections SET status = 'open', status_reason = NULL WHERE status_cause = 'kismet_rule';
  EXCEPTION WHEN check_violation THEN refused := true;
  END;
  IF NOT refused THEN RAISE EXCEPTION 'labels: FAIL (open kept a cause)'; END IF;
  RAISE NOTICE 'labels: PASS (reasons per outcome; a cause of the list for benign / false_positive only; other needs a note)';
END $$;

ROLLBACK;
