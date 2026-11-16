# Disk and data retention on the box - the decision before ~February 2027

Status: **design, nothing built** (2026-11-16). Follows risk R1 of
`dashboard-v2/PLAN.md` and options C/D of `live-upload.md` 9.3. The decision
is the developer's; this file gives the numbers and a recommendation.

## 1. Measured 2026-11-16 ~14:15 UTC (read-only: `df`, `docker ps -s`, psql as `claude_ro`)

| Item | Size | Note |
|---|---|---|
| Disk | 30.1 GB, **9.85 GB free** | 13.8 GB free on 2026-10-24: -3.95 GB in 23 days (~170 MB/day) |
| Database (`pg_database_size`) | **2.78 GB**, 2.94 GB after the client import below | 1.86 GB on 2026-10-24: **+40 MB/day** |
| - `observations` | 2.15 GB | 9.08 M rows since 2026-09-15; ~149k rows/day (last 14 days) |
| - `devices` | 147 MB -> 198 MB | 222,866 rows (221,355 clients) -> 491,968 (490,453 clients): the Pi's whole client inventory, copied on 2026-11-16 before its client retention |
| - client tables (`probes`, `client_bssids`, `associations`) | 90 MB -> 149 MB | probes 325k, client BSSIDs 267k, associations 31k (2026-09-15 .. 11-16), the same copy; no new client rows arrive (the live ingest takes none) |
| - everything else | ~0.4 GB | rollups, load history, configuration history, polls, alerts, ... |
| `server/backups/` | 1.1 GB | 7 dumps of ~143-150 MB (+~1 MB/day each) and `keep/` (104 MB) |
| Container `twitch-drops-miner` (not this project) | **3.5 GB** writable layer | 990 MB image on 2026-10-17; most of the free space lost since 10-24 that is not the database |
| Docker reclaimable | ~0.7 GB | images 632 MB, build cache 83 MB (`docker image prune`, `docker builder prune -a`) |

## 2. How long it lasts

Keeping >= 1 GB free (vacuum, temp files, image builds) leaves ~8.8 GB.

| Growth | per day | 1 GB floor reached |
|---|---|---|
| this project only: database ~40 MB + dumps ~7 MB | ~47 MB | **~late May 2027** (~190 days) |
| plus the other container, if it keeps growing as since 10-24 (~2.5 GB in 23 days, not measured directly) | ~150-170 MB | **~mid-January 2027** |

The notifier (`api/app/notify.py`, since 2026-11-16) mails when the box has
less than `NOTIFY_DISK_MIN_GB` (2 GB) free.

## 3. Options

- **A. The other container** (`twitch-drops-miner`): its writable layer is the
  largest unknown. Recreating it (or limiting what it writes) is the
  developer's - outside this project.
- **B. Raw-observation retention** (live-upload 9.3 C): keep raw
  `observations` for N days on the box (e.g. 180), everything derived
  forever (`ap_rssi_hourly`, `ap_load_hourly`, events, baselines,
  configuration history). Before a month goes, it is archived as a dump
  (`pg_dump --table=observations` of that month, sha256, copied off the box
  by hand); `ops/restore_observations_from_dump.sh` brings a month back.
  Deleting one day per night (~150k rows) keeps the WAL and the vacuum small
  on the e2-micro; the freed space is reused by the table (not returned to the
  filesystem), so `observations` stops growing at ~N/60 of 2.15 GB per 60 days
  of data. Cost: `evil-twin`, `deauth-flood`, `fp-audit` and
  `refresh_ap_baselines()` over windows older than N days need the archive
  restored first; the dashboard's raw-RSSI view (48 h) is unaffected.
- **C. Partition `observations` by month** (live-upload 9.3 D): retention
  becomes `DROP PARTITION` (instant, no bloat). It needs one rewrite of the
  table - ~2.2 GB extra free plus temp space and a maintenance window with
  ingest off (`INGEST_ENABLED=0`; the sensor keeps its 14 days) - so it fits
  only while the box has > ~4 GB free (project growth only: until ~March
  2027). Worth it only together with B, and only if B's nightly deletes turn
  out too slow.
- **D. A larger disk**: an online resize (`gcloud compute disks resize`,
  `growpart`, `resize2fs`; snapshot first). Above 30 GB it leaves the free
  tier (billed per GB-month). Minutes of work, the developer's (Claude never
  runs gcloud).
- **E. Nothing**: if data collection ends by ~May 2027 and A is handled, the
  project's own growth fits.

## 4. Recommendation

1. Now: look at A (the developer's call) and keep the notifier on.
2. Decide by **2027-02-01**: if collection runs past ~May 2027, B with N = 180
   days and monthly archives - no rewrite needed; C only if B proves too slow;
   D if the archives are not wanted at all.
3. What the decision has to fix: N; where the monthly archives live (the Mac,
   by hand, like the backups); and that detector or baseline re-runs older
   than N days start with a restore. Then the scripts (archive + nightly
   delete, each a dry run by default, with a fixture test) are ~1 day.
