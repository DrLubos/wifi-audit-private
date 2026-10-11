#!/bin/sh
# Restore, from a pg_dump taken before the schema-7 migration, the observation
# rows of devices that were not APs at the migration (2026-10-03) but are APs
# on the server now - the migration copied AP rows only, and observations_v5
# is dropped. Run by the developer on the box:
#
#   cd ~/wifi-audit/server
#   sh ops/restore_observations_from_dump.sh backups/keep/wifi_audit-20261003T103952Z.dump [SENSOR] \
#     2>&1 | tee ~/restore-obs-$(date -u +%Y%m%dT%H%MZ).log
#
# SENSOR = sensors.name the dump's rows belong to (default pi-fri). The dump is
# read twice through pg_restore (devices, then observations; inside the db
# container, the file on stdin) and ops/dump_observations.py: ONE transaction
# inserts the rows (ON CONFLICT DO NOTHING) and refreshes ap_baselines; after
# COMMIT observations is vacuumed and refresh_rollups() runs from the earliest
# restored ts (~1 min for the whole history). Nothing else is touched;
# re-running inserts nothing. Afterwards (read-only, claude_ro):
#   tests/sql/check_restore_observations.sql and tests/sql/check_schema8.sql
set -eu
[ $# -ge 1 ] || { echo "usage: sh ops/restore_observations_from_dump.sh DUMP [SENSOR]" >&2; exit 2; }
[ -f "$1" ] || { echo "restore: no such file: $1" >&2; exit 1; }
dump=$(cd "$(dirname "$1")" && pwd)/$(basename "$1")
sensor=${2:-pi-fri}
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "restore: no .env next to docker-compose.yml" >&2; exit 1; }
set -a; . ./.env; set +a
psql_db() { docker compose exec -T db psql -X -q -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 "$@"; }

echo "== $(date -u +%FT%TZ) checks"
version=$(echo "SELECT value FROM schema_meta WHERE key = 'schema_version'" | psql_db -At)
case "$version" in
  8|9) ;;
  *) echo "restore: schema_version is '$version', expected 8 or 9" >&2; exit 1 ;;
esac
sid=$(psql_db -At -v sensor="$sensor" <<'EOF'
SELECT id FROM sensors WHERE name = :'sensor';
EOF
)
[ -n "$sid" ] || { echo "restore: unknown sensor '$sensor'" >&2; exit 1; }
echo "dump $dump, sensor $sensor (id $sid)"

echo "== $(date -u +%FT%TZ) observations before"
psql_db -c "SELECT count(*) AS rows, min(ts) AS first_ts, max(ts) AS last_ts FROM observations WHERE sensor_id = $sid"

echo "== $(date -u +%FT%TZ) restore (one transaction), vacuum, refresh_rollups"
{
  docker compose exec -T db pg_restore --data-only --table=devices -f - < "$dump"
  docker compose exec -T db pg_restore --data-only --table=observations -f - < "$dump"
} | python3 ops/dump_observations.py --sensor-id "$sid" | psql_db

echo "== $(date -u +%FT%TZ) observations after"
psql_db -c "SELECT count(*) AS rows, min(ts) AS first_ts, max(ts) AS last_ts FROM observations WHERE sensor_id = $sid"
echo "== $(date -u +%FT%TZ) done"
