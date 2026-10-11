-- Server schema 8 -> 7: removes what ops/migrate_schema8.sql added. ONE
-- transaction. Run as the database owner:
--
--   docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 < ops/rollback_schema8.sql
--
-- Then apply the schema.sql of the last schema-7 commit (the current one
-- declares 8 and would re-create the table):
--   git show 8478e2e:server/schema.sql | docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1
-- and use the importer of that commit too: the schema-8 importer writes
-- ap_load_hourly.
--
-- Lost: the rows of ap_load_hourly. They can be loaded again - the backfill
-- from the kept dump and snapshots, and collector buffer v7 rows from any
-- snapshot taken within the Pi's 14-day retention.
\set ON_ERROR_STOP on
\pset pager off

BEGIN;
DO $$
DECLARE v text := (SELECT value FROM schema_meta WHERE key = 'schema_version');
BEGIN
  IF v IS DISTINCT FROM '8' THEN
    RAISE EXCEPTION 'rollback_schema8: expected schema_version 8, found %', v;
  END IF;
END $$;
SELECT count(*) AS ap_load_hourly_rows_dropped FROM ap_load_hourly;
DROP FUNCTION load_ap_load_samples(integer);
DROP FUNCTION ap_channel_at(integer, text, timestamptz);
DROP TABLE ap_load_hourly;
UPDATE schema_meta SET value = '7' WHERE key = 'schema_version';
COMMIT;
