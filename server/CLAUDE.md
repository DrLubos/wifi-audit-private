# CLAUDE.md — wifi-audit server (backend)

Persistent context for the server side. Read at the start of every session.
The Pi sensor lives in ../wifi-sensor/ — read ../wifi-sensor/CLAUDE.md for its
context, and collector/store.py + collector/shape.py there for the exact record
shape the sensor produces. Do not modify anything under ../wifi-sensor/ from here.

## What this is

The backend that receives, stores and (later) analyses data from the fixed passive
Wi-Fi sensor. Part of the wifi-audit monorepo. It is the "more than a dumb store"
layer: it keeps the sensor's time series, learns each AP's baseline, and surfaces
detections and a dashboard for the thesis demo/defense.

## Deployment target (real constraints)

- Single GCP box: ~1 GB RAM, 1 shared core, us-east, with swap available. The
  developer has already run a Postgres + FastAPI + Caddy + React docker-compose stack
  on this same box (the VehicleLocalization bachelor project - https://github.com/DrLubos/VehicleLocalization), so this shape is proven
  here. Everything runs ON the server, as containers — nothing is built or served
  from a local machine.
- Runs as a **docker-compose stack**, one container per tier, modelled on the
  developer's VehicleLocalization layout.
- Deplyment server is available via `ssh google` it is google instance.

## Containers (this is the target architecture)

Three services for now (the set the developer asked for):

- **db** — `postgres:17` (plain Postgres, NOT PostGIS: this project has no
  coordinates; `location` is free text). `server/schema.sql` is mounted read-only
  into `/docker-entrypoint-initdb.d/` so it runs automatically on first init. A named
  volume holds the data. The DB port is NOT published to the host.
- **api** — FastAPI (Python), one container serving both the sensor ingest endpoints
  (`/api/ingest/*`, later) and the dashboard read endpoints (`/api/*`). Talks to db
  over the internal compose network. (VL split ingest and web into two FastAPI
  containers; keep it as ONE here unless auth cleanly separates them.)
- **caddy + React (frontend tier)** — Caddy serves the built React `dist` directly
  and reverse-proxies `/api/*` to the api container; it also terminates TLS. This is
  the only container that publishes ports (80/443). "React + Caddy" = this one tier,
  so no separate static/apache container is needed.

Conventions for the stack:
- Secrets (DB user/password/name, DATABASE_URL) come from a **gitignored `.env`** that
  compose reads — never hardcoded in `docker-compose.yml` or committed (VL hardcoded
  them; do not copy that here).
- Publish only Caddy's 80/443. Do not expose the Postgres port to the host.
- Each service has its own Dockerfile where it needs one; pin base image tags.

## Current scope (respect these boundaries)

- **Milestone 3: the dashboard on REAL data**, built (dashboard v2).
- **Live upload (now, approved 2026-10-17): `docs/live-upload.md`** - the
  sensor uploads each poll (AP devices, polls, configuration history, AP
  observations, AP load, AP frequency histogram, alerts; never the client
  tables) to `/api/ingest/v1`; one merge for ingest and snapshot import
  (`ingest_merge()`); the snapshot import is a recovery tool from then on.
  Live since 2026-10-17 20:46:38 UTC (rollout record: its section 17):
  schema 15 (`ap_config_changes` is a table maintained incrementally by
  `refresh_config_changes()`; the rules live in the view
  `ap_config_changes_v`), the `refresher` compose service (rollups once a
  minute from the dirty mark, contact periods, nightly baselines; since
  schema 16, 2026-10-18, one `refresh_runs` row per step - the
  refresh-duration series of `evaluation/pipeline/`), ingest tokens via
  `ops/sensor_token.sh` only. The box serves HTTPS as
  `34-42-184-97.sslip.io` (Let's Encrypt, HSTS) - the uploader depends on
  that name, so the external IP must not change.
- Later still: detection. For the demo, at most ONE illustrative, honest detection
  (unknown BSSID advertising a protected SSID, gated by an operator-approved
  known-good whitelist). Rigorous detection + ROC vs staged attacks is thesis work
  after the topic is approved — do not build it now.
- **Prototype detectors exist in `server/detection/`** (batch, run on demand with
  `docker compose run --rm detect <detector> --from .. --to ..`; never scheduled,
  never part of ingest; read-only on every source table, writes only `detections`,
  idempotent re-runs). One so far: `deauth-flood`. Read `detection/README.md`
  before touching it. Hard-won fact: `observations.disconnects` (Kismet
  `client_disconnects`) is NOT a cumulative counter but the size of the current
  burst (never > 11, resets to 1 after a pause and after every DEAUTHFLOOD alert),
  so never build on its deltas; only a change to a non-zero value carries
  information ("at least one new burst since the previous poll"). Since buffer
  v3 (collector redeploy 2026-09-22) `observations.disconnects_last` holds the
  unix second of the AP's last deauth/disassoc frame (Kismet sets it on every
  such frame): a value newer than the previous poll is an exact "activity in
  this interval" bit and dates the event. NULL on everything seeded before v3 -
  detectors must fall back to the counter rule there. `num_alerts` is dead in
  Kismet (never incremented) and is not stored.

## Database

- **PostgreSQL**, schema in `server/schema.sql` (already written and committed):
  mirrors the collector's tables with a `sensor_id` on every row, plus `sensors`,
  `ap_baselines` and `detections`. It is **TimescaleDB-ready** (observations keyed
  `(sensor_id, device_key, ts)` since schema 7 - ts is in the key, which is all a
  hypertable needs) but Timescale is NOT enabled — plain Postgres has
  zero overhead for the seeded demo. The commented `create_hypertable` block at the
  end is turned on only when live ingest and volume arrive.
- **Multi-sensor from the start:** every time-series/device row carries `sensor_id`,
  even though there is one sensor today (the thesis framing is an overlay of sensors).
- Seed path in `server/seed/` (export a Pi snapshot -> import with psql). Idempotent
  and accumulating, so the server keeps history the Pi's 14-day retention discards.
- **Dataset rules (schema 4-5, 2026-09-27/28; `../wifi-sensor/docs/findings.md` §11):**
  - **RSSI floors -106/-120 dBm are censored values, not levels** (rtw88 CCK/OFDM
    clamps of the capture adapter; 3.6 % of all readings). Every RSSI reader goes
    through `rssi_valid(rssi)` (floor -> NULL) and counts floors with
    `rssi_is_floor(rssi, rssi_floor)`; never compute a median/MAD/threshold on raw
    `observations.rssi`. Raw history is **not** rewritten (rows before buffer v5
    carry the raw value; from v5 the collector stores NULL + `rssi_floor`).
    `ap_baselines.n_floor` keeps the censoring; an AP whose floor share exceeds
    0.35 is not eligible for evil_twin (b) - never just drop floors from a weak
    AP's median (biased upward). The floor list lives once in `schema.sql`; a
    collector test keeps it equal to `wifi-sensor/collector/dataset_rules.py`.
  - **`device_config_history` before the v5 collector is ~42 % artefact**
    (A -> incomplete beacon record -> A: channel/HT/beacon rate/country NULL).
    Never read raw history rows as changes: the view **`ap_config_changes`**
    (schema 5; materialized since schema 6, refreshed by `refresh_rollups()`)
    is the one reading of the configuration timeline - no-record
    states skipped, NULL = unknown per field, `crypt_bits` 0 unknown unless
    crypt is `Open` (so a WPA2 -> Open downgrade still shows), a hidden AP's
    cloaked beacon (`ssid` '') vs its named record is one state.
    `ap_channel_changes` is its `adv_channel` rows (detectors use it). Keep every
    history column (crypt/MFP/country/beacon-rate changes are security signals).
    Fixture test: `tests/sql/ap_config_changes_test.sql` (must stay green; it
    proves the rules cannot hide a downgrade).
  - **The dashboard reads read tables, not the time series (schema 6,
    2026-09-29):** `ap_rssi_hourly`, `sensor_hourly`, `ap_summary`,
    `sensor_summary` and the materialized `ap_config_changes` are derived data,
    written only by `refresh_rollups(sensor_id, from)` (the importer calls it
    after every load; idempotent; incremental = full, fixture test
    `tests/sql/refresh_rollups_test.sql`). `sensor_summary.refreshed_at` is the
    api's ETag. The dashboard endpoints never read raw
    observations; raw 15-min RSSI buckets only for a window of at most 48 h.
    Measurements before/after: `docs/performance.md`.
  - **Slim observations (schema 7 / collector buffer v6, `ops/migrate_schema7.sh`):**
    the server keeps **AP rows only** (other device types stay on the Pi for its
    14 days and in the backups) with `ts, sensor_id, device_key, rssi,
    rssi_floor, pk_total, pk_data, disconnects, disconnects_last,
    bss_timestamp`. The AP load is the latest value on `devices.cur_*` (never
    through the configuration history), a client's BSSID is in `client_bssids`
    (deduplicated like `associations`). Keying rule: **PRIMARY KEY (sensor_id,
    device_key, ts) INCLUDE (rssi, rssi_floor)** plus a **BRIN on ts** (rows are
    written in ts order); ts stays in the key, so TimescaleDB stays possible.
    The pre-migration table is kept as `observations_v5` until the developer
    drops it. Read-only check of the migration: `tests/sql/check_schema7.sql`.
  - **AP queries must stay index-only scans** on the primary key
    `observations_pkey` (INCLUDE rssi, rssi_floor): a column outside the index
    makes every AP page read ~30k heap pages (7-9 s cold, measured 2026-09-28).
    Add a column to the INCLUDE list rather than read it from the heap; the
    importer vacuums `observations` after each load.
  - **AP load history (schema 8 / collector buffer v7, `ops/migrate_schema8.sh`):**
    `ap_load_hourly` = per AP, UTC hour and advertised channel: poll count,
    max/sum of the client count and, for polls with a BSS Load element,
    max/sum of the AP-reported station count and utilisation. Source data
    (like `observations`), never written by `refresh_rollups()`; every writer
    keeps the row with more polls. Rows come from the v7 buffer (`source
    'buffer'`) or from the per-poll columns of buffers <= v5 (`'v5_obs'`,
    `load_ap_load_samples()`: importer and `ops/backfill_ap_load_from_dump.sh`).
    The channel of a poll is `ap_channel_at()` (the `adv_channel` reading of
    `ap_config_changes`). There is no load history between buffer v6
    (2026-10-03 11:00:20 UTC) and v7. Dashboard v2 plan:
    `docs/dashboard-v2/PLAN.md`.
  - **Dashboard v2 model (schema 9, `ops/migrate_schema9.sh`; design in
    `docs/dashboard-v2/SPEC.md` and `PLAN.md`):** one account (`accounts`,
    tz Europe/Bratislava) with `account_settings`, `networks` (managed /
    friendly) and account-level `aps` (one per BSSID; `ap_sensors` maps each
    sensor's device). `aps.classification` is the MANUAL classification only;
    the effective one is `ap_classification()` / view `account_aps`.
    **`detections` is the event table** (`kind` detection | kismet | config |
    sensor): the batch detectors and `refresh_events()` write rows; `status`
    (open | confirmed | false_positive), `status_at`, `status_note` are the
    operator's labels (input for the false-positive analysis) and are never
    touched by a detector re-run or a refresh. Kismet alerts become episodes
    per header and AP, attributed by `transmitter_mac` only - **no client MAC
    in an event row**. Derived by `refresh_rollups()`: `ap_security_periods`
    (`security_level()` from Kismet 2025-09-R1's crypt bits, "weakest
    offered"), `sensor_periods`, the events. **A collecting poll is `ok AND
    ds_running AND devices_active > 0`** (`ok` alone counted the adapter loss
    as successful). Fixture tests: `security_level_test.sql`,
    `classification_test.sql`, `events_test.sql`, the extended
    `refresh_rollups_test.sql`; read-only check `check_schema9.sql`.
  - **An AP stays an AP (schema 10, `ops/migrate_schema10.sh`):** Kismet
    re-types devices (an AP that also sends as a station is reported as a
    client later; 38 devices on 2026-10-12). Every AP set - `refresh_rollups()`,
    `ap_inventory`, `refresh_ap_baselines()`, `load_ap_load_samples()`, the
    detectors, the importer's observation filter - is **`devices.ever_ap`**,
    set by the trigger `devices_ever_ap()` (typed ap, or an advertised
    configuration = it beaconed) and never cleared; never filter APs on
    `devices.type` again. Kismet episodes are attributed to the account AP
    (`aps`, by the transmitter's BSSID = `detections.mac`) and adopt a row
    without a subject stored for exactly their window; the duplicates of
    before were merged once by `ops/merge_duplicate_events.sql` (keeps
    statuses and notes, `audit_log` 'event.merge'). Read-only check:
    `tests/sql/check_schema10.sql`.
  - **Labels (schema 12) and the hardware boundary (schema 11, 13):** a label has
    one of three outcomes - confirmed, benign, false_positive - each with a
    required reason (`detections.status_reason`; a NULL-safe constraint, RQ2
    counts by it). A change of the capture hardware (the antenna swapped, anything
    that shifts every RSSI) is a `hardware_change` entry of the configuration
    history (`sensor_annotations`, a data boundary entered by hand; the antenna
    itself is not recorded - it is passive and undetectable, its gain feeds
    nothing); `sensor_adapters` is the read-only adapter record (VID:PID + MAC,
    seeded now, reported by the sensor in the live-upload phase). `hardware_period()`
    reads those entries: `refresh_ap_baselines()` learns a baseline inside one
    hardware period (`ap_baselines.hw_from`) and evil_twin (b) compares a reading
    only with a baseline of its period. Never fit or compare an RSSI statistic
    across a hardware change. Fixture test `tests/sql/hardware_test.sql`; read-only
    check `check_schema13.sql`.
  - **The label's cause (schema 17, 2026-10-25):** a benign or
    false-positive label needs `detections.status_cause` from a fixed list of
    11 (`writes.CAUSES` = the constraint = `events.js` CAUSES, tested;
    `unknown` = no known cause, `other` needs a note); confirmed and open have
    none. `evaluation/rq2` reads it (the rules there are suggestions only).
  - **Configuration delivery (schema 18, 2026-10-25; `docs/config-delivery.md`):**
    the desired `sensor_config` rides in the responses to the uploader's own
    requests, only to an uploader reporting `config_state.accepting` (Pi
    `CONFIG_APPLY=1`, **off by default and off now** - enabling it changes
    capture: the developer's); reported attempts become
    `sensor_config_attempts`, `confirmed_at` and a dated `change` boundary in
    `sensor_annotations`; refusals, rollbacks and failures are never
    re-delivered unless retried; `polls.config_version` per poll.
  - **Backups:** `ops/backup_nightly.sh` runs `backup.sh` (KEEP 7) at 01:05 UTC
    from the box user's crontab (`ops/backup_cron.sh`) and writes
    `<dump>.sha256` + `backups/LATEST`; the developer's Mac pulls the newest
    dump hourly (`ops/mac/pull_backup.sh`, a user LaunchAgent) into
    `~/wifi-audit-backups/nightly/` (sha256 checked, newest 14 kept).
  - **Writes (phase 5):** every write endpoint needs `WRITE_TOKEN` (`.env`,
    passed by compose; empty = writes disabled) and runs in
    `deps.write_tx()` (audit_log rows, `accounts.write_version + 1`); the
    bodies are validated in `app/v2/writes.py`, the write SQL is
    `app/v2/sql/writes.sql` (never in a read group: the perf tool would
    EXPLAIN ANALYZE it). Claude never sends an authorized write to the box -
    the token stays with the developer; `v2_smoke.py` sends writes only
    without or with a wrong token (refused before any handler runs).
  - **API v2 (`/api/v2`, `api/app/v2/`, PLAN.md section 7):** the SQL lives in
    `app/v2/sql/<group>.sql` as named statements (`-- name: x`, psycopg
    `%(param)s`, fragments `{{_x}}`), never inline; `tests/perf/ep_v2.py`
    runs exactly those under EXPLAIN as claude_ro, `measure.sh MODE=v2` adds
    HTTP timings and `v2_smoke.py` (status codes, 304, paging, privacy scan).
    Everything but `deps.py` and `routes/` is stdlib and unit-tested on the
    Mac (`api/tests`). Rules learned on the e2-micro: JIT is off per request
    (it cost 1.3 s on a 0.2 s query); per-row SQL functions are slow here
    (`security_meets()` ~1 ms, `account_aps` ~90 ms for 850 APs) - call them
    once per distinct input in a MATERIALIZED CTE, and the AP list reads
    `account_aps` through `app/v2/apcache.py` (keyed by refreshed_at +
    write_version); bucket numbers via `width_bucket()`, no hour-map joins;
    back-to-back queries spend the CPU burst credit, so time them after idle.
    **No MAC but an account AP BSSID in any response**: the detectors'
    evidence names deauth sources (client MACs), every response with
    evidence or detector summaries goes through `privacy.redact()` (Kismet
    device keys hold their MAC byte-reversed). The first dashboard's
    `/api/detections/{id}`, which returned that evidence unredacted, was
    removed with its endpoints (phase 5c).
  - **An AP's band comes from its advertised channel** (`ap_band()`:
    `devices.adv_channel`, else the busiest frequency of `device_freq_hist`),
    stored as `ap_baselines.band`. `observations.freq_khz` was never the
    reception channel of `rssi` (Kismet's device frequency, updated from other
    frames) and is not stored from schema 7.
  - Read-only evil_twin FP audit: `python -m detection fp-audit` (never
    `refresh_ap_baselines()` with a split window for an audit - it rewrites
    `ap_baselines`). Documented sensor gaps for it: `detection/degraded_windows.csv`.

## Operating rules for Claude Code (the server box)

Rules set 2026-10-17 (live-upload rollout mode; they replace those of
2026-09-29). Claude Code writes the code in this repo on the Mac and rolls it
out itself; the developer keeps the items under "Stays the developer's".

- **Allowed - git:** commit and push its own commits (never force-push, never
  rewrite history); `git pull` on the box.
- **Allowed - read-only:** exploration over `ssh google` (`docker compose
  ps/logs/stats/top`, `docker system df`, `free`, `df`, `vmstat`, `iostat`,
  `uptime`, `lscpu`, `dmesg`, `journalctl`, reading files); psql as the
  read-only role `claude_ro` (`ops/create_ro_role.sql`) for every check;
  `tests/perf/measure.sh` incl. its db restarts and page-cache drops;
  temporary scripts in `/tmp/wifi-audit-testing` (delete them when done).
- **Allowed - operations, only through the repo's scripts and compose:**
  `./backup.sh`; migrations and rollbacks via `ops/` (`migrate_schemaN.sh`,
  `rollback_schemaN.sql`); `docker compose build` / `up -d` of the stack's
  services; `caddy reload`; smoke tests; the SQL fixture tests and checks;
  the N8 refresh measurement (`refresh_rollups()`); snapshot imports
  (`seed/import_snapshot.py`); `ops/sensor_token.sh` - the token is written
  straight into the Pi's `/etc/wifi-sensor/uploader.conf` in one pipeline,
  never printed into the session or a log (if it leaks: revoke it, make a new
  one); `INGEST_ENABLED` in `.env` (no other `.env` value).
- **Allowed - disk cleanup (live-upload 9.4 A):** copy the older buffer
  snapshots to the developer's Mac (`~/wifi-audit-snapshots/`), compare
  sha256 on both sides, delete them on the box only if every hash matches;
  `docker image prune` and `docker builder prune -a`.
- **Guardrails:** before every migration a fresh `./backup.sh`, copied to the
  Mac (`~/wifi-audit-backups/`) with matching sha256, before it starts. One
  step at a time, verified (fixture tests, check scripts, smoke test, logs)
  before the next. If something fails: stop, roll back with the prepared
  rollback if needed, report - never improvise fixes on the live database.
- **Never:** delete database data or Docker volumes (`docker volume prune`,
  `docker system prune --volumes`, `down -v`); run `ops/delete_client_rows.sh`;
  edit `.env` secrets; run `gcloud` or touch disks; write to the database
  outside the scripts above.
- **Stays the developer's:** deleting client rows, any decision outside the
  approved design (`docs/live-upload.md`), and the clock-validation runs on
  the Pi (`wifi-sensor/CLAUDE.md`).

## Privacy (same rule as the sensor)

No bystander PII: no client IP addresses, no WPS serial/model/name fields. The
sensor already excludes these; the server must not reintroduce them.

## Conventions

- English only for identifiers/comments/commits. No credentials in git (DB URL,
  secrets via a gitignored `.env`). Commit often, keep changes reviewable.
- Claude Code writes/edits files here and runs the operations listed under
  "Operating rules" above; everything else on the box is the developer's.
