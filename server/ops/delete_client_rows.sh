#!/bin/sh
# The client data the snapshot imports put on the server (live upload decision
# D4, docs/live-upload.md): associations, client_bssids, probes and - with
# --client-devices - the client device rows (devices that have never been an
# AP) with their frequency histograms. Nothing on the server reads them; since
# schema 14 neither the live ingest nor a default snapshot import writes them.
#
# DRY RUN by default: prints what would be deleted and the space it holds.
#
#   cd ~/wifi-audit/server && sh ops/delete_client_rows.sh                    # dry run
#   ./backup.sh                                                               # first, and copy the dump off the box
#   sh ops/delete_client_rows.sh --execute [--client-devices] [--vacuum-full] 2>&1 | tee ~/delete-clients-$(date -u +%Y%m%dT%H%MZ).log
#
# --execute       delete, in ONE transaction (any error - e.g. a row still
#                 referencing a client device - leaves everything as it was)
# --client-devices also the devices that are not ever_ap, and their
#                 device_freq_hist rows (an AP Kismet re-typed as a client is
#                 ever_ap and stays)
# --vacuum-full   afterwards VACUUM FULL the emptied tables, so the files shrink
#                 and the disk space returns (locks each table while it is
#                 rewritten: seconds to a minute; plain VACUUM only makes the
#                 space reusable inside the tables)
# Irreversible except by restoring the dump. sensor_summary.devices_by_type
# counts the remaining devices after the next refresh_rollups().
set -eu
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "delete_client_rows: no .env next to docker-compose.yml" >&2; exit 1; }
set -a; . ./.env; set +a
psql_db() { docker compose exec -T db psql -X -q -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 "$@"; }

execute=0; devices=0; vacuum_full=0
for a in "$@"; do
  case "$a" in
    --execute) execute=1 ;;
    --client-devices) devices=1 ;;
    --vacuum-full) vacuum_full=1 ;;
    *) echo "delete_client_rows: unknown option $a" >&2; exit 2 ;;
  esac
done

echo "== $(date -u +%FT%TZ) what the client data holds"
psql_db <<'SQL'
\pset pager off
SELECT 'associations' AS table_name, count(*) AS rows, pg_size_pretty(pg_total_relation_size('associations')) AS table_size FROM associations
UNION ALL SELECT 'client_bssids', count(*), pg_size_pretty(pg_total_relation_size('client_bssids')) FROM client_bssids
UNION ALL SELECT 'probes', count(*), pg_size_pretty(pg_total_relation_size('probes')) FROM probes
UNION ALL SELECT 'devices (never an AP)', count(*) FILTER (WHERE NOT ever_ap), pg_size_pretty(pg_total_relation_size('devices')) FROM devices
UNION ALL SELECT 'device_freq_hist (of those)', count(*) FILTER (WHERE NOT d.ever_ap), pg_size_pretty(pg_total_relation_size('device_freq_hist'))
          FROM device_freq_hist f JOIN devices d USING (sensor_id, device_key);
SQL

if [ "$execute" -ne 1 ]; then
  echo "== dry run: nothing deleted (--execute after ./backup.sh)"
  exit 0
fi

echo "== $(date -u +%FT%TZ) deleting (one transaction)"
psql_db -v devices="$devices" <<'SQL'
\pset pager off
BEGIN;
SET LOCAL lock_timeout = '10s';
DELETE FROM associations;
DELETE FROM client_bssids;
DELETE FROM probes;
\if :devices
DELETE FROM device_freq_hist f USING devices d
WHERE d.sensor_id = f.sensor_id AND d.device_key = f.device_key AND NOT d.ever_ap;
DELETE FROM devices WHERE NOT ever_ap;
\endif
COMMIT;
SQL

if [ "$vacuum_full" -eq 1 ]; then
  echo "== $(date -u +%FT%TZ) VACUUM FULL"
  if [ "$devices" -eq 1 ]; then
    psql_db -c 'VACUUM (FULL, ANALYZE) associations, client_bssids, probes, device_freq_hist, devices'
  else
    psql_db -c 'VACUUM (FULL, ANALYZE) associations, client_bssids, probes'
  fi
else
  psql_db -c 'VACUUM (ANALYZE) associations, client_bssids, probes, device_freq_hist, devices'
fi
echo "== $(date -u +%FT%TZ) done"
