#!/bin/sh
# Server schema 14 -> 15 on the box, as the database owner (after ./backup.sh,
# copied off the box):
#
#   cd ~/wifi-audit/server && sh ops/migrate_schema15.sh 2>&1 | tee ~/migrate15-$(date -u +%Y%m%dT%H%MZ).log
#
# An incremental refresh_rollups() (live upload N8, docs/live-upload.md 6.4):
# ap_config_changes becomes a table refreshed per AP. The api and the refresher
# keep running (they read the same columns). MIGRATE_DB=<name> runs it against
# another database of the same server (the scratch rehearsal, ops/scratch_db.sh).
#   1. checks schema_version 14
#   2. ops/migrate_schema15.sql - ONE transaction
#   3. schema.sql (schema 15; idempotent)
#   4. every SQL fixture test, then refresh_rollups() timed twice (incremental)
set -eu
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "migrate_schema15: no .env next to docker-compose.yml" >&2; exit 1; }
set -a; . ./.env; set +a
DB="${MIGRATE_DB:-$POSTGRES_DB}"
psql_db() { docker compose exec -T db psql -X -q -U "$POSTGRES_USER" -d "$DB" -v ON_ERROR_STOP=1 "$@"; }

echo "== $(date -u +%FT%TZ) checks (database $DB)"
version=$(echo "SELECT value FROM schema_meta WHERE key = 'schema_version'" | psql_db -At)
[ "$version" = 14 ] || { echo "migrate_schema15: schema_version is '$version', expected 14" >&2; exit 1; }

echo "== $(date -u +%FT%TZ) migration (one transaction)"
psql_db < ops/migrate_schema15.sql

echo "== $(date -u +%FT%TZ) schema.sql (schema 15)"
psql_db < schema.sql

echo "== $(date -u +%FT%TZ) SQL fixture tests"
for t in tests/sql/*_test.sql; do echo "-- $t"; psql_db < "$t"; done

echo "== $(date -u +%FT%TZ) refresh_rollups, incremental, timed (twice)"
for i in 1 2; do
  psql_db -c '\timing on' -c "SELECT s.id, r.from_hour, r.ap_hours, r.aps FROM sensors s, refresh_rollups(s.id, now() - interval '1 hour') r WHERE EXISTS (SELECT 1 FROM sensor_summary ss WHERE ss.sensor_id = s.id)"
done

echo "== $(date -u +%FT%TZ) done - schema 15 (no restart needed)"
