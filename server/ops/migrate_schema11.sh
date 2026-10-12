#!/bin/sh
# Server schema 10 -> 11 on the box, as the database owner. Run by the developer
# after ./backup.sh (and the dump copied off the box), with the antenna of
# pi-fri:
#
#   cd ~/wifi-audit/server && ANTENNA_NAME='<name>' ANTENNA_GAIN_DBI=<dBi> \
#     sh ops/migrate_schema11.sh 2>&1 | tee ~/migrate11-$(date -u +%Y%m%dT%H%MZ).log
#
# Optional: ANTENNA_BANDS (default 2.4,5), ANTENNA_CONNECTOR (default unknown),
# ANTENNA_SINCE (default pi-fri's first poll - the antenna of every
# measurement so far).
#
#   1. checks schema_version 10 and the antenna values
#   2. ops/migrate_schema11.sql - ONE transaction: detections.status_reason
#      with its constraint, ap_baselines.hw_from
#   3. schema.sql (schema 11: capture_hardware, sensor_hardware with its
#      trigger, hardware_period(), the hardware-bounded refresh_ap_baselines(),
#      the merge function with the reason; idempotent)
#   4. ops/seed_capture_hardware.sql - the adapter, the antenna, attached
#   5. ops/seed_sensor_history.sql - the corrections of 2026-10-12 - and
#      refresh_events() per sensor (the causes of the sensor events)
#   6. refresh_ap_baselines(sensor, 200, 2) per sensor (as the importer does;
#      fills hw_from, the statistics stay those of one hardware period)
#   7. the SQL fixture tests (scratch schemas, rolled back)
# Steps 3-7 run only after step 2 committed; each is safe to re-run.
set -eu
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "migrate_schema11: no .env next to docker-compose.yml" >&2; exit 1; }
set -a; . ./.env; set +a
: "${ANTENNA_NAME:?migrate_schema11: set ANTENNA_NAME (the antenna on pi-fri)}"
: "${ANTENNA_GAIN_DBI:?migrate_schema11: set ANTENNA_GAIN_DBI (its gain in dBi)}"
ANTENNA_BANDS=${ANTENNA_BANDS:-2.4,5}
ANTENNA_CONNECTOR=${ANTENNA_CONNECTOR:-}
psql_db() { docker compose exec -T db psql -X -q -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 "$@"; }

echo "== $(date -u +%FT%TZ) checks"
version=$(echo "SELECT value FROM schema_meta WHERE key = 'schema_version'" | psql_db -At)
[ "$version" = 10 ] || { echo "migrate_schema11: schema_version is '$version', expected 10" >&2; exit 1; }
if [ -z "${ANTENNA_SINCE:-}" ]; then
  ANTENNA_SINCE=$(echo "SELECT min(p.ts) FROM polls p JOIN sensors s ON s.id = p.sensor_id WHERE s.name = 'pi-fri'" | psql_db -At)
fi
echo "antenna: $ANTENNA_NAME, $ANTENNA_GAIN_DBI dBi, bands $ANTENNA_BANDS, connector '${ANTENNA_CONNECTOR}', since $ANTENNA_SINCE"

echo "== $(date -u +%FT%TZ) migration (one transaction)"
psql_db < ops/migrate_schema11.sql

echo "== $(date -u +%FT%TZ) schema.sql (schema 11)"
psql_db < schema.sql

echo "== $(date -u +%FT%TZ) capture hardware"
psql_db -v antenna_name="$ANTENNA_NAME" -v antenna_gain_dbi="$ANTENNA_GAIN_DBI" \
        -v antenna_bands="$ANTENNA_BANDS" -v antenna_connector="$ANTENNA_CONNECTOR" \
        -v antenna_since="$ANTENNA_SINCE" < ops/seed_capture_hardware.sql

echo "== $(date -u +%FT%TZ) sensor history corrections, events"
psql_db < ops/seed_sensor_history.sql
psql_db -c 'SELECT s.id AS sensor_id, r.* FROM sensors s CROSS JOIN LATERAL refresh_events(s.id) r'

echo "== $(date -u +%FT%TZ) RSSI baselines inside one hardware period"
psql_db -c '\timing on' \
        -c 'SELECT s.id AS sensor_id, refresh_ap_baselines(s.id, 200, 2) AS baselines FROM sensors s' \
        -c 'SELECT sensor_id, hw_from, count(*) FILTER (WHERE rssi_median IS NOT NULL) AS with_statistics, count(*) FROM ap_baselines GROUP BY 1, 2 ORDER BY 1, 2'

echo "== $(date -u +%FT%TZ) SQL fixture tests"
for t in tests/sql/*_test.sql; do echo "-- $t"; psql_db < "$t"; done

echo "== $(date -u +%FT%TZ) done - schema 11. Next: WRITE_TOKEN in .env, rebuild the api, then the frontend"
