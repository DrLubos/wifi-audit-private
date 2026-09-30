# Dashboard performance

What made the dashboard slow on the demo box, what server schema 6 changed,
and the measurements before and after. All times are measured on the real box
with the real data (UTC dates); the tools are in `../tests/perf/`.

## 1. Setup

| | |
|---|---|
| Box | GCP **e2-micro**, us-central1-c: 2 vCPUs sharing a **0.25-vCPU sustained** budget with bursting; 955 MB RAM + 4 GB swap file; 19 GB disk (14 GB used) |
| Stack | docker compose: postgres 17.11 (`shared_buffers` 128 MB, `work_mem` 8 MB), FastAPI / uvicorn (one worker, psycopg pool 1-4), Caddy 2 (plain HTTP on :80, `encode zstd gzip`) |
| Data | the seed 2026-09-15 14:55 .. 2026-09-27 14:27: 2,545,860 observation rows (1,848,717 of them APs), 580 APs among 108,554 devices, 32,169 polls, 68,620 config-history rows; database 918 MB + 544 MB WAL |
| Client | the developer's Mac in Slovakia, through Caddy over the internet |

Tools:

- `measure.sh` - per endpoint: `EXPLAIN (ANALYZE, BUFFERS)` cold (db restart +
  page-cache drop) and warm, HTTP to the api container (no Caddy) cold and warm
  (p50/p95/max of 20 back-to-back requests); sizes of every table and index.
  `MODE=coldstart` splits the cold start (restart only / cache drop only /
  both / the default harness), with a VM-stall meter, disk reads, swap-ins and
  per-process page faults (`coldstart_probe.py`).
- `e2e.sh` - every request of every page from the Mac through Caddy:
  `time_connect`, `time_starttransfer`, `time_total`, bytes on the wire and
  decompressed.
- `api_snapshot.sh` + `compare_api.py` - the API's answers for the busiest, a
  floor-heavy and a hidden AP, compared field by field across schema versions.

## 2. Diagnosis (schema 5, 2026-09-29)

Ranked by what a user of the dashboard waits for.

