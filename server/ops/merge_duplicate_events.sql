-- Schema 10 cleanup: the duplicate Kismet event rows written before schema 10
-- (an episode stored once with its AP and once without a subject, because
-- Kismet re-typed the transmitter - 2026-10-12: the pairs 82/375, 56/367,
-- 368/80). merge_duplicate_kismet_events() (schema.sql) merges each row
-- without a subject into its attributed twin of the same window and deletes
-- it; the kept row keeps or takes over the operator's status and notes, and
-- every merge is in audit_log ('event.merge', with both rows' labels). Run
-- as the database owner, from server/. A dry run by default (rolled back):
--
--   docker compose exec -T db psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 \
--     < ops/merge_duplicate_events.sql
--
-- then, if the list is right, the same with -v apply=1:
--
--   docker compose exec -T db psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 \
--     -v apply=1 < ops/merge_duplicate_events.sql
--
-- Idempotent: a second run lists nothing.
\set ON_ERROR_STOP on
\pset pager off

BEGIN;
DO $$
DECLARE v text := (SELECT value FROM schema_meta WHERE key = 'schema_version');
BEGIN
  IF v::int < 10 THEN
    RAISE EXCEPTION 'merge_duplicate_events: needs schema 10 (found %)', v;
  END IF;
END $$;
SELECT s.name AS sensor, m.*
FROM sensors s CROSS JOIN LATERAL merge_duplicate_kismet_events(s.id) m
ORDER BY s.name, m.kept_id;
\if :{?apply}
COMMIT;
\echo merge_duplicate_events: merged and committed
\else
ROLLBACK;
\echo merge_duplicate_events: dry run, rolled back - run again with -v apply=1 to merge
\endif
