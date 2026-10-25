#!/bin/bash
# Pull the box's newest nightly database dump to this Mac and check it
# (run hourly by the LaunchAgent of install_backup_pull.sh; by hand works too).
#
#   ~/Developer/wifi-audit-backup-pull/pull_backup.sh
#
# Reads backups/LATEST and <dump>.sha256 on the box (ops/backup_nightly.sh),
# fetches the dump with rsync --partial into DEST/.incoming (an interrupted
# transfer - the Mac asleep, offline - resumes on the next run), compares
# the sha256 with the box's, and only then moves it to DEST. Keeps the newest
# KEEP nightly dumps in DEST (default 14); deletes nothing anywhere else.
# Exit 0: done or nothing to do; 1: the box unreachable (tried again next
# hour); 2: a sha256 mismatch (the file is deleted, the next run fetches again).
set -uo pipefail
HOST=${WIFI_AUDIT_BACKUP_HOST:-google}
REMOTE_DIR=${WIFI_AUDIT_BACKUP_REMOTE:-wifi-audit/server/backups}
DEST=${WIFI_AUDIT_BACKUP_DEST:-$HOME/wifi-audit-backups/nightly}
KEEP=${WIFI_AUDIT_BACKUP_KEEP:-14}
SSH=(/usr/bin/ssh -o BatchMode=yes -o ConnectTimeout=20 -o ServerAliveInterval=30)
log() { echo "$(date -u +%FT%TZ) $*"; }

mkdir -p "$DEST/.incoming"
# one run at a time: an atomic mkdir; a lock left by a killed run expires after 3 h
LOCK=$DEST/.incoming/.lock
if [[ -d $LOCK && -n $(find "$LOCK" -maxdepth 0 -mmin +180 2>/dev/null) ]]; then rmdir "$LOCK"; fi
mkdir "$LOCK" 2>/dev/null || { log "another pull is running"; exit 0; }
trap 'rmdir "$LOCK"' EXIT

name=$("${SSH[@]}" "$HOST" "cat $REMOTE_DIR/LATEST" 2>/dev/null | tr -d '[:space:]') || true
[[ -n $name ]] || { log "box unreachable or no LATEST yet"; exit 1; }
[[ $name =~ ^[A-Za-z0-9_.-]+\.dump$ ]] || { log "unexpected LATEST: $name"; exit 1; }
[[ -f $DEST/$name ]] && exit 0                     # already here and checked

want=$("${SSH[@]}" "$HOST" "cat $REMOTE_DIR/$name.sha256" 2>/dev/null | awk '{print $1}') || true
[[ $want =~ ^[0-9a-f]{64}$ ]] || { log "no sha256 for $name on the box"; exit 1; }
log "pulling $name"
/usr/bin/rsync --partial --timeout=120 -e "${SSH[*]}" "$HOST:$REMOTE_DIR/$name" "$DEST/.incoming/$name" \
  || { log "transfer interrupted - resumes next run"; exit 1; }
got=$(/usr/bin/shasum -a 256 "$DEST/.incoming/$name" | awk '{print $1}')
if [[ $got != "$want" ]]; then
  log "sha256 MISMATCH for $name (box $want, here $got) - deleted, fetched again next run"
  rm -f -- "$DEST/.incoming/$name"
  exit 2
fi
mv -- "$DEST/.incoming/$name" "$DEST/$name"
printf '%s  %s\n' "$got" "$name" > "$DEST/$name.sha256"
log "ok $name sha256 $got"
# keep the newest KEEP nightly dumps (this folder only)
ls -1t "$DEST"/*.dump 2>/dev/null | tail -n +"$((KEEP + 1))" | while read -r old; do
  rm -f -- "$old" "$old.sha256" && log "pruned $(basename "$old")"
done
exit 0
