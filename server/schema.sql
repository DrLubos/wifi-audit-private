-- wifi-audit server schema (PostgreSQL 14+, no extensions).
--
-- Idempotent: run as often as you like with
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f schema.sql
-- It is the complete definition at the current version: a fresh volume is
-- created from it, and re-applying it to a database at the same version only
-- replaces functions and views. Upgrades between versions are separate
-- migration scripts (ops/migrate_schema7.sh for 6 -> 7, ops/migrate_schema8.sh
-- for 7 -> 8, ops/migrate_schema9.sh for 8 -> 9).
--
-- Mirrors the sensor's SQLite buffer (wifi-sensor/collector/store.py, schema v7)
-- with a sensor_id on every row, and adds sensors, ap_baselines and detections.
-- Differences from the buffer, all intentional:
--   * unix seconds -> timestamptz, MAC strings -> macaddr, 0/1 -> boolean,
--     alerts.raw -> jsonb; the buffer's `key` columns are named device_key here.
--   * sensor-local state is not mirrored: the `id` serials, the `sent` upload
--     flags and the `meta` table.
--   * observations holds AP rows only, with the columns the detectors and the
--     dashboard read (schema 7); the Pi keeps every device type for its 14 days.
--   * natural keys everywhere; observations is keyed (sensor_id, device_key, ts):
--     ts is in the key, so it still converts to a TimescaleDB hypertable
--     without a rewrite (see the end).
-- Privacy: no column for client IP addresses or WPS identity fields exists and
-- none may be added (see CLAUDE.md).

BEGIN;

CREATE TABLE IF NOT EXISTS schema_meta (
  key   text PRIMARY KEY,
  value text NOT NULL);
-- 2: observations.disconnects_last (buffer v3)
-- 3: polls.ds_hop_n / ds_hop_visited / ds_hop_ok (buffer v4)
-- 4: RSSI floors as censored values: rssi_valid(), rssi_is_floor(),
--    observations.rssi_floor (buffer v5), ap_baselines.n_floor; view
--    ap_channel_changes; refresh_ap_baselines() clears baselines that no
--    longer qualify
-- 5: covering index observations_device_ts_rssi_floor (rssi, rssi_floor)
--    replaces observations_device_ts_rssi; view ap_config_changes (the
--    cleaned configuration timeline), ap_channel_changes derived from it
-- 6: dashboard read tables ap_rssi_hourly, sensor_hourly, ap_summary,
--    sensor_summary, filled by refresh_rollups(); ap_config_changes is a
--    materialized view refreshed there (ap_channel_changes stays a view on it)
-- 7: slim observations, AP rows only, keyed (sensor_id, device_key, ts)
--    INCLUDE (rssi, rssi_floor) + BRIN on ts; devices.cur_* (latest AP load);
--    client_bssids; ap_baselines.band (from devices.adv_channel) replaces
--    main_freq_khz; collector buffer v6 (ops/migrate_schema7.sh)
-- 8: ap_load_hourly (AP load per hour and advertised channel: collector
--    buffer v7, and the per-poll load columns of buffers <= v5 as source
--    'v5_obs'); ap_channel_at(), load_ap_load_samples() (ops/migrate_schema8.sh)
-- 9: dashboard v2 model (server/docs/dashboard-v2/PLAN.md section 4.3):
--    accounts, account_settings, networks, account-level aps + ap_sensors,
--    ap_security_periods, sensor_periods, sensor_annotations, sensor_config,
--    sensor_uploads, kismet_alert_rules, audit_log; detections as the event
--    table (kind, status, ts_end, updated_at; acked -> status); security_level()
--    and friends; refresh_events(); new rollup columns (ops/migrate_schema9.sh)
INSERT INTO schema_meta (key, value) VALUES ('schema_version', '9')
  ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;

-- --- accounts and sensors ------------------------------------------------------------

-- The account the dashboard shows (schema 9). One today; the api takes it from
-- one place (deps.get_account) so login can replace that later. tz sets the
-- dashboard's bucket and day boundaries; it must have whole-hour offsets, so
-- every bucket edge is a UTC hour.
CREATE TABLE IF NOT EXISTS accounts (
  id            serial PRIMARY KEY,
  name          text NOT NULL UNIQUE,
  tz            text NOT NULL DEFAULT 'Europe/Bratislava',
  write_version bigint NOT NULL DEFAULT 0,     -- bumped by every write endpoint (part of the ETags)
  created_at    timestamptz NOT NULL DEFAULT now());
INSERT INTO accounts (id, name) VALUES (1, 'Account') ON CONFLICT DO NOTHING;
SELECT setval(pg_get_serial_sequence('accounts', 'id'), (SELECT max(id) FROM accounts));
COMMENT ON TABLE accounts IS
  'The account the dashboard shows (schema 9): name, time zone of its buckets, write version for the ETags.';

CREATE TABLE IF NOT EXISTS sensors (
  id          serial PRIMARY KEY,
  account_id  integer NOT NULL DEFAULT 1 REFERENCES accounts(id),
  name        text NOT NULL UNIQUE,          -- short handle, e.g. 'pi-fri'
  description text,
  location    text,                          -- free text, no coordinates
  hardware    text,                          -- e.g. 'Raspberry Pi 3B+ · RTL8821CU (USB)'
  tz          text NOT NULL DEFAULT 'UTC',   -- day boundaries for daily statistics
  created_at  timestamptz NOT NULL DEFAULT now());
COMMENT ON TABLE sensors IS
  'One row per fixed sensor. Every time-series and device row references it.';

-- --- mirrored collector tables ----------------------------------------------------

CREATE TABLE IF NOT EXISTS polls (
  sensor_id      integer NOT NULL REFERENCES sensors(id),
  ts             timestamptz NOT NULL,       -- collector time
  kismet_ts      timestamptz,                -- kismet.system.timestamp.sec
  devices_total  integer,                    -- kismet.system.devices.count
  devices_active integer,                    -- devices returned by the last-time query
  new_obs        integer,                    -- observation rows written by that poll
  ds_running     boolean,                    -- every datasource running
  ds_error       text,                       -- first datasource error reason
  ds_packets     bigint,                     -- sum of datasource num_packets
  ds_hop_n       integer,                    -- live hop-list length (buffer v4)
  ds_hop_visited integer,                    -- hop entries really tuned (buffer v4)
  ds_hop_ok      boolean,                    -- configured list, fully visited (buffer v4; NULL = no explicit list / older buffer)
  duration_ms    integer,
  ok             boolean NOT NULL DEFAULT true,
  error          text,
  PRIMARY KEY (sensor_id, ts));
-- ds_hop_*: rows from buffers before v4 have NULL. Degraded channel coverage
-- (Kismet 2025-09 collapsing the hop list) is invisible in ds_packets - see
-- wifi-sensor/docs/findings.md section 7.
COMMENT ON TABLE polls IS
  'One row per collector poll (30 s): collector, Kismet and datasource health.';

CREATE TABLE IF NOT EXISTS devices (
  sensor_id  integer NOT NULL REFERENCES sensors(id),
  device_key text NOT NULL,                  -- Kismet device key (instance-local)
  mac        macaddr NOT NULL,
  type       text NOT NULL,                  -- ap | client | bridged | adhoc | wds | device | unknown
  manuf      text,
  first_seen timestamptz NOT NULL,           -- Kismet first_time
  last_seen  timestamptz NOT NULL,           -- Kismet last_time of the newest observation
  -- advertised AP configuration, last known values (NULL for non-APs)
  ssid        text,                          -- '' = cloaked SSID
  cloaked     boolean,
  crypt       text,                          -- Kismet crypt_string, e.g. 'WPA2-PSK CCMP'
  crypt_bits  bigint,                        -- Kismet crypt_bitfield (uint64)
  mfp_sup     boolean,
  mfp_req     boolean,
  adv_channel text,
  ht_mode     text,
  beacon_rate integer,
  country     text,
  config_changed_at timestamptz,             -- poll ts of the last configuration change
  -- AP load, latest value (schema 7 / buffer v6; NULL for non-APs). Not
  -- configuration: no history row, never part of the configuration diff.
  cur_n_clients     integer,                 -- Kismet num_associated_clients
  cur_qbss_stations integer,                 -- AP-reported station count (QBSS IE)
  cur_util_pct      real,                    -- AP-reported channel utilisation (QBSS IE)
  cur_at            timestamptz,             -- poll ts of these values
  PRIMARY KEY (sensor_id, device_key));
CREATE INDEX IF NOT EXISTS devices_mac       ON devices (sensor_id, mac);
CREATE INDEX IF NOT EXISTS devices_type      ON devices (sensor_id, type);
CREATE INDEX IF NOT EXISTS devices_ssid      ON devices (sensor_id, ssid);
CREATE INDEX IF NOT EXISTS devices_last_seen ON devices (sensor_id, last_seen);
COMMENT ON TABLE devices IS
  'Current identity of every device the sensor has seen and, for APs, the advertised configuration. Never pruned.';

CREATE TABLE IF NOT EXISTS device_config_history (
  sensor_id  integer NOT NULL,
  device_key text NOT NULL,
  ts         timestamptz NOT NULL,           -- poll ts at which the NEW config was seen
  -- the configuration that was replaced
  ssid        text,
  cloaked     boolean,
  crypt       text,
  crypt_bits  bigint,
  mfp_sup     boolean,
  mfp_req     boolean,
  adv_channel text,
  ht_mode     text,
  beacon_rate integer,
  country     text,
  PRIMARY KEY (sensor_id, device_key, ts),
  FOREIGN KEY (sensor_id, device_key) REFERENCES devices (sensor_id, device_key));
CREATE INDEX IF NOT EXISTS device_config_history_ts ON device_config_history (sensor_id, ts);
COMMENT ON TABLE device_config_history IS
  'Previous AP configuration, appended when the advertised configuration changes. The beacon IE checksum and fingerprint were never part of the diff (they vary per beacon; not stored from schema 7).';

-- The core time series: one row per AP and poll in which the AP was active.
-- Schema 7 (ops/migrate_schema7.sh): AP rows only (other device types stay on
-- the Pi for its 14 days and in the backups), only the columns the detectors
-- and the dashboard read, 8-byte columns first (no alignment padding), rows
-- written in ts order. Not stored any more: Kismet's last_time, device
-- frequency (freq_khz, which was never the reception channel of rssi - the
-- AP's channel is devices.adv_channel) and channel, rssi_min/rssi_max, pk_tx,
-- pk_rx, bytes, the beacon IE checksum and fingerprint; the AP load
-- (n_clients, qbss_stations, util_pct) is the latest value on devices, a
-- client's BSSID is in client_bssids.
CREATE TABLE IF NOT EXISTS observations (
  ts               timestamptz NOT NULL,     -- poll ts
  pk_total         bigint,                   -- cumulative counters as Kismet reports them
  pk_data          bigint,
  bss_timestamp    bigint,                   -- AP uptime (us); a drop means a restart
  disconnects_last timestamptz,              -- last deauth/disassoc frame (buffer v3; NULL before, or none yet)
  sensor_id        integer NOT NULL,
  disconnects      integer,                  -- client_disconnects: size of the current deauth/disassoc burst
  rssi             smallint,                 -- Kismet sig_last (one frame), NULL when no reading;
                                             -- may hold a raw floor value (read via rssi_valid())
  rssi_floor       boolean,                  -- buffer v5+: the reading was an adapter floor value
  device_key       text NOT NULL,
  -- The key leads with the device: every reader takes one AP's rows (the AP
  -- page, the hourly rollup, evil_twin) or all APs' rows of a time window
  -- (deauth_flood, through the BRIN index). It INCLUDEs rssi and rssi_floor,
  -- so the RSSI readers are index-only scans: a column outside the index made
  -- every AP page read ~30k heap pages (7-9 s cold, measured 2026-09-28).
  -- Index-only scans need the heap pages all-visible: the seed importer
  -- vacuums observations after each load. ts is still in the key, which is
  -- what a TimescaleDB hypertable needs.
  CONSTRAINT observations_pkey PRIMARY KEY (sensor_id, device_key, ts) INCLUDE (rssi, rssi_floor),
  CONSTRAINT observations_device_fkey
    FOREIGN KEY (sensor_id, device_key) REFERENCES devices (sensor_id, device_key));
-- Rows arrive in ts order (seed import, migration), so a BRIN index answers
-- time-window scans at a fraction of a b-tree's size.
CREATE INDEX IF NOT EXISTS observations_ts_brin ON observations USING brin (ts);
-- RSSI floors: buffer v5 stores the adapter's floor readings (-106/-120) as
-- rssi NULL + rssi_floor true; rows imported from older buffers keep the raw
-- value in rssi and NULL in rssi_floor. Never rewritten - read through
-- rssi_valid() / rssi_is_floor().
COMMENT ON TABLE observations IS
  'One row per AP per poll in which the AP was active: the RSSI/counter time series the detectors and the dashboard read. AP rows only (schema 7).';

-- AP load per UTC hour and advertised channel (schema 8). Source data like
-- observations, never written by refresh_rollups(): rows come from the
-- collector's ap_load_hourly (buffer v7, source 'buffer') or are rebuilt from
-- the per-poll n_clients / qbss_stations / util_pct of buffers <= v5
-- (load_ap_load_samples(), source 'v5_obs'). Sums and counts, so a partial
-- hour merges exactly: every writer keeps the row with more polls (ON CONFLICT
-- ... WHERE EXCLUDED.polls > ap_load_hourly.polls). The channel is in the key
-- because campus APs change channel many times a day (dynamic channel
-- selection); '' = unknown. Read time (dashboard v2): clients of an AP-hour =
-- max(qbss_max) when qbss_polls > 0, else max(clients_max); utilisation =
-- sum(util_sum) / sum(qbss_polls). Before buffer v7 there is no BSS-Load
-- presence flag: for 'v5_obs' a poll counts in qbss_polls when its station
-- count or utilisation is > 0.
CREATE TABLE IF NOT EXISTS ap_load_hourly (
  sensor_id     integer NOT NULL,
  device_key    text NOT NULL,
  hour          timestamptz NOT NULL,        -- start of the UTC hour
  channel       text NOT NULL,               -- advertised channel at those polls ('' = unknown)
  polls         integer NOT NULL,            -- polls with a new observation of the AP on this channel
  clients_polls integer NOT NULL,            -- polls with a Kismet client count
  clients_max   integer,                     -- Kismet num_associated_clients
  clients_sum   integer,
  qbss_polls    integer NOT NULL,            -- polls whose beacon carried a BSS Load element
  qbss_max      integer,                     -- AP-reported station count
  qbss_sum      integer,
  util_max      real,                        -- AP-reported channel utilisation, %
  util_sum      real,
  source        text NOT NULL CHECK (source IN ('buffer', 'v5_obs')),
  PRIMARY KEY (sensor_id, device_key, hour, channel),
  FOREIGN KEY (sensor_id, device_key) REFERENCES devices (sensor_id, device_key));
CREATE INDEX IF NOT EXISTS ap_load_hourly_hour ON ap_load_hourly (sensor_id, hour);
COMMENT ON TABLE ap_load_hourly IS
  'AP load per UTC hour and advertised channel: client count, BSS Load station count and channel utilisation as sums and counts (schema 8). Source data, not derived; the row with more polls wins on every load.';

CREATE TABLE IF NOT EXISTS device_freq_hist (
  sensor_id  integer NOT NULL,
  device_key text NOT NULL,
  freq_khz   integer NOT NULL,
  packets    bigint NOT NULL,                -- cumulative, as reported by Kismet
  updated_ts timestamptz NOT NULL,
  PRIMARY KEY (sensor_id, device_key, freq_khz),
  FOREIGN KEY (sensor_id, device_key) REFERENCES devices (sensor_id, device_key));

