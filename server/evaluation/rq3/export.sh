#!/usr/bin/env bash
# RQ3 continuous vs one-shot: simulated one-shot audits from the stored data - read-only.
#
#   evaluation/rq3/export.sh [--from 2026-09-15] [--to 2026-10-24] [--sensor pi-fri] [--out DIR]
#                            [--n 100] [--durations 900,3600] [--pairs 60] [--gaps 86400,604800] [--seed S]
#                            [--hours 24h|work]
#
# Run on the box from server/. SQL as the read-only role claude_ro inside the
# db container; the sampling and the summaries in python3 -m evaluation.rq3
# (standard library). The window queries run in chunks of 25 with a pause
# between them (each window costs ~1-2 s of cold index reads on the e2-micro).
# --from / --to are UTC (default: the first poll to the start of today).
# --hours work: audits only on weekdays 08:00-17:00 Europe/Bratislava (the
# window inside); 24h (default): at any time. Default DIR:
# ~/rq3-exports/<sensor>_<from>_<to>[_work]/. README.md: the method.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
FROM=2026-09-15 TO=$(date -u +%F) SENSOR=pi-fri OUT="" PAUSE=3 HOURS=24h
PLAN_ARGS=()
while [[ $# -gt 0 ]]; do
  case $1 in
    --from) FROM=$2; shift 2 ;;
    --to) TO=$2; shift 2 ;;
    --sensor) SENSOR=$2; shift 2 ;;
    --out) OUT=$2; shift 2 ;;
    --n | --durations | --pairs | --gaps | --seed | --pair-duration) PLAN_ARGS+=("$1" "$2"); shift 2 ;;
    --hours) HOURS=$2; PLAN_ARGS+=("$1" "$2"); shift 2 ;;
    -h | --help) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option $1 (--help)" >&2; exit 2 ;;
  esac
done
[[ $SENSOR =~ ^[A-Za-z0-9._-]+$ ]] || { echo "--sensor: a sensor name" >&2; exit 2; }
FROM=$(date -u -d "$FROM" +%FT%TZ) || exit 2
TO=$(date -u -d "$TO" +%FT%TZ) || exit 2
[[ $FROM < $TO ]] || { echo "--from must be before --to" >&2; exit 2; }
[[ -f docker-compose.yml ]] || { echo "run from server/ (docker-compose.yml)" >&2; exit 2; }
[[ $HOURS == 24h || $HOURS == work ]] || { echo "--hours: 24h or work" >&2; exit 2; }
SUFFIX=""; [[ $HOURS == work ]] && SUFFIX=_work
OUT=${OUT:-$HOME/rq3-exports/${SENSOR}_${FROM//:/}_${TO//:/}${SUFFIX}}
mkdir -p "$OUT/raw"
PAIR_DUR=3600
for ((i = 0; i < ${#PLAN_ARGS[@]}; i += 2)); do
  [[ ${PLAN_ARGS[i]} == --pair-duration ]] && PAIR_DUR=${PLAN_ARGS[i + 1]}
done

psql_ro() {   # $1 = SQL file, then psql -v options
  local f=$1; shift
  docker compose exec -T db sh -c 'exec psql -U claude_ro -d "$POSTGRES_DB" -X -q -v ON_ERROR_STOP=1 "$@"' sh \
    -v sensor="$SENSOR" -v from="$FROM" -v to="$TO" "$@" -f - <"$f"
}
rq3() { (cd "$HERE/../.." && python3 -m evaluation.rq3 "$@"); }

psql_ro "$HERE/timeline.sql" >"$OUT/timeline.csv"
echo "timeline.csv: $(($(wc -l <"$OUT/timeline.csv") - 1)) collecting polls"
psql_ro "$HERE/reference.sql" >"$OUT/reference.csv"
rq3 plan "$OUT/timeline.csv" --out "$OUT" --pair-duration "$PAIR_DUR" "${PLAN_ARGS[@]}"

for f in "$OUT"/plan/windows_*.txt; do
  b=$(basename "$f" .txt); d=${b#windows_}; d=${d%%_*}
  psql_ro "$HERE/windows.sql" -v starts="$(cat "$f")" -v dur_s="$d" >"$OUT/raw/$b.csv"
  echo "$b: $(($(wc -l <"$OUT/raw/$b.csv") - 1)) rows"; sleep "$PAUSE"
done
for f in "$OUT"/plan/pairs_*_t1.txt; do
  b=$(basename "$f" _t1.txt); g=${b#pairs_}; g=${g%%_*}
  psql_ro "$HERE/pairs.sql" -v t1s="$(cat "$f")" -v t2s="$(cat "${f%_t1.txt}_t2.txt")" \
    -v dur_s="$PAIR_DUR" -v gap_s="$g" >"$OUT/raw/$b.csv"
  echo "$b: $(($(wc -l <"$OUT/raw/$b.csv") - 1)) pairs"; sleep "$PAUSE"
done
rq3 report "$OUT"
{
  echo "exported_at=$(date -u +%FT%TZ)"
  echo "sensor=$SENSOR from=$FROM to=$TO plan=${PLAN_ARGS[*]:-defaults} pair_duration=$PAIR_DUR"
  echo "git=$(git -C "$HERE" rev-parse --short HEAD 2>/dev/null || echo unknown)"
} >"$OUT/export.txt"
echo "-> $OUT"
