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

```
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f server/schema.sql
```

Idempotent; re-run after every schema change.

## 3. Import

Copy the snapshot to wherever psql runs (the server box), then:

```
python3 server/seed/import_snapshot.py buffer-snapshot.db --sensor pi-fri \
    --location "FRI, 3rd floor" --tz Europe/Bratislava \
    | psql "$DATABASE_URL" -v ON_ERROR_STOP=1
```

- `--sensor NAME` is the `sensors.name` the data is filed under (created on
  first use). `--location`, `--description` and `--tz` are optional; when omitted
  on a later run the stored values are kept (`tz` defaults to `UTC` for a new
  sensor and sets the day boundaries of the baseline statistics).
- The stream runs inside one transaction: on any error (bad MAC, unknown time
  zone, FK violation) psql stops and nothing is committed. Use `--dry-run` to see
  what the snapshot contains without emitting SQL.
- psql's command tags tell what happened: `COPY n` = rows staged from the
  snapshot, `INSERT 0 n` = rows actually inserted/updated. The stream ends with
  a per-table row count for the sensor.
- After loading, `refresh_ap_baselines(sensor_id, 200, 2)` is called (skip with
  `--no-baselines`, tune with `--min-obs/--min-days`).

Re-running with the same or a later snapshot is safe and accumulates: time
series rows are inserted only if new, `devices` keep the earliest `first_seen`
and latest `last_seen` (configuration from the newer row), `associations` and
`probes` merge first/last times. That is how the server keeps history beyond the
Pi's 14-day retention: snapshot again before the oldest observations are pruned.

## Checks after a seed

```sql
SELECT * FROM sensors;
SELECT count(*) FROM observations;                       -- ~870k for the 3.9-day buffer
SELECT count(*) FILTER (WHERE ok) * 100.0 / count(*) AS coverage_pct FROM polls;
SELECT type, count(*) FROM devices GROUP BY type ORDER BY 2 DESC;
SELECT count(*), percentile_cont(0.5) WITHIN GROUP (ORDER BY rssi_sd) AS median_sd,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY rssi_robust_sd) AS median_robust_sd
FROM ap_baselines;                                       -- ~106 APs, ~2.3 dB / ~1.5 dB
SELECT header, count(*) FROM alerts GROUP BY header;
SELECT ssid, bssid, oui, random_bssid, lifetime, rssi_median FROM ap_inventory
 WHERE NOT hidden ORDER BY lifetime DESC LIMIT 20;
```

The expected figures are those of `wifi-sensor/docs/findings.md` for the
snapshot of 2026-09-19; a later snapshot shifts them.
