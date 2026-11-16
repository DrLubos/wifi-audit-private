#!/usr/bin/env bash
# Restore test of a backup (2026-11-16): a dump nobody ever restored is not
# known to be a backup. Restores a dump (default: the newest, backups/LATEST)
# into the scratch database (ops/scratch_db.sh: never the live one), compares
# it with the live database (read as claude_ro) and drops the scratch copy
# again - also when a step fails. Writes only the scratch database.
#
#   cd ~/wifi-audit/server
#   ops/restore_check.sh                       # the newest nightly dump
#   ops/restore_check.sh backups/<dump>        # a given one
#
# Checks: the dump's sha256 against its .sha256 file (when there is one);
# pg_restore --exit-on-error completes; the schema version equals the live
# one; every table below has rows; per time-series table the restored rows
# against the live rows up to the restored copy's newest poll (a difference
# is reported, not failed: a late backlog can add older rows after the dump).
# Needs ~1.5x the database size of free disk and the box for ~45 min (the
# first run, 2026-11-16: the restore of a 142 MB dump took 36 min, load ~9):
# not during the build of an image or a staged experiment. Exit 0 = PASS.
set -euo pipefail
cd "$(dirname "$0")/.."
[[ -f .env ]] || { echo "restore_check: no .env next to docker-compose.yml" >&2; exit 1; }
set -a; . ./.env; set +a
SCRATCH=wifi_audit_scratch

dump=${1:-}
if [[ -z $dump ]]; then
  [[ -f backups/LATEST ]] || { echo "restore_check: no backups/LATEST - give a dump" >&2; exit 2; }
  dump="backups/$(cat backups/LATEST)"
fi
[[ -f $dump ]] || { echo "restore_check: no such dump: $dump" >&2; exit 2; }
free_kb=$(df -Pk . | awk 'NR == 2 {print $4}')
size_kb=$(du -k "$dump" | awk '{print $1}')
echo "== $(date -u +%FT%TZ) restore check of $dump ($((size_kb / 1024)) MB; $((free_kb / 1024)) MB free)"

if [[ -f $dump.sha256 ]]; then
  (cd "$(dirname "$dump")" && sha256sum --quiet -c "$(basename "$dump").sha256") \
    || { echo "FAIL: the dump does not match its .sha256" >&2; exit 1; }
  echo "sha256: equal to $(basename "$dump").sha256"
fi

# The counts read up to ~10 M rows; claude_ro's own statement_timeout (120 s)
# cancelled the observations count on the busy box (2026-11-16), so these
# sessions set a longer one.
SLOW='-c statement_timeout=30min'
live() { docker compose exec -T -e PGOPTIONS="$SLOW" db psql -X -At -v ON_ERROR_STOP=1 -U claude_ro -d "$POSTGRES_DB" -c "$1"; }
scratch() { docker compose exec -T -e PGOPTIONS="$SLOW" db psql -X -At -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$SCRATCH" -c "$1"; }

cleanup() { sh ops/scratch_db.sh drop >/dev/null 2>&1 || echo "restore_check: drop $SCRATCH by hand: sh ops/scratch_db.sh drop" >&2; }
trap cleanup EXIT
t0=$(date +%s)
sh ops/scratch_db.sh restore "$dump"
echo "restore: $(( $(date +%s) - t0 )) s"

fail=0
v_live=$(live "SELECT value FROM schema_meta WHERE key = 'schema_version'")
v_dump=$(scratch "SELECT value FROM schema_meta WHERE key = 'schema_version'")
if [[ $v_live == "$v_dump" ]]; then echo "schema version: $v_dump (= live)"; else echo "FAIL: schema version $v_dump, live $v_live"; fail=1; fi

newest=$(scratch "SELECT max(ts) FROM polls")
echo "newest poll in the dump: $newest"
printf '%-22s %14s %14s %s\n' table restored "live <= newest" note
# time-series tables: compared up to the dump's newest poll
for spec in polls:ts observations:ts alerts:ts detections:ts ap_load_hourly:hour sensor_heartbeats:received_at; do
  t=${spec%%:*} c=${spec##*:}
  r=$(scratch "SELECT count(*) FROM $t WHERE $c <= '$newest'")
  l=$(live "SELECT count(*) FROM $t WHERE $c <= '$newest'")
  note=""
  if [[ $r -eq 0 ]]; then note="FAIL: empty"; fail=1
  elif [[ $r -ne $l ]]; then note="differs by $((l - r)) (rows stored later with an older time?)"; fi
  printf '%-22s %14s %14s %s\n' "$t" "$r" "$l" "$note"
done
# the rest: rows present
for t in sensors devices aps networks sensor_config sensor_annotations audit_log ap_baselines ap_rssi_hourly; do
  r=$(scratch "SELECT count(*) FROM $t")
  if [[ $r -eq 0 ]]; then note="FAIL: empty"; fail=1; else note=""; fi
  printf '%-22s %14s %14s %s\n' "$t" "$r" "-" "$note"
done

if [[ $fail -eq 0 ]]; then
  echo "== $(date -u +%FT%TZ) PASS ($(( $(date +%s) - t0 )) s); the scratch copy is dropped"
else
  echo "== $(date -u +%FT%TZ) FAIL; the scratch copy is dropped" >&2
fi
exit $fail
