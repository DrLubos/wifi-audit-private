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
  developer has already run PostgreSQL + PostGIS + FastAPI + Caddy + React on this
  same box, so this stack is proven here. Everything runs ON the server — nothing is
  built or served from a local machine.
- Stack: **PostgreSQL + FastAPI (Python) backend + a React frontend + Caddy**, all
  on the server. Caddy sits in front as reverse proxy: routes `/api/*` to FastAPI and
  everything else to the React app, and handles TLS. Keep queries modest; if the
  React build is memory-tight, swap covers it.

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

## Database

- **PostgreSQL.** Design the schema as plain SQL that is **TimescaleDB-ready**: the
  observations time-series table is an ordinary table keyed on (ts, sensor_id,
  device_key) so it converts cleanly to a hypertable + continuous aggregates when
  live ingest and volume arrive. Do NOT enable TimescaleDB yet — plain Postgres has
  zero overhead for the seeded demo.
- **Multi-sensor from the start:** every time-series / device row carries a
  sensor_id FK, even though there is one sensor today (the thesis framing is an
  overlay of fixed sensors).
- Mirror the collector's tables (polls, devices, device_config_history,
  observations, device_freq_hist, associations, probes, alerts) and ADD:
  sensors, ap_baselines (per-sensor per-AP: rssi median/sd, known BSSID<->SSID,
  trusted flag), detections (type, device, severity, evidence, acked).
- Deliver schema as a single idempotent `schema.sql` the developer runs with psql.

## Privacy (same rule as the sensor)

No bystander PII: no client IP addresses, no WPS serial/model/name fields. The
sensor already excludes these; the server must not reintroduce them.

## Conventions

- English only for identifiers/comments/commits. No credentials in git (DB URL,
  secrets via env / a gitignored file). Commit often, keep changes reviewable.
- Claude Code writes/edits files here; the developer runs psql / the server. Do not
  run destructive DB commands or deploy on the developer's behalf.
