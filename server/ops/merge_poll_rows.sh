#!/bin/sh
# Merge AP observation rows of polls the server stored without all of their
# rows, from an extract of the sensor's buffer (the sensor's
# analysis/extract_poll_rows.py). For rows the uploader cannot send again -
# 2026-11-02: after a power cut the buffer gave the row ids of lost polls to
# new rows, the uploader's cursor passed 54 AP observations of polls 123578
# and 123579, and the boot's clock anchor changed later, so a re-send would
# store the two polls' rows twice, one second apart. On the box, from server/,
# after a fresh ./backup.sh:
#
#   sh ops/merge_poll_rows.sh EXTRACT.json [SENSOR]             # dry run, rolled back
#   sh ops/merge_poll_rows.sh --apply EXTRACT.json [SENSOR]     # commit
#
# SENSOR = sensors.name (default pi-fri). ops/poll_rows_sql.py turns the
# extract into ONE transaction: each row gets the ts of THE stored poll with
# its seq and its time on the sensor's clock (none or several: stop), then
# ingest_merge() stores what is missing (AP rows only, ON CONFLICT DO
# NOTHING, discard windows honoured) and marks the sensor for the refresher,
# which rebuilds the rollups from the earliest new row. Nothing else is
# touched; re-running inserts nothing.
set -eu
apply=""
if [ "${1:-}" = "--apply" ]; then apply="-v apply=1"; shift; fi
[ $# -ge 1 ] || { echo "usage: sh ops/merge_poll_rows.sh [--apply] EXTRACT.json [SENSOR]" >&2; exit 2; }
[ -f "$1" ] || { echo "merge_poll_rows: no such file: $1" >&2; exit 1; }
extract=$(cd "$(dirname "$1")" && pwd)/$(basename "$1")
sensor=${2:-pi-fri}
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "merge_poll_rows: no .env next to docker-compose.yml" >&2; exit 1; }
set -a; . ./.env; set +a
psql_db() { docker compose exec -T db psql -X -q -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 "$@"; }

version=$(echo "SELECT value FROM schema_meta WHERE key = 'schema_version'" | psql_db -At)
[ "$version" -ge 15 ] 2>/dev/null || { echo "merge_poll_rows: schema_version is '$version', needs 15 (ingest_merge)" >&2; exit 1; }
sid=$(psql_db -At -v sensor="$sensor" <<'EOF'
SELECT id FROM sensors WHERE name = :'sensor';
EOF
)
[ -n "$sid" ] || { echo "merge_poll_rows: unknown sensor '$sensor'" >&2; exit 1; }
echo "== $(date -u +%FT%TZ) extract $extract, sensor $sensor (id $sid)${apply:+, APPLY}"
script=$(mktemp)
trap 'rm -f "$script"' EXIT
python3 ops/poll_rows_sql.py --sensor-id "$sid" "$extract" > "$script"
# shellcheck disable=SC2086
psql_db $apply < "$script"
echo "== $(date -u +%FT%TZ) done"
