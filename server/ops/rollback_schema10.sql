-- Server schema 10 -> 9: removes what ops/migrate_schema10.sql and the
-- schema-10 schema.sql added. ONE transaction. Run as the database owner,
-- from server/:
--
--   docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 < ops/rollback_schema10.sql
--
-- Then apply the schema.sql of the last schema-9 commit (its functions read
-- devices.type again) and refresh in full:
--   git show 535103c:server/schema.sql | docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1
--   docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c 'SELECT s.id, r.* FROM sensors s CROSS JOIN LATERAL refresh_rollups(s.id, NULL) r'
--
-- Nothing of the operator's is lost; merged duplicate events stay merged
-- (audit_log has the merged rows' labels). The re-typed APs leave the AP
-- tables again with the schema-9 refresh.
\set ON_ERROR_STOP on
\pset pager off

BEGIN;
DO $$
DECLARE v text := (SELECT value FROM schema_meta WHERE key = 'schema_version');
BEGIN
  IF v IS DISTINCT FROM '10' THEN
    RAISE EXCEPTION 'rollback_schema10: expected schema_version 10, found %', v;
  END IF;
END $$;
DROP TRIGGER IF EXISTS devices_ever_ap ON devices;
DROP FUNCTION IF EXISTS devices_ever_ap();
DROP FUNCTION IF EXISTS merge_duplicate_kismet_events(integer);
DROP INDEX IF EXISTS devices_ever_ap;
-- ap_inventory reads the column: the schema-9 schema.sql recreates it
DROP VIEW IF EXISTS ap_inventory;
ALTER TABLE devices DROP COLUMN ever_ap;
UPDATE schema_meta SET value = '9' WHERE key = 'schema_version';
COMMIT;
