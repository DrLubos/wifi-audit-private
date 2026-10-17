-- Server schema 13 -> 14 (schema.sql, schema 14): live upload from the sensor
-- (docs/live-upload.md). ONE transaction: any error leaves schema 13 as it is.
-- Run through ops/migrate_schema14.sh (afterwards it applies schema.sql and
-- runs the fixture tests). Additive: new tables and functions, new columns
-- without defaults (metadata only - no rewrite of polls), a wider annotation
-- kind check, autovacuum settings. Nothing existing is read differently,
-- except that the snapshot import now merges through ingest_merge().
--
--   1. checks: schema 13; lock_timeout 10 s
--   2. the live-upload tables: sensor_tokens, sensor_status,
--      sensor_heartbeats, ingest_state, sensor_clock_boots, sensor_seq_gaps
--   3. polls.seq / received_at / ts_offset_s; the batch columns of
--      sensor_uploads and their unique key
--   4. sensor_annotations kinds 'lost' and 'unreachable'
--   5. autovacuum settings of observations, devices, ap_load_hourly
--   6. ap_evidence() - devices_ever_ap() reads it (same rule as before)
--   7. ingest_stage_create(), ingest_merge(), refresh_contact_periods()
--   8. schema_meta 14
\set ON_ERROR_STOP on
\pset pager off

BEGIN;
SET LOCAL lock_timeout = '10s';
DO $$
BEGIN
  IF (SELECT value FROM schema_meta WHERE key = 'schema_version') IS DISTINCT FROM '13' THEN
    RAISE EXCEPTION 'migrate_schema14: expected schema_version 13';
  END IF;
END $$;

\echo [2/8] live-upload tables
-- --- live upload (schema 14; docs/live-upload.md) -----------------------------------------

-- Per-sensor ingest tokens. A token is wsi1.<id>.<64 hex>: the id is public (it
-- attributes a wrong secret to its sensor), the server keeps only the SHA-256
-- of the whole token. Created and revoked by ops/sensor_token.sh; deleting a
-- sensor deletes its tokens with its configuration.
CREATE TABLE sensor_tokens (
  id           serial PRIMARY KEY,
  sensor_id    integer NOT NULL REFERENCES sensors(id),
  token_hash   bytea NOT NULL UNIQUE CHECK (length(token_hash) = 32),
  label        text,
  created_at   timestamptz NOT NULL DEFAULT now(),
  last_used_at timestamptz,
  revoked_at   timestamptz);

-- The sensor's latest contact: one row, written by every authenticated ingest
-- request (batch or heartbeat) - the Sensors page reads it directly, never
-- through the rollups. reject_* / auth_fail_* track a stall until it opens a
-- sensor event (docs/live-upload.md 6.5).
CREATE TABLE sensor_status (
  sensor_id       integer PRIMARY KEY REFERENCES sensors(id),
  last_contact_at timestamptz NOT NULL,
  last_batch_at   timestamptz,
  last_batch_seq  bigint,
  buffer_id       uuid,
  heartbeat       jsonb NOT NULL DEFAULT '{}',   -- the latest heartbeat as received (validated)
  reject_seq      bigint,                        -- the batch the server keeps refusing (422)
  reject_since    timestamptz,
  reject_count    integer NOT NULL DEFAULT 0,
  reject_reason   text,
  auth_fail_since timestamptz,                   -- wrong secret or revoked token since
  auth_fail_count integer NOT NULL DEFAULT 0,
  updated_at      timestamptz NOT NULL DEFAULT now());

-- Heartbeat history, at most one row a minute per sensor (~0.3 MB/day): the
-- contact periods (refresh_contact_periods()) and the thesis measurements
-- (clock offset, backlog, replay).
CREATE TABLE sensor_heartbeats (
  sensor_id         integer NOT NULL REFERENCES sensors(id),
  received_at       timestamptz NOT NULL,
  sensor_ts         timestamptz NOT NULL,        -- the sensor's corrected clock at sending
  rtt_ms            integer,                     -- of the uploader's previous request
  clock_offset_s    real,                        -- received_at - sensor_ts - rtt/2
  chrony_synced     boolean,
  chrony_offset_s   real,
  boot_id           uuid,
  uptime_s          bigint,
  last_poll_ts      timestamptz,
  backlog_polls     integer,                     -- polls not yet acknowledged
  backlog_oldest_ts timestamptz,
  held_polls        integer,                     -- waiting for their boot's clock anchor
  unverified_polls  integer,                     -- of boots that ended without an anchor
  buffer_oldest_ts  timestamptz,
  buffer_bytes      bigint,
  disk_free_bytes   bigint,
  uploader_version  text,
  PRIMARY KEY (sensor_id, received_at));

