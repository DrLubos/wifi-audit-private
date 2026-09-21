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
  information ("at least one new burst since the previous poll").

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

## Privacy (same rule as the sensor)

No bystander PII: no client IP addresses, no WPS serial/model/name fields. The
sensor already excludes these; the server must not reintroduce them.

## Conventions

- English only for identifiers/comments/commits. No credentials in git (DB URL,
  secrets via a gitignored `.env`). Commit often, keep changes reviewable.
- Claude Code writes/edits files here; the developer runs docker compose / psql / the
  deploy. Do not run destructive DB commands, `docker compose up`, or deploy on the
  developer's behalf.
