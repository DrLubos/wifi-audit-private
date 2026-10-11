-- Server schema 8 -> 9: the dashboard v2 data model
-- (server/docs/dashboard-v2/PLAN.md section 4.3). ONE transaction: any error
-- leaves schema 8 exactly as it was. Run through ops/migrate_schema9.sh
-- (afterwards it applies schema.sql - functions, views -, seeds the sensor
-- history, runs a FULL refresh_rollups() per sensor and the fixture tests).
--
-- What happens:
--   1. checks: schema 8, no accounts table yet; lock_timeout 10 s
--   2. accounts (row 1 'Account' - the developer names it in
--      ops/seed_account.example.sql), sensors.account_id (default 1) and
--      sensors.hardware
--   3. the configuration tables: account_settings (row for account 1),
--      networks, aps, ap_sensors, sensor_annotations, sensor_config,
--      sensor_uploads, kismet_alert_rules (seeded), audit_log
--   4. the derived tables ap_security_periods, sensor_periods (filled by the
--      refresh afterwards)
--   5. detections becomes the event table: kind, status (from acked; 0 acked
--      rows live on 2026-10-11), status_at/status_note (renamed from
--      acked_at/ack_note), ts_end, updated_at; acked dropped; indexes
--   6. new rollup columns (sensor_hourly, sensor_summary, ap_summary) and the
--      hour index of ap_rssi_hourly
--   7. schema_meta 9
-- The table definitions are the ones of schema.sql
-- (tests/test_schema_files.py keeps them equal).
\set ON_ERROR_STOP on
\pset pager off
\timing on
SET lock_timeout = '10s';

BEGIN;

\echo [1/7] checks
DO $$
DECLARE v text := (SELECT value FROM schema_meta WHERE key = 'schema_version');
BEGIN
  IF v IS DISTINCT FROM '8' THEN
    RAISE EXCEPTION 'migrate_schema9: expected schema_version 8, found %', v;
  END IF;
  IF to_regclass('accounts') IS NOT NULL THEN
    RAISE EXCEPTION 'migrate_schema9: accounts already exists';
  END IF;
END $$;

\echo [2/7] accounts, sensors.account_id, sensors.hardware
CREATE TABLE accounts (
  id            serial PRIMARY KEY,
  name          text NOT NULL UNIQUE,
  tz            text NOT NULL DEFAULT 'Europe/Bratislava',
  write_version bigint NOT NULL DEFAULT 0,     -- bumped by every write endpoint (part of the ETags)
  created_at    timestamptz NOT NULL DEFAULT now());
INSERT INTO accounts (id, name) VALUES (1, 'Account');
SELECT setval(pg_get_serial_sequence('accounts', 'id'), 1);
ALTER TABLE sensors
  ADD COLUMN account_id integer NOT NULL DEFAULT 1 REFERENCES accounts(id),
  ADD COLUMN hardware text;