-- What the refresher still has to do: the earliest ts an ingest inserted since
-- the last refresh_rollups() (NULL = nothing), since when it is waiting, and
-- the last time an ingest moved it (the refresher clears it only if unchanged).
CREATE TABLE ingest_state (
  sensor_id   integer PRIMARY KEY REFERENCES sensors(id),
  dirty_from  timestamptz,
  dirty_since timestamptz,
  dirty_at    timestamptz);

-- One row per boot whose polls the uploader corrected (docs/live-upload.md 8.3):
-- the true epoch of the boot (wall - CLOCK_MONOTONIC at the anchor), the stale
-- epoch the clock ran on before, the step's monotonic time, the offsets applied.
CREATE TABLE sensor_clock_boots (
  sensor_id       integer NOT NULL REFERENCES sensors(id),
  boot_id         uuid NOT NULL,
  source          text NOT NULL CHECK (source IN ('ntp', 'server')),
  epoch_true      double precision NOT NULL,
  epoch_stale     double precision,
  step_mono       double precision,
  polls_corrected integer NOT NULL DEFAULT 0,
  offset_min_s    real,
  offset_max_s    real,
  recorded_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (sensor_id, boot_id));

-- Polls of a buffer that never arrived, by their buffer row ids: 'lost' (pruned
-- on the sensor before the upload, or a gap the server found) or 'unverified'
-- (a boot that ended before its clock could be anchored - not uploaded).
CREATE TABLE sensor_seq_gaps (
  sensor_id   integer NOT NULL REFERENCES sensors(id),
  buffer_id   uuid NOT NULL,
  seq_from    bigint NOT NULL,
  seq_to      bigint NOT NULL,
  reason      text NOT NULL CHECK (reason IN ('lost', 'unverified')),
  polls       integer NOT NULL,
  raw_from    timestamptz,                       -- the sensor's raw clock, for 'unverified' only indicative
  raw_to      timestamptz,
  recorded_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (sensor_id, buffer_id, seq_from));

\echo [3/8] polls and sensor_uploads columns
ALTER TABLE polls ADD COLUMN seq bigint;
ALTER TABLE polls ADD COLUMN received_at timestamptz;
ALTER TABLE polls ADD COLUMN ts_offset_s real;
ALTER TABLE sensor_uploads ADD COLUMN buffer_id uuid;
ALTER TABLE sensor_uploads ADD COLUMN batch_seq bigint;
ALTER TABLE sensor_uploads ADD COLUMN sent_at timestamptz;
ALTER TABLE sensor_uploads ADD COLUMN attempt integer;
ALTER TABLE sensor_uploads ADD COLUMN replays integer NOT NULL DEFAULT 0;
ALTER TABLE sensor_uploads ADD COLUMN bytes integer;
ALTER TABLE sensor_uploads ADD COLUMN duration_ms integer;
ALTER TABLE sensor_uploads ADD CONSTRAINT sensor_uploads_batch UNIQUE (sensor_id, buffer_id, batch_seq);

\echo [4/8] annotation kinds
ALTER TABLE sensor_annotations DROP CONSTRAINT sensor_annotations_kind_check;
ALTER TABLE sensor_annotations ADD CONSTRAINT sensor_annotations_kind_check
  CHECK (kind IN ('gap', 'no_frames', 'coverage', 'restart', 'change', 'hardware_change',
                  'lost', 'unreachable'));

\echo [5/8] autovacuum settings
ALTER TABLE observations SET (autovacuum_vacuum_insert_threshold = 20000,
                              autovacuum_vacuum_insert_scale_factor = 0);
ALTER TABLE devices SET (autovacuum_vacuum_scale_factor = 0.05);
ALTER TABLE ap_load_hourly SET (autovacuum_vacuum_scale_factor = 0.05);

