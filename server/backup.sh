#!/usr/bin/env bash
# Backup of the wifi-audit PostgreSQL database: pg_dump in custom format
# (compressed, restorable table by table with pg_restore) to a timestamped
# file OUTSIDE the container, next to this script under backups/ (gitignored).
#
#   ./backup.sh                 # -> backups/<db>-<UTC stamp>.dump
#   KEEP=14 ./backup.sh         # keep the newest 14 (default 7)
#   BACKUP_DIR=/mnt/x ./backup.sh
#
# Read-only on the database. Credentials come from .env, as for compose.
# Restore: see README.md ("Backup and restore").
set -euo pipefail

cd "$(dirname "$0")"
[ -f .env ] || { echo "backup.sh: no .env next to docker-compose.yml" >&2; exit 1; }
set -a; . ./.env; set +a
: "${POSTGRES_USER:?POSTGRES_USER missing in .env}" "${POSTGRES_DB:?POSTGRES_DB missing in .env}"

KEEP="${KEEP:-7}"
BACKUP_DIR="${BACKUP_DIR:-./backups}"
mkdir -p "$BACKUP_DIR"

stamp=$(date -u +%Y%m%dT%H%M%SZ)
out="$BACKUP_DIR/${POSTGRES_DB}-${stamp}.dump"
tmp="$out.part"

docker compose exec -T db pg_dump -U "$POSTGRES_USER" -Fc "$POSTGRES_DB" > "$tmp"

# Trust the file only if pg_restore can read its table of contents.
docker compose exec -T db pg_restore --list < "$tmp" > /dev/null
mv "$tmp" "$out"

printf '%s  %s  sha256 %s\n' "$out" "$(du -h "$out" | cut -f1)" "$(sha256sum "$out" | cut -d' ' -f1)"

# Prune: keep the newest $KEEP dumps of this database.
ls -1t "$BACKUP_DIR"/"${POSTGRES_DB}"-*.dump 2>/dev/null | tail -n +"$((KEEP + 1))" | while read -r old; do
  rm -f -- "$old" && echo "pruned $old"
done
