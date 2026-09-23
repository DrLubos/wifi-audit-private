#!/usr/bin/env bash
#
# kismet-log-retention.sh - keep the kismetdb log directory bounded.
#
# Run hourly by wifi-sensor-kismet-log-retention.timer and before every Kismet
# start by kismet-prestart.sh (both as root, installed by install_sensor.sh).
# This is the ONLY component of the sensor that deletes files at runtime - an
# approved policy exception (2026-09-23), see CLAUDE.md.
#
# Why: Kismet opens a new kismetdb file on every start and never removes old
# ones. On 2026-09-22 11 GB of logs (79 files, 69 of them empty from a crash
# loop) filled the Pi's root filesystem and the collector lost ~20 h of data.
#
# Scope - strictly these names directly inside KISMET_LOG_DIR, nothing else:
#   ${KISMET_LOG_TITLE}-*.kismet  and  ${KISMET_LOG_TITLE}-*.kismet-journal
# Never deleted: a file a running kismet process has open (via /proc/<pid>/fd;
# Kismet is not dumpable, so this needs root) and, as a fallback, always the
# newest .kismet file.
#
# Order (oldest mtime first):
#   1. empty .kismet files (a crash loop leaves one per start attempt)
#   2. .kismet files older than KISMET_LOG_KEEP_DAYS
#   3. while the logs total more than KISMET_LOG_MAX_MB, or the filesystem has
#      less than KISMET_LOG_MIN_FREE_MB free: the oldest remaining .kismet
# A -journal file goes together with its .kismet, or when its .kismet is gone.
#
# Environment:
#   KISMET_LOG_DIR           log directory                     (required)
#   KISMET_LOG_TITLE         log file name prefix              (required)
#   KISMET_LOG_KEEP_DAYS     maximum age in days               (default 3)
#   KISMET_LOG_MAX_MB        size cap for all logs together    (default 1024)
#   KISMET_LOG_MIN_FREE_MB   free space to keep on the fs      (default 2048)
#
# Usage: kismet-log-retention.sh [--dry-run]
set -u
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

DIR=${KISMET_LOG_DIR:-}
TITLE=${KISMET_LOG_TITLE:-}
KEEP_DAYS=${KISMET_LOG_KEEP_DAYS:-3}
MAX_MB=${KISMET_LOG_MAX_MB:-1024}
MIN_FREE_MB=${KISMET_LOG_MIN_FREE_MB:-2048}
DRY=0

log()  { echo "kismet-log-retention: $*"; }
warn() { echo "kismet-log-retention: WARNING: $*" >&2; }

case ${1:-} in
  "") ;;
  --dry-run) DRY=1 ;;
  *) warn "usage: $0 [--dry-run]"; exit 2 ;;
esac

if [[ -z $DIR || -z $TITLE ]]; then
  warn "KISMET_LOG_DIR and KISMET_LOG_TITLE must be set - nothing done"
  exit 2
fi
if [[ ! -d $DIR ]]; then
  warn "$DIR is not a directory - nothing done"
  exit 2
fi
for v in KEEP_DAYS MAX_MB MIN_FREE_MB; do
  if ! [[ ${!v} =~ ^[0-9]+$ ]]; then
    warn "$v must be a non-negative integer (got '${!v}') - nothing done"
    exit 2
  fi
done

# "mtime size" of a file (GNU stat on the Pi, BSD stat as a fallback).
file_meta() { stat -c '%Y %s' -- "$1" 2>/dev/null || stat -f '%m %z' -- "$1"; }
mb() { echo $(( ($1 + 1048575) / 1048576 )); }

