#!/usr/bin/env bash
# Copy the box's newest database dump to this Mac and check it - run by hand,
# from the Mac (needs only ssh/rsync to the box):
#
#   server/ops/fetch_backup.sh            # -> ~/wifi-audit-backups/<dump>
#   server/ops/fetch_backup.sh google ~/elsewhere
#
# The newest dump is the newest backups/*.dump (nightly or a by-hand
# ./backup.sh - backups/LATEST names only the nightly one). The transfer resumes
# when interrupted (rsync --partial; run it again). The sha256 is computed on
# BOTH sides and must be equal, and equal to the nightly job's <dump>.sha256
# when there is one; only then is the file moved into place. Already there and
# equal: nothing is copied. Exit 0 = ok, 1 = no box / no dump, 2 = mismatch
# (the copy is deleted).
set -euo pipefail
HOST=${1:-google}
DEST=${2:-$HOME/wifi-audit-backups}
REMOTE=wifi-audit/server/backups
mkdir -p "$DEST"

name=$(ssh -o ConnectTimeout=20 "$HOST" "cd $REMOTE && ls -1t *.dump | head -1" \
       | tr -d '[:space:]') || true
[[ $name =~ ^[A-Za-z0-9_.-]+\.dump$ ]] || { echo "no dump found on $HOST:$REMOTE" >&2; exit 1; }
box=$(ssh "$HOST" "cd $REMOTE && sha256sum $name" | awk '{print $1}')
nightly=$(ssh "$HOST" "cat $REMOTE/$name.sha256 2>/dev/null" | awk '{print $1}' || true)
[[ $box =~ ^[0-9a-f]{64}$ ]] || { echo "cannot hash $name on the box" >&2; exit 1; }
if [[ -n $nightly && $nightly != "$box" ]]; then
  echo "MISMATCH on the box: $name hashes $box, its .sha256 says $nightly" >&2; exit 2
fi
if [[ -f $DEST/$name ]] && [[ $(shasum -a 256 "$DEST/$name" | awk '{print $1}') == "$box" ]]; then
  echo "$name already in $DEST, sha256 equal ($box)"; exit 0
fi
echo "copying $name from $HOST ..."
rsync --partial --timeout=120 "$HOST:$REMOTE/$name" "$DEST/.$name.part"
mac=$(shasum -a 256 "$DEST/.$name.part" | awk '{print $1}')
if [[ $mac != "$box" ]]; then
  rm -f -- "$DEST/.$name.part"
  echo "MISMATCH: box $box, Mac $mac - the copy was deleted, run again" >&2; exit 2
fi
mv -- "$DEST/.$name.part" "$DEST/$name"
echo "ok: $DEST/$name"
echo "sha256 box $box"
echo "sha256 Mac $mac"
