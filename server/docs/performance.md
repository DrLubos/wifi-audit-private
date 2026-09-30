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

The "after" columns are filled once schema 6 is deployed and measured with
the same tools.

**On the box, api container direct** (`measure.sh`; warm = 20 back-to-back
requests, so p95/max include throttle slices; SQL = planning + execution of
the endpoint's statements, cold = after db restart + page-cache drop):

| Endpoint | SQL cold, before | SQL cold, after | SQL warm, before | SQL warm, after | HTTP warm p50 / p95 / max, before | after |
|---|---|---|---|---|---|---|
| `/api/overview` | 677 ms (675 pages read) | | 106 ms | | 105 / 312 / 601 ms | |
| `/api/aps` | 557 ms (376 pages) | | 5 ms | | 54 / 122 / 207 ms | |
| `/api/aps/{key}` | 1,296 ms (822 pages) | | 106 ms | | 110 / 139 / 308 ms | |
| `/api/aps/{key}/rssi` (hourly) | 426 ms (321 pages) | | 35 ms | | 43 / 72 / 79 ms | |
| `/api/detections` | 310 ms (31 pages) | | 3 ms | | 14 / 25 / 43 ms | |

**From the Mac through Caddy** (`e2e.sh`; each request on its own connection,
so every line includes one ~195 ms TCP connect; warm = median of 5):

| Request | first after 3 h idle, before | after | warm, before | after |
|---|---|---|---|---|
| `/` (index.html) | 333 ms | | 353 ms | |
| JS bundle (121 kB gzip) | 1,477 ms | | 727 ms | |
| `/api/health` | 907 ms | | 458 ms | |
| `/api/overview` | 4,424 ms | | 522 ms | |
| `/api/findings` | 830 ms | | 342 ms | |
| `/api/alerts?limit=50` | 1,121 ms | | 362 ms | |
| `/api/aps` | 1,362 ms | | 568 ms | |
| `/api/aps/{key}` | 2,487 ms | | 593 ms | |
| `/api/aps/{key}/rssi` | 471 ms | | 376 ms | |
| `/api/detections` | 817 ms | | 392 ms | |

Cold start after a db restart (`MODE=coldstart`, first request, before):
`/api/health` 2.5, 3.1 and 3.9 s in three of four runs (pool check backoff;
0.36 s once, when the compose healthcheck had already met the dead
connection), then the first `/api/overview` 0.5-1.8 s,
first `/api/aps/{key}` 1.1-2.4 s; after a page-cache drop alone 66-149 ms.
