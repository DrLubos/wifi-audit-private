# Live upload from the sensor – design

Status: **live since 2026-10-17 20:46:38 UTC** (steps 1-5 deployed, step 6
docs; the rollout record is section 17). Decisions D1-D14, N1-N11 are in
section 13.
Replaces `dashboard-v2/PLAN.md` section 11.1 for the upload; config delivery
stays deferred (D9).

Inputs: the three `CLAUDE.md` files, `dashboard-v2/PLAN.md` (4.3, 9, 11),
`wifi-sensor/collector/` (buffer v7), `seed/import_snapshot.py`,
`schema.sql` (schema 13), and the live state read-only:

- box (2026-10-17): 955 MB RAM / ~370 MB available; disk **2.85 GB free,
  86 % used** (3.7 GB on 10-13 – four buffer snapshots of ~430 MB each were
  added, section 9.3); last poll on the server **2026-10-14 05:21:28 UTC**
  (snapshot import of 10-14); database 1.24 GB;
- Pi (2026-10-17): Python 3.13.5, SQLite 3.46.1, chrony with `makestep 1 3`
  and `rtcsync`, **no fake-hwclock and no RTC** – a boot starts on a fixed
  stale date (the current boot's journal begins at 2026-09-15 13:52, weeks
  behind); `chronyc -c tracking` works unprivileged; capture adapter
  `0bda:c811` on USB port `1-1.3`, interfaces `wlan1` + `wlan1mon`;
- volumes: ~2 880 polls/day (30 s), ~61 AP observation rows a poll (~175 k
  a day, 224 B/row on the server incl. indexes ≈ 39 MB/day),
  `device_config_history` ~540 rows/day.

Contents: 1 Summary · 2 Where the starting position changed · 3 Architecture
· 4 Sensor: collector changes and the uploader · 5 Wire protocol · 6 Server ·
7 Schema 14 · 8 Gaps, status, clock · 9 Load, limits, disk · 10 Failure
modes · 11 What the thesis can measure · 12 Testing · 13 Decisions · 14 Build
order · 15 Deploy and rollback · 16 Future improvements

---

## 1. Summary

A separate stdlib-only daemon on the Pi (`wifi-sensor-uploader.service`)
watches the collector's buffer **read-only** (a cheap check every 2 s) and
sends **each poll as soon as it is committed** – steady state one small
batch per poll, every 30 s – gzip-JSON over HTTPS to `/api/ingest/v1/batch`,
with a per-sensor token whose SHA-256 is the only thing the server stores.
After an outage the backlog is replayed oldest first in batches of 120 polls
with 2 s pacing. Every batch carries the heartbeat; a standalone heartbeat
goes out when no batch was sent for 60 s.

The server COPYs a batch into the same session staging tables the snapshot
importer uses and calls **one SQL function, `ingest_merge()`**, which the
importer calls too. Every merge is on the existing natural keys, so a
replayed batch inserts nothing twice. Sensor status and last contact are
written by every request and read directly (immediate); the dashboard's
rollup tables are refreshed by a `refresher` container on a configurable
interval that limits its own duty cycle.

Timestamps are made trustworthy without an NTP gate: the collector stores a
monotonic timestamp and the boot id with every poll, records one anchor per
boot when the clock becomes synchronised, and the uploader places every
poll of an unsynchronised stretch on the true time line from the anchor
before sending it (8.3). A boot that never gets an anchor is reported as
"time unverified" and not uploaded.

---

## 2. Where the starting position changed

| Starting position | Outcome |
|---|---|
| Disk spool with retry | **The buffer is the spool** (D1). The uploader keeps a cursor of what the server acknowledged and rebuilds a batch from the buffer on every attempt |
| Batch id or natural key | **Both**: natural keys dedupe the data; the batch id (`buffer_id`, `batch_seq`) only feeds the upload log, which counts replays |
| Payload without client MACs | Schema 7 is not MAC-free (associations, probes, client_bssids, alert MACs). Uploaded: polls, AP devices, configuration history, AP observations, AP load, AP frequency histogram, alerts unchanged (D3); pseudonymising client MACs in alerts is a PLAN follow-up (`PLAN.md` 11.5) |
| Same merge for seed and ingest | `ingest_stage_create()` + `ingest_merge()` in `schema.sql` (6.3); the snapshot import becomes a recovery tool (D4) |
| Upload every minute | **Upload after every poll** (amended 2026-10-17): detection within 2 s, one batch per poll |
| Outage = data gap | Only sensor/capture outages are gaps; an upload outage is late data, shown as a contact gap (8.1) |
| Clock gate (first proposal) | **No gate** (D6 changed): capture continues without NTP, timestamps corrected afterwards from monotonic time, target ±10 s (8.3) |
| Rate limit in Caddy | Caddy caps the body size only; rate limits in the api (9.2) |

**Collector vs separate uploader** (asked 2026-10-17): the separate process
stays. Doing the upload in a collector thread would save the 2-s detection
delay (≤ 2 s of the latency budget) and one process (~15 MB RSS), but: a
thread shares the interpreter, the SQLite connection discipline and the
process lifetime – a hung TLS handshake holding the GIL in C code, an
exception in a shared helper or a memory leak in the upload path would stop
or restart capture; systemd could no longer restart, limit (`MemoryMax`) or
disable the upload on its own; and the rollback "disable the uploader,
capture untouched" would no longer exist. The latency gain does not pay for
that.

---

## 3. Architecture

```
Pi                                                     server box (e2-micro)
───────────────────────────────────────────────        ─────────────────────────────────────────────
collector (v8) ──writes──> buffer.db (WAL)             Caddy :443  /api/ingest/*  body ≤ 2 MB
  per poll: ts, mono, boot_id, clock_synced              │
  per boot: clock anchor at the first synced poll        ▼
                         │ read-only (mode=ro)         api (FastAPI, 1 worker)
uploader ────────────────┘                               POST /api/ingest/v1/batch      (+ heartbeat)
  every 2 s: PRAGMA data_version / max(polls.id)         POST /api/ingest/v1/heartbeat  (idle, 60 s)
  new poll -> place on the true time line -> POST          token -> sensor_id; validate; COPY stage_*
  2xx: advance cursor     error: backoff, same cursor      SELECT ingest_merge(sid, 'ingest')
  backlog: 120 polls/batch, 2 s pacing                     sensor_status (immediate), sensor_uploads
  state: /var/lib/wifi-sensor-uploader/state.json        refresher (api image): refresh_rollups on a
                                                           self-limiting interval; contact periods;
import_snapshot.py (recovery tool) ──> ingest_merge()      nightly baselines
```

---

## 4. Sensor

### 4.1 Collector changes (buffer v8, step 3)

All `ALTER TABLE ADD COLUMN` (metadata only) plus new tables; rollback =
`meta.schema_version` 7 (the v7 code ignores the extra columns and tables).

| Change | Why |
|---|---|
| `polls.mono` (REAL, `time.monotonic()` = CLOCK_MONOTONIC, read next to the wall clock), `polls.boot_id` (`/proc/sys/kernel/random/boot_id`), `polls.clock_synced` (chrony `Leap status` Normal **and** \|System time offset\| < 0.5 s), `polls.chrony_offset` | correction (8.3) |
| `clock_anchors(boot_id PK, mono, wall, poll_id, source 'ntp')`: written once per boot at the first synced poll | the true epoch of the boot = `wall - mono` |
| `observations.poll_id`, `device_config_history.poll_id`, `alerts.poll_id`; the poll row is inserted first in the poll's transaction | rows are placed by their poll, never by matching a (possibly stale) ts |
| `devices.boot_id`, `devices.boot_last`: "new observation" = the device was not yet seen in this boot, or Kismet's `last_time` grew within the boot | today `is_new_obs` compares Kismet's `last_time` with `last_seen` of earlier boots: under a stale clock no known device ever gets an observation |
| The resume point `last_kismet_ts` is reset when the boot changes | under a stale clock `devices_since(<real time>)` returns nothing; today a stale stretch silently records empty polls |
| `ap_load_pending(poll_id, key, channel, n_clients, qbss, qbss_stations, util_pct)`: per-poll AP load of unsynchronised polls; folded into `ap_load_hourly` with the true hour when the boot's anchor is recorded, then deleted (N2) | an hourly aggregate cannot be shifted afterwards; the stale clock's hours collide with real hours of the past |
| Prune by append order: rows of unsynced polls are pruned by their corrected time (anchored) or, for a boot without anchor, when the first synced poll after them is older than the retention | today's prune (`ts < now - 14 d` on the synced clock) would delete a stale stretch – stamped weeks back – within the hour, before the upload |
| `meta.buffer_id` (UUID, written once) | a new buffer restarts row ids (4.3) |

