#!/bin/sh
# Server schema 18 -> 19 on the box, as the database owner (after ./backup.sh,
# copied off the box):
#
#   cd ~/wifi-audit/server && sh ops/migrate_schema19.sh 2>&1 | tee ~/migrate19-$(date -u +%Y%m%dT%H%MZ).log
#
# The label cause 'duplicate' (the constraint's list; existing labels stay
# valid). Deploy the api and the frontend afterwards. MIGRATE_DB=<name> runs it against
# another database of the same server (the scratch rehearsal, ops/scratch_db.sh).
#   1. checks schema_version 18
#   2. ops/migrate_schema19.sql - ONE transaction
#   3. schema.sql (schema 19; idempotent)
#   4. every SQL fixture test
# Rollback: ops/rollback_schema19.sql.
set -eu
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "migrate_schema19: no .env next to docker-compose.yml" >&2; exit 1; }
set -a; . ./.env; set +a
DB="${MIGRATE_DB:-$POSTGRES_DB}"
psql_db() { docker compose exec -T db psql -X -q -U "$POSTGRES_USER" -d "$DB" -v ON_ERROR_STOP=1 "$@"; }

echo "== $(date -u +%FT%TZ) checks (database $DB)"
version=$(echo "SELECT value FROM schema_meta WHERE key = 'schema_version'" | psql_db -At)
[ "$version" = 18 ] || { echo "migrate_schema19: schema_version is '$version', expected 18" >&2; exit 1; }

echo "== $(date -u +%FT%TZ) migration (one transaction)"
psql_db < ops/migrate_schema19.sql

echo "== $(date -u +%FT%TZ) schema.sql (schema 19)"
psql_db < schema.sql

echo "== $(date -u +%FT%TZ) SQL fixture tests"
for t in tests/sql/*_test.sql; do echo "-- $t"; psql_db < "$t"; done

echo "== $(date -u +%FT%TZ) check"
psql_db -At -c "SELECT 'schema_version ' || value FROM schema_meta WHERE key = 'schema_version'" \
        -c "SELECT 'cause constraint has duplicate: ' || (pg_get_constraintdef(oid) LIKE '%duplicate%') FROM pg_constraint WHERE conname = 'detections_status_cause'"

echo "== $(date -u +%FT%TZ) done - schema 19 (deploy the api and the frontend next)"
