-- Server schema 14 -> 13: the live-upload objects go. ONE transaction, as the
-- database owner, from server/:
--
--   docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 < ops/rollback_schema14.sql
--
-- Then apply the schema.sql of the last schema-13 commit and restore its
-- importer (the schema-14 importer calls ingest_merge(), which is gone):
--   git show 9a33634:server/schema.sql | docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1
--   git checkout 9a33634 -- seed/import_snapshot.py
-- Lost with the rollback: tokens, heartbeats, contact and loss records, the
-- clock corrections and polls.seq / received_at / ts_offset_s (the polls
-- themselves stay). Refused while an ingest token is active - revoke the
-- tokens first (ops/sensor_token.sh), so no sensor keeps uploading into a
-- schema that no longer has the merge.
\set ON_ERROR_STOP on
BEGIN;
DO $$
BEGIN
  IF (SELECT value FROM schema_meta WHERE key = 'schema_version') IS DISTINCT FROM '14' THEN
    RAISE EXCEPTION 'rollback_schema14: expected schema_version 14';
  END IF;
  IF EXISTS (SELECT 1 FROM sensor_tokens WHERE revoked_at IS NULL) THEN
    RAISE EXCEPTION 'rollback_schema14: active ingest tokens - revoke them first';
  END IF;
END $$;

DROP FUNCTION refresh_contact_periods(integer, boolean);
DROP FUNCTION ingest_merge(integer, text);
DROP FUNCTION ingest_stage_create();

CREATE OR REPLACE FUNCTION devices_ever_ap() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'UPDATE' THEN             -- OLD exists only for an update
    IF OLD.ever_ap THEN
      NEW.ever_ap := true;
    END IF;
  END IF;
  NEW.ever_ap := NEW.ever_ap OR NEW.type = 'ap' OR NEW.ssid IS NOT NULL OR NEW.crypt IS NOT NULL
                 OR NEW.adv_channel IS NOT NULL;
  RETURN NEW;
END
$$;
DROP FUNCTION ap_evidence(text, text, text, text);

ALTER TABLE observations RESET (autovacuum_vacuum_insert_threshold, autovacuum_vacuum_insert_scale_factor);
ALTER TABLE devices RESET (autovacuum_vacuum_scale_factor);
ALTER TABLE ap_load_hourly RESET (autovacuum_vacuum_scale_factor);

DELETE FROM sensor_annotations WHERE kind IN ('lost', 'unreachable');
ALTER TABLE sensor_annotations DROP CONSTRAINT sensor_annotations_kind_check;
ALTER TABLE sensor_annotations ADD CONSTRAINT sensor_annotations_kind_check
  CHECK (kind IN ('gap', 'no_frames', 'coverage', 'restart', 'change', 'hardware_change'));

ALTER TABLE sensor_uploads DROP CONSTRAINT sensor_uploads_batch;
ALTER TABLE sensor_uploads DROP COLUMN buffer_id, DROP COLUMN batch_seq, DROP COLUMN sent_at,
  DROP COLUMN attempt, DROP COLUMN replays, DROP COLUMN bytes, DROP COLUMN duration_ms;
ALTER TABLE polls DROP COLUMN seq, DROP COLUMN received_at, DROP COLUMN ts_offset_s;

DROP TABLE sensor_seq_gaps;
DROP TABLE sensor_clock_boots;
DROP TABLE ingest_state;
DROP TABLE sensor_heartbeats;
DROP TABLE sensor_status;
DROP TABLE sensor_tokens;

INSERT INTO schema_meta (key, value) VALUES ('schema_version', '13')
  ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
COMMIT;