No other behaviour changes: with a synchronised clock (every boot so far) the
written data is the same as v7 – `is_new_obs` is equivalent when Kismet's
times are real. A dated `findings.md` section 7 row records the v8 deploy.

### 4.2 Uploader process and files

- `wifi-sensor/uploader/`: `uploader.py` (main loop), `batch.py` (pure:
  cursor + buffer -> batch; selection and time placement), `clock.py` (pure:
  anchors, offsets, placement rules of 8.3), `heartbeat.py` (sysfs adapter
  identity, chrony state, disk, backlog), `client.py` (urllib, gzip, TLS,
  status mapping), `state.py` (atomic cursor file), `tests/` incl.
  `fake_server.py`. **Standard library only.**
- `systemd/wifi-sensor-uploader.service`: sensor user, `Nice=10`,
  `MemoryMax=80M`, `StateDirectory=wifi-sensor-uploader`,
  `ReadWritePaths=/var/lib/wifi-sensor` only because a WAL reader writes the
  `-shm` file; the buffer is opened `mode=ro`.
- `install_uploader.sh` (the developer runs it): copies to
  `/opt/wifi-sensor/uploader`, creates `/etc/wifi-sensor/uploader.conf`
  from the example if missing (`0640 root:<sensor user>`), installs the
  unit, enables it only when `INGEST_URL` and `INGEST_TOKEN` are set. Never
  deletes anything.
- Config: `INGEST_URL` (https only), `INGEST_TOKEN`, `DB_PATH`,
  `CAPTURE_IFACE`, `WATCH_S` 2, `HEARTBEAT_S` 60, `CATCHUP_POLLS` 120,
  `CATCHUP_PAUSE_S` 2, `MAX_BATCH_BYTES` 1 MB, `BACKOFF_MAX_S` 600,
  `RETENTION_DAYS` (read from `collector.conf` if absent).
- **TLS** (amendment 2): `urllib` with `ssl.create_default_context()` –
  certificate and host name always verified, no option to turn it off; a
  test against a local server with a self-signed certificate asserts the
  refusal.

### 4.3 Cursor, detection, batches

```json
{"buffer_id": "…", "polls_id": 123456, "obs_id": 98765432, "dch_id": 4567,
 "alerts_id": 890, "load_ts": 1760388000, "freq_ts": 1760388000, "batch_seq": 1042,
 "server_anchors": {"<boot_id>": {"mono": 812.4, "wall": 1760388061.2}}}
```

- **Detection**: every `WATCH_S` (2 s) `PRAGMA data_version` (changes when
  another connection committed; no I/O beyond the shared memory) and, if it
  changed, `SELECT max(id) FROM polls`. A new poll is sent at once.
- **Spine = polls** in id (= append = true) order. A batch is the next
  uploadable polls: steady state the one new poll, catch-up up to
  `CATCHUP_POLLS`. A poll is uploadable when it is clock-synced, or its boot
  has an anchor (NTP from the buffer, or a server anchor, 8.3). The spine
  stops at the first poll of the **current** boot that is neither (held
  back until the anchor). A poll of an **earlier** boot without anchor will
  never get one: the cursor passes it, it is counted as time-unverified and
  reported (heartbeat, `unverified` ranges in the batch).
- Rows of the batch's polls: `observations` / `device_config_history` /
  `alerts` by `poll_id` (rows of v7 and older by id range and raw ts, as the
  importer does); AP observations only (the importer's `_AP_EVIDENCE` rule;
  a test keeps the two equal); `ap_load_hourly` rows with `last_ts` in the
  batch (the current hour is re-sent with each poll – fuller-wins);
  AP `device_freq_hist` rows updated in the batch; the `devices` rows of
  every AP referenced, with the time fields of 8.3.
- **Lost rows**: if the cursor's next poll id is below the oldest poll in
  the buffer, the range was pruned before upload; the batch reports it
  (`lost` range) and the server records it (8.1).
- Size: halve the polls when the batch exceeds `MAX_BATCH_BYTES` or the
  server answers 413.
