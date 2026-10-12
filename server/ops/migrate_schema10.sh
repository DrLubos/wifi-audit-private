#!/bin/sh
# Server schema 9 -> 10 on the box, as the database owner. Run by the developer
# after ./backup.sh (and the dump copied off the box):
#
#   cd ~/wifi-audit/server && sh ops/migrate_schema10.sh 2>&1 | tee ~/migrate10-$(date -u +%Y%m%dT%H%MZ).log
#
#   1. checks schema_version 9
#   2. ops/migrate_schema10.sql - ONE transaction: devices.ever_ap, backfilled
#   3. schema.sql (schema 10: the devices_ever_ap() trigger, every AP set on
#      ever_ap, Kismet episodes attributed to the account AP, the merge
#      function; idempotent)
#   4. VACUUM (ANALYZE) devices
#   5. a FULL refresh_rollups(sensor, NULL) per sensor - the re-typed APs come
#      back into every AP table over their whole history, the Kismet episodes
#      are attributed again
#   6. ops/merge_duplicate_events.sql as a DRY RUN (lists the duplicate pairs;
#      the developer applies it with -v apply=1 afterwards)
#   7. the SQL fixture tests (scratch schemas, rolled back)
# Steps 3-7 run only after step 2 committed; each is safe to re-run.
set -eu
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "migrate_schema10: no .env next to docker-compose.yml" >&2; exit 1; }
set -a; . ./.env; set +a
psql_db() { docker compose exec -T db psql -X -q -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 "$@"; }

echo "== $(date -u +%FT%TZ) checks"
version=$(echo "SELECT value FROM schema_meta WHERE key = 'schema_version'" | psql_db -At)
[ "$version" = 9 ] || { echo "migrate_schema10: schema_version is '$version', expected 9" >&2; exit 1; }

echo "== $(date -u +%FT%TZ) migration (one transaction)"
psql_db < ops/migrate_schema10.sql

echo "== $(date -u +%FT%TZ) schema.sql (schema 10)"
psql_db < schema.sql

echo "== $(date -u +%FT%TZ) vacuum / analyze"
psql_db -c 'VACUUM (ANALYZE) devices'

echo "== $(date -u +%FT%TZ) full refresh_rollups per sensor"
psql_db -c '\timing on' \
        -c 'SELECT s.id AS sensor_id, r.* FROM sensors s CROSS JOIN LATERAL refresh_rollups(s.id, NULL) r' \
        -c 'ANALYZE ap_rssi_hourly, sensor_hourly, ap_summary, sensor_summary, ap_security_periods, aps, ap_sensors, detections' \
        -c 'SELECT count(*) AS aps, count(*) FILTER (WHERE d.type <> '"'"'ap'"'"') AS retyped FROM ap_summary s JOIN devices d USING (sensor_id, device_key)' \
        -c 'SELECT kind, count(*) FILTER (WHERE mac IS NULL AND device_key IS NULL) AS no_subject, count(*) FROM detections GROUP BY 1 ORDER BY 1'

echo "== $(date -u +%FT%TZ) duplicate Kismet events (dry run)"
psql_db < ops/merge_duplicate_events.sql

echo "== $(date -u +%FT%TZ) SQL fixture tests"
for t in tests/sql/*_test.sql; do echo "-- $t"; psql_db < "$t"; done

echo "== $(date -u +%FT%TZ) done - schema 10. Next: the merge with -v apply=1 (ops/merge_duplicate_events.sql), rebuild the api"
