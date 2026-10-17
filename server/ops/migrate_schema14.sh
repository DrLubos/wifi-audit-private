#!/bin/sh
# Server schema 13 -> 14 on the box, as the database owner (after ./backup.sh):
#
#   cd ~/wifi-audit/server && sh ops/migrate_schema14.sh 2>&1 | tee ~/migrate14-$(date -u +%Y%m%dT%H%MZ).log
#
# Live upload, step 1 (docs/live-upload.md): the tables and the one merge of
# the snapshot import and the live ingest. Additive - the running api of
# schema 13 keeps working (it reads none of the new objects), so no image is
# rebuilt for this step.
#   1. checks schema_version 13
#   2. ops/migrate_schema14.sql - ONE transaction: the live-upload tables, the
#      polls / sensor_uploads columns, the annotation kinds, autovacuum
#      settings, ap_evidence(), ingest_stage_create(), ingest_merge(),
#      refresh_contact_periods()
#   3. schema.sql (schema 14; idempotent)
#   4. every SQL fixture test, then the read-only check
set -eu
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "migrate_schema14: no .env next to docker-compose.yml" >&2; exit 1; }
set -a; . ./.env; set +a
psql_db() { docker compose exec -T db psql -X -q -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 "$@"; }

echo "== $(date -u +%FT%TZ) checks"
version=$(echo "SELECT value FROM schema_meta WHERE key = 'schema_version'" | psql_db -At)
[ "$version" = 13 ] || { echo "migrate_schema14: schema_version is '$version', expected 13" >&2; exit 1; }

echo "== $(date -u +%FT%TZ) migration (one transaction)"
psql_db < ops/migrate_schema14.sql

echo "== $(date -u +%FT%TZ) schema.sql (schema 14)"
psql_db < schema.sql

echo "== $(date -u +%FT%TZ) SQL fixture tests"
for t in tests/sql/*_test.sql; do echo "-- $t"; psql_db < "$t"; done

echo "== $(date -u +%FT%TZ) check"
psql_db < tests/sql/check_schema14.sql

echo "== $(date -u +%FT%TZ) done - schema 14 (no restart needed)"