\echo [6/8] ap_evidence()
CREATE OR REPLACE FUNCTION ap_evidence(p_type text, p_ssid text, p_crypt text, p_adv_channel text)
RETURNS boolean
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT coalesce(p_type = 'ap', false) OR p_ssid IS NOT NULL OR p_crypt IS NOT NULL OR p_adv_channel IS NOT NULL
$$;

CREATE OR REPLACE FUNCTION devices_ever_ap() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'UPDATE' THEN             -- OLD exists only for an update
    IF OLD.ever_ap THEN
      NEW.ever_ap := true;
    END IF;
  END IF;
  NEW.ever_ap := NEW.ever_ap OR ap_evidence(NEW.type, NEW.ssid, NEW.crypt, NEW.adv_channel);
  RETURN NEW;
END
$$;

\echo [7/8] ingest functions
CREATE OR REPLACE FUNCTION ingest_stage_create()
RETURNS void
LANGUAGE plpgsql
SET client_min_messages = warning       -- no "already exists, skipping" notice on every call
AS $$
BEGIN
  CREATE TEMP TABLE IF NOT EXISTS stage_devices (device_key text, mac text, type text, manuf text,
    first_seen bigint, last_seen bigint, ssid text, cloaked integer, crypt text, crypt_bits bigint,
    mfp_sup integer, mfp_req integer, adv_channel text, ht_mode text, beacon_rate integer,
    country text, config_changed_at bigint, cur_n_clients integer, cur_qbss_stations integer,
    cur_util_pct real, cur_at bigint) ON COMMIT DELETE ROWS;
  CREATE TEMP TABLE IF NOT EXISTS stage_polls (seq bigint, ts bigint, ts_offset_s real,
    kismet_ts bigint, devices_total integer, devices_active integer, new_obs integer,
    ds_running integer, ds_error text, ds_packets bigint, ds_hop_n integer,
    ds_hop_visited integer, ds_hop_ok integer, duration_ms integer, ok integer,
    error text) ON COMMIT DELETE ROWS;
  CREATE TEMP TABLE IF NOT EXISTS stage_device_config_history (device_key text, ts bigint,
    ssid text, cloaked integer, crypt text, crypt_bits bigint, mfp_sup integer, mfp_req integer,
    adv_channel text, ht_mode text, beacon_rate integer, country text) ON COMMIT DELETE ROWS;
  CREATE TEMP TABLE IF NOT EXISTS stage_observations (ts bigint, device_key text, rssi smallint,
    rssi_floor integer, pk_total bigint, pk_data bigint, disconnects integer,
    disconnects_last bigint, bss_timestamp bigint) ON COMMIT DELETE ROWS;
  CREATE TEMP TABLE IF NOT EXISTS stage_ap_load_hourly (device_key text, hour bigint,
    channel text, polls integer, clients_polls integer, clients_max integer, clients_sum integer,
    qbss_polls integer, qbss_max integer, qbss_sum integer, util_max real,
    util_sum real) ON COMMIT DELETE ROWS;
  CREATE TEMP TABLE IF NOT EXISTS stage_load_samples (ts timestamptz, device_key text,
    n_clients integer, qbss_stations integer, util_pct real) ON COMMIT DELETE ROWS;
  CREATE TEMP TABLE IF NOT EXISTS stage_device_freq_hist (device_key text, freq_khz integer,
    packets bigint, updated_ts bigint) ON COMMIT DELETE ROWS;
  CREATE TEMP TABLE IF NOT EXISTS stage_associations (ap_key text, client_mac text,
    first_seen bigint, last_seen bigint) ON COMMIT DELETE ROWS;
  CREATE TEMP TABLE IF NOT EXISTS stage_client_bssids (device_key text, bssid text,
    first_seen bigint, last_seen bigint) ON COMMIT DELETE ROWS;
  CREATE TEMP TABLE IF NOT EXISTS stage_probes (device_key text, ssid text, first_time bigint,
    last_time bigint) ON COMMIT DELETE ROWS;
  CREATE TEMP TABLE IF NOT EXISTS stage_alerts (hash text, ts double precision, header text,
    class text, severity smallint, source_mac text, dest_mac text, transmitter_mac text,
    other_mac text, channel text, freq_khz integer, device_key text, text text,
    raw text) ON COMMIT DELETE ROWS;
END
$$;

