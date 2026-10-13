#!/bin/sh
# Server schema 12 -> 13 on the box, as the database owner (after ./backup.sh):
#
#   cd ~/wifi-audit/server && ADAPTER_USB_ID=<VID:PID> ADAPTER_MAC=<mac> \
#     sh ops/migrate_schema13.sh 2>&1 | tee ~/migrate13-$(date -u +%Y%m%dT%H%MZ).log
#
# ADAPTER_USB_ID (e.g. 0bda:c811) and ADAPTER_MAC are the identity of pi-fri's
# capture adapter (lsusb; /sys/class/net/wlan1/address on the Pi) - optional,
# empty = not known yet; the sensor will report them itself in the live-upload
# phase.
#   1. checks schema_version 12
#   2. ops/migrate_schema13.sql - ONE transaction: sensor_adapters, the hardware
#      changes recorded by hand become 'hardware_change' history entries,
#      hardware_period() reads them, capture_hardware / sensor_hardware dropped
#   3. schema.sql (schema 13; idempotent)
#   4. refresh_ap_baselines(sensor, 200, 2) per sensor (hw_from follows the new
#      boundary; with no entry every baseline spans the whole history, as before)
#   5. every SQL fixture test, then the read-only check
# Build the api and frontend images BEFORE this (docker compose build api
# frontend - building does not restart them) and `up -d` right after it: the
# running api of schema 12 reads the dropped tables on /settings and /sensors.
set -eu
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "migrate_schema13: no .env next to docker-compose.yml" >&2; exit 1; }
set -a; . ./.env; set +a
psql_db() { docker compose exec -T db psql -X -q -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 "$@"; }

echo "== $(date -u +%FT%TZ) checks"
version=$(echo "SELECT value FROM schema_meta WHERE key = 'schema_version'" | psql_db -At)
[ "$version" = 12 ] || { echo "migrate_schema13: schema_version is '$version', expected 12" >&2; exit 1; }

echo "== $(date -u +%FT%TZ) migration (one transaction)"
psql_db -v adapter_usb_id="${ADAPTER_USB_ID:-}" -v adapter_mac="${ADAPTER_MAC:-}" < ops/migrate_schema13.sql

echo "== $(date -u +%FT%TZ) schema.sql (schema 13)"
psql_db < schema.sql

echo "== $(date -u +%FT%TZ) RSSI baselines"
psql_db -c '\timing on' \
        -c 'SELECT s.id AS sensor_id, refresh_ap_baselines(s.id, 200, 2) AS baselines FROM sensors s' \
        -c 'SELECT sensor_id, hw_from, count(*) FILTER (WHERE rssi_median IS NOT NULL) AS with_statistics, count(*) FROM ap_baselines GROUP BY 1, 2 ORDER BY 1, 2'

echo "== $(date -u +%FT%TZ) SQL fixture tests"
for t in tests/sql/*_test.sql; do echo "-- $t"; psql_db < "$t"; done

echo "== $(date -u +%FT%TZ) check"
psql_db < tests/sql/check_schema13.sql

echo "== $(date -u +%FT%TZ) done - schema 13. Next: docker compose up -d api frontend"
