#!/bin/sh
# Server schema 8 -> 9 on the box, as the database owner. Run by the developer
# after ./backup.sh (and the dump copied off the box):
#
#   cd ~/wifi-audit/server && sh ops/migrate_schema9.sh 2>&1 | tee ~/migrate9-$(date -u +%Y%m%dT%H%MZ).log
#
#   1. checks schema_version 8
#   2. ops/migrate_schema9.sql - ONE transaction: the dashboard v2 tables,
#      detections as the event table (status from acked), new rollup columns
#   3. schema.sql (schema 9: functions, views; idempotent)
#   4. ops/seed_sensor_history.sql (annotations + capture-config history of
#      pi-fri; idempotent)
#   5. VACUUM (ANALYZE) of the changed tables
#   6. a FULL refresh_rollups(sensor, NULL) per sensor - the new hourly
#      columns need every hour - which also derives the events
#   7. the SQL fixture tests (scratch schemas, rolled back)
# Steps 3-7 run only after step 2 committed; each is safe to re-run. The api
# must be rebuilt afterwards (the old routers read detections.status).
set -eu
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "migrate_schema9: no .env next to docker-compose.yml" >&2; exit 1; }
set -a; . ./.env; set +a
psql_db() { docker compose exec -T db psql -X -q -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 "$@"; }

echo "== $(date -u +%FT%TZ) checks"
version=$(echo "SELECT value FROM schema_meta WHERE key = 'schema_version'" | psql_db -At)
[ "$version" = 8 ] || { echo "migrate_schema9: schema_version is '$version', expected 8" >&2; exit 1; }

echo "== $(date -u +%FT%TZ) migration (one transaction)"
psql_db < ops/migrate_schema9.sql

echo "== $(date -u +%FT%TZ) schema.sql (schema 9)"
psql_db < schema.sql

echo "== $(date -u +%FT%TZ) sensor history seed"
psql_db < ops/seed_sensor_history.sql

echo "== $(date -u +%FT%TZ) vacuum / analyze"
psql_db -c 'VACUUM (ANALYZE) detections' -c 'VACUUM (ANALYZE) sensors' -c 'VACUUM (ANALYZE) sensor_hourly' \
        -c 'VACUUM (ANALYZE) sensor_summary' -c 'VACUUM (ANALYZE) ap_summary' -c 'ANALYZE ap_rssi_hourly'

echo "== $(date -u +%FT%TZ) full refresh_rollups per sensor (also derives the events)"
psql_db -c '\timing on' \
        -c 'SELECT s.id AS sensor_id, r.* FROM sensors s CROSS JOIN LATERAL refresh_rollups(s.id, NULL) r' \
        -c 'ANALYZE ap_rssi_hourly, sensor_hourly, ap_summary, sensor_summary, ap_config_changes, ap_security_periods, aps, ap_sensors, sensor_periods, detections' \
        -c 'SELECT kind, type, severity, count(*) FROM detections GROUP BY 1, 2, 3 ORDER BY 1, 2, 3' \
        -c 'SELECT kind, starts_at, ends_at, polls FROM sensor_periods ORDER BY starts_at' \
        -c 'SELECT level, label, count(*) FROM ap_summary s JOIN ap_security_periods p USING (sensor_id, device_key) WHERE p.valid_to = '"'"'infinity'"'"' GROUP BY 1, 2 ORDER BY 1, 2'

echo "== $(date -u +%FT%TZ) SQL fixture tests"
for t in tests/sql/*_test.sql; do echo "-- $t"; psql_db < "$t"; done

echo "== $(date -u +%FT%TZ) done - schema 9. Next: rebuild the api; fill in and run ops/seed_account.example.sql"
