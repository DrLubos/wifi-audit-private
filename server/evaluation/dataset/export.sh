#!/usr/bin/env bash
# Dataset overview for the thesis - read-only.
#
#   evaluation/dataset/export.sh [--from 2026-09-15] [--to 2026-10-24] [--sensor pi-fri] [--out DIR]
#
# Run on the box from server/: the SQL files as the read-only role claude_ro
# inside the db container -> one CSV each, then python3 -m evaluation.dataset
# renders dataset.md from them. --from / --to are UTC (default: all data up to
# now). Default DIR: ~/dataset-exports/<sensor>_<UTC now>/. README.md: the definitions.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
FROM=2026-09-01 TO=$(date -u +%FT%TZ) SENSOR=pi-fri OUT=""
while [[ $# -gt 0 ]]; do
  case $1 in
    --from) FROM=$2; shift 2 ;;
    --to) TO=$2; shift 2 ;;
    --sensor) SENSOR=$2; shift 2 ;;
    --out) OUT=$2; shift 2 ;;
    -h | --help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option $1 (--help)" >&2; exit 2 ;;
  esac
done
[[ $SENSOR =~ ^[A-Za-z0-9._-]+$ ]] || { echo "--sensor: a sensor name" >&2; exit 2; }
FROM=$(date -u -d "$FROM" +%FT%TZ) || exit 2
TO=$(date -u -d "$TO" +%FT%TZ) || exit 2
[[ $FROM < $TO ]] || { echo "--from must be before --to" >&2; exit 2; }
[[ -f docker-compose.yml ]] || { echo "run from server/ (docker-compose.yml)" >&2; exit 2; }
OUT=${OUT:-$HOME/dataset-exports/${SENSOR}_$(date -u +%Y%m%dT%H%MZ)}
mkdir -p "$OUT"

for m in period outages boundaries aps events alerts; do
  docker compose exec -T db sh -c 'exec psql -U claude_ro -d "$POSTGRES_DB" -X -q -v ON_ERROR_STOP=1 "$@"' sh \
    -v sensor="$SENSOR" -v from="$FROM" -v to="$TO" -f - <"$HERE/$m.sql" >"$OUT/$m.csv"
  echo "$m.csv: $(($(wc -l <"$OUT/$m.csv") - 1)) rows"
done
(cd "$HERE/../.." && python3 -m evaluation.dataset "$OUT" --sensor "$SENSOR" --from "$FROM" --to "$TO")
{
  echo "exported_at=$(date -u +%FT%TZ)"
  echo "sensor=$SENSOR from=$FROM to=$TO"
  echo "git=$(git -C "$HERE" rev-parse --short HEAD 2>/dev/null || echo unknown)"
} >"$OUT/export.txt"
echo "-> $OUT"
