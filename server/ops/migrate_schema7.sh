#!/bin/sh
# Server schema 6 -> 7 on the box, as the database owner. Run by the developer
# after ./backup.sh (and the dump copied off the box):
#
#   cd ~/wifi-audit/server && sh ops/migrate_schema7.sh 2>&1 | tee ~/migrate7-$(date -u +%Y%m%dT%H%MZ).log
#
#   1. checks schema_version 6 and the free disk space against an estimate
#      (new table + its index + the WAL they generate + the sort spill of the
#      ts-ordered copy, +25 %); stops before touching anything if it is short
#   2. ops/migrate_schema7.sql - ONE transaction (see its header)
#   3. schema.sql (schema 7: functions, views; idempotent)
#   4. VACUUM (ANALYZE) of the new and changed tables - the visibility map the
#      index-only scans need
#   5. refresh_rollups(sensor, now()) per sensor: rebuilds ap_summary (band,
#      cur_*) and the latest hour; the hourly tables keep their rows (the AP
#      rows are unchanged - tests/sql/check_schema7.sql proves it read-only)
#   6. the SQL fixture tests (scratch schema, rolled back)
# Steps 3-6 run only after step 2 committed; each is safe to re-run.
set -eu
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "migrate_schema7: no .env next to docker-compose.yml" >&2; exit 1; }
set -a; . ./.env; set +a
psql_db() { docker compose exec -T db psql -X -q -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 "$@"; }

echo "== $(date -u +%FT%TZ) checks"
version=$(echo "SELECT value FROM schema_meta WHERE key = 'schema_version'" | psql_db -At)
[ "$version" = 6 ] || { echo "migrate_schema7: schema_version is '$version', expected 6" >&2; exit 1; }

# Estimate per AP row (measured layout: 29-char device keys):
#   heap ~125 B (32 B header + 88 B data + line pointer), PK entry ~64 B,
#   WAL ~ heap + index again (wal_level replica logs the new table),
#   sort spill of the ORDER BY ts copy ~ heap.
ap_rows=$(echo "SELECT coalesce(sum(ap_obs), 0) FROM sensor_hourly" | psql_db -At)
need_kb=$(( ap_rows * (125 + 64 + 125 + 64 + 125) / 1024 * 5 / 4 ))
free_kb=$(docker compose exec -T db df -kP /var/lib/postgresql/data | awk 'NR == 2 { print $4 }')
echo "AP observation rows: $ap_rows; estimated peak need: $((need_kb / 1024)) MB; free: $((free_kb / 1024)) MB"
if [ "$free_kb" -lt "$need_kb" ]; then
  echo "migrate_schema7: not enough free disk space - nothing was changed" >&2
  exit 1
fi

echo "== $(date -u +%FT%TZ) migration (one transaction)"
psql_db < ops/migrate_schema7.sql

echo "== $(date -u +%FT%TZ) schema.sql (schema 7)"
psql_db < schema.sql

echo "== $(date -u +%FT%TZ) vacuum / analyze"
psql_db -c 'VACUUM (ANALYZE) observations' -c 'VACUUM (ANALYZE) client_bssids' \
        -c 'VACUUM (ANALYZE) devices' -c 'VACUUM (ANALYZE) ap_baselines'

echo "== $(date -u +%FT%TZ) refresh_rollups per sensor"
psql_db -c '\timing on' \
        -c 'SELECT s.id AS sensor_id, r.* FROM sensors s CROSS JOIN LATERAL refresh_rollups(s.id, now()) r' \
        -c 'ANALYZE ap_rssi_hourly, sensor_hourly, ap_summary, sensor_summary, ap_config_changes'

echo "== $(date -u +%FT%TZ) SQL fixture tests"
for t in tests/sql/*_test.sql; do psql_db < "$t"; done

echo "== $(date -u +%FT%TZ) done - observations_v5 is kept; drop it later with: DROP TABLE observations_v5;"
