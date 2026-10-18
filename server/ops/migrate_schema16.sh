#!/bin/sh
# Server schema 15 -> 16 on the box, as the database owner (after ./backup.sh,
# copied off the box):
#
#   cd ~/wifi-audit/server && sh ops/migrate_schema16.sh 2>&1 | tee ~/migrate16-$(date -u +%Y%m%dT%H%MZ).log
#
# refresh_runs: one row per refresher step (the refresh-duration series of the
# system evaluation). Nothing existing changes; the api and the refresher keep
# running. Deploy the new refresher afterwards (docker compose build api &&
# docker compose up -d api refresher). MIGRATE_DB=<name> runs it against
# another database of the same server (the scratch rehearsal, ops/scratch_db.sh).
#   1. checks schema_version 15
#   2. ops/migrate_schema16.sql - ONE transaction
#   3. schema.sql (schema 16; idempotent)
#   4. every SQL fixture test
# Rollback: ops/rollback_schema16.sql.
set -eu
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "migrate_schema16: no .env next to docker-compose.yml" >&2; exit 1; }
set -a; . ./.env; set +a
DB="${MIGRATE_DB:-$POSTGRES_DB}"
psql_db() { docker compose exec -T db psql -X -q -U "$POSTGRES_USER" -d "$DB" -v ON_ERROR_STOP=1 "$@"; }

echo "== $(date -u +%FT%TZ) checks (database $DB)"
version=$(echo "SELECT value FROM schema_meta WHERE key = 'schema_version'" | psql_db -At)
[ "$version" = 15 ] || { echo "migrate_schema16: schema_version is '$version', expected 15" >&2; exit 1; }

echo "== $(date -u +%FT%TZ) migration (one transaction)"
psql_db < ops/migrate_schema16.sql

echo "== $(date -u +%FT%TZ) schema.sql (schema 16)"
psql_db < schema.sql

echo "== $(date -u +%FT%TZ) SQL fixture tests"
for t in tests/sql/*_test.sql; do echo "-- $t"; psql_db < "$t"; done

echo "== $(date -u +%FT%TZ) check"
psql_db -At -c "SELECT 'schema_version ' || value FROM schema_meta WHERE key = 'schema_version'" \
        -c "SELECT 'refresh_runs rows ' || count(*) FROM refresh_runs"

echo "== $(date -u +%FT%TZ) done - schema 16 (deploy the refresher next)"
