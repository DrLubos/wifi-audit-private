#!/bin/sh
# Storage and response-time measurement of the dashboard endpoints on the
# server box - the before/after numbers for the p95 < 300 ms requirement.
#
#   cd server
#   sh tests/perf/measure.sh 2>&1 | tee ~/perf-$(date -u +%Y%m%dT%H%MZ).txt
#
# Runs on the Mac over `ssh google` (HOST=, REMOTE_DIR= override). Database
# access is read-only: psql connects as the role claude_ro
# (ops/create_ro_role.sql - no write privilege, read-only transactions). The box
# itself is NOT left alone: every cold run restarts the db container and drops
# the host page cache (sudo -n), so the dashboard answers 503 for ~10 s, ten
# times in all. NO_COLD=1 skips the cold runs (warm numbers only).
#
# Back-to-back warm requests run the e2-micro out of CPU burst credit after
# ~12 s, then the whole VM is paused ~85 % of the time in ~220 ms slices
# (docs/performance.md): warm p95/max include that, p50 mostly not.
#
# MODE=v2 (the default) measures the dashboard endpoints (/api/v2, PLAN.md 7.4): per
# statement group of app/v2/sql the EXPLAIN (ANALYZE, BUFFERS) cold and warm
# (ep_v2.py: the api's own SQL, RANGE=30d by default), HTTP warm timings,
# then v2_smoke.py in the api container: every endpoint 200 (and its 404 /
# 422 cases), the ETag 304 round trip, event paging back and forth, and the
# privacy scan (every MAC in every response a BSSID of the account's APs).
# The first dashboard's endpoints (/api/overview ...) and their measurement
# (the old default mode) were removed in phase 5c; their numbers stay in
# docs/performance.md.
#
# MODE=detectors measures the batch detectors' time-window scans (ep_detectors.py).
#
# MODE=coldstart measures only what the first requests after a cold start
# cost and why (coldstart_probe.py, on the box): four variants, each after
# 60 s of idle - db restart only, page-cache drop only, both (requests through
# Caddy), and both with the cold request made the default way (a python
# started in the api container). Per request: time, VM stall time (the
# e2-micro CPU throttle), disk reads, swap-ins and the processes with the most
# major page faults and CPU time. VARIANTS="both harness" runs a subset.
set -eu
HOST="${HOST:-google}"
REMOTE_DIR="${REMOTE_DIR:-wifi-audit/server}"
N="${N:-20}"
MODE="${MODE:-v2}"
here=$(cd "$(dirname "$0")" && pwd)

on_box() { ssh -o BatchMode=yes "$HOST" "cd $REMOTE_DIR && set -a && . ./.env && set +a && $1"; }
psql_ro() {
  on_box 'docker compose exec -T db psql -X -q -U claude_ro -d "$POSTGRES_DB" -v ON_ERROR_STOP=1'
}
make_cold() {
  on_box 'docker compose restart db >/dev/null 2>&1 && sync && echo 3 | sudo -n tee /proc/sys/vm/drop_caches >/dev/null && until docker compose exec -T db pg_isready -q -U claude_ro -d "$POSTGRES_DB"; do sleep 1; done'
}
restart_db() {
  on_box 'docker compose restart db >/dev/null 2>&1 && until docker compose exec -T db pg_isready -q -U claude_ro -d "$POSTGRES_DB"; do sleep 1; done'
}
drop_cache() {
  on_box 'sync && echo 3 | sudo -n tee /proc/sys/vm/drop_caches >/dev/null'
}
probe() {
  on_box "python3 - $*" < "$here/coldstart_probe.py"
}
http() {
  args=""
  for a in "$@"; do args="$args '$a'"; done
  on_box "docker compose exec -T api python -$args" < "$here/http_timing.py"
}

if [ "$MODE" = v2 ]; then
  R="${RANGE:-30d}"
  echo "# v2 measurement $(date -u +%Y-%m-%dT%H:%M:%SZ), host $HOST, N=$N, range $R, cold runs: $([ -n "${NO_COLD:-}" ] && echo no || echo yes)"
  on_box 'git log --oneline -1; nproc; df -h / | tail -1; free -m | sed -n 2,3p; du -sh backups 2>/dev/null || true'
  echo "== sizes"
  psql_ro < "$here/sizes.sql"
  for g in context sensors overview events networks aps spectrum settings; do
    echo
    echo "=================== v2 $g"
    if [ -z "${NO_COLD:-}" ]; then
      make_cold
      echo "-- EXPLAIN cold"
      python3 "$here/ep_v2.py" --range "$R" "$g" | psql_ro
    fi
    python3 "$here/ep_v2.py" --range "$R" "$g" | psql_ro >/dev/null
    echo "-- EXPLAIN warm"
    python3 "$here/ep_v2.py" --range "$R" "$g" | psql_ro
  done
  sleep 60                        # idle: the CPU burst credit refills before the HTTP runs
  echo
  echo "=================== v2 HTTP (warm)"
  http warm "$N" /api/v2/sensors /api/v2/overview/status "/api/v2/overview/period?range=$R" \
    "/api/v2/events" "/api/v2/networks?range=$R" "/api/v2/networks/timeseries?range=$R" \
    "/api/v2/clients/heatmap?range=$R" "/api/v2/aps?range=$R" "/api/v2/spectrum?range=$R" \
    "/api/v2/spectrum/utilization?range=$R&view=timeline" /api/v2/settings/sensors
  echo
  echo "=================== v2 smoke test + privacy scan"
  BSSIDS=$(echo "SELECT string_agg(upper(bssid::text), ',') FROM aps;" \
           | on_box 'docker compose exec -T db psql -X -q -At -U claude_ro -d "$POSTGRES_DB"')
  on_box "docker compose exec -T api python - '$BSSIDS'" < "$here/v2_smoke.py"
  echo "# done $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  exit 0
fi

if [ "$MODE" = coldstart ]; then
  echo "# cold-start breakdown $(date -u +%Y-%m-%dT%H:%M:%SZ), host $HOST"
  B64=$(base64 < "$here/http_timing.py" | tr -d '\n')
  for v in ${VARIANTS:-restart drop both harness}; do
    sleep 60                      # idle: the CPU burst credit refills
    echo "== $v"
    case $v in
      restart) restart_db ;;
      drop)    drop_cache ;;
      both|harness) make_cold ;;
    esac
    if [ "$v" = harness ]; then probe harness - "$B64"; else probe http -; fi
  done
  echo "# done $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  exit 0
fi

if [ "$MODE" = detectors ]; then
  echo "# detector scans $(date -u +%Y-%m-%dT%H:%M:%SZ), host $HOST"
  on_box 'git log --oneline -1'
  # the scans are heavy enough to spend the e2-micro's CPU burst credit: nothing is timed after them
  echo "=================== detectors: deauth_flood / evil_twin time-window scans (EXPLAIN, first + warm run)"
  python3 "$here/ep_detectors.py" | cat "$here/common.sql" - | psql_ro
  echo "# done $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  exit 0
fi

echo "measure.sh: unknown MODE '$MODE' (v2, coldstart, detectors)" >&2
exit 2
