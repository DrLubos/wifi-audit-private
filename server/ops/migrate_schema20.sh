#!/bin/sh
# Server schema 19 -> 20 on the box, as the database owner (after ./backup.sh,
# copied off the box):
#
#   cd ~/wifi-audit/server && sh ops/migrate_schema20.sh 2>&1 | tee ~/migrate20-$(date -u +%Y%m%dT%H%MZ).log
#
# Discard mode (docs/discard-mode.md): two new tables, the annotation kind
# 'excluded', the functions in schema.sql. Nothing existing is rewritten.
# Deploy the api, the refresher and the frontend afterwards. MIGRATE_DB=<name>
# runs it against another database of the same server (the scratch rehearsal,
# ops/scratch_db.sh).
#   1. checks schema_version 19
#   2. ops/migrate_schema20.sql - ONE transaction
#   3. schema.sql (schema 20; idempotent)
#   4. every SQL fixture test (tests/sql/discard_test.sql among them)
#   5. a check: the version, the tables, the periods unchanged (no window yet)
# Rollback: ops/rollback_schema20.sql.
set -eu
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "migrate_schema20: no .env next to docker-compose.yml" >&2; exit 1; }
set -a; . ./.env; set +a
DB="${MIGRATE_DB:-$POSTGRES_DB}"
psql_db() { docker compose exec -T db psql -X -q -U "$POSTGRES_USER" -d "$DB" -v ON_ERROR_STOP=1 "$@"; }

echo "== $(date -u +%FT%TZ) checks (database $DB)"
version=$(echo "SELECT value FROM schema_meta WHERE key = 'schema_version'" | psql_db -At)
[ "$version" = 19 ] || { echo "migrate_schema20: schema_version is '$version', expected 19" >&2; exit 1; }

echo "== $(date -u +%FT%TZ) migration (one transaction)"
psql_db < ops/migrate_schema20.sql

echo "== $(date -u +%FT%TZ) schema.sql (schema 20)"
psql_db < schema.sql

echo "== $(date -u +%FT%TZ) SQL fixture tests"
for t in tests/sql/*_test.sql; do echo "-- $t"; psql_db < "$t"; done

echo "== $(date -u +%FT%TZ) check"
psql_db -At -c "SELECT 'schema_version ' || value FROM schema_meta WHERE key = 'schema_version'" \
        -c "SELECT 'windows ' || count(*) FROM sensor_discard_windows" \
        -c "SELECT 'annotation kinds have excluded: ' || (pg_get_constraintdef(oid) LIKE '%excluded%') FROM pg_constraint WHERE conname = 'sensor_annotations_kind_check'"
# the new refresh_sensor_periods() without a window gives the stored periods
# (one transaction, rolled back: the stored rows, then recomputed - the sensor
# is off during the rollout, so no poll moves them in between)
sig="SELECT count(*) || ' ' || coalesce(md5(string_agg(sensor_id || starts_at::text || coalesce(ends_at::text, '') || kind || polls, ',' ORDER BY sensor_id, starts_at, kind)), '') FROM sensor_periods;"
periods=$(printf '%s\n' "BEGIN;" "$sig" "SELECT count(refresh_sensor_periods(id)) FROM sensors;" "$sig" "ROLLBACK;" | psql_db -At)
before=$(echo "$periods" | sed -n 1p); after=$(echo "$periods" | sed -n 3p)
echo "sensor_periods stored: $before; recomputed: $after"
[ "$before" = "$after" ] || { echo "migrate_schema20: the periods changed without a window" >&2; exit 1; }

echo "== $(date -u +%FT%TZ) done - schema 20 (deploy the api, the refresher and the frontend next)"