- **Initial cursor** (D5): without a state file the uploader asks the server
  (`resume` = the server's newest poll seq and ts for the sensor) and starts
  after it. Today that is 2026-10-14 05:21:28 – the first upload fills the
  gap since.
- After a 2xx the cursor file is replaced atomically (temp, fsync, rename);
  a crash in between re-sends the batch (counted as a replay).

### 4.4 Loop, retry, backoff

```
every WATCH_S (2 s):
  new uploadable polls?  -> POST /batch (heartbeat inside)
     2xx       -> advance cursor; backlog left -> sleep CATCHUP_PAUSE_S, next batch
     413       -> halve, retry at once
     401 / 403 -> backoff to BACKOFF_MAX_S, log hourly (server: stall event, 8.5)
     409       -> another request of this sensor runs: next tick
     422       -> backoff as 5xx; never skips data (server: stall event when persistent)
     429 / 5xx / network / timeout -> 5 s · 2^n, max 600 s, ±20 % jitter
  no batch sent for HEARTBEAT_S -> POST /heartbeat (also while backing off)
```

Timeouts: connect 10 s, total 30 s. Order is strict: the cursor never passes
a batch the server has not committed.

### 4.5 Heartbeat contents (~1 KB, inside every batch and standalone)

`sensor_ts`, `mono`, `boot_id`, `uptime_s`; chrony leap status, system time
offset, stratum; collector: last poll id/ts/ok, buffer bytes (+WAL), buffer
version, `buffer_id`, `RETENTION_DAYS`, oldest poll in the buffer; backlog:
polls not yet acknowledged, oldest of them, polls held back for an anchor,
time-unverified polls (per boot: count, raw span); disk free on `/`;
adapter: `usb_id`, `mac`, `driver`, chipset (from the driver name),
`interface`, or `null`; uploader version; last request result and RTT.

---

## 5. Wire protocol

All under `/api/ingest/v1/`, `Authorization: Bearer <token>`,
`Content-Encoding: gzip`, `Content-Type: application/json`. The payload never
names a sensor: the sensor is the token's.

Token format (N1): `wsi1.<token id>.<64 hex>` – the id is public and lets
the server attribute a **wrong** secret to its sensor (stall event,
amendment 1); the server stores only `sha256(token)`.

**`POST /heartbeat`** `{"v": 1, "heartbeat": {...}}` -> `200 {"server_ts",
"resume": {"seq", "ts"} | null, "last_batch_seq"}`. `server_ts` with the
request's RTT gives the server anchor (8.3).

**`POST /batch`**:

```json
{"v": 1, "batch_seq": 1043, "buffer_id": "…", "buffer_version": "8",
 "sent_at": 1760388061.2, "attempt": 1, "heartbeat": {...},
 "boots": [{"boot_id": "…", "source": "ntp", "epoch_true": 1760380000.31,
            "epoch_stale": 1757944356.0, "step_mono": 812.4, "polls_corrected": 27}],
 "gaps": [{"seq_from": 1200, "seq_to": 1260, "reason": "unverified" | "lost"}],
 "tables": {
   "devices":      {"cols": ["device_key", "mac", "type", …], "rows": [[…], …]},
   "polls":        {"cols": ["seq", "ts", "ts_offset_s", "kismet_ts", …], "rows": […]},
   "observations": {"cols": […], "rows": […]}, …}}
```

Rows are arrays in the order of `cols`; `cols` must equal the staging
columns of that table (`ingest_stage_create()`), versioned by `v`. Times are
unix seconds on the **corrected** time line; `polls.ts_offset_s` is the
correction applied (NULL = the poll was clock-synced).

Responses: `200 {"batch_seq", "replay", "staged", "inserted", "conflicts",
"ms", "server_ts"}`; `400` malformed; `401` unknown token / wrong secret;
`403` revoked; `409` a batch of this sensor is running; `413` too large;
`422` invalid content (first error named); `429` rate limit (`Retry-After`);
`503` ingest disabled or the database unreachable.

---

## 6. Server

### 6.1 Tokens (D2)

- `ops/sensor_token.sh create|revoke|list <sensor>` on the box, as the
  database owner: generates `wsi1.<id>.<32 random bytes hex>`, stores
  `sha256`, prints the token **once**. No Settings UI in this phase.
- Several active tokens per sensor (rotation); `revoked_at`;
  `last_used_at` updated at most once a minute.
- SHA-256, not bcrypt/argon: 256 random bits need no slow hash, and the box
  has 0.25 vCPU sustained. Lookup by id, `hmac.compare_digest` of the hash.
- On the Pi only in `/etc/wifi-sensor/uploader.conf` (`0640`), sent only to
  `INGEST_URL`, never logged.
- **Deleting a sensor revokes its tokens** (amendment 4): the Settings
  delete removes the sensor's `sensor_tokens` rows with its configuration
  (they cannot outlive the sensor row); the audit row lists the token ids.
  The guard is unchanged: a sensor that ever uploaded has data and is never
  deleted.

### 6.2 The ingest request

`app/ingest/` (stdlib, unit-tested: `validate.py`, `ratelimit.py`,
`tokens.py`) and `routes/ingest.py` (psycopg):

1. Rate limit and size checks (9.2); token -> sensor.
2. Decompress with a hard limit (16 MB), parse, validate: column lists,
   types, MAC format, every time within `[2026-09-01, server_now + 300 s]`
   (a future time rejects the batch), row caps.
3. One transaction: `pg_try_advisory_xact_lock(sensor)` (else 409);
   `SELECT ingest_stage_create()`; COPY each table into its stage table;
   `SELECT ingest_merge(sid, 'ingest')`; `sensor_uploads` row
   (`ON CONFLICT (sensor_id, buffer_id, batch_seq)` -> `replays + 1`);
   `sensor_clock_boots`, `sensor_seq_gaps` (+ `lost` annotations);
   `sensor_status` upsert (contact, heartbeat, stall state, 8.5); a
   `sensor_heartbeats` row if the last is ≥ 55 s old; the adapter check
   (8.4); commit.
4. Response with the counts; typical steady-state cost a few ms of SQL.

### 6.3 One merge for both paths

- `ingest_stage_create()`: the session temp tables `stage_devices`,
  `stage_polls`, `stage_device_config_history`, `stage_observations`,
  `stage_ap_load_hourly`, `stage_load_samples`, `stage_device_freq_hist`,
  `stage_associations`, `stage_client_bssids`, `stage_probes`,
  `stage_alerts` – `CREATE TEMP TABLE IF NOT EXISTS … ON COMMIT DELETE ROWS`:
  created once per database connection and reused (no catalog churn from
  2 880 create/drop cycles a day).
- `ingest_merge(p_sensor_id, p_via) RETURNS jsonb`: the importer's merge
  statements, moved into `schema.sql`, in foreign-key order, for every stage
  table that has rows: devices newest-wins (incl. `cur_*`; a NULL time
  field keeps the stored value), polls / observations / history / alerts
  `DO NOTHING` (polls get `received_at = now()`), `ap_load_hourly`
  fuller-wins, frequency newer-wins, the client tables LEAST/GREATEST,
  `load_ap_load_samples()`. Returns staged, inserted and conflicting rows per
  table and the earliest inserted ts; moves `ingest_state.dirty_from` back
  to it.
- `import_snapshot.py`: same CLI; COPYs with explicit column lists and one
  `ingest_merge(:sid, 'snapshot')`; stages `polls.id` as `seq`; the client
  tables only with `--with-client-tables` (D4); keeps its own refresh and
  vacuum (a manual bulk tool). Existing client rows on the server stay;
  `ops/delete_client_rows.sh` (dry run by default, `--execute` after a
  backup) is there for the developer's later decision.
- Tests: `tests/sql/ingest_merge_test.sql` (every merge rule, a re-merge
  inserts 0, non-AP observations dropped, an older device row does not
  overwrite a newer one, a NULL time field keeps the stored one,
  fuller-wins, dirty_from) and a Python test that the importer's stage
  columns, the ingest validator's and `ingest_stage_create()` agree.

### 6.4 The refresher (D7)

A compose service `refresher` (the api image, `python -m app.refresher`,
~40 MB): per sensor with `ingest_state.dirty_from` set,
`refresh_rollups(sid, dirty_from)`, then clear it unless an ingest moved it
meanwhile; `refresh_contact_periods(sid)` (8.1); nightly 03:30 Europe/
Bratislava `refresh_ap_baselines(sid, 200, 2)`.

- **Interval**: `REFRESH_INTERVAL_S` (default 60) with a duty-cycle cap:
  after a run of d seconds the next starts no earlier than
  `max(interval, 4 · d)` (≤ 20 % of one core). Every run logs its duration;
  the first days of logs give the shortest safe interval.
- **What is known now**: `refresh_rollups()` rebuilds steps 4–8 for the
  whole sensor on every call (the `ap_config_changes` materialized view, all
  `ap_summary` rows, security and sensor periods, events) – only steps 2–3
  are incremental. A full run took 6 min 52 s on 2026-10-12 (schema-10
  migration log). The incremental cost is not measured (it writes; Claude
  cannot run it): **measured at the first deploy of step 2** (15.1). If it
  is > 15 s, the 1-min target is not reachable without making steps 4–8
  conditional (only when history or polls arrived – the merge knows) –
  a follow-up, not in this phase.
- Status and last contact never wait for it: the Sensors endpoint reads
  `sensor_status` directly (its ETag includes `sensor_status.updated_at`);
  rollup-based charts lag by at most one refresh.
- **Measuring after the step-2 deploy (N8)**: the refresher logs each run's
  duration; on the box over a day: `vmstat 60` (the `st` steal column is
  where the e2-micro's throttling shows - a shared-core VM has no credit
  balance to read; it is allowed 0.25 vCPU sustained with short bursts to
  2 vCPU), `docker stats --no-stream` every few minutes (refresher and db
  CPU), Cloud Monitoring "CPU utilization" of the instance, and the
  dashboard's `measure.sh MODE=v2` timings with and without a refresh
  running.
- **If an incremental refresh takes minutes**: make steps 4–8 incremental
  instead of tuning the interval - `ingest_merge()` already knows what
  arrived; it would record per sensor which kinds changed
  (`ingest_state.dirty_config` when history or device configuration rows
  changed, `dirty_alerts`, the set of touched APs). Then step 4 (the
  materialized `ap_config_changes`) and the security periods / config
  events run only when the configuration changed, and only for the touched
  APs (the view becomes a table maintained per AP); step 5 (`ap_summary`)
  updates the touched APs' rows instead of rebuilding all ~900; step 7
  (`sensor_periods`) recomputes from the last closed period; step 8 the
  Kismet episodes only when alerts arrived. Each stays "incremental = full"
  under `refresh_rollups_test.sql`. Designed after the measurement, not
  before.
- `observations` autovacuum: `autovacuum_vacuum_insert_threshold = 20000`,
  `autovacuum_vacuum_insert_scale_factor = 0` (a vacuum about every 3 h of
  inserts keeps the index-only AP scans); `devices` and `ap_load_hourly`
  get `autovacuum_vacuum_scale_factor = 0.05` (each poll now updates ~60–70
  of their rows).

### 6.5 Sensor events for stalls (amendment 1)

Written by the ingest routes into `detections` (`kind 'sensor'`, severity
high, no subject); "open" = `ts_end IS NULL`, closed automatically by setting
`ts_end` when uploads resume. One open event per stall type and sensor;
operator labels (`status`) are never touched. `refresh_events()` does not
know these types and leaves them alone.

| Type | Opens | Closes |
|---|---|---|
| `upload_rejected` | the same `batch_seq` refused with 422 ≥ 3 times over ≥ 10 min | a batch is accepted |
| `upload_unauthorized` | a revoked token (403), or a wrong secret for a known token id (401), repeatedly over ≥ 10 min | an authenticated request |
| `upload_retention_risk` | heartbeat: the oldest unacknowledged poll is older than `RETENTION_DAYS - 2` days (D10) | it is younger again |

Network or server outages stay non-events (D12).

---

## 7. Schema 14

```sql
CREATE TABLE sensor_tokens (
  id           serial PRIMARY KEY,               -- public part of the token (wsi1.<id>.<secret>)
  sensor_id    integer NOT NULL REFERENCES sensors(id),
  token_hash   bytea NOT NULL UNIQUE CHECK (length(token_hash) = 32),
  label        text,
  created_at   timestamptz NOT NULL DEFAULT now(),
  last_used_at timestamptz,
  revoked_at   timestamptz);

CREATE TABLE sensor_status (                      -- one row per sensor, every authenticated request
  sensor_id        integer PRIMARY KEY REFERENCES sensors(id),
  last_contact_at  timestamptz NOT NULL,
  last_batch_at    timestamptz, last_batch_seq bigint, buffer_id uuid,
  heartbeat        jsonb NOT NULL DEFAULT '{}',   -- the latest heartbeat (validated)
  reject_seq       bigint, reject_since timestamptz, reject_count integer NOT NULL DEFAULT 0,
  reject_reason    text,
  auth_fail_since  timestamptz, auth_fail_count integer NOT NULL DEFAULT 0,
  updated_at       timestamptz NOT NULL DEFAULT now());

CREATE TABLE sensor_heartbeats (                  -- history, at most one a minute (~0.3 MB/day)
  sensor_id integer NOT NULL REFERENCES sensors(id), received_at timestamptz NOT NULL,
  sensor_ts timestamptz NOT NULL, rtt_ms integer, clock_offset_s real,
  chrony_synced boolean, chrony_offset_s real, boot_id uuid, uptime_s bigint,
  last_poll_ts timestamptz, backlog_polls integer, backlog_oldest_ts timestamptz,
  held_polls integer, unverified_polls integer, buffer_oldest_ts timestamptz,
  buffer_bytes bigint, disk_free_bytes bigint, uploader_version text,
  PRIMARY KEY (sensor_id, received_at));

CREATE TABLE ingest_state (sensor_id integer PRIMARY KEY REFERENCES sensors(id),
                           dirty_from timestamptz, dirty_since timestamptz, dirty_at timestamptz);

CREATE TABLE sensor_clock_boots (                 -- one row per corrected boot (thesis: offsets)
  sensor_id integer NOT NULL REFERENCES sensors(id), boot_id uuid NOT NULL,
  source text NOT NULL CHECK (source IN ('ntp', 'server')),
  epoch_true double precision NOT NULL, epoch_stale double precision, step_mono double precision,
  polls_corrected integer NOT NULL DEFAULT 0, offset_min_s real, offset_max_s real,
  recorded_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (sensor_id, boot_id));

CREATE TABLE sensor_seq_gaps (                    -- polls that never arrived, and why
  sensor_id integer NOT NULL REFERENCES sensors(id), buffer_id uuid NOT NULL,
  seq_from bigint NOT NULL, seq_to bigint NOT NULL,
  reason text NOT NULL CHECK (reason IN ('lost', 'unverified')),
  polls integer NOT NULL, raw_from timestamptz, raw_to timestamptz,
  recorded_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (sensor_id, buffer_id, seq_from));

ALTER TABLE polls ADD COLUMN seq bigint;                -- buffer polls.id (snapshots too)
ALTER TABLE polls ADD COLUMN received_at timestamptz;   -- when the server stored the poll
ALTER TABLE polls ADD COLUMN ts_offset_s real;          -- clock correction applied (NULL = synced)
ALTER TABLE sensor_uploads ADD COLUMN buffer_id uuid, ADD COLUMN batch_seq bigint,
  ADD COLUMN sent_at timestamptz, ADD COLUMN attempt integer,
  ADD COLUMN replays integer NOT NULL DEFAULT 0, ADD COLUMN bytes integer,
  ADD COLUMN duration_ms integer, ADD CONSTRAINT sensor_uploads_batch UNIQUE (sensor_id, buffer_id, batch_seq);
-- sensor_annotations.kind += 'lost', 'unreachable'
-- functions: ingest_stage_create(), ingest_merge(), refresh_contact_periods()
-- autovacuum settings (6.4)
```

`ADD COLUMN` without a default is metadata only. Migration and rollback
follow `ops/migrate_schema13.*`; `check_schema14.sql` read-only afterwards.

---

## 8. Gaps, status and the clock

### 8.1 Four kinds of "missing"

| What happened | On the server after the upload | Shown as |
|---|---|---|
| Capture down (adapter lost, Kismet hung) | polls with `ok`/`ds_running`/`devices_active` = 0 | `sensor_periods` outage (unchanged) |
| Sensor down (power, collector dead) | a poll-ts gap > 90 s | `sensor_periods` outage (unchanged) |
| **Upload down** (network, server, token) | nothing missing once replayed | contact gaps from `sensor_heartbeats` > 3 min -> `sensor_annotations` kind `unreachable` (no data boundary, no event, no coverage effect) |
| **Lost** (pruned before upload, buffer lost) | a `polls.seq` gap reported by the uploader or found by the server | `sensor_seq_gaps` reason `lost` + a `lost` annotation with the exact number of polls |
| **Time unverified** (a boot without anchor) | not uploaded | `sensor_seq_gaps` reason `unverified`; a count on the Sensors page; the time span is unknown, so no annotation on the time line |

While an upload outage lasts, the charts end at the newest received poll
(cells after it are `no_data`, never 0, as today); the Sensors page labels
the edge ("data delayed – N polls waiting" / "no contact since …").

### 8.2 Sensor status (replaces SPEC Q7's "data until")

From `sensor_status` (immediate) and `sensor_summary.last_capture_at`,
against `account_settings.sensor_offline_min` (15):
**online** (contact and capture fresh, backlog ≤ 10 polls) · **catching up**
(contact fresh, backlog > 10: count and ETA from the recent rate) · **not
capturing** (contact fresh, the newest poll not collecting) · **clock
unverified** (polls held back for an anchor) · **unreachable** (no contact
for > `sensor_offline_min`: "last seen 3 h ago") · **no data**. "Last seen"
= last contact. The card adds last poll, upload latency, backlog, clock
offset, time-unverified polls and the adapter.

### 8.3 Clock correction (D6 as changed 2026-10-17)

**Facts.** No RTC, no fake-hwclock: every boot starts on a fixed stale date
(weeks behind, growing). chrony steps the clock (`makestep 1 3`) at its
first update once a source is reachable. The data's timestamps are the Pi's
wall clock; the server never re-stamps, it only rejects (future times) or
flags.

**Recording (collector).** Per poll: `ts` (wall, as today), `mono`
(CLOCK_MONOTONIC, read next to it; it carries chrony's frequency
correction from the drift file), `boot_id`, `clock_synced`. Per boot: one
anchor `(boot_id, mono_a, wall_a)` at the first synced poll. The true epoch
of the boot is `E = wall_a - mono_a`.

**Correction (uploader, at batch build; raw rows never rewritten).**

- A synced poll: `ts` as recorded, `ts_offset_s` NULL.
- An unsynced poll of an anchored boot: `true_ts = round(E + mono)`,
  `ts_offset_s = true_ts - ts`. No need to know when chrony stepped: the
  anchor and the monotonic clock span it.
- An unsynced poll of the current boot without anchor: held back. If HTTPS
  works but NTP does not, the uploader creates a **server anchor** after
  2 min of contact: `wall_a = server_ts + RTT/2` at `mono_a` = the request's
  midpoint (±RTT/2, ~60 ms from the dormitory to us-central1), stored in its
  state file and sent as `boots[].source = 'server'`. An NTP anchor recorded
  later for the same boot does not change polls already sent.
  **Limit (found 2026-10-17):** the request is verified TLS (amendment 2),
  and certificate validity is checked against the Pi's own clock. With
  today's stale boot date (2026-09-15, weeks behind) every certificate
  issued since is "not yet valid", so the server anchor cannot work - nor
  Tailscale's control plane. It works when the boot clock is at most a few
  days behind, which is what N11 gives.
- A boot that ended without anchor: never uploaded (8.1, time unverified).

**Every wall-clock value inside a poll's rows** (uploaded tables only, D3).
`delta = E - E_stale` where `E_stale = ts - mono` of the boot's first
unsynced poll (the stale epoch). A Kismet time `v` of boot b is **stale** iff
`v < E_stale + step_mono`, where `step_mono` = mono of the boot's first
synced poll (+∞ while only a server anchor exists – then no step happened):
pre-step values lie below that line, post-step values above it whenever the
clock error exceeds the poll interval; for a smaller error the shift is
skipped (|delta| < 10 s) or the misplacement is bounded by delta.

| Field | Source clock | Handling |
|---|---|---|
| `polls.ts` | collector wall at the poll | per-poll offset from mono (exact ±0.5 s + drift) |
| `polls.kismet_ts` | Kismet's "now" at the poll | the poll's offset |
| `observations.ts` | = poll ts | the poll's offset (via `poll_id`) |
| `observations.disconnects_last` | Kismet frame time, this boot (Kismet restarts each boot) | stale -> `+ delta`, else as is |
| `observations.last_time` (not stored on the server; used for `devices.last_seen`) | Kismet frame time | stale -> `+ delta` |
| `device_config_history.ts` | = poll ts | the poll's offset (via `poll_id`) |
| `alerts.ts` | Kismet alert time | stale (boot of its `poll_id`) -> `+ delta` |
| `alerts.raw` | Kismet JSON incl. its timestamp | **unchanged** – the audit copy; `alerts.ts` is the corrected time |
| `devices.last_seen` | Kismet `last_time` (Pi: MAX over all boots) | sent as the max corrected `observations.last_time` of the device in the batch (merge: GREATEST) |
| `devices.first_seen` | Kismet `first_time` (Pi: MIN over all boots – a stale value wins there) | the Pi value when it is placeable (outside every stale window of an anchored boot, and not after the batch); otherwise the device's earliest corrected `last_time` in the batch (merge: LEAST keeps a true earlier value uploaded before) |
| `devices.config_changed_at`, `devices.cur_at` (+ `cur_*`) | poll ts of the latest change / sighting (maybe a later poll than the batch) | sent only when placeable as above, else NULL (merge keeps the stored value; `cur_*` ignored without `cur_at`) |
| `device_freq_hist.updated_ts` | poll ts | row sent only when placeable |
| `ap_load_hourly.hour` | poll ts, bucketed on the Pi | synced polls only; unsynced polls' load goes to `ap_load_pending` and is folded with the true hour at the NTP anchor (N2); for a server-anchored boot the pending samples are sent as load samples (`load_ap_load_samples()`, fuller-wins – the boot's first and last hour may keep only the larger part) |
| `observations` row selection (`is_new_obs`) | Kismet `last_time` vs the stored `last_seen` | per boot (4.1) |

