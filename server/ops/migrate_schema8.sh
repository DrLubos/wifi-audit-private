#!/bin/sh
# Server schema 7 -> 8 on the box, as the database owner. Run by the developer
# after ./backup.sh (and the dump copied off the box):
#
#   cd ~/wifi-audit/server && sh ops/migrate_schema8.sh 2>&1 | tee ~/migrate8-$(date -u +%Y%m%dT%H%MZ).log
#
#   1. checks schema_version 7
#   2. ops/migrate_schema8.sql - ONE transaction: the empty table
#      ap_load_hourly, ap_channel_at(), load_ap_load_samples(); additive, no
#      existing table is locked
#   3. schema.sql (schema 8: functions, views; idempotent)
#   4. the SQL fixture tests (scratch schema, rolled back)
# Steps 3-4 run only after step 2 committed; each is safe to re-run.
# Then fill the table: ops/backfill_ap_load_from_dump.sh and a re-import of
# the kept 2026-10-10 snapshot (server/docs/dashboard-v2/PLAN.md, phase 1).
set -eu
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "migrate_schema8: no .env next to docker-compose.yml" >&2; exit 1; }
set -a; . ./.env; set +a
psql_db() { docker compose exec -T db psql -X -q -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 "$@"; }

echo "== $(date -u +%FT%TZ) checks"
version=$(echo "SELECT value FROM schema_meta WHERE key = 'schema_version'" | psql_db -At)
[ "$version" = 7 ] || { echo "migrate_schema8: schema_version is '$version', expected 7" >&2; exit 1; }

echo "== $(date -u +%FT%TZ) migration (one transaction)"
psql_db < ops/migrate_schema8.sql

echo "== $(date -u +%FT%TZ) schema.sql (schema 8)"
psql_db < schema.sql

echo "== $(date -u +%FT%TZ) SQL fixture tests"
for t in tests/sql/*_test.sql; do echo "-- $t"; psql_db < "$t"; done

echo "== $(date -u +%FT%TZ) done - schema 8; ap_load_hourly is empty until the backfill and the next import"