CREATE OR REPLACE FUNCTION ingest_merge(p_sensor_id integer, p_via text)
RETURNS jsonb
LANGUAGE plpgsql
SET work_mem = '64MB'      -- a snapshot stages ~2.5M observation rows
SET jit = off
AS $$
DECLARE
  v_staged    jsonb := '{}';
  v_inserted  jsonb := '{}';
  v_conflicts jsonb := '{}';
  v_s   bigint;
  v_n   bigint;
  v_t   timestamptz;
  v_min timestamptz;
  v_any boolean := false;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM sensors WHERE id = p_sensor_id) THEN
    RAISE EXCEPTION 'ingest_merge: unknown sensor_id %', p_sensor_id;
  END IF;
  IF p_via IS NULL OR p_via NOT IN ('snapshot', 'ingest') THEN
    RAISE EXCEPTION 'ingest_merge: via must be snapshot or ingest, not %', p_via;
  END IF;
  PERFORM ingest_stage_create();
  PERFORM pg_advisory_xact_lock(hashtext('ingest_merge'), p_sensor_id);

  -- 1. devices (every other table references them)
  SELECT count(*) INTO v_s FROM stage_devices;
  IF v_s > 0 THEN
    INSERT INTO devices (sensor_id, device_key, mac, type, manuf, first_seen, last_seen, ssid,
                         cloaked, crypt, crypt_bits, mfp_sup, mfp_req, adv_channel, ht_mode,
                         beacon_rate, country, config_changed_at, cur_n_clients,
                         cur_qbss_stations, cur_util_pct, cur_at)
    SELECT p_sensor_id, s.device_key, s.mac::macaddr, s.type, s.manuf, to_timestamp(s.first_seen),
           to_timestamp(s.last_seen), s.ssid, s.cloaked::boolean, s.crypt, s.crypt_bits,
           s.mfp_sup::boolean, s.mfp_req::boolean, s.adv_channel, s.ht_mode, s.beacon_rate,
           s.country, to_timestamp(s.config_changed_at), s.cur_n_clients, s.cur_qbss_stations,
           s.cur_util_pct, to_timestamp(s.cur_at)
    FROM stage_devices s
    WHERE p_via = 'snapshot'
       OR ap_evidence(s.type, s.ssid, s.crypt, s.adv_channel)
       OR EXISTS (SELECT 1 FROM devices k
                  WHERE k.sensor_id = p_sensor_id AND k.device_key = s.device_key AND k.ever_ap)
    ON CONFLICT (sensor_id, device_key) DO UPDATE SET
      -- every SET expression sees the stored row, so all compare with the old last_seen
      mac = CASE WHEN EXCLUDED.last_seen >= devices.last_seen THEN EXCLUDED.mac ELSE devices.mac END,
      type = CASE WHEN EXCLUDED.last_seen >= devices.last_seen THEN EXCLUDED.type ELSE devices.type END,
      manuf = coalesce(CASE WHEN EXCLUDED.last_seen >= devices.last_seen THEN EXCLUDED.manuf END,
                       devices.manuf, EXCLUDED.manuf),
      first_seen = least(devices.first_seen, EXCLUDED.first_seen),
      ssid = CASE WHEN EXCLUDED.last_seen >= devices.last_seen THEN EXCLUDED.ssid ELSE devices.ssid END,
      cloaked = CASE WHEN EXCLUDED.last_seen >= devices.last_seen THEN EXCLUDED.cloaked ELSE devices.cloaked END,
      crypt = CASE WHEN EXCLUDED.last_seen >= devices.last_seen THEN EXCLUDED.crypt ELSE devices.crypt END,
      crypt_bits = CASE WHEN EXCLUDED.last_seen >= devices.last_seen THEN EXCLUDED.crypt_bits ELSE devices.crypt_bits END,
      mfp_sup = CASE WHEN EXCLUDED.last_seen >= devices.last_seen THEN EXCLUDED.mfp_sup ELSE devices.mfp_sup END,
      mfp_req = CASE WHEN EXCLUDED.last_seen >= devices.last_seen THEN EXCLUDED.mfp_req ELSE devices.mfp_req END,
      adv_channel = CASE WHEN EXCLUDED.last_seen >= devices.last_seen THEN EXCLUDED.adv_channel ELSE devices.adv_channel END,
      ht_mode = CASE WHEN EXCLUDED.last_seen >= devices.last_seen THEN EXCLUDED.ht_mode ELSE devices.ht_mode END,
      beacon_rate = CASE WHEN EXCLUDED.last_seen >= devices.last_seen THEN EXCLUDED.beacon_rate ELSE devices.beacon_rate END,
      country = CASE WHEN EXCLUDED.last_seen >= devices.last_seen THEN EXCLUDED.country ELSE devices.country END,
      config_changed_at = CASE WHEN EXCLUDED.last_seen >= devices.last_seen
                               THEN coalesce(EXCLUDED.config_changed_at, devices.config_changed_at)
                               ELSE devices.config_changed_at END,
      cur_n_clients = CASE WHEN EXCLUDED.cur_at IS NOT NULL AND (devices.cur_at IS NULL OR EXCLUDED.cur_at >= devices.cur_at)
                           THEN EXCLUDED.cur_n_clients ELSE devices.cur_n_clients END,
      cur_qbss_stations = CASE WHEN EXCLUDED.cur_at IS NOT NULL AND (devices.cur_at IS NULL OR EXCLUDED.cur_at >= devices.cur_at)
                               THEN EXCLUDED.cur_qbss_stations ELSE devices.cur_qbss_stations END,
      cur_util_pct = CASE WHEN EXCLUDED.cur_at IS NOT NULL AND (devices.cur_at IS NULL OR EXCLUDED.cur_at >= devices.cur_at)
                          THEN EXCLUDED.cur_util_pct ELSE devices.cur_util_pct END,
      cur_at = CASE WHEN EXCLUDED.cur_at IS NOT NULL AND (devices.cur_at IS NULL OR EXCLUDED.cur_at >= devices.cur_at)
                    THEN EXCLUDED.cur_at ELSE devices.cur_at END,
      last_seen = greatest(devices.last_seen, EXCLUDED.last_seen);
    GET DIAGNOSTICS v_n = ROW_COUNT;
    v_staged := v_staged || jsonb_build_object('devices', v_s);
    v_inserted := v_inserted || jsonb_build_object('devices', v_n);
    v_conflicts := v_conflicts || jsonb_build_object('devices', v_s - v_n);
    v_any := v_any OR v_n > 0;
  END IF;

  -- 2. polls
  SELECT count(*) INTO v_s FROM stage_polls;
  IF v_s > 0 THEN
    WITH ins AS (
      INSERT INTO polls (sensor_id, ts, kismet_ts, devices_total, devices_active, new_obs, ds_running,
                         ds_error, ds_packets, ds_hop_n, ds_hop_visited, ds_hop_ok, duration_ms, ok,
                         error, seq, received_at, ts_offset_s)
      SELECT p_sensor_id, to_timestamp(s.ts), to_timestamp(s.kismet_ts), s.devices_total,
             s.devices_active, s.new_obs, s.ds_running::boolean, s.ds_error, s.ds_packets, s.ds_hop_n,
             s.ds_hop_visited, s.ds_hop_ok::boolean, s.duration_ms, s.ok::boolean, s.error, s.seq,
             now(), s.ts_offset_s
      FROM stage_polls s
      ORDER BY s.ts
      ON CONFLICT DO NOTHING
      RETURNING ts)
    SELECT count(*), min(ts) INTO v_n, v_t FROM ins;
    v_staged := v_staged || jsonb_build_object('polls', v_s);
    v_inserted := v_inserted || jsonb_build_object('polls', v_n);
    v_conflicts := v_conflicts || jsonb_build_object('polls', v_s - v_n);
    v_min := least(v_min, v_t);
    v_any := v_any OR v_n > 0;
    IF p_via = 'snapshot' THEN
      -- polls stored before schema 14 learn their buffer row id from a later snapshot
      UPDATE polls p SET seq = s.seq
      FROM stage_polls s
      WHERE p.sensor_id = p_sensor_id AND p.ts = to_timestamp(s.ts) AND p.seq IS NULL
        AND s.seq IS NOT NULL;
      GET DIAGNOSTICS v_n = ROW_COUNT;
      v_inserted := v_inserted || jsonb_build_object('polls_seq_filled', v_n);
    END IF;
  END IF;

  -- 3. the configuration history (before the load samples: their channel comes from it)
  SELECT count(*) INTO v_s FROM stage_device_config_history;
  IF v_s > 0 THEN
    INSERT INTO device_config_history (sensor_id, device_key, ts, ssid, cloaked, crypt, crypt_bits,
                                       mfp_sup, mfp_req, adv_channel, ht_mode, beacon_rate, country)
    SELECT p_sensor_id, s.device_key, to_timestamp(s.ts), s.ssid, s.cloaked::boolean, s.crypt,
           s.crypt_bits, s.mfp_sup::boolean, s.mfp_req::boolean, s.adv_channel, s.ht_mode,
           s.beacon_rate, s.country
    FROM stage_device_config_history s
    JOIN devices d ON d.sensor_id = p_sensor_id AND d.device_key = s.device_key
                  AND (p_via = 'snapshot' OR d.ever_ap)
    ON CONFLICT DO NOTHING;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    v_staged := v_staged || jsonb_build_object('device_config_history', v_s);
    v_inserted := v_inserted || jsonb_build_object('device_config_history', v_n);
    v_conflicts := v_conflicts || jsonb_build_object('device_config_history', v_s - v_n);
    v_any := v_any OR v_n > 0;
  END IF;

  -- 4. observations: AP rows only (devices.ever_ap, schema 10)
  SELECT count(*) INTO v_s FROM stage_observations;
  IF v_s > 0 THEN
    WITH ins AS (
      INSERT INTO observations (ts, pk_total, pk_data, bss_timestamp, disconnects_last, sensor_id,
                                disconnects, rssi, rssi_floor, device_key)
      SELECT to_timestamp(s.ts), s.pk_total, s.pk_data, s.bss_timestamp,
             to_timestamp(s.disconnects_last), p_sensor_id, s.disconnects, s.rssi,
             s.rssi_floor::boolean, s.device_key
      FROM stage_observations s
      JOIN devices d ON d.sensor_id = p_sensor_id AND d.device_key = s.device_key AND d.ever_ap
      ORDER BY s.ts, s.device_key
      ON CONFLICT DO NOTHING
      RETURNING ts)
    SELECT count(*), min(ts) INTO v_n, v_t FROM ins;
    v_staged := v_staged || jsonb_build_object('observations', v_s);
    v_inserted := v_inserted || jsonb_build_object('observations', v_n);
    v_conflicts := v_conflicts || jsonb_build_object('observations', v_s - v_n);
    v_min := least(v_min, v_t);
    v_any := v_any OR v_n > 0;
  END IF;

  -- 5. AP load per hour (buffer v7): the row with more polls wins
  SELECT count(*) INTO v_s FROM stage_ap_load_hourly;
  IF v_s > 0 THEN
    INSERT INTO ap_load_hourly AS t (sensor_id, device_key, hour, channel, polls, clients_polls,
                                     clients_max, clients_sum, qbss_polls, qbss_max, qbss_sum,
                                     util_max, util_sum, source)
    SELECT p_sensor_id, s.device_key, to_timestamp(s.hour), s.channel, s.polls, s.clients_polls,
           s.clients_max, s.clients_sum, s.qbss_polls, s.qbss_max, s.qbss_sum, s.util_max,
           s.util_sum, 'buffer'
    FROM stage_ap_load_hourly s
    JOIN devices d ON d.sensor_id = p_sensor_id AND d.device_key = s.device_key AND d.ever_ap
    ON CONFLICT (sensor_id, device_key, hour, channel) DO UPDATE SET
      polls = EXCLUDED.polls, clients_polls = EXCLUDED.clients_polls,
      clients_max = EXCLUDED.clients_max, clients_sum = EXCLUDED.clients_sum,
      qbss_polls = EXCLUDED.qbss_polls, qbss_max = EXCLUDED.qbss_max,
      qbss_sum = EXCLUDED.qbss_sum, util_max = EXCLUDED.util_max,
      util_sum = EXCLUDED.util_sum, source = EXCLUDED.source
      WHERE EXCLUDED.polls > t.polls;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    v_staged := v_staged || jsonb_build_object('ap_load_hourly', v_s);
    v_inserted := v_inserted || jsonb_build_object('ap_load_hourly', v_n);
    v_any := v_any OR v_n > 0;
  END IF;

  -- 6. per-poll AP load samples (buffers <= v5; schema 8) - the same fuller-wins rule
  SELECT count(*) INTO v_s FROM stage_load_samples;
  IF v_s > 0 THEN
    SELECT coalesce(sum(l.inserted + l.updated), 0) INTO v_n FROM load_ap_load_samples(p_sensor_id) l;
    v_staged := v_staged || jsonb_build_object('load_samples', v_s);
    v_inserted := v_inserted || jsonb_build_object('load_samples', v_n);
    v_any := v_any OR v_n > 0;
  END IF;

  -- 7. frequency histogram: the newer updated_ts wins
  SELECT count(*) INTO v_s FROM stage_device_freq_hist;
  IF v_s > 0 THEN
    INSERT INTO device_freq_hist (sensor_id, device_key, freq_khz, packets, updated_ts)
    SELECT p_sensor_id, s.device_key, s.freq_khz, s.packets, to_timestamp(s.updated_ts)
    FROM stage_device_freq_hist s
    JOIN devices d ON d.sensor_id = p_sensor_id AND d.device_key = s.device_key
                  AND (p_via = 'snapshot' OR d.ever_ap)
    ON CONFLICT (sensor_id, device_key, freq_khz) DO UPDATE SET
      packets = EXCLUDED.packets, updated_ts = EXCLUDED.updated_ts
      WHERE EXCLUDED.updated_ts > device_freq_hist.updated_ts;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    v_staged := v_staged || jsonb_build_object('device_freq_hist', v_s);
    v_inserted := v_inserted || jsonb_build_object('device_freq_hist', v_n);
    v_any := v_any OR v_n > 0;
  END IF;

  -- 8. client tables: snapshot only (live upload decision D3)
  IF p_via = 'snapshot' THEN
    SELECT count(*) INTO v_s FROM stage_associations;
    IF v_s > 0 THEN
      INSERT INTO associations (sensor_id, ap_key, client_mac, first_seen, last_seen)
      SELECT p_sensor_id, s.ap_key, s.client_mac::macaddr, to_timestamp(s.first_seen),
             to_timestamp(s.last_seen)
      FROM stage_associations s
      ON CONFLICT (sensor_id, ap_key, client_mac) DO UPDATE SET
        first_seen = least(associations.first_seen, EXCLUDED.first_seen),
        last_seen = greatest(associations.last_seen, EXCLUDED.last_seen);
      GET DIAGNOSTICS v_n = ROW_COUNT;
      v_staged := v_staged || jsonb_build_object('associations', v_s);
      v_inserted := v_inserted || jsonb_build_object('associations', v_n);
    END IF;
    SELECT count(*) INTO v_s FROM stage_client_bssids;
    IF v_s > 0 THEN
      INSERT INTO client_bssids (sensor_id, client_key, bssid, first_seen, last_seen)
      SELECT p_sensor_id, s.device_key, s.bssid::macaddr, to_timestamp(s.first_seen),
             to_timestamp(s.last_seen)
      FROM stage_client_bssids s
      ON CONFLICT (sensor_id, client_key, bssid) DO UPDATE SET
        first_seen = least(client_bssids.first_seen, EXCLUDED.first_seen),
        last_seen = greatest(client_bssids.last_seen, EXCLUDED.last_seen);
      GET DIAGNOSTICS v_n = ROW_COUNT;
      v_staged := v_staged || jsonb_build_object('client_bssids', v_s);
      v_inserted := v_inserted || jsonb_build_object('client_bssids', v_n);
    END IF;
    SELECT count(*) INTO v_s FROM stage_probes;
    IF v_s > 0 THEN
      INSERT INTO probes (sensor_id, device_key, ssid, first_time, last_time)
      SELECT p_sensor_id, s.device_key, s.ssid, to_timestamp(s.first_time), to_timestamp(s.last_time)
      FROM stage_probes s
      ON CONFLICT (sensor_id, device_key, ssid) DO UPDATE SET
        first_time = least(probes.first_time, EXCLUDED.first_time),
        last_time = greatest(probes.last_time, EXCLUDED.last_time);
      GET DIAGNOSTICS v_n = ROW_COUNT;
      v_staged := v_staged || jsonb_build_object('probes', v_s);
      v_inserted := v_inserted || jsonb_build_object('probes', v_n);
    END IF;
  END IF;

  -- 9. Kismet's alerts, deduplicated by hash (unchanged, D3)
  SELECT count(*) INTO v_s FROM stage_alerts;
  IF v_s > 0 THEN
    WITH ins AS (
      INSERT INTO alerts (sensor_id, hash, ts, header, class, severity, source_mac, dest_mac,
                          transmitter_mac, other_mac, channel, freq_khz, device_key, text, raw)
      SELECT p_sensor_id, s.hash, to_timestamp(s.ts), s.header, s.class, s.severity,
             s.source_mac::macaddr, s.dest_mac::macaddr, s.transmitter_mac::macaddr,
             s.other_mac::macaddr, s.channel, s.freq_khz, s.device_key, s.text, s.raw::jsonb
      FROM stage_alerts s
      ON CONFLICT (sensor_id, hash) DO NOTHING
      RETURNING ts)
    SELECT count(*), min(ts) INTO v_n, v_t FROM ins;
    v_staged := v_staged || jsonb_build_object('alerts', v_s);
    v_inserted := v_inserted || jsonb_build_object('alerts', v_n);
    v_conflicts := v_conflicts || jsonb_build_object('alerts', v_s - v_n);
    v_min := least(v_min, v_t);
    v_any := v_any OR v_n > 0;
  END IF;

  -- 10. the refresher's work: rollups from the earliest new ts (a device-only
  --     change: the current hour and the whole-sensor steps)
  IF v_any THEN
    INSERT INTO ingest_state AS i (sensor_id, dirty_from, dirty_since, dirty_at)
    VALUES (p_sensor_id, coalesce(v_min, now()), now(), now())
    ON CONFLICT (sensor_id) DO UPDATE SET
      dirty_from = least(i.dirty_from, EXCLUDED.dirty_from),
      dirty_since = coalesce(i.dirty_since, EXCLUDED.dirty_since),
      dirty_at = EXCLUDED.dirty_at;
  END IF;

  RETURN jsonb_build_object('via', p_via, 'staged', v_staged, 'inserted', v_inserted,
                            'conflicts', v_conflicts, 'min_ts', v_min);
