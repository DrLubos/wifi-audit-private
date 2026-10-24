#!/usr/bin/env bash
# RQ2 false-positive analysis of the labelled events - read-only.
#
#   evaluation/rq2/export.sh [--from 2026-09-15] [--to 2026-11-01] [--sensor pi-fri] [--out DIR]
#
# Run on the box from server/: events.sql as the read-only role claude_ro
# inside the db container -> DIR/events_raw.csv, then python3 -m
# evaluation.rq2 (standard library, Python >= 3.10) -> the RQ2 tables in DIR.
# --from / --to are UTC on the event's start (default: everything up to now).
# Default DIR: ~/rq2-exports/<sensor>_<UTC now>/. README.md: every metric.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
FROM=2026-09-01 TO=$(date -u +%FT%TZ) SENSOR=pi-fri OUT=""
while [[ $# -gt 0 ]]; do
  case $1 in
    --from) FROM=$2; shift 2 ;;
    --to) TO=$2; shift 2 ;;
    --sensor) SENSOR=$2; shift 2 ;;
    --out) OUT=$2; shift 2 ;;
    -h | --help) sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option $1 (--help)" >&2; exit 2 ;;
  esac
done
[[ $SENSOR =~ ^[A-Za-z0-9._-]+$ ]] || { echo "--sensor: a sensor name" >&2; exit 2; }
FROM=$(date -u -d "$FROM" +%FT%TZ) || exit 2
TO=$(date -u -d "$TO" +%FT%TZ) || exit 2
[[ $FROM < $TO ]] || { echo "--from must be before --to" >&2; exit 2; }
[[ -f docker-compose.yml ]] || { echo "run from server/ (docker-compose.yml)" >&2; exit 2; }
OUT=${OUT:-$HOME/rq2-exports/${SENSOR}_$(date -u +%Y%m%dT%H%MZ)}
mkdir -p "$OUT"

docker compose exec -T db sh -c 'exec psql -U claude_ro -d "$POSTGRES_DB" -X -q -v ON_ERROR_STOP=1 "$@"' sh \
  -v sensor="$SENSOR" -v from="$FROM" -v to="$TO" -f - <"$HERE/events.sql" >"$OUT/events_raw.csv"
echo "events_raw.csv: $(($(wc -l <"$OUT/events_raw.csv") - 1)) events"
(cd "$HERE/../.." && python3 -m evaluation.rq2 "$OUT/events_raw.csv" --out "$OUT")
{
  echo "exported_at=$(date -u +%FT%TZ)"
  echo "sensor=$SENSOR from=$FROM to=$TO"
  echo "git=$(git -C "$HERE" rev-parse --short HEAD 2>/dev/null || echo unknown)"
} >"$OUT/export.txt"
echo "-> $OUT"