# Files a running kismet server holds open, one path per line.
OPEN=""
for pid in $(pidof kismet 2>/dev/null || pgrep -x kismet 2>/dev/null); do
  for fd in /proc/"$pid"/fd/*; do
    t=$(readlink "$fd" 2>/dev/null) && OPEN+="$t"$'\n'
  done
done
if [[ -n $(pidof kismet 2>/dev/null || true) && -z $OPEN ]]; then
  warn "kismet is running but its open files are unreadable (not root?) - relying on the newest-file rule"
fi
is_open() { [[ -n $OPEN ]] && grep -Fxq -- "$1" <<<"$OPEN"; }

# .kismet files as "mtime<TAB>size<TAB>path", oldest first.
LIST=$(
  for f in "$DIR/$TITLE"-*.kismet; do
    [[ -f $f ]] || continue
    read -r mt sz < <(file_meta "$f") || continue
    printf '%s\t%s\t%s\n' "$mt" "$sz" "$f"
  done | sort -n -k1,1
)
NEWEST=$(tail -n 1 <<<"$LIST" | cut -f3)

total=0
for f in "$DIR/$TITLE"-*.kismet "$DIR/$TITLE"-*.kismet-journal; do
  [[ -f $f ]] || continue
  read -r _ sz < <(file_meta "$f") && total=$(( total + sz ))
done
free_kb=$(df -Pk -- "$DIR" | awk 'NR == 2 {print $4}')
free=$(( ${free_kb:-0} * 1024 ))
now=$(date +%s)
freed=0 nfiles=0

# remove PATH SIZE REASON
remove() {
  if [[ $DRY -eq 1 ]]; then
    log "would delete $(basename "$1") ($(mb "$2") MB, $3)"
  elif rm -f -- "$1"; then
    log "deleted $(basename "$1") ($(mb "$2") MB, $3)"
  else
    warn "could not delete $1"
    return 1
  fi
  freed=$(( freed + $2 )); total=$(( total - $2 )); free=$(( free + $2 ))
  nfiles=$(( nfiles + 1 ))
}

# remove_log PATH SIZE REASON - a .kismet file and its -journal, if not open.
remove_log() {
  local j="$1-journal" jsz
  remove "$1" "$2" "$3" || return
  if [[ -f $j ]] && ! is_open "$j"; then
    read -r _ jsz < <(file_meta "$j") && remove "$j" "$jsz" "journal of a deleted log"
  fi
}

protected() { [[ $1 == "$NEWEST" ]] || is_open "$1"; }

# Passes 1 and 2: empty and expired files. Survivors stay candidates for pass 3.
REMAINING=()
while IFS=$'\t' read -r mt sz f; do
  [[ -n $f ]] || continue
  if protected "$f"; then continue; fi
  if [[ $sz -eq 0 ]]; then
    remove_log "$f" "$sz" "empty"
  elif (( now - mt > KEEP_DAYS * 86400 )); then
    remove_log "$f" "$sz" "older than ${KEEP_DAYS} d"
  else
    REMAINING+=("$sz"$'\t'"$f")
  fi
done <<<"$LIST"

# Pass 3: size cap and free-space floor, oldest first.
i=0
while (( total > MAX_MB * 1048576 || free < MIN_FREE_MB * 1048576 )) && (( i < ${#REMAINING[@]} )); do
  IFS=$'\t' read -r sz f <<<"${REMAINING[$i]}"
  remove_log "$f" "$sz" "size cap / free-space floor"
  i=$(( i + 1 ))
done

# Orphaned journals (their .kismet is already gone).
for j in "$DIR/$TITLE"-*.kismet-journal; do
  [[ -f $j && ! -e ${j%-journal} ]] || continue
  is_open "$j" && continue
  read -r _ sz < <(file_meta "$j") && remove "$j" "$sz" "orphaned journal"
done

if (( total > MAX_MB * 1048576 || free < MIN_FREE_MB * 1048576 )); then
  warn "still over the limits after deleting everything allowed (logs $(mb "$total") MB, free $(( free / 1048576 )) MB) - the newest/open log or other data is the cause"
fi
log "$([[ $DRY -eq 1 ]] && echo 'dry run: would free' || echo freed) $(mb "$freed") MB in $nfiles file(s); logs now $(mb "$total") MB, $(( free / 1048576 )) MB free"
exit 0
