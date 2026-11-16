#!/bin/sh
# Server schema 20 -> 21 on the box, as the database owner (after ./backup.sh,
# copied off the box):
#
#   cd ~/wifi-audit/server && sh ops/migrate_schema21.sh 2>&1 | tee ~/migrate21-$(date -u +%Y%m%dT%H%MZ).log
#
# ingest_state.taken_from / taken_since: the dirty mark a refresh has taken
# stays in the table until that refresh is done, so a refresher that dies in
# the middle of one no longer loses the window (app/refresher.py). Two
# nullable columns on a one-row-per-sensor table; nothing is rewritten. The
# running (schema-20) refresher keeps working; deploy the new refresher
# afterwards. MIGRATE_DB=<name> runs it against another database of the same
# server (the scratch rehearsal, ops/scratch_db.sh).
#   1. checks schema_version 20
#   2. ops/migrate_schema21.sql - ONE transaction
#   3. schema.sql (schema 21; idempotent)
#   4. every SQL fixture test
#   5. a check: the version, the two columns, no mark taken
# Rollback: ops/rollback_schema21.sql (the schema-20 refresher first).
set -eu
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "migrate_schema21: no .env next to docker-compose.yml" >&2; exit 1; }
set -a; . ./.env; set +a
DB="${MIGRATE_DB:-$POSTGRES_DB}"
psql_db() { docker compose exec -T db psql -X -q -U "$POSTGRES_USER" -d "$DB" -v ON_ERROR_STOP=1 "$@"; }

echo "== $(date -u +%FT%TZ) checks (database $DB)"
version=$(echo "SELECT value FROM schema_meta WHERE key = 'schema_version'" | psql_db -At)
[ "$version" = 20 ] || { echo "migrate_schema21: schema_version is '$version', expected 20" >&2; exit 1; }

echo "== $(date -u +%FT%TZ) migration (one transaction)"
psql_db < ops/migrate_schema21.sql

echo "== $(date -u +%FT%TZ) schema.sql (schema 21)"
psql_db < schema.sql

echo "== $(date -u +%FT%TZ) SQL fixture tests"
for t in tests/sql/*_test.sql; do echo "-- $t"; psql_db < "$t"; done

echo "== $(date -u +%FT%TZ) check"
psql_db -At -c "SELECT 'schema_version ' || value FROM schema_meta WHERE key = 'schema_version'" \
        -c "SELECT 'ingest_state columns: ' || string_agg(column_name, ', ' ORDER BY ordinal_position) FROM information_schema.columns WHERE table_name = 'ingest_state'" \
        -c "SELECT 'marks taken: ' || count(taken_from) || ' of ' || count(*) || ' sensor(s)' FROM ingest_state"

echo "== $(date -u +%FT%TZ) done - schema 21 (deploy the refresher next)"
