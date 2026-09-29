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

- **Milestone 3, step 1 (now): a demo dashboard on REAL data, seeded from a
  one-time export of the Pi buffer.** No live ingest yet. No full firehose.
- Later: a live ingest API + the collector's upload step, sending a SUBSET (current
  AP inventory, per-AP RSSI over time, config-change events, alerts) — never the raw
  per-poll firehose (associations/probes are millions of rows).
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
  `(ts, sensor_id, device_key)`) but Timescale is NOT enabled — plain Postgres has
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
    (schema 5) is the one reading of the configuration timeline - no-record
    states skipped, NULL = unknown per field, `crypt_bits` 0 unknown unless
    crypt is `Open` (so a WPA2 -> Open downgrade still shows), a hidden AP's
    cloaked beacon (`ssid` '') vs its named record is one state.
    `ap_channel_changes` is its `adv_channel` rows (detectors use it). Keep every
    history column (crypt/MFP/country/beacon-rate changes are security signals).
    Fixture test: `tests/sql/ap_config_changes_test.sql` (must stay green; it
    proves the rules cannot hide a downgrade).
  - **AP queries must stay index-only scans** on
    `observations_device_ts_rssi_floor` (INCLUDE rssi, rssi_floor): a column
    outside the index makes every AP page read ~30k heap pages (7-9 s cold,
    measured 2026-09-28). Add a column to the INCLUDE list rather than read it
    from the heap; the importer vacuums `observations` after each load.
  - **`observations.freq_khz` is not the reception channel of `rssi`** (Kismet's
    device frequency, updated from other frames) and not reliably the AP's
    channel; use `devices.adv_channel`.
  - Read-only evil_twin FP audit: `python -m detection fp-audit` (never
    `refresh_ap_baselines()` with a split window for an audit - it rewrites
    `ap_baselines`). Documented sensor gaps for it: `detection/degraded_windows.csv`.

## Operating rules for Claude Code (the server box)

Same split as on the Pi: Claude Code writes files in this repo on the Mac; the
developer runs anything that changes the box. Rules set 2026-09-29.

- **Allowed:** read-only exploration over `ssh google`: `docker compose
  ps/logs/stats/top`, `docker system df`, `free`, `df`, `vmstat`, `iostat`,
  `uptime`, `lscpu`, `dmesg`, `journalctl`, reading files.
- **Allowed:** psql **only as the role `claude_ro`**
  (`ops/create_ro_role.sql`: login without a password - local socket inside
  the db container only -, `pg_read_all_data` + `pg_monitor`, read-only
  transactions, 120 s statement timeout, may set `track_io_timing`):
  `docker compose exec -T db psql -U claude_ro -d "$POSTGRES_DB"`.
- **Allowed:** running `tests/perf/measure.sh`, including its db restarts and
  page-cache drops (nobody else uses the box).
- **Allowed:** creating and running temporary test scripts in
  `/tmp/wifi-audit-testing` on the server (create it there; delete its
  contents when done). Nothing outside that folder. Scripts that touch the
  database still use only `claude_ro`; anything that would write, restart or
  deploy stays under "Not allowed" - a script does not change that.
- **Not allowed:** database writes, `docker compose up/down/build/rm`, editing
  or deleting files on the server, package or service changes. The developer
  runs those (schema, imports, backups, deploys) when Claude says what to run.

## Privacy (same rule as the sensor)

No bystander PII: no client IP addresses, no WPS serial/model/name fields. The
sensor already excludes these; the server must not reintroduce them.

## Conventions

- English only for identifiers/comments/commits. No credentials in git (DB URL,
  secrets via a gitignored `.env`). Commit often, keep changes reviewable.
- Claude Code writes/edits files here; the developer runs docker compose / psql writes /
  the deploy (see "Operating rules" above for what Claude may run on the box).
