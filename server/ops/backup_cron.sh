#!/usr/bin/env bash
# The nightly backup's cron entry in the box user's crontab (no root needed):
#
#   ops/backup_cron.sh install     # adds (or replaces) the one tagged line
#   ops/backup_cron.sh remove      # removes it; every other crontab line stays
#   ops/backup_cron.sh show
#
# 01:05 UTC: away from the refresher's nightly baselines (03:30
# Europe/Bratislava = 01:30/02:30 UTC) and the other cron jobs on the hour.
# Log: ~/backup-nightly.log (appended; the job rotates it at 1 MB).
set -euo pipefail
SERVER=$(cd "$(dirname "$0")/.." && pwd)
TAG="# wifi-audit nightly backup"
LINE="5 1 * * * cd $SERVER && { [ \$(stat -c\%s ~/backup-nightly.log 2>/dev/null || echo 0) -lt 1048576 ] || mv ~/backup-nightly.log ~/backup-nightly.log.1; ops/backup_nightly.sh >> ~/backup-nightly.log 2>&1; } $TAG"
current=$(crontab -l 2>/dev/null || true)
case "${1:-}" in
  install) { [ -z "$current" ] || printf '%s\n' "$current" | grep -vF "$TAG" || true; echo "$LINE"; } | crontab -
           crontab -l | grep -F "$TAG" ;;
  remove)  { [ -z "$current" ] || printf '%s\n' "$current" | grep -vF "$TAG" || true; } | crontab -
           echo "removed" ;;
  show)    printf '%s\n' "$current" | grep -F "$TAG" || echo "not installed" ;;
  *) echo "usage: ops/backup_cron.sh install | remove | show" >&2; exit 2 ;;
esac
