#!/bin/sh
# Server schema 11 -> 12 on the box, as the database owner (after ./backup.sh):
#
#   cd ~/wifi-audit/server && ANTENNA_NAME='<name>' ANTENNA_GAIN_DBI=<dBi> \
#     sh ops/migrate_schema12.sh 2>&1 | tee ~/migrate12-$(date -u +%Y%m%dT%H%MZ).log
#
# ANTENNA_NAME / ANTENNA_GAIN_DBI correct the antenna the schema-11 seed
# stored (empty = keep; the gain must end within -10 .. 30 dBi).
#   1. checks schema_version 11
#   2. ops/migrate_schema12.sql - ONE transaction (the antenna, the three
#      label outcomes with NULL-safe reasons, the plan and gain checks)
#   3. schema.sql (schema 12; idempotent)
#   4. every SQL fixture test, then the read-only check
set -eu
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "migrate_schema12: no .env next to docker-compose.yml" >&2; exit 1; }
set -a; . ./.env; set +a
psql_db() { docker compose exec -T db psql -X -q -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 "$@"; }

echo "== $(date -u +%FT%TZ) checks"
version=$(echo "SELECT value FROM schema_meta WHERE key = 'schema_version'" | psql_db -At)
[ "$version" = 11 ] || { echo "migrate_schema12: schema_version is '$version', expected 11" >&2; exit 1; }

echo "== $(date -u +%FT%TZ) migration (one transaction)"
psql_db -v antenna_name="${ANTENNA_NAME:-}" -v antenna_gain_dbi="${ANTENNA_GAIN_DBI:-}" < ops/migrate_schema12.sql

echo "== $(date -u +%FT%TZ) schema.sql (schema 12)"
psql_db < schema.sql

echo "== $(date -u +%FT%TZ) SQL fixture tests"
for t in tests/sql/*_test.sql; do echo "-- $t"; psql_db < "$t"; done

echo "== $(date -u +%FT%TZ) check"
psql_db < tests/sql/check_schema12.sql

echo "== $(date -u +%FT%TZ) done - schema 12. Next: WRITE_TOKEN in .env, rebuild the api, then the frontend"
