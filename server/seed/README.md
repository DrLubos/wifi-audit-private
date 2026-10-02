# Seeding the server from a Pi buffer snapshot

One-time (repeatable) path from the sensor's SQLite buffer into the server's
PostgreSQL. Two stdlib-only Python scripts; psql does the database work.

```
Pi: buffer.db  --export_snapshot.py-->  buffer-snapshot.db  --scp-->  server box
                                                                        |
                              import_snapshot.py  ->  SQL + COPY stream -> psql
```

## 1. Snapshot on the Pi

The collector keeps running; the source is opened read-only and `VACUUM INTO`
writes a compact, consistent copy (one self-contained file, no `-wal`/`-shm`).
Run as the sensor user (the owner of `buffer.db`). Write to the home directory,
not `/tmp` (a RAM disk on recent Pi OS; the buffer is a few hundred MB).

```
ssh pi 'python3 - ~/buffer-snapshot.db' < server/seed/export_snapshot.py
scp pi:~/buffer-snapshot.db .
```

The script prints the row counts and the poll span of the copy. If the ssh user
is not the sensor user: `ssh pi 'sudo -u pi python3 - /var/tmp/buffer-snapshot.db'`.

## 2. Schema

In the compose stack the `db` container runs `schema.sql` itself on its first
start (`/docker-entrypoint-initdb.d/`). To re-apply it after a change (it is
idempotent), or against a database you reach directly:

```
cd server
docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 < schema.sql
# or:  psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f server/schema.sql
```

(Pipe the host file in; the copy mounted inside the container goes stale after
a `git pull` - see the note in `../README.md`.)

## 3. Import

Copy the snapshot to the server box and pipe the stream into psql inside the
`db` container (the DB port is not published; psql on the container's unix
socket needs no password):

```
cd server
python3 seed/import_snapshot.py buffer-snapshot.db --sensor pi-fri \
    --location "FRI, 3rd floor" --tz Europe/Bratislava \
    | docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1
# or, with direct access:  ... | psql "$DATABASE_URL" -v ON_ERROR_STOP=1
```

`$POSTGRES_USER`/`$POSTGRES_DB` are the values from `.env`
(`set -a; . ./.env; set +a`).

- `--sensor NAME` is the `sensors.name` the data is filed under (created on
  first use). `--location`, `--description` and `--tz` are optional; when omitted
  on a later run the stored values are kept (`tz` defaults to `UTC` for a new
  sensor and sets the day boundaries of the baseline statistics).
- The stream runs inside one transaction: on any error (bad MAC, unknown time
  zone, FK violation) psql stops and nothing is committed. Use `--dry-run` to see
  what the snapshot contains without emitting SQL.
- psql's command tags tell what happened: `COPY n` = rows staged from the
  snapshot, `INSERT 0 n` = rows actually inserted/updated. For `polls`,
  `observations` and `alerts` the inserted rows and the earliest inserted `ts`
  are collected in the session's temp table `import_from` instead (their tag
  reads `INSERT 0 1`) and printed after COMMIT. The transaction ends with a
  per-table row count for the sensor.
- After loading, `refresh_ap_baselines(sensor_id, 200, 2)` is called (skip with
  `--no-baselines`, tune with `--min-obs/--min-days`).
- After COMMIT the stream runs `VACUUM (ANALYZE) observations` (outside the
  transaction): the AP queries read `observations` through a covering index as
  index-only scans, which need the new heap pages marked all-visible; without
  it they fetch one heap page per reading until autovacuum catches up (days of
  imports). A failed load stops before it (ON_ERROR_STOP).
- Then `refresh_rollups(sensor_id, earliest inserted ts)` rebuilds the
  dashboard tables (schema 6) from that hour on - the whole history on the
  sensor's first refresh; nothing inserted = the latest hour and the per-AP
  and per-sensor rows. It runs in its own transaction after the vacuum: if it
  fails, the import stays; fix the cause and run
  `SELECT * FROM refresh_rollups(<sensor_id>)` by hand (idempotent). Skip it
  with `--no-rollups`. Measured cost of a full-history run on the 12-day seed:
  ~16 s for the AP hour aggregate plus ~5 s for the configuration view.
- **Server schema 7 / buffer v6:** only AP observation rows are imported
  (filtered by the snapshot's device type and again by the merged server
  type), with the columns the server keeps (`ts`, `rssi`, `rssi_floor`,
  `pk_total`, `pk_data`, `disconnects`, `disconnects_last`, `bss_timestamp`);
  the buffer's other observation columns are ignored. A client's BSSID goes to
  `client_bssids` - from a v6 buffer's `client_bssids` table plus the per-poll
  `observations.bssid` of rows written before v6 (or by a v5 collector),
  merged by first/last poll. A v6 buffer's AP load (`devices.cur_*`) wins when
  its `cur_at` is at least as new as the stored one; a v5 snapshot leaves it
  unchanged. Apply schema 7 (`ops/migrate_schema7.sh`) before importing with
  this importer.
- Buffer schema v2 to v6 snapshots are accepted. v3 (collector deployed
  2026-09-22 or later) carries `observations.disconnects_last`, v4 (2026-09-27 or
  later) `polls.ds_hop_n / ds_hop_visited / ds_hop_ok` (hop-list coverage), v5
  `observations.rssi_floor` (the RSSI was an adapter floor value, stored as NULL
  in `rssi`); from older snapshots those columns are imported as NULL. Older
  snapshots carry the floor values -106/-120 raw in `rssi`; the server does not
  rewrite them, every reader applies `rssi_valid()` (`schema.sql`, schema 4). Apply the current
  `schema.sql` (step 2) before importing a newer snapshot, otherwise the INSERT
  fails on the unknown columns.

Re-running with the same or a later snapshot is safe and accumulates: time
series rows are inserted only if new, `devices` keep the earliest `first_seen`
and latest `last_seen` (configuration from the newer row), `associations` and
`probes` and `client_bssids` merge first/last times. That is how the server keeps history beyond the
Pi's 14-day retention: snapshot again before the oldest observations are pruned.

## Checks after a seed

```sql
SELECT * FROM sensors;
SELECT count(*) FROM observations;                       -- ~870k for the 3.9-day buffer
SELECT count(*) FILTER (WHERE ok) * 100.0 / count(*) AS coverage_pct FROM polls;
SELECT type, count(*) FROM devices GROUP BY type ORDER BY 2 DESC;
SELECT count(*), percentile_cont(0.5) WITHIN GROUP (ORDER BY rssi_sd) AS median_sd,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY rssi_robust_sd) AS median_robust_sd
FROM ap_baselines WHERE rssi_median IS NOT NULL;         -- APs that qualify now
SELECT header, count(*) FROM alerts GROUP BY header;
SELECT ssid, bssid, oui, random_bssid, lifetime, rssi_median FROM ap_inventory
 WHERE NOT hidden ORDER BY lifetime DESC LIMIT 20;
```

The expected figures are those of `wifi-sensor/docs/findings.md` for the
snapshot of 2026-09-19; a later snapshot shifts them, and since schema 4 the
baselines exclude the RSSI floor values (findings sections 2 and 11: about 95 APs,
median sd ~2.4 dB for that window). `refresh_ap_baselines()` clears the
statistics of an AP that no longer qualifies (the row stays, for its `trusted`
flag), hence the `rssi_median IS NOT NULL` filter.
