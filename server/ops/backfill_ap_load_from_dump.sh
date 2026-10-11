#!/bin/sh
# Backfill ap_load_hourly (schema 8) from a pg_dump taken before the schema-7
# migration, whose observations still have the per-poll n_clients /
# qbss_stations / util_pct. Run by the developer on the box, after
# ops/migrate_schema8.sh:
#
#   cd ~/wifi-audit/server
#   sh ops/backfill_ap_load_from_dump.sh backups/keep/wifi_audit-20261003T103952Z.dump [SENSOR] \
#     2>&1 | tee ~/backfill8-dump-$(date -u +%Y%m%dT%H%MZ).log
#
# SENSOR = sensors.name the dump's rows belong to (default pi-fri). The dump's
# observations table is streamed through pg_restore (inside the db container,
# reading the file from stdin) and ops/dump_load_samples.py into one
# transaction that calls load_ap_load_samples(); nothing is restored into a
# table, the dump is only read. Idempotent: a second run, or the later
# re-import of a snapshot covering the same hours, changes no row (the row
# with more polls wins). Measured on the box (2026-10-11, read-only): the
# channel attribution takes ~5.5 s per day of samples; expect a few minutes
# for the ~16 days in the 2026-10-03 dump, plus pg_restore reading ~110 MB.
# The dashboard does not read ap_load_hourly before API v2, so no
# refresh_rollups() here; the next import refreshes as usual.
set -eu
[ $# -ge 1 ] || { echo "usage: sh ops/backfill_ap_load_from_dump.sh DUMP [SENSOR]" >&2; exit 2; }
[ -f "$1" ] || { echo "backfill: no such file: $1" >&2; exit 1; }
dump=$(cd "$(dirname "$1")" && pwd)/$(basename "$1")
sensor=${2:-pi-fri}
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "backfill: no .env next to docker-compose.yml" >&2; exit 1; }
set -a; . ./.env; set +a
psql_db() { docker compose exec -T db psql -X -q -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 "$@"; }

echo "== $(date -u +%FT%TZ) checks"
version=$(echo "SELECT value FROM schema_meta WHERE key = 'schema_version'" | psql_db -At)
[ "$version" = 8 ] || { echo "backfill: schema_version is '$version', expected 8" >&2; exit 1; }
sid=$(psql_db -At -v sensor="$sensor" <<'EOF'
SELECT id FROM sensors WHERE name = :'sensor';
EOF
)
[ -n "$sid" ] || { echo "backfill: unknown sensor '$sensor'" >&2; exit 1; }
echo "dump $dump, sensor $sensor (id $sid)"

summary() {
  psql_db -c "SELECT source, count(*) AS rows, sum(polls) AS polls, min(hour) AS first_hour,
                     max(hour) AS last_hour
              FROM ap_load_hourly WHERE sensor_id = $sid GROUP BY 1 ORDER BY 1"
}
echo "== $(date -u +%FT%TZ) ap_load_hourly before"
summary

echo "== $(date -u +%FT%TZ) backfill (one transaction)"
docker compose exec -T db pg_restore --data-only --table=observations < "$dump" \
  | python3 ops/dump_load_samples.py --sensor-id "$sid" \
  | psql_db

echo "== $(date -u +%FT%TZ) ap_load_hourly after"
summary
psql_db -c "ANALYZE ap_load_hourly"
echo "== $(date -u +%FT%TZ) done - next: re-import buffer-snapshot-20261010.db with the schema-8 importer"
