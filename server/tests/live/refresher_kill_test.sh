#!/bin/sh
# Live test of the refresher's taken mark (schema 21, app/refresher.py): a
# refresher killed in the middle of a refresh must find its mark in
# ingest_state.taken_from at the next start and refresh that window again.
# Run by the developer on the box, from server/:
#
#   sh tests/live/refresher_kill_test.sh          # watches one refresh, kills nothing
#   sh tests/live/refresher_kill_test.sh --run    # the test: kills the refresher once
#
# --run samples ingest_state until a refresh holds a mark, kills the refresher
# container (SIGKILL: no handler runs, as when the box loses power), starts it
# again and waits for three things:
#   1. the mark is still in the table right after the kill (taken_from);
#   2. the new process logs "the mark of a refresh that did not finish is back";
#   3. a rollups step from that mark or earlier ends ok and taken_from is
#      empty again.
# Nothing else is touched: this script writes no data (it reads as claude_ro,
# ops/create_ro_role.sql), the database, the api and the ingest keep running,
# the refresher is away for a few seconds (the rollups wait; the ingest's own
# mark keeps them) and is started again whatever happens - also when the
# script fails or is interrupted.
# What it does not do: stop Postgres. The backend the killed refresher leaves
# behind finishes its statement, so the hours are right already when the new
# refresher refreshes them once more - the test shows the way of the mark,
# not a repaired loss.
#
# WAIT_S (300): how long to wait for a refresh, and for the recovery.
# Exit: 0 PASS (or a watched refresh without --run), 1 FAIL, 2 nothing tested
# (no refresh came in WAIT_S seconds, or the refresh ended before the kill
# landed - run it again), 3 cannot run.
set -eu
me=refresher_kill_test
run=""
case "${1:-}" in
  --run) run=1 ;;
  "") ;;
  *) echo "usage: sh tests/live/refresher_kill_test.sh [--run]" >&2; exit 3 ;;
esac
cd "$(dirname "$0")/../.."
WAIT_S="${WAIT_S:-300}"
SLEEP_S="${SLEEP_S:-2}"
BACK="the mark of a refresh that did not finish is back"

ro() { docker compose exec -T db sh -c 'psql -X -At -U claude_ro -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -c "$1"' sh "$1"; }
say() { echo "== $(date -u +%FT%TZ) $*"; }
state() { ro "SELECT 'dirty_from=' || coalesce(dirty_from::text, '-') || ' taken_from=' || coalesce(taken_from::text, '-') FROM ingest_state WHERE sensor_id = $1"; }
taken() { ro "SELECT coalesce(taken_from::text, '') FROM ingest_state WHERE sensor_id = $1"; }

col=$(ro "SELECT count(*) FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'ingest_state' AND column_name = 'taken_from'") \
  || { echo "$me: cannot read the database as claude_ro (ops/create_ro_role.sql)" >&2; exit 3; }
[ "$col" = 1 ] || { echo "$me: ingest_state.taken_from is missing - schema 21 needed (ops/migrate_schema21.sh)" >&2; exit 3; }
[ -n "$(docker compose ps -q refresher)" ] || { echo "$me: the refresher is not running" >&2; exit 3; }

# --- a refresh that holds a mark
say "watching ingest_state for a refresh that holds a mark (up to $WAIT_S s)"
deadline=$(( $(date +%s) + WAIT_S ))
seen=""
while [ "$(date +%s)" -le "$deadline" ]; do
  seen=$(ro "SELECT sensor_id || ' ' || taken_from FROM ingest_state WHERE taken_from IS NOT NULL ORDER BY sensor_id LIMIT 1")
  [ -z "$seen" ] || break
done
[ -n "$seen" ] || { say "NOTHING TESTED: no refresh held a mark in $WAIT_S s - run again"; exit 2; }
sid=${seen%% *}
mark=${seen#* }
case "$sid" in ''|*[!0-9]*) echo "$me: unexpected sensor id '$sid'" >&2; exit 3 ;; esac
case "$mark" in ''|*[!0-9:.+\ -]*) echo "$me: unexpected mark '$mark'" >&2; exit 3 ;; esac

if [ -z "$run" ]; then
  say "sensor $sid: a refresh holds the mark $mark - $(state "$sid")"
  while [ "$(date +%s)" -le "$deadline" ] && [ -n "$(taken "$sid")" ]; do :; done
  say "sensor $sid: after the refresh - $(state "$sid")"
  echo "watched only: 'sh tests/live/refresher_kill_test.sh --run' kills the refresher at that point of a refresh"
  exit 0
fi

# --- the kill; from here on the refresher is started again whatever happens
since=$(date -u +%FT%TZ)       # the box's clock, as the database's and the log's
trap 'docker compose up -d refresher >/dev/null 2>&1 || true' EXIT
trap 'exit 1' INT TERM
out=$(docker compose kill refresher 2>&1) || { echo "$out" >&2; echo "$me: the refresher could not be killed" >&2; exit 3; }
say "sensor $sid: refresher killed while a refresh held the mark $mark"
after=$(taken "$sid") || after=unreadable
out=$(docker compose up -d refresher 2>&1) || { echo "$out" >&2; say "FAIL: the refresher did not start again - start it: docker compose up -d refresher"; exit 1; }
say "refresher started again"
if [ "$after" = unreadable ]; then
  say "FAIL: the mark could not be read after the kill"
  exit 1
fi
if [ -z "$after" ]; then
  say "NOTHING TESTED: the refresh ended before the kill landed (no taken mark right after it) - run again"
  exit 2
fi
say "1/3 the mark stayed in the table after the kill: taken_from=$after"

# --- the recovery
deadline=$(( $(date +%s) + WAIT_S ))
back="" again=0 left=$after
while [ "$(date +%s)" -le "$deadline" ]; do
  if [ -z "$back" ] && docker compose logs --since "$since" refresher 2>&1 | grep -q "$BACK"; then back=1; fi
  again=$(ro "SELECT count(*) FROM refresh_runs WHERE step = 'rollups' AND ok AND sensor_id = $sid AND started_at >= '$since'::timestamptz AND refresh_from <= '$mark'::timestamptz") || again=0
  left=$(taken "$sid") || left=unreadable
  if [ -n "$back" ] && [ "$again" -ge 1 ] && [ -z "$left" ]; then break; fi
  sleep "$SLEEP_S"
done

fail=""
if [ -n "$back" ]; then say "2/3 the new refresher put the mark back: $(docker compose logs --since "$since" refresher 2>&1 | grep "$BACK" | tail -1 | sed 's/^[^|]*| //')"
else say "2/3 MISSING: no '$BACK' in the refresher's log since $since"; fail=1; fi
if [ "$again" -ge 1 ] && [ -z "$left" ]; then say "3/3 a rollups step from $mark or earlier ended ok and taken_from is empty - $(state "$sid")"
else say "3/3 MISSING: rollups steps from the mark since the kill: $again; $(state "$sid")"; fail=1; fi

if [ -n "$fail" ]; then
  say "FAIL - the refresher's log since the kill:"
  docker compose logs --since "$since" refresher 2>&1 | tail -15
  exit 1
fi
say "PASS: the refresher killed in the middle of a refresh did not lose the mark $mark"
