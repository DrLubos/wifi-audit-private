#!/usr/bin/env bash
# export.sh - the live-upload pipeline metrics of one sensor and date range as
# CSV, for the system-evaluation chapter (README.md says how each is computed).
#
# Run on the box, from server/ (read-only: psql as claude_ro in the db
# container, `docker compose logs` of the refresher):
#
#   evaluation/pipeline/export.sh --from 2026-10-17T20:46:38Z --to 2026-10-19
#   evaluation/pipeline/export.sh --from 2026-10-18 --to 2026-10-25 --sensor pi-fri --out ~/exports/week1
#   evaluation/pipeline/export.sh ... --refresher-log ~/refresher-archive.log
#
# then copy the folder to the Mac: scp -r google:<out> .
#
# Options: --from / --to (UTC, any `date -d` form; --to exclusive), --sensor
# (default pi-fri), --out (default ~/pipeline-exports/<sensor>_<from>_<to>),
# --gap-s (contact-gap threshold, default 180 = refresh_contact_periods()),
# --refresher-log FILE (only for times before refresh_runs' first row, schema
# 16; default: `docker compose logs refresher` - lost when the container is
# recreated, see README.md).
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
SENSOR=pi-fri FROM="" TO="" OUT="" GAP_S=180 RLOG=""
while [[ $# -gt 0 ]]; do
  case $1 in
    --from) FROM=$2; shift 2 ;;
    --to) TO=$2; shift 2 ;;
    --sensor) SENSOR=$2; shift 2 ;;
    --out) OUT=$2; shift 2 ;;
    --gap-s) GAP_S=$2; shift 2 ;;
    --refresher-log) RLOG=$2; shift 2 ;;
    -h | --help) sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option $1 (--help)" >&2; exit 2 ;;
  esac