END
$$;

CREATE OR REPLACE FUNCTION refresh_contact_periods(p_sensor_id integer, p_full boolean DEFAULT false)
RETURNS integer
LANGUAGE plpgsql
SET jit = off
AS $$
DECLARE
  v_from timestamptz;
  v_base timestamptz;
  v_n integer;
BEGIN
  IF p_full THEN
    v_base := '-infinity';
  ELSE
    SELECT least(coalesce(min(a.starts_at) FILTER (WHERE a.ends_at IS NULL), 'infinity'),
                 now() - interval '2 days')
      INTO v_from
    FROM sensor_annotations a
    WHERE a.sensor_id = p_sensor_id AND a.kind = 'unreachable';
    SELECT coalesce(max(h.received_at), '-infinity') INTO v_base
    FROM sensor_heartbeats h
    WHERE h.sensor_id = p_sensor_id AND h.received_at < v_from;
  END IF;

  DELETE FROM sensor_annotations
  WHERE sensor_id = p_sensor_id AND kind = 'unreachable' AND starts_at >= v_base;
  INSERT INTO sensor_annotations (sensor_id, starts_at, ends_at, kind, boundary, label, ref)
  SELECT p_sensor_id, g.prev, g.next, 'unreachable', false,
         'No contact with the sensor (its data arrives when contact returns)', 'sensor_heartbeats'
  FROM (SELECT h.received_at AS prev, lead(h.received_at) OVER (ORDER BY h.received_at) AS next
        FROM sensor_heartbeats h
        WHERE h.sensor_id = p_sensor_id AND h.received_at >= v_base) g
  WHERE coalesce(g.next, now()) - g.prev > interval '3 minutes';
  GET DIAGNOSTICS v_n = ROW_COUNT;
  RETURN v_n;
END
$$;

\echo [8/8] schema_meta
SELECT (SELECT count(*) FROM polls) AS polls, (SELECT count(*) FROM sensor_uploads) AS uploads,
       (SELECT count(*) FROM devices WHERE ever_ap) AS ap_devices;
INSERT INTO schema_meta (key, value) VALUES ('schema_version', '14')
  ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
COMMIT;
