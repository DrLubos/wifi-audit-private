#!/bin/sh
# Server schema 16 -> 17 on the box, as the database owner (after ./backup.sh,
# copied off the box):
#
#   cd ~/wifi-audit/server && sh ops/migrate_schema17.sh 2>&1 | tee ~/migrate17-$(date -u +%Y%m%dT%H%MZ).log
#
# detections.status_cause: the cause of a benign / false-positive label (RQ2).
# Refused while such labels exist. Deploy the new api right afterwards
# (docker compose build api && docker compose up -d api refresher) and the
# frontend (its label dialog sends the cause). MIGRATE_DB=<name> runs it against
# another database of the same server (the scratch rehearsal, ops/scratch_db.sh).
#   1. checks schema_version 16
#   2. ops/migrate_schema17.sql - ONE transaction
#   3. schema.sql (schema 17; idempotent)
#   4. every SQL fixture test
# Rollback: ops/rollback_schema17.sql.
set -eu
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "migrate_schema17: no .env next to docker-compose.yml" >&2; exit 1; }
set -a; . ./.env; set +a
DB="${MIGRATE_DB:-$POSTGRES_DB}"
psql_db() { docker compose exec -T db psql -X -q -U "$POSTGRES_USER" -d "$DB" -v ON_ERROR_STOP=1 "$@"; }

echo "== $(date -u +%FT%TZ) checks (database $DB)"
version=$(echo "SELECT value FROM schema_meta WHERE key = 'schema_version'" | psql_db -At)
[ "$version" = 16 ] || { echo "migrate_schema17: schema_version is '$version', expected 16" >&2; exit 1; }

echo "== $(date -u +%FT%TZ) migration (one transaction)"
psql_db < ops/migrate_schema17.sql

echo "== $(date -u +%FT%TZ) schema.sql (schema 17)"
psql_db < schema.sql

echo "== $(date -u +%FT%TZ) SQL fixture tests"
for t in tests/sql/*_test.sql; do echo "-- $t"; psql_db < "$t"; done

echo "== $(date -u +%FT%TZ) check"
psql_db -At -c "SELECT 'schema_version ' || value FROM schema_meta WHERE key = 'schema_version'" \
        -c "SELECT 'labels with a cause ' || count(status_cause) || ' of ' || count(*) FILTER (WHERE status <> 'open') FROM detections"

echo "== $(date -u +%FT%TZ) done - schema 17 (deploy the api next)"