CREATE TABLE IF NOT EXISTS associations (
  sensor_id  integer NOT NULL,
  ap_key     text NOT NULL,                  -- device_key of the AP
  client_mac macaddr NOT NULL,
  first_seen timestamptz NOT NULL,           -- poll ts at which the pair was first listed
  last_seen  timestamptz NOT NULL,           -- poll ts at which the AP still listed the client
  PRIMARY KEY (sensor_id, ap_key, client_mac),
  FOREIGN KEY (sensor_id, ap_key) REFERENCES devices (sensor_id, device_key));
CREATE INDEX IF NOT EXISTS associations_client ON associations (sensor_id, client_mac);
COMMENT ON TABLE associations IS
  'AP x client MAC pairs from Kismet''s associated_client_map. last_seen means "still listed at that poll", not "last frame exchanged".';

CREATE TABLE IF NOT EXISTS client_bssids (
  sensor_id  integer NOT NULL,
  client_key text NOT NULL,                  -- device_key of the client
  bssid      macaddr NOT NULL,               -- Kismet last_bssid of the client
  first_seen timestamptz NOT NULL,           -- poll ts at which the pair was first seen
  last_seen  timestamptz NOT NULL,           -- poll ts at which the pair was last seen
  PRIMARY KEY (sensor_id, client_key, bssid),
  FOREIGN KEY (sensor_id, client_key) REFERENCES devices (sensor_id, device_key));
CREATE INDEX IF NOT EXISTS client_bssids_bssid ON client_bssids (sensor_id, bssid);
COMMENT ON TABLE client_bssids IS
  'Client x BSSID pairs (Kismet last_bssid), deduplicated like associations; replaces the per-poll observations.bssid (schema 7 / buffer v6).';

CREATE TABLE IF NOT EXISTS probes (
  sensor_id  integer NOT NULL,
  device_key text NOT NULL,                  -- client device
  ssid       text NOT NULL,                  -- '' = wildcard probe
  first_time timestamptz NOT NULL,
  last_time  timestamptz NOT NULL,
  PRIMARY KEY (sensor_id, device_key, ssid),
  FOREIGN KEY (sensor_id, device_key) REFERENCES devices (sensor_id, device_key));
CREATE INDEX IF NOT EXISTS probes_ssid ON probes (sensor_id, ssid);

CREATE TABLE IF NOT EXISTS alerts (
  id              bigserial PRIMARY KEY,
  sensor_id       integer NOT NULL REFERENCES sensors(id),
  hash            text NOT NULL,             -- Kismet alert hash (or sha1: derived by the sensor)
  ts              timestamptz,
  header          text NOT NULL,             -- e.g. DEAUTHFLOOD, CHANCHANGE
  class           text,
  severity        smallint,
  source_mac      macaddr,
  dest_mac        macaddr,
  transmitter_mac macaddr,
  other_mac       macaddr,
  channel         text,
  freq_khz        integer,
  device_key      text,                      -- no FK: Kismet may name a device the collector never stored
  text            text,
  raw             jsonb NOT NULL,            -- complete Kismet alert
  UNIQUE (sensor_id, hash));
CREATE INDEX IF NOT EXISTS alerts_ts ON alerts (sensor_id, ts);
COMMENT ON TABLE alerts IS 'Kismet''s native WIDS alerts, deduplicated by hash per sensor.';

-- --- server additions ---------------------------------------------------------------

CREATE TABLE IF NOT EXISTS ap_baselines (
  sensor_id  integer NOT NULL,
  device_key text NOT NULL,
  -- known identity: the BSSID <-> SSID pair this baseline was learned under
  bssid      macaddr NOT NULL,
  ssid       text,                           -- '' = hidden
  crypt      text,                           -- known-good encryption string
  trusted    boolean NOT NULL DEFAULT false, -- operator-approved (frozen identity)
  trusted_at timestamptz,
  note       text,
  -- RSSI statistics as seen by this sensor (dBm)
  rssi_median    real,
  rssi_mean      real,
  rssi_sd        real,                       -- population std dev
  rssi_robust_sd real,                       -- 1.4826 * MAD
  rssi_p5        smallint,
  rssi_p95       smallint,
  band           text,                       -- '2.4' / '5' / '6' (ap_band(): advertised channel, else busiest frequency)
  n_obs          integer,                    -- valid RSSI readings (floor values excluded)
  n_floor        integer,                    -- floor readings (censored) in the same window
  n_days         integer,                    -- distinct days (in sensors.tz) with a reading
  window_start   timestamptz,
  window_end     timestamptz,
  computed_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (sensor_id, device_key),
  FOREIGN KEY (sensor_id, device_key) REFERENCES devices (sensor_id, device_key));
CREATE INDEX IF NOT EXISTS ap_baselines_ssid  ON ap_baselines (sensor_id, ssid);
CREATE INDEX IF NOT EXISTS ap_baselines_bssid ON ap_baselines (sensor_id, bssid);
COMMENT ON TABLE ap_baselines IS
  'Per-sensor per-AP RSSI baseline (median + MAD-robust sigma, same definition as analysis/rssi_stability.py) and the known BSSID<->SSID pair. trusted = operator whitelist.';

-- The event table of the dashboard (schema 9). kind 'detection': the batch
-- detectors in detection/ (and the classification rule event
-- unknown_bssid_managed_ssid); 'kismet': episodes of Kismet's WIDS alerts;
-- 'config': security changes of an AP's advertised configuration; 'sensor':
-- outages and limited capture. Only the detectors and refresh_events() write
-- rows; the status columns are the operator's labels (input for the
-- false-positive analysis) and are never touched by either - only by the
-- status endpoint of the api (audit_log).
CREATE TABLE IF NOT EXISTS detections (
  id          bigserial PRIMARY KEY,
  sensor_id   integer NOT NULL REFERENCES sensors(id),
  kind        text NOT NULL DEFAULT 'detection' CHECK (kind IN ('detection', 'kismet', 'config', 'sensor')),
  ts          timestamptz NOT NULL,          -- time of the evidence / start of the episode
  ts_end      timestamptz,                   -- end of the episode or outage (NULL: a point, or still going)
  type        text NOT NULL,                 -- detector type, Kismet header, 'security_change', 'sensor_outage', ...
  severity    text NOT NULL CHECK (severity IN ('info', 'low', 'medium', 'high', 'critical')),
  -- subject: a stored device, or just a MAC/SSID when no device row exists;
  -- never a client MAC (Kismet alerts are attributed to their BSSID only)
  device_key  text,
  mac         macaddr,
  ssid        text,
  summary     text NOT NULL,
  evidence    jsonb NOT NULL DEFAULT '{}',
  status      text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'confirmed', 'false_positive')),
  status_at   timestamptz,
  status_note text,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now());   -- last change by a detector or refresh_events() (ETags)
CREATE INDEX IF NOT EXISTS detections_ts   ON detections (sensor_id, ts);
CREATE INDEX IF NOT EXISTS detections_open ON detections (sensor_id, severity) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS detections_feed ON detections (ts DESC, id DESC);   -- keyset paging of the feed
-- One row per (type, subject, episode start). The batch detectors refresh the
-- row of an episode they have already stored (detection/detections.py matches
-- on the stored window) instead of adding one, refresh_events() likewise; this
-- index is the guarantee for an exact re-run. The subject is the device_key or,
-- for a BSSID without a device row, the upper-case MAC; sensor events have none
-- (NULLS NOT DISTINCT).
CREATE UNIQUE INDEX IF NOT EXISTS detections_dedupe
  ON detections (sensor_id, type, coalesce(device_key, upper(mac::text)), ts) NULLS NOT DISTINCT;
COMMENT ON TABLE detections IS
  'Events (schema 9): detector results, Kismet alert episodes, security configuration changes, sensor outages. Written by the batch detectors and refresh_events(); status/status_at/status_note are the operator''s and survive every re-run.';

-- --- account configuration (schema 9; written by the api's write endpoints) ----------------

-- Typed columns, not jsonb: CHECKs and explicit migrations. Defaults: the
-- detectors' own CLI defaults; the evil_twin channel guard off (coverage over
-- false-positive suppression until a staged evaluation measures its cost).
CREATE TABLE IF NOT EXISTS account_settings (
  account_id           integer PRIMARY KEY REFERENCES accounts(id),
  -- classification rules (ap_classification())
  ignore_random_bssid  boolean NOT NULL DEFAULT true,
  suspicious_unallowed boolean NOT NULL DEFAULT true,
  managed_by_vendor    boolean NOT NULL DEFAULT false,
  ignore_weaker_dbm    smallint,                       -- NULL = off
  -- mobile APs: case-insensitive prefixes of devices.manuf
  mobile_vendors       text[] NOT NULL DEFAULT ARRAY['Apple', 'Samsung', 'Google', 'OnePlus', 'Motorola'],
  -- detector parameters (the batch detectors read them in a later step)
  deauth_min_bursts    integer NOT NULL DEFAULT 3,
  deauth_window_s      integer NOT NULL DEFAULT 600,
  evil_twin_k          real NOT NULL DEFAULT 6,
  evil_twin_min_db     real NOT NULL DEFAULT 8,
  evil_twin_guard_s    integer NOT NULL DEFAULT 0,
  sensor_offline_min   integer NOT NULL DEFAULT 15,
  updated_at           timestamptz NOT NULL DEFAULT now());
INSERT INTO account_settings (account_id) VALUES (1) ON CONFLICT DO NOTHING;