done
[[ -n $FROM && -n $TO ]] || { echo "--from and --to are required (--help)" >&2; exit 2; }
[[ $GAP_S =~ ^[0-9]+$ ]] || { echo "--gap-s: seconds" >&2; exit 2; }
[[ $SENSOR =~ ^[A-Za-z0-9._-]+$ ]] || { echo "--sensor: a sensor name" >&2; exit 2; }
FROM=$(date -u -d "$FROM" +%FT%TZ) || exit 2
TO=$(date -u -d "$TO" +%FT%TZ) || exit 2
[[ $FROM < $TO ]] || { echo "--from must be before --to" >&2; exit 2; }
OUT=${OUT:-$HOME/pipeline-exports/${SENSOR}_${FROM//:/}_${TO//:/}}
mkdir -p "$OUT"
[[ -f docker-compose.yml ]] || { echo "run from server/ (docker-compose.yml)" >&2; exit 2; }

psql_ro() {   # $1 = SQL file; read-only role over the db container's socket
  docker compose exec -T db sh -c 'exec psql -U claude_ro -d "$POSTGRES_DB" -X -q -v ON_ERROR_STOP=1 "$@"' sh \
    -v sensor="$SENSOR" -v from="$FROM" -v to="$TO" -v gap_s="$GAP_S" -f - <"$1"
}

for m in polls uploads gaps heartbeats contact_daily contact_gaps summary; do
  psql_ro "$HERE/$m.sql" >"$OUT/$m.csv"
  echo "$m.csv: $(($(wc -l <"$OUT/$m.csv") - 1)) rows"
done

# refresh.csv - refresher steps: refresh_runs (schema 16), and before its
# first row the refresher's log (lost on every container recreate)
has_runs=$(echo "SELECT to_regclass('public.refresh_runs') IS NOT NULL" \
  | docker compose exec -T db sh -c 'exec psql -U claude_ro -d "$POSTGRES_DB" -X -q -At')
first_db=""
[[ $has_runs == t ]] && first_db=$(echo "SELECT to_char(min(started_at) AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.MS\"Z\"') FROM refresh_runs" \
  | docker compose exec -T db sh -c 'exec psql -U claude_ro -d "$POSTGRES_DB" -X -q -At' 2>/dev/null || true)
log_to=$TO
[[ -n $first_db && $first_db < $TO ]] && log_to=$first_db
echo "ts,step,sensor,refresh_from,duration_s,ap_hours,sensor_hours,waited_s,baselines,gaps,next_in_s,ok,error,source" >"$OUT/refresh.csv"
if [[ $FROM < $log_to ]]; then
  if [[ -n $RLOG ]]; then
    cat "$RLOG"
  else
    docker compose logs --no-log-prefix --since "$FROM" --until "$log_to" refresher 2>/dev/null
  fi | awk -v from="$FROM" -v to="$log_to" -v sensor="$SENSOR" '
    BEGIN { OFS = "," }
    $1 ~ /^[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]$/ {
      ts = $1 "T" substr($2, 1, 8) "." substr($2, 10, 3) "Z"
      if (ts < from || ts >= to) next
      step = ""; ok = "t"; delete kv
      if ($4 == "refresh_rollups") step = "rollups"
      else if ($4 == "refresh_contact_periods") step = "contact_periods"
      else if ($4 == "refresh_ap_baselines") step = "ap_baselines"
      else if ($4 == "round" && $5 == "took") { step = "round"; kv["s"] = $6; kv["next"] = $10 }
      else if ($3 == "WARNING" && $4 == "database") { step = "error"; ok = "f" }
      if (step == "") next
      for (i = 5; i <= NF; i++) if (split($i, a, "=") == 2) kv[a[1]] = a[2]
      if (("sensor" in kv) && kv["sensor"] != sensor) next
      sub(/\+00:00$/, "Z", kv["from"])
      print ts, step, kv["sensor"], kv["from"], kv["s"], kv["ap_hours"], kv["sensor_hours"], kv["waited_s"], \
            kv["baselines"], kv["gaps"], kv["next"], ok, "", "log"
    }' >>"$OUT/refresh.csv"
fi
[[ $has_runs == t ]] && psql_ro "$HERE/refresh.sql" >>"$OUT/refresh.csv"
echo "refresh.csv: $(($(wc -l <"$OUT/refresh.csv") - 1)) rows (refresh_runs from ${first_db:-never}; the log before)"

# refresh aggregates appended to summary.csv (rollups that succeeded)
awk -F, 'NR > 1 && $2 == "rollups" && $12 == "t" { print $5 }' "$OUT/refresh.csv" | sort -n | awk '
  { v[NR] = $1 }
  END {
    if (NR == 0) { print "refresh_rollups_runs,0,runs,none recorded in the range"; exit }
    p50 = v[int(0.5 * (NR - 1)) + 1]; p95 = v[int(0.95 * (NR - 1)) + 1]
    printf "refresh_rollups_runs,%d,runs,refresh_runs + the log before it\n", NR
    printf "refresh_rollups_p50,%s,s,\nrefresh_rollups_p95,%s,s,\nrefresh_rollups_max,%s,s,\n", p50, p95, v[NR]
  }' >>"$OUT/summary.csv"
awk -F, 'NR > 1 && $2 == "rollups" && $12 == "f" { n++ } END { printf "refresh_rollups_failed,%d,runs,\n", n }' \
  "$OUT/refresh.csv" >>"$OUT/summary.csv"
awk -F, 'NR > 1 && $2 == "ap_baselines" && $9 > 0 { n++; if ($5 + 0 > m) m = $5 + 0 }
  END { if (n) printf "refresh_ap_baselines_max,%s,s,nightly (%d runs)\n", m, n }' "$OUT/refresh.csv" >>"$OUT/summary.csv"
first_row=$(awk -F, 'NR == 2 { print $1 }' "$OUT/refresh.csv")
echo "refresh_first_row,${first_row:-none},ts,no refresher record before this" >>"$OUT/summary.csv"

{
  echo "exported_at=$(date -u +%FT%TZ)"
  echo "sensor=$SENSOR from=$FROM to=$TO gap_s=$GAP_S"
  echo "refresher_log=${RLOG:-docker compose logs refresher}"
  echo "git=$(git -C "$HERE" rev-parse --short HEAD 2>/dev/null || echo unknown)"
} >"$OUT/export.txt"
echo "-> $OUT"