Backward steps (a stale clock *ahead* of true time) cannot occur with this
Pi's fixed past epoch; if one is seen (`delta < 0`), polls are still
corrected from mono, Kismet times are left raw and the boot is flagged in
`sensor_clock_boots`.

**Accuracy budget (target ±10 s).** Poll `ts` is an integer second (±0.5 s);
anchor: chrony's offset at sync (< 0.5 s by the `clock_synced` rule) or the
server anchor (±RTT/2); monotonic drift between the poll and the anchor:
residual frequency error after the drift file, typically ≤ 5 ppm = 0.43 s a
day – ±10 s holds for unsynced stretches up to ~3 weeks at 5 ppm. Slewing a
large offset instead of stepping would bend CLOCK_MONOTONIC too; `makestep`
prevents it, and `clock_synced` requires |offset| < 0.5 s.

**Validation test** (the developer runs it; N3):
`wifi-sensor/docs/clock-validation.md`, a step-by-step checklist that never
touches the network and recovers by itself: the next boot's clock is set a
few hours back through the mtime of `/var/lib/systemd/timesync/clock` (what
systemd raises the boot clock to), and chrony's sources are held offline for
N minutes after that boot (chronyd still runs - Kismet is ordered after it).
Runs: A (30 min, 3 h, 24 h; ground truth from a sampler on the Mac),
B (a boot that ends before its anchor -> time-unverified), C (the real
33-day stale date, only from the dorm LAN: Tailscale's TLS fails with it),
D (the server anchor). The clock is never moved by hand while connected
(`date -s` backwards breaks WireGuard handshakes from the Pi). A dated
`findings.md` row for each run.

### 8.4 Adapter identity from the heartbeat (D8)

`adapter = null` (interface missing): no change, card shows "adapter not
present". First report while the seeded row has no `usb_id`/`mac` (today):
fill them, `reported_by = 'sensor'`, no boundary. A different `usb_id` or
`mac`: update the row, a `hardware_change` annotation (boundary, at the
heartbeat's corrected `sensor_ts`), audit row `sensor.adapter`. A chipset
other than RTL8821CU: a warning on the card (`rssi_valid()` knows only its
floors).

---

## 9. Load, limits, disk

### 9.1 Volumes (per-poll upload)

| Mode | Per request | Body (gzip) | Server |
|---|---|---|---|
| Steady, 1 batch per poll (2 880/day) | 1 poll, ~61 observations, ~61 devices, ~70 load rows, ~0.2 history rows, a few alerts, the heartbeat | ~6–9 KB | one transaction, ~10–30 ms |
| Idle heartbeat (only without batches) | – | ~0.6 KB | one upsert |
| Catch-up, 120 polls | ~7.3 k observations, ~900 devices, ~1 k load rows | ~250–400 KB | est. 0.5–1.5 s |
| Daily | ~2 900 requests | ~20–25 MB/day upload | same rows as the snapshots, plus ~170 k device and ~200 k load-row updates (HOT where possible; autovacuum 6.4) |

Replay of the current 3.5-day gap (10-14 05:21 → deploy): ~85 batches ≈
2–4 min of server time, plus 2 s pacing ≈ 5–7 min.

### 9.2 Limits

- Caddy `handle /api/ingest/*`: `request_body { max_size 2MB }`.
- api: decompressed ≤ 16 MB; ≤ 240 polls and ≤ 30 k rows a batch; one batch
  per sensor at a time (advisory lock -> 409); a global semaphore of 1 for
  merges (the dashboard keeps 3 of the 4 pool connections); per sensor
  token bucket **120 batches/min** (steady 2/min, catch-up ~25/min) and 10
  heartbeats/min; 20 failed authentications/min per client IP -> 429 for
  10 min.
- `INGEST_ENABLED=0` in `.env` -> 503 on every ingest route.

### 9.3 Disk (amendment 3; measured read-only 2026-10-17)

| Item | Size | Note |
|---|---|---|
| Free | **2.85 GB** of 19.7 GB (86 %) | 3.7 GB on 10-13 |
| `server/buffer-snapshot*.db` (4 files, 10-10 … 10-14) | **1.72 GB** | the 10-13 → 10-17 drop; after D4 snapshots are recovery only |
| Docker: reclaimable images 818 MB, build cache 178 MB | ~1.0 GB | `docker image prune`, `docker builder prune` |
| `twitch-drops-miner` image (not this project) | 990 MB | your call |
| disabled snap revisions (core22/24, snapd, gcloud) | ~0.5–1 GB | `snap remove --revision` |
| `server/backups/` (7 dumps + 1 kept) | 569 MB | ~66 MB per dump, growing ~2.5 MB/day each |
| database | 1.24 GB (`observations` 871 MB, `devices` 142 MB, client tables 91 MB) | |

**Growth with continuous collection**: observations ~39 MB/day, other
tables ~3–5 MB/day, heartbeats 0.3 MB/day ≈ **~43 MB/day**, plus dumps
(~18 MB/day if `backup.sh` keeps 7 daily). Keeping ≥ 1 GB free for vacuum,
temp files and image builds:

| Scenario | Usable | Days until the 1 GB floor |
|---|---|---|
| as is | 1.85 GB | **~30–43 days** (mid/late November) |
| after removing the snapshots (keep the newest one) + docker prune | ~4.2 GB | ~70–100 days |
| + disk enlarged to 30 GB (the e2-micro free tier includes 30 GB standard PD in us-east1; online resize: `gcloud compute disks resize`, `growpart`, `resize2fs`) | ~14.5 GB | ~240–340 days |

Options (N4 – your decision before the uploader is enabled):

- **A. Clean up now** (snapshots except the newest, docker prune, old
  snaps): +~2.4 GB, no design work. Reversible only by re-exporting.
- **B. Enlarge the disk to 30 GB**: likely free tier, minutes of work, an
  online resize (snapshot the disk first).
- **C. Raw-observation retention with rollups kept**: delete
  `observations` older than N days (e.g. 120) after archiving each month as
  a dump off the box (`restore_observations_from_dump.sh` exists); rollups,
  load history, events stay. Cost: detectors and baselines can re-run only
  inside N days unless restored. Design + script, ~1 day.
- **D. Partition `observations` by month** (retention becomes `DROP
  PARTITION`, no bloat): a table rewrite needing ~1 GB free and a
  maintenance window; best combined with C or TimescaleDB later.
- Recommendation: **A + B now**, C designed before ~6 months of data; D only
  with C.

### 9.4 Disk: the commands (N4 A + B; the developer runs them)

**A. Cleanup** (measured 2026-10-17). First copy the snapshots off the box
- they are raw data - and compare the hashes, from the Mac:

```sh
mkdir -p ~/wifi-audit-snapshots
scp 'google:~/wifi-audit/server/buffer-snapshot*.db' ~/wifi-audit-snapshots/      # 4 files, 1.72 GB
ssh google 'cd ~/wifi-audit/server && sha256sum buffer-snapshot*.db'
cd ~/wifi-audit-snapshots && shasum -a 256 buffer-snapshot*.db                      # the same four hashes
```

Then on the box (`cd ~/wifi-audit/server`):

| Command | Frees (expected) | Removes |
|---|---|---|
| `rm buffer-snapshot-20261010.db buffer-snapshot-20261012.db buffer-snapshot-20261013.db` | 1.29 GB | the three older snapshots (copied); `buffer-snapshot.db` (10-14) stays until the first live upload is verified, then `rm buffer-snapshot.db` (+0.43 GB) |
| `docker image prune` | ~0.5 GB | **dangling images only** (42 untagged layers of old api/frontend builds). Never a tagged image, never one a container uses, never a volume. The base images `python:3.13-slim`, `node:22-alpine`, `postgres:17` stay (no re-download at the next build) |
| `docker builder prune -a` | 178 MB | the build cache only (the next api/frontend build takes longer). Never images, containers or volumes |
| check: `docker system df; df -h /` | | `Local Volumes` must stay 1.59 GB (the database volume) |

Optional, system-level (your call): `sudo journalctl --vacuum-size=100M`
(311 MB of journals -> ~0.2 GB freed); the disabled snap revisions (~0.54
GB): `sudo snap remove core22 --revision=2437`, `sudo snap remove core24
--revision=1643`, `sudo snap remove google-cloud-cli --revision=509`, `sudo
snap remove snapd --revision=27738`, and `sudo snap set system
refresh.retain=2`. **Never** `docker system prune --volumes` or `docker
volume prune` (the database is the volume `wifi-audit_db-data`).

Expected: ~2.0 GB (snapshots + docker), ~2.7 GB with the optional items,
+0.43 GB when the last snapshot goes.

**B. Grow the disk to 30 GB** (online, no reboot).

Check first - the free tier is one e2-micro in us-west1, us-central1 or
us-east1 **and 30 GB-month of *standard* persistent disk per billing account
in total** (every disk of the account counts; balanced and SSD disks are
never free; snapshots beyond 5 GB-month are billed):

1. Console -> Compute Engine -> VM instances: the box is `e2-micro` in
   **us-central1-c** (read from the metadata server - eligible).
2. Console -> Compute Engine -> Storage -> Disks: the boot disk (device
   `instance-20250906-161250`; the disk usually has the same name) must show
   **Type: Standard persistent disk**. If it is "Balanced", it is billed
   already and 30 GB costs more - stop and decide. No other disks may exist in
   the account (they count towards the 30 GB).
3. Billing -> Reports: no unexpected Compute Engine charges so far.

Steps:

1. A fresh `./backup.sh` copied off the box (the database). Optional: a disk
   snapshot (Disks -> the disk -> Create snapshot; ~17 GB used, billed beyond
   5 GB - delete it a day later).
2. Resize: Console -> Disks -> the disk -> Edit -> Size **30** -> Save, or in
   Cloud Shell: `gcloud compute disks resize instance-20250906-161250
   --zone=us-central1-c --size=30GB` (it asks to confirm; growing only).
3. On the box (Ubuntu 24.04, ext4 root on `/dev/sda1`, the **last**
   partition - checked: it ends at the disk's end, so it can grow):
   `lsblk` (sda now 30G) -> `sudo growpart /dev/sda 1` -> `sudo resize2fs
   /dev/sda1` -> `df -h /` (~29 G, ~12-13 GB free). Both are online
   operations on a mounted ext4 root.

---

## 10. Failure modes

| # | Failure | Effect | Handling |
|---|---|---|---|
| F1 | Network / server down | nothing sent | backoff ≤ 10 min, replay in order; contact gap, no event |
| F2 | Server commits, response lost | batch re-sent | natural keys -> 0 inserted; `replays + 1` |
| F3 | Uploader crash / power cut | cursor not advanced | as F2; atomic rename |
| F4 | Outage longer than the retention | rows pruned before upload | retention-risk event 2 days before (D10); loss exact via `seq` |
| F5 | Buffer replaced | row ids restart | new `buffer_id` -> cursor from the server's resume point |
| F6 | Boot without NTP | stale timestamps | corrected from mono (8.3); held back until anchored |
| F7 | Reboot before an anchor | that boot's time unknown | time-unverified, counted, not uploaded |
| F8 | Clock slewed instead of stepped | mono bent | `makestep` configured; `clock_synced` needs \|offset\| < 0.5 s |
| F9 | Token leaked | writes as that sensor | revoke + new token; writes only that sensor's rows |
| F10 | Token wrong / revoked | 401 / 403 | backoff; `upload_unauthorized` event |
| F11 | Batch refused | 422 | never skipped; `upload_rejected` event |
| F12 | Oversized batch | 413 | halve |
| F13 | Merge bug | wrong rows | fixture tests; snapshot path and backups independent; re-send after a fix is idempotent |
| F14 | Refresher stuck | charts stale, status fine | `ingest_state.dirty_at` age on the Sensors page; compose restarts it |
| F15 | Replay slows the dashboard | slower pages | semaphore 1, pacing; measured |
| F16 | Disk full on the box | 5xx | Pi keeps 14 days; 9.3 |
| F17 | Uploader too heavy on the Pi | competes with Kismet | `MemoryMax`, `Nice=10`, 2-s check is one shared-memory read |
| F18 | Uploader blocks the collector | lost polls | read-only, short read transactions (a long read would stall WAL checkpoints) |
| F19 | Adapter missing | – | `null` is not a change |
| F20 | Snapshot import during ingest | contention | both take the per-sensor advisory lock |
| F21 | Ingest and refresh overlap | – | the refresh has its own advisory lock; ingest only marks dirty |

---

## 11. What the thesis can measure

1. **Upload latency** per poll: `polls.received_at - polls.ts` (corrected
   ts; synced polls only for the steady-state figure). Components: poll
   duration (`duration_ms`, the ts is the poll's start) + detection ≤ 2 s +
   request ~0.2–0.5 s. **Expected: typically 3–8 s, at most ~35 s** (a poll
   that waits behind a retry or a catch-up batch). Time to the charts: +
   one refresh interval.
2. **Loss under outage**: `sensor_seq_gaps` reason `lost` (expected 0 below
   the retention); ground truth = a snapshot export after the experiment,
   per-hour counts compared (script in `evaluation/`).
3. **Backlog replay time** vs outage length: from the first contact after a
   gap to `backlog_polls = 0`; polls/s and rows/s; server CPU
   (`docker stats`) and dashboard latency (`measure.sh MODE=v2`) during it.
4. **Cost**: bytes per poll and per day, merge time, refresh duration.
5. **Delivery**: retries (`attempt`), duplicate deliveries (`replays`),
   contact availability.
6. **Clock**: `ts_offset_s` per corrected poll, `sensor_clock_boots`, the
   validation runs of 8.3 (error vs time without NTP), offset over time
   from heartbeats.

Staged outages without touching the Pi's network: stop the uploader for
10 min / 1 h / 6 h / 24 h, or `INGEST_ENABLED=0` on the server (exercises
retries over the real network).

---

## 12. Testing (Claude cannot reach the Pi for this)

- **Collector v8** (`collector/tests`): poll row first + `poll_id`; mono,
  boot and synced recorded; anchor once per boot; resume point reset on a
  new boot; `is_new_obs` per boot (a stale-clock boot still writes
  observations); load pending while unsynced and folded with the true hour
  at the anchor; prune by corrected time and append order (a stale stretch
  survives until uploaded); v7 -> v8 migration on a v7 file; chrony output
  parsing (synced, unsynced, offset too large, chronyc missing).
- **Uploader** (`uploader/tests`): fixture buffers written by the real
  `store.py`; batch selection; cursor only on 2xx; rebuild after failure;
  413 halving; backoff and cap; `buffer_id` reset; lost and unverified
  ranges; held-back polls; the time placement table of 8.3 field by field
  (stale and synced values, the step line, |delta| < 10 s, server anchor,
  backward step); token never logged; https only; **an invalid certificate
  is refused**; adapter from a fake sysfs tree.
- **End to end on the Mac** (D11): `fake_server.py` (stdlib, TLS with a
  test CA, fails on demand: drop after commit, 5xx bursts, 413, slow) – the
  replay test asserts the fake's rows equal the buffer exactly once, in
  order, on the corrected time line.
- **Server**: stdlib tests (`validate`, `ratelimit`, `tokens`, the stall
  logic); `ingest_merge_test.sql`, `contact_periods_test.sql`, the existing
  fixture tests green; importer tests; `v2_smoke.py` ingest refusals (no
  token, wrong secret, oversized) – Claude may run those against the box.
- **After each deploy** (Claude, read-only): `check_schema14.sql` – no
  unexplained `seq` gaps, latency distribution, heartbeat continuity.

---

## 13. Decisions

### 13.1 Decided 2026-10-17

| # | Decision |
|---|---|
| D1 | The buffer is the spool – yes |
| D2 | Tokens only via `ops/sensor_token.sh`; no Settings UI for tokens or sensor config in this phase |
| D3 | The table list as written; alerts unchanged. Follow-up in `PLAN.md` 11.5: pseudonymising client MACs in alerts on the Pi (estimate, not built) |
| D4 | Once live upload runs, the snapshot import is a recovery tool; client tables only behind `--with-client-tables`; existing client rows stay; `ops/delete_client_rows.sh` dry-run-first, for a later decision after a backup |
| D5 | Start from the server's last poll; the first upload fills the gap, no manual snapshot. **Retention**: the server's last poll is now 2026-10-14 05:21 (an import after the design), so the Pi starts pruning that range around **2026-10-28** (not 10-25) – see the timeline in 14 |
| D6 | **Changed**: no clock gate; capture without NTP, correct afterwards, ±10 s (8.3) |
| D7 | Separate refresher, configurable interval; measure and propose the shortest safe one (6.4) |
| D8, D9 | yes; config delivery out of this phase |
| D10 | Retention warning – yes, as a stall event |
| D11 | Fake server only; the first real run is `--check` after a fresh backup |
| D12, D13 | yes |
| D14 | Replaced by the per-poll upload; catch-up 120 polls, 2 s pacing, tuned after measuring |
| Amendments | 1 stall events (6.5); 2 TLS always verified + test; 3 disk measured before step 2 (9.3); 4 delete sensor revokes tokens (6.1) |

### 13.2 Questions N1–N10 – **all accepted 2026-10-17** as proposed, with these notes

- N4: the developer copies the buffer snapshots off the box before anything
  is deleted (they are raw data); cleanup only of unused images and build
  cache, never volumes - exact commands in 9.4.
- N7: unverified polls stay on the Pi, but are **counted** (heartbeat,
  `sensor_seq_gaps` reason `unverified`, the Sensors page) so the thesis can
  report the loss.
- N8: after the step-2 deploy, measure CPU use and CPU throttling of the
  e2-micro, not only the refresh duration; if an incremental refresh takes
  minutes, make steps 4–8 of `refresh_rollups()` incremental (6.4) rather
  than tune the interval.
- N3: the checklist is `wifi-sensor/docs/clock-validation.md`.

| # | Question | Decision |
|---|---|---|
| N1 | Token format `wsi1.<id>.<secret>` with a public id, so a wrong secret can be attributed to its sensor (needed for the `upload_unauthorized` event); the id reveals only which token row is meant | yes |
| N2 | AP load of unsynchronised polls: a pending table on the Pi, folded with the true hour at the anchor (exact), instead of "not recorded" for such stretches | pending + fold |
| N3 | The clock validation (8.3) needs you to stop chrony / set the clock / reboot on the Pi – run 1 (soft) right after the v8 deploy, run 2 when convenient? | yes |
| N4 | Disk: A + B before the uploader is enabled (9.3) | A + B |
| N5 | Heartbeat history at most once a minute (status upserted on every request) | yes |
| N6 | `polls.seq` also for snapshot imports (the buffer's row ids – the buffer has never been replaced), so loss analysis covers the seeded history | yes |
| N7 | Time-unverified polls stay on the Pi (pruned with the retention) – no quarantine upload | yes |
| N8 | Refresher default interval 60 s with the 4× duty-cycle cap; the measured incremental cost at the step-2 deploy decides whether 1 min is real | yes |
| N9 | `devices.config_changed_at` merge: a NULL from a newer row keeps the stored value (today it overwrites with NULL); applies to the importer too | yes |
| N10 | The recovery snapshot import also leaves the **client devices** (and their frequency histograms) on the Pi unless `--with-client-tables` - they only serve the client tables; matches what the live ingest accepts. `ops/delete_client_rows.sh --client-devices` could remove the 221 k existing client device rows (~140 MB with `--vacuum-full`) | yes (built in step 1) |

### 13.3 Open (2026-10-17)

| # | Question | Recommendation |
|---|---|---|
| N11 (accepted with N1-N10, built in step 3) | **Save the clock on the Pi**: a timer (hourly + at shutdown) that sets the mtime of `/var/lib/systemd/timesync/clock` to now while chrony is synchronised (installed by `install_sensor.sh`; the clock-test arm flag pauses it). A boot without NTP then starts at most an hour + the downtime behind instead of weeks: TLS (Tailscale, the ingest server, so the server anchor) keeps working, and the correction has a small offset to fix. No package (fake-hwclock does the same through its own file). Changes no captured data; a dated `findings.md` section 7 row | yes, in step 3 |

---

## 14. Build order and timeline

1. **Server – schema 14 + `ingest_merge()`**, the importer switched to it
   (client tables behind the flag, `seq` staged), `ops/delete_client_rows.sh`,
   fixture tests, `check_schema14.sql`. Deployable alone: the importer
   behaves as before (a re-import inserts 0 rows).
2. **Server – ingest api, stall events, refresher, Caddy limit,
   `ops/sensor_token.sh`, sensor delete revokes tokens**, stdlib tests,
   smoke additions. Deployable alone (no token = no ingest). The incremental
   refresh is measured at this deploy.
3. **Sensor – collector v8** (8.3 recording, per-boot `is_new_obs`, resume
   reset, load pending, prune, `buffer_id`), tests, `collector/README.md`.
4. **Sensor – uploader**, unit, installer, example config, tests, fake
   server, clock sampler, `wifi-sensor/README.md`.
5. **Frontend** – status states (8.2), contact gaps, backlog, clock,
   unverified polls, adapter.
6. **Docs** – both `CLAUDE.md` files, SPEC Q7 superseded, `findings.md`
   rows after the deploys.

**Timeline against the retention (D5).** The gap starts 2026-10-14 05:21;
the Pi prunes it from ~2026-10-28 05:21 (hourly prune, 14 days). The upload
needs steps 1–4 deployed, the disk decision (N4) and a `--check`. Steps 1–2
can be on the box in 1–2 days, 3–4 are the bulk (the clock work). **If step
4 is not deployed by 2026-10-24**, take a safety snapshot import that day
(it costs ~430 MB of disk – delete the file after the import). I will say at
each stop whether that date still holds.

---

## 15. Deploy and rollback

Each step's exact commands are given at its stop and recorded here when
built.

### 15.1 Step 1 – schema 14 (server, developer on the box)

```sh
cd ~/wifi-audit && git pull && cd server
./backup.sh                                                       # copy the dump off the box
sh ops/migrate_schema14.sh 2>&1 | tee ~/migrate14-$(date -u +%Y%m%dT%H%MZ).log
```

The api needs no rebuild for step 1 (it does not read the new tables yet).
Then Claude, read-only: `tests/sql/check_schema14.sql`.

Rollback: `docker compose exec -T db psql -U "$POSTGRES_USER" -d
"$POSTGRES_DB" -v ON_ERROR_STOP=1 < ops/rollback_schema14.sql`, then the
schema-13 `schema.sql` (`git show 9a33634:server/schema.sql | docker compose
exec -T db psql …`) and the schema-13 importer (`git checkout 9a33634 --
seed/import_snapshot.py`).

### 15.2 Step 2 – ingest api, refresher, Caddy limit (server)

Needs step 1 (schema 14) applied first: the new api reads its tables.

```sh
cd ~/wifi-audit && git pull && cd server
docker compose build api                                   # the api and the refresher's image
docker compose up -d api refresher
docker compose exec frontend caddy reload --config /etc/caddy/Caddyfile
```

No token is created yet (step 4): without one every ingest request is a
401, so nothing changes for the dashboard except the Sensors endpoint's
`contact` field (null) and the nightly baseline refresh. Then Claude,
read-only: `measure.sh MODE=v2` (its `v2_smoke.py` now also checks the
ingest refusals: 401 for no / malformed / unknown token, 413 for 3 MB).

**N8 measurement at this stop** (the incremental refresh is a write, so the
developer runs it; Claude watches `vmstat 5` and `docker stats` read-only
meanwhile): three times, a few minutes apart -

```sh
docker compose exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c '\timing on' \
  -c "SELECT * FROM refresh_rollups(1, now() - interval '1 hour')"
```

If it takes more than ~15 s, steps 4–8 become incremental (6.4) before the
uploader is enabled; otherwise `REFRESH_INTERVAL_S` stays 60.

Rollback: `INGEST_ENABLED=0` in `.env` + `docker compose up -d api` stops
every ingest at once. Full: `git checkout 7edf108 -- api docker-compose.yml
frontend/Caddyfile tests/perf/v2_smoke.py`, `docker compose stop refresher &&
docker compose rm -f refresher`, `docker compose build api && docker compose
up -d api`, Caddy reload.

### 15.3 Later steps

Recorded at their stops (Pi steps 3–4: `install_collector.sh`,
`install_uploader.sh`, the token, `--check` after a fresh backup, enable).

---

## 16. Future improvements

- **N11 (save the clock, if not decided otherwise)**, then an **RTC module
  (DS3231 on I²C, `dtoverlay=i2c-rtc,ds3231`)**: the hardware
  answer to the stale boot clock – boots start on true time ±2 ppm, the
  correction of 8.3 becomes a rarely used safety net (and a check).
- Make `refresh_rollups()` steps 4–8 conditional on what arrived (the 1-min
  chart lag).
- Config delivery (D9; designed 2026-10-24 in `config-delivery.md`, not built), pseudonymised alert MACs (`PLAN.md` 11.5), raw
  retention / partitioning (9.3 C, D), live push to the browser (PLAN 11.3).

---

## 17. Rollout record (2026-10-17)

Run by Claude in the rollout mode of 2026-10-17 (`CLAUDE.md`), the Pi's two
sudo steps by the developer. Times UTC.

| Time | Step | Result |
|---|---|---|
| 16:41 | backup before step 1 (developer, copied off the box) | `wifi_audit-20261017T164104Z.dump` |
| 18:12 | **step 1**: `ops/migrate_schema14.sh` | schema 14; all 10 fixture tests PASS (incl. `ingest_merge`, `contact_periods`); `check_schema14.sql` as claude_ro |
| 18:13 | **step 2**: api + refresher + Caddy body limit | smoke 127/127 incl. the ingest refusals (401 no / malformed / unknown token, 413 for 3 MB) |
| 18:19-18:27 | N8 measurement: incremental `refresh_rollups()` | 68 s (during the smoke runs), 82 s (instrumented), then 13.9 / 16.6 / 15.5 s quiet - over the ~15 s rule and ~23 % of a core at one a minute -> schema 15 |
| 18:31 | backup before schema 15 | `wifi_audit-20261017T183120Z.dump`, sha256 equal on the box and the Mac |
| 18:34-18:50 | rehearsal on a scratch copy (`ops/scratch_db.sh`) | caught a syntax error (fixed); 21 / 27 s showed two more full scans (new-AP hours, the timeline of every seen AP - both fixed); rollback rehearsed (back to the materialized view); then all tests PASS and **2.2 / 2.3 s**; timeline = full recompute |
| 20:31 | **schema 15 live**: `ops/migrate_schema15.sh` | all tests PASS; 25,265 timeline rows copied; incremental refresh 26 s (cold) then **3.8 s**; live timeline = full recompute (0 / 0) |
| 20:35 | **HTTPS** (the box served plain HTTP - `SITE_ADDRESS=:80` - found at step 4; the developer chose sslip.io) | `SITE_ADDRESS=34-42-184-97.sslip.io` (`.env` backed up as `.env.bak-20261017`), Let's Encrypt certificate, http -> https 308, HSTS; the Pi verifies it with Python 3.13's strict checks |
| 20:39 | ingest token 1 | created by `ops/sensor_token.sh --stdout`, piped into the Pi user's home, never shown |
| 20:41 | **step 3** (developer): `install_collector.sh` | buffer v8, first v8 poll 20:41:15 = the boot's NTP anchor; N11 timer; `findings.md` section 7 |
| 20:42 | backup before the first real upload | `wifi_audit-20261017T204244Z.dump` |
| 20:43 | `uploader.py --check` | heartbeat accepted; adapter identity filled by the sensor (`0bda:c811` + MAC, no boundary - D8); first batch 120 polls / 102 KB |
| 20:44 / 20:46 | **step 4** (developer): `install_uploader.sh` | first start exited (unit `Group=kismet`: no access to the root:pi 0640 token) - fixed (`Group=` the sensor user), re-run; running from 20:46:38 |
| 20:46:48-20:57:18 | **backlog replay** | 10,083 polls (2026-10-14 05:21:58 on) in 85 batches of 120, **10.5 min**; 2.2 s average merge per catch-up batch; 0 lost, 0 unverified, 0 replays |
| 21:05 | **step 5**: frontend | the live states; 14/14 test files pass in the build |
| 21:23-21:37 | refresher fixes (below) | rounds 2.4-4.3 s, each from the previous minute; no baseline repeat on restart |
| 21:32:30 | uploader reinstalled (developer): hourly frequency histograms, heartbeat stamped before the POST | batches without histogram rows; clock offset reads 0.35-0.41 s (both clocks within 0.5 ms of NTP: the rest is the one-way latency incl. TCP+TLS setup), before 0.45-0.71 s |
| 21:41 | disk cleanup (9.4 A) | the three older buffer snapshots on the Mac (`~/wifi-audit-snapshots/`, sha256 equal), deleted on the box; dangling images + build cache pruned (0.54 GB); 14 GB free of 29 GB; `buffer-snapshot.db` (10-14) kept |

**First measurements** (section 11):

- Upload latency, steady state: p50 3.3 s, p95 4.1 s, max 4.3 s (receive -
  poll ts; the first live polls). A steady batch: 485 ms merge, 12 KB gzip.
- Replay: 3.6 days of polls in 10.5 min (~16 polls/s, paced 2 s).
- CPU (`vmstat`, e2-micro): ~13.5 % user during the replay, ~2 % in steady
  state, steal 0 %.
- Refresh: 2-4 s incremental on schema 15 (see the fix below for the
  first live hour).

**Found on the first live day and fixed:**

- *Refresher never caught up:* it cleared the dirty mark only if no ingest
  moved it during the round - with a batch every 30 s and rounds longer
  than that, never; every round re-refreshed from 2026-10-14 23:42 (23-114
  s). It now takes the mark atomically before the refresh.
- *Clock offset* read -1.2 s with both clocks synced: the server subtracted
  half the previous request's RTT (mostly merge time). Now receive - send
  (offset + one-way latency), the uploader stamping right before the POST.
- *Frequency histograms* went with every batch (~1,200 rows, ~3.5 M updates
  a day): now at most once per UTC hour - **deployed to the Pi with the next
  `sudo ./install_uploader.sh`** (the developer's).
- *Nightly baselines* re-ran after every refresher restart: the last run's
  day now comes from `ap_baselines.computed_at`. The first live run took
  663 s (257 s in the step-2 measurement, uncontended), and the duty-cycle
  cap counted it, holding the next rollup round back 41 min: the cap now
  paces the rollups and contact periods only.
- *Postgres crash-restarts* ("server process exited with exit code 2", no
  message before it, then recovery; 2026-10-14 06:05, 10-16 05:05, 10-17
  21:04 and 21:23 - the first two before live upload): no OOM kill in the
  kernel log or Docker's state, /dev/shm 2 % used; cause open. The uploader
  rode through (502/503, retries, then one batch of 3 polls, 0 gaps), but the
  refresher could not put back the dirty mark it had taken over a dead
  connection: it now keeps it in memory and writes it back first thing in
  the next round. The window lost at 21:23 (before the fix) was checked:
  hourly rollups, security periods and events are recomputed by later
  rounds, and for the 29 APs with history in 20:40-21:30 the
  `ap_config_changes` table equals the rules view (0 missing, 0 extra).

**Open (the developer's):** whether the external IP `34.42.184.97` is static
(Console -> VPC network -> IP addresses; promote it if ephemeral - the
sslip.io name and the uploader depend on it); the clock-validation runs
(`wifi-sensor/docs/clock-validation.md`; their helper scripts
`clock-test.sh`, `clock_validation.py`, `clock_truth.py` are still to be
written); deleting client rows (`ops/delete_client_rows.sh`, D4).

**2026-10-18 (follow-up):** the external IP promoted to static (developer);
`http://<IP>` now redirects to the HTTPS name (`PUBLIC_IP` in `.env`, the
Caddyfile); no retention-risk event for a backlog the server already holds
(the false one of the first `--check` was labelled a false positive);
deleting an empty sensor works again (an all-zero `sensor_summary` row no
longer counts as data; `test` deleted); Postgres diagnostic logging for the
crash-restarts (`docs/postgres-crashes.md`); clock-validation helpers built
(`wifi-sensor/docs/clock-validation.md`); pipeline metrics export
(`evaluation/pipeline/`).

**2026-10-18, schema 16:** `refresh_runs` (one row per refresher step) after
a backup copied to the Mac (`wifi_audit-20261018T155827Z.dump`, sha256 equal);
`ops/migrate_schema16.sh` 16:35 UTC, all 10 fixture tests PASS; the
refresher records from 16:36:40; `evaluation/pipeline/export.sh` reads it
(the container log only before that).