-- Managed (operator's own, checked against a policy) and friendly (someone
-- else's, known and legitimate) networks. allowed_ouis hold truncated MACs
-- (trunc(): EC:58:EA:00:00:00); comparisons truncate both sides.
CREATE TABLE IF NOT EXISTS networks (
  id                bigserial PRIMARY KEY,
  account_id        integer NOT NULL REFERENCES accounts(id),
  ssid              text NOT NULL CHECK (ssid <> ''),
  kind              text NOT NULL CHECK (kind IN ('managed', 'friendly')),
  expected_security text CHECK (expected_security IN ('enterprise', 'psk_or_better', 'wpa3', 'open_allowed')),
  require_mfp       boolean NOT NULL DEFAULT false,
  allowed_bssids    macaddr[] NOT NULL DEFAULT '{}',
  allowed_ouis      macaddr[] NOT NULL DEFAULT '{}',
  note              text,
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (account_id, ssid),
  CHECK (kind = 'managed' OR (expected_security IS NULL AND NOT require_mfp)));

-- One physical AP = one BSSID in the account, seen by one or more sensors.
-- first_seen / last_seen and ap_sensors are derived (refresh_rollups());
-- classification is the MANUAL classification only (NULL = the rules decide,
-- ap_classification()), note the operator's - the refresh never touches them.
CREATE TABLE IF NOT EXISTS aps (
  id             bigserial PRIMARY KEY,
  account_id     integer NOT NULL REFERENCES accounts(id),
  bssid          macaddr NOT NULL,
  first_seen     timestamptz NOT NULL,        -- first observation by any of its sensors
  last_seen      timestamptz NOT NULL,
  classification text CHECK (classification IN ('managed', 'friendly', 'unknown', 'suspicious', 'ignored')),
  classified_at  timestamptz,
  note           text,
  note_at        timestamptz,
  UNIQUE (account_id, bssid));
CREATE TABLE IF NOT EXISTS ap_sensors (
  ap_id      bigint NOT NULL REFERENCES aps(id),
  sensor_id  integer NOT NULL,
  device_key text NOT NULL,
  PRIMARY KEY (sensor_id, device_key),
  FOREIGN KEY (sensor_id, device_key) REFERENCES devices (sensor_id, device_key));
CREATE INDEX IF NOT EXISTS ap_sensors_ap ON ap_sensors (ap_id);

-- Documented causes and changes of a sensor (wifi-sensor/docs/findings.md
-- sections 6 and 7, detection/degraded_windows.csv): the cause text of the
-- outage table and of sensor events, the data boundaries of the
-- configuration history. Seeded by ops/seed_sensor_history.sql.
CREATE TABLE IF NOT EXISTS sensor_annotations (
  sensor_id integer NOT NULL REFERENCES sensors(id),
  starts_at timestamptz NOT NULL,
  ends_at   timestamptz,                       -- NULL = a point in time (a restart, a change)
  kind      text NOT NULL CHECK (kind IN ('gap', 'no_frames', 'coverage', 'restart', 'change')),
  boundary  boolean NOT NULL DEFAULT false,    -- a data boundary (a channel-plan change)
  label     text NOT NULL,
  ref       text,                              -- e.g. 'findings.md section 6'
  PRIMARY KEY (sensor_id, starts_at, kind));

-- Capture configuration of a sensor; the newest version is the desired one.
-- confirmed_at: the sensor reported it applied it (ingest, later); for rows
-- seeded from findings section 7 the effective time. channels NULL when only
-- the length of a historical list is known.
CREATE TABLE IF NOT EXISTS sensor_config (
  sensor_id    integer NOT NULL REFERENCES sensors(id),
  version      integer NOT NULL,
  channel_plan text NOT NULL CHECK (channel_plan IN ('auto', 'all_89', 'ht20_37', 'no_empty_dfs_59', 'locked', 'custom')),
  channels     text[],                         -- explicit Kismet hop entries
  n_channels   integer NOT NULL,
  dwell_s      real NOT NULL,
  poll_s       integer NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  created_by   text NOT NULL DEFAULT 'operator',   -- 'seed' for rows taken from findings section 7
  confirmed_at timestamptz,
  note         text,
  PRIMARY KEY (sensor_id, version));

-- One row per snapshot import (and, later, per ingest batch).
CREATE TABLE IF NOT EXISTS sensor_uploads (
  id             bigserial PRIMARY KEY,
  sensor_id      integer NOT NULL REFERENCES sensors(id),
  received_at    timestamptz NOT NULL DEFAULT now(),
  via            text NOT NULL CHECK (via IN ('snapshot', 'ingest')),
  buffer_version text,
  polls_from     timestamptz,
  polls_to       timestamptz,
  buffer_bytes   bigint,                       -- size of the sensor's buffer (export_snapshot.py meta)
  rows           jsonb NOT NULL DEFAULT '{}'); -- rows inserted per table
CREATE INDEX IF NOT EXISTS sensor_uploads_sensor ON sensor_uploads (sensor_id, received_at);

-- Which Kismet alerts become events, and severity overrides (NULL = Kismet's
-- own: kismet_severity()). SOURCEERROR / SOURCEOPEN are sensor health (the
-- capture-crash count, sensor_periods), not events.
CREATE TABLE IF NOT EXISTS kismet_alert_rules (
  header        text PRIMARY KEY,
  as_event      boolean NOT NULL DEFAULT true,
  severity      text CHECK (severity IN ('info', 'low', 'medium', 'high', 'critical')),
  episode_gap_s integer NOT NULL DEFAULT 3600, -- alerts of one header on one AP closer than this form one event
  label         text);
INSERT INTO kismet_alert_rules (header, as_event, severity, label) VALUES
  ('SOURCEERROR',       false, NULL,  'Capture source error'),
  ('SOURCEOPEN',        false, NULL,  'Capture source opened'),
  ('DEAUTHFLOOD',       true,  NULL,  'Deauthentication flood (Kismet)'),
  ('BCASTDISCON',       true,  NULL,  'Broadcast deauthentication or disassociation'),
  ('NOCLIENTMFP',       true,  NULL,  'Client without MFP on a network with MFP'),
  ('BEACONRATE',        true,  'low', 'Beacon rate changed'),
  ('DISCONCODEINVALID', true,  'low', 'Invalid disconnection reason code'),
  ('ADVCRYPTCHANGE',    true,  NULL,  'Advertised encryption changed (Kismet)'),
  ('CRYPTODROP',        true,  NULL,  'Encryption downgrade (Kismet)')
  ON CONFLICT DO NOTHING;

-- Every write endpoint of the api appends one row (thesis traceability of the
-- operator's labels and settings).
CREATE TABLE IF NOT EXISTS audit_log (
  id         bigserial PRIMARY KEY,
  at         timestamptz NOT NULL DEFAULT now(),
  account_id integer NOT NULL REFERENCES accounts(id),
  action     text NOT NULL,                  -- e.g. 'event.status', 'ap.classification'
  target     text,
  old        jsonb,
  new        jsonb);

-- --- helpers ------------------------------------------------------------------------------

-- RSSI floors of the capture adapter (RTL8821CU / rtw88_8821cu): -106 dBm is the
-- smallest value its CCK power estimate can produce (lna_gain_table_1: -44 - 2*31;
-- every 2.4 GHz beacon is CCK) and -120 the OFDM clamp (max(PWDB - 110, -120)).
-- A floor means "at or below the floor", i.e. a censored reading, never a level:
-- statistics use rssi_valid(), and the share of floors per AP (n_floor) gates
-- eligibility. Raw rows are never rewritten. The same list is
-- wifi-sensor/collector/dataset_rules.py RSSI_FLOOR_DBM (a collector test keeps
-- them equal); a different capture adapter needs its floors re-derived.
CREATE OR REPLACE FUNCTION rssi_valid(r smallint) RETURNS smallint
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT CASE WHEN r IN (-106, -120) THEN NULL ELSE r END
$$;

-- A floor reading: a raw floor value (rows from buffers before v5) or the v5 flag.
CREATE OR REPLACE FUNCTION rssi_is_floor(r smallint, flag boolean) RETURNS boolean
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT coalesce(flag, false) OR coalesce(r IN (-106, -120), false)
$$;

-- Band of an AP: from its advertised channel number (1-14: 2.4 GHz, 32-177:
-- 5 GHz; the RTL8821CU captures no 6 GHz, whose channel numbers would
-- overlap), else from the frequency with the most packets in
-- device_freq_hist (Kismet's lifetime map). observations.freq_khz was never
-- the AP's channel (findings section 11) and is no longer stored.
CREATE OR REPLACE FUNCTION band_of_channel(ch text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT CASE WHEN ch ~ '^[0-9]{1,3}$' THEN
           CASE WHEN ch::int BETWEEN 1 AND 14 THEN '2.4'
                WHEN ch::int BETWEEN 32 AND 177 THEN '5' END END
$$;

CREATE OR REPLACE FUNCTION band_of_freq(khz integer) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT CASE WHEN khz IS NULL OR khz <= 0 THEN NULL
              WHEN khz < 3000000 THEN '2.4'
              WHEN khz < 5925000 THEN '5' ELSE '6' END
$$;

CREATE OR REPLACE FUNCTION ap_band(p_sensor_id integer, p_device_key text) RETURNS text
LANGUAGE sql STABLE AS $$
  SELECT coalesce(
    (SELECT band_of_channel(adv_channel) FROM devices
     WHERE sensor_id = p_sensor_id AND device_key = p_device_key),
    (SELECT band_of_freq(freq_khz) FROM device_freq_hist
     WHERE sensor_id = p_sensor_id AND device_key = p_device_key AND packets > 0
     ORDER BY packets DESC, freq_khz LIMIT 1))
$$;

-- The advertised channel of an AP at p_ts: the adv_channel of the earliest
-- device_config_history row after p_ts that knows it (that row holds the
-- configuration replaced at its ts, so it was valid at p_ts), else the
-- current one. Same reading as the adv_channel rows of ap_config_changes
-- (NULL = unknown, states without a beacon record skipped; fixture test
-- tests/sql/ap_channel_at_test.sql). NULL when no channel is known.
CREATE OR REPLACE FUNCTION ap_channel_at(p_sensor_id integer, p_device_key text, p_ts timestamptz)
RETURNS text
LANGUAGE sql STABLE PARALLEL SAFE AS $$
  SELECT coalesce(
    (SELECT h.adv_channel FROM device_config_history h
     WHERE h.sensor_id = p_sensor_id AND h.device_key = p_device_key AND h.ts > p_ts
       AND h.adv_channel IS NOT NULL
     ORDER BY h.ts LIMIT 1),
    (SELECT d.adv_channel FROM devices d
     WHERE d.sensor_id = p_sensor_id AND d.device_key = p_device_key))
$$;

-- Per-poll AP load samples of the session -> ap_load_hourly (schema 8). Reads
-- the temp table stage_load_samples (ts timestamptz, device_key text,
-- n_clients integer, qbss_stations integer, util_pct real) that the caller
-- filled - the importer from a snapshot's rows of buffers <= v5, the dump
-- backfill from the schema-6 observations - and aggregates it per AP, UTC hour
-- and advertised channel at the poll (the reading of ap_channel_at(), computed
-- set-based; tests/sql/ap_load_test.sql checks both agree), source 'v5_obs'.
-- Samples of devices that are not APs on the server, or unknown, are ignored;
-- a duplicate (device_key, ts) counts once, as in observations. A stored row
-- is replaced only by one with more polls, so re-loading the same or a
-- shorter window changes nothing. Returns the aggregated rows and how many
-- were inserted / updated.
CREATE OR REPLACE FUNCTION load_ap_load_samples(p_sensor_id integer)
RETURNS TABLE (hours integer, inserted integer, updated integer)
LANGUAGE plpgsql
SET work_mem = '64MB'      -- the backfill sorts ~2.5M samples
SET jit = off
AS $$
#variable_conflict use_column
BEGIN
  IF NOT EXISTS (SELECT 1 FROM sensors WHERE id = p_sensor_id) THEN
    RAISE EXCEPTION 'load_ap_load_samples: unknown sensor_id %', p_sensor_id;
  END IF;
  RETURN QUERY
  WITH s AS (
    SELECT DISTINCT ON (x.device_key, x.ts)
           x.device_key, x.ts, x.n_clients, x.qbss_stations, x.util_pct
    FROM stage_load_samples x
    JOIN devices d ON d.sensor_id = p_sensor_id AND d.device_key = x.device_key AND d.type = 'ap'
    ORDER BY x.device_key, x.ts
  ), u AS (      -- the samples and the APs' known channels from the history, one stream
    SELECT s.device_key, s.ts, 0 AS kind, NULL::text AS h_channel,
           s.n_clients, s.qbss_stations, s.util_pct
    FROM s
    UNION ALL
    SELECT h.device_key, h.ts, 1, h.adv_channel, NULL, NULL, NULL
    FROM device_config_history h
    WHERE h.sensor_id = p_sensor_id AND h.adv_channel IS NOT NULL
      AND h.device_key IN (SELECT s.device_key FROM s)
  ), g AS (      -- grp = known history channels at or after the row; a history row sorts
                 -- before a sample of the same ts (its configuration ended there)
    SELECT u.*, count(u.h_channel) OVER (PARTITION BY u.device_key ORDER BY u.ts, u.kind DESC
                                         ROWS BETWEEN CURRENT ROW AND UNBOUNDED FOLLOWING) AS grp
    FROM u
  ), n AS (      -- the channel of the next history row, as ap_channel_at() reads it (set-based:
                 -- one lookup per sample costs ~0.3 ms on the e2-micro, measured 2026-10-11)
    SELECT g.*, max(g.h_channel) OVER (PARTITION BY g.device_key, g.grp) AS next_channel
    FROM g
  ), c AS (
    SELECT n.device_key, date_trunc('hour', n.ts, 'UTC') AS hour,
           coalesce(n.next_channel, d.adv_channel, '') AS channel,
           n.n_clients,
           coalesce(n.qbss_stations > 0 OR n.util_pct > 0, false) AS qbss,
           n.qbss_stations, n.util_pct
    FROM n
    JOIN devices d ON d.sensor_id = p_sensor_id AND d.device_key = n.device_key
    WHERE n.kind = 0
  ), a AS (
    SELECT c.device_key, c.hour, c.channel,
           count(*)::integer AS polls,
           count(c.n_clients)::integer AS clients_polls,
           max(c.n_clients) AS clients_max,
           sum(c.n_clients)::integer AS clients_sum,
           (count(*) FILTER (WHERE c.qbss))::integer AS qbss_polls,
           max(c.qbss_stations) FILTER (WHERE c.qbss) AS qbss_max,
           (sum(c.qbss_stations) FILTER (WHERE c.qbss))::integer AS qbss_sum,
           max(c.util_pct) FILTER (WHERE c.qbss) AS util_max,
           sum(c.util_pct) FILTER (WHERE c.qbss) AS util_sum
    FROM c
    GROUP BY 1, 2, 3
  ), up AS (
    INSERT INTO ap_load_hourly AS t (sensor_id, device_key, hour, channel, polls, clients_polls,
                                     clients_max, clients_sum, qbss_polls, qbss_max, qbss_sum,
                                     util_max, util_sum, source)
    SELECT p_sensor_id, a.device_key, a.hour, a.channel, a.polls, a.clients_polls,
           a.clients_max, a.clients_sum, a.qbss_polls, a.qbss_max, a.qbss_sum,
           a.util_max, a.util_sum, 'v5_obs'
    FROM a
    ON CONFLICT (sensor_id, device_key, hour, channel) DO UPDATE SET
        polls = EXCLUDED.polls, clients_polls = EXCLUDED.clients_polls,
        clients_max = EXCLUDED.clients_max, clients_sum = EXCLUDED.clients_sum,
        qbss_polls = EXCLUDED.qbss_polls, qbss_max = EXCLUDED.qbss_max,
        qbss_sum = EXCLUDED.qbss_sum, util_max = EXCLUDED.util_max,
        util_sum = EXCLUDED.util_sum, source = EXCLUDED.source
      WHERE EXCLUDED.polls > t.polls
    RETURNING (t.xmax = 0) AS is_insert
  )
  SELECT (SELECT count(*) FROM a)::integer,
         (count(*) FILTER (WHERE up.is_insert))::integer,
         (count(*) FILTER (WHERE NOT up.is_insert))::integer
  FROM up;
END
$$;

-- --- security of an AP (schema 9) -------------------------------------------------------
--
-- crypt_bits is Kismet's dot11.advertisedssid.crypt_bitfield: the 64-bit
-- dot11_crypt_* set of Kismet 2025-09-R1 (packet_ieee80211.h:216-289; the
-- field is registered on crypt_set, phy_80211_components.cc:273 - the legacy
-- 32-bit crypt_* set is another field and not stored). Bits used:
--   general   0 WEP, 1 WPA, 2 WPA1, 3 WPA2, 4 WPA3
--   group     7 WEP40, 8 WEP104, 9 TKIP, 10 CCMP, ... 17 BIP-CMAC-256
--   pairwise  23 WEP40, 24 WEP104, 25 TKIP, 26 CCMP, 28/29 GCMP, 30 CCMP-256, 34 OCB
--   AKM       37 802.1X, 38 PSK, 39 802.1X-FT, 40 PSK-FT, 41 802.1X-SHA256,
--             42 PSK-SHA256, 43 TDLS, 44 SAE, 45 SAE-FT, 46 AP-peer, 47/48
--             802.1X suite B, 49 802.1X-FT-SHA384, 50-53 FILS, 54 OWE,
--             55 PSK-FT-SHA384, 56 PSK-SHA384, 57 WPS
-- Kismet quirks: the OWE-transition vendor IE ORs the LEGACY constant
-- crypt_wpa_owe (1 << 17) into this set (phy_80211_dissectors.cc:2383), so
-- an open BSS in OWE transition shows bit 17 alone; the crypt string
-- (crypt_to_simple_string()) labels OWE '-SAE' and hides WPA1 behind WPA2.
-- Levels are read from the bits; the string only when the bits are unknown
-- (0 is Kismet's missing field unless the crypt string is 'Open', the same
-- rule as ap_config_changes). Mapping and live combinations:
-- server/docs/dashboard-v2/PLAN.md section 5. Fixture test:
-- tests/sql/security_level_test.sql.

-- true when any of the given bit positions is set
CREATE OR REPLACE FUNCTION crypt_has(p_bits bigint, VARIADIC p_positions integer[])
RETURNS boolean
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT (p_bits & (SELECT bit_or(1::bigint << b) FROM unnest(p_positions) AS b)) <> 0
$$;

-- The kind of security an AP offers, "weakest offered" first: open, wep,
-- wpa1_mixed (WPA1 + WPA2), wpa1, tkip (TKIP as group or pairwise cipher),
-- transition (PSK + SAE), psk, enterprise3 (802.1X suite B / FT-SHA384),
-- enterprise, sae, owe, unknown.
CREATE OR REPLACE FUNCTION crypt_kind(p_crypt text, p_bits bigint)
RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT CASE
    WHEN p_bits IS NULL OR (p_bits = 0 AND p_crypt IS DISTINCT FROM 'Open') THEN
      CASE WHEN p_crypt = 'Open' THEN 'open'
           WHEN p_crypt ~ 'WEP' THEN 'wep'
           WHEN p_crypt ~ 'WPA1' THEN 'wpa1'
           WHEN p_crypt ~ 'TKIP' THEN 'tkip'
           WHEN p_crypt ~ '-PSK' AND p_crypt ~ '-SAE' THEN 'transition'
           WHEN p_crypt ~ '-PSK' THEN 'psk'
           WHEN p_crypt ~ '-EAP' THEN 'enterprise'
           WHEN p_crypt ~ '-SAE' THEN 'sae'
           ELSE 'unknown' END
    WHEN NOT crypt_has(p_bits, VARIADIC ARRAY[0, 1, 2, 3, 4, 7, 8, 23, 24]
                                        || ARRAY(SELECT generate_series(37, 57))) THEN 'open'
    WHEN crypt_has(p_bits, 0, 7, 8, 23, 24) THEN 'wep'
    WHEN crypt_has(p_bits, 2) AND crypt_has(p_bits, 3) THEN 'wpa1_mixed'
    WHEN crypt_has(p_bits, 2) THEN 'wpa1'
    WHEN crypt_has(p_bits, 9, 25) THEN 'tkip'
    WHEN crypt_has(p_bits, 38, 40, 42, 55, 56) AND crypt_has(p_bits, 44, 45) THEN 'transition'
    WHEN crypt_has(p_bits, 38, 40, 42, 55, 56) THEN 'psk'
    WHEN crypt_has(p_bits, 47, 48, 49) THEN 'enterprise3'
    WHEN crypt_has(p_bits, 37, 39, 41, 50, 51, 52, 53) THEN 'enterprise'
    WHEN crypt_has(p_bits, 44, 45) THEN 'sae'
    WHEN crypt_has(p_bits, 54) THEN 'owe'
    ELSE 'unknown' END
$$;

-- open | legacy (WEP, WPA1, TKIP offered) | psk_no_mfp (any PSK AKM, incl.
-- WPA2/WPA3 transition, without MFP required) | strong (802.1X, SAE, OWE, or
-- PSK with MFP required) | unknown. mfp_sup is part of the signature (SPEC);
-- the level depends on mfp_req only.
CREATE OR REPLACE FUNCTION security_level(p_crypt text, p_bits bigint, p_mfp_sup boolean, p_mfp_req boolean)
RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT CASE k
    WHEN 'open' THEN 'open'
    WHEN 'wep' THEN 'legacy' WHEN 'wpa1_mixed' THEN 'legacy' WHEN 'wpa1' THEN 'legacy' WHEN 'tkip' THEN 'legacy'
    WHEN 'psk' THEN CASE WHEN p_mfp_req THEN 'strong' ELSE 'psk_no_mfp' END
    WHEN 'transition' THEN CASE WHEN p_mfp_req THEN 'strong' ELSE 'psk_no_mfp' END
    WHEN 'enterprise3' THEN 'strong' WHEN 'enterprise' THEN 'strong' WHEN 'sae' THEN 'strong' WHEN 'owe' THEN 'strong'
    ELSE 'unknown' END
  FROM (SELECT crypt_kind(p_crypt, p_bits) AS k) x
$$;

-- The name of the configuration in tables ("WPA2/WPA3 transition" stays apart
-- from plain "WPA2-PSK").
CREATE OR REPLACE FUNCTION security_label(p_crypt text, p_bits bigint)
RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT CASE crypt_kind(p_crypt, p_bits)
    WHEN 'open' THEN 'Open' WHEN 'wep' THEN 'WEP' WHEN 'wpa1_mixed' THEN 'WPA1/WPA2 mixed'
    WHEN 'wpa1' THEN 'WPA1' WHEN 'tkip' THEN 'WPA2 (TKIP)' WHEN 'transition' THEN 'WPA2/WPA3 transition'
    WHEN 'psk' THEN 'WPA2-PSK' WHEN 'enterprise3' THEN 'WPA3-Enterprise' WHEN 'enterprise' THEN 'WPA2-Enterprise'
    WHEN 'sae' THEN 'WPA3-SAE' WHEN 'owe' THEN 'OWE' ELSE 'Unknown' END
$$;

-- required | capable | no; NULL when neither flag is known
CREATE OR REPLACE FUNCTION mfp_state(p_mfp_sup boolean, p_mfp_req boolean)
RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT CASE WHEN p_mfp_req THEN 'required' WHEN p_mfp_sup THEN 'capable'
              WHEN p_mfp_sup IS NOT NULL OR p_mfp_req IS NOT NULL THEN 'no' END
$$;

-- open 0 < legacy 1 < psk_no_mfp 2 < strong 3 (unknown: NULL)
CREATE OR REPLACE FUNCTION security_rank(p_level text)
RETURNS integer
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT CASE p_level WHEN 'open' THEN 0 WHEN 'legacy' THEN 1 WHEN 'psk_no_mfp' THEN 2 WHEN 'strong' THEN 3 END
$$;

-- The set bits by name: {"general": [...], "group": [...], "pairwise": [...],
-- "akm": [...], "eap": [...]}; NULL for an unknown (NULL / 0) bitfield.
CREATE OR REPLACE FUNCTION security_detail(p_bits bigint)
RETURNS jsonb
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT CASE WHEN p_bits IS NULL OR p_bits = 0 THEN NULL ELSE (
    SELECT jsonb_object_agg(g.grp, g.names)
    FROM (SELECT b.grp, jsonb_agg(b.name ORDER BY b.pos) AS names
          FROM (VALUES
            (0, 'general', 'WEP'), (1, 'general', 'WPA'), (2, 'general', 'WPA1'), (3, 'general', 'WPA2'),
            (4, 'general', 'WPA3'),
            (7, 'group', 'WEP40'), (8, 'group', 'WEP104'), (9, 'group', 'TKIP'), (10, 'group', 'CCMP'),
            (11, 'group', 'BIP-CMAC-128'), (12, 'group', 'GCMP-128'), (13, 'group', 'GCMP-256'),
            (14, 'group', 'CCMP-256'), (15, 'group', 'BIP-GMAC-128'), (16, 'group', 'BIP-GMAC-256'),
            (17, 'group', 'BIP-CMAC-256'), (18, 'group', 'OCB'),
            (23, 'pairwise', 'WEP40'), (24, 'pairwise', 'WEP104'), (25, 'pairwise', 'TKIP'),
            (26, 'pairwise', 'CCMP'), (27, 'pairwise', 'BIP-CMAC-128'), (28, 'pairwise', 'GCMP-128'),
            (29, 'pairwise', 'GCMP-256'), (30, 'pairwise', 'CCMP-256'), (31, 'pairwise', 'BIP-GMAC-128'),
            (32, 'pairwise', 'BIP-GMAC-256'), (33, 'pairwise', 'BIP-CMAC-256'), (34, 'pairwise', 'OCB'),
            (37, 'akm', '802.1X'), (38, 'akm', 'PSK'), (39, 'akm', '802.1X-FT'), (40, 'akm', 'PSK-FT'),
            (41, 'akm', '802.1X-SHA256'), (42, 'akm', 'PSK-SHA256'), (43, 'akm', 'TDLS'), (44, 'akm', 'SAE'),
            (45, 'akm', 'SAE-FT'), (46, 'akm', 'AP-PEER'), (47, 'akm', '802.1X-SUITE-B'),
            (48, 'akm', '802.1X-SUITE-B-192'), (49, 'akm', '802.1X-FT-SHA384'), (50, 'akm', 'FILS-SHA256'),
            (51, 'akm', 'FILS-SHA384'), (52, 'akm', 'FILS-SHA256-FT'), (53, 'akm', 'FILS-SHA384-FT'),
            (54, 'akm', 'OWE'), (55, 'akm', 'PSK-FT-SHA384'), (56, 'akm', 'PSK-SHA384'), (57, 'akm', 'WPS'),
            (58, 'eap', 'LEAP'), (59, 'eap', 'TLS'), (60, 'eap', 'TTLS'), (61, 'eap', 'PEAP'),
            (62, 'eap', 'IDENTITY')) AS b(pos, grp, name)
          WHERE (p_bits & (1::bigint << b.pos)) <> 0
          GROUP BY b.grp) g) END
$$;

-- Does an AP meet a managed network's policy? NULL = met, else the first
-- reason. enterprise: an 802.1X AKM and no PSK; psk_or_better: PSK (also
-- transition), 802.1X or SAE (OWE has no authentication); wpa3: SAE without
-- PSK, or 802.1X with MFP required; open_allowed: anything. require_mfp:
-- MFP required (not applicable to an open AP). Unknown security never meets
-- a policy other than open_allowed.
CREATE OR REPLACE FUNCTION security_meets(p_expected text, p_require_mfp boolean, p_crypt text,
                                          p_bits bigint, p_mfp_req boolean)
RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT CASE
    WHEN p_expected IS NULL OR p_expected = 'open_allowed' THEN
      CASE WHEN p_require_mfp AND x.k NOT IN ('open', 'unknown') AND NOT coalesce(p_mfp_req, false)
           THEN 'MFP not required' END
    WHEN x.k = 'unknown' THEN 'security unknown'
    WHEN p_expected = 'enterprise' AND x.k NOT IN ('enterprise', 'enterprise3') THEN 'not Enterprise'
    WHEN p_expected = 'psk_or_better' AND x.k NOT IN ('psk', 'transition', 'enterprise', 'enterprise3', 'sae')
      THEN 'weaker than WPA2-PSK'
    WHEN p_expected = 'wpa3'
         AND NOT (x.k = 'sae' OR (x.k IN ('enterprise', 'enterprise3') AND coalesce(p_mfp_req, false)))
      THEN 'not WPA3'
    WHEN p_require_mfp AND NOT coalesce(p_mfp_req, false) THEN 'MFP not required'
  END
  FROM (SELECT crypt_kind(p_crypt, p_bits) AS k) x
$$;

-- Kismet's alert severity scale (alertracker.h, kis_alert_severity: info 0,
-- low 5, medium 10, high 15, critical 20) as the event severity.
CREATE OR REPLACE FUNCTION kismet_severity(p_severity smallint)
RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT CASE WHEN p_severity >= 20 THEN 'critical' WHEN p_severity >= 15 THEN 'high'
              WHEN p_severity >= 10 THEN 'medium' WHEN p_severity >= 5 THEN 'low' ELSE 'info' END
$$;

-- --- classification of an account AP (schema 9) ------------------------------------------
--
-- Effective classification: the manual one if set, else the rules of
-- account_settings, in this order: (1) the AP's network name (SSID, or the
-- name seen for a hidden AP) is a MANAGED network: BSSID in allowed_bssids ->
-- managed; OUI in allowed_ouis -> managed if managed_by_vendor, else unknown;
-- neither -> suspicious if suspicious_unallowed and the network has an
-- allowed list at all, else unknown; (2) a FRIENDLY network -> friendly;
-- (3) a randomized BSSID and ignore_random_bssid -> ignored; (4) typical RSSI
-- below ignore_weaker_dbm -> ignored; (5) unknown. Rule 1 comes before 3 on
-- purpose: a randomized BSSID broadcasting a managed SSID is suspicious, not
-- ignored. Fixture test: tests/sql/classification_test.sql.
CREATE OR REPLACE FUNCTION ap_classification(p_account integer, p_manual text, p_network text,
                                             p_bssid macaddr, p_random boolean, p_rssi_typical real)
RETURNS text
LANGUAGE sql STABLE AS $$
  SELECT coalesce(
    p_manual,
    (SELECT CASE
       WHEN n.kind = 'managed' THEN
         CASE WHEN p_bssid = ANY (n.allowed_bssids) THEN 'managed'
              WHEN trunc(p_bssid) IN (SELECT trunc(o) FROM unnest(n.allowed_ouis) AS o) THEN
                CASE WHEN st.managed_by_vendor THEN 'managed' ELSE 'unknown' END
              WHEN st.suspicious_unallowed
                   AND (cardinality(n.allowed_bssids) > 0 OR cardinality(n.allowed_ouis) > 0) THEN 'suspicious'
              ELSE 'unknown' END
       ELSE 'friendly' END
     FROM networks n
     JOIN account_settings st ON st.account_id = n.account_id
     WHERE n.account_id = p_account AND n.ssid = p_network),
    (SELECT CASE WHEN p_random AND st.ignore_random_bssid THEN 'ignored'
                 WHEN p_rssi_typical < st.ignore_weaker_dbm THEN 'ignored' END
     FROM account_settings st WHERE st.account_id = p_account),
    'unknown')
$$;

-- Mobile AP (SPEC section 4): randomized BSSID, a vendor of the account's
-- mobile list (case-insensitive prefix of devices.manuf), or a lifetime under
-- 2 h once it has ended (not seen for an hour before p_latest, the account's
-- latest collecting poll).
CREATE OR REPLACE FUNCTION ap_is_mobile(p_account integer, p_random boolean, p_manuf text,
                                        p_first timestamptz, p_last timestamptz, p_latest timestamptz)
RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT coalesce(p_random, false)
      OR EXISTS (SELECT 1 FROM account_settings st, unnest(st.mobile_vendors) AS v
                 WHERE st.account_id = p_account AND p_manuf ILIKE v || '%')
      OR coalesce(p_last - p_first < interval '2 hours' AND p_last < p_latest - interval '1 hour', false)
$$;

-- Recompute the RSSI baselines of one sensor from its observations.
-- Defaults match analysis/rssi_stability.py: an AP qualifies with >= 200
-- valid readings on >= 2 distinct days. Floor readings are excluded from every
-- statistic and counted in n_floor (floor share = n_floor / (n_obs + n_floor));
-- baselines of APs that no longer qualify are cleared (statistics NULL). p_from / p_until restrict the readings used
-- (NULL = unbounded) so a baseline can be fitted on an earlier window.
-- A trusted row keeps its ssid/crypt (the operator-approved identity); only
-- the statistics are refreshed. Returns the number of rows upserted.
CREATE OR REPLACE FUNCTION refresh_ap_baselines(
    p_sensor_id integer,
    p_min_obs   integer     DEFAULT 200,
    p_min_days  integer     DEFAULT 2,
    p_from      timestamptz DEFAULT NULL,
    p_until     timestamptz DEFAULT NULL)
RETURNS integer
LANGUAGE plpgsql AS $$
DECLARE
  v_tz text;
  v_n  integer;
BEGIN
  SELECT tz INTO v_tz FROM sensors WHERE id = p_sensor_id;
  IF v_tz IS NULL THEN
    RAISE EXCEPTION 'refresh_ap_baselines: unknown sensor_id %', p_sensor_id;
  END IF;

  WITH obs AS (
    SELECT o.device_key, o.ts, rssi_valid(o.rssi) AS rssi,
           rssi_is_floor(o.rssi, o.rssi_floor) AS is_floor,
           (o.ts AT TIME ZONE v_tz)::date AS obs_day
    FROM observations o
    JOIN devices d ON d.sensor_id = o.sensor_id AND d.device_key = o.device_key
    WHERE o.sensor_id = p_sensor_id
      AND d.type = 'ap'
      AND (o.rssi IS NOT NULL OR o.rssi_floor)
      AND (p_from  IS NULL OR o.ts >= p_from)
      AND (p_until IS NULL OR o.ts <  p_until)
  ), stats AS (
    -- statistics over valid readings only; floors are counted, never averaged
    SELECT device_key,
           count(rssi)::integer                                  AS n_obs,
           count(*) FILTER (WHERE is_floor)::integer             AS n_floor,
           count(DISTINCT obs_day) FILTER (WHERE rssi IS NOT NULL)::integer AS n_days,
           percentile_cont(0.5)  WITHIN GROUP (ORDER BY rssi)    AS med,
           avg(rssi)                                             AS mean,
           stddev_pop(rssi)                                      AS sd,
           percentile_cont(0.05) WITHIN GROUP (ORDER BY rssi)    AS p5,
           percentile_cont(0.95) WITHIN GROUP (ORDER BY rssi)    AS p95,
           min(ts) FILTER (WHERE rssi IS NOT NULL)               AS w_start,
           max(ts) FILTER (WHERE rssi IS NOT NULL)               AS w_end
    FROM obs
    GROUP BY device_key
    HAVING count(rssi) >= p_min_obs
       AND count(DISTINCT obs_day) FILTER (WHERE rssi IS NOT NULL) >= p_min_days
  ), mad AS (
    SELECT o.device_key,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY abs(o.rssi - s.med)) AS mad
    FROM obs o
    JOIN stats s ON s.device_key = o.device_key
    WHERE o.rssi IS NOT NULL
    GROUP BY o.device_key
  )
  INSERT INTO ap_baselines (
      sensor_id, device_key, bssid, ssid, crypt,
      rssi_median, rssi_mean, rssi_sd, rssi_robust_sd, rssi_p5, rssi_p95,
      band, n_obs, n_floor, n_days, window_start, window_end, computed_at)
  SELECT p_sensor_id, s.device_key, d.mac, d.ssid, d.crypt,
         s.med, s.mean, s.sd, 1.4826 * m.mad,
         round(s.p5)::smallint, round(s.p95)::smallint,
         ap_band(p_sensor_id, s.device_key), s.n_obs, s.n_floor, s.n_days, s.w_start, s.w_end, now()
  FROM stats s
  JOIN mad m ON m.device_key = s.device_key
  JOIN devices d ON d.sensor_id = p_sensor_id AND d.device_key = s.device_key
  ON CONFLICT (sensor_id, device_key) DO UPDATE SET
      bssid = EXCLUDED.bssid,
      ssid  = CASE WHEN ap_baselines.trusted THEN ap_baselines.ssid  ELSE EXCLUDED.ssid  END,
      crypt = CASE WHEN ap_baselines.trusted THEN ap_baselines.crypt ELSE EXCLUDED.crypt END,
      rssi_median    = EXCLUDED.rssi_median,
      rssi_mean      = EXCLUDED.rssi_mean,
      rssi_sd        = EXCLUDED.rssi_sd,
      rssi_robust_sd = EXCLUDED.rssi_robust_sd,
      rssi_p5        = EXCLUDED.rssi_p5,
      rssi_p95       = EXCLUDED.rssi_p95,
      band           = EXCLUDED.band,
      n_obs          = EXCLUDED.n_obs,
      n_floor        = EXCLUDED.n_floor,
      n_days         = EXCLUDED.n_days,
      window_start   = EXCLUDED.window_start,
      window_end     = EXCLUDED.window_end,
      computed_at    = EXCLUDED.computed_at;
  GET DIAGNOSTICS v_n = ROW_COUNT;

  -- Schema 4: a baseline row this call did not refresh (the AP no longer
  -- qualifies on valid readings in the window) loses its statistics, so no
  -- consumer keeps using a stale or floor-dominated baseline. The row itself,
  -- with trusted/note, stays.
  UPDATE ap_baselines SET
      rssi_median = NULL, rssi_mean = NULL, rssi_sd = NULL, rssi_robust_sd = NULL,
      rssi_p5 = NULL, rssi_p95 = NULL, band = NULL,
      n_obs = NULL, n_floor = NULL, n_days = NULL, window_start = NULL, window_end = NULL,
      computed_at = now()
  WHERE sensor_id = p_sensor_id AND computed_at < now();

  RETURN v_n;
END
$$;

-- AP inventory with the derived columns the dashboard (and later detection)
-- need, defined once: OUI, randomised (locally administered) BSSID, hidden
-- SSID, lifetime, plus the baseline if one exists.
CREATE OR REPLACE VIEW ap_inventory AS
SELECT d.sensor_id,
       d.device_key,
       d.mac                                   AS bssid,
       trunc(d.mac)                            AS oui,
       (d.mac & macaddr '02:00:00:00:00:00') <> macaddr '00:00:00:00:00:00' AS random_bssid,
       d.manuf,
       d.ssid,
       (d.ssid IS NULL OR d.ssid = '')         AS hidden,
       d.cloaked, d.crypt, d.crypt_bits, d.mfp_sup, d.mfp_req,
       d.adv_channel, d.ht_mode, d.beacon_rate, d.country,
       d.first_seen,
       d.last_seen,
       d.last_seen - d.first_seen              AS lifetime,
       d.config_changed_at,
       b.trusted,
       b.rssi_median, b.rssi_robust_sd, b.rssi_sd,
       b.n_obs                                 AS baseline_n_obs,
       b.n_days                                AS baseline_n_days,
       b.computed_at                           AS baseline_at,
       b.n_floor                               AS baseline_n_floor,
       b.n_floor::real / nullif(b.n_obs + b.n_floor, 0) AS baseline_floor_share
FROM devices d
LEFT JOIN ap_baselines b ON b.sensor_id = d.sensor_id AND b.device_key = d.device_key
WHERE d.type = 'ap';

-- Changes of an AP's advertised configuration, one row per changed field - the
-- one reading of the configuration timeline (docs/findings.md section 11).
-- device_config_history holds the REPLACED configuration at the poll ts where
-- the new one was first seen, devices the current one; neither is rewritten,
-- every column stays (a crypt/MFP/country/beacon-rate change is a security
-- signal). Rules, in the spirit of rssi_valid():
--   1. a state without a beacon record (crypt and adv_channel both NULL) says
--      nothing about any field and is skipped;
--   2. per field, NULL = unknown and is skipped: before collector buffer v5
--      (2026-09-27 19:57:29 UTC) an incomplete beacon record (channel, HT mode,
--      beacon rate and country missing) wrote a row on almost every poll,
--      A -> incomplete -> A;
--   3. crypt_bits 0 is Kismet's "field missing" unless the crypt string is
--      'Open' (an open AP's real bitfield is 0), so a WPA2 -> Open downgrade
--      still shows, in crypt and in crypt_bits;
--   4. a hidden AP's cloaked beacon (ssid '') carries no name, and its cloaked
--      flag is unknown there: hidden APs alternate between that record and a
--      named, uncloaked one, which is one state, not a change;
--   5. a change is a known value followed by a different known value, dated at
--      the ts where the old value was replaced.
-- Values are text (booleans 'true'/'false', crypt_bits decimal). Fixture test:
-- tests/sql/ap_config_changes_test.sql.
--
-- MATERIALIZED since schema 6 (as a plain view it read every history row of
-- an AP on each AP page: 85 ms warm for the busiest AP, 4.7 s for all APs);
-- refresh_rollups() refreshes it after every import - history only changes
-- there.
CREATE MATERIALIZED VIEW IF NOT EXISTS ap_config_changes AS
WITH st AS (
  SELECT sensor_id, device_key, ts, ssid, cloaked, crypt, crypt_bits, mfp_sup, mfp_req,
         adv_channel, ht_mode, beacon_rate, country
  FROM device_config_history
  UNION ALL
  SELECT sensor_id, device_key, NULL::timestamptz, ssid, cloaked, crypt, crypt_bits, mfp_sup, mfp_req,
         adv_channel, ht_mode, beacon_rate, country
  FROM devices
), known AS (
  SELECT st.sensor_id, st.device_key, st.ts, f.field, f.val
  FROM st CROSS JOIN LATERAL (VALUES
    ('ssid',        nullif(st.ssid, '')),
    ('cloaked',     CASE WHEN st.ssid = '' THEN NULL ELSE st.cloaked::text END),
    ('crypt',       st.crypt),
    ('crypt_bits',  CASE WHEN st.crypt_bits = 0 AND st.crypt IS DISTINCT FROM 'Open' THEN NULL
                         ELSE st.crypt_bits::text END),
    ('mfp_sup',     st.mfp_sup::text),
    ('mfp_req',     st.mfp_req::text),
    ('adv_channel', st.adv_channel),
    ('ht_mode',     st.ht_mode),
    ('beacon_rate', st.beacon_rate::text),
    ('country',     st.country)) AS f(field, val)
  WHERE f.val IS NOT NULL AND NOT (st.crypt IS NULL AND st.adv_channel IS NULL)
), seq AS (
  SELECT k.sensor_id, k.device_key, k.ts, k.field, k.val,
         lead(k.val) OVER (PARTITION BY k.sensor_id, k.device_key, k.field
                           ORDER BY k.ts NULLS LAST) AS new_val
  FROM known k
)
SELECT sensor_id, device_key, ts, field, val AS old_value, new_val AS new_value
FROM seq
WHERE ts IS NOT NULL AND new_val IS NOT NULL AND new_val <> val
WITH DATA;
-- One row per (AP, ts, field): the unique index serves the per-AP lookups
-- (sensor_id, device_key, ts) and REFRESH ... CONCURRENTLY.
CREATE UNIQUE INDEX IF NOT EXISTS ap_config_changes_key
  ON ap_config_changes (sensor_id, device_key, ts, field);
COMMENT ON MATERIALIZED VIEW ap_config_changes IS
  'Changes of an AP''s advertised configuration, one row per changed field (old -> new), with the pre-v5 incomplete-record churn, Kismet''s missing-field crypt_bits 0 and the hidden-beacon/named-record alternation read as unknown. History is not rewritten. Materialized (schema 6), refreshed by refresh_rollups().';

-- Changes of an AP's advertised channel: the adv_channel rows of
-- ap_config_changes (same rows as its schema-4 definition: 11,388 on 138 APs,
-- compared 2026-09-28). Used by the evil_twin channel guard and fp-audit.
-- A plain view on the materialized one: as current as its last refresh.
CREATE OR REPLACE VIEW ap_channel_changes AS
SELECT sensor_id, device_key, ts, old_value AS from_channel, new_value AS to_channel
FROM ap_config_changes
WHERE field = 'adv_channel';

-- --- dashboard read tables (schema 6) ------------------------------------------------
--
-- Small tables the dashboard reads instead of the raw time series. Measured
-- before them (2026-09-29, tests/perf/measure.sh): /api/aps read 580 APs
-- scattered over 108k devices rows, the AP page recomputed the usual country
-- over all APs and read the AP's whole history, the overview ran two window
-- scans over every poll. Derived data only: written by refresh_rollups() and
-- nothing else, so together they are one snapshot of the source tables as of
-- sensor_summary.refreshed_at, from which the api derives its ETag. RSSI goes
-- through rssi_valid() / rssi_is_floor() as everywhere (floors are counted,
-- never a level). Hours are UTC hours (date_trunc('hour', ts, 'UTC')).

CREATE TABLE IF NOT EXISTS ap_rssi_hourly (
  sensor_id  integer NOT NULL,
  device_key text NOT NULL,
  hour       timestamptz NOT NULL,           -- start of the UTC hour
  n_obs      integer NOT NULL,               -- observation rows of the AP in the hour
  n_valid    integer NOT NULL,               -- readings with rssi_valid() not NULL
  n_floor    integer NOT NULL,               -- floor readings, rssi_is_floor() (censored)
  median     real,                           -- percentiles and extremes of the valid
  p10        real,                           --   readings only; NULL when n_valid = 0
  p90        real,
  min        smallint,
  max        smallint,
  PRIMARY KEY (sensor_id, device_key, hour),
  FOREIGN KEY (sensor_id, device_key) REFERENCES devices (sensor_id, device_key));
-- The dashboard's period cards (schema 9) count distinct APs per bucket of
-- hours across all APs: index-only by hour.
CREATE INDEX IF NOT EXISTS ap_rssi_hourly_hour
  ON ap_rssi_hourly (sensor_id, hour) INCLUDE (device_key, n_obs, n_valid, median);
COMMENT ON TABLE ap_rssi_hourly IS
  'Per AP and UTC hour: observation count and the valid-RSSI distribution (floor readings counted in n_floor, never in the statistics). Filled by refresh_rollups().';

CREATE TABLE IF NOT EXISTS sensor_hourly (
  sensor_id       integer NOT NULL REFERENCES sensors(id),
  hour            timestamptz NOT NULL,      -- start of the UTC hour
  polls           integer NOT NULL,          -- collector polls in the hour
  polls_ok        integer NOT NULL,
  hop_ok_polls    integer NOT NULL,          -- polls with ds_hop_ok = true (full channel coverage)
  hop_known_polls integer NOT NULL,          -- polls with ds_hop_ok not NULL (buffer v4+)
  gap_s           real NOT NULL,             -- seconds of the hour inside a poll gap > 90 s
  active_aps      integer NOT NULL,          -- APs with an observation in the hour
  new_aps         integer NOT NULL,          -- APs first observed in the hour
  alerts          integer NOT NULL,          -- Kismet alerts with ts in the hour
  ap_obs          integer NOT NULL,          -- AP observation rows in the hour
  -- schema 9: a collecting poll = ok AND datasource running AND devices > 0
  -- (ok alone counted the 2026-10-06 adapter loss as successful)
  capture_polls   integer NOT NULL DEFAULT 0,
  limited_polls   integer NOT NULL DEFAULT 0,  -- collecting with ds_hop_ok = false
  PRIMARY KEY (sensor_id, hour));
COMMENT ON TABLE sensor_hourly IS
  'Per sensor and UTC hour: poll health, hop coverage, gap time, AP activity and alerts. An hour inside a long gap has a row with polls = 0. Filled by refresh_rollups().';

CREATE TABLE IF NOT EXISTS ap_summary (
  sensor_id    integer NOT NULL,
  device_key   text NOT NULL,
  -- identity and advertised configuration: devices, as in ap_inventory
  bssid        macaddr NOT NULL,
  random_bssid boolean NOT NULL,             -- locally administered bit
  manuf        text,
  ssid         text,
  hidden       boolean NOT NULL,             -- no SSID in the current record
  cloaked      boolean,
  crypt        text,
  crypt_bits   bigint,
  mfp_sup      boolean,
  mfp_req      boolean,
  adv_channel  text,
  ht_mode      text,
  beacon_rate  integer,
  country      text,
  first_seen   timestamptz NOT NULL,
  last_seen    timestamptz NOT NULL,
  config_changed_at timestamptz,
  -- hidden APs (ap_config_changes rule 4): a cloaked beacon ('' + cloaked)
  -- seen now or in the history, and the current or latest name seen
  hidden_beacon boolean NOT NULL,
  name_seen     text,
  -- baseline: the ap_baselines row (has_baseline false = none; a row whose
  -- statistics refresh_ap_baselines() cleared has NULL statistics)
  has_baseline   boolean NOT NULL,
  trusted        boolean NOT NULL,
  trusted_at     timestamptz,
  note           text,
  rssi_median    real,
  rssi_mean      real,
  rssi_sd        real,
  rssi_robust_sd real,
  rssi_p5        smallint,
  rssi_p95       smallint,
  band           text,                       -- ap_baselines.band
  baseline_n_obs   integer,
  baseline_n_floor integer,
  baseline_n_days  integer,
  baseline_window_start timestamptz,
  baseline_window_end   timestamptz,
  baseline_at    timestamptz,                -- ap_baselines.computed_at
  -- observations of the AP (sums of ap_rssi_hourly; first/last from the index)
  n_obs        bigint NOT NULL,
  n_valid      bigint NOT NULL,
  n_floor      bigint NOT NULL,
  first_obs    timestamptz,
  last_obs     timestamptz,
  -- AP load, latest value (devices.cur_*)
  cur_n_clients     integer,
  cur_qbss_stations integer,
  cur_util_pct      real,
  cur_at            timestamptz,
  -- configuration history
  history_rows   integer NOT NULL,           -- raw device_config_history rows (incl. the pre-v5 churn)
  n_changes      integer NOT NULL,           -- change events in ap_config_changes (distinct ts)
  last_change_at timestamptz,
  last_change    jsonb,                      -- [{field, old, new}] at last_change_at
  -- schema 9: security (security_level() / security_label() of the current
  -- configuration; crypt_bits_known = the bitfield, or the last known one of
  -- the same crypt string when Kismet's missing-field 0 replaced it) and the
  -- median of the hourly RSSI medians over the AP's life
  security         text,
  security_label   text,
  crypt_bits_known bigint,
  rssi_typical     real,
  PRIMARY KEY (sensor_id, device_key),
  FOREIGN KEY (sensor_id, device_key) REFERENCES devices (sensor_id, device_key));
COMMENT ON TABLE ap_summary IS
  'One row per AP with everything /api/aps and the AP page header show: inventory, baseline, observation counts, hidden-AP reading, config-change count. Rebuilt by refresh_rollups(); a change to ap_baselines (e.g. trusted) shows after the next refresh.';

CREATE TABLE IF NOT EXISTS sensor_summary (
  sensor_id        integer PRIMARY KEY REFERENCES sensors(id),
  first_poll       timestamptz,
  last_poll        timestamptz,
  polls            integer NOT NULL,
  polls_ok         integer NOT NULL,
  gap_count        integer NOT NULL,         -- poll gaps > 90 s
  gap_total_s      double precision NOT NULL,
  gap_max_s        double precision NOT NULL,
  gaps             jsonb NOT NULL,           -- the 10 longest: [{gap_start, gap_end, gap_s}]
  devices_by_type  jsonb NOT NULL,           -- {"ap": n, "client": n, ...}
  obs_total        bigint NOT NULL,          -- sum(polls.new_obs): rows the collector wrote, all device types
  obs_ap           bigint NOT NULL,          -- AP observation rows on the server (sum of sensor_hourly.ap_obs)
  country_expected text,                     -- the country most of the sensor's APs advertise
  alerts           integer NOT NULL,
  alerts_last_ts   timestamptz,
  baselines        integer NOT NULL,         -- ap_baselines rows with statistics
  refreshed_at     timestamptz NOT NULL,     -- when refresh_rollups() last ran: the api's ETag
  -- schema 9: the last collecting poll and the hop list of the last poll
  last_capture_at  timestamptz,
  last_hop_n       integer,
  last_hop_visited integer,
  last_hop_ok      boolean);
COMMENT ON TABLE sensor_summary IS
  'One row per sensor: what the overview shows, plus country_expected for the AP page. refreshed_at versions every read table of the sensor.';

-- The security reading of an AP's configuration timeline (schema 9): one row
-- per period between changes of crypt, crypt_bits, mfp_sup or mfp_req in
-- ap_config_changes (so a WPA2 -> Open downgrade is two periods), with the
-- level and label of the period. Feeds the per-hour security of the period
-- cards, "security changes in the period" and the config events.
CREATE TABLE IF NOT EXISTS ap_security_periods (
  sensor_id  integer NOT NULL,
  device_key text NOT NULL,
  valid_from timestamptz NOT NULL,           -- '-infinity' for the first known state
  valid_to   timestamptz NOT NULL,           -- 'infinity' for the current state
  crypt      text,
  crypt_bits bigint,
  mfp_sup    boolean,
  mfp_req    boolean,
  level      text NOT NULL,
  label      text NOT NULL,
  PRIMARY KEY (sensor_id, device_key, valid_from));
COMMENT ON TABLE ap_security_periods IS
  'Per AP and sensor: the periods between security-relevant configuration changes, with security_level()/security_label(). Rebuilt by refresh_rollups().';

-- Outages and limited capture of a sensor (schema 9), from polls: every poll
-- is collecting (ok, datasource running, devices > 0), limited (collecting,
-- ds_hop_ok = false) or down; the interval to the next poll takes the poll's
-- state, or down when it is longer than 90 s. Runs of down >= 90 s are
-- outages, runs of limited >= 60 s limited periods. Before buffer v4
-- (ds_hop_ok NULL) limited windows exist only as sensor_annotations.
CREATE TABLE IF NOT EXISTS sensor_periods (
  sensor_id integer NOT NULL REFERENCES sensors(id),
  starts_at timestamptz NOT NULL,            -- an outage after a poll gap starts at the last good poll
  ends_at   timestamptz,                     -- NULL = still going at the sensor's last poll
  kind      text NOT NULL CHECK (kind IN ('outage', 'limited')),
  polls     integer NOT NULL,                -- polls in the period in that state (not collecting / limited)
  PRIMARY KEY (sensor_id, starts_at, kind));
COMMENT ON TABLE sensor_periods IS
  'Outages and limited-capture periods of a sensor, derived from polls. Rebuilt by refresh_rollups().';

-- The security periods of one sensor's APs (schema 9; ap_security_periods):
-- one row per period between changes of crypt, crypt_bits, mfp_sup, mfp_req in
-- ap_config_changes; each field at the start of a period = the new value of
-- its last change up to then, else the old value of its first later change,
-- else the current value (ap_summary, so it runs after the ap_summary
-- rebuild). A period whose crypt string differs from the current one takes the
-- current bitfield only if the bitfield itself has changes on record
-- (otherwise it is unknown and security_level() reads the crypt string).
-- Whole-sensor rebuild; returns the rows written. Called by refresh_rollups();
-- fixture test tests/sql/events_test.sql.
CREATE OR REPLACE FUNCTION refresh_security_periods(p_sensor_id integer)
RETURNS integer
LANGUAGE plpgsql
SET jit = off
AS $$
#variable_conflict use_column
DECLARE v_n integer;
BEGIN
  DELETE FROM ap_security_periods WHERE sensor_id = p_sensor_id;
  INSERT INTO ap_security_periods (sensor_id, device_key, valid_from, valid_to, crypt, crypt_bits,
                                   mfp_sup, mfp_req, level, label)
  WITH chg AS MATERIALIZED (
    SELECT device_key, ts, field, old_value, new_value
    FROM ap_config_changes
    WHERE sensor_id = p_sensor_id AND field IN ('crypt', 'crypt_bits', 'mfp_sup', 'mfp_req')
  ), pts AS (
    SELECT device_key, '-infinity'::timestamptz AS valid_from FROM ap_summary WHERE sensor_id = p_sensor_id
    UNION
    SELECT c.device_key, c.ts FROM chg c
    JOIN ap_summary s ON s.sensor_id = p_sensor_id AND s.device_key = c.device_key
  ), st AS (
    SELECT p.device_key, p.valid_from,
           lead(p.valid_from, 1, 'infinity'::timestamptz)
             OVER (PARTITION BY p.device_key ORDER BY p.valid_from) AS valid_to,
           coalesce(
             (SELECT c.new_value FROM chg c WHERE c.device_key = p.device_key AND c.field = 'crypt'
                AND c.ts <= p.valid_from ORDER BY c.ts DESC LIMIT 1),
             (SELECT c.old_value FROM chg c WHERE c.device_key = p.device_key AND c.field = 'crypt'
                AND c.ts > p.valid_from ORDER BY c.ts LIMIT 1),
             s.crypt) AS crypt,
           coalesce(
             (SELECT c.new_value FROM chg c WHERE c.device_key = p.device_key AND c.field = 'crypt_bits'
                AND c.ts <= p.valid_from ORDER BY c.ts DESC LIMIT 1),
             (SELECT c.old_value FROM chg c WHERE c.device_key = p.device_key AND c.field = 'crypt_bits'
                AND c.ts > p.valid_from ORDER BY c.ts LIMIT 1))::bigint AS chg_bits,
           EXISTS (SELECT 1 FROM chg c WHERE c.device_key = p.device_key AND c.field = 'crypt_bits')
             AS bits_changed,
           coalesce(
             (SELECT c.new_value FROM chg c WHERE c.device_key = p.device_key AND c.field = 'mfp_sup'
                AND c.ts <= p.valid_from ORDER BY c.ts DESC LIMIT 1),
             (SELECT c.old_value FROM chg c WHERE c.device_key = p.device_key AND c.field = 'mfp_sup'
                AND c.ts > p.valid_from ORDER BY c.ts LIMIT 1),
             s.mfp_sup::text)::boolean AS mfp_sup,
           coalesce(
             (SELECT c.new_value FROM chg c WHERE c.device_key = p.device_key AND c.field = 'mfp_req'
                AND c.ts <= p.valid_from ORDER BY c.ts DESC LIMIT 1),
             (SELECT c.old_value FROM chg c WHERE c.device_key = p.device_key AND c.field = 'mfp_req'
                AND c.ts > p.valid_from ORDER BY c.ts LIMIT 1),
             s.mfp_req::text)::boolean AS mfp_req,
           s.crypt AS cur_crypt, s.crypt_bits_known AS cur_bits
    FROM pts p
    JOIN ap_summary s ON s.sensor_id = p_sensor_id AND s.device_key = p.device_key
  ), v AS (
    SELECT st.*,
           CASE WHEN st.bits_changed THEN coalesce(st.chg_bits, st.cur_bits)
                WHEN st.crypt IS NOT DISTINCT FROM st.cur_crypt THEN st.cur_bits END AS crypt_bits
    FROM st
  )
  SELECT p_sensor_id, v.device_key, v.valid_from, v.valid_to, v.crypt, v.crypt_bits, v.mfp_sup, v.mfp_req,
         security_level(v.crypt, v.crypt_bits, v.mfp_sup, v.mfp_req), security_label(v.crypt, v.crypt_bits)
  FROM v;
  GET DIAGNOSTICS v_n = ROW_COUNT;
  RETURN v_n;
END
$$;

-- Outages and limited capture of one sensor (schema 9; sensor_periods), from
-- polls (definition at the table). Whole-history rebuild (a window scan over
-- ~70k polls, < 1 s); returns the rows written. Called by refresh_rollups();
-- fixture test tests/sql/events_test.sql.
CREATE OR REPLACE FUNCTION refresh_sensor_periods(p_sensor_id integer)
RETURNS integer
LANGUAGE plpgsql
SET jit = off
AS $$
#variable_conflict use_column
DECLARE v_n integer;
BEGIN
  DELETE FROM sensor_periods WHERE sensor_id = p_sensor_id;
  INSERT INTO sensor_periods (sensor_id, starts_at, ends_at, kind, polls)
  WITH p AS (
    SELECT ts, lead(ts) OVER (ORDER BY ts) AS next_ts,
           CASE WHEN NOT (ok AND coalesce(ds_running, false) AND coalesce(devices_active, 0) > 0) THEN 'outage'
                WHEN ds_hop_ok = false THEN 'limited' ELSE 'ok' END AS poll_st
    FROM polls WHERE sensor_id = p_sensor_id
  ), s AS (
    SELECT ts, next_ts, poll_st,
           CASE WHEN next_ts - ts > interval '90 seconds' THEN 'outage' ELSE poll_st END AS st
    FROM p
  ), r AS (
    SELECT x.*, sum(CASE WHEN x.st IS DISTINCT FROM x.prev_st THEN 1 ELSE 0 END) OVER (ORDER BY x.ts) AS run
    FROM (SELECT s.*, lag(s.st) OVER (ORDER BY s.ts) AS prev_st FROM s) x
  ), g AS (
    SELECT min(ts) AS starts_at,
           CASE WHEN bool_or(next_ts IS NULL) THEN NULL ELSE max(next_ts) END AS ends_at,
           max(ts) AS last_ts, min(st) AS kind, (count(*) FILTER (WHERE poll_st = st))::integer AS polls
    FROM r GROUP BY run
  )
  SELECT p_sensor_id, g.starts_at, g.ends_at, g.kind, g.polls
  FROM g
  WHERE g.kind IN ('outage', 'limited')
    AND coalesce(g.ends_at, g.last_ts) - g.starts_at
        >= CASE g.kind WHEN 'outage' THEN interval '90 seconds' ELSE interval '60 seconds' END;
  GET DIAGNOSTICS v_n = ROW_COUNT;
  RETURN v_n;
END
$$;

-- The derived event rows of one sensor (schema 9), written into detections:
--   kismet  episodes of Kismet's WIDS alerts: alerts of one header on one AP
--           (the AP whose MAC is the alert's transmitter_mac - Kismet's BSSID
--           argument; never a source/destination MAC, which can be a client)
--           closer than kismet_alert_rules.episode_gap_s (1 h); headers with
--           as_event = false (SOURCEERROR, SOURCEOPEN) are not events.
--           Severity: the rule's override, else kismet_severity(). An episode
--           is matched to the stored row of the same type and AP whose window
--           overlaps it (as the batch detectors' sink does), else inserted.
--   config  every change point of ap_security_periods: down to open high,
--           other downgrades medium, same level low, upgrades info.
--   sensor  sensor_periods of at least account_settings.sensor_offline_min
--           (outage, medium) or 10 min (limited, low), cause from
--           sensor_annotations.
--   rule    detection 'unknown_bssid_managed_ssid' for every AP that the
--           classification rules (not a manual classification) mark suspicious.
-- Only ts/ts_end/severity/summary/evidence of a stored row are refreshed, and
-- only when they change (updated_at = the ETag); status, status_at and
-- status_note - the operator's labels - are never touched, and no stored row
-- is deleted. No client MAC, probe or Kismet alert text is copied. Fixture
-- test: tests/sql/events_test.sql. Called by refresh_rollups() and by the
-- api's network/settings write endpoints.
CREATE OR REPLACE FUNCTION refresh_events(p_sensor_id integer)
RETURNS TABLE (n_kismet integer, n_config integer, n_sensor integer, n_rule integer)
LANGUAGE plpgsql
SET jit = off
AS $$
#variable_conflict use_column
DECLARE
  v_account   integer;
  v_offline_s integer;
  v_last_poll timestamptz;
  v_kismet    integer := 0;
  v_config    integer := 0;
  v_sensor    integer := 0;
  v_rule      integer := 0;
  v_n         integer;
  v_id        bigint;
  e           record;
BEGIN
  SELECT se.account_id, coalesce(st.sensor_offline_min, 15) * 60
    INTO v_account, v_offline_s
  FROM sensors se LEFT JOIN account_settings st ON st.account_id = se.account_id
  WHERE se.id = p_sensor_id;
  IF v_account IS NULL THEN
    RAISE EXCEPTION 'refresh_events: unknown sensor_id %', p_sensor_id;
  END IF;
  SELECT max(ts) INTO v_last_poll FROM polls WHERE sensor_id = p_sensor_id;

  -- 1. Kismet alert episodes
  FOR e IN
    WITH a AS (
      SELECT al.id, al.ts, al.header, al.severity AS kseverity, al.channel,
             ap.device_key, ap.mac, ap.ssid,
             coalesce(r.episode_gap_s, 3600) AS gap_s, r.severity AS override,
             coalesce(r.label, al.header) AS label
      FROM alerts al
      LEFT JOIN kismet_alert_rules r ON r.header = al.header
      LEFT JOIN LATERAL (SELECT d.device_key, d.mac, nullif(d.ssid, '') AS ssid
                         FROM devices d
                         WHERE d.sensor_id = al.sensor_id AND d.type = 'ap' AND d.mac = al.transmitter_mac
                         ORDER BY d.last_seen DESC LIMIT 1) ap ON true
      WHERE al.sensor_id = p_sensor_id AND al.ts IS NOT NULL AND coalesce(r.as_event, true)
    ), b AS (
      SELECT a.*,
             CASE WHEN lag(a.ts) OVER w IS NULL
                    OR a.ts - lag(a.ts) OVER w > make_interval(secs => a.gap_s) THEN 1 ELSE 0 END AS starts
      FROM a
      WINDOW w AS (PARTITION BY a.header, a.device_key ORDER BY a.ts, a.id)
    ), c AS (
      SELECT b.*, sum(b.starts) OVER (PARTITION BY b.header, b.device_key ORDER BY b.ts, b.id) AS episode
      FROM b
    )
    SELECT c.header, c.device_key, (array_agg(c.mac))[1] AS mac, (array_agg(c.ssid))[1] AS ssid,
           min(c.ts) AS ts, max(c.ts) AS ts_end, count(*) AS n, max(c.gap_s) AS gap_s,
           coalesce(min(c.override), kismet_severity(max(c.kseverity))) AS severity,
           min(c.label) || CASE WHEN count(*) > 1 THEN ' (' || count(*) || ' alerts)' ELSE '' END AS summary,
           jsonb_build_object(
             'header', c.header, 'alerts', count(*), 'first', min(c.ts), 'last', max(c.ts),
             'kismet_severity', max(c.kseverity),
             'channels', coalesce(to_jsonb(array_agg(DISTINCT c.channel) FILTER (WHERE c.channel IS NOT NULL)),
                                  '[]'::jsonb)) AS evidence
    FROM c
    GROUP BY c.header, c.device_key, c.episode
  LOOP
    v_id := NULL;
    SELECT x.id INTO v_id FROM detections x
    WHERE x.sensor_id = p_sensor_id AND x.kind = 'kismet' AND x.type = e.header
      AND x.device_key IS NOT DISTINCT FROM e.device_key
      AND x.ts <= e.ts_end + make_interval(secs => e.gap_s)
      AND coalesce(x.ts_end, x.ts) >= e.ts - make_interval(secs => e.gap_s)
    ORDER BY x.ts LIMIT 1;
    IF v_id IS NULL THEN
      INSERT INTO detections (sensor_id, kind, ts, ts_end, type, severity, device_key, mac, ssid,
                              summary, evidence)
      VALUES (p_sensor_id, 'kismet', e.ts, e.ts_end, e.header, e.severity, e.device_key, e.mac, e.ssid,
              e.summary, e.evidence)
      ON CONFLICT DO NOTHING;
    ELSE
      UPDATE detections x SET ts = e.ts, ts_end = e.ts_end, severity = e.severity, ssid = e.ssid,
                              summary = e.summary, evidence = e.evidence, updated_at = now()
      WHERE x.id = v_id
        AND (x.ts, x.ts_end, x.severity, x.ssid, x.summary, x.evidence)
            IS DISTINCT FROM (e.ts, e.ts_end, e.severity, e.ssid, e.summary, e.evidence);
    END IF;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    v_kismet := v_kismet + v_n;
  END LOOP;

  -- 2. security changes
  INSERT INTO detections AS x (sensor_id, kind, ts, type, severity, device_key, mac, ssid, summary, evidence)
  SELECT p_sensor_id, 'config', cur.valid_from, 'security_change',
         CASE WHEN security_rank(cur.level) < security_rank(prev.level) AND cur.level = 'open' THEN 'high'
              WHEN security_rank(cur.level) < security_rank(prev.level) THEN 'medium'
              WHEN security_rank(cur.level) > security_rank(prev.level) THEN 'info'
              ELSE 'low' END,
         cur.device_key, d.mac, nullif(d.ssid, ''),
         CASE WHEN cur.label IS DISTINCT FROM prev.label
                THEN 'Security ' || prev.label || ' → ' || cur.label
              WHEN mfp_state(cur.mfp_sup, cur.mfp_req) IS DISTINCT FROM mfp_state(prev.mfp_sup, prev.mfp_req)
                THEN 'MFP ' || coalesce(mfp_state(prev.mfp_sup, prev.mfp_req), 'unknown') || ' → '
                     || coalesce(mfp_state(cur.mfp_sup, cur.mfp_req), 'unknown')
              ELSE 'Encryption ' || coalesce(prev.crypt, 'unknown') || ' → ' || coalesce(cur.crypt, 'unknown') END,
         jsonb_build_object(
           'from', jsonb_build_object('crypt', prev.crypt, 'crypt_bits', prev.crypt_bits,
                                      'mfp', mfp_state(prev.mfp_sup, prev.mfp_req),
                                      'level', prev.level, 'label', prev.label),
           'to', jsonb_build_object('crypt', cur.crypt, 'crypt_bits', cur.crypt_bits,
                                    'mfp', mfp_state(cur.mfp_sup, cur.mfp_req),
                                    'level', cur.level, 'label', cur.label))
  FROM ap_security_periods cur
  JOIN ap_security_periods prev
    ON prev.sensor_id = cur.sensor_id AND prev.device_key = cur.device_key AND prev.valid_to = cur.valid_from
  JOIN devices d ON d.sensor_id = cur.sensor_id AND d.device_key = cur.device_key
  WHERE cur.sensor_id = p_sensor_id AND cur.valid_from > '-infinity'
  ON CONFLICT (sensor_id, type, (coalesce(device_key, upper(mac::text))), ts) DO UPDATE SET
      severity = EXCLUDED.severity, ssid = EXCLUDED.ssid, summary = EXCLUDED.summary,
      evidence = EXCLUDED.evidence, updated_at = now()
    WHERE (x.severity, x.ssid, x.summary, x.evidence)
          IS DISTINCT FROM (EXCLUDED.severity, EXCLUDED.ssid, EXCLUDED.summary, EXCLUDED.evidence);
  GET DIAGNOSTICS v_config = ROW_COUNT;

  -- 3. sensor outages and limited capture
  INSERT INTO detections AS x (sensor_id, kind, ts, ts_end, type, severity, summary, evidence)
  SELECT p_sensor_id, 'sensor', sp.starts_at, sp.ends_at,
         CASE sp.kind WHEN 'outage' THEN 'sensor_outage' ELSE 'sensor_limited' END,
         CASE sp.kind WHEN 'outage' THEN 'medium' ELSE 'low' END,
         CASE sp.kind WHEN 'outage' THEN 'Sensor not collecting' ELSE 'Capture limited (hop list degraded)' END,
         jsonb_build_object(
           'kind', sp.kind, 'polls', sp.polls, 'ongoing', sp.ends_at IS NULL,
           'duration_s', extract(epoch FROM coalesce(sp.ends_at, v_last_poll) - sp.starts_at)::integer,
           'cause', (SELECT string_agg(an.label, '; ' ORDER BY an.starts_at)
                     FROM sensor_annotations an
                     WHERE an.sensor_id = sp.sensor_id AND an.kind IN ('gap', 'no_frames', 'coverage')
                       AND an.starts_at < coalesce(sp.ends_at, 'infinity')
                       AND coalesce(an.ends_at, an.starts_at) > sp.starts_at))
  FROM sensor_periods sp
  WHERE sp.sensor_id = p_sensor_id
    AND coalesce(sp.ends_at, v_last_poll) - sp.starts_at
        >= CASE sp.kind WHEN 'outage' THEN make_interval(secs => v_offline_s) ELSE interval '10 minutes' END
  ON CONFLICT (sensor_id, type, (coalesce(device_key, upper(mac::text))), ts) DO UPDATE SET
      ts_end = EXCLUDED.ts_end, summary = EXCLUDED.summary, evidence = EXCLUDED.evidence, updated_at = now()
    WHERE (x.ts_end, x.summary, x.evidence) IS DISTINCT FROM (EXCLUDED.ts_end, EXCLUDED.summary, EXCLUDED.evidence);
  GET DIAGNOSTICS v_sensor = ROW_COUNT;

  -- 4. the classification rule: an unknown BSSID broadcasting a managed SSID
  INSERT INTO detections AS x (sensor_id, kind, ts, type, severity, device_key, mac, ssid, summary, evidence)
  SELECT p_sensor_id, 'detection', coalesce(s.first_obs, s.first_seen), 'unknown_bssid_managed_ssid', 'high',
         s.device_key, s.bssid, n.ssid,
         'Unknown BSSID broadcasting managed SSID "' || n.ssid || '"',
         jsonb_build_object(
           'network', n.ssid, 'random_bssid', s.random_bssid, 'security', s.security,
           'security_label', s.security_label, 'channel', s.adv_channel,
           'allowed_bssids', cardinality(n.allowed_bssids),
           'allowed_ouis', to_jsonb(ARRAY(SELECT upper(left(trunc(o)::text, 8)) FROM unnest(n.allowed_ouis) AS o)))
  FROM ap_summary s
  JOIN aps a ON a.account_id = v_account AND a.bssid = s.bssid
  JOIN networks n ON n.account_id = v_account AND n.kind = 'managed'
                 AND n.ssid = coalesce(nullif(s.ssid, ''), s.name_seen)
  WHERE s.sensor_id = p_sensor_id AND a.classification IS NULL
    AND ap_classification(v_account, NULL, n.ssid, s.bssid, s.random_bssid, s.rssi_typical) = 'suspicious'
  ON CONFLICT (sensor_id, type, (coalesce(device_key, upper(mac::text))), ts) DO UPDATE SET
      ssid = EXCLUDED.ssid, summary = EXCLUDED.summary, evidence = EXCLUDED.evidence, updated_at = now()
    WHERE (x.ssid, x.summary, x.evidence) IS DISTINCT FROM (EXCLUDED.ssid, EXCLUDED.summary, EXCLUDED.evidence);
  GET DIAGNOSTICS v_rule = ROW_COUNT;

  RETURN QUERY SELECT v_kismet, v_config, v_sensor, v_rule;
END
$$;

-- Recompute the read tables of one sensor from the hour of p_from onward
-- (p_from NULL, or the sensor never refreshed: the whole history). Idempotent;
-- the seed importer calls it after every load with the earliest ts it
-- inserted. Per hour tables: the hours >= the hour of p_from - moved back to
-- the hour of the last poll before p_from, so a poll gap ending after p_from
-- is split over all its hours again - are deleted and recomputed; an AP with
-- no hourly row left (new, or re-classified as an AP) is computed over its
-- whole history, rows of devices that are no longer APs are dropped, and the
-- sensor hours go back to the earliest hour either touched. Invariant: an
-- incremental run leaves the same rows as a full one (fixture test
-- tests/sql/refresh_rollups_test.sql). Then ap_config_changes is refreshed
-- (all sensors) and ap_summary / sensor_summary are rebuilt. Measured on the 12-day seed (2026-09-29, read-only): the
-- full-history AP hour aggregate took 16 s cold, the config view 4.7 s.
-- Schema 9 adds, all whole-sensor rebuilds or idempotent upserts (so the
-- invariant holds): ap_summary security columns, ap_security_periods, the
-- account-level aps (first/last seen only) and ap_sensors, sensor_periods,
-- and finally refresh_events() (derived event rows; never their status).
CREATE OR REPLACE FUNCTION refresh_rollups(p_sensor_id integer, p_from timestamptz DEFAULT NULL)
RETURNS TABLE (from_hour timestamptz, ap_hours integer, sensor_hours integer, aps integer,
               refreshed_at timestamptz)
LANGUAGE plpgsql
SET work_mem = '32MB'      -- the hour aggregate sorts ~1.8M rows on a full run
SET jit = off              -- JIT compilation costs more than it saves on these statements
AS $$
#variable_conflict use_column
DECLARE
  v_from timestamptz;        -- first hour recomputed for every AP
  v_sfrom timestamptz;       -- first hour recomputed per sensor (earlier after a re-classification)
  v_gone timestamptz;        -- first hour of rows dropped for devices that are no longer APs
  v_new  timestamptz;        -- first hour of APs computed over their whole history
  v_now  timestamptz := clock_timestamp();
  v_ap_hours integer;
  v_sensor_hours integer;
  v_aps integer;
  v_account integer;
BEGIN
  SELECT account_id INTO v_account FROM sensors WHERE id = p_sensor_id;
  IF v_account IS NULL THEN
    RAISE EXCEPTION 'refresh_rollups: unknown sensor_id %', p_sensor_id;
  END IF;
  -- one refresh per sensor at a time: two would delete and insert the same rows
  PERFORM pg_advisory_xact_lock(hashtext('refresh_rollups'), p_sensor_id);

  -- 1. first hour to recompute
  IF p_from IS NULL OR NOT EXISTS (SELECT 1 FROM sensor_summary WHERE sensor_id = p_sensor_id) THEN
    v_from := '-infinity';
  ELSE
    v_from := date_trunc('hour',
                least(p_from, (SELECT max(ts) FROM polls WHERE sensor_id = p_sensor_id AND ts < p_from)),
                'UTC');
  END IF;

  -- 2. per AP and hour
  WITH gone AS (
    DELETE FROM ap_rssi_hourly h
    WHERE h.sensor_id = p_sensor_id
      AND NOT EXISTS (SELECT 1 FROM devices d
                      WHERE d.sensor_id = h.sensor_id AND d.device_key = h.device_key
                        AND d.type = 'ap')
    RETURNING h.hour)
  SELECT min(hour) INTO v_gone FROM gone;
  DELETE FROM ap_rssi_hourly WHERE sensor_id = p_sensor_id AND hour >= v_from;
  SELECT min(date_trunc('hour', (SELECT min(o.ts) FROM observations o
                                 WHERE o.sensor_id = d.sensor_id AND o.device_key = d.device_key),
                        'UTC'))
    INTO v_new
  FROM devices d
  WHERE d.sensor_id = p_sensor_id AND d.type = 'ap'
    AND NOT EXISTS (SELECT 1 FROM ap_rssi_hourly h
                    WHERE h.sensor_id = d.sensor_id AND h.device_key = d.device_key);
  v_sfrom := least(v_from, v_gone, v_new);
  INSERT INTO ap_rssi_hourly (sensor_id, device_key, hour, n_obs, n_valid, n_floor,
                              median, p10, p90, min, max)
  SELECT p_sensor_id, device_key, hour, n_obs, n_valid, n_floor, pct[1], pct[2], pct[3], lo, hi
  FROM (
    SELECT a.device_key, date_trunc('hour', o.ts, 'UTC') AS hour,
           count(*)::integer AS n_obs,
           count(rssi_valid(o.rssi))::integer AS n_valid,
           (count(*) FILTER (WHERE rssi_is_floor(o.rssi, o.rssi_floor)))::integer AS n_floor,
           percentile_cont(ARRAY[0.5, 0.1, 0.9]) WITHIN GROUP (ORDER BY rssi_valid(o.rssi)) AS pct,
           min(rssi_valid(o.rssi)) AS lo,
           max(rssi_valid(o.rssi)) AS hi
    FROM (SELECT d.device_key,
                 CASE WHEN EXISTS (SELECT 1 FROM ap_rssi_hourly h
                                   WHERE h.sensor_id = d.sensor_id AND h.device_key = d.device_key)
                      THEN v_from ELSE '-infinity'::timestamptz END AS since
          FROM devices d
          WHERE d.sensor_id = p_sensor_id AND d.type = 'ap') a
    JOIN observations o
      ON o.sensor_id = p_sensor_id AND o.device_key = a.device_key AND o.ts >= a.since
    GROUP BY 1, 2) s;
  GET DIAGNOSTICS v_ap_hours = ROW_COUNT;

  -- 3. per sensor and hour (from v_sfrom: a re-classified device changed
  --    the AP counts of its hours before v_from too)
  DELETE FROM sensor_hourly WHERE sensor_id = p_sensor_id AND hour >= v_sfrom;
  INSERT INTO sensor_hourly (sensor_id, hour, polls, polls_ok, hop_ok_polls, hop_known_polls,
                             gap_s, active_aps, new_aps, alerts, ap_obs, capture_polls, limited_polls)
  WITH p AS (
    SELECT date_trunc('hour', ts, 'UTC') AS hour, count(*) AS polls,
           count(*) FILTER (WHERE ok) AS polls_ok,
           count(*) FILTER (WHERE ds_hop_ok) AS hop_ok, count(ds_hop_ok) AS hop_known,
           count(*) FILTER (WHERE ok AND ds_running AND devices_active > 0) AS capture,
           count(*) FILTER (WHERE ok AND ds_running AND devices_active > 0 AND ds_hop_ok = false) AS limited
    FROM polls WHERE sensor_id = p_sensor_id AND ts >= v_sfrom
    GROUP BY 1
  ), g AS (    -- poll gaps > 90 s as on the overview, incl. one ending at the first poll >= v_sfrom
    SELECT prev_ts, ts
    FROM (SELECT ts, lag(ts) OVER (ORDER BY ts) AS prev_ts
          FROM polls
          WHERE sensor_id = p_sensor_id
            AND ts >= coalesce((SELECT max(ts) FROM polls
                                WHERE sensor_id = p_sensor_id AND ts < v_sfrom), v_sfrom)) x
    WHERE ts - prev_ts > interval '90 seconds'
  ), gh AS (   -- ... split over the hours they cover
    SELECT h.hour,
           sum(extract(epoch FROM least(g.ts, h.hour + interval '1 hour') - greatest(g.prev_ts, h.hour))) AS gap_s
    FROM g
    CROSS JOIN LATERAL generate_series(date_trunc('hour', g.prev_ts, 'UTC'), g.ts,
                                       interval '1 hour') AS h(hour)
    WHERE h.hour >= v_sfrom AND h.hour < g.ts
    GROUP BY h.hour
  ), a AS (
    SELECT hour, count(*) AS active_aps, sum(n_obs) AS ap_obs
    FROM ap_rssi_hourly WHERE sensor_id = p_sensor_id AND hour >= v_sfrom
    GROUP BY hour
  ), f AS (    -- the first hour in which each AP was observed
    SELECT first_hour AS hour, count(*) AS new_aps
    FROM (SELECT min(hour) AS first_hour FROM ap_rssi_hourly
          WHERE sensor_id = p_sensor_id GROUP BY device_key) x
    WHERE first_hour >= v_sfrom
    GROUP BY first_hour
  ), al AS (
    SELECT date_trunc('hour', ts, 'UTC') AS hour, count(*) AS alerts
    FROM alerts WHERE sensor_id = p_sensor_id AND ts >= v_sfrom
    GROUP BY 1
  ), hrs AS (
    SELECT hour FROM p UNION SELECT hour FROM gh UNION SELECT hour FROM a UNION SELECT hour FROM al
  )
  SELECT p_sensor_id, hrs.hour, coalesce(p.polls, 0), coalesce(p.polls_ok, 0),
         coalesce(p.hop_ok, 0), coalesce(p.hop_known, 0), coalesce(gh.gap_s, 0),
         coalesce(a.active_aps, 0), coalesce(f.new_aps, 0), coalesce(al.alerts, 0),
         coalesce(a.ap_obs, 0), coalesce(p.capture, 0), coalesce(p.limited, 0)
  FROM hrs
  LEFT JOIN p  ON p.hour  = hrs.hour
  LEFT JOIN gh ON gh.hour = hrs.hour
  LEFT JOIN a  ON a.hour  = hrs.hour
  LEFT JOIN f  ON f.hour  = hrs.hour
  LEFT JOIN al ON al.hour = hrs.hour;
  GET DIAGNOSTICS v_sensor_hours = ROW_COUNT;

  -- 4. the configuration timeline (all sensors; history only changes with an
  --    import). CONCURRENTLY: AP pages and detectors keep reading meanwhile.
  IF (SELECT relispopulated FROM pg_class WHERE oid = to_regclass('ap_config_changes')) THEN
    REFRESH MATERIALIZED VIEW CONCURRENTLY ap_config_changes;
  ELSE
    REFRESH MATERIALIZED VIEW ap_config_changes;
  END IF;

  -- 5. one row per AP, rebuilt
  DELETE FROM ap_summary WHERE sensor_id = p_sensor_id;
  INSERT INTO ap_summary (
      sensor_id, device_key, bssid, random_bssid, manuf, ssid, hidden, cloaked, crypt, crypt_bits,
      mfp_sup, mfp_req, adv_channel, ht_mode, beacon_rate, country, first_seen, last_seen,
      config_changed_at, hidden_beacon, name_seen,
      has_baseline, trusted, trusted_at, note, rssi_median, rssi_mean, rssi_sd, rssi_robust_sd,
      rssi_p5, rssi_p95, band, baseline_n_obs, baseline_n_floor, baseline_n_days,
      baseline_window_start, baseline_window_end, baseline_at,
      n_obs, n_valid, n_floor, first_obs, last_obs,
      cur_n_clients, cur_qbss_stations, cur_util_pct, cur_at,
      history_rows, n_changes, last_change_at, last_change,
      security, security_label, crypt_bits_known, rssi_typical)
  SELECT d.sensor_id, d.device_key, d.mac,
         (d.mac & macaddr '02:00:00:00:00:00') <> macaddr '00:00:00:00:00:00',
         d.manuf, d.ssid, (d.ssid IS NULL OR d.ssid = ''), d.cloaked, d.crypt, d.crypt_bits,
         d.mfp_sup, d.mfp_req, d.adv_channel, d.ht_mode, d.beacon_rate, d.country,
         d.first_seen, d.last_seen, d.config_changed_at,
         coalesce(hi.hidden_beacon, false) OR (coalesce(d.cloaked, false) AND coalesce(d.ssid, '') = ''),
         coalesce(nullif(d.ssid, ''), hi.name_seen),
         b.device_key IS NOT NULL, coalesce(b.trusted, false), b.trusted_at, b.note,
         b.rssi_median, b.rssi_mean, b.rssi_sd, b.rssi_robust_sd, b.rssi_p5, b.rssi_p95,
         b.band, b.n_obs, b.n_floor, b.n_days, b.window_start, b.window_end, b.computed_at,
         coalesce(r.n_obs, 0), coalesce(r.n_valid, 0), coalesce(r.n_floor, 0),
         (SELECT min(o.ts) FROM observations o
          WHERE o.sensor_id = d.sensor_id AND o.device_key = d.device_key),
         (SELECT max(o.ts) FROM observations o
          WHERE o.sensor_id = d.sensor_id AND o.device_key = d.device_key),
         d.cur_n_clients, d.cur_qbss_stations, d.cur_util_pct, d.cur_at,
         coalesce(hi.n_rows, 0), coalesce(c.n_changes, 0), c.last_change_at, c.last_change,
         security_level(d.crypt, kb.bits, d.mfp_sup, d.mfp_req), security_label(d.crypt, kb.bits),
         kb.bits, rt.rssi_typical
  FROM devices d
  LEFT JOIN ap_baselines b ON b.sensor_id = d.sensor_id AND b.device_key = d.device_key
  LEFT JOIN (SELECT device_key, sum(n_obs) AS n_obs, sum(n_valid) AS n_valid, sum(n_floor) AS n_floor
             FROM ap_rssi_hourly WHERE sensor_id = p_sensor_id
             GROUP BY device_key) r ON r.device_key = d.device_key
  LEFT JOIN (SELECT device_key, count(*) AS n_rows,
                    bool_or(cloaked AND ssid = '') AS hidden_beacon,
                    (array_agg(ssid ORDER BY ts DESC) FILTER (WHERE ssid <> ''))[1] AS name_seen
             FROM device_config_history WHERE sensor_id = p_sensor_id
             GROUP BY device_key) hi ON hi.device_key = d.device_key
  LEFT JOIN (SELECT device_key, count(*) AS n_changes, max(ts) AS last_change_at,
                    (array_agg(changes ORDER BY ts DESC))[1] AS last_change
             FROM (SELECT device_key, ts,
                          jsonb_agg(jsonb_build_object('field', field, 'old', old_value,
                                                       'new', new_value) ORDER BY field) AS changes
                   FROM ap_config_changes WHERE sensor_id = p_sensor_id
                   GROUP BY device_key, ts) e
             GROUP BY device_key) c ON c.device_key = d.device_key
  -- the bitfield, or the last known one of the same crypt string when
  -- Kismet's missing-field 0 replaced it (ap_config_changes rule 3)
  LEFT JOIN LATERAL (
    SELECT CASE WHEN d.crypt_bits <> 0 OR d.crypt = 'Open' THEN d.crypt_bits
                ELSE (SELECT h.crypt_bits FROM device_config_history h
                      WHERE h.sensor_id = d.sensor_id AND h.device_key = d.device_key
                        AND h.crypt IS NOT DISTINCT FROM d.crypt AND h.crypt_bits <> 0
                      ORDER BY h.ts DESC LIMIT 1) END AS bits) kb ON true
  LEFT JOIN (SELECT device_key, percentile_cont(0.5) WITHIN GROUP (ORDER BY median) AS rssi_typical
             FROM ap_rssi_hourly WHERE sensor_id = p_sensor_id AND median IS NOT NULL
             GROUP BY device_key) rt ON rt.device_key = d.device_key
  WHERE d.sensor_id = p_sensor_id AND d.type = 'ap';
  GET DIAGNOSTICS v_aps = ROW_COUNT;

  -- 5b. security periods (refresh_security_periods())
  PERFORM refresh_security_periods(p_sensor_id);

  -- 5c. account-level APs (one per BSSID of the account; first/last seen over
  --     all its sensors - never the manual classification or the note) and the
  --     sensor's mapping to them
  -- (alias t: the function's OUT parameter is also called aps)
  INSERT INTO aps AS t (account_id, bssid, first_seen, last_seen)
  SELECT v_account, s.bssid, min(coalesce(s.first_obs, s.first_seen)), max(coalesce(s.last_obs, s.last_seen))
  FROM ap_summary s
  JOIN sensors se ON se.id = s.sensor_id AND se.account_id = v_account
  WHERE s.bssid IN (SELECT x.bssid FROM ap_summary x WHERE x.sensor_id = p_sensor_id)
  GROUP BY s.bssid
  ON CONFLICT (account_id, bssid) DO UPDATE SET
      first_seen = EXCLUDED.first_seen, last_seen = EXCLUDED.last_seen
    WHERE (t.first_seen, t.last_seen) IS DISTINCT FROM (EXCLUDED.first_seen, EXCLUDED.last_seen);
  DELETE FROM ap_sensors WHERE sensor_id = p_sensor_id;
  INSERT INTO ap_sensors (ap_id, sensor_id, device_key)
  SELECT a.id, s.sensor_id, s.device_key
  FROM ap_summary s
  JOIN aps a ON a.account_id = v_account AND a.bssid = s.bssid
  WHERE s.sensor_id = p_sensor_id;

  -- 6. one row per sensor
  WITH gap AS (
    SELECT prev_ts AS gap_start, ts AS gap_end, ts - prev_ts AS len
    FROM (SELECT ts, lag(ts) OVER (ORDER BY ts) AS prev_ts
          FROM polls WHERE sensor_id = p_sensor_id) x
    WHERE ts - prev_ts > interval '90 seconds'
  )
  INSERT INTO sensor_summary (sensor_id, first_poll, last_poll, polls, polls_ok,
                              gap_count, gap_total_s, gap_max_s, gaps, devices_by_type,
                              obs_total, obs_ap, country_expected, alerts, alerts_last_ts,
                              baselines, refreshed_at, last_capture_at, last_hop_n, last_hop_visited,
                              last_hop_ok)
  SELECT p_sensor_id, pl.first_poll, pl.last_poll, pl.polls, pl.polls_ok,
         (SELECT count(*) FROM gap),
         (SELECT coalesce(extract(epoch FROM sum(len)), 0) FROM gap),
         (SELECT coalesce(extract(epoch FROM max(len)), 0) FROM gap),
         (SELECT coalesce(jsonb_agg(jsonb_build_object('gap_start', gap_start, 'gap_end', gap_end,
                                                       'gap_s', extract(epoch FROM len)::float8)
                                    ORDER BY len DESC), '[]'::jsonb)
          FROM (SELECT * FROM gap ORDER BY len DESC LIMIT 10) t),
         (SELECT coalesce(jsonb_object_agg(type, n), '{}'::jsonb)
          FROM (SELECT type, count(*) AS n FROM devices WHERE sensor_id = p_sensor_id
                GROUP BY type) t),
         pl.obs_total,
         (SELECT coalesce(sum(ap_obs), 0) FROM sensor_hourly WHERE sensor_id = p_sensor_id),
         (SELECT mode() WITHIN GROUP (ORDER BY country) FROM devices
          WHERE sensor_id = p_sensor_id AND type = 'ap' AND country IS NOT NULL),
         (SELECT count(*) FROM alerts WHERE sensor_id = p_sensor_id),
         (SELECT max(ts) FROM alerts WHERE sensor_id = p_sensor_id),
         (SELECT count(*) FROM ap_baselines WHERE sensor_id = p_sensor_id AND rssi_median IS NOT NULL),
         v_now,
         (SELECT max(ts) FROM polls
          WHERE sensor_id = p_sensor_id AND ok AND ds_running AND devices_active > 0),
         lp.ds_hop_n, lp.ds_hop_visited, lp.ds_hop_ok
  FROM (SELECT min(ts) AS first_poll, max(ts) AS last_poll, count(*) AS polls,
               count(*) FILTER (WHERE ok) AS polls_ok, coalesce(sum(new_obs), 0) AS obs_total
        FROM polls WHERE sensor_id = p_sensor_id) pl
  LEFT JOIN LATERAL (SELECT ds_hop_n, ds_hop_visited, ds_hop_ok FROM polls
                     WHERE sensor_id = p_sensor_id ORDER BY ts DESC LIMIT 1) lp ON true
  ON CONFLICT (sensor_id) DO UPDATE SET
      first_poll = EXCLUDED.first_poll, last_poll = EXCLUDED.last_poll,
      polls = EXCLUDED.polls, polls_ok = EXCLUDED.polls_ok,
      gap_count = EXCLUDED.gap_count, gap_total_s = EXCLUDED.gap_total_s,
      gap_max_s = EXCLUDED.gap_max_s, gaps = EXCLUDED.gaps,
      devices_by_type = EXCLUDED.devices_by_type, obs_total = EXCLUDED.obs_total,
      obs_ap = EXCLUDED.obs_ap, country_expected = EXCLUDED.country_expected,
      alerts = EXCLUDED.alerts, alerts_last_ts = EXCLUDED.alerts_last_ts,
      baselines = EXCLUDED.baselines, refreshed_at = EXCLUDED.refreshed_at,
      last_capture_at = EXCLUDED.last_capture_at, last_hop_n = EXCLUDED.last_hop_n,
      last_hop_visited = EXCLUDED.last_hop_visited, last_hop_ok = EXCLUDED.last_hop_ok;

  -- 7. outages and limited capture (refresh_sensor_periods())
  PERFORM refresh_sensor_periods(p_sensor_id);

  -- 8. the derived events (Kismet episodes, security changes, sensor outages,
  --    the classification rule); never their status
  PERFORM refresh_events(p_sensor_id);

  RETURN QUERY SELECT v_sfrom, v_ap_hours, v_sensor_hours, v_aps, v_now;
END
$$;

-- --- the dashboard's account-level AP (schema 9) -----------------------------------------
--
-- One row per account AP with its current attributes: the identity and
-- configuration of its newest sensor row, the network it belongs to (SSID, or
-- the name seen for a hidden AP; a hidden AP without one is its own network),
-- mobile flag, effective classification and its source. Computed per request
-- (~850 APs); the api pages and filters on it.
CREATE OR REPLACE VIEW account_aps AS
SELECT a.id AS ap_id, a.account_id, a.bssid, trunc(a.bssid) AS oui, a.first_seen, a.last_seen,
       a.classification AS manual_classification, a.classified_at, a.note, a.note_at,
       c.sensor_id, c.device_key, c.ssid, c.hidden, c.name_seen,
       coalesce(nullif(c.ssid, ''), c.name_seen) AS network_name,
       coalesce(nullif(c.ssid, ''), c.name_seen, 'hidden:' || upper(a.bssid::text)) AS network_key,
       c.manuf, c.random_bssid, c.adv_channel, band_of_channel(c.adv_channel) AS band, c.ht_mode, c.country,
       c.crypt, c.crypt_bits_known, c.mfp_sup, c.mfp_req, c.security, c.security_label,
       c.cur_n_clients, c.cur_qbss_stations, c.cur_util_pct, c.cur_at,
       n.id AS network_id, n.kind AS network_kind,
       sen.sensor_ids, sen.rssi_typical,
       ap_is_mobile(a.account_id, c.random_bssid, c.manuf, a.first_seen, a.last_seen, lat.latest) AS mobile,
       ap_classification(a.account_id, a.classification, coalesce(nullif(c.ssid, ''), c.name_seen),
                         a.bssid, c.random_bssid, sen.rssi_typical) AS classification,
       CASE WHEN a.classification IS NOT NULL THEN 'manual' ELSE 'rule' END AS classified_by
FROM aps a
JOIN LATERAL (SELECT s.* FROM ap_sensors x
              JOIN ap_summary s ON s.sensor_id = x.sensor_id AND s.device_key = x.device_key
              WHERE x.ap_id = a.id
              ORDER BY s.last_seen DESC, s.sensor_id LIMIT 1) c ON true
JOIN LATERAL (SELECT array_agg(x.sensor_id ORDER BY x.sensor_id) AS sensor_ids,
                     max(s.rssi_typical) AS rssi_typical
              FROM ap_sensors x
              JOIN ap_summary s ON s.sensor_id = x.sensor_id AND s.device_key = x.device_key
              WHERE x.ap_id = a.id) sen ON true
LEFT JOIN networks n ON n.account_id = a.account_id AND n.ssid = coalesce(nullif(c.ssid, ''), c.name_seen)
LEFT JOIN LATERAL (SELECT max(ss.last_capture_at) AS latest
                   FROM sensor_summary ss JOIN sensors se ON se.id = ss.sensor_id
                   WHERE se.account_id = a.account_id) lat ON true;
COMMENT ON VIEW account_aps IS
  'One row per account AP (schema 9): current identity/configuration from its newest sensor row, network, security, mobile flag, effective classification (manual, else the rules of account_settings).';

COMMIT;

-- --- later: TimescaleDB (NOT enabled; zero overhead for the seeded demo) --------------
-- The primary keys above already contain ts, which is what a hypertable's unique
-- index requires, so the conversion is a rewrite-free one-liner per table:
--
--   CREATE EXTENSION IF NOT EXISTS timescaledb;
--   SELECT create_hypertable('observations', 'ts',
--                            chunk_time_interval => INTERVAL '7 days', migrate_data => true);
--   SELECT create_hypertable('polls', 'ts',
--                            chunk_time_interval => INTERVAL '30 days', migrate_data => true);
--   -- then continuous aggregates: per-AP hourly RSSI median / n_obs, per-day AP activity.
