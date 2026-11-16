#!/usr/bin/env bash
# Nightly database backup on the box (run by cron, ops/backup_cron.sh):
# ./backup.sh with KEEP=7, then a sha256 file next to the new dump
# (backups/<dump>.sha256, `sha256sum` format) and backups/LATEST (its file
# name) - what ops/fetch_backup.sh, run by hand on the Mac, reads. One run at
# a time (flock). Read-only on the database, like backup.sh.
#
#   cd ~/wifi-audit/server && ops/backup_nightly.sh       # by hand, the same as the cron run
#
# Note: KEEP=7 counts every dump in backups/ (the pre-migration ones too);
# backups/keep/ is never pruned.
set -euo pipefail
cd "$(dirname "$0")/.."
KEEP="${KEEP:-7}"

# A failed run sends an e-mail (api/app/notify.py, through the api image; it
# sends nothing while NOTIFY_SMTP_HOST / NOTIFY_TO are empty in .env).
notify_failure() {
  local rc=$?
  [[ $rc -eq 0 ]] && return 0
  docker compose run --rm --no-deps -T --entrypoint python refresher -m app.notify \
      --subject "nightly backup failed" \
      --body "ops/backup_nightly.sh on $(hostname) exited with status $rc at $(date -u +%FT%TZ). The log: ~/backup-nightly.log." \
      </dev/null >/dev/null 2>&1 || echo "backup_nightly: the failure mail was not sent either" >&2
  return 0
}
trap notify_failure EXIT
exec 9>"${TMPDIR:-/tmp}/wifi-audit-backup.lock"
flock -n 9 || { echo "backup_nightly: another backup is running" >&2; exit 1; }

echo "== $(date -u +%FT%TZ) backup (KEEP=$KEEP)"
out=$(KEEP="$KEEP" ./backup.sh)
echo "$out"
dump=$(printf '%s\n' "$out" | head -1 | awk '{print $1}')
[[ -f $dump && $dump == *.dump ]] || { echo "backup_nightly: no dump in backup.sh's output" >&2; exit 1; }
name=$(basename "$dump")
(cd "$(dirname "$dump")" && sha256sum "$name" > "$name.sha256.part" && mv "$name.sha256.part" "$name.sha256")
printf '%s\n' "$name" > "$(dirname "$dump")/LATEST.part" && mv "$(dirname "$dump")/LATEST.part" "$(dirname "$dump")/LATEST"
# a .sha256 whose dump was pruned goes too
for s in "$(dirname "$dump")"/*.dump.sha256; do
  [[ -f ${s%.sha256} ]] || { rm -f -- "$s"; echo "pruned $s"; }
done
echo "== $(date -u +%FT%TZ) done: $name"
