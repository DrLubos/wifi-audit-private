#!/bin/sh
# A scratch copy of the database on the same server, for rehearsing a
# migration on real data before it touches the live database (live upload,
# schema 15). Never the live database: the name is fixed and the script
# refuses it. Restoring needs ~1.5x the database size of free disk.
#
#   cd ~/wifi-audit/server
#   sh ops/scratch_db.sh restore backups/<dump>   # creates wifi_audit_scratch from a ./backup.sh dump
#   MIGRATE_DB=wifi_audit_scratch sh ops/migrate_schemaN.sh
#   sh ops/scratch_db.sh drop                       # removes the scratch copy
set -eu
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "scratch_db: no .env next to docker-compose.yml" >&2; exit 1; }
set -a; . ./.env; set +a
SCRATCH=wifi_audit_scratch
[ "$SCRATCH" != "$POSTGRES_DB" ] || { echo "scratch_db: refusing - that is the live database" >&2; exit 1; }
dc() { docker compose exec -T db "$@"; }

case "${1:-}" in
  restore)
    [ $# -eq 2 ] && [ -f "$2" ] || { echo "usage: sh ops/scratch_db.sh restore <dump file>" >&2; exit 2; }
    exists=$(dc psql -X -At -U "$POSTGRES_USER" -d postgres -c "SELECT count(*) FROM pg_database WHERE datname = '$SCRATCH'")
    [ "$exists" = 0 ] || { echo "scratch_db: $SCRATCH exists - drop it first" >&2; exit 1; }
    dc createdb -U "$POSTGRES_USER" "$SCRATCH"
    echo "== $(date -u +%FT%TZ) restoring $2 into $SCRATCH"
    dc pg_restore -U "$POSTGRES_USER" -d "$SCRATCH" --no-owner --exit-on-error < "$2"
    dc psql -X -q -U "$POSTGRES_USER" -d "$SCRATCH" -c "VACUUM (ANALYZE)"
    echo "== $(date -u +%FT%TZ) restored: $(dc psql -X -At -U "$POSTGRES_USER" -d "$SCRATCH" -c "SELECT value FROM schema_meta WHERE key = 'schema_version'") (schema version)"
    ;;
  drop)
    dc dropdb -U "$POSTGRES_USER" --if-exists "$SCRATCH"
    echo "== dropped $SCRATCH"
    ;;
  *) echo "usage: sh ops/scratch_db.sh restore <dump> | drop" >&2; exit 2 ;;
esac
