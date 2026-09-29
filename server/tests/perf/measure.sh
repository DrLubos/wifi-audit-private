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
# Per endpoint: HTTP cold (one request right after a restart), EXPLAIN
# (ANALYZE, BUFFERS) cold (after another restart) and warm (the second of two
# runs), then HTTP warm p50/p95/max over N requests (default 20). HTTP goes to
# the api container directly (127.0.0.1:8000, like its healthcheck), not
# through Caddy. The AP endpoints use the AP with the most readings.
set -eu
HOST="${HOST:-google}"
REMOTE_DIR="${REMOTE_DIR:-wifi-audit/server}"
N="${N:-20}"
here=$(cd "$(dirname "$0")" && pwd)

on_box() { ssh -o BatchMode=yes "$HOST" "cd $REMOTE_DIR && set -a && . ./.env && set +a && $1"; }
psql_ro() {
  on_box 'docker compose exec -T db psql -X -q -U claude_ro -d "$POSTGRES_DB" -v ON_ERROR_STOP=1'
}
make_cold() {
  on_box 'docker compose restart db >/dev/null 2>&1 && sync && echo 3 | sudo -n tee /proc/sys/vm/drop_caches >/dev/null && until docker compose exec -T db pg_isready -q -U claude_ro -d "$POSTGRES_DB"; do sleep 1; done'
}
http() {
  args=""
  for a in "$@"; do args="$args '$a'"; done
  on_box "docker compose exec -T api python -$args" < "$here/http_timing.py"
}
endpoint_sql() { cat "$here/common.sql" "$here/ep_$1.sql"; }

echo "# perf measurement $(date -u +%Y-%m-%dT%H:%M:%SZ), host $HOST, N=$N, cold runs: $([ -n "${NO_COLD:-}" ] && echo no || echo yes)"
echo "== box"
on_box 'git log --oneline -1; nproc; df -h / | tail -1; free -m | sed -n 2,3p; du -sh backups 2>/dev/null || true'

echo "== sizes"
psql_ro < "$here/sizes.sql"

KEY=$({ cat "$here/common.sql"; echo '\echo :key'; } | psql_ro)
echo "== busiest AP: $KEY"

for ep in overview aps ap ap_rssi detections; do
  case $ep in
    overview)   set -- "/api/overview" ;;
    aps)        set -- "/api/aps" ;;
    ap)         set -- "/api/aps/$KEY" ;;
    ap_rssi)    set -- "/api/aps/$KEY/rssi?bucket=3600" ;;
    detections) set -- "/api/detections" ;;
  esac
  echo
  echo "=================== $ep"
  if [ -z "${NO_COLD:-}" ]; then
    make_cold
    http cold "$@"
    make_cold
    echo "-- EXPLAIN cold"
    endpoint_sql $ep | psql_ro
  fi
  endpoint_sql $ep | psql_ro >/dev/null
  echo "-- EXPLAIN warm"
  endpoint_sql $ep | psql_ro
  http warm "$N" "$@"
done

echo
echo "=================== AP page, the other bucket widths; Overview page, its other requests (warm)"
http warm "$N" "/api/aps/$KEY/rssi?bucket=900" "/api/aps/$KEY/rssi?bucket=21600" \
  "/api/findings" "/api/alerts?limit=50"
echo "# done $(date -u +%Y-%m-%dT%H:%M:%SZ)"