\echo [3/7] configuration tables
CREATE TABLE account_settings (
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
CREATE TABLE networks (
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
CREATE TABLE aps (
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
CREATE TABLE ap_sensors (
  ap_id      bigint NOT NULL REFERENCES aps(id),
  sensor_id  integer NOT NULL,
  device_key text NOT NULL,
  PRIMARY KEY (sensor_id, device_key),
  FOREIGN KEY (sensor_id, device_key) REFERENCES devices (sensor_id, device_key));
CREATE INDEX ap_sensors_ap ON ap_sensors (ap_id);
INSERT INTO account_settings (account_id) VALUES (1);
CREATE TABLE sensor_annotations (
  sensor_id integer NOT NULL REFERENCES sensors(id),
  starts_at timestamptz NOT NULL,
  ends_at   timestamptz,                       -- NULL = a point in time (a restart, a change)
  kind      text NOT NULL CHECK (kind IN ('gap', 'no_frames', 'coverage', 'restart', 'change')),
  boundary  boolean NOT NULL DEFAULT false,    -- a data boundary (a channel-plan change)
  label     text NOT NULL,
  ref       text,                              -- e.g. 'findings.md section 6'
  PRIMARY KEY (sensor_id, starts_at, kind));
CREATE TABLE sensor_config (
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
CREATE TABLE sensor_uploads (
  id             bigserial PRIMARY KEY,
  sensor_id      integer NOT NULL REFERENCES sensors(id),
  received_at    timestamptz NOT NULL DEFAULT now(),
  via            text NOT NULL CHECK (via IN ('snapshot', 'ingest')),
  buffer_version text,
  polls_from     timestamptz,
  polls_to       timestamptz,
  buffer_bytes   bigint,                       -- size of the sensor's buffer (export_snapshot.py meta)
  rows           jsonb NOT NULL DEFAULT '{}');
CREATE INDEX sensor_uploads_sensor ON sensor_uploads (sensor_id, received_at);
CREATE TABLE kismet_alert_rules (
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
CREATE TABLE audit_log (
  id         bigserial PRIMARY KEY,
  at         timestamptz NOT NULL DEFAULT now(),
  account_id integer NOT NULL REFERENCES accounts(id),
  action     text NOT NULL,                  -- e.g. 'event.status', 'ap.classification'
  target     text,
  old        jsonb,
  new        jsonb);

\echo [4/7] derived tables
CREATE TABLE ap_security_periods (
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
CREATE TABLE sensor_periods (
  sensor_id integer NOT NULL REFERENCES sensors(id),
  starts_at timestamptz NOT NULL,            -- an outage after a poll gap starts at the last good poll
  ends_at   timestamptz,                     -- NULL = still going at the sensor's last poll
  kind      text NOT NULL CHECK (kind IN ('outage', 'limited')),
  polls     integer NOT NULL,                -- polls in the period in that state (not collecting / limited)
  PRIMARY KEY (sensor_id, starts_at, kind));

\echo [5/7] detections -> the event table
SELECT acked, count(*) AS rows_before FROM detections GROUP BY 1 ORDER BY 1;
ALTER TABLE detections
  ADD COLUMN kind text NOT NULL DEFAULT 'detection' CHECK (kind IN ('detection', 'kismet', 'config', 'sensor')),
  ADD COLUMN ts_end timestamptz,
  ADD COLUMN status text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'confirmed', 'false_positive')),
  ADD COLUMN updated_at timestamptz;
UPDATE detections SET status = CASE WHEN acked THEN 'confirmed' ELSE 'open' END,
                      updated_at = created_at;
ALTER TABLE detections ALTER COLUMN updated_at SET NOT NULL,
                       ALTER COLUMN updated_at SET DEFAULT now();
ALTER TABLE detections RENAME COLUMN acked_at TO status_at;
ALTER TABLE detections RENAME COLUMN ack_note TO status_note;
DROP INDEX detections_open;
ALTER TABLE detections DROP COLUMN acked;
CREATE INDEX detections_open ON detections (sensor_id, severity) WHERE status = 'open';
CREATE INDEX detections_feed ON detections (ts DESC, id DESC);
DROP INDEX detections_dedupe;
CREATE UNIQUE INDEX detections_dedupe
  ON detections (sensor_id, type, coalesce(device_key, upper(mac::text)), ts) NULLS NOT DISTINCT;
SELECT kind, status, count(*) AS rows_after FROM detections GROUP BY 1, 2 ORDER BY 1, 2;

\echo [6/7] rollup columns, ap_rssi_hourly hour index
ALTER TABLE sensor_hourly
  ADD COLUMN capture_polls integer NOT NULL DEFAULT 0,
  ADD COLUMN limited_polls integer NOT NULL DEFAULT 0;
ALTER TABLE sensor_summary
  ADD COLUMN last_capture_at timestamptz,
  ADD COLUMN last_hop_n integer,
  ADD COLUMN last_hop_visited integer,
  ADD COLUMN last_hop_ok boolean;
ALTER TABLE ap_summary
  ADD COLUMN security text,
  ADD COLUMN security_label text,
  ADD COLUMN crypt_bits_known bigint,
  ADD COLUMN rssi_typical real;
CREATE INDEX ap_rssi_hourly_hour
  ON ap_rssi_hourly (sensor_id, hour) INCLUDE (device_key, n_obs, n_valid, median);

\echo [7/7] schema 9
INSERT INTO schema_meta (key, value) VALUES ('schema_version', '9')
  ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;

COMMIT;
SELECT clock_timestamp()::timestamptz(0) AS committed;