| # | Cause | Evidence | Effect |
|---|---|---|---|
| 1 | **Distance.** Every request is at least one round trip Slovakia - us-central1 | `time_connect` 190-203 ms; a trivial request on a fresh connection (`/api/health`) 348 ms | ~350 ms per request on a new connection, ~150-200 ms on a reused one; the JS bundle (121 kB gzip) takes 722 ms, 376 ms of it the transfer (TCP slow start: several round trips). Dominates every warm page load |
| 2 | **e2-micro CPU throttle.** Once the burst credit is spent the hypervisor pauses the whole VM | stall meter (sleep 10 ms, oversleep = stall): idle 0 stalls in 60 s; one busy thread runs 12.5 s at full speed after 60 s idle, then the VM is paused 210-220 ms of every ~250 ms (4.27 s of every 5 s). Under a request loop 135 stalls = 28.7 s of 60 s. vmstat `st` stays at 1-2 % (max 18 %): **steal time does not show it** | the warm outliers: `/api/findings` p50 7 ms but p95 227 ms = 7 + one 220 ms slice; `/api/alerts` 12 or 232 ms; every request of a throttled phase pays +220 ms per slice. Back-to-back benchmarks trigger it; a single page view (0.1-0.5 CPU-s) does not |
| 3 | **First request after a db restart waits in the connection pool** | `MODE=coldstart`: after `restart db` only, `/api/health` 2.5-3.1 s with 60 ms of api CPU, 2.4 MB read, no stall; after a page-cache drop only, every first request 66-149 ms. psycopg_pool 3.2 (`_getconn_with_check_loop`) sleeps 1 s, 2 s, 4 s (+-10 %) after each pooled connection that fails its check | the "~2 s on every endpoint alike" of the cold runs: not SQL, not the disk - a timer. Real, not only a test artefact: every db restart and every crash recovery (all backends killed) leaves dead pooled connections; three of them exceed the 5 s pool timeout (503) |
| 4 | **Server work per request** (what schema 6 removes) | warm SQL: overview 102 ms (two window scans over 32k polls, a 108k-row device count), AP page 102 ms (85 ms of it the config-changes view re-sorting 26k rows of the AP's history), `/api/aps` 4 ms SQL but 54 ms HTTP (FastAPI's encoder over 580 x 19 values, 264 kB) | ~100 ms of CPU per heavy request on a 0.25-vCPU budget - it also feeds cause 2. Cold: 376-822 random page reads per endpoint (`/api/aps` reads 580 APs scattered over 108k device rows; the AP page recomputes the usual country over all APs) |
| 5 | **Cold SQL itself** | EXPLAIN cold, planning + execution: overview 677 ms, `/api/aps` 557 ms, AP page 1.3 s, RSSI 426 ms, detections 310 ms; ~0.9 ms per 8 kB random read, cold catalog/planning 100-190 ms | only after a restart or when the pages were evicted |
| 6 | **Memory: swapped-out processes and evicted pages - the first visit after idle** | 240-316 MB of swap in use while ~300 MB of RAM was free (swapped out under earlier pressure, swapped in only on access; `vm.swappiness` 60); the api (uvicorn) had 27.6 MB swapped out against 22 MB resident, TwitchDropsMiner 53 MB. **First page loads after 3 h idle** (no restart, no cache drop; 2026-09-30 01:22, `e2e.sh first` from the Mac): overview 4.4 s, AP page 2.5 s, `/api/aps` 1.4 s, `/api/alerts` 1.1 s, `/api/health` 0.9 s, even the static JS bundle 1.5 s (Caddy's pages). During that pass: 2,122 major page faults - postgres 1,367 (its shared buffers had been swapped out), uvicorn 599, caddy 105 -, 20.6 MB swapped in, 50 MB read, iowait 30-53 % for ~15 s, CPU < 10 %, no throttle stall | the slowest case a real user meets: seconds per request, all of it disk waits |
| 7 | **Background CPU on the box** | idle box (pidstat, 111 s): 2.8 % of one vCPU = 11 % of the sustained budget. Two thirds of it are the healthchecks: the api healthcheck (every 30 s) starts a python that compiles `urllib`, `http.client`, `email`, ... from source (the image has no stdlib bytecode, `PYTHONDONTWRITEBYTECODE`) - 0.58 % of a vCPU for that python alone, 0.5-0.9 s per probe idle and 4-5 CPU-s plus a 5 s timeout under load - plus dockerd/containerd/shim for every `docker exec` (0.82 %) and the db healthcheck every 10 s. TwitchDropsMiner (a second, unrelated compose project): 0.59 % | drains the 0.25-vCPU budget that cause 2 is about |
| 8 | **Crash recoveries on 2026-09-27** | three backends exited with code 2 (15:52:55, 17:01:43, 17:31:05), each during memory pressure while a `detect` run was connected; no OOM kill in the kernel log; each time "database system was not properly shut down; automatic recovery" | the cumulative statistics were reset (`n_live_tup` 0, no `last_analyze` on every table but `ap_baselines`): PostgreSQL discards them after crash recovery. Planner statistics (`pg_statistic`) survive |

Not a cause: plans (index-only scans, 0 heap fetches), CPU steal as reported
by the guest, Caddy (`/api/findings` 8.0 ms through Caddy vs 6.4 ms direct,
p50), disk space (4.4 GB free).

### Sizes the client receives (schema 5)

| Request | raw | gzip |
|---|---|---|
| index.html | 395 B | (below Caddy's minimum) |
| JS bundle (vite build) | 345,190 B | 121,038 B |
| CSS | 9,398 B | 2,997 B |
| `/api/overview` | 1,032 B | 475 B |
| `/api/aps` | 264,364 B | 29,309 B |
| `/api/aps/{key}` (busiest AP) | 15,889 B | 1,911 B |
| `/api/aps/{key}/rssi?bucket=3600` | 8,319 B | 1,924 B |
| `/api/detections` | 16,562 B | 2,780 B |

## 3. Changes

**Server schema 6** (`schema.sql`): four read tables and a materialized view,
written only by `refresh_rollups(sensor_id, from)` - the importer calls it
after every load:

- `ap_rssi_hourly` (per AP and UTC hour: counts, valid-RSSI median/p10/p90/min/max; floors counted, never averaged),
- `sensor_hourly` (per hour: polls, hop coverage, gap seconds, active/new APs, alerts, AP rows),
- `ap_summary` (one row per AP: everything `/api/aps` and the AP page header show),
- `sensor_summary` (the overview; `refreshed_at` versions all of them),
- `ap_config_changes` materialized (same definition, same fixture test).

API: `/api/overview`, `/api/aps`, `/api/aps/{key}` read only these; the RSSI
chart is hourly from `ap_rssi_hourly`, raw 15-minute buckets only for a window
of at most 48 h; `/api/aps` is one JSON text built by PostgreSQL; detections
without evidence (`/api/detections/{id}` has it); ETag from `refreshed_at`, a
matching `If-None-Match` gets 304 before any query.

Box and image: the api pool drops dead connections on every `/api/health`
(the healthcheck) and closes extra idle ones after 120 s (cause 3); the api
image ships stdlib bytecode, api healthcheck every 60 s, db healthcheck every
30 s (cause 7); `stop_grace_period: 60s` for db.

## 4. Before / after

Before: schema 5, 2026-09-29/30. After: schema 6 deployed, measured
2026-09-30 07:17-07:40 UTC with the same tools and the same data (no import
in between). Raw files: `~/perf-before.txt`, `~/perf-after.txt`,
`~/perf-after-coldstart.txt`, `~/perf-after-e2e-warm.txt` on the Mac.

**Same answers.** `api_snapshot.sh` before and after, `compare_api.py`:
0 differences - overview, all 580 AP-list rows, the detection list, and for
the busiest AP (IK-WIFI, 29,861 readings), a floor-heavy one ("Šariš Hilton",
3,691 of 3,929 readings at a floor) and a hidden one (UZVD-zamestnanci):
observation / valid / floor counts, first and last reading, 160 / 0 / 165
config-change events with their fields, hidden-AP name, baseline, and every
hourly bucket (median, min, max, n, n_floor) and 15-minute bucket of the last
48 h. The overview gains `ap_observations` and `refreshed_at`. A matching
`If-None-Match` gets 304 with no body through Caddy on every schema-6 endpoint.

**On the box, api container direct** (`measure.sh`; warm = 20 back-to-back
requests, so p95/max include throttle slices; SQL = planning + execution of
the endpoint's statements, cold = after db restart + page-cache drop):

| Endpoint | SQL cold, before | after | SQL warm, before | after | HTTP cold, before | after | HTTP warm p50 / p95 / max, before | after |
|---|---|---|---|---|---|---|---|---|
| `/api/overview` | 677 ms (675 pages read) | 112 ms (5) | 106 ms | 2 ms | 4,850 ms | 326 ms | 105 / 312 / 601 ms | 9 / 61 / 78 ms |
| `/api/aps` | 557 ms (376 pages) | 71 ms (20) | 5 ms | 13 ms | 2,967 ms | 473 ms | 54 / 122 / 207 ms | 37 / 91 / 180 ms |
| `/api/aps/{key}` | 1,296 ms (822 pages) | 128 ms (12) | 106 ms | 6 ms | 2,272 ms | 579 ms | 110 / 139 / 308 ms | 26 / 76 / 153 ms |
| `/api/aps/{key}/rssi` (hourly) | 426 ms (321 pages) | 276 ms | 35 ms | 1 ms | 2,191 ms | 265 ms | 43 / 72 / 79 ms | 24 / 41 / 43 ms |
| `/api/detections` | 310 ms (31 pages) | 239 ms (26) | 3 ms | 7 ms | 2,203 ms | 562 ms | 14 / 25 / 43 ms | 18 / 26 / 64 ms |
| 15-min RSSI view | whole history, 31 kB | last 48 h: 446 ms | - | 8 ms | | | 80 / 126 / 207 ms | 29 / 72 / 73 ms |

- HTTP cold "after" is the endpoint's request; the `/api/health` request
  just before it (the reconnect to the restarted db) took 0.4-0.9 s, which
  the "before" figures still contained.
- `/api/aps` stays the heaviest warm request: its SQL builds the 265 kB JSON
  (13 ms warm instead of 5 ms of plain rows), but the Python encoding (~50 ms
  before) is gone. Paging comes with the new frontend.
- Detections: the list no longer carries the evidence (16,562 -> 4,569 B);
  its SQL got slightly slower warm (7 ms vs 3 ms: the AP lookup on
  `ap_summary` has no index on bssid - 580 rows, not worth one).
- The raw 15-minute view costs 446 ms cold (274 ms of it planning with a cold
  catalog), 8 ms warm.
- Storage of the read tables: 6.4 MB in all (`ap_rssi_hourly` 3.9 MB,
  `ap_config_changes` 2.1 MB, `ap_summary` 248 kB, `sensor_hourly` 64 kB).

**Cold start** (`MODE=coldstart`, first request of each variant):

| Variant | `/api/health` before | after | first `/api/overview` before | after | first `/api/aps/{key}` before | after |
|---|---|---|---|---|---|---|
| db restart only | 2,494 / 3,120 ms | 1,038 ms | 1,751 / 527 ms | 24 ms | 2,421 / 1,638 ms | 180 ms |
| page-cache drop only | 66 ms | 107 ms | 89 ms | 29 ms | 118 ms | 20 ms |
| restart + drop | 355 / 3,887 ms | 90 ms | 851 / 915 ms | 433 ms | 1,109 / 1,074 ms | 193 ms |
| restart + drop, in-container harness | 63 ms | 855 ms | 4,505 / 815 ms | 328 ms | | |

After a restart the pool now discards all dead connections in one
`pool.check()` (api log: three "discarding broken connection" in the same
millisecond, no backoff sleeps). The remaining ~1 s of the first request
after a restart is page-ins on the request path (docker-proxy 77 and Caddy 28
major faults, 14 MB read), not the pool.

**From the Mac through Caddy** (`e2e.sh`; each request on its own
connection). The network differed between the runs - TCP connect ~195 ms
before, ~130 ms after - so the last two columns give each API call minus
`/api/health` of the same pass: the server's share above a trivial request.

| Request | first after 3 h idle, before | after | warm, before | after | warm over `/api/health`, before | after |
|---|---|---|---|---|---|---|
| `/` (index.html) | 333 ms | 274 ms | 353 ms | 270 ms | | |
| JS bundle (121 kB gzip) | 1,477 ms | 1,169 ms | 727 ms | 655 ms | | |
| `/api/health` | 907 ms | 620 ms | 458 ms | 287 ms | 0 | 0 |
| `/api/overview` | 4,424 ms | 1,852 ms | 522 ms | 273 ms | +64 ms | -14 ms |
| `/api/findings` | 830 ms | 551 ms | 342 ms | 278 ms | -116 ms | -9 ms |
| `/api/alerts?limit=50` | 1,121 ms | 744 ms | 362 ms | 282 ms | -96 ms | -5 ms |
| `/api/aps` | 1,362 ms | 1,135 ms | 568 ms | 422 ms | +110 ms | +135 ms |
| `/api/aps/{key}` | 2,487 ms | 500 ms | 593 ms | 285 ms | +135 ms | -2 ms |
| `/api/aps/{key}/rssi` | 471 ms | 326 ms | 376 ms | 283 ms | -82 ms | -4 ms |
| `/api/detections` | 817 ms | 342 ms | 392 ms | 278 ms | -66 ms | -9 ms |

Warm, every page's API calls now sit at the network floor except `/api/aps`
(29 kB gzip: the transfer's extra round trips). The negative deltas before
come from `/api/health` itself being slow in that pass (median 458 ms, max
649 ms); the cause of that was not measured.

**First visit after idle** (`e2e.sh first`, one request each, no restart, no
cache drop; on the box vmstat and per-process /proc counters around the pass;
all three runs the same way). Last request before each idle period and start
of the measurement:

| Run | Schema | `vm.swappiness` | Idle from | Measured at | Idle duration |
|---|---|---|---|---|---|
| A | 5 | 60 | ~2026-09-29 22:22 | 2026-09-30 01:22 | 3 h 00 min |
| B | 6 | 60 | 2026-09-30 07:40:22 | 2026-09-30 13:49:55 | 6 h 09 min |
| C | 6 | 10 (set 14:09:00, one warm pass at 14:11) | 2026-09-30 14:11:45 | 2026-09-30 22:41:26 | 8 h 29 min |

| | A: schema 5, 3 h | B: schema 6, 6 h | C: schema 6 + swappiness 10, 8.5 h |
|---|---|---|---|
| `/api/overview` (first API call after `/api/health`) | 4,424 ms | 1,852 ms | 3,043 ms |
| AP page (`/api/aps/{key}`) | 2,487 ms | 500 ms | 1,041 ms |
| `/api/aps` | 1,362 ms | 1,135 ms | 1,020 ms |
| `/api/alerts?limit=50` | 1,121 ms | 744 ms | 974 ms |
| `/api/health` | 907 ms | 620 ms | 566 ms |
| JS bundle | 1,477 ms | 1,169 ms | 1,192 ms |
| major page faults during the pass | 2,122 (postgres 1,367, uvicorn 599, caddy 105) | 943 (uvicorn 477, postgres 322, caddy 53) | 1,082 (uvicorn 572, postgres 284, caddy 115) |
| swapped in | 20.6 MB | 6.8 MB | 6.5 MB |
| read from disk (pgpgin, incl. swap-in) | 49.4 MB | 26.3 MB | 90.7 MB |
| swap in use at the start | 318 MB | 215 MB | 192 MB |
| seconds with iowait > 5 % (max) | 15 s (53 %) | 13 s (48 %) | 13 s (65 %) |
| TCP connect | ~195 ms | ~135 ms | ~135 ms |

What each change did:

- **Schema 6 (A -> B)** removed the database's share of the cold visit:
  postgres faults 1,367 -> 322, swap-in 20.6 -> 6.8 MB, the AP page 2.5 s ->
  0.5 s, the overview 4.4 s -> 1.9 s, despite twice the idle time. The pages
  the dashboard needs are now a few MB of read tables instead of the
  observations index and the devices table.
- **`vm.swappiness` 10 (B -> C)** showed no benefit in this run. Swap-in stayed
  the same (6.8 -> 6.5 MB) and uvicorn still paged the most (477 -> 572
  faults): the api's rarely used memory gets swapped out during a long idle
  at either setting. Disk reads rose from 26 to 91 MB and the overview was
  slower (3.0 s), consistent with the kernel now evicting file pages (data
  files, binaries) instead of process memory. One run each, with different
  idle durations (6 h vs 8.5 h) and times of day, is not enough to call it
  worse; it did not remove the remaining cost. What the first visit still
  pays is the api, Caddy and file pages coming back from disk after hours of
  idle - outside the schema's reach; keeping them resident would take a
  periodic warm request or more RAM, not a schema change.

Warm pass right after each idle run: every API call 288-350 ms, `/api/aps`
447-454 ms - the network floor, as in the warm table above.
