#!/bin/sh
# Server schema 17 -> 18 on the box, as the database owner (after ./backup.sh,
# copied off the box):
#
#   cd ~/wifi-audit/server && sh ops/migrate_schema18.sh 2>&1 | tee ~/migrate18-$(date -u +%Y%m%dT%H%MZ).log
#
# Configuration delivery (docs/config-delivery.md 9): sensor_config's hash and
# delivery fields, sensor_config_attempts, sensor_status.capabilities,
# polls.config_version. Deploy the new api right afterwards (docker compose
# build api && docker compose up -d api refresher). MIGRATE_DB=<name> runs it against
# another database of the same server (the scratch rehearsal, ops/scratch_db.sh).
#   1. checks schema_version 17
#   2. ops/migrate_schema18.sql - ONE transaction
#   3. schema.sql (schema 18; idempotent)
#   4. every SQL fixture test
# Rollback: ops/rollback_schema18.sql.
set -eu
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "migrate_schema18: no .env next to docker-compose.yml" >&2; exit 1; }
set -a; . ./.env; set +a
DB="${MIGRATE_DB:-$POSTGRES_DB}"
psql_db() { docker compose exec -T db psql -X -q -U "$POSTGRES_USER" -d "$DB" -v ON_ERROR_STOP=1 "$@"; }

echo "== $(date -u +%FT%TZ) checks (database $DB)"
version=$(echo "SELECT value FROM schema_meta WHERE key = 'schema_version'" | psql_db -At)
[ "$version" = 17 ] || { echo "migrate_schema18: schema_version is '$version', expected 17" >&2; exit 1; }

echo "== $(date -u +%FT%TZ) migration (one transaction)"
psql_db < ops/migrate_schema18.sql

echo "== $(date -u +%FT%TZ) schema.sql (schema 18)"
psql_db < schema.sql

echo "== $(date -u +%FT%TZ) SQL fixture tests"
for t in tests/sql/*_test.sql; do echo "-- $t"; psql_db < "$t"; done

echo "== $(date -u +%FT%TZ) check"
psql_db -At -c "SELECT 'schema_version ' || value FROM schema_meta WHERE key = 'schema_version'" \
        -c "SELECT 'sensor_config_attempts ' || count(*) FROM sensor_config_attempts" \
        -c "SELECT 'polls with config_version ' || count(config_version) FROM polls"

echo "== $(date -u +%FT%TZ) done - schema 18 (deploy the api next)"
